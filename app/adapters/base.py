"""The Feed contract every data module depends on. Keeping this abstract means
the 12 modules never import Helius directly — so swapping or adding a provider
(SolanaTracker for rug/price) is a localized change, not a rewrite. Each method
returns plain dicts/lists in a normalized shape; a provider that can't answer
returns an empty result with .last_error set, never a crash."""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class NewToken:
    mint: str
    symbol: str = ""
    name: str = ""
    pool: str = "pump.fun"
    dev_wallet: str = ""
    launch_ts: float = 0.0
    raw: dict = field(default_factory=dict)


class Feed:
    """Abstract data feed. Subclass per provider."""
    name = "abstract"

    def __init__(self):
        self.last_error: str | None = None

    # PUMP SCOUT
    def new_tokens(self, since_ts: float, limit: int = 100) -> list[NewToken]:
        raise NotImplementedError

    # BOUNCE FINDER — recent price points (oldest→newest), for off-the-lows detection
    def price_history(self, mint: str, points: int = 30) -> list[float]:
        raise NotImplementedError

    # live price + liquidity + mcap snapshot
    def token_market(self, mint: str) -> dict:
        """-> {price, liquidity, mcap} (0.0 where unknown)."""
        raise NotImplementedError

    # WHALE / KOL WATCH — recent buys by a set of wallets on a mint (or any mint)
    def wallet_buys(self, wallets: list[str], since_ts: float) -> list[dict]:
        """-> [{wallet, mint, usd, ts}] for BUY actions only."""
        raise NotImplementedError

    # HOLDER FLOW
    def holders(self, mint: str) -> dict:
        """-> {count, new_recent, buy_pressure}  (buy_pressure in [-1,1])."""
        raise NotImplementedError

    # RUG GATE — authority + dev checks
    def rug_report(self, mint: str, dev_wallet: str = "") -> dict:
        """-> {pass: bool, reason: str, mint_authority, freeze_authority,
               lp_locked, dev_prior_rugs}."""
        raise NotImplementedError

    # BUNDLE HUNTER — coordinated-buy clusters at/near launch
    def bundle_report(self, mint: str) -> dict:
        """-> {clusters: int, bundled_pct: float, detail: str}."""
        raise NotImplementedError
