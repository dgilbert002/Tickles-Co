"""Minimal Twilly-style market scanner for rubicon_surgeon.

Polls Binance public endpoints (no API keys) every N seconds and writes
MARKET_STATE.json + MARKET_INDICATORS.json into the Surgeon workspace.
Format mirrors the Twilly scanner spec just enough for the Surgeon SOUL
to reason over it: prices, 24h changes, funding, mark vs index, and a
small set of indicators (RSI/EMA/ATR/momentum/Bollinger).

Stdlib-only where possible; uses urllib + numpy if available. Falls back
to pure-python math if numpy isn't installed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import List

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOG = logging.getLogger("surgeon.scanner")

ASSETS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}

BINANCE_PRICE = "https://fapi.binance.com/fapi/v1/ticker/price?symbol={s}"
BINANCE_PREM  = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol={s}"
BINANCE_24H   = "https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={s}"
BINANCE_KLINE = "https://fapi.binance.com/fapi/v1/klines?symbol={s}&interval=1m&limit=100"


def get_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "rubicon-surgeon/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i - 1]
        if diff >= 0:
            gains.append(diff); losses.append(0)
        else:
            gains.append(0); losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return round(e, 4)


def atr(klines: List[list], period: int = 14) -> float:
    if len(klines) < period + 1:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(klines)):
        high = float(klines[i][2]); low = float(klines[i][3]); prev = float(klines[i - 1][4])
        tr = max(high - low, abs(high - prev), abs(low - prev))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 4)


def bollinger(closes: List[float], period: int = 20, k: float = 2.0) -> dict:
    if len(closes) < period:
        m = sum(closes) / max(len(closes), 1)
        return {"mid": round(m, 4), "upper": round(m, 4), "lower": round(m, 4)}
    s = closes[-period:]
    m = sum(s) / period
    var = sum((x - m) ** 2 for x in s) / period
    sd = var ** 0.5
    return {"mid": round(m, 4), "upper": round(m + k * sd, 4), "lower": round(m - k * sd, 4)}


def scan_one(sym: str) -> dict:
    price = get_json(BINANCE_PRICE.format(s=sym))
    prem = get_json(BINANCE_PREM.format(s=sym))
    t24 = get_json(BINANCE_24H.format(s=sym))
    kl = get_json(BINANCE_KLINE.format(s=sym))
    closes = [float(k[4]) for k in kl]
    mark = float(prem.get("markPrice", price["price"]))
    index = float(prem.get("indexPrice", mark))
    funding = float(prem.get("lastFundingRate", 0.0))
    divergence_pct = (mark - index) / index * 100.0 if index else 0.0
    last_close = closes[-1] if closes else float(price["price"])
    return {
        "symbol": sym,
        "price": float(price["price"]),
        "markPrice": mark,
        "indexPrice": index,
        "divergencePct": round(divergence_pct, 4),
        "fundingRate": funding,
        "change24hPct": float(t24.get("priceChangePercent", 0.0)),
        "volume24h": float(t24.get("volume", 0.0)),
        "indicators": {
            "rsi14": rsi(closes),
            "ema20": ema(closes, 20),
            "ema50": ema(closes, 50),
            "atr14": atr(kl),
            "bollinger": bollinger(closes),
            "lastClose": last_close,
        },
    }


def write_outputs(out_dir: str, data: List[dict]) -> None:
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    market_state = {
        "timestamp": ts,
        "exchange_primary": "binance",
        "assets": {d["symbol"]: {k: v for k, v in d.items() if k != "indicators"} for d in data},
    }
    market_indicators = {
        "timestamp": ts,
        "assets": {d["symbol"]: d["indicators"] for d in data},
    }
    with open(os.path.join(out_dir, "MARKET_STATE.json"), "w") as f:
        json.dump(market_state, f, indent=2)
    with open(os.path.join(out_dir, "MARKET_INDICATORS.json"), "w") as f:
        json.dump(market_indicators, f, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/root/.openclaw/workspace/rubicon_surgeon")
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    LOG.info("scanner started, output=%s interval=%ss", args.output, args.interval)
    while not stop["flag"]:
        data: List[dict] = []
        for _, sym in ASSETS.items():
            try:
                data.append(scan_one(sym))
            except Exception as exc:
                LOG.warning("%s failed: %s", sym, exc)
        if data:
            try:
                write_outputs(args.output, data)
                LOG.info("wrote MARKET_STATE + MARKET_INDICATORS for %d assets", len(data))
            except Exception as exc:
                LOG.warning("write failed: %s", exc)
        for _ in range(args.interval):
            if stop["flag"]:
                break
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
