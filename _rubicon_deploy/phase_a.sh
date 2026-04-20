#!/bin/bash
# Phase A: Infrastructure prep (idempotent).
# - Create tickles_rubicon DB cloned from tickles_jarvais schema
# - Register Tickles MCP with OpenClaw
# - Install funding-rate collector service
set -u

LOG=/root/rubicon-deploy.log
mkdir -p /root
touch "$LOG"
log() { echo "$(date -u +%FT%TZ) [phase-a] $*" | tee -a "$LOG"; }

log "===== PHASE A START ====="

set -a
source /opt/tickles/.env 2>/dev/null
set +a
: "${DB_HOST:=127.0.0.1}"
: "${DB_PORT:=5432}"
: "${DB_USER:=admin}"
export PGPASSWORD="${DB_PASSWORD}"

# ---------- A1: clone schema to tickles_rubicon ----------
log "A1: check/create tickles_rubicon"
EXISTS=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -At -c "SELECT 1 FROM pg_database WHERE datname='tickles_rubicon';" 2>&1)
if [ "$EXISTS" = "1" ]; then
  log "A1: tickles_rubicon already exists (idempotent skip-create)"
else
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -c "CREATE DATABASE tickles_rubicon OWNER admin;" 2>&1 | tee -a "$LOG"
fi

# Clone schema (no data) from tickles_jarvais
log "A1: clone schema from tickles_jarvais -> tickles_rubicon"
if command -v pg_dump >/dev/null 2>&1; then
  pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d tickles_jarvais --schema-only --no-owner --no-privileges 2>/dev/null \
    | psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d tickles_rubicon -v ON_ERROR_STOP=0 2>&1 | tail -20 | tee -a "$LOG"
else
  log "A1: pg_dump not present — attempting apt install"
  apt-get install -y postgresql-client >/dev/null 2>&1
  pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d tickles_jarvais --schema-only --no-owner --no-privileges 2>/dev/null \
    | psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d tickles_rubicon -v ON_ERROR_STOP=0 2>&1 | tail -20 | tee -a "$LOG"
fi

log "A1: verify tickles_rubicon tables"
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d tickles_rubicon -c "\dt" 2>&1 | tee -a "$LOG"

# ---------- A2: register Tickles MCP with OpenClaw ----------
log "A2: register Tickles MCP with OpenClaw"
# Read Tickles MCP token if set
TICKLES_MCP_TOKEN=""
if [ -f /etc/tickles/mcp.env ]; then
  TICKLES_MCP_TOKEN=$(grep -E '^TICKLES_MCP_TOKEN=' /etc/tickles/mcp.env | head -1 | cut -d= -f2- | tr -d '"'"'")
fi
log "A2: tickles-mcpd daemon status: $(systemctl is-active tickles-mcpd.service 2>/dev/null)"
log "A2: healthz: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:7777/healthz)"

MCP_JSON='{"transport":"http","url":"http://127.0.0.1:7777/mcp"'
if [ -n "$TICKLES_MCP_TOKEN" ]; then
  MCP_JSON="${MCP_JSON},\"headers\":{\"Authorization\":\"Bearer ${TICKLES_MCP_TOKEN}\"}"
fi
MCP_JSON="${MCP_JSON}}"

log "A2: openclaw mcp set tickles '<json>' (token len=${#TICKLES_MCP_TOKEN})"
echo "$MCP_JSON" | openclaw mcp set tickles /dev/stdin 2>&1 | tail -10 | tee -a "$LOG" || \
  openclaw mcp set tickles "$MCP_JSON" 2>&1 | tail -10 | tee -a "$LOG"

log "A2: listing MCP servers in openclaw now"
openclaw mcp list 2>&1 | head -20 | tee -a "$LOG"

# ---------- A3: funding-rate collector ----------
log "A3: install /opt/tickles/shared/daemons/funding_collector.py"
mkdir -p /opt/tickles/shared/daemons
cat > /opt/tickles/shared/daemons/funding_collector.py <<'PY'
"""Minimal funding-rate collector.

Polls CCXT funding rate for a short list of perpetual symbols every 60s
and INSERTs into derivatives_snapshots. Idempotent via unique constraint
(instrument_id, source, snapshot_at).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone

import ccxt
import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOG = logging.getLogger("tickles.funding_collector")

DB_DSN = os.environ.get(
    "DATABASE_URL",
    f"postgres://{os.environ.get('DB_USER','admin')}:{os.environ.get('DB_PASSWORD','')}"
    f"@{os.environ.get('DB_HOST','127.0.0.1')}:{os.environ.get('DB_PORT','5432')}/tickles_shared",
)
POLL_S = int(os.environ.get("FUNDING_POLL_S", "60"))
SYMBOLS = os.environ.get("FUNDING_SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT").split(",")
EXCHANGE = os.environ.get("FUNDING_EXCHANGE", "binance")

_stop = asyncio.Event()


async def ensure_instrument(pool: asyncpg.Pool, exchange: str, symbol: str) -> int:
    """Return instrument_id, creating a minimal row if missing.

    Existing convention: asset_class='crypto', symbol uses slash form (BTC/USDT).
    We strip any :USDT perp suffix because that's a ccxt market-type modifier,
    not part of the canonical symbol.
    """
    norm_symbol = symbol.split(":")[0]  # keep slash; drop :USDT perp suffix
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT id FROM instruments WHERE exchange=$1 AND symbol=$2 LIMIT 1",
            exchange, norm_symbol,
        )
        if row:
            return row["id"]
        row = await con.fetchrow(
            """INSERT INTO instruments (exchange, symbol, asset_class, is_active)
               VALUES ($1, $2, 'crypto', true) RETURNING id""",
            exchange, norm_symbol,
        )
        LOG.info("created instrument %s/%s id=%s", exchange, norm_symbol, row["id"])
        return row["id"]


async def poll_once(pool: asyncpg.Pool) -> int:
    ex = getattr(ccxt, EXCHANGE)({"enableRateLimit": True})
    rows_written = 0
    for sym in SYMBOLS:
        sym = sym.strip()
        if not sym:
            continue
        try:
            r = await asyncio.to_thread(ex.fetch_funding_rate, sym)
        except Exception as exc:
            LOG.warning("fetch_funding_rate %s failed: %s", sym, exc)
            continue
        rate = r.get("fundingRate")
        ts_ms = r.get("timestamp") or r.get("fundingTimestamp")
        if rate is None:
            continue
        if isinstance(ts_ms, (int, float)) and ts_ms > 0:
            as_of = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        else:
            as_of = datetime.now(timezone.utc)
        inst_id = await ensure_instrument(pool, EXCHANGE, sym)
        async with pool.acquire() as con:
            try:
                await con.execute(
                    """INSERT INTO derivatives_snapshots
                       (instrument_id, snapshot_at, funding_rate, source)
                       VALUES ($1, $2, $3, $4)
                       ON CONFLICT (instrument_id, source, snapshot_at) DO NOTHING""",
                    inst_id, as_of, float(rate), f"ccxt:{EXCHANGE}",
                )
                rows_written += 1
            except Exception as exc:
                LOG.warning("insert failed %s: %s", sym, exc)
    return rows_written


async def main() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop.set)

    pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=2)
    LOG.info("connected to db, poll every %ss, symbols=%s, exchange=%s", POLL_S, SYMBOLS, EXCHANGE)
    try:
        while not _stop.is_set():
            n = await poll_once(pool)
            LOG.info("wrote %d funding snapshots", n)
            try:
                await asyncio.wait_for(_stop.wait(), timeout=POLL_S)
            except asyncio.TimeoutError:
                pass
    finally:
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
PY
chmod +x /opt/tickles/shared/daemons/funding_collector.py

log "A3: install systemd unit tickles-funding-collector.service"
cat > /etc/systemd/system/tickles-funding-collector.service <<'UNIT'
[Unit]
Description=Tickles V2 funding-rate collector (CCXT -> derivatives_snapshots)
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tickles
EnvironmentFile=-/opt/tickles/.env
Environment=PYTHONPATH=/opt/tickles
Environment=FUNDING_POLL_S=60
Environment=FUNDING_EXCHANGE=binance
Environment=FUNDING_SYMBOLS=BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT
ExecStart=/usr/bin/python3 /opt/tickles/shared/daemons/funding_collector.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now tickles-funding-collector.service 2>&1 | tail -5 | tee -a "$LOG"
sleep 5
log "A3: funding-collector status: $(systemctl is-active tickles-funding-collector.service)"
journalctl -u tickles-funding-collector.service -n 15 --no-pager 2>&1 | tail -15 | tee -a "$LOG"

# Quick check: did it write anything?
sleep 5
log "A3: derivatives_snapshots row count now:"
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d tickles_shared -At -c "SELECT COUNT(*) FROM derivatives_snapshots;" 2>&1 | tee -a "$LOG"

log "===== PHASE A END ====="
