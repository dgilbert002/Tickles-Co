# Restart Tickles workers — checklist

> Use this after **env changes**, **gateway/model slot changes**, or **mem0 config changes**.
> Python workers load config at startup; a running process will keep stale gateway keys,
> model slots, and mem0 clients until restarted.

Last verified: 2026-05-30.

## When to restart

| Change type | Why restart |
|-------------|-------------|
| `.env` / `~/.bashrc` API keys, `MEM0_MODEL`, DB password | Workers read env once at boot |
| Dashboard model-slot picker (Requesty vs gateway) | `resolve_slot_gateway()` caches per process |
| `shared/utils/mem0_config.py` (cache, embeddings, Qdrant) | `_MEM0_CACHE` is process-local |
| SQL migration on `prompt_versions`, `mem0_recall_log`, etc. | Long-lived daemons may hold old schema assumptions |
| Copy-trade sizing knobs in DB | Copy monitor reloads each tick — **no restart needed** for sizing only |

## Order (recommended)

1. **Postgres / Qdrant** — confirm both up (`systemctl status postgresql`, `docker ps` for qdrant).
2. **Gateway-dependent interpretation chain** (heaviest, restart first):
   ```bash
   sudo systemctl restart tickles-interpretation.service
   sudo systemctl restart tickles-postmortem.service
   sudo systemctl restart tickles-chart-hacker-opinion.service
   ```
3. **Copy / position loop**:
   ```bash
   sudo systemctl restart tickles-copy-trade-monitor.service
   sudo systemctl restart tickles-position-monitor.service
   sudo systemctl restart tickles-demo-bridge.service
   ```
4. **Learning / scoring**:
   ```bash
   sudo systemctl restart tickles-edge-scorer.service
   sudo systemctl restart tickles-coach.service
   sudo systemctl restart tickles-memu-listener.service
   ```
5. **Control plane**:
   ```bash
   sudo systemctl restart tickles-mcpd.service
   sudo systemctl restart tickles-dashboard.service
   ```
6. **Collectors / market data** (only if you changed their env):
   ```bash
   sudo systemctl restart tickles-telegram-collector.service
   sudo systemctl restart tickles-discord-collector.service
   sudo systemctl restart tickles-candle-daemon.service
   sudo systemctl restart tickles-md-gateway.service
   sudo systemctl restart tickles-funding-collector.service
   ```

## One-liner (full rolling restart)

```bash
for u in \
  tickles-interpretation tickles-postmortem tickles-chart-hacker-opinion \
  tickles-copy-trade-monitor tickles-position-monitor tickles-demo-bridge \
  tickles-edge-scorer tickles-coach tickles-memu-listener \
  tickles-mcpd tickles-dashboard; do
  sudo systemctl restart "${u}.service" && echo "ok ${u}" || echo "FAIL ${u}"
done
```

## Verify after restart

```bash
# All tickles units active
systemctl list-units 'tickles-*' --state=running --no-pager

# Recent errors (last 2 min)
journalctl -u tickles-interpretation -u tickles-copy-trade-monitor \
  -u tickles-mcpd --since '2 min ago' --no-pager | tail -40

# MCP alive
curl -s http://127.0.0.1:7777/health | head -c 200

# mem0 recall log accepting signal_source (after migration)
psql -U admin -d tickles_shared -c \
  "SELECT column_name FROM information_schema.columns \
   WHERE table_name='mem0_recall_log' AND column_name='signal_source';"
```

## Rollback

If a restart breaks a service, check logs then revert code and restart again:

```bash
journalctl -u tickles-interpretation -n 80 --no-pager
git checkout -- path/to/file   # if needed
sudo systemctl restart tickles-interpretation.service
```

See also: [`LEARNING_LOOP_2026_05_29.md`](LEARNING_LOOP_2026_05_29.md) for mem0 lean rollback.
