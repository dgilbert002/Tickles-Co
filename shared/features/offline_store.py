"""DuckDB-backed offline feature store.

Features are written to one parquet file per (feature_view,
entity_key) partition and queried through DuckDB. DuckDB can query
parquet directly so the "database" is just a directory of files.

Layout on disk::

    <root>/
      <feature_view_name>/
        entity=<entity_key>/data.parquet   (full history)

``write_batch`` appends new rows (dedupe on timestamp, keep latest).
``read_range`` returns a DataFrame for a time range.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import pandas as pd

from shared.features.schema import FeatureView

_DEFAULT_ROOT = os.environ.get(
    "TICKLES_FEATURE_OFFLINE_ROOT",
    "/opt/tickles/var/features" if os.name != "nt" else str(Path.home() / ".tickles" / "features"),
)


class OfflineStore:
    """DuckDB + parquet offline store."""

    def __init__(self, root: Optional[str] = None) -> None:
        self.root = Path(root or _DEFAULT_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)

    def _partition_dir(self, fv: FeatureView, entity_key: str) -> Path:
        safe_ek = entity_key.replace("/", "_").replace(":", "_")
        return self.root / fv.name / f"entity={safe_ek}"

    def _parquet_path(self, fv: FeatureView, entity_key: str) -> Path:
        return self._partition_dir(fv, entity_key) / "data.parquet"

    def write_batch(self, fv: FeatureView, entity_key: str, df: pd.DataFrame) -> int:
        """Append rows; dedupe on timestamp. Returns rows written."""
        if df.empty:
            return 0
        fv.validate_output(df)
        out_df = df.copy()
        out_df["entity_key"] = entity_key
        out_df.index.name = "ts"
        out_df = out_df.reset_index()

        parquet_path = self._parquet_path(fv, entity_key)
        parquet_path.parent.mkdir(parents=True, exist_ok=True)

        if parquet_path.exists():
            existing = pd.read_parquet(parquet_path)
            combined = pd.concat([existing, out_df], ignore_index=True)
            combined = combined.drop_duplicates(subset=["ts"], keep="last")
            combined = combined.sort_values("ts")
        else:
            combined = out_df.sort_values("ts")

        combined.to_parquet(parquet_path, index=False)
        return int(len(out_df))

    def read_range(
        self,
        fv: FeatureView,
        entity_key: str,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
        columns: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        parquet_path = self._parquet_path(fv, entity_key)
        if not parquet_path.exists():
            return pd.DataFrame(columns=["ts", *(columns or fv.feature_names())])
        cols = ["ts", *(columns or fv.feature_names())]
        df = pd.read_parquet(parquet_path, columns=cols)
        if start is not None:
            df = df[df["ts"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["ts"] <= pd.Timestamp(end)]
        return df.reset_index(drop=True)

    def get_historical_features(
        self,
        fv: FeatureView,
        entity_keys: List[str],
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
        columns: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """Join multiple entity keys into a single dataframe."""
        frames = []
        for ek in entity_keys:
            piece = self.read_range(fv, ek, start=start, end=end, columns=columns)
            if not piece.empty:
                piece = piece.copy()
                piece["entity_key"] = ek
                frames.append(piece)
        if not frames:
            return pd.DataFrame(columns=["ts", "entity_key", *(columns or fv.feature_names())])
        return pd.concat(frames, ignore_index=True).sort_values(["entity_key", "ts"]).reset_index(
            drop=True
        )

    def partitions(self, fv: FeatureView) -> List[str]:
        fv_root = self.root / fv.name
        if not fv_root.exists():
            return []
        out: List[str] = []
        for p in fv_root.iterdir():
            if p.is_dir() and p.name.startswith("entity="):
                out.append(p.name.removeprefix("entity="))
        return sorted(out)
