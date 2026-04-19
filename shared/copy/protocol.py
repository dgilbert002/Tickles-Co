"""
shared.copy.protocol — dataclasses for the copy-trader.

Three main types:

* :class:`CopySource`  — a leader we watch (account, wallet, feed).
* :class:`SourceFill`  — one raw fill observed on a source.
* :class:`CopyTrade`   — our mirrored trade after applying the source's
                         sizing rule / filters.

The copy service is deliberately pluggable — the live :class:`CcxtCopySource`
polls ``fetch_my_trades`` on a configured account; :class:`StaticCopySource`
is for tests and dry-runs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


SIDE_BUY = "buy"
SIDE_SELL = "sell"

SIZE_MODE_RATIO = "ratio"                  # scale by fraction of source size
SIZE_MODE_FIXED_NOTIONAL_USD = "fixed_notional_usd"
SIZE_MODE_REPLICATE = "replicate"          # copy source size 1:1

SOURCE_KIND_CCXT = "ccxt_account"
SOURCE_KIND_WALLET = "wallet"              # on-chain wallet (future)
SOURCE_KIND_FEED = "feed"                  # signal feed (future)
SOURCE_KIND_STATIC = "static"              # test / dry-run only


@dataclass
class CopySource:
    id: Optional[int]
    name: str
    kind: str
    identifier: str                        # account id / address / feed id
    venue: Optional[str] = None
    size_mode: str = SIZE_MODE_RATIO
    size_value: float = 0.1
    max_notional_usd: Optional[float] = None
    symbol_whitelist: List[str] = field(default_factory=list)
    symbol_blacklist: List[str] = field(default_factory=list)
    enabled: bool = True
    company_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    last_checked_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["last_checked_at"] = (
            self.last_checked_at.isoformat() if self.last_checked_at else None
        )
        return d


@dataclass
class SourceFill:
    """One raw fill observed on a source."""

    fill_id: str                           # unique per source
    symbol: str
    side: str                              # 'buy' | 'sell'
    price: float
    qty_base: float
    notional_usd: Optional[float] = None
    ts: Optional[datetime] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat() if self.ts else None
        return d


@dataclass
class CopyTrade:
    id: Optional[int]
    source_id: int
    source_fill_id: str
    source_trade_ts: datetime
    symbol: str
    side: str
    mapped_qty_base: float
    mapped_notional_usd: float
    source_price: Optional[float] = None
    source_qty_base: Optional[float] = None
    source_notional_usd: Optional[float] = None
    status: str = "pending"                # pending | submitted | filled | skipped | rejected
    skip_reason: Optional[str] = None
    company_id: Optional[str] = None
    correlation_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["source_trade_ts"] = (
            self.source_trade_ts.isoformat()
            if isinstance(self.source_trade_ts, datetime)
            else self.source_trade_ts
        )
        return d


__all__ = [
    "CopySource",
    "CopyTrade",
    "SourceFill",
    "SIDE_BUY",
    "SIDE_SELL",
    "SIZE_MODE_FIXED_NOTIONAL_USD",
    "SIZE_MODE_RATIO",
    "SIZE_MODE_REPLICATE",
    "SOURCE_KIND_CCXT",
    "SOURCE_KIND_FEED",
    "SOURCE_KIND_STATIC",
    "SOURCE_KIND_WALLET",
]
