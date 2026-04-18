"""
JarvAIs — OHLCV Candlestick Chart Generator
Generates multi-timeframe candlestick charts from the candles DB table
using mplfinance. Used as a FALLBACK when no external chart images
(TradingView, mentor, signal provider) are available for a symbol.

Usage:
    from core.chart_generator import generate_chart_for_dossier
    result = generate_chart_for_dossier(db, "BTCUSDT", levels={"entry": 69000, ...})
    # result = {"path": "data/generated_charts/BTCUSDT_20260308_143022.png", ...}
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from datetime import datetime as _dt_utc
def utcnow():
    return _dt_utc.utcnow()

logger = logging.getLogger("jarvais.chart_generator")

CHART_OUTPUT_DIR = os.path.join("data", "generated_charts")
TIMEFRAMES_TO_PLOT = ["D1", "H4", "H1"]
CANDLE_LIMITS = {"D1": 60, "H4": 60, "H1": 80}


def _fetch_candles(db, symbol: str, timeframe: str,
                   limit: int = 60) -> Optional[pd.DataFrame]:
    """Fetch OHLCV candles from DB and return as a pandas DataFrame
    indexed by datetime (mplfinance requirement)."""
    rows = db.fetch_all("""
        SELECT candle_time, open, high, low, close, volume
        FROM candles
        WHERE symbol = %s AND timeframe = %s
        ORDER BY candle_time DESC
        LIMIT %s
    """, (symbol, timeframe, limit))

    if not rows or len(rows) < 5:
        return None

    df = pd.DataFrame(rows)
    df.columns = ["Date", "Open", "High", "Low", "Close", "Volume"]
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").set_index("Date")
    for col in ("Open", "High", "Low", "Close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0).astype(int)
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df if len(df) >= 5 else None


def _build_level_lines(levels: Dict, df: pd.DataFrame) -> List:
    """Build mplfinance hlines dict from price levels (entry, SL, TP1-3)."""
    hlines_vals = []
    hlines_colors = []
    hlines_widths = []
    label_map = {
        "entry": ("dodgerblue", 1.2),
        "stop_loss": ("red", 1.5),
        "take_profit_1": ("limegreen", 1.0),
        "take_profit_2": ("green", 0.8),
        "take_profit_3": ("darkgreen", 0.8),
        "pdh": ("orange", 0.7),
        "pdl": ("orange", 0.7),
    }

    price_min = float(df["Low"].min())
    price_max = float(df["High"].max())
    price_range = price_max - price_min
    if price_range <= 0:
        return []

    for key, price in levels.items():
        if price is None:
            continue
        price = float(price)
        if not (price_min - price_range * 0.15 <= price <= price_max + price_range * 0.15):
            continue
        color, width = label_map.get(key, ("gray", 0.6))
        hlines_vals.append(price)
        hlines_colors.append(color)
        hlines_widths.append(width)

    if not hlines_vals:
        return []
    return [{"hlines": hlines_vals, "colors": hlines_colors,
             "linewidths": hlines_widths, "linestyle": "--"}]


def generate_chart_for_dossier(db, symbol: str,
                                levels: Optional[Dict] = None,
                                timeframes: Optional[List[str]] = None,
                                ) -> Optional[Dict]:
    """Generate a composite candlestick chart image for a dossier.

    Args:
        db: DatabaseManager instance
        symbol: Trading symbol (e.g. "BTCUSDT")
        levels: Price levels to overlay: {entry, stop_loss, take_profit_1, ...}
        timeframes: List of timeframes to plot (default: D1, H4, H1)

    Returns:
        Dict with "path" (str) and "description" (str), or None on failure.
    """
    try:
        import mplfinance as mpf
        import matplotlib
        matplotlib.use("Agg")
    except ImportError:
        logger.error("[ChartGen] mplfinance not installed — pip install mplfinance")
        return None

    tfs = timeframes or TIMEFRAMES_TO_PLOT
    levels = levels or {}
    frames = {}

    for tf in tfs:
        limit = CANDLE_LIMITS.get(tf, 60)
        df = _fetch_candles(db, symbol, tf, limit)
        if df is not None:
            frames[tf] = df

    if not frames:
        logger.info(f"[ChartGen] No candle data found for {symbol} — skipping chart generation")
        return None

    os.makedirs(CHART_OUTPUT_DIR, exist_ok=True)
    timestamp = utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{symbol}_{timestamp}.png"
    filepath = os.path.join(CHART_OUTPUT_DIR, filename)

    n_panels = len(frames)
    try:
        if n_panels == 1:
            tf, df = next(iter(frames.items()))
            _plot_single(mpf, df, tf, symbol, levels, filepath)
        else:
            _plot_multi(mpf, frames, symbol, levels, filepath)

        logger.info(f"[ChartGen] Generated chart for {symbol}: {filepath} "
                    f"({n_panels} timeframes, {os.path.getsize(filepath)} bytes)")

        return {
            "path": filepath,
            "source": "generated",
            "author": "JarvAIs ChartGen",
            "description": (f"Auto-generated {'/'.join(frames.keys())} candlestick chart "
                           f"for {symbol} with {sum(len(d) for d in frames.values())} candles"),
            "analysis_id": None,
        }

    except Exception as e:
        logger.error(f"[ChartGen] Chart generation failed for {symbol}: {e}")
        return None


def _plot_single(mpf, df: pd.DataFrame, tf: str, symbol: str,
                 levels: Dict, filepath: str):
    """Plot a single-timeframe candlestick chart."""
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        rc={"font.size": 9})

    kwargs = {
        "type": "candle",
        "style": style,
        "title": f"{symbol} — {tf}",
        "volume": True if df["Volume"].sum() > 0 else False,
        "figsize": (14, 8),
        "savefig": filepath,
        "tight_layout": True,
    }

    hlines_list = _build_level_lines(levels, df)
    if hlines_list:
        kwargs["hlines"] = hlines_list[0]

    mpf.plot(df, **kwargs)
    import matplotlib.pyplot as plt
    plt.close("all")


def _plot_multi(mpf, frames: Dict[str, pd.DataFrame], symbol: str,
                levels: Dict, filepath: str):
    """Plot multi-timeframe candlestick charts stacked vertically."""
    import matplotlib.pyplot as plt

    n = len(frames)
    fig, axes = plt.subplots(n, 1, figsize=(14, 6 * n),
                              gridspec_kw={"hspace": 0.35})
    if n == 1:
        axes = [axes]

    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        rc={"font.size": 8})

    for idx, (tf, df) in enumerate(frames.items()):
        ax = axes[idx]

        plot_kwargs = {
            "type": "candle",
            "style": style,
            "ax": ax,
            "volume": False,
        }

        hlines_list = _build_level_lines(levels, df)
        if hlines_list:
            plot_kwargs["hlines"] = hlines_list[0]

        mpf.plot(df, **plot_kwargs)
        ax.set_title(f"{symbol} — {tf} ({len(df)} candles)", fontsize=10, color="white")

    fig.patch.set_facecolor("#1a1a2e")
    fig.suptitle(f"{symbol} Multi-Timeframe Analysis (Generated)",
                 fontsize=13, color="white", y=0.98)
    fig.savefig(filepath, dpi=120, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
