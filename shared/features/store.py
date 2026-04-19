"""High-level FeatureStore that coordinates online + offline stores.

Example usage::

    store = FeatureStore()
    # materialize latest 2h of candles for a symbol
    store.materialize("returns_basic", "binance:BTC/USDT", candles_df)
    # pull latest features for live trading
    latest = store.get_online("returns_basic", "binance:BTC/USDT")
    # pull historical features for training / backtesting
    hist = store.get_historical(
        "returns_basic",
        ["binance:BTC/USDT"],
        start=pd.Timestamp("2025-01-01"),
        end=pd.Timestamp("2025-02-01"),
    )
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from shared.features.offline_store import OfflineStore
from shared.features.online_store import (
    InMemoryOnlineStore,
    OnlineStore,
    RedisOnlineStore,
)
from shared.features.registry import FEATURE_VIEWS, get_feature_view
from shared.features.schema import FeatureView

log = logging.getLogger("tickles.features")


class FeatureStore:
    def __init__(
        self,
        online: Optional[OnlineStore] = None,
        offline: Optional[OfflineStore] = None,
        use_in_memory_online: bool = False,
    ) -> None:
        if online is not None:
            self.online = online
        elif use_in_memory_online:
            self.online = InMemoryOnlineStore()
        else:
            try:
                self.online = RedisOnlineStore()
            except Exception as exc:  # pragma: no cover - redis missing
                log.warning("redis unavailable (%s); falling back to in-memory online store", exc)
                self.online = InMemoryOnlineStore()
        self.offline = offline or OfflineStore()

    # ---------------- write path ----------------

    def materialize(
        self,
        view_name: str,
        entity_key: str,
        candles_df: pd.DataFrame,
        params: Optional[Dict[str, Any]] = None,
        *,
        write_online: bool = True,
        write_offline: bool = True,
    ) -> Dict[str, Any]:
        """Compute features from candles and push to both stores.

        Returns a small summary dict that CLIs / services can log.
        """
        fv = get_feature_view(view_name)
        if fv.compute is None:
            raise ValueError(f"feature view {view_name!r} has no compute function")
        out = fv.compute(candles_df, entity_key, params or {})
        fv.validate_output(out)

        rows_offline = 0
        if write_offline and not out.empty:
            rows_offline = self.offline.write_batch(fv, entity_key, out)

        wrote_online = False
        if write_online and not out.empty:
            # Take last non-NaN row as the "live" vector.
            last = out.dropna(how="all").iloc[-1] if not out.dropna(how="all").empty else None
            if last is not None:
                values = {
                    k: (None if pd.isna(v) else float(v)) for k, v in last.to_dict().items()
                }
                ts = out.dropna(how="all").index[-1]
                ts_unix = float(pd.Timestamp(ts).timestamp())
                self.online.write(fv, entity_key, values, ts_unix=ts_unix)
                wrote_online = True

        return {
            "view": view_name,
            "entity": entity_key,
            "rows_offline": rows_offline,
            "wrote_online": wrote_online,
            "num_features": len(fv.features),
        }

    # ---------------- read paths ----------------

    def get_online(self, view_name: str, entity_key: str) -> Optional[Dict[str, Any]]:
        fv = get_feature_view(view_name)
        return self.online.read(fv, entity_key)

    def get_online_many(
        self, view_name: str, entity_keys: List[str]
    ) -> Dict[str, Optional[Dict[str, Any]]]:
        fv = get_feature_view(view_name)
        return self.online.read_many(fv, entity_keys)

    def get_historical(
        self,
        view_name: str,
        entity_keys: List[str],
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
        columns: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        fv = get_feature_view(view_name)
        return self.offline.get_historical_features(
            fv, entity_keys, start=start, end=end, columns=columns
        )

    # ---------------- discovery ----------------

    def views(self) -> List[FeatureView]:
        return sorted(FEATURE_VIEWS.values(), key=lambda fv: fv.name)
