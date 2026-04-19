"""
shared.copy.mapper — pure function that maps a :class:`SourceFill` to
our own target :class:`CopyTrade`.

Applies:
* sizing mode (``ratio`` / ``fixed_notional_usd`` / ``replicate``)
* max notional cap
* symbol whitelist / blacklist
* price floor (so we don't divide by zero)

Returns a :class:`MappingResult` with either a ready-to-persist trade
or a ``skip_reason`` describing why the source fill was dropped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from shared.copy.protocol import (
    SIZE_MODE_FIXED_NOTIONAL_USD,
    SIZE_MODE_RATIO,
    SIZE_MODE_REPLICATE,
    CopySource,
    CopyTrade,
    SourceFill,
)


@dataclass
class MappingResult:
    trade: Optional[CopyTrade]
    skip_reason: Optional[str] = None

    @property
    def kept(self) -> bool:
        """True when the mapper produced an actionable trade.

        A skipped mapping still produces a ``CopyTrade`` (with
        ``status='skipped'`` and a ``skip_reason``) so the audit table
        records *why* the fill was ignored — but those don't count as
        "kept" from the composer's point of view.
        """
        return self.trade is not None and self.skip_reason is None


class CopyMapper:
    """Maps source fills to our mirrored trades using the source's rule."""

    def map(
        self,
        source: CopySource,
        fill: SourceFill,
        *,
        correlation_id: Optional[str] = None,
    ) -> MappingResult:
        if not source.enabled:
            return self._skip(source, fill, "source_disabled",
                              correlation_id=correlation_id)
        if source.symbol_whitelist and fill.symbol not in source.symbol_whitelist:
            return self._skip(source, fill, "symbol_not_whitelisted",
                              correlation_id=correlation_id)
        if fill.symbol in (source.symbol_blacklist or []):
            return self._skip(source, fill, "symbol_blacklisted",
                              correlation_id=correlation_id)
        if fill.price <= 0 or fill.qty_base <= 0:
            return self._skip(source, fill, "invalid_price_or_qty",
                              correlation_id=correlation_id)

        source_notional = fill.notional_usd
        if source_notional is None or source_notional <= 0:
            source_notional = fill.price * fill.qty_base

        if source.size_mode == SIZE_MODE_RATIO:
            mapped_qty = fill.qty_base * float(source.size_value or 0.0)
            mapped_notional = source_notional * float(source.size_value or 0.0)
        elif source.size_mode == SIZE_MODE_FIXED_NOTIONAL_USD:
            mapped_notional = float(source.size_value or 0.0)
            if mapped_notional <= 0:
                return self._skip(source, fill, "fixed_notional_zero",
                                  correlation_id=correlation_id)
            mapped_qty = mapped_notional / fill.price
        elif source.size_mode == SIZE_MODE_REPLICATE:
            mapped_qty = fill.qty_base
            mapped_notional = source_notional
        else:
            return self._skip(source, fill,
                              f"unknown_size_mode:{source.size_mode!r}",
                              correlation_id=correlation_id)

        if mapped_qty <= 0 or mapped_notional <= 0:
            return self._skip(source, fill, "mapped_zero",
                              correlation_id=correlation_id)

        if source.max_notional_usd and mapped_notional > float(source.max_notional_usd):
            cap = float(source.max_notional_usd)
            scale = cap / mapped_notional
            mapped_qty *= scale
            mapped_notional = cap

        trade = CopyTrade(
            id=None,
            source_id=int(source.id or 0),
            source_fill_id=fill.fill_id,
            source_trade_ts=fill.ts,  # type: ignore[arg-type]
            symbol=fill.symbol,
            side=fill.side,
            source_price=float(fill.price),
            source_qty_base=float(fill.qty_base),
            source_notional_usd=float(source_notional),
            mapped_qty_base=round(float(mapped_qty), 10),
            mapped_notional_usd=round(float(mapped_notional), 4),
            status="pending",
            company_id=source.company_id,
            correlation_id=correlation_id,
            metadata={
                "size_mode": source.size_mode,
                "size_value": float(source.size_value or 0.0),
            },
        )
        return MappingResult(trade=trade, skip_reason=None)

    def _skip(
        self,
        source: CopySource,
        fill: SourceFill,
        reason: str,
        *,
        correlation_id: Optional[str],
    ) -> MappingResult:
        trade = CopyTrade(
            id=None,
            source_id=int(source.id or 0),
            source_fill_id=fill.fill_id,
            source_trade_ts=fill.ts,  # type: ignore[arg-type]
            symbol=fill.symbol,
            side=fill.side,
            source_price=float(fill.price),
            source_qty_base=float(fill.qty_base),
            source_notional_usd=(
                float(fill.notional_usd or (fill.price * fill.qty_base))
            ),
            mapped_qty_base=0.0,
            mapped_notional_usd=0.0,
            status="skipped",
            skip_reason=reason,
            company_id=source.company_id,
            correlation_id=correlation_id,
            metadata={
                "size_mode": source.size_mode,
                "size_value": float(source.size_value or 0.0),
            },
        )
        return MappingResult(trade=trade, skip_reason=reason)


__all__ = ["CopyMapper", "MappingResult"]
