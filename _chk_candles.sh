#!/usr/bin/env bash
set -e
export PGPASSWORD=paperclip
psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -c "SELECT tablename FROM pg_tables WHERE tablename LIKE 'candle%' ORDER BY tablename LIMIT 20;"
echo "---"
psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -c "SELECT COUNT(*) FROM candles;" 2>&1 | head -n 5 || true
echo "---"
psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -c "SELECT venue, symbol, timeframe, COUNT(*) FROM candles GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 5;" 2>&1 | head -n 10 || true
