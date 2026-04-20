#!/bin/bash
# Look up the asset_class_t enum, fix collector, restart.
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [a3-fix] $*" | tee -a "$LOG"; }

set -a
source /opt/tickles/.env 2>/dev/null
set +a
export PGPASSWORD="${DB_PASSWORD}"

log "asset_class_t enum values"
psql -h 127.0.0.1 -p 5432 -U admin -d tickles_shared -At -c "SELECT unnest(enum_range(NULL::asset_class_t));" 2>&1 | tee -a "$LOG"

log "Also check the instruments table columns"
psql -h 127.0.0.1 -p 5432 -U admin -d tickles_shared -c "\d instruments" 2>&1 | head -30 | tee -a "$LOG"

log "pick the right asset_class value - patching collector to use the right one"
# We'll read the first enum value and patch the collector to use it.
# But actually let's look at existing instruments to see what's used.
log "existing instruments (what asset_class values are in use?)"
psql -h 127.0.0.1 -p 5432 -U admin -d tickles_shared -c "SELECT DISTINCT asset_class, COUNT(*) FROM instruments GROUP BY asset_class;" 2>&1 | tee -a "$LOG"

log "also check if a BTC/USDT perp already exists on binance"
psql -h 127.0.0.1 -p 5432 -U admin -d tickles_shared -c "SELECT id, exchange, symbol, asset_class FROM instruments WHERE exchange='binance' AND symbol LIKE '%BTC%' LIMIT 5;" 2>&1 | tee -a "$LOG"
