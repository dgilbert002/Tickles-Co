"""Redis-backed online feature store.

Layout (single Redis HASH per (feature_view, entity_key))::

    key:   tickles:fv:{view_name}:{entity_key}
    type:  hash
    field: <feature_name>  value: <stringified>
    field: __timestamp     value: <iso8601>
    field: __ts_unix       value: <float seconds>

TTL is applied per-key using the FeatureView.ttl_seconds value.

The store also exposes a tiny in-memory fake (``InMemoryOnlineStore``)
used by unit tests so we never need a real Redis for CI.
"""

from __future__ import annotations

import json
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, Optional

from shared.features.schema import FeatureView

_REDIS_URL = os.environ.get("TICKLES_REDIS_URL", "redis://127.0.0.1:6379/0")


def _key(view_name: str, entity_key: str) -> str:
    return f"tickles:fv:{view_name}:{entity_key}"


class OnlineStore(ABC):
    @abstractmethod
    def write(
        self,
        fv: FeatureView,
        entity_key: str,
        values: Dict[str, Any],
        ts_unix: Optional[float] = None,
    ) -> None: ...

    @abstractmethod
    def read(self, fv: FeatureView, entity_key: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def read_many(
        self, fv: FeatureView, entity_keys: Iterable[str]
    ) -> Dict[str, Optional[Dict[str, Any]]]: ...


class RedisOnlineStore(OnlineStore):
    """Production online store."""

    def __init__(self, url: Optional[str] = None) -> None:
        import redis  # lazy import so module loads without redis in CI

        self._r = redis.Redis.from_url(url or _REDIS_URL, decode_responses=True)

    def write(
        self,
        fv: FeatureView,
        entity_key: str,
        values: Dict[str, Any],
        ts_unix: Optional[float] = None,
    ) -> None:
        ts = ts_unix if ts_unix is not None else time.time()
        payload: Dict[str, str] = {
            "__timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)),
            "__ts_unix": f"{ts:.6f}",
        }
        for k, v in values.items():
            payload[k] = _serialize(v)
        k = _key(fv.name, entity_key)
        pipe = self._r.pipeline()
        pipe.hset(k, mapping=payload)
        if fv.ttl_seconds > 0:
            pipe.expire(k, fv.ttl_seconds)
        pipe.execute()

    def read(self, fv: FeatureView, entity_key: str) -> Optional[Dict[str, Any]]:
        raw = self._r.hgetall(_key(fv.name, entity_key))
        if not raw:
            return None
        if not isinstance(raw, dict):  # pragma: no cover - redis-async guard
            return None
        typed: Dict[str, str] = {str(k): str(v) for k, v in raw.items()}
        return _deserialize_row(fv, typed)

    def read_many(
        self, fv: FeatureView, entity_keys: Iterable[str]
    ) -> Dict[str, Optional[Dict[str, Any]]]:
        out: Dict[str, Optional[Dict[str, Any]]] = {}
        for ek in entity_keys:
            out[ek] = self.read(fv, ek)
        return out


class InMemoryOnlineStore(OnlineStore):
    """Test double — same semantics as Redis store but in a dict."""

    def __init__(self) -> None:
        self._mem: Dict[str, Dict[str, str]] = {}

    def write(
        self,
        fv: FeatureView,
        entity_key: str,
        values: Dict[str, Any],
        ts_unix: Optional[float] = None,
    ) -> None:
        ts = ts_unix if ts_unix is not None else time.time()
        row: Dict[str, str] = {
            "__timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)),
            "__ts_unix": f"{ts:.6f}",
        }
        for k, v in values.items():
            row[k] = _serialize(v)
        self._mem[_key(fv.name, entity_key)] = row

    def read(self, fv: FeatureView, entity_key: str) -> Optional[Dict[str, Any]]:
        raw = self._mem.get(_key(fv.name, entity_key))
        if raw is None:
            return None
        return _deserialize_row(fv, raw)

    def read_many(
        self, fv: FeatureView, entity_keys: Iterable[str]
    ) -> Dict[str, Optional[Dict[str, Any]]]:
        return {ek: self.read(fv, ek) for ek in entity_keys}


def _serialize(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return json.dumps(v)
    return json.dumps(str(v))


def _deserialize_row(fv: FeatureView, raw: Dict[str, str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for feat in fv.features:
        if feat.name not in raw:
            continue
        out[feat.name] = _try_decode(raw[feat.name])
    out["__timestamp"] = raw.get("__timestamp")
    ts_raw = raw.get("__ts_unix")
    try:
        out["__ts_unix"] = float(ts_raw) if ts_raw else None
    except ValueError:
        out["__ts_unix"] = None
    return out


def _try_decode(s: str) -> Any:
    if s == "":
        return None
    try:
        return json.loads(s)
    except Exception:
        return s
