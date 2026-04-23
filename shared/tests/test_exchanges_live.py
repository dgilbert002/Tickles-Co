"""
Module: test_exchanges_live
Purpose: Live integration test across 5 exchanges — connectivity, candles,
         backtesting, paper trading, and validation.
Location: /opt/tickles/shared/tests/test_exchanges_live.py

Run: python3 shared/tests/test_exchanges_live.py
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import ccxt.async_support as ccxt_async  # type: ignore
    _CCXT_AVAILABLE = True
except ImportError:
    ccxt_async = None  # type: ignore
    _CCXT_AVAILABLE = False

from shared.utils.config import load_env

load_env()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exchange configuration
# ---------------------------------------------------------------------------

EXCHANGE_CONFIG = {
    "bybit": {
        "ccxt_class": "bybit",
        "api_key_env": "BYBIT_DEMO_API_KEY",
        "secret_env": "BYBIT_DEMO_API_SECRET",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "sandbox": True,
    },
    "binance": {
        "ccxt_class": "binance",
        "api_key_env": "BINANCE_API_KEY",
        "secret_env": "BINANCE_SECRET",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "sandbox": False,
    },
    "blofin": {
        "ccxt_class": "blofin",
        "api_key_env": "BLOFIN_DEMO_API_KEY",
        "secret_env": "BLOFIN_DEMO_API_SECRET",
        "passphrase_env": "BLOFIN_DEMO_API_PHRASE",
        "symbol": "BTC/USDT:USDT",
        "timeframe": "1h",
        "sandbox": False,
    },
    "bitget": {
        "ccxt_class": "bitget",
        "api_key_env": "BITGET_API_KEY",
        "secret_env": "BITGET_API_SECRET",
        "passphrase_env": "BITGET_API_PHASE",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "sandbox": False,
    },
    "capital_com": {
        "ccxt_class": None,
        "adapter": "capital",
        "symbol": "BTCUSD",
        "timeframe": "1h",
    },
}


# ---------------------------------------------------------------------------
# Report collector
# ---------------------------------------------------------------------------

class ExchangeReport:
    def __init__(self) -> None:
        self.results: Dict[str, Dict[str, Any]] = {}
        self.api_key_issues: List[str] = []

    def add(self, exchange: str, category: str, result: Dict[str, Any]) -> None:
        if exchange not in self.results:
            self.results[exchange] = {}
        self.results[exchange][category] = result

    def to_text(self) -> str:
        lines = [
            "=" * 80,
            "EXCHANGE INTEGRATION TEST REPORT",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            "=" * 80,
        ]
        for exchange, categories in self.results.items():
            lines.append("")
            lines.append("-" * 60)
            lines.append(f"EXCHANGE: {exchange.upper()}")
            lines.append("-" * 60)
            for category, result in categories.items():
                status = result.get("status", "unknown")
                icon = "✅" if status == "ok" else "❌" if status == "error" else "⚠️"
                lines.append(f"  {icon} {category}: {status}")
                if "detail" in result:
                    lines.append(f"     Detail: {result['detail']}")
                if "data" in result and result["data"]:
                    for k, v in result["data"].items():
                        lines.append(f"     {k}: {v}")
        return "\n".join(lines)


report = ExchangeReport()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_exchange(name: str, cfg: Dict[str, Any]) -> Optional[Any]:
    if ccxt_async is None: return None
    ccxt_class = cfg.get("ccxt_class")
    if not ccxt_class or not hasattr(ccxt_async, ccxt_class): return None
    api_key = os.environ.get(cfg.get("api_key_env", ""), "")
    secret = os.environ.get(cfg.get("secret_env", ""), "")
    passphrase = os.environ.get(cfg.get("passphrase_env", ""), "")
    exchange_kwargs = {"enableRateLimit": True, "timeout": 30000}
    if api_key and secret:
        exchange_kwargs["apiKey"] = api_key
        exchange_kwargs["secret"] = secret
        if passphrase: exchange_kwargs["password"] = passphrase
    if cfg.get("sandbox"): exchange_kwargs["options"] = {"sandboxMode": True}
    try:
        return getattr(ccxt_async, ccxt_class)(exchange_kwargs)
    except Exception: return None

def _create_public_exchange(cfg: Dict[str, Any]) -> Optional[Any]:
    if ccxt_async is None: return None
    ccxt_class = cfg.get("ccxt_class")
    if not ccxt_class or not hasattr(ccxt_async, ccxt_class): return None
    try:
        return getattr(ccxt_async, ccxt_class)({"enableRateLimit": True, "timeout": 30000})
    except Exception: return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_connectivity(name: str, cfg: Dict[str, Any]) -> None:
    if cfg.get("adapter") == "capital":
        try:
            from shared.connectors.capital_adapter import CapitalAdapter
            email, password, api_key = os.environ.get("CAPITAL_EMAIL"), os.environ.get("CAPITAL_PASSWORD"), os.environ.get("CAPITAL_API_KEY")
            if not all([email, password, api_key]):
                report.add(name, "connectivity", {"status": "warning", "detail": "Missing credentials"})
                return
            adapter = CapitalAdapter(environment="demo")
            await adapter.authenticate(email, password, api_key)
            instruments = await adapter.get_instruments()
            await adapter.close()
            report.add(name, "connectivity", {"status": "ok", "detail": f"Connected — {len(instruments)} instruments"})
        except Exception as exc:
            report.add(name, "connectivity", {"status": "error", "detail": str(exc)})
        return

    exchange = _create_exchange(name, cfg)
    if not exchange:
        report.add(name, "connectivity", {"status": "error", "detail": "No exchange instance"})
        return
    try:
        await exchange.load_markets()
        report.add(name, "connectivity", {"status": "ok", "detail": f"Loaded {len(exchange.markets)} markets"})
    except Exception as exc:
        report.add(name, "connectivity", {"status": "warning", "detail": f"Auth failed: {exc}"})
        pub = _create_public_exchange(cfg)
        if pub:
            try:
                await pub.load_markets()
                report.add(name, "connectivity_public", {"status": "ok", "detail": f"Public OK — {len(pub.markets)} markets"})
            except Exception as exc2:
                report.add(name, "connectivity_public", {"status": "error", "detail": str(exc2)})
            finally: await pub.close()
    finally:
        try: await exchange.close()
        except Exception: pass

async def test_fetch_candles(name: str, cfg: Dict[str, Any]) -> None:
    if cfg.get("adapter") == "capital":
        try:
            from shared.connectors.capital_adapter import CapitalAdapter
            email, password, api_key = os.environ.get("CAPITAL_EMAIL"), os.environ.get("CAPITAL_PASSWORD"), os.environ.get("CAPITAL_API_KEY")
            adapter = CapitalAdapter(environment="demo")
            await adapter.authenticate(email, password, api_key)
            candles = await adapter.fetch_ohlcv(cfg["symbol"], timeframe=cfg["timeframe"], limit=50)
            await adapter.close()
            report.add(name, "candles", {"status": "ok", "detail": f"Fetched {len(candles)} candles"})
        except Exception as exc:
            report.add(name, "candles", {"status": "error", "detail": str(exc)})
        return

    exchange = _create_exchange(name, cfg)
    if not exchange:
        report.add(name, "candles", {"status": "error", "detail": "No exchange instance"})
        return
    try:
        candles = await exchange.fetch_ohlcv(cfg["symbol"], cfg["timeframe"], limit=50)
        report.add(name, "candles", {"status": "ok", "detail": f"Fetched {len(candles)} candles"})
    except Exception:
        pub = _create_public_exchange(cfg)
        if pub:
            try:
                candles = await pub.fetch_ohlcv(cfg["symbol"], cfg["timeframe"], limit=50)
                report.add(name, "candles", {"status": "ok", "detail": f"Public: fetched {len(candles)} candles"})
            except Exception as exc2:
                report.add(name, "candles", {"status": "error", "detail": str(exc2)})
            finally: await pub.close()
    finally:
        try: await exchange.close()
        except Exception: pass

async def test_fetch_ticker(name: str, cfg: Dict[str, Any]) -> None:
    if cfg.get("adapter") == "capital":
        report.add(name, "ticker", {"status": "warning", "detail": "Not supported"})
        return
    exchange = _create_exchange(name, cfg)
    if not exchange:
        report.add(name, "ticker", {"status": "error", "detail": "No exchange instance"})
        return
    try:
        ticker = await exchange.fetch_ticker(cfg["symbol"])
        report.add(name, "ticker", {"status": "ok", "detail": f"Ticker: {ticker.get('last')}"})
    except Exception:
        pub = _create_public_exchange(cfg)
        if pub:
            try:
                ticker = await pub.fetch_ticker(cfg["symbol"])
                report.add(name, "ticker", {"status": "ok", "detail": f"Public Ticker: {ticker.get('last')}"})
            except Exception as exc2:
                report.add(name, "ticker", {"status": "error", "detail": str(exc2)})
            finally: await pub.close()
    finally:
        try: await exchange.close()
        except Exception: pass

async def test_fetch_balance(name: str, cfg: Dict[str, Any]) -> None:
    if cfg.get("adapter") == "capital":
        try:
            from shared.connectors.capital_adapter import CapitalAdapter
            email, password, api_key = os.environ.get("CAPITAL_EMAIL"), os.environ.get("CAPITAL_PASSWORD"), os.environ.get("CAPITAL_API_KEY")
            if not all([email, password, api_key]):
                report.add(name, "balance", {"status": "warning", "detail": "Missing credentials"})
                return
            adapter = CapitalAdapter(environment="demo")
            await adapter.authenticate(email, password, api_key)
            balance = await adapter.fetch_balance()
            await adapter.close()
            # Capital.com usually uses USD or EUR as base currency for the account
            currency = balance.get("info", {}).get("accounts", [{}])[0].get("currency", "USD")
            report.add(name, "balance", {"status": "ok", "detail": f"Balance OK: {balance.get(currency, {}).get('free', 0)} {currency}"})
        except Exception as exc:
            report.add(name, "balance", {"status": "error", "detail": str(exc)})
        return

    api_key = os.environ.get(cfg.get("api_key_env", ""), "")
    if not api_key:
        report.add(name, "balance", {"status": "warning", "detail": "No API key"})
        return
    exchange = _create_exchange(name, cfg)
    if not exchange:
        report.add(name, "balance", {"status": "error", "detail": "No exchange instance"})
        return
    try:
        balance = await exchange.fetch_balance()
        report.add(name, "balance", {"status": "ok", "detail": f"Balance OK: {balance.get('USDT', {}).get('free', 0)} USDT"})
    except Exception as exc:
        report.add(name, "balance", {"status": "error", "detail": str(exc)})
    finally:
        try: await exchange.close()
        except Exception: pass

async def _run_backtest_from_candles(candles: List, symbol: str, source: str, timeframe: str) -> Dict[str, Any]:
    import pandas as pd
    import sys
    import os
    
    # Ensure /opt/tickles is in sys.path so 'shared' can be imported correctly
    if "/opt/tickles" not in sys.path:
        sys.path.insert(0, "/opt/tickles")
        
    try:
        from shared.backtest.engine import BacktestConfig, run_backtest
        from shared.backtest.strategies.single_indicator import sma_cross
    except ImportError as e:
        # Fallback for when shared.backtest is not in the path or has issues
        return {"status": "error", "detail": f"Import failed: {e}"}
        
    # Convert Candle objects to list of dicts if needed
    processed_candles = []
    for c in candles:
        if hasattr(c, "timestamp"):
            processed_candles.append({
                "snapshotTime": c.timestamp,
                "openPrice": float(c.open),
                "highPrice": float(c.high),
                "lowPrice": float(c.low),
                "closePrice": float(c.close),
                "volume": float(c.volume)
            })
        else:
            # Already a list/dict from CCXT
            processed_candles.append({
                "snapshotTime": pd.to_datetime(c[0], unit="ms"),
                "openPrice": float(c[1]),
                "highPrice": float(c[2]),
                "lowPrice": float(c[3]),
                "closePrice": float(c[4]),
                "volume": float(c[5])
            })

    df = pd.DataFrame(processed_candles)
    df["snapshotTime"] = pd.to_datetime(df["snapshotTime"], utc=True)
    df.set_index("snapshotTime", inplace=True, drop=False)
    cfg = BacktestConfig(
        symbol=symbol, source=source, timeframe=timeframe,
        start_date=str(df.index[0]), end_date=str(df.index[-1]),
        direction="long", initial_capital=10000.0, fee_taker_bps=10.0, slippage_bps=5.0,
        strategy_name="sma_cross", indicator_name="sma", indicator_params={"fast_period": 10, "slow_period": 30},
    )
    result = run_backtest(df, sma_cross, cfg)
    return {"status": "ok", "total_trades": result.num_trades, "total_return_pct": round(result.pnl_pct, 2)}

async def test_backtest_with_candles(name: str, cfg: Dict[str, Any]) -> None:
    symbol, timeframe = cfg["symbol"], cfg["timeframe"]
    if cfg.get("adapter") == "capital":
        try:
            from shared.connectors.capital_adapter import CapitalAdapter
            email, password, api_key = os.environ.get("CAPITAL_EMAIL"), os.environ.get("CAPITAL_PASSWORD"), os.environ.get("CAPITAL_API_KEY")
            adapter = CapitalAdapter(environment="demo")
            await adapter.authenticate(email, password, api_key)
            candles = await adapter.fetch_ohlcv(symbol, timeframe=timeframe, limit=200)
            await adapter.close()
            if candles and len(candles) >= 50:
                bt_result = await _run_backtest_from_candles(candles, symbol, name, timeframe)
                if bt_result.get("status") == "ok":
                    report.add(name, "backtest", {"status": "ok", "detail": "Backtest OK", "data": bt_result})
                else:
                    report.add(name, "backtest", {"status": "error", "detail": bt_result.get("detail")})
        except Exception as exc:
            report.add(name, "backtest", {"status": "error", "detail": str(exc)})
        return
    exchange = _create_exchange(name, cfg)
    if not exchange:
        report.add(name, "backtest", {"status": "error", "detail": "No exchange instance"})
        return
    try:
        candles = await exchange.fetch_ohlcv(symbol, timeframe, limit=200)
        if candles and len(candles) >= 50:
            bt_result = await _run_backtest_from_candles(candles, symbol, name, timeframe)
            if bt_result.get("status") == "ok":
                report.add(name, "backtest", {"status": "ok", "detail": "Backtest OK", "data": bt_result})
            else:
                report.add(name, "backtest", {"status": "error", "detail": bt_result.get("detail")})
    except Exception:
        pub = _create_public_exchange(cfg)
        if pub:
            try:
                candles = await pub.fetch_ohlcv(symbol, timeframe, limit=200)
                if candles and len(candles) >= 50:
                    bt_result = await _run_backtest_from_candles(candles, symbol, name, timeframe)
                    if bt_result.get("status") == "ok":
                        report.add(name, "backtest", {"status": "ok", "detail": "Public Backtest OK", "data": bt_result})
                    else:
                        report.add(name, "backtest", {"status": "error", "detail": bt_result.get("detail")})
            except Exception as exc2:
                report.add(name, "backtest", {"status": "error", "detail": str(exc2)})
            finally: await pub.close()
    finally:
        try: await exchange.close()
        except Exception: pass

async def test_paper_trading(name: str, cfg: Dict[str, Any]) -> None:
    try:
        from shared.execution.paper import PaperExecutionAdapter
        from shared.execution.protocol import ExecutionIntent, DIRECTION_LONG, ORDER_TYPE_MARKET
        adapter = PaperExecutionAdapter()
        intent = ExecutionIntent(
            company_id="test_company", strategy_id=999, agent_id="test_agent",
            exchange=name, account_id_external=f"paper_test_{name}",
            symbol=cfg["symbol"], direction=DIRECTION_LONG, order_type=ORDER_TYPE_MARKET,
            quantity=0.001, requested_price=50000.0, time_in_force="IOC",
            client_order_id=f"test_{name}_{int(time.time())}",
        )
        events = await adapter.submit(intent)
        status = "unknown"
        for ev in events:
            if hasattr(ev, "status"): status = ev.status
            elif isinstance(ev, dict) and "status" in ev: status = ev["status"]
        report.add(name, "paper_trading", {"status": "ok" if status in ("filled", "accepted", "new") else "warning", "detail": f"Paper order status: {status}"})
    except Exception as exc:
        report.add(name, "paper_trading", {"status": "error", "detail": str(exc)})

async def test_validation_engine(name: str, cfg: Dict[str, Any]) -> None:
    try:
        import sys
        if "/opt/tickles" not in sys.path:
            sys.path.insert(0, "/opt/tickles")
            
        try:
            from shared.trading.validation import ValidationEngine, VERDICT_CONTINUE
        except ImportError as e:
            report.add(name, "validation", {"status": "error", "detail": f"Import failed: {e}"})
            return
            
        from unittest.mock import MagicMock
        engine = ValidationEngine("test")
        engine.ch = MagicMock()
        agg_continue = {"total_validated": 10, "rule1_pass_rate": 95.0, "avg_pnl_delta_pct": 0.02}
        v1 = engine._compute_verdict(agg_continue, 5)
        report.add(name, "validation", {"status": "ok" if v1 == VERDICT_CONTINUE else "error", "detail": "Validation logic OK"})
    except Exception as exc:
        report.add(name, "validation", {"status": "error", "detail": str(exc)})

async def run_all_tests() -> None:
    if ccxt_async is None: return
    print("=" * 80 + "\nSTARTING EXCHANGE INTEGRATION TESTS\n" + "=" * 80)
    for name, cfg in EXCHANGE_CONFIG.items():
        print(f"\n>>> Testing {name.upper()}...")
        await test_connectivity(name, cfg)
        await test_fetch_candles(name, cfg)
        await test_fetch_ticker(name, cfg)
        await test_fetch_balance(name, cfg)
        await test_backtest_with_candles(name, cfg)
        await test_paper_trading(name, cfg)
        await test_validation_engine(name, cfg)
        print(f"  Done with {name.upper()}")
    print("\n" + report.to_text())

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run_all_tests())
