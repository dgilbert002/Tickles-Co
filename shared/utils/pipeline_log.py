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
from logging.handlers import TimedRotatingFileHandler

LOG_DIR = "/var/log/tickles"
LOG_FILE = os.path.join(LOG_DIR, "pipeline.log")
RETENTION_DAYS = 30

_handler_added = False
_logger = None


def _format_event(event, tp_id, **fields):
    parts = [event, f"tp={tp_id}"]
    for key in (
        "exchange", "account", "agent", "actor",
        "symbol", "side",
        "entry", "SL", "TP", "qty", "lev", "notional",
        "status", "price", "dist_pct", "candle_low", "candle_high",
        "order_id", "fill_price", "slippage",
        "age_h", "reason", "src",
    ):
        if key in fields and fields[key] is not None:
            value = fields[key]
            if isinstance(value, float):
                parts.append(f"{key}={value:.6f}")
            else:
                parts.append(f"{key}={value}")
    return " ".join(parts)


def setup():
    global _handler_added, _logger
    logger = logging.getLogger("tickles.pipeline")
    if _handler_added:
        _logger = logger
        return logger

    os.makedirs(LOG_DIR, exist_ok=True)

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
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _handler_added = True
    _logger = logger
    logger.info(
        "PIPE_INIT pipeline logger installed retention=%ddays path=%s",
        RETENTION_DAYS, LOG_FILE,
    )
    return logger


def get():
    global _logger
    if _logger is None:
        _logger = setup()
    return _logger


def event(name, tp_id, **fields):
    get().info(_format_event(name, tp_id, **fields))
