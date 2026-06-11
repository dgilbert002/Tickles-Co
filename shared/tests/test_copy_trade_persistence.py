"""
Regression tests for the Round-7 copy-trade persistence layer.

Guards against re-introduction of:
  1. Hard-coded $1000 / 0-trade defaults bypassing persistence on startup.
  2. The `_started_at` filter that orphaned existing open positions on restart.
  3. NAME_TO_ID drift between the three places it used to be duplicated.
  4. Missing _save_agent / _save_state hooks on open + close paths.
  5. The migration / backfill script keeping their column contracts.

The tests are pure source-string assertions (the same style as
`test_bug_hunt_2026_05_24_round2.py`) so they run without a live DB and
finish in milliseconds.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path("/opt/tickles")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase 1 — persistence layer wiring
# ---------------------------------------------------------------------------

class TestRound7_NameToIdIsModuleConstant(unittest.TestCase):
    """`NAME_TO_ID` was duplicated in 3 functions. Round-7 factored it to
    a module-level constant. Assert there's exactly ONE definition and
    the three known callers reference it."""

    def test_module_constant_exists(self) -> None:
        src = _read("shared/intelligence/copy_trade_monitor.py")
        # Module-level: matches `NAME_TO_ID = {` at column 0.
        self.assertRegex(src, r"(?m)^NAME_TO_ID = \{")
        self.assertRegex(src, r"(?m)^ID_TO_NAME = \{")

    def test_no_inline_redefinitions(self) -> None:
        src = _read("shared/intelligence/copy_trade_monitor.py")
        # The previous in-function dict literals had this exact pattern.
        # It must not appear anywhere except the module-level definition.
        # Count `NAME_TO_ID = {` occurrences — must be exactly 1.
        defs = re.findall(r"\bNAME_TO_ID = \{", src)
        self.assertEqual(
            len(defs), 1,
            f"Round-7: NAME_TO_ID redefinition leaked back in (found {len(defs)} "
            f"definitions). It must be a single module-level constant.",
        )


class TestRound7_PersistenceMethodsExist(unittest.TestCase):
    """The three persistence hooks must be present and properly named."""

    def setUp(self) -> None:
        self.src = _read("shared/intelligence/copy_trade_monitor.py")

    def test_load_state_method(self) -> None:
        self.assertIn("async def _load_state(self)", self.src)
        self.assertIn("FROM public.copy_agent_state", self.src)

    def test_save_agent_method(self) -> None:
        self.assertIn("async def _save_agent(self, agent_name: str)", self.src)
        self.assertIn("INSERT INTO public.copy_agent_state", self.src)
        self.assertIn("ON CONFLICT (agent_id) DO UPDATE SET", self.src)

    def test_save_state_bulk_method(self) -> None:
        self.assertIn("async def _save_state(self)", self.src)


class TestRound7_LoadStateWiredIntoStartup(unittest.TestCase):
    """`run_forever` MUST call `_load_state()` before the first tick.
    Without this, persistence is functionally a no-op."""

    def test_run_forever_calls_load_state(self) -> None:
        src = _read("shared/intelligence/copy_trade_monitor.py")
        self.assertRegex(
            src,
            r"async def run_forever\(self\)[\s\S]+?await self\._load_state\(\)",
        )

    def test_load_called_before_first_tick(self) -> None:
        src = _read("shared/intelligence/copy_trade_monitor.py")
        # The position of _load_state must precede the `while not self._stop`
        # loop within run_forever.
        m = re.search(
            r"async def run_forever\(self\):([\s\S]+?)while not self\._stop\.is_set",
            src,
        )
        self.assertIsNotNone(m, "run_forever signature changed")
        body = m.group(1)
        self.assertIn(
            "_load_state()", body,
            "Round-7: _load_state must be invoked before the run-loop. "
            "Putting it inside the loop would re-hydrate every tick and "
            "clobber in-memory updates.",
        )


class TestRound7_SaveAgentWiredIntoOpenAndClosePaths(unittest.TestCase):
    """Every code path that mutates `agent['balance']` / `open_positions`
    MUST call `_save_agent` so the next restart sees the change."""

    def setUp(self) -> None:
        self.src = _read("shared/intelligence/copy_trade_monitor.py")

    def test_open_path_calls_save_agent(self) -> None:
        # The open path is in `_enter_position` (or named differently);
        # detect via the position-append + _log_trade_open block where
        # we wired the save.
        self.assertRegex(
            self.src,
            r"_log_trade_open\(agent_name, agent\[\"open_positions\"\]\[-1\], sym\)"
            r"[\s\S]+?await self\._save_agent\(agent_name\)",
        )

    def test_close_path_calls_save_agent(self) -> None:
        self.assertRegex(
            self.src,
            r"async def _close_agent_position[\s\S]+?"
            r"await self\._save_agent\(agent_name\)",
        )

    def test_contest_score_push_calls_save_state(self) -> None:
        self.assertRegex(
            self.src,
            r"async def _update_contest_scores[\s\S]+?await self\._save_state\(\)",
        )


# ---------------------------------------------------------------------------
# Phase 2 — re-attach to existing open positions on startup
# ---------------------------------------------------------------------------

class TestRound7_StartedAtFilterRemoved(unittest.TestCase):
    """The `signal_timestamp >= self._started_at` filter caused every
    restart to orphan currently-open trader positions. Round-7 replaces
    it with a 7-day lookback."""

    def test_no_started_at_filter(self) -> None:
        src = _read("shared/intelligence/copy_trade_monitor.py")
        # The OLD predicate was `tp.signal_timestamp >= $1` parameterised
        # with `self._started_at`. The NEW predicate uses INTERVAL.
        self.assertNotRegex(
            src,
            r"signal_timestamp >= \$1[\s\S]+?self\._started_at",
            "Round-7 Phase 2: the `signal_timestamp >= self._started_at` "
            "filter is back. Restarts will orphan open positions again.",
        )

    def test_uses_7_day_lookback(self) -> None:
        src = _read("shared/intelligence/copy_trade_monitor.py")
        self.assertIn(
            "signal_timestamp >= NOW() - INTERVAL '7 days'", src,
            "Round-7 Phase 2: 7-day lookback predicate missing — without "
            "it, restart re-attach can't find existing open positions.",
        )


class TestRound7_LoadStateRebuildsEnteredSet(unittest.TestCase):
    """Without rebuilding `_entered_positions` at load time, the next
    tick double-enters every position we already paper-traded."""

    def test_load_state_queries_competition_trades(self) -> None:
        src = _read("shared/intelligence/copy_trade_monitor.py")
        self.assertRegex(
            src,
            r"async def _load_state[\s\S]+?"
            r"FROM competition_trades[\s\S]+?"
            r"self\._entered_positions\.add",
        )


# ---------------------------------------------------------------------------
# Phase 3 — backfill script + Phase 0 migration shape
# ---------------------------------------------------------------------------

class TestRound7_MigrationShape(unittest.TestCase):
    """Schema columns must match what `_save_agent` writes."""

    def setUp(self) -> None:
        self.sql = _read(
            "shared/intelligence/migrations/2026_05_24_copy_agent_state.sql"
        )

    def test_table_exists(self) -> None:
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS public.copy_agent_state", self.sql,
        )

    def test_required_columns(self) -> None:
        for col in (
            "agent_id", "balance", "starting_balance", "total_pnl",
            "total_fees", "wins", "losses", "trades",
            "open_positions", "entered_position_ids", "updated_at",
        ):
            self.assertIn(col, self.sql, f"missing column: {col}")

    def test_seven_known_agents_seeded(self) -> None:
        for agent_id in (
            "copy_spot_seq", "copy_lev_parallel", "copy_lev_be_lock",
            "copy_opt_spot_seq", "copy_opt_lev_parallel",
            "copy_opt_lev_be_lock", "copy_charthacker",
        ):
            self.assertIn(f"'{agent_id}'", self.sql, f"missing seed: {agent_id}")

    def test_idempotent_seed(self) -> None:
        self.assertIn("ON CONFLICT (agent_id) DO NOTHING", self.sql)


class TestRound7_BackfillScriptShape(unittest.TestCase):
    """The one-shot recovery script must be safe and have a dry-run."""

    def setUp(self) -> None:
        self.src = _read("shared/scripts/backfill_copy_agent_state.py")

    def test_dry_run_default(self) -> None:
        # The argparse flag is named `--apply` which means absence of it
        # implies dry-run. Assert that.
        self.assertIn("--apply", self.src)
        self.assertIn("dry-run", self.src.lower())
        # Ensure the main function only writes when apply is True.
        self.assertRegex(
            self.src,
            r"if not apply:[\s\S]+?dry-run; no writes performed",
        )

    def test_uses_upsert_for_idempotency(self) -> None:
        self.assertIn("ON CONFLICT (agent_id) DO UPDATE SET", self.src)

    def test_aggregates_from_competition_trades(self) -> None:
        self.assertIn("FROM competition_trades", self.src)
        self.assertIn("contest_id=$1 AND agent_id=$2", self.src)

    def test_seven_known_agents(self) -> None:
        for agent_id in (
            "copy_spot_seq", "copy_lev_parallel", "copy_lev_be_lock",
            "copy_opt_spot_seq", "copy_opt_lev_parallel",
            "copy_opt_lev_be_lock", "copy_charthacker",
        ):
            self.assertIn(f'"{agent_id}"', self.src, f"missing agent: {agent_id}")


# ---------------------------------------------------------------------------
# Phase 4 — dashboard reads dedicated columns (already in place; smoke test)
# ---------------------------------------------------------------------------

class TestRound7_DashboardReadsContestParticipants(unittest.TestCase):
    """The dashboard route must continue reading equity_usd /
    realized_pnl_usd / unrealized_pnl_usd from contest_participants —
    `_update_contest_scores` writes those columns and the dashboard
    reads them. If the read changes, this whole pipeline goes silent."""

    def test_dashboard_reads_required_columns(self) -> None:
        src = _read("shared/dashboard/snapshot.py")
        self.assertIn("equity_usd", src)
        self.assertIn("realized_pnl_usd", src)
        self.assertIn("unrealized_pnl_usd", src)
        self.assertIn("FROM contest_participants", src)


if __name__ == "__main__":
    unittest.main()
