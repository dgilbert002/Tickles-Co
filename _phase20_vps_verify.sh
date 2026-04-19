#!/usr/bin/env bash
set -e
cd /opt/tickles
git fetch origin main
git reset --hard origin/main
echo "=== HEAD ==="
git log -1 --oneline

echo "=== Features CLI: list ==="
/opt/tickles/.venv/bin/python -m shared.cli.features_cli list

echo "=== Features CLI: describe returns_basic ==="
/opt/tickles/.venv/bin/python -m shared.cli.features_cli describe returns_basic

echo "=== Features CLI: materialize returns_basic from candles DB ==="
/opt/tickles/.venv/bin/python -m shared.cli.features_cli materialize \
  --view returns_basic --entity binance:BTC/USDT \
  --symbol BTC/USDT --venue binance --timeframe 1m --limit 500

echo "=== Redis key present? ==="
redis-cli HGETALL tickles:fv:returns_basic:binance:BTC/USDT 2>&1 | head -n 20

echo "=== Parquet file present? ==="
ls -la /opt/tickles/var/features/returns_basic/ 2>&1 || true

echo "=== Features CLI: online-get ==="
/opt/tickles/.venv/bin/python -m shared.cli.features_cli online-get \
  --view returns_basic --entity binance:BTC/USDT

echo "=== Features CLI: partitions ==="
/opt/tickles/.venv/bin/python -m shared.cli.features_cli partitions --view returns_basic

echo "=== Pytest full suite ==="
/opt/tickles/.venv/bin/python -m pytest shared/tests/ -q 2>&1 | tail -n 5

echo "=== Systemd still active? ==="
systemctl is-active paperclip tickles-bt-workers tickles-candle-daemon tickles-catalog tickles-md-gateway

echo "=== Done ==="
