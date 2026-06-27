"""Pipeline correlation logger for end-to-end trade tracking.

Adds a dedicated log handler that writes structured events to
/var/log/tickles/pipeline.log with 30-day rotation.

Event prefixes (call pipeline_log.event('PREFIX', tp_id, **fields)):
  PIPE_INTE  - interpretation service created a signal
  PIPE_PEND  - tracked_position row created (pending)
  PIPE_ACT   - position activated by wick-touch
  PIPE_CAND  - 1m candle fetched and checked
  PIPE_TPCH  - paper TP hit
  PIPE_SLCH  - paper SL hit
  PIPE_COPY  - paper engine entered a position
  PIPE_EXIT  - paper engine closed a position
  PIPE_BE    - BE-lock applied
  PIPE_BRGE  - demo bridge started/finished processing a signal
  PIPE_QUEUE - demo bridge queued (price >5% or no data)
  PIPE_PROM  - demo bridge promoted queued -> pending
  PIPE_PLACE - demo bridge placed order on exchange
  PIPE_EXPIRE - demo bridge expired/cancelled
  PIPE_FILL  - demo bridge detected fill on exchange
  PIPE_SYNCE - demo bridge synced live position from exchange
  PIPE_MIRR  - demo bridge mirrored paper trade to exchange
  PIPE_REJ   - demo bridge order rejected
  PIPE_DIST  - demo bridge distance check (smart queue band)

Every event includes the tracked_position_id (tp_id) so all events for
one trade can be correlated by grepping the log for the same id.
"""
import logging
import os
import threading
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = "/var/log/tickles"
LOG_FILE = os.path.join(LOG_DIR, "pipeline.log")
RETENTION_DAYS = 30

# Allowed fields. Anything outside this list is dropped (or quoted) to
# keep the log format predictable. Order matters: it determines field
# order in the output line.
_ALLOWED_FIELDS = (
    "exchange", "account", "agent", "actor",
    "symbol", "side",
    "entry", "SL", "TP", "qty", "lev", "notional",
    "status", "price", "dist_pct", "candle_low", "candle_high",
    "order_id", "fill_price", "slippage",
    "age_h", "reason", "src",
)

# Fields that should always render with full precision (not 6dp).
# These are usually IDs and counts, not prices.
_INT_FIELDS = {"lev", "age_h"}

# Thread-safe single-init.
_lock = threading.Lock()
_handler_added: bool = False
_logger = None  # type: ignore[assignment]


def _format_value(key: str, value) -> str:
    """Format a single field value into a log-safe token."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return f"{key}={1 if value else 0}"
    if isinstance(value, int):
        return f"{key}={value}"
    if isinstance(value, float):
        if key in _INT_FIELDS:
            return f"{key}={value:.0f}"
        # Prices/levels: 6dp is enough for sub-cent precision.
        return f"{key}={value:.6f}"
    if isinstance(value, str):
        # Replace spaces and tabs so the log line stays one-field-one-token.
        # Use | to visually separate sub-fields when src packs multiple
        # values (e.g. "scanned=100 expired=0 cancelled=0 elapsed=29.63s").
        safe = value.replace("\t", " ").replace(" |", "|")
        safe = safe.replace(" ", "_")
        return f"{key}={safe}"
    # Functions, classes, methods, generators, etc. are not useful in
    # a structured log line. Drop them silently so a buggy call site
    # doesn't pollute the file.
    return ""


def _format_event(event_name, tp_id, **fields) -> str:
    """Return a one-line structured event string.

    Defensive:
      * non-str event_name → 'BAD_EVENT'
      * unknown fields → included as `[ExtraField:val]`
      * None values are skipped
      * tp_id is always present (default 0 for batch events)

    Example:
      PIPE_PLACE tp=15200 exchange=toobit symbol=APE/USDT:USDT side=long
        entry=0.151100 SL=0.132100 TP=0.163800 qty=120 lev=3 order_id=abc123
    """
    if not isinstance(event_name, str) or not event_name:
        event_name = "BAD_EVENT"
    parts = [event_name, f"tp={tp_id if tp_id is not None else 0}"]
    seen = set()
    for key in _ALLOWED_FIELDS:
        if key in fields:
            seen.add(key)
            formatted = _format_value(key, fields[key])
            if formatted:
                parts.append(formatted)
    # Append unknown fields at the end (preserves debugging info).
    for key, value in fields.items():
        if key in seen or value is None:
            continue
        # Only serializable types.
        if not isinstance(value, (str, int, float, bool)):
            continue
        formatted = _format_value(key, value)
        if formatted:
            parts.append(formatted)
    return " ".join(parts)


def setup():
    """Install the pipeline log handler on the root logger. Idempotent."""
    global _handler_added, _logger
    with _lock:
        if _handler_added:
            return _logger
        logger = logging.getLogger("tickles.pipeline")

        os.makedirs(LOG_DIR, exist_ok=True)

        try:
            handler = TimedRotatingFileHandler(
                LOG_FILE,
                when="midnight",
                interval=1,
                backupCount=RETENTION_DAYS,
                encoding="utf-8",
                utc=False,
            )
            handler.suffix = "%Y-%m-%d"
            handler.setLevel(logging.INFO)
            handler.setFormatter(logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
        except (PermissionError, OSError) as exc:
            # Read-only filesystem or other write issue. Fall back to a
            # stream handler so the daemon still gets *some* output for
            # forensics, but log the failure.
            handler = logging.StreamHandler()
            handler.setLevel(logging.INFO)
            handler.setFormatter(logging.Formatter(
                fmt="%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            print(
                f"PIPE_INIT WARN: could not open {LOG_FILE}: {exc}; "
                f"falling back to stderr"
            )

        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _handler_added = True
        _logger = logger
        logger.info(
            "PIPE_INIT retention=%ddays path=%s",
            RETENTION_DAYS, LOG_FILE,
        )
        return _logger


def get():
    global _logger
    if _logger is None:
        _logger = setup()
    return _logger


def event(name, tp_id, **fields):
    """Emit a structured pipeline event.

    Safe to call from any thread. Never raises — if the logger can't
    format, it falls back to a minimal line.
    """
    try:
        get().info(_format_event(name, tp_id, **fields))
    except Exception as exc:  # noqa: BLE001 - last-line-of-defense
        # Last-ditch fallback: emit a minimal event so the failure is
        # visible in the regular log stream.
        try:
            logger = get()
            logger.error(
                "PIPE_BAD name=%r tp=%r err=%r", name, tp_id, exc,
            )
        except Exception:
            pass
