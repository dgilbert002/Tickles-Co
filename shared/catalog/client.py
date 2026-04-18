"""
Catalog Python client — for agents and services.

Very thin wrapper around the Data Catalog REST API so agents write:

    from shared.catalog.client import Catalog
    cat = Catalog()
    print(cat.stats())
    print(cat.best_backtests(sort="sharpe", n=10))

instead of stitching URLs together.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import requests


class Catalog:
    def __init__(self, base: Optional[str] = None, timeout: float = 5.0):
        self.base = base or os.getenv("CATALOG_URL", "http://127.0.0.1:8765")
        self.timeout = timeout

    def _get(self, path: str, **params) -> Any:
        url = self.base.rstrip("/") + path
        r = requests.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ---- discovery ----
    def health(self) -> Dict:           return self._get("/health")
    def stats(self) -> Dict:            return self._get("/stats")
    def exchanges(self) -> List[Dict]:  return self._get("/exchanges")
    def instruments(self) -> List[Dict]:return self._get("/instruments")
    def coverage(self, symbol: str) -> Dict:
        return self._get(f"/instruments/{symbol}")
    def timeframes(self) -> List[Dict]: return self._get("/timeframes")
    def indicators(self) -> Dict:       return self._get("/indicators")
    def strategies(self) -> Dict:       return self._get("/strategies")

    # ---- backtests ----
    def best_backtests(self, *, sort="sharpe", n=20,
                       symbol=None, strategy=None) -> List[Dict]:
        kw = {"sort": sort, "n": n}
        if symbol:   kw["symbol"]   = symbol
        if strategy: kw["strategy"] = strategy
        return self._get("/backtests/top", **kw)

    def backtest(self, hash_or_id: str) -> Dict:
        return self._get(f"/backtests/lookup/{hash_or_id}")
