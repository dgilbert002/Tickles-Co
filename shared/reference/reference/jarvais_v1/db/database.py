"""
JarvAIs Database Manager
Handles all MySQL database operations: connection pooling, CRUD operations,
and query helpers for all tables defined in schema.sql.
"""

import json
import logging
from datetime import datetime, date
from typing import Optional, Dict, Any, List, Tuple
from decimal import Decimal
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB

from core.config import get_config

logger = logging.getLogger("jarvais.database")


class DatabaseManager:
    """
    Thread-safe database manager with MySQL connection pooling.
    MySQL ONLY — no SQLite, no fallback.
    """

    def __init__(self):
        cfg = get_config().database
        # Phase 6b: increased pool size for parallel workers
        # (8 candle + 6 dossier + 2 build + 2 sync + tracker + dashboard + misc)
        self._pool = PooledDB(
            creator=pymysql,
            maxconnections=40,
            mincached=4,
            maxcached=20,
            blocking=True,
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=10,
            read_timeout=60,
            write_timeout=60,
            setsession=["SET time_zone = '+00:00'"]
        )
        logger.info(f"Database pool initialized: {cfg.host}:{cfg.port}/{cfg.database} "
                    f"(pool max={self._pool._maxconnections}, session TZ=UTC)")

    def get_pool_stats(self) -> Dict:
        """Return connection pool utilisation metrics (Phase 6b)."""
        pool = self._pool
        return {
            "max_connections": pool._maxconnections,
            "min_cached": pool._mincached,
            "max_cached": pool._maxcached,
            "idle": pool._idle_cache.qsize() if hasattr(pool, "_idle_cache") else -1,
        }

    @contextmanager
    def get_connection(self):
        """Get a connection from the pool with automatic return."""
        conn = self._pool.connection()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def get_cursor(self):
        """Get a cursor with automatic connection management."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            try:
                yield cursor
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cursor.close()

    def execute(self, sql: str, params: tuple = None) -> int:
        """Execute a single SQL statement. Returns affected row count."""
        with self.get_cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount

    def execute_returning_id(self, sql: str, params: tuple = None) -> int:
        """Execute an INSERT and return the auto-generated ID."""
        with self.get_cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.lastrowid

    @staticmethod
    def _convert_row(row: Dict) -> Dict:
        """Convert Decimal values to float for JSON serialization and arithmetic."""
        if row is None:
            return None
        return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items()}

    def fetch_one(self, sql: str, params: tuple = None) -> Optional[Dict]:
        """Fetch a single row as a dictionary."""
        with self.get_cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
            return self._convert_row(row) if row else None

    def fetch_all(self, sql: str, params: tuple = None) -> List[Dict]:
        """Fetch all rows as a list of dictionaries."""
        with self.get_cursor() as cursor:
            cursor.execute(sql, params)
            return [self._convert_row(r) for r in cursor.fetchall()]

    def init_schema(self, schema_path: str = None):
        """Execute the schema.sql file to create all tables."""
        if schema_path is None:
            from pathlib import Path
            schema_path = Path(__file__).parent / "schema.sql"

        with open(schema_path, "r") as f:
            schema_sql = f.read()

        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Split by semicolons and execute each statement
            statements = [s.strip() for s in schema_sql.split(";") if s.strip()]
            for stmt in statements:
                if stmt and not stmt.startswith("--"):
                    try:
                        cursor.execute(stmt)
                    except pymysql.err.OperationalError as e:
                        # Skip "database exists" or "table exists" errors
                        if e.args[0] not in (1007, 1050):
                            raise
            conn.commit()
            cursor.close()
        logger.info("Database schema initialized successfully")

    # ─────────────────────────────────────────────────────────────────
    # EA Signals
    # ─────────────────────────────────────────────────────────────────

    def insert_signal(self, signal_data: Dict[str, Any]) -> int:
        """Insert a new EA signal and return its ID."""
        sql = """
            INSERT INTO ea_signals (
                account_id, timestamp, symbol, direction, votes_long, votes_short,
                vote_ratio, market_state, htf_trend, htf_context, strategy_details,
                indicator_values, ea_stop_loss, ea_take_profit, ea_lot_size,
                current_price, spread_points, status, hypothetical_entry
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """
        params = (
            signal_data["account_id"],
            signal_data.get("timestamp", datetime.utcnow()),
            signal_data["symbol"],
            signal_data["direction"],
            signal_data.get("votes_long", 0),
            signal_data.get("votes_short", 0),
            signal_data.get("vote_ratio", 0),
            signal_data.get("market_state"),
            signal_data.get("htf_trend"),
            signal_data.get("htf_context"),
            json.dumps(signal_data.get("strategy_details")) if signal_data.get("strategy_details") else None,
            json.dumps(signal_data.get("indicator_values")) if signal_data.get("indicator_values") else None,
            signal_data.get("ea_stop_loss"),
            signal_data.get("ea_take_profit"),
            signal_data.get("ea_lot_size"),
            signal_data.get("current_price"),
            signal_data.get("spread_points"),
            "received",
            signal_data.get("current_price")  # hypothetical_entry = current price at signal time
        )
        signal_id = self.execute_returning_id(sql, params)
        logger.info(f"[{signal_data['account_id']}] Signal #{signal_id} inserted: "
                     f"{signal_data['direction']} {signal_data['symbol']}")
        return signal_id

    def update_signal_status(self, signal_id: int, status: str):
        """Update the processing status of a signal."""
        self.execute(
            "UPDATE ea_signals SET status = %s WHERE id = %s",
            (status, signal_id)
        )

    def update_signal_hypothetical(self, signal_id: int, exit_price: float, pnl: float):
        """Update the hypothetical outcome of a signal for EA baseline tracking."""
        self.execute(
            "UPDATE ea_signals SET hypothetical_exit = %s, hypothetical_pnl = %s, "
            "hypothetical_closed = TRUE WHERE id = %s",
            (exit_price, pnl, signal_id)
        )

    def get_signal(self, signal_id: int) -> Optional[Dict]:
        """Get a signal by ID."""
        return self.fetch_one("SELECT * FROM ea_signals WHERE id = %s", (signal_id,))

    def get_recent_signals(self, account_id: str, limit: int = 50) -> List[Dict]:
        """Get recent signals for an account."""
        return self.fetch_all(
            "SELECT * FROM ea_signals WHERE account_id = %s ORDER BY timestamp DESC LIMIT %s",
            (account_id, limit)
        )

    # ─────────────────────────────────────────────────────────────────
    # AI Decisions
    # ─────────────────────────────────────────────────────────────────

    def insert_decision(self, decision_data: Dict[str, Any]) -> int:
        """Insert an AI decision and return its ID."""
        sql = """
            INSERT INTO ai_decisions (
                signal_id, account_id, timestamp, model_used, decision, confidence,
                trader_reasoning, coach_assessment, coach_challenges,
                trader_response_to_challenges, full_dossier, full_dialogue,
                suggested_lot_modifier, suggested_tp_levels, suggested_sl_level,
                is_free_trade, risk_mode, token_count_input, token_count_output,
                api_cost_usd, latency_ms
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """
        params = (
            decision_data["signal_id"],
            decision_data["account_id"],
            decision_data.get("timestamp", datetime.utcnow()),
            decision_data["model_used"],
            decision_data["decision"],
            decision_data["confidence"],
            decision_data.get("trader_reasoning"),
            decision_data.get("coach_assessment"),
            decision_data.get("coach_challenges"),
            decision_data.get("trader_response_to_challenges"),
            decision_data.get("full_dossier"),
            decision_data.get("full_dialogue"),
            decision_data.get("suggested_lot_modifier", 1.0),
            json.dumps(decision_data.get("suggested_tp_levels")) if decision_data.get("suggested_tp_levels") else None,
            decision_data.get("suggested_sl_level"),
            decision_data.get("is_free_trade", False),
            decision_data.get("risk_mode"),
            decision_data.get("token_count_input", 0),
            decision_data.get("token_count_output", 0),
            decision_data.get("api_cost_usd", 0),
            decision_data.get("latency_ms", 0)
        )
        decision_id = self.execute_returning_id(sql, params)
        logger.info(f"[{decision_data['account_id']}] Decision #{decision_id}: "
                     f"{decision_data['decision']} (confidence: {decision_data['confidence']})")
        return decision_id

    def get_decision(self, decision_id: int) -> Optional[Dict]:
        """Get a decision by ID."""
        return self.fetch_one("SELECT * FROM ai_decisions WHERE id = %s", (decision_id,))

    def get_decisions_for_signal(self, signal_id: int) -> List[Dict]:
        """Get all decisions for a specific signal."""
        return self.fetch_all(
            "SELECT * FROM ai_decisions WHERE signal_id = %s ORDER BY timestamp",
            (signal_id,)
        )

    # ─────────────────────────────────────────────────────────────────
    # Trades
    # ─────────────────────────────────────────────────────────────────

    def insert_trade(self, trade_data: Dict[str, Any]) -> int:
        """Insert a new trade and return its ID."""
        sql = """
            INSERT INTO trades (
                decision_id, signal_id, account_id, is_live, is_free_trade,
                magic_number, mt5_ticket, symbol, direction, entry_price,
                stop_loss, original_stop_loss, take_profit_1, take_profit_2,
                take_profit_3, lot_size, original_lot_size, open_time,
                ai_confidence_at_entry, balance_at_entry, risk_percent_used,
                maturity_phase, alpha_sources, status
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, 'open'
            )
        """
        params = (
            trade_data.get("decision_id"),
            trade_data.get("signal_id"),
            trade_data["account_id"],
            trade_data.get("is_live", False),
            trade_data.get("is_free_trade", False),
            trade_data["magic_number"],
            trade_data.get("mt5_ticket"),
            trade_data["symbol"],
            trade_data["direction"],
            trade_data["entry_price"],
            trade_data["stop_loss"],
            trade_data["stop_loss"],  # original_stop_loss = initial stop_loss
            trade_data.get("take_profit_1"),
            trade_data.get("take_profit_2"),
            trade_data.get("take_profit_3"),
            trade_data["lot_size"],
            trade_data["lot_size"],  # original_lot_size = initial lot_size
            trade_data.get("open_time", datetime.utcnow()),
            trade_data.get("ai_confidence_at_entry"),
            trade_data.get("balance_at_entry"),
            trade_data.get("risk_percent_used"),
            trade_data.get("maturity_phase", 1),
            trade_data.get("alpha_sources")  # JSON string of alpha sources
        )
        trade_id = self.execute_returning_id(sql, params)
        logger.info(f"[{trade_data['account_id']}] Trade #{trade_id} opened: "
                     f"{trade_data['direction']} {trade_data['symbol']} @ {trade_data['entry_price']}")
        return trade_id

    def close_trade(self, trade_id: int, exit_price: float, pnl_usd: float,
                    pnl_pips: float, close_reason: str, commission: float = 0,
                    swap: float = 0):
        """Close a trade with final P&L."""
        self.execute(
            """UPDATE trades SET
                exit_price = %s, pnl_usd = %s, pnl_pips = %s,
                close_reason = %s, commission = %s, swap = %s,
                close_time = %s, status = 'closed'
            WHERE id = %s""",
            (exit_price, pnl_usd, pnl_pips, close_reason, commission, swap,
             datetime.utcnow(), trade_id)
        )
        logger.info(f"Trade #{trade_id} closed: {close_reason}, P&L: ${pnl_usd:.2f}")

    def update_trade_sl(self, trade_id: int, new_sl: float, be_triggered: bool = False):
        """Update stop loss for a trade (e.g., break-even move)."""
        if be_triggered:
            self.execute(
                "UPDATE trades SET stop_loss = %s, be_triggered = TRUE, be_trigger_time = %s WHERE id = %s",
                (new_sl, datetime.utcnow(), trade_id)
            )
        else:
            self.execute(
                "UPDATE trades SET stop_loss = %s WHERE id = %s",
                (new_sl, trade_id)
            )

    def update_trade_lot_size(self, trade_id: int, new_lot_size: float,
                               partial_close_pct: float, partial_pnl: float):
        """Update lot size after partial close."""
        self.execute(
            """UPDATE trades SET
                lot_size = %s, partial_close_pct = %s, partial_close_pnl = %s,
                status = 'partial_closed'
            WHERE id = %s""",
            (new_lot_size, partial_close_pct, partial_pnl, trade_id)
        )

    def get_open_trades(self, account_id: str) -> List[Dict]:
        """Get all open trades for an account."""
        return self.fetch_all(
            "SELECT * FROM trades WHERE account_id = %s AND status IN ('open', 'partial_closed') "
            "ORDER BY open_time",
            (account_id,)
        )

    def get_trade(self, trade_id: int) -> Optional[Dict]:
        """Get a trade by ID."""
        return self.fetch_one("SELECT * FROM trades WHERE id = %s", (trade_id,))

    def get_trades_by_date_range(self, account_id: str, start_date: date,
                                  end_date: date) -> List[Dict]:
        """Get trades within a date range."""
        return self.fetch_all(
            "SELECT * FROM trades WHERE account_id = %s AND DATE(open_time) BETWEEN %s AND %s "
            "ORDER BY open_time",
            (account_id, start_date, end_date)
        )

    def get_all_closed_trades(self, account_id: str, limit: int = 500) -> List[Dict]:
        """Get closed trades for performance analysis."""
        return self.fetch_all(
            "SELECT * FROM trades WHERE account_id = %s AND status = 'closed' "
            "ORDER BY close_time DESC LIMIT %s",
            (account_id, limit)
        )

    def get_trade_count(self, account_id: str) -> int:
        """Get total number of closed trades for maturity assessment."""
        result = self.fetch_one(
            "SELECT COUNT(*) as cnt FROM trades WHERE account_id = %s AND status = 'closed'",
            (account_id,)
        )
        return result["cnt"] if result else 0

    # ─────────────────────────────────────────────────────────────────
    # Trade Lessons
    # ─────────────────────────────────────────────────────────────────

    def insert_lesson(self, lesson_data: Dict[str, Any]) -> int:
        """Insert a post-mortem lesson and return its ID.
        Supports both legacy trade_id and dossier-based dossier_id workflows."""
        sql = """
            INSERT INTO trade_lessons (
                trade_id, dossier_id, signal_id, symbol, account_id,
                model_used, outcome, pnl_usd,
                what_worked, what_failed, lesson_text,
                root_cause, optimal_trade_summary,
                confidence_calibration,
                full_post_mortem, technical_factors, timing_factors,
                fundamental_factors, decision_quality, execution_factors,
                qdrant_vector_id, token_count, api_cost_usd
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """
        params = (
            lesson_data.get("trade_id"),
            lesson_data.get("dossier_id"),
            lesson_data.get("signal_id"),
            lesson_data.get("symbol"),
            lesson_data.get("account_id", "jarvais"),
            lesson_data.get("model_used"),
            lesson_data["outcome"],
            lesson_data.get("pnl_usd"),
            lesson_data.get("what_worked"),
            lesson_data.get("what_failed"),
            lesson_data.get("lesson_text"),
            lesson_data.get("root_cause"),
            lesson_data.get("optimal_trade_summary"),
            lesson_data.get("confidence_calibration"),
            lesson_data.get("full_post_mortem"),
            json.dumps(lesson_data.get("technical_factors")) if lesson_data.get("technical_factors") else None,
            json.dumps(lesson_data.get("timing_factors")) if lesson_data.get("timing_factors") else None,
            json.dumps(lesson_data.get("fundamental_factors")) if lesson_data.get("fundamental_factors") else None,
            json.dumps(lesson_data.get("decision_quality")) if lesson_data.get("decision_quality") else None,
            json.dumps(lesson_data.get("execution_factors")) if lesson_data.get("execution_factors") else None,
            lesson_data.get("qdrant_vector_id"),
            lesson_data.get("token_count", 0),
            lesson_data.get("api_cost_usd", 0)
        )
        return self.execute_returning_id(sql, params)

    def get_recent_lessons(self, account_id: str, limit: int = 20) -> List[Dict]:
        """Get recent lessons for memory context."""
        return self.fetch_all(
            "SELECT * FROM trade_lessons WHERE account_id = %s ORDER BY timestamp DESC LIMIT %s",
            (account_id, limit)
        )

    # ─────────────────────────────────────────────────────────────────
    # Daily Performance
    # ─────────────────────────────────────────────────────────────────

    def upsert_daily_performance(self, perf_data: Dict[str, Any]):
        """Insert or update daily performance record."""
        sql = """
            INSERT INTO daily_performance (
                account_id, date, total_signals, signals_approved, signals_vetoed,
                signals_rejected_risk, total_trades, wins, losses, breakevens,
                win_rate, total_pnl, ea_hypothetical_pnl, ai_alpha,
                free_trades_count, free_trades_pnl, avg_confidence,
                avg_confidence_winners, avg_confidence_losers, max_drawdown,
                max_drawdown_pct, total_api_cost, total_token_input,
                total_token_output, maturity_phase, daily_review_summary,
                balance_eod, balance_sod
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON DUPLICATE KEY UPDATE
                total_signals = VALUES(total_signals),
                signals_approved = VALUES(signals_approved),
                signals_vetoed = VALUES(signals_vetoed),
                signals_rejected_risk = VALUES(signals_rejected_risk),
                total_trades = VALUES(total_trades),
                wins = VALUES(wins),
                losses = VALUES(losses),
                breakevens = VALUES(breakevens),
                win_rate = VALUES(win_rate),
                total_pnl = VALUES(total_pnl),
                ea_hypothetical_pnl = VALUES(ea_hypothetical_pnl),
                ai_alpha = VALUES(ai_alpha),
                free_trades_count = VALUES(free_trades_count),
                free_trades_pnl = VALUES(free_trades_pnl),
                avg_confidence = VALUES(avg_confidence),
                avg_confidence_winners = VALUES(avg_confidence_winners),
                avg_confidence_losers = VALUES(avg_confidence_losers),
                max_drawdown = VALUES(max_drawdown),
                max_drawdown_pct = VALUES(max_drawdown_pct),
                total_api_cost = VALUES(total_api_cost),
                total_token_input = VALUES(total_token_input),
                total_token_output = VALUES(total_token_output),
                maturity_phase = VALUES(maturity_phase),
                daily_review_summary = VALUES(daily_review_summary),
                balance_eod = VALUES(balance_eod),
                balance_sod = VALUES(balance_sod)
        """
        params = (
            perf_data["account_id"], perf_data["date"],
            perf_data.get("total_signals", 0), perf_data.get("signals_approved", 0),
            perf_data.get("signals_vetoed", 0), perf_data.get("signals_rejected_risk", 0),
            perf_data.get("total_trades", 0), perf_data.get("wins", 0),
            perf_data.get("losses", 0), perf_data.get("breakevens", 0),
            perf_data.get("win_rate"), perf_data.get("total_pnl", 0),
            perf_data.get("ea_hypothetical_pnl", 0), perf_data.get("ai_alpha", 0),
            perf_data.get("free_trades_count", 0), perf_data.get("free_trades_pnl", 0),
            perf_data.get("avg_confidence"), perf_data.get("avg_confidence_winners"),
            perf_data.get("avg_confidence_losers"), perf_data.get("max_drawdown", 0),
            perf_data.get("max_drawdown_pct", 0), perf_data.get("total_api_cost", 0),
            perf_data.get("total_token_input", 0), perf_data.get("total_token_output", 0),
            perf_data.get("maturity_phase", 1), perf_data.get("daily_review_summary"),
            perf_data.get("balance_eod"), perf_data.get("balance_sod")
        )
        self.execute(sql, params)

    def get_daily_performance(self, account_id: str, target_date: date) -> Optional[Dict]:
        """Get daily performance for a specific date."""
        return self.fetch_one(
            "SELECT * FROM daily_performance WHERE account_id = %s AND date = %s",
            (account_id, target_date)
        )

    def get_performance_range(self, account_id: str, start_date: date,
                               end_date: date) -> List[Dict]:
        """Get daily performance records within a date range."""
        return self.fetch_all(
            "SELECT * FROM daily_performance WHERE account_id = %s "
            "AND date BETWEEN %s AND %s ORDER BY date",
            (account_id, start_date, end_date)
        )

    def get_consecutive_positive_alpha_days(self, account_id: str) -> int:
        """Count consecutive days with positive AI alpha (for maturity assessment)."""
        rows = self.fetch_all(
            "SELECT date, ai_alpha FROM daily_performance WHERE account_id = %s "
            "ORDER BY date DESC LIMIT 365",
            (account_id,)
        )
        count = 0
        for row in rows:
            if row["ai_alpha"] and row["ai_alpha"] > 0:
                count += 1
            else:
                break
        return count

    # ─────────────────────────────────────────────────────────────────
    # Self-Discovered Patterns
    # ─────────────────────────────────────────────────────────────────

    def insert_pattern(self, pattern_data: Dict[str, Any]) -> int:
        """Insert a self-discovered pattern."""
        sql = """
            INSERT INTO self_discovered_patterns (
                account_id, discovered_date, pattern_description, conditions,
                sample_size, win_rate, avg_pnl, avg_confidence, confidence_level,
                qdrant_vector_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        params = (
            pattern_data.get("account_id", "collective"),
            pattern_data.get("discovered_date", date.today()),
            pattern_data["pattern_description"],
            json.dumps(pattern_data.get("conditions")) if pattern_data.get("conditions") else None,
            pattern_data.get("sample_size", 0),
            pattern_data.get("win_rate"),
            pattern_data.get("avg_pnl"),
            pattern_data.get("avg_confidence"),
            pattern_data.get("confidence_level", "Low"),
            pattern_data.get("qdrant_vector_id")
        )
        return self.execute_returning_id(sql, params)

    def get_active_patterns(self, account_id: str = None) -> List[Dict]:
        """Get active patterns, optionally filtered by account."""
        if account_id:
            return self.fetch_all(
                "SELECT * FROM self_discovered_patterns WHERE is_active = TRUE "
                "AND (account_id = %s OR account_id = 'collective') ORDER BY win_rate DESC",
                (account_id,)
            )
        return self.fetch_all(
            "SELECT * FROM self_discovered_patterns WHERE is_active = TRUE ORDER BY win_rate DESC"
        )

    # ─────────────────────────────────────────────────────────────────
    # Config (Runtime)
    # ─────────────────────────────────────────────────────────────────

    def get_config_value(self, account_id: str, key: str) -> Optional[str]:
        """Get a runtime config value."""
        result = self.fetch_one(
            "SELECT config_value FROM config WHERE account_id = %s AND config_key = %s",
            (account_id, key)
        )
        return result["config_value"] if result else None

    def set_config_value(self, account_id: str, key: str, value: str,
                          updated_by: str = "user"):
        """Set a runtime config value (upsert)."""
        self.execute(
            """INSERT INTO config (account_id, config_key, config_value, updated_by)
               VALUES (%s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE config_value = VALUES(config_value),
               updated_by = VALUES(updated_by)""",
            (account_id, key, value, updated_by)
        )

    # ─────────────────────────────────────────────────────────────────
    # API Log
    # ─────────────────────────────────────────────────────────────────

    def log_api_call(self, log_data: Dict[str, Any]):
        """Log an AI API call for cost tracking.
        Supports granular tracking: context, source, source_detail, author,
        news_item_id, media_type. Falls back gracefully if columns don't exist.
        """
        # Build column list dynamically based on what data is provided
        # Core columns always present
        columns = ["account_id", "provider", "model", "role", "signal_id", "trade_id",
                    "token_count_input", "token_count_output", "cost_usd", "latency_ms",
                    "success", "error_message"]
        values = [
            log_data["account_id"], log_data["provider"], log_data["model"],
            log_data["role"], log_data.get("signal_id"), log_data.get("trade_id"),
            log_data.get("token_count_input", 0), log_data.get("token_count_output", 0),
            log_data.get("cost_usd", 0), log_data.get("latency_ms", 0),
            log_data.get("success", True), log_data.get("error_message")
        ]

        # Optional granular tracking columns (added by migration)
        optional_cols = ["context", "source", "source_detail", "author", "news_item_id", "media_type", "dossier_id", "cost_source", "duo_id", "actual_cost_usd"]
        for col in optional_cols:
            val = log_data.get(col)
            if val is not None:
                columns.append(col)
                values.append(val)

        placeholders = ", ".join(["%s"] * len(columns))
        col_str = ", ".join(columns)
        sql = f"INSERT INTO ai_api_log ({col_str}) VALUES ({placeholders})"

        try:
            self.execute(sql, tuple(values))
        except Exception as e:
            err_str = str(e)
            if "1054" in err_str or "Unknown column" in err_str:
                # Fallback: insert without the new columns (pre-migration DB)
                core_cols = ["account_id", "provider", "model", "role", "signal_id", "trade_id",
                             "token_count_input", "token_count_output", "cost_usd", "latency_ms",
                             "success", "error_message"]
                core_vals = [
                    log_data["account_id"], log_data["provider"], log_data["model"],
                    log_data["role"], log_data.get("signal_id"), log_data.get("trade_id"),
                    log_data.get("token_count_input", 0), log_data.get("token_count_output", 0),
                    log_data.get("cost_usd", 0), log_data.get("latency_ms", 0),
                    log_data.get("success", True), log_data.get("error_message")
                ]
                fallback_sql = f"INSERT INTO ai_api_log ({', '.join(core_cols)}) VALUES ({', '.join(['%s']*len(core_cols))})"
                self.execute(fallback_sql, tuple(core_vals))
            else:
                raise

    def get_daily_api_cost(self, account_id: str, target_date: date = None) -> float:
        """Get total API cost for a specific day."""
        if target_date is None:
            target_date = date.today()
        result = self.fetch_one(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM ai_api_log "
            "WHERE account_id = %s AND DATE(timestamp) = %s",
            (account_id, target_date)
        )
        return float(result["total"]) if result else 0.0

    def get_total_ai_cost_system(self) -> Dict[str, float]:
        """System-wide AI cost: all-time total + today's total.
        Powers the Overview 'Total AI Cost' KPI card.
        """
        total = self.fetch_one(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM ai_api_log"
        )
        today = self.fetch_one(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM ai_api_log "
            "WHERE DATE(timestamp) = CURDATE()"
        )
        return {
            "total_usd": float(total["total"]) if total else 0.0,
            "today_usd": float(today["total"]) if today else 0.0,
        }

    def get_signals_ai_cost(self, account_id: str = "default") -> Dict[str, Any]:
        """Token usage and cost for Signal AI roles (signal_ai + signal_update).
        Powers the Signals tab 'Token Usage' and 'Est. Total Cost'.
        Note: Signal AI logs with account_id='global', so we query both
        the specified account and 'global' to capture all signal costs.
        """
        result = self.fetch_one(
            "SELECT COALESCE(SUM(token_count_input), 0) as tci, "
            "COALESCE(SUM(token_count_output), 0) as tco, "
            "COALESCE(SUM(cost_usd), 0) as cost, "
            "COUNT(*) as calls "
            "FROM ai_api_log "
            "WHERE account_id IN (%s, 'global') AND role IN ('signal_ai', 'signal_update')",
            (account_id,)
        )
        if result:
            return {
                "token_count_input": int(result["tci"]),
                "token_count_output": int(result["tco"]),
                "cost_usd": float(result["cost"]),
                "api_calls": int(result["calls"]),
            }
        return {"token_count_input": 0, "token_count_output": 0, "cost_usd": 0.0, "api_calls": 0}

    def get_ai_cost_breakdown(self, days: int = 90, model: str = None,
                              provider: str = None, role: str = None,
                              context: str = None) -> Dict[str, Any]:
        """Full AI cost breakdown for the AI Cost modal.
        Groups by model, provider, role, context + daily and monthly series.
        Supports optional filters.
        """
        # Build WHERE clause with optional filters
        where_parts = ["timestamp >= DATE_SUB(CURDATE(), INTERVAL %s DAY)"]
        params_base = [days]
        if model:
            where_parts.append("model = %s")
            params_base.append(model)
        if provider:
            where_parts.append("provider = %s")
            params_base.append(provider)
        if role:
            where_parts.append("role = %s")
            params_base.append(role)
        if context:
            if context == '(untagged)':
                where_parts.append("context IS NULL")
            else:
                where_parts.append("context = %s")
                params_base.append(context)
        where_clause = " AND ".join(where_parts)

        def _group_query(group_col):
            sql = (
                f"SELECT {group_col} as name, "
                "COALESCE(SUM(cost_usd), 0) as cost_usd, "
                "COALESCE(SUM(token_count_input), 0) as token_count_input, "
                "COALESCE(SUM(token_count_output), 0) as token_count_output, "
                "COUNT(*) as api_calls "
                f"FROM ai_api_log WHERE {where_clause} "
                f"GROUP BY {group_col} ORDER BY cost_usd DESC"
            )
            rows = self.fetch_all(sql, tuple(params_base))
            return [dict(r) for r in (rows or [])]

        by_model = _group_query("model")
        by_provider = _group_query("provider")
        by_role = _group_query("role")

        # context column may not exist on older DBs
        try:
            by_context = _group_query("context")
        except Exception:
            by_context = []

        # Daily series
        daily_sql = (
            "SELECT DATE(timestamp) as date, "
            "COALESCE(SUM(cost_usd), 0) as cost_usd, "
            "COUNT(*) as api_calls "
            f"FROM ai_api_log WHERE {where_clause} "
            "GROUP BY DATE(timestamp) ORDER BY date"
        )
        daily_rows = self.fetch_all(daily_sql, tuple(params_base))
        daily = [{"date": str(r["date"]), "cost_usd": float(r["cost_usd"]),
                  "api_calls": int(r["api_calls"])} for r in (daily_rows or [])]

        # Monthly series
        monthly_sql = (
            "SELECT DATE_FORMAT(timestamp, '%%Y-%%m') as month, "
            "COALESCE(SUM(cost_usd), 0) as cost_usd, "
            "COUNT(*) as api_calls "
            f"FROM ai_api_log WHERE {where_clause} "
            "GROUP BY DATE_FORMAT(timestamp, '%%Y-%%m') ORDER BY month"
        )
        monthly_rows = self.fetch_all(monthly_sql, tuple(params_base))
        monthly = [{"month": r["month"], "cost_usd": float(r["cost_usd"]),
                    "api_calls": int(r["api_calls"])} for r in (monthly_rows or [])]

        return {
            "by_model": by_model,
            "by_provider": by_provider,
            "by_role": by_role,
            "by_context": by_context,
            "daily": daily,
            "monthly": monthly,
            "filters": {"days": days, "model": model, "provider": provider,
                        "role": role, "context": context},
        }

    # ─────────────────────────────────────────────────────────────────
    # Position Events
    # ─────────────────────────────────────────────────────────────────

    def insert_position_event(self, event_data: Dict[str, Any]):
        """Log a position event (SL move, partial close, etc.)."""
        sql = """
            INSERT INTO position_events (
                trade_id, account_id, event_type, old_value, new_value,
                description, current_price, current_pnl
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        params = (
            event_data["trade_id"], event_data["account_id"],
            event_data["event_type"], event_data.get("old_value"),
            event_data.get("new_value"), event_data.get("description"),
            event_data.get("current_price"), event_data.get("current_pnl")
        )
        self.execute(sql, params)

    def get_position_events(self, trade_id: int) -> List[Dict]:
        """Get all events for a specific trade."""
        return self.fetch_all(
            "SELECT * FROM position_events WHERE trade_id = %s ORDER BY timestamp",
            (trade_id,)
        )

    # ─────────────────────────────────────────────────────────────────
    # Aggregate Queries for Dashboard & Analytics
    # ─────────────────────────────────────────────────────────────────

    def get_today_pnl(self, account_id: str) -> float:
        """Get today's total P&L for an account."""
        result = self.fetch_one(
            "SELECT COALESCE(SUM(pnl_usd), 0) as total FROM trades "
            "WHERE account_id = %s AND DATE(close_time) = CURDATE() AND status = 'closed'",
            (account_id,)
        )
        return float(result["total"]) if result else 0.0

    def get_today_trade_count(self, account_id: str) -> int:
        """Get number of trades opened today."""
        result = self.fetch_one(
            "SELECT COUNT(*) as cnt FROM trades "
            "WHERE account_id = %s AND DATE(open_time) = CURDATE()",
            (account_id,)
        )
        return result["cnt"] if result else 0

    def get_today_signals_count(self, account_id: str) -> Dict[str, int]:
        """Get today's signal counts by status."""
        rows = self.fetch_all(
            "SELECT status, COUNT(*) as cnt FROM ea_signals "
            "WHERE account_id = %s AND DATE(created_at) = CURDATE() GROUP BY status",
            (account_id,)
        )
        return {row["status"]: row["cnt"] for row in rows}

    def get_equity_curve(self, account_id: str, days: int = 90) -> List[Dict]:
        """Get equity curve data (daily balance) for charting."""
        return self.fetch_all(
            "SELECT date, balance_eod, total_pnl, ea_hypothetical_pnl, ai_alpha "
            "FROM daily_performance WHERE account_id = %s "
            "ORDER BY date DESC LIMIT %s",
            (account_id, days)
        )

    def get_confidence_calibration(self, account_id: str, min_trades: int = 20) -> List[Dict]:
        """Get win rate by confidence bucket for calibration analysis."""
        return self.fetch_all(
            """SELECT
                FLOOR(ai_confidence_at_entry / 10) * 10 as confidence_bucket,
                COUNT(*) as trade_count,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                ROUND(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) / COUNT(*) * 100, 1) as win_rate,
                ROUND(AVG(pnl_usd), 2) as avg_pnl
            FROM trades
            WHERE account_id = %s AND status = 'closed' AND ai_confidence_at_entry IS NOT NULL
            GROUP BY confidence_bucket
            HAVING trade_count >= %s
            ORDER BY confidence_bucket""",
            (account_id, min_trades)
        )

    def get_veto_accuracy(self, account_id: str) -> Dict[str, Any]:
        """Calculate veto accuracy: how often did vetoed signals turn out to be losers?"""
        result = self.fetch_one(
            """SELECT
                COUNT(*) as total_vetoed,
                SUM(CASE WHEN hypothetical_pnl < 0 THEN 1 ELSE 0 END) as correct_vetoes,
                SUM(CASE WHEN hypothetical_pnl >= 0 THEN 1 ELSE 0 END) as missed_opportunities,
                COALESCE(SUM(CASE WHEN hypothetical_pnl < 0 THEN ABS(hypothetical_pnl) ELSE 0 END), 0) as money_saved,
                COALESCE(SUM(CASE WHEN hypothetical_pnl >= 0 THEN hypothetical_pnl ELSE 0 END), 0) as opportunity_cost
            FROM ea_signals
            WHERE account_id = %s AND status = 'vetoed' AND hypothetical_closed = TRUE""",
            (account_id,)
        )
        return result if result else {}

    # ─────────────────────────────────────────────────────────────────
    # Role Model History
    # ─────────────────────────────────────────────────────────────────

    def insert_model_assignment(self, data: Dict[str, Any]) -> int:
        """Record a new model assignment for a role."""
        sql = """
            INSERT INTO role_model_history (
                account_id, role, model, assigned_at
            ) VALUES (%s, %s, %s, %s)
        """
        return self.execute_returning_id(sql, (
            data.get("account_id", "global"),
            data["role"],
            data["model"],
            data.get("assigned_at", datetime.utcnow())
        ))

    def retire_model_assignment(self, account_id: str, role: str):
        """Mark the current model for a role as replaced."""
        self.execute(
            "UPDATE role_model_history SET replaced_at = %s "
            "WHERE account_id = %s AND role = %s AND replaced_at IS NULL",
            (datetime.utcnow(), account_id, role)
        )

    def update_model_assignment_stats(self, assignment_id: int, stats: Dict[str, Any]):
        """Update running statistics for a model assignment."""
        sql = """
            UPDATE role_model_history SET
                signals_processed = %s, trades_executed = %s,
                wins = %s, losses = %s, win_rate = %s,
                total_pnl = %s, avg_confidence = %s,
                total_api_calls = %s, total_input_tokens = %s,
                total_output_tokens = %s, total_cost_usd = %s,
                avg_latency_ms = %s, score = %s
            WHERE id = %s
        """
        self.execute(sql, (
            stats.get("signals_processed", 0), stats.get("trades_executed", 0),
            stats.get("wins", 0), stats.get("losses", 0),
            stats.get("win_rate"), stats.get("total_pnl", 0),
            stats.get("avg_confidence"), stats.get("total_api_calls", 0),
            stats.get("total_input_tokens", 0), stats.get("total_output_tokens", 0),
            stats.get("total_cost_usd", 0), stats.get("avg_latency_ms", 0),
            stats.get("score"), assignment_id
        ))

    def get_current_model_for_role(self, account_id: str, role: str) -> Optional[Dict]:
        """Get the currently active model assignment for a role."""
        return self.fetch_one(
            "SELECT * FROM role_model_history "
            "WHERE account_id = %s AND role = %s AND replaced_at IS NULL "
            "ORDER BY assigned_at DESC LIMIT 1",
            (account_id, role)
        )

    def get_model_history_for_role(self, account_id: str, role: str,
                                    limit: int = 20) -> List[Dict]:
        """Get the model assignment history for a specific role."""
        return self.fetch_all(
            "SELECT * FROM role_model_history "
            "WHERE account_id = %s AND role = %s ORDER BY assigned_at DESC LIMIT %s",
            (account_id, role, limit)
        )

    def get_all_model_history(self, account_id: str, limit: int = 50) -> List[Dict]:
        """Get model assignment history across all roles."""
        return self.fetch_all(
            "SELECT * FROM role_model_history "
            "WHERE account_id = %s ORDER BY assigned_at DESC LIMIT %s",
            (account_id, limit)
        )

    # ─────────────────────────────────────────────────────────────────
    # Role Daily Costs
    # ─────────────────────────────────────────────────────────────────

    def upsert_role_daily_cost(self, data: Dict[str, Any]):
        """Insert or update daily cost record for a role."""
        sql = """
            INSERT INTO role_daily_costs (
                account_id, date, role, model, api_calls, input_tokens,
                output_tokens, cost_usd, signals_processed, trades_contributed,
                pnl_contribution, avg_latency_ms, max_latency_ms, error_count
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON DUPLICATE KEY UPDATE
                model = VALUES(model),
                api_calls = VALUES(api_calls),
                input_tokens = VALUES(input_tokens),
                output_tokens = VALUES(output_tokens),
                cost_usd = VALUES(cost_usd),
                signals_processed = VALUES(signals_processed),
                trades_contributed = VALUES(trades_contributed),
                pnl_contribution = VALUES(pnl_contribution),
                avg_latency_ms = VALUES(avg_latency_ms),
                max_latency_ms = VALUES(max_latency_ms),
                error_count = VALUES(error_count)
        """
        self.execute(sql, (
            data["account_id"], data["date"], data["role"],
            data.get("model"), data.get("api_calls", 0),
            data.get("input_tokens", 0), data.get("output_tokens", 0),
            data.get("cost_usd", 0), data.get("signals_processed", 0),
            data.get("trades_contributed", 0), data.get("pnl_contribution", 0),
            data.get("avg_latency_ms", 0), data.get("max_latency_ms", 0),
            data.get("error_count", 0)
        ))

    def get_role_costs_by_period(self, account_id: str, role: str,
                                  start_date: date, end_date: date) -> List[Dict]:
        """Get daily cost records for a role within a date range."""
        return self.fetch_all(
            "SELECT * FROM role_daily_costs "
            "WHERE account_id = %s AND role = %s AND date BETWEEN %s AND %s "
            "ORDER BY date",
            (account_id, role, start_date, end_date)
        )

    # ─────────────────────────────────────────────────────────────────
    # Role Score Snapshots
    # ─────────────────────────────────────────────────────────────────

    def upsert_role_score_snapshot(self, data: Dict[str, Any]):
        """Insert or update a daily role score snapshot."""
        sql = """
            INSERT INTO role_score_snapshots (
                account_id, date, trader_score, coach_score,
                analyst_score, postmortem_score
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                trader_score = VALUES(trader_score),
                coach_score = VALUES(coach_score),
                analyst_score = VALUES(analyst_score),
                postmortem_score = VALUES(postmortem_score)
        """
        self.execute(sql, (
            data["account_id"], data["date"],
            data.get("trader_score"), data.get("coach_score"),
            data.get("analyst_score"), data.get("postmortem_score")
        ))

    def get_role_score_trend(self, account_id: str, days: int = 30) -> List[Dict]:
        """Get role score trend data for charting."""
        return self.fetch_all(
            "SELECT * FROM role_score_snapshots "
            "WHERE account_id = %s ORDER BY date DESC LIMIT %s",
            (account_id, days)
        )

    # ─────────────────────────────────────────────────────────────────
    # Memory Audit
    # ─────────────────────────────────────────────────────────────────

    def insert_memory_audit(self, data: Dict[str, Any]) -> int:
        """Log a memory operation to the audit trail."""
        sql = """
            INSERT INTO memory_audit (
                collection, role, text_preview, metadata_json, storage_backend
            ) VALUES (%s, %s, %s, %s, %s)
        """
        return self.execute_returning_id(sql, (
            data["collection"],
            data.get("role", "all"),
            data.get("text_preview", "")[:2000],
            json.dumps(data.get("metadata")) if data.get("metadata") else None,
            data.get("storage_backend", "mem0")
        ))

    def get_memory_audit_log(self, collection: str = None, limit: int = 100) -> List[Dict]:
        """Get memory audit log entries."""
        if collection:
            return self.fetch_all(
                "SELECT * FROM memory_audit WHERE collection = %s "
                "ORDER BY stored_at DESC LIMIT %s",
                (collection, limit)
            )
        return self.fetch_all(
            "SELECT * FROM memory_audit ORDER BY stored_at DESC LIMIT %s",
            (limit,)
        )

    # ─────────────────────────────────────────────────────────────────
    # Memory Fallback
    # ─────────────────────────────────────────────────────────────────

    def insert_memory_fallback(self, data: Dict[str, Any]) -> int:
        """Store a memory in fallback when Mem0/Qdrant are unavailable."""
        sql = """
            INSERT INTO memory_fallback (
                collection, text_content, metadata_json
            ) VALUES (%s, %s, %s)
        """
        return self.execute_returning_id(sql, (
            data["collection"],
            data.get("text_content") or data.get("text", ""),
            json.dumps(data.get("metadata")) if data.get("metadata") else None
        ))

    def get_unmigrated_fallback_memories(self, limit: int = 100) -> List[Dict]:
        """Get memories that haven't been migrated to Mem0/Qdrant yet."""
        return self.fetch_all(
            "SELECT * FROM memory_fallback WHERE migrated_to_mem0 = FALSE "
            "ORDER BY stored_at LIMIT %s",
            (limit,)
        )

    def mark_fallback_migrated(self, fallback_id: int):
        """Mark a fallback memory as successfully migrated."""
        self.execute(
            "UPDATE memory_fallback SET migrated_to_mem0 = TRUE, migrated_at = %s "
            "WHERE id = %s",
            (datetime.utcnow(), fallback_id)
        )

    # ─────────────────────────────────────────────────────────────────
    # Role Scorecard Dashboard Queries
    # (These power the expanded Role Scorecard tab)
    # ─────────────────────────────────────────────────────────────────

    def get_role_costs_summary(self, account_id: str, days: int = 7) -> List[Dict]:
        """
        Get aggregated cost summary per role for the Role Scorecard tab.
        Returns one row per role with totals for the requested period.
        """
        return self.fetch_all(
            """SELECT
                role,
                SUM(api_calls) as api_calls,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                SUM(input_tokens + output_tokens) as total_tokens,
                ROUND(SUM(cost_usd), 6) as cost_usd,
                SUM(signals_processed) as signals_processed,
                SUM(trades_contributed) as trades_contributed,
                ROUND(SUM(pnl_contribution), 2) as pnl_contribution,
                ROUND(AVG(avg_latency_ms), 0) as avg_latency_ms,
                SUM(error_count) as error_count,
                -- Derived metrics
                CASE WHEN SUM(trades_contributed) > 0
                    THEN ROUND(SUM(cost_usd) / SUM(trades_contributed), 6)
                    ELSE 0 END as cost_per_trade,
                CASE WHEN SUM(signals_processed) > 0
                    THEN ROUND(SUM(cost_usd) / SUM(signals_processed), 6)
                    ELSE 0 END as cost_per_signal,
                CASE WHEN SUM(cost_usd) > 0
                    THEN ROUND(SUM(pnl_contribution) / SUM(cost_usd), 2)
                    ELSE 0 END as pnl_per_dollar_ai
            FROM role_daily_costs
            WHERE account_id = %s AND date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            GROUP BY role
            ORDER BY cost_usd DESC""",
            (account_id, days)
        )

    def get_role_daily_cost_series(self, account_id: str, days: int = 30) -> List[Dict]:
        """
        Get daily cost and P&L series for the cost efficiency charts.
        Returns one row per day with total AI spend and total P&L.
        """
        return self.fetch_all(
            """SELECT
                date,
                ROUND(SUM(cost_usd), 6) as total_ai_cost,
                ROUND(SUM(pnl_contribution), 2) as total_pnl,
                SUM(api_calls) as total_api_calls
            FROM role_daily_costs
            WHERE account_id = %s AND date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            GROUP BY date
            ORDER BY date""",
            (account_id, days)
        )

    def get_role_cost_breakdown(self, account_id: str, days: int = 30) -> List[Dict]:
        """
        Get cost breakdown by role for the doughnut chart.
        Returns one row per role with total cost.
        """
        return self.fetch_all(
            """SELECT
                role,
                ROUND(SUM(cost_usd), 6) as total_cost
            FROM role_daily_costs
            WHERE account_id = %s AND date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            GROUP BY role
            ORDER BY total_cost DESC""",
            (account_id, days)
        )

    def get_model_comparison_for_role(self, account_id: str, role: str) -> List[Dict]:
        """
        Get the two most recent model assignments for a role for side-by-side comparison.
        Returns up to 2 rows with full performance metrics.
        """
        return self.fetch_all(
            """SELECT
                model, assigned_at, replaced_at,
                signals_processed, trades_executed, wins, losses,
                win_rate, total_pnl, avg_confidence,
                total_api_calls, total_input_tokens, total_output_tokens,
                total_cost_usd, avg_latency_ms, score,
                CASE WHEN trades_executed > 0
                    THEN ROUND(total_cost_usd / trades_executed, 6)
                    ELSE 0 END as cost_per_trade,
                CASE WHEN total_cost_usd > 0
                    THEN ROUND(total_pnl / total_cost_usd, 2)
                    ELSE 0 END as roi
            FROM role_model_history
            WHERE account_id = %s AND role = %s
            ORDER BY assigned_at DESC LIMIT 2""",
            (account_id, role)
        )

    # ─────────────────────────────────────────────────────────────────
    # Cost Aggregation (called by nightly cron job)
    # ─────────────────────────────────────────────────────────────────

    def aggregate_daily_role_costs(self, account_id: str, target_date: date):
        """
        Aggregate ai_api_log into role_daily_costs for a specific date.
        Called by the nightly cost aggregation job.
        """
        rows = self.fetch_all(
            """SELECT
                role,
                model,
                COUNT(*) as api_calls,
                SUM(token_count_input) as input_tokens,
                SUM(token_count_output) as output_tokens,
                SUM(cost_usd) as cost_usd,
                ROUND(AVG(latency_ms), 0) as avg_latency_ms,
                MAX(latency_ms) as max_latency_ms,
                SUM(CASE WHEN success = FALSE THEN 1 ELSE 0 END) as error_count
            FROM ai_api_log
            WHERE account_id = %s AND DATE(timestamp) = %s
            GROUP BY role, model""",
            (account_id, target_date)
        )

        for row in rows:
            self.upsert_role_daily_cost({
                "account_id": account_id,
                "date": target_date,
                "role": row["role"],
                "model": row["model"],
                "api_calls": row["api_calls"],
                "input_tokens": row["input_tokens"] or 0,
                "output_tokens": row["output_tokens"] or 0,
                "cost_usd": row["cost_usd"] or 0,
                "avg_latency_ms": row["avg_latency_ms"] or 0,
                "max_latency_ms": row["max_latency_ms"] or 0,
                "error_count": row["error_count"] or 0
            })

        logger.info(f"[{account_id}] Aggregated {len(rows)} role cost records for {target_date}")

    def aggregate_daily_role_pnl(self, account_id: str, target_date: date):
        """
        Update role_daily_costs with P&L contribution from trades.
        Must be called after aggregate_daily_role_costs.
        """
        # Trader role gets the trade P&L
        result = self.fetch_one(
            """SELECT
                COUNT(*) as trades,
                COALESCE(SUM(pnl_usd), 0) as total_pnl
            FROM trades
            WHERE account_id = %s AND DATE(close_time) = %s AND status = 'closed'""",
            (account_id, target_date)
        )
        if result and result["trades"] > 0:
            self.execute(
                """UPDATE role_daily_costs SET
                    trades_contributed = %s, pnl_contribution = %s
                WHERE account_id = %s AND date = %s AND role = 'trader'""",
                (result["trades"], result["total_pnl"], account_id, target_date)
            )

        # Analyst role gets signal count
        sig_result = self.fetch_one(
            """SELECT COUNT(*) as signals
            FROM ea_signals
            WHERE account_id = %s AND DATE(created_at) = %s""",
            (account_id, target_date)
        )
        if sig_result and sig_result["signals"] > 0:
            self.execute(
                """UPDATE role_daily_costs SET signals_processed = %s
                WHERE account_id = %s AND date = %s AND role = 'analyst'""",
                (sig_result["signals"], account_id, target_date)
            )



    # ─────────────────────────────────────────────────────────────────────
    # Aliases for cross-module compatibility
    # ─────────────────────────────────────────────────────────────────────

    def get_role_cost_summary(self, account_id: str, days: int = 7) -> List[Dict]:
        return self.get_role_costs_summary(account_id, days=days)

    def get_role_score_snapshots(self, account_id: str, days: int = 30) -> List[Dict]:
        return self.get_role_score_trend(account_id, days=days)

    def get_role_model_history(self, account_id: str) -> List[Dict]:
        return self.get_all_model_history(account_id)

    def initialize_schema(self, schema_path: str = None):
        return self.init_schema(schema_path)

    def insert_trade_lesson(self, lesson_data: Dict[str, Any]) -> int:
        return self.insert_lesson(lesson_data)

    def insert_ai_decision(self, decision_data: Dict[str, Any]) -> int:
        return self.insert_decision(decision_data)

    def insert_score_snapshot(self, snapshot_data: Dict[str, Any]):
        return self.upsert_role_score_snapshot(snapshot_data)

    def insert_daily_performance(self, perf_data: Dict[str, Any]):
        return self.upsert_daily_performance(perf_data)

    def log_memory_audit(self, audit_data: Dict[str, Any]):
        return self.insert_memory_audit(audit_data)

    def update_trade_close(self, trade_id: int, exit_price: float, pnl_usd: float,
                           pnl_pips: float = 0, close_reason: str = ""):
        return self.close_trade(trade_id, exit_price, pnl_usd, pnl_pips, close_reason)

    def log_signal(self, signal_data: Dict[str, Any]) -> int:
        return self.insert_signal(signal_data)

    def log_trade(self, trade_data: Dict[str, Any]) -> int:
        return self.insert_trade(trade_data)

    def update_signal_ea_hypothetical(self, signal_id: int, ea_exit_price: float,
                                       ea_pnl: float):
        return self.update_signal_hypothetical(signal_id, ea_exit_price, ea_pnl)

    # ─────────────────────────────────────────────────────────────────────
    # New query methods for dashboard and engine compatibility
    # ─────────────────────────────────────────────────────────────────────

    def execute_query(self, sql: str, params: tuple = None) -> List[Dict]:
        return self.fetch_all(sql, params)

    def execute_update(self, sql: str, params: tuple = None) -> int:
        return self.execute(sql, params)

    def get_all_account_configs(self) -> List[Dict]:
        rows = self.fetch_all(
            """SELECT DISTINCT account_id,
                      MAX(CASE WHEN config_key = 'account_name' THEN config_value END) as name,
                      MAX(CASE WHEN config_key = 'broker' THEN config_value END) as broker,
                      MAX(CASE WHEN config_key = 'account_type' THEN config_value END) as account_type,
                      MAX(CASE WHEN config_key = 'is_live' THEN config_value END) as is_live
               FROM config
               WHERE account_id != 'global'
               GROUP BY account_id ORDER BY account_id"""
        )
        if not rows:
            rows = self.fetch_all(
                """SELECT DISTINCT account_id, account_id as name,
                          account_id as broker, 'demo' as account_type, 'false' as is_live
                   FROM trades ORDER BY account_id"""
            )
        # Ensure all fields are present for JS
        for r in (rows or []):
            r.setdefault('broker', r.get('name', r.get('account_id', 'Unknown')))
            r.setdefault('account_type', 'demo')
            r.setdefault('name', r.get('account_id', 'Unknown'))
        return rows or []

    def get_all_account_ids(self) -> List[str]:
        rows = self.fetch_all("SELECT DISTINCT account_id FROM trades ORDER BY account_id")
        return [r['account_id'] for r in rows] if rows else []

    def get_all_daily_performance(self, account_id: str) -> List[Dict]:
        return self.fetch_all(
            "SELECT * FROM daily_performance WHERE account_id = %s ORDER BY date ASC",
            (account_id,)
        ) or []

    def get_all_trades(self, account_id: str) -> List[Dict]:
        return self.fetch_all(
            "SELECT * FROM trades WHERE account_id = %s ORDER BY open_time ASC",
            (account_id,)
        ) or []

    def get_recent_trades(self, account_id: str, limit: int = 50) -> List[Dict]:
        return self.fetch_all(
            "SELECT * FROM trades WHERE account_id = %s ORDER BY open_time DESC LIMIT %s",
            (account_id, limit)
        ) or []

    def get_trades_in_period(self, account_id: str, start_date, end_date) -> List[Dict]:
        return self.fetch_all(
            """SELECT * FROM trades WHERE account_id = %s
               AND open_time >= %s AND open_time <= %s ORDER BY open_time ASC""",
            (account_id, start_date, end_date)
        ) or []

    def get_signals_in_period(self, account_id: str, start_date, end_date) -> List[Dict]:
        return self.fetch_all(
            """SELECT * FROM ea_signals WHERE account_id = %s
               AND timestamp >= %s AND timestamp <= %s ORDER BY timestamp ASC""",
            (account_id, start_date, end_date)
        ) or []

    def get_decisions_in_period(self, account_id: str, start_date, end_date) -> List[Dict]:
        return self.fetch_all(
            """SELECT d.* FROM ai_decisions d
               JOIN ea_signals s ON d.signal_id = s.id
               WHERE s.account_id = %s AND d.created_at >= %s AND d.created_at <= %s
               ORDER BY d.created_at ASC""",
            (account_id, start_date, end_date)
        ) or []

    def get_today_trades(self, account_id: str) -> List[Dict]:
        return self.fetch_all(
            """SELECT * FROM trades WHERE account_id = %s
               AND DATE(open_time) = CURDATE() ORDER BY open_time ASC""",
            (account_id,)
        ) or []

    def get_today_signals(self, account_id: str) -> List[Dict]:
        return self.fetch_all(
            """SELECT * FROM ea_signals WHERE account_id = %s
               AND DATE(created_at) = CURDATE() ORDER BY created_at ASC""",
            (account_id,)
        ) or []

    def get_today_decisions(self, account_id: str) -> List[Dict]:
        return self.fetch_all(
            """SELECT d.* FROM ai_decisions d
               JOIN ea_signals s ON d.signal_id = s.id
               WHERE s.account_id = %s AND DATE(d.created_at) = CURDATE()
               ORDER BY d.created_at ASC""",
            (account_id,)
        ) or []

    def get_ai_decision_by_signal(self, signal_id: int) -> Optional[Dict]:
        return self.fetch_one(
            "SELECT * FROM ai_decisions WHERE signal_id = %s ORDER BY created_at DESC LIMIT 1",
            (signal_id,)
        )

    def get_daily_cost_vs_pnl(self, account_id: str, days: int = 30) -> List[Dict]:
        return self.fetch_all(
            """SELECT rdc.date,
                      SUM(rdc.cost_usd) as cost,
                      COALESCE(dp.total_pnl, 0) as pnl
               FROM role_daily_costs rdc
               LEFT JOIN daily_performance dp
                   ON rdc.account_id = dp.account_id AND rdc.date = dp.date
               WHERE rdc.account_id = %s
               AND rdc.date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
               GROUP BY rdc.date, dp.total_pnl
               ORDER BY rdc.date ASC""",
            (account_id, days)
        ) or []

    def get_peak_equity(self, account_id: str) -> float:
        result = self.fetch_one(
            "SELECT MAX(cumulative_pnl) as peak FROM daily_performance WHERE account_id = %s",
            (account_id,)
        )
        return float(result['peak']) if result and result['peak'] else 0.0

    def update_peak_equity(self, account_id: str, peak: float):
        self.set_config_value(account_id, 'peak_equity', str(peak), 'system')

    def get_weekly_pnl(self, account_id: str) -> float:
        result = self.fetch_one(
            """SELECT COALESCE(SUM(pnl_usd), 0) as weekly_pnl
               FROM trades WHERE account_id = %s AND status = 'closed'
               AND close_time >= DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY)""",
            (account_id,)
        )
        return float(result['weekly_pnl']) if result else 0.0

    def insert_daily_review(self, review_data: Dict[str, Any]):
        self.execute(
            """INSERT INTO trade_lessons
               (trade_id, account_id, outcome, lesson_text, root_cause, timestamp)
               VALUES (0, %s, 'BREAKEVEN', %s, %s, NOW())""",
            (review_data.get('account_id', 'system'),
             review_data.get('summary', ''),
             review_data.get('tags', 'daily_review'))
        )

    def search_memories_fallback(self, query: str, limit: int = 10) -> List[Dict]:
        return self.fetch_all(
            """SELECT * FROM memory_fallback
               WHERE migrated = FALSE AND memory_text LIKE %s
               ORDER BY created_at DESC LIMIT %s""",
            (f'%{query}%', limit)
        ) or []

    def get_trade_with_reasoning(self, trade_id: int) -> Dict:
        trade = self.get_trade(trade_id)
        if not trade:
            return {}
        signal = self.fetch_one(
            "SELECT * FROM ea_signals WHERE id = %s",
            (trade.get('signal_id', 0),)
        )
        decision = None
        if signal:
            decision = self.fetch_one(
                "SELECT * FROM ai_decisions WHERE signal_id = %s ORDER BY created_at DESC LIMIT 1",
                (signal['id'],)
            )
        lessons = self.fetch_all(
            "SELECT * FROM trade_lessons WHERE trade_id = %s", (trade_id,)
        )
        events = self.get_position_events(trade_id)
        return {
            "trade": trade, "signal": signal, "decision": decision,
            "lessons": lessons or [], "events": events or []
        }

    def get_recent_alpha(self, minutes: int = 60) -> List[Dict]:
        items = []
        # Get signals
        signals = self.fetch_all(
            """SELECT 'signal' as type, 'signal' as category, symbol, direction,
                      0 as confidence, created_at as timestamp,
                      CONCAT(symbol, ' ', direction, ' @ ', ROUND(current_price, 2)) as headline,
                      CONCAT('EA Signal: ', symbol, ' ', direction, ' | Market: ', COALESCE(market_state, 'unknown'),
                             ' | HTF: ', COALESCE(htf_trend, 'unknown')) as detail,
                      'EA Signal' as source,
                      CASE WHEN direction = 'BUY' THEN 'bullish' ELSE 'bearish' END as sentiment
               FROM ea_signals
               WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
               ORDER BY created_at DESC LIMIT 20""",
            (minutes,)
        )
        if signals:
            items.extend(signals)
        # Get AI decisions
        decisions = self.fetch_all(
            """SELECT 'decision' as type, 'insight' as category,
                      s.symbol, s.direction,
                      d.confidence, d.created_at as timestamp,
                      CONCAT('AI ', d.decision, ': ', s.symbol, ' ', s.direction, ' (', d.confidence, '%%)') as headline,
                      d.trader_reasoning as detail,
                      CONCAT('AI ', d.model_used) as source,
                      CASE WHEN d.decision = 'Approve' THEN
                          CASE WHEN s.direction = 'BUY' THEN 'bullish' ELSE 'bearish' END
                      ELSE 'neutral' END as sentiment
               FROM ai_decisions d JOIN ea_signals s ON d.signal_id = s.id
               WHERE d.created_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
               ORDER BY d.created_at DESC LIMIT 20""",
            (minutes,)
        )
        if decisions:
            items.extend(decisions)
        # Get news items
        news = self.fetch_all(
            """SELECT 'news' as type, category, headline,
                      detail, source, url,
                      sentiment, collected_at as timestamp,
                      symbols, relevance
               FROM news_items
               WHERE collected_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
               ORDER BY collected_at DESC LIMIT 50""",
            (minutes,)
        )
        if news:
            for n in news:
                # Parse symbols JSON
                if n.get('symbols') and isinstance(n['symbols'], str):
                    try:
                        import json as _json
                        n['tags'] = _json.loads(n['symbols'])
                    except:
                        n['tags'] = []
                elif isinstance(n.get('symbols'), list):
                    n['tags'] = n['symbols']
                else:
                    n['tags'] = []
            items.extend(news)
        return sorted(items, key=lambda x: str(x.get('timestamp', '')), reverse=True)[:50]

    def get_alpha_summary(self, minutes: int = 60) -> Dict:
        signal_count = self.fetch_one(
            "SELECT COUNT(*) as cnt FROM ea_signals WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)",
            (minutes,)
        )
        decision_count = self.fetch_one(
            """SELECT COUNT(*) as cnt,
                      SUM(CASE WHEN decision = 'Approve' THEN 1 ELSE 0 END) as approved,
                      SUM(CASE WHEN decision = 'Veto' THEN 1 ELSE 0 END) as vetoed
               FROM ai_decisions WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)""",
            (minutes,)
        )
        news_count = self.fetch_one(
            "SELECT COUNT(*) as cnt FROM news_items WHERE collected_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)",
            (minutes,)
        )
        # Build AI summary text
        n_signals = signal_count['cnt'] if signal_count else 0
        n_decisions = decision_count['cnt'] if decision_count else 0
        n_approved = int(decision_count['approved'] or 0) if decision_count else 0
        n_vetoed = int(decision_count['vetoed'] or 0) if decision_count else 0
        n_news = news_count['cnt'] if news_count else 0
        
        summary_parts = []
        if n_news > 0:
            summary_parts.append(f"{n_news} news items collected")
        if n_signals > 0:
            summary_parts.append(f"{n_signals} EA signals received")
        if n_decisions > 0:
            summary_parts.append(f"{n_decisions} AI decisions ({n_approved} approved, {n_vetoed} vetoed)")
        if not summary_parts:
            summary_parts.append(f"No activity in the last {minutes} minutes. Monitoring markets...")
        
        return {
            "signals": n_signals,
            "decisions": n_decisions,
            "approved": n_approved,
            "vetoed": n_vetoed,
            "news": n_news,
            "period_minutes": minutes,
            "summary": ". ".join(summary_parts) + "."
        }


    # ─────────────────────────────────────────────────────────────────
    # News Items
    # ─────────────────────────────────────────────────────────────────

    def store_news_item(self, item: Dict[str, Any]) -> bool:
        """Store a news item. Returns True if inserted, False if duplicate.
        Supports TradingView-specific fields: author, author_badge, source_detail,
        chart_image_url, ai_analysis, direction, boosts, comments_count, tv_timeframe, external_id.
        """
        import hashlib
        title = item.get('title', '') or item.get('headline', '')
        url = item.get('url', '')
        source = item.get('source', '')
        # Use external_id for hash if available (better dedup for TradingView/Telegram)
        ext_id = item.get('external_id', '')
        if ext_id:
            hash_input = f"{ext_id}:{title}:{url}"
        elif source == 'news':
            # News RSS: hash on title + source_detail only (not URL).
            # Google News URLs change on every fetch for the same article.
            src_detail = item.get('source_detail', '')
            hash_input = f"{title[:500]}:{src_detail}"
        else:
            hash_input = f"{title}:{url}"
        hash_key = hashlib.sha256(hash_input.encode()).hexdigest()[:64]
        try:
            author_val = item.get('author')
            affected = self.execute(
                """INSERT IGNORE INTO news_items
                   (hash_key, source, headline, detail, url, category,
                    relevance, sentiment, symbols, tags, published_at,
                    author, author_name, author_badge, source_detail, chart_image_url,
                    media_url, media_type,
                    ai_analysis, direction, boosts, comments_count,
                    tv_timeframe, external_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (hash_key, item.get('source', 'unknown'),
                 title[:500] if title else '',
                 item.get('body', item.get('detail', item.get('summary', ''))),
                 url[:500] if url else '',
                 item.get('category', 'news'),
                 item.get('relevance', 'medium'),
                 item.get('sentiment', 'neutral'),
                 json.dumps(item.get('symbols', [])),
                 json.dumps(item.get('tags', [])),
                 item.get('published_at'),
                 author_val,
                 item.get('author_name') or author_val,
                 item.get('author_badge'),
                 item.get('source_detail'),
                 item.get('chart_image_url'),
                 item.get('media_url'),
                 item.get('media_type'),
                 item.get('ai_analysis'),
                 item.get('direction'),
                 item.get('boosts', 0),
                 item.get('comments_count', 0),
                 item.get('tv_timeframe'),
                 item.get('external_id'))
            )
            # INSERT IGNORE returns 0 affected rows for duplicates
            if affected > 0:
                try:
                    row = self.fetch_one(
                        "SELECT id FROM news_items WHERE hash_key = %s", (hash_key,)
                    )
                    if row:
                        from services.vectorization_worker import queue_for_vectorization
                        body = item.get('body', item.get('detail', item.get('summary', '')))
                        vec_text = f"{title}\n{body}"[:30000]
                        queue_for_vectorization(self, "news_items", row["id"], "feed_items", vec_text, {
                            "source": item.get("source", ""),
                            "author": item.get("author", ""),
                            "category": item.get("category", ""),
                            "symbols": item.get("symbols", []),
                            "sentiment": item.get("sentiment", ""),
                            "source_detail": item.get("source_detail", ""),
                        })
                except Exception as vec_err:
                    logger.debug(f"Vectorization queue error: {vec_err}")
                return True
            return False
        except Exception as e:
            logger.debug(f"store_news_item error: {e}")
            return False

    def get_news_count(self, minutes: int = 60) -> int:
        result = self.fetch_one(
            "SELECT COUNT(*) as cnt FROM news_items WHERE collected_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)",
            (minutes,)
        )
        return result['cnt'] if result else 0

    # ─────────────────────────────────────────────────────────────────
    # Missing methods needed by dashboard API
    # ─────────────────────────────────────────────────────────────────

    def get_alpha_for_trade(self, trade_id: int) -> List[Dict]:
        """Get news/alpha items that were available at the time of a trade."""
        trade = self.get_trade(trade_id)
        if not trade:
            return []
        open_time = trade.get('open_time')
        if not open_time:
            return []
        return self.fetch_all(
            """SELECT title as headline, source, sentiment, relevance, collected_at as timestamp
               FROM news_items
               WHERE collected_at <= %s AND collected_at >= DATE_SUB(%s, INTERVAL 60 MINUTE)
               ORDER BY collected_at DESC LIMIT 20""",
            (open_time, open_time)
        ) or []

    def get_model_performance(self) -> Dict:
        """Get aggregated model performance stats."""
        models = self.fetch_all(
            """SELECT
                d.model_used as model,
                GROUP_CONCAT(DISTINCT rdc.role) as roles,
                COUNT(*) as calls,
                SUM(d.token_count_input) as input_tokens,
                SUM(d.token_count_output) as output_tokens,
                SUM(d.api_cost_usd) as cost,
                SUM(CASE WHEN d.decision = 'Approve' THEN 1 ELSE 0 END) as trades_approved,
                AVG(d.confidence) as avg_confidence
            FROM ai_decisions d
            LEFT JOIN role_daily_costs rdc ON d.model_used = rdc.model AND d.account_id = rdc.account_id
            GROUP BY d.model_used
            ORDER BY cost DESC"""
        )
        # Calculate win rate per model from trades
        for m in (models or []):
            model_name = m.get('model', '')
            wr = self.fetch_one(
                """SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN t.pnl_usd > 0 THEN 1 ELSE 0 END) as wins
                FROM trades t
                JOIN ai_decisions d ON t.decision_id = d.id
                WHERE d.model_used = %s AND t.status = 'closed'""",
                (model_name,)
            )
            if wr and wr['total'] and wr['total'] > 0:
                m['win_rate'] = (wr['wins'] / wr['total']) * 100
            else:
                m['win_rate'] = 0.0
        total_cost = sum(m.get('cost', 0) or 0 for m in (models or []))
        return {"models": models or [], "total_cost": round(total_cost, 4)}

    def get_logs(self, level: str = "all", phase: str = "all", limit: int = 200) -> List[Dict]:
        """Get system logs from ai_api_log as a proxy."""
        conditions = ["1=1"]
        params = []
        if level != "all":
            if level == "error":
                conditions.append("success = 0")
            elif level == "warning":
                conditions.append("latency_ms > 5000")
        if phase != "all":
            conditions.append("role = %s")
            params.append(phase.lower())
        params.append(limit)
        rows = self.fetch_all(
            f"""SELECT id, role as phase,
                      CASE WHEN success = 0 THEN 'error'
                           WHEN latency_ms > 5000 THEN 'warning'
                           ELSE 'info' END as level,
                      CONCAT(model, ' | ', role, ' | ',
                             token_count_input, ' in / ', token_count_output, ' out | $',
                             ROUND(cost_usd, 4), ' | ', latency_ms, 'ms',
                             CASE WHEN success = 0 THEN CONCAT(' | ERROR: ', COALESCE(error_message, 'unknown')) ELSE '' END
                      ) as message,
                      created_at as timestamp
               FROM ai_api_log
               WHERE {' AND '.join(conditions)}
               ORDER BY created_at DESC LIMIT %s""",
            tuple(params)
        )
        return rows or []

    def purge_logs(self):
        """Purge old API logs (keep last 24 hours)."""
        self.execute("DELETE FROM ai_api_log WHERE created_at < DATE_SUB(NOW(), INTERVAL 24 HOUR)")

    def get_role_scorecard_data(self, account_id: str) -> Dict:
        """Build role scorecard data from actual DB tables."""
        # Get latest scores
        latest_scores = self.fetch_one(
            "SELECT * FROM role_score_snapshots WHERE account_id = %s ORDER BY date DESC LIMIT 1",
            (account_id,)
        ) or {}
        
        # Get trader stats
        trader_stats = self.fetch_one(
            """SELECT COUNT(*) as signals_evaluated,
                      SUM(CASE WHEN decision = 'Approve' THEN 1 ELSE 0 END) as approved,
                      AVG(confidence) as avg_confidence
               FROM ai_decisions WHERE account_id = %s""",
            (account_id,)
        ) or {}
        
        # Get trader win rate and P&L
        trader_pnl = self.fetch_one(
            """SELECT COUNT(*) as trades, SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                      SUM(pnl_usd) as total_pnl,
                      AVG(CASE WHEN pnl_usd > 0 THEN ai_confidence_at_entry END) as avg_conf_wins,
                      AVG(CASE WHEN pnl_usd <= 0 THEN ai_confidence_at_entry END) as avg_conf_losses
               FROM trades WHERE account_id = %s AND status = 'closed'""",
            (account_id,)
        ) or {}
        
        # Get coach stats (vetoes)
        coach_stats = self.fetch_one(
            """SELECT SUM(CASE WHEN decision = 'Veto' THEN 1 ELSE 0 END) as vetoes,
                      COUNT(*) as total
               FROM ai_decisions WHERE account_id = %s""",
            (account_id,)
        ) or {}
        
        # Get veto accuracy
        veto_acc = self.get_veto_accuracy(account_id)
        
        # Get analyst stats from ai_api_log
        analyst_stats = self.fetch_one(
            """SELECT COUNT(*) as dossiers,
                      SUM(token_count_input + token_count_output) as tokens,
                      SUM(cost_usd) as cost
               FROM ai_api_log WHERE account_id = %s AND role = 'analyst'""",
            (account_id,)
        ) or {}
        
        # Get postmortem stats
        pm_stats = self.fetch_one(
            """SELECT COUNT(DISTINCT trade_id) as reviews,
                      COUNT(*) as lessons
               FROM trade_lessons WHERE account_id = %s""",
            (account_id,)
        ) or {}
        pm_tokens = self.fetch_one(
            """SELECT SUM(token_count_input + token_count_output) as tokens
               FROM ai_api_log WHERE account_id = %s AND role = 'postmortem'""",
            (account_id,)
        ) or {}
        
        # Get pattern count
        pattern_count = self.fetch_one(
            "SELECT COUNT(*) as cnt FROM self_discovered_patterns WHERE account_id = %s AND is_active = 1",
            (account_id,)
        ) or {'cnt': 0}
        
        # Get current models
        trader_model = self.fetch_one(
            "SELECT model FROM role_model_history WHERE account_id = %s AND role = 'trader' AND replaced_at IS NULL ORDER BY assigned_at DESC LIMIT 1",
            (account_id,)
        )
        coach_model = self.fetch_one(
            "SELECT model FROM role_model_history WHERE account_id = %s AND role = 'coach' AND replaced_at IS NULL ORDER BY assigned_at DESC LIMIT 1",
            (account_id,)
        )
        analyst_model = self.fetch_one(
            "SELECT model FROM role_model_history WHERE account_id = %s AND role = 'analyst' AND replaced_at IS NULL ORDER BY assigned_at DESC LIMIT 1",
            (account_id,)
        )
        pm_model = self.fetch_one(
            "SELECT model FROM role_model_history WHERE account_id = %s AND role = 'postmortem' AND replaced_at IS NULL ORDER BY assigned_at DESC LIMIT 1",
            (account_id,)
        )
        
        total_trades = trader_pnl.get('trades', 0) or 0
        wins = trader_pnl.get('wins', 0) or 0
        
        return {
            "trader": {
                "score": latest_scores.get('trader_score', 0) or 0,
                "model": trader_model['model'] if trader_model else 'Not assigned',
                "signals_evaluated": trader_stats.get('signals_evaluated', 0) or 0,
                "approved": trader_stats.get('approved', 0) or 0,
                "win_rate": (wins / total_trades * 100) if total_trades > 0 else 0,
                "avg_conf_wins": trader_pnl.get('avg_conf_wins', 0) or 0,
                "avg_conf_losses": trader_pnl.get('avg_conf_losses', 0) or 0,
                "total_pnl": trader_pnl.get('total_pnl', 0) or 0
            },
            "coach": {
                "score": latest_scores.get('coach_score', 0) or 0,
                "model": coach_model['model'] if coach_model else 'Not assigned',
                "challenges": 0,  # TODO: track challenges separately
                "vetoes": coach_stats.get('vetoes', 0) or 0,
                "correct_vetoes": veto_acc.get('correct_vetoes', 0),
                "veto_accuracy": veto_acc.get('accuracy', 0),
                "money_saved": veto_acc.get('money_saved', 0),
                "missed_opportunities": veto_acc.get('missed_opportunities', 0)
            },
            "analyst": {
                "score": latest_scores.get('analyst_score', 0) or 0,
                "model": analyst_model['model'] if analyst_model else 'Not assigned',
                "dossiers": analyst_stats.get('dossiers', 0) or 0,
                "sources_used": 7,  # From our news collector config
                "avg_data_points": 15,
                "summaries": analyst_stats.get('dossiers', 0) or 0,
                "tokens": analyst_stats.get('tokens', 0) or 0,
                "cost": analyst_stats.get('cost', 0) or 0
            },
            "postmortem": {
                "score": latest_scores.get('postmortem_score', 0) or 0,
                "model": pm_model['model'] if pm_model else 'Not assigned',
                "reviews": pm_stats.get('reviews', 0) or 0,
                "lessons": pm_stats.get('lessons', 0) or 0,
                "patterns": pattern_count.get('cnt', 0) or 0,
                "corrections": 0,
                "confidence_adjustments": 0,
                "tokens": pm_tokens.get('tokens', 0) or 0
            }
        }

# ─────────────────────────────────────────────────────────────────────
# Shared Helpers
# ─────────────────────────────────────────────────────────────────────

def get_mentor_usernames(db) -> list:
    """Get all usernames linked to mentor profiles (source_username + display_name).
    Shared across TradingFloor and TradeDossierBuilder."""
    rows = db.fetch_all("""
        SELECT DISTINCT upl.source_username
        FROM user_profiles up
        JOIN user_profile_links upl ON upl.user_profile_id = up.id
        WHERE up.is_mentor = 1
          AND upl.source_username IS NOT NULL AND upl.source_username != ''
    """)
    names = [r["source_username"] for r in (rows or [])]
    profile_names = db.fetch_all(
        "SELECT display_name FROM user_profiles WHERE is_mentor = 1")
    for p in (profile_names or []):
        if p["display_name"] and p["display_name"] not in names:
            names.append(p["display_name"])
    return names


# ─────────────────────────────────────────────────────────────────────
# Singleton accessor
# ─────────────────────────────────────────────────────────────────────

_db_instance = None

def get_db() -> DatabaseManager:
    """Get the singleton DatabaseManager instance."""
    global _db_instance
    if _db_instance is None:
        _db_instance = DatabaseManager()
    return _db_instance
