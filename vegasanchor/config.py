"""Configuration. Every knob lives here; .env supplies only secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv


def load_env(path: str = ".env") -> None:
    load_dotenv(path)
    load_dotenv()  # fall back to a .env anywhere up the tree


@dataclass
class Config:
    # ---- Signal ----
    devig_method: str = "power"          # "power" (favors accuracy on favorites) or "multiplicative"
    min_edge: float = 0.04               # required edge in probability points (0.04 = 4c per share)
    fee_buffer: float = 0.005            # shaved off every edge to cover fees/slippage
    max_spread: float = 0.06             # skip markets wider than this (bid/ask, YES basis)
    min_price: float = 0.05              # ignore lottery tickets below this cost
    max_price: float = 0.97              # and near-certainties above it
    min_open_interest: float = 500.0     # USD; thin books are untradeable at size
    include_live: bool = True            # include in-progress games
    max_books_stale_min: int = 30        # warn if the odds feed looks stale

    # ---- Live in-game ----
    inplay_sigma: float = 11.0           # SD of full-game margin; fit it from logged data

    # ---- Sizing ----
    kelly_fraction: float = 0.25         # fractional Kelly; 1.0 = full Kelly (do not)
    max_pct_bankroll_per_trade: float = 0.10
    min_order_usd: float = 5.0           # exchange minimum; smaller orders are rejected
    max_positions: int = 6

    # ---- Execution ----
    dry_run: bool = True                 # NEVER defaults to live
    maker_only: bool = False             # True = post inside the spread, may not fill
    tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    manual_indicator: str = "MANUAL_ORDER_INDICATOR_AUTOMATIC"
    loop_seconds: int = 60
    kill_switch_file: str = "KILL_SWITCH"
    journal_path: str = "trade_journal.jsonl"

    # ---- Credentials / feed (from .env) ----
    pm_api_key_id: str = ""
    pm_private_key_b64: str = ""
    odds_api_key: str = ""
    bookmakers: List[str] = field(default_factory=lambda: ["fanduel"])
    odds_region: str = "us"

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        load_env()
        books = [
            b.strip().lower()
            for b in os.getenv("ODDS_API_BOOKMAKERS", "fanduel").split(",")
            if b.strip()
        ]
        cfg = cls(
            pm_api_key_id=os.getenv("PM_API_KEY_ID", "").strip(),
            pm_private_key_b64=os.getenv("PM_PRIVATE_KEY_BASE64", "").strip(),
            odds_api_key=os.getenv("ODDS_API_KEY", "").strip(),
            bookmakers=books or ["fanduel"],
            odds_region=os.getenv("ODDS_API_REGION", "us").strip() or "us",
        )
        for k, v in overrides.items():
            if v is not None and hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    @property
    def effective_min_edge(self) -> float:
        return self.min_edge + self.fee_buffer

    def apply_preset(self, name: str) -> "Config":
        """
        Named strategy presets.

        `conviction` encodes the one actionable finding from the historical
        record in RESEARCH.md: across 185 resolved positions, the smallest
        stake quartile lost 12 points against its own entry price while the
        largest beat it by 9. The consistent loser was the marginal small bet.

        So this preset does not chase the winning quartile -- it removes the
        losing tail: a higher edge bar, fewer concurrent positions, and more
        size on the ones that clear it. Stated in advance and left fixed, so
        forward results are a test rather than a curve fit.
        """
        if name == "conviction":
            self.min_edge = 0.06
            self.max_positions = 3
            self.max_pct_bankroll_per_trade = 0.20
            self.kelly_fraction = 0.25
            self.min_order_usd = max(self.min_order_usd, 10.0)
        elif name == "baseline":
            pass
        else:
            raise ValueError(f"unknown preset: {name!r}")
        return self
