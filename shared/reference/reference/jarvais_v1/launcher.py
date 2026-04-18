"""
JarvAIs Launcher — Multi-Instance Process Manager

Spawns and monitors one trading process per MT5 account.
Each process runs its own FastAPI signal server, cognitive engine, and MT5 executor.
All processes share the same MySQL and Qdrant databases for collective intelligence.

Features:
- Spawns one Python process per account defined in config.json
- Monitors process health and auto-restarts crashed processes
- Runs the shared web dashboard on a separate port
- Graceful shutdown on Ctrl+C
- Logs per-process output to separate log files
- Daily review scheduler (triggers collective learning at end of day)

Usage:
    python launcher.py                    # Start all accounts
    python launcher.py --account demo1    # Start a single account
    python launcher.py --dashboard-only   # Start only the dashboard
"""

import os
import sys
import json
import time
import signal
import logging
import argparse
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, List

# ─────────────────────────────────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────────────────────────────────

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "launcher.log")
    ]
)

# Silence noisy third-party loggers
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("telethon.client.downloads").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("jarvais.launcher")


# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load the master configuration file."""
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        logger.error("config.json not found. Please create it from config.example.json.")
        sys.exit(1)
    with open(config_path, "r") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────
# Process Manager
# ─────────────────────────────────────────────────────────────────────

class ProcessManager:
    """
    Manages the lifecycle of all JarvAIs trading processes.

    Each MT5 account gets its own subprocess running the trading_process.py script.
    The dashboard runs as a separate subprocess.
    """

    def __init__(self, config: dict):
        self.config = config
        self.processes: Dict[str, subprocess.Popen] = {}
        self.dashboard_process: Optional[subprocess.Popen] = None
        self.review_process: Optional[subprocess.Popen] = None
        self.running = False
        self.restart_counts: Dict[str, int] = {}
        self.max_restarts = 10  # Max restarts per account before giving up
        self.restart_cooldown = 30  # Seconds between restart attempts
        self.last_restart: Dict[str, float] = {}
        self.last_daily_review: Optional[datetime] = None
        self.project_dir = Path(__file__).parent

    def start_all(self, single_account: Optional[str] = None):
        """Start all trading processes and the dashboard."""
        self.running = True
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        accounts = self.config.get("accounts", [])
        if not accounts:
            logger.error("No accounts defined in config.json")
            sys.exit(1)

        # Filter to single account if specified
        if single_account:
            accounts = [a for a in accounts if a.get("account_id") == single_account]
            if not accounts:
                logger.error(f"Account '{single_account}' not found in config.json")
                sys.exit(1)

        logger.info(f"Starting JarvAIs with {len(accounts)} account(s)")

        # Start dashboard first
        self._start_dashboard()

        # Start each trading process
        for account in accounts:
            account_id = account.get("account_id", "unknown")
            self._start_trading_process(account_id, account)

        # Enter monitoring loop
        self._monitor_loop()

    def start_dashboard_only(self):
        """Start only the web dashboard without trading processes."""
        self.running = True
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        logger.info("Starting JarvAIs Dashboard only")
        self._start_dashboard()

        # Keep running
        while self.running:
            time.sleep(5)
            if self.dashboard_process and self.dashboard_process.poll() is not None:
                logger.warning("Dashboard process died, restarting...")
                self._start_dashboard()

    def _start_trading_process(self, account_id: str, account_config: dict):
        """Start a single trading process for an account."""
        log_file = LOG_DIR / f"account_{account_id}.log"
        env = os.environ.copy()
        env["JARVAIS_ACCOUNT_ID"] = account_id
        env["JARVAIS_ACCOUNT_CONFIG"] = json.dumps(account_config)

        # Each account gets its own signal server port
        base_port = self.config.get("signal_server", {}).get("base_port", 8001)
        account_index = next(
            (i for i, a in enumerate(self.config.get("accounts", []))
             if a.get("account_id") == account_id), 0
        )
        port = base_port + account_index
        env["JARVAIS_SIGNAL_PORT"] = str(port)

        cmd = [
            sys.executable, str(self.project_dir / "trading_process.py"),
            "--account", account_id,
            "--port", str(port)
        ]

        try:
            with open(log_file, "a") as lf:
                lf.write(f"\n{'='*60}\n")
                lf.write(f"Process started at {datetime.now().isoformat()}\n")
                lf.write(f"{'='*60}\n")

            process = subprocess.Popen(
                cmd,
                env=env,
                stdout=open(log_file, "a"),
                stderr=subprocess.STDOUT,
                cwd=str(self.project_dir)
            )

            self.processes[account_id] = process
            self.restart_counts[account_id] = self.restart_counts.get(account_id, 0)
            logger.info(
                f"Started trading process for account '{account_id}' "
                f"(PID: {process.pid}, Signal Port: {port})"
            )
        except Exception as e:
            logger.error(f"Failed to start process for account '{account_id}': {e}")

    def _start_dashboard(self):
        """Start the web dashboard process."""
        dashboard_port = self.config.get("dashboard", {}).get("port", 5000)
        log_file = LOG_DIR / "dashboard.log"

        cmd = [
            sys.executable, str(self.project_dir / "dashboard_process.py"),
            "--port", str(dashboard_port)
        ]

        try:
            self.dashboard_process = subprocess.Popen(
                cmd,
                stdout=open(log_file, "a"),
                stderr=subprocess.STDOUT,
                cwd=str(self.project_dir)
            )
            logger.info(
                f"Started dashboard on port {dashboard_port} "
                f"(PID: {self.dashboard_process.pid})"
            )
        except Exception as e:
            logger.error(f"Failed to start dashboard: {e}")

    def _monitor_loop(self):
        """
        Main monitoring loop. Checks process health every 10 seconds.
        Auto-restarts crashed processes. Triggers daily reviews.
        """
        logger.info("Entering monitoring loop (Ctrl+C to stop)")

        while self.running:
            time.sleep(10)

            # Check each trading process
            for account_id, process in list(self.processes.items()):
                if process.poll() is not None:
                    exit_code = process.returncode
                    logger.warning(
                        f"Trading process for '{account_id}' exited "
                        f"with code {exit_code}"
                    )

                    # Check restart limits
                    self.restart_counts[account_id] = \
                        self.restart_counts.get(account_id, 0) + 1

                    if self.restart_counts[account_id] > self.max_restarts:
                        logger.error(
                            f"Account '{account_id}' exceeded max restarts "
                            f"({self.max_restarts}). Not restarting."
                        )
                        continue

                    # Check cooldown
                    last = self.last_restart.get(account_id, 0)
                    if time.time() - last < self.restart_cooldown:
                        logger.info(
                            f"Waiting for cooldown before restarting '{account_id}'"
                        )
                        continue

                    # Restart
                    logger.info(
                        f"Restarting '{account_id}' "
                        f"(attempt {self.restart_counts[account_id]}/{self.max_restarts})"
                    )
                    account_config = next(
                        (a for a in self.config.get("accounts", [])
                         if a.get("account_id") == account_id), {}
                    )
                    self._start_trading_process(account_id, account_config)
                    self.last_restart[account_id] = time.time()

            # Check dashboard
            if self.dashboard_process and self.dashboard_process.poll() is not None:
                logger.warning("Dashboard process died, restarting...")
                self._start_dashboard()

            # Check if daily review is due
            self._check_daily_review()

    def _check_daily_review(self):
        """
        Trigger the daily collective review at the configured time.
        Default: 23:00 server time (after most markets close).
        """
        review_hour = self.config.get("daily_review", {}).get("hour", 23)
        review_minute = self.config.get("daily_review", {}).get("minute", 0)
        now = datetime.now()

        # Check if it's review time and we haven't done it today
        if (now.hour == review_hour and
                now.minute >= review_minute and
                (self.last_daily_review is None or
                 self.last_daily_review.date() < now.date())):

            logger.info("Triggering daily collective review...")
            self._run_daily_review()
            self.last_daily_review = now

    def _run_daily_review(self):
        """
        Run the daily collective review process.
        This analyzes all accounts' performance and generates
        cross-account insights for the hive mind.
        """
        log_file = LOG_DIR / "daily_review.log"
        cmd = [
            sys.executable, str(self.project_dir / "daily_review.py")
        ]

        try:
            self.review_process = subprocess.Popen(
                cmd,
                stdout=open(log_file, "a"),
                stderr=subprocess.STDOUT,
                cwd=str(self.project_dir)
            )
            logger.info(f"Daily review started (PID: {self.review_process.pid})")
        except Exception as e:
            logger.error(f"Failed to start daily review: {e}")

    def _handle_shutdown(self, signum, frame):
        """Gracefully shut down all processes."""
        logger.info("Shutdown signal received. Stopping all processes...")
        self.running = False

        # Stop trading processes
        for account_id, process in self.processes.items():
            logger.info(f"Stopping account '{account_id}' (PID: {process.pid})...")
            try:
                process.terminate()
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.warning(f"Force killing '{account_id}'...")
                process.kill()

        # Stop dashboard
        if self.dashboard_process:
            logger.info("Stopping dashboard...")
            try:
                self.dashboard_process.terminate()
                self.dashboard_process.wait(timeout=5)
            except (subprocess.TimeoutExpired, Exception):
                self.dashboard_process.kill()

        # Stop daily review if running
        if self.review_process and self.review_process.poll() is None:
            self.review_process.terminate()

        logger.info("All processes stopped. Goodbye.")
        import threading

        def _force_exit():
            alive = [t for t in threading.enumerate()
                     if t.is_alive() and not t.daemon and t is not threading.main_thread()]
            if alive:
                logger.warning(f"Force-exit: {len(alive)} non-daemon threads still alive: "
                               f"{[t.name for t in alive[:5]]}")
            os._exit(0)

        t = threading.Timer(10.0, _force_exit)
        t.daemon = True
        t.start()
        sys.exit(0)


# ─────────────────────────────────────────────────────────────────────
# Trading Process Entry Point (imported by subprocess)
# ─────────────────────────────────────────────────────────────────────

def create_trading_process_script():
    """
    Create the trading_process.py script that each account subprocess runs.
    This is generated once and reused.
    """
    script_path = Path(__file__).parent / "trading_process.py"
    if script_path.exists():
        return

    script_content = '''"""
JarvAIs Trading Process — Per-Account Entry Point

This script is spawned by the launcher for each MT5 account.
It runs the FastAPI signal server and handles the full trading lifecycle.

Environment variables (set by launcher):
    JARVAIS_ACCOUNT_ID: The account identifier
    JARVAIS_ACCOUNT_CONFIG: JSON string of account configuration
    JARVAIS_SIGNAL_PORT: Port for the signal server
"""

import os
import sys
import json
import logging
import argparse
import asyncio
from pathlib import Path

import uvicorn

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from core.config import get_config, ConfigManager
from core.signal_server import create_signal_app
from db.database import get_db, DatabaseManager

logger = logging.getLogger("jarvais.trading")


def main():
    parser = argparse.ArgumentParser(description="JarvAIs Trading Process")
    parser.add_argument("--account", required=True, help="Account ID")
    parser.add_argument("--port", type=int, default=8001, help="Signal server port")
    args = parser.parse_args()

    # Setup logging for this account
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [{args.account}] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / f"account_{args.account}.log")
        ]
    )

    logger.info(f"Starting trading process for account: {args.account}")
    logger.info(f"Signal server will listen on port: {args.port}")

    # Load configuration
    config = get_config()
    account_config = config.get_account(args.account)
    if not account_config:
        logger.error(f"Account '{args.account}' not found in configuration")
        sys.exit(1)

    # Initialize database connection
    try:
        db = get_db()
        logger.info("Database connection established")
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        sys.exit(1)

    # Create and start the signal server
    # The signal server handles:
    # 1. Receiving EA signals via HTTP POST
    # 2. Triggering the cognitive engine for AI validation
    # 3. Executing approved trades via MT5
    # 4. Running post-trade analysis
    app = create_signal_app(
        account_id=args.account,
        account_config=account_config,
        config=config,
        db=db
    )

    logger.info(f"Trading process ready. Listening for EA signals on port {args.port}")

    # Run the FastAPI server
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=args.port,
        log_level="warning",  # Reduce uvicorn noise
        access_log=False
    )


if __name__ == "__main__":
    main()
'''

    with open(script_path, "w") as f:
        f.write(script_content)
    logger.info(f"Created trading_process.py")


def create_dashboard_process_script():
    """
    Create the dashboard_process.py script.
    """
    script_path = Path(__file__).parent / "dashboard_process.py"
    if script_path.exists():
        return

    script_content = '''"""
JarvAIs Dashboard Process

Runs the web dashboard as a standalone FastAPI server.
"""

import sys
import argparse
import logging
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).parent))

from dashboard.web_dashboard import create_dashboard_app

logger = logging.getLogger("jarvais.dashboard")


def main():
    parser = argparse.ArgumentParser(description="JarvAIs Dashboard")
    parser.add_argument("--port", type=int, default=5000, help="Dashboard port")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [Dashboard] %(levelname)s: %(message)s"
    )

    logger.info(f"Starting JarvAIs Dashboard on port {args.port}")

    app = create_dashboard_app()

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=args.port,
        log_level="warning",
        access_log=False
    )


if __name__ == "__main__":
    main()
'''

    with open(script_path, "w") as f:
        f.write(script_content)
    logger.info(f"Created dashboard_process.py")


def create_daily_review_script():
    """
    Create the daily_review.py script that runs the collective
    end-of-day analysis across all accounts.
    """
    script_path = Path(__file__).parent / "daily_review.py"
    if script_path.exists():
        return

    script_content = '''"""
JarvAIs Daily Review — Collective Intelligence Analysis

Runs at the end of each trading day to:
1. Analyze each account's performance
2. Run the AI self-coaching dialogue
3. Identify cross-account patterns
4. Update the hive mind memory
5. Adjust confidence thresholds if warranted
6. Generate the daily performance report
"""

import sys
import json
import logging
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.config import get_config
from core.model_interface import get_model_interface
from core.memory_manager import MemoryManager
from db.database import get_db
from analytics.performance_engine import PerformanceEngine

logger = logging.getLogger("jarvais.review")


def run_daily_review():
    """Execute the full daily review process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [DailyReview] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(Path(__file__).parent / "logs" / "daily_review.log")
        ]
    )

    logger.info("=" * 60)
    logger.info(f"DAILY REVIEW — {date.today().isoformat()}")
    logger.info("=" * 60)

    # Initialize components using singleton pattern
    config = get_config()
    db = get_db()
    model = get_model_interface()
    memory = MemoryManager()
    performance = PerformanceEngine()

    accounts = config.accounts  # Dict[str, AccountConfig]
    today = date.today().isoformat()

    # ── Step 1: Per-Account Analysis ──
    all_account_summaries = []

    for account_id, account in accounts.items():
        logger.info(f"Analyzing account: {account_id}")

        try:
            # Get today's performance
            daily_perf = performance.calculate_daily_performance(account_id, today)

            # Get comparison data
            comparison = performance.get_ea_vs_ai_comparison(account_id, days=30)

            summary = {
                "account_id": account_id,
                "is_live": getattr(account, "is_live", False),
                "today_trades": daily_perf.get("total_trades", 0),
                "today_pnl": daily_perf.get("actual_pnl", 0),
                "today_wins": daily_perf.get("wins", 0),
                "today_losses": daily_perf.get("losses", 0),
                "month_win_rate": comparison.jarvais_win_rate if comparison else 0,
                "month_ai_alpha": comparison.ai_alpha if comparison else 0,
                "month_veto_accuracy": comparison.classification.veto_accuracy if comparison else 0,
            }
            all_account_summaries.append(summary)

            # Save daily performance to database
            performance.save_daily_performance(account_id, daily_perf)

            logger.info(
                f"  {account_id}: {daily_perf.get('total_trades', 0)} trades, "
                f"P&L: ${daily_perf.get('actual_pnl', 0):.2f}, "
                f"Win Rate: {daily_perf.get('win_rate', 0):.1%}"
            )

        except Exception as e:
            logger.error(f"  Failed to analyze {account_id}: {e}")

    # ── Step 2: AI Self-Coaching Review ──
    logger.info("Running AI self-coaching review...")

    review_prompt = f"""You are the JarvAIs Trading Intelligence performing your daily self-review.

## Today's Date: {today}

## Account Performance Summary:
{json.dumps(all_account_summaries, indent=2)}

## Your Task:
Perform a thorough, honest self-assessment of today's trading. You must:

1. **Identify what went well today** — Which trades were good decisions and why?
2. **Identify what went wrong** — Which trades were mistakes and what caused them?
3. **Identify patterns** — Are there recurring themes in your wins or losses?
4. **Cross-account insights** — If multiple accounts traded, what can they learn from each other?
5. **Confidence calibration** — Were your confidence scores accurate? Did high-confidence trades actually win more?
6. **Actionable improvements** — What specific changes should you make tomorrow?
7. **Risk assessment** — Are you taking appropriate risk? Too much? Too little?

Be brutally honest. Do not make excuses. Focus on what YOU can control and improve.

Respond in JSON format:
{{
    "overall_assessment": "string (1-2 paragraphs)",
    "what_worked": ["list of specific things that worked"],
    "what_failed": ["list of specific things that failed"],
    "patterns_discovered": ["list of patterns noticed"],
    "cross_account_insights": ["list of insights from comparing accounts"],
    "confidence_calibration": "string (assessment of confidence accuracy)",
    "improvements_for_tomorrow": ["list of specific actionable improvements"],
    "risk_assessment": "string (are we taking appropriate risk?)",
    "overall_grade": "A/B/C/D/F",
    "mood": "string (how would you describe your trading performance today?)"
}}"""

    try:
        review_result = model.query(
            role="daily_review",
            system_prompt="You are the JarvAIs Trading Intelligence performing your daily self-review. Respond in JSON format.",
            user_prompt=review_prompt,
            context="launcher_daily_review",
            source="system",
            source_detail="launcher_daily_review",
            media_type="text",
        )

        # Parse ModelResponse into dict for downstream use
        review_response = None
        if review_result and review_result.success and review_result.content:
            import re as _re
            raw = review_result.content.strip()
            raw = _re.sub(r'^```(?:json)?\s*', '', raw)
            raw = _re.sub(r'\s*```$', '', raw)
            try:
                review_response = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"Daily review: could not parse JSON, storing raw text")
                review_response = {"overall_assessment": raw, "overall_grade": "N/A"}

        if review_response:
            # Store the review in memory
            memory.store_memory(
                collection="daily_reviews",
                content=json.dumps(review_response),
                metadata={
                    "date": today,
                    "type": "daily_review",
                    "grade": review_response.get("overall_grade", "N/A"),
                    "total_accounts": len(all_account_summaries),
                    "total_pnl": sum(s.get("today_pnl", 0) for s in all_account_summaries)
                }
            )

            logger.info(f"Daily review complete. Grade: {review_response.get('overall_grade', 'N/A')}")
            logger.info(f"Assessment: {review_response.get('overall_assessment', 'N/A')[:200]}...")

            # Log improvements
            improvements = review_response.get("improvements_for_tomorrow", [])
            for i, imp in enumerate(improvements, 1):
                logger.info(f"  Improvement {i}: {imp}")

    except Exception as e:
        logger.error(f"AI self-coaching review failed: {e}")

    # ── Step 3: Confidence Threshold Self-Adjustment ──
    logger.info("Checking confidence threshold optimization...")

    for account_id, account in accounts.items():
        try:
            analysis = performance.get_confidence_analysis(account_id, days=30)
            if analysis and analysis.get("buckets"):
                buckets = analysis["buckets"]

                # Find the optimal threshold
                # Look for the lowest confidence level where win rate exceeds 55%
                optimal_threshold = None
                for bucket_label in sorted(buckets.keys()):
                    bucket = buckets[bucket_label]
                    if bucket.get("total_trades", 0) >= 10:  # Need enough data
                        if bucket.get("win_rate", 0) >= 0.55:
                            # Extract the lower bound of the bucket range
                            try:
                                threshold = int(bucket_label.split("-")[0])
                                if optimal_threshold is None or threshold < optimal_threshold:
                                    optimal_threshold = threshold
                            except (ValueError, IndexError):
                                pass

                if optimal_threshold:
                    current = config.raw.get("risk_management", {}).get("confidence_threshold", 65)
                    if optimal_threshold != current:
                        logger.info(
                            f"  {account_id}: Suggesting confidence threshold "
                            f"adjustment from {current} to {optimal_threshold} "
                            f"(based on 30-day win rate analysis)"
                        )
                        # Note: We log the suggestion but don't auto-adjust yet.
                        # In Phase 2 maturity, this becomes automatic.

        except Exception as e:
            logger.error(f"  Confidence analysis failed for {account_id}: {e}")

    logger.info("=" * 60)
    logger.info("DAILY REVIEW COMPLETE")
    logger.info("=" * 60)


if __name__ == "__main__":
    run_daily_review()
'''

    with open(script_path, "w") as f:
        f.write(script_content)
    logger.info(f"Created daily_review.py")


# ─────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="JarvAIs Trading Intelligence — Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python launcher.py                    Start all accounts + dashboard
    python launcher.py --account demo1    Start only the 'demo1' account
    python launcher.py --dashboard-only   Start only the web dashboard
        """
    )
    parser.add_argument(
        "--account",
        help="Start only a specific account (by account_id)"
    )
    parser.add_argument(
        "--dashboard-only",
        action="store_true",
        help="Start only the web dashboard without trading"
    )
    args = parser.parse_args()

    # Banner
    print("""
    ╔══════════════════════════════════════════════════════╗
    ║                                                      ║
    ║        ██╗ █████╗ ██████╗ ██╗   ██╗ █████╗ ██╗      ║
    ║        ██║██╔══██╗██╔══██╗██║   ██║██╔══██╗██║      ║
    ║        ██║███████║██████╔╝██║   ██║███████║██║      ║
    ║   ██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██╔══██║██║      ║
    ║   ╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║  ██║██║      ║
    ║    ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝  ╚═╝╚═╝      ║
    ║                                                      ║
    ║          Trading Intelligence System v1.0             ║
    ║                                                      ║
    ╚══════════════════════════════════════════════════════╝
    """)

    # Load config
    config = load_config()

    # Generate subprocess scripts if they don't exist
    create_trading_process_script()
    create_dashboard_process_script()
    create_daily_review_script()

    # Start the process manager
    manager = ProcessManager(config)

    if args.dashboard_only:
        manager.start_dashboard_only()
    else:
        manager.start_all(single_account=args.account)


if __name__ == "__main__":
    main()
