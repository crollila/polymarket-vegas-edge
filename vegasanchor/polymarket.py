"""
Polymarket US clients.

Two separate services, and mixing them up is the first thing that breaks:

  gateway.polymarket.us  public, unauthenticated -- market discovery + prices
  api.polymarket.us      Ed25519-signed          -- balances, positions, orders

Price model (verified against the live gateway, and the part the first draft
got wrong): an NFL moneyline market is ONE yes/no instrument, not two prices.
`marketSides[0]` is the LONG side (YES = that team wins) and `marketSides[1]`
is the SHORT side, but BOTH `price` fields quote the same YES contract --
side[0].price is the best bid and side[1].price is the best ask. Reading
side[1].price as "the second team's probability" invents enormous fake edges.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

from .teams import canonical_team, short_label

GATEWAY_URL = "https://gateway.polymarket.us"
TRADE_URL = "https://api.polymarket.us"


@dataclass
class MoneylineMarket:
    """One NFL moneyline market, normalized."""
    event_slug: str
    market_slug: str
    question: str
    yes_team: str            # canonical name; YES resolves true if this team wins
    no_team: str             # canonical name; the other side
    kickoff: str             # ISO8601 gameStartTime
    live: bool
    tick_size: float
    best_bid: float = 0.0    # YES-basis
    best_ask: float = 0.0    # YES-basis
    open_interest: float = 0.0
    shares_traded: float = 0.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def yes_cost_taker(self) -> float:
        """What you pay per share to BUY YES right now (lift the offer)."""
        return self.best_ask

    @property
    def no_cost_taker(self) -> float:
        """
        What you pay per share to BUY NO right now.

        Buying NO is selling YES, so you hit the bid: cost = 1 - best_bid.
        """
        return 1.0 - self.best_bid

    def label(self) -> str:
        return f"{short_label(self.yes_team)} vs {short_label(self.no_team)}"

    def teams_mismatch(self, game) -> bool:
        """
        Last-chance guard before pricing: the sportsbook game must be the same
        two teams as this market. Cheap insurance against an indexing slip
        wiring the Chargers' fair value onto the Rams' market.
        """
        return frozenset((self.yes_team, self.no_team)) != game.teams


class GatewayClient:
    """Public market data. No credentials required."""

    def __init__(self, base_url: str = GATEWAY_URL, timeout: int = 20):
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        r = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def iter_sports_events(self, page_size: int = 200, max_pages: int = 12) -> List[Dict[str, Any]]:
        """
        Page through active sports events.

        The gateway ignores league/series filter params (verified -- passing
        leagues=nfl returns the same unfiltered page), so we pull everything
        and filter client-side.
        """
        events: List[Dict[str, Any]] = []
        seen = set()
        for page in range(max_pages):
            batch = self.get("/v1/events", params={
                "categories": "sports",
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": page_size,
                "offset": page * page_size,
            }).get("events", []) or []
            if not batch:
                break
            new = 0
            for ev in batch:
                slug = ev.get("slug")
                if slug and slug not in seen:
                    seen.add(slug)
                    events.append(ev)
                    new += 1
            if new == 0 or len(batch) < page_size:
                break
        return events

    def nfl_moneyline_markets(self, include_live: bool = True) -> List[MoneylineMarket]:
        """Every active NFL moneyline (full-game winner) market."""
        out: List[MoneylineMarket] = []
        for ev in self.iter_sports_events():
            if not self._is_nfl(ev):
                continue
            is_live = bool(ev.get("live"))
            if is_live and not include_live:
                continue
            for m in ev.get("markets", []) or []:
                parsed = self._parse_moneyline(ev, m, is_live)
                if parsed:
                    out.append(parsed)
        return out

    @staticmethod
    def _is_nfl(ev: Dict[str, Any]) -> bool:
        for p in ev.get("participants", []) or []:
            league = ((p.get("team") or {}).get("league") or "").lower()
            if league:
                return league == "nfl"
        slug = (ev.get("slug") or "").lower()
        series = (ev.get("seriesSlug") or "").lower()
        return slug.startswith("nfl-") or series.startswith("nfl")

    @staticmethod
    def _parse_moneyline(ev: Dict[str, Any], m: Dict[str, Any], is_live: bool) -> Optional[MoneylineMarket]:
        if (m.get("marketType") or "").lower() != "moneyline":
            return None
        if m.get("closed") or not m.get("active"):
            return None
        sides = m.get("marketSides") or []
        if len(sides) != 2:
            return None

        # Trust the explicit `long` flag rather than list order.
        long_side = next((s for s in sides if s.get("long")), None)
        short_side = next((s for s in sides if not s.get("long")), None)
        if long_side is None or short_side is None:
            return None

        yes_team = canonical_team(((long_side.get("team") or {}).get("name")) or "")
        no_team = canonical_team(((short_side.get("team") or {}).get("name")) or "")
        # Both sides must resolve, and to different teams, or we skip the game.
        if not yes_team or not no_team or yes_team == no_team:
            return None

        slug = m.get("slug")
        if not slug:
            return None

        return MoneylineMarket(
            event_slug=ev.get("slug") or "",
            market_slug=slug,
            question=m.get("question") or "",
            yes_team=yes_team,
            no_team=no_team,
            kickoff=m.get("gameStartTime") or ev.get("startDate") or "",
            live=is_live,
            tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
        )

    def load_bbo(self, market: MoneylineMarket) -> bool:
        """Fill in live bid/ask. Returns False if the book is unusable."""
        try:
            data = self.get(f"/v1/markets/{market.market_slug}/bbo")
        except requests.RequestException:
            return False
        md = data.get("marketData") or {}
        try:
            market.best_bid = float((md.get("bestBid") or {}).get("value"))
            market.best_ask = float((md.get("bestAsk") or {}).get("value"))
        except (TypeError, ValueError):
            return False
        market.open_interest = float(md.get("openInterest") or 0.0)
        market.shares_traded = float(md.get("sharesTraded") or 0.0)
        return 0.0 < market.best_bid <= market.best_ask < 1.0


class TradeClient:
    """Authenticated trading client for api.polymarket.us (Ed25519 signed)."""

    def __init__(self, api_key_id: str, private_key_b64: str,
                 base_url: str = TRADE_URL, timeout: int = 20):
        if not api_key_id or not private_key_b64:
            raise ValueError("Missing PM_API_KEY_ID / PM_PRIVATE_KEY_BASE64")
        self.api_key_id = api_key_id
        self.private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(private_key_b64)[:32]
        )
        self.base_url = base_url
        self.timeout = timeout
        self.session = requests.Session()

    def _headers(self, method: str, path: str) -> Dict[str, str]:
        ts = str(int(time.time() * 1000))
        sig = self.private_key.sign(f"{ts}{method.upper()}{path}".encode("utf-8"))
        return {
            "X-PM-Access-Key": self.api_key_id,
            "X-PM-Timestamp": ts,
            "X-PM-Signature": base64.b64encode(sig).decode("utf-8"),
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        r = self.session.request(
            method.upper(), f"{self.base_url}{path}",
            headers=self._headers(method, path), json=body, timeout=self.timeout,
        )
        try:
            data = r.json()
        except ValueError:
            data = {"raw": r.text}
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} {method} {path}: {data}")
        return data

    def balances(self) -> Any:
        return self.request("GET", "/v1/account/balances")

    def buying_power(self) -> float:
        for b in self.balances().get("balances", []) or []:
            if b.get("currency") == "USD":
                return float(b.get("buyingPower") or 0.0)
        return 0.0

    def positions(self) -> Any:
        return self.request("GET", "/v1/portfolio/positions")

    def open_orders(self) -> List[Dict[str, Any]]:
        return self.request("GET", "/v1/orders/open").get("orders", []) or []

    def preview_order(self, payload: Dict[str, Any]) -> Any:
        # The preview endpoint requires the payload wrapped in {"request": ...}
        # -- sending it bare returns "Request is required".
        return self.request("POST", "/v1/order/preview", {"request": payload})

    def create_order(self, payload: Dict[str, Any]) -> Any:
        return self.request("POST", "/v1/orders", payload)


def open_position_count(positions_json: Any) -> int:
    positions = positions_json.get("positions") or {}
    count = 0
    for p in positions.values():
        try:
            if int(float(p.get("netPosition", 0))) != 0:
                count += 1
        except (TypeError, ValueError):
            continue
    return count
