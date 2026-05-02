# Tickles position-pipeline systemd units (F4)

This directory holds the systemd unit files for the daemons that make the
position pipeline run end-to-end. Each unit:

  * runs as `root` from the repo root (`/opt/tickles`)
  * loads `.env` from the repo root via `EnvironmentFile=-/opt/tickles/.env`
  * sets `PYTHONPATH=/opt/tickles` and `PYTHONUNBUFFERED=1`
  * appends both stdout and stderr to `/var/log/tickles/<name>.log`
  * restarts automatically (`always` for hot loops, `on-failure` for batch loops)

## The pipeline daemons

The full position-tracking pipeline is seven processes. **Six** are managed
by the units in this directory. Candle collection is already handled by the
pre-existing `tickles-candle-daemon.service` in `/etc/systemd/system/`
(running `shared.candles.daemon`) — do not start a second collector or you
will double-write `public.candles`.

| # | Unit                                  | Module                                          | Log                                       | Restart    |
|---|---------------------------------------|-------------------------------------------------|-------------------------------------------|------------|
| 1 | tickles-candle-daemon.service *       | shared.candles.daemon                           | /var/log/tickles/candle_daemon.log        | always     |
| 2 | tickles-interpretation.service        | shared.intelligence.interpretation_service      | /var/log/tickles/interpretation.log       | always     |
| 3 | tickles-position-monitor.service      | shared.intelligence.position_monitor            | /var/log/tickles/position_monitor.log     | always     |
| 4 | tickles-postmortem.service            | shared.intelligence.postmortem_service          | /var/log/tickles/postmortem.log           | always     |
| 5 | tickles-edge-scorer.service           | shared.scripts.run_edge_scorer_service          | /var/log/tickles/edge_scorer.log          | on-failure |
| 6 | tickles-coach.service                 | shared.intelligence.coach_service               | /var/log/tickles/coach.log                | on-failure |
| 7 | tickles-chart-hacker-opinion.service  | shared.scripts.run_chart_hacker_opinion         | /var/log/tickles/chart_hacker_opinion.log | on-failure |

`*` Already installed and active — managed elsewhere, do **not** redeploy.

## Install / reload (six units in this directory)

```bash
# 1. Make sure the log directory exists
sudo install -d -m 0755 /var/log/tickles

# 2. Copy units into place
sudo cp /opt/tickles/systemd/tickles-interpretation.service \
        /opt/tickles/systemd/tickles-position-monitor.service \
        /opt/tickles/systemd/tickles-postmortem.service \
        /opt/tickles/systemd/tickles-edge-scorer.service \
        /opt/tickles/systemd/tickles-coach.service \
        /opt/tickles/systemd/tickles-chart-hacker-opinion.service \
        /etc/systemd/system/

# 3. Reload systemd
sudo systemctl daemon-reload

# 4. Enable + start the six pipeline daemons
for u in tickles-interpretation tickles-position-monitor \
         tickles-postmortem tickles-edge-scorer tickles-coach \
         tickles-chart-hacker-opinion; do
  sudo systemctl enable "$u"
  sudo systemctl restart "$u"
done

# 5. Watch any of them
tail -f /var/log/tickles/postmortem.log
```

## Verify after restart

```bash
for u in tickles-candle-daemon tickles-interpretation tickles-position-monitor \
         tickles-postmortem tickles-edge-scorer tickles-coach \
         tickles-chart-hacker-opinion; do
  printf "%-40s %s\n" "$u" "$(systemctl is-active "$u")"
done
```

Watch each new daemon's log for at least 60 seconds after restart and
confirm no traceback appears before treating it as healthy.

## Why the position pipeline is THESE seven

This is the F1–F10 plan's full causal chain:

  1. **candle-daemon** writes 1m candles → `public.candles`
  2. **interpretation** turns trader media items into rows in `tracked_positions`
  3. **position-monitor** snapshots open positions, fires F2 expiry-close +
     F10 fee-aware realized P&L when a position exits
  4. **postmortem** runs F3 LLM causal post-mortems on every closed position
  5. **edge-scorer** scores per-trade edge from postmortem outputs
  6. **coach** runs prompt A/B testing using edge scores
  7. **chart-hacker-opinion** issues live vision opinions on still-open positions

If any one of these is down, the pipeline silently degrades — which is
exactly the failure mode the F1–F10 series was written to fix.
