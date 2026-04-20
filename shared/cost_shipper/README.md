# Tickles Cost Shipper

Ships per-agent OpenRouter spend from OpenClaw session logs into Paperclip
`cost_events` so the existing Paperclip Costs page, by-agent/by-provider
aggregations and budget incident pipeline all "just work" for every company.

## How it works

```
~/.openclaw/agents/<urlKey>/sessions/*.jsonl   →   shipper.py   →   POST /api/companies/:id/cost-events
                                                   (dedup sqlite)
```

Every assistant message OpenClaw writes contains a full `usage` + `cost` block
with the OpenRouter `responseId`. The shipper tails those files, dedups by
`responseId`, resolves `urlKey → (companyId, agentId)` via Paperclip's HTTP API,
and posts a cost event. Paperclip is in `local_trusted` mode so localhost
requests land as the board actor — no bearer token needed.

## Files

| File | Purpose |
|------|---------|
| `shipper.py` | Long-running daemon (`run_forever`) + one-shot backfill. |
| `pricing.py` | Defensive pricing table for when OpenClaw omits `cost`. |
| `reconciler.py` | Daily drift check against `/api/v1/auth/key`. |
| `systemd/tickles-cost-shipper.service` | systemd unit for the daemon. |
| `systemd/tickles-cost-reconciler.service` + `.timer` | daily reconciler. |

## Install on VPS

```bash
# one-time
sudo mkdir -p /opt/tickles/shared/cost_shipper /var/lib/tickles/cost-shipper
sudo rsync -a /opt/tickles/shared/cost_shipper/ /opt/tickles/shared/cost_shipper/

# systemd
sudo cp systemd/tickles-cost-shipper.service /etc/systemd/system/
sudo cp systemd/tickles-cost-reconciler.* /etc/systemd/system/
sudo systemctl daemon-reload

# backfill last 7 days of existing session history
sudo PAPERCLIP_URL=http://127.0.0.1:3100 python3 /opt/tickles/shared/cost_shipper/shipper.py --backfill-days 7

# enable continuous shipping
sudo systemctl enable --now tickles-cost-shipper.service
sudo systemctl enable --now tickles-cost-reconciler.timer
```

## Verify

```bash
curl -s http://127.0.0.1:3100/api/companies/<companyId>/costs/by-agent | jq
```

Expect one row per recently-active agent with non-zero `costCents`.

## Roll back

```bash
sudo systemctl disable --now tickles-cost-shipper.service
sudo systemctl disable --now tickles-cost-reconciler.timer
sudo rm /etc/systemd/system/tickles-cost-shipper.service \
        /etc/systemd/system/tickles-cost-reconciler.*
sudo systemctl daemon-reload
```

Paperclip cost_events rows stay in place (they are append-only) but no new rows
will be produced until the shipper is re-enabled.
