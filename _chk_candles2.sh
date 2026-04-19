#!/usr/bin/env bash
export PGPASSWORD=paperclip
echo "=== search all DBs for candles tables ==="
for db in paperclip postgres; do
  echo "--- db=$db ---"
  psql -h 127.0.0.1 -p 54329 -U paperclip -d $db -c "SELECT schemaname, tablename FROM pg_tables WHERE tablename LIKE 'candle%' OR tablename LIKE '%ohlcv%' ORDER BY 1,2 LIMIT 30;" 2>&1 | head -n 40
done
echo "=== search all schemas in paperclip ==="
psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -c "SELECT table_schema, table_name FROM information_schema.tables WHERE table_name ILIKE '%candle%' OR table_name ILIKE '%ohlc%' OR table_name ILIKE '%market_data%' ORDER BY 1,2 LIMIT 30;" 2>&1 | head -n 40
echo "=== check candle-daemon log for DB hint ==="
journalctl -u tickles-candle-daemon -n 20 --no-pager 2>&1 | head -n 30
