#!/bin/bash
set -a
source /opt/tickles/.env 2>/dev/null
set +a
export PGPASSWORD="${DB_PASSWORD}"
psql -h 127.0.0.1 -p 5432 -U admin -d tickles_shared -c "SELECT COUNT(*) AS rows, MAX(snapshot_at) AS latest FROM derivatives_snapshots;"
echo "--- latest 3 rows ---"
psql -h 127.0.0.1 -p 5432 -U admin -d tickles_shared -c "SELECT ds.snapshot_at, i.exchange, i.symbol, ds.funding_rate, ds.source FROM derivatives_snapshots ds JOIN instruments i ON i.id=ds.instrument_id ORDER BY snapshot_at DESC LIMIT 5;"
