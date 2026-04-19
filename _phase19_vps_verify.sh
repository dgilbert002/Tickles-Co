#!/usr/bin/env bash
set -e
cd /opt/tickles
git fetch origin main
git reset --hard origin/main
echo "=== Git HEAD ==="
git log -1 --oneline

echo "=== Install vectorbt in venv (best-effort) ==="
/opt/tickles/.venv/bin/pip install --quiet vectorbt 2>&1 | tail -n 3 || echo "vectorbt install skipped/failed (non-fatal)"

echo "=== Engines CLI: list ==="
/opt/tickles/.venv/bin/python -m shared.cli.engines_cli list

echo "=== Engines CLI: capabilities ==="
/opt/tickles/.venv/bin/python -m shared.cli.engines_cli capabilities

echo "=== Engines CLI: sample classic ==="
/opt/tickles/.venv/bin/python -m shared.cli.engines_cli sample --engine classic

echo "=== Engines CLI: parity ==="
/opt/tickles/.venv/bin/python -m shared.cli.engines_cli parity --engines classic,vectorbt

echo "=== Pytest (full suite) ==="
cd /opt/tickles && /opt/tickles/.venv/bin/python -m pytest shared/tests/ -q 2>&1 | tail -n 10

echo "=== Systemd units still active? ==="
systemctl is-active openclaw paperclip tickles-data-sufficiency tickles-market-data-gateway 2>&1 || true

echo "=== Done ==="
