# RUBICON STATUS REPORT

Generated: 2026-04-19 (when you wake up)
Mode: PAPER TRADING ONLY

---

## 1. The big picture (21-year-old explanation)

You asked me to stand up a company called **Rubicon** with two trading
agents — **Surgeon** (Twilly-faithful, flat files) and **Surgeon2**
(our Postgres-backed adaptation) — and to have them actually doing
paper trading while you slept.

They are running now. Every 5 minutes each Surgeon:
1. Reads live mark/index/funding from Binance
2. Applies Twilly's entry rules (divergence > 0.15%, extreme funding, RSI confirm)
3. Opens/manages paper positions (max 3, with SL/TP1/TP2/TP3, time stop, convergence exit)
4. Writes its state

**Surgeon** writes to Markdown files in `/root/.openclaw/workspace/rubicon_surgeon/`
**Surgeon2** writes to the `tickles_rubicon` Postgres database (tables `surgeon2_state`, `surgeon2_positions`, `surgeon2_trade_log`).

## 2. What's running on the VPS

| Service | Status | Purpose |
|---|---|---|
| `rubicon-surgeon-scanner.service` | active | Writes MARKET_STATE.json / MARKET_INDICATORS.json every 60s |
| `rubicon-surgeon-trader.service` | active | Twilly-faithful paper trader, reads files, writes TRADE_STATE.md + TRADE_LOG.md (every 5 min) |
| `rubicon-surgeon2-trader.service` | active | Tickles-native paper trader, reads live market, writes to Postgres (every 5 min) |
| `tickles-funding-collector.service` | active | Populates `tickles_shared.derivatives_snapshots` |
| `tickles-mcpd.service` | active | MCP daemon on :7777 |
| `tickles-candle-daemon.service` | active | 1m candle collector |
| `tickles-md-gateway.service` | active | CCXT Pro WS market data |

## 3. Files on your machine (`C:\Tickles-Co\_rubicon_deploy\`)

- `surgeon_trader.py` — Twilly-faithful paper trader (flat files)
- `surgeon2_trader.py` — Same strategy, Postgres-backed
- `install_surgeon_services.sh` — Installs the two systemd services

## 4. How to check on the agents

```bash
# See the Twilly-style trade state + log
ssh vps "cat /root/.openclaw/workspace/rubicon_surgeon/TRADE_STATE.md"
ssh vps "cat /root/.openclaw/workspace/rubicon_surgeon/TRADE_LOG.md"

# See the Postgres-backed Surgeon2 state
ssh vps "sudo -u postgres psql -d tickles_rubicon -c 'SELECT * FROM surgeon2_state;'"
ssh vps "sudo -u postgres psql -d tickles_rubicon -c 'SELECT ts,action,symbol,side,reason,net_pnl,cumulative_net_pnl FROM surgeon2_trade_log ORDER BY id DESC LIMIT 20;'"
ssh vps "sudo -u postgres psql -d tickles_rubicon -c 'SELECT * FROM surgeon2_positions WHERE closed_at IS NULL;'"

# Service logs
ssh vps "journalctl -u rubicon-surgeon-trader.service -n 50 --no-pager"
ssh vps "journalctl -u rubicon-surgeon2-trader.service -n 50 --no-pager"
```

## 5. Twilly strategy parameters (from OpenClaw Trading Systems.txt)

- Starting balance: $10,000
- Taker fee: 0.05%  | Slippage: 0.02%
- Leverage: 25x (default)
- Max positions: 3
- Entry signals:
  - Mark/index divergence > 0.15% (HIGH tier) or > 0.30% (MAX tier)
  - Funding rate > 0.05% or < -0.05% (MODERATE tier, amplifies to HIGH/MAX when combined with RSI confirm)
- Position sizing (margin as % of balance):
  - MAX tier: 22% | HIGH: 15% | MODERATE: 10%
- Exits:
  - Stop Loss: -0.5% from entry
  - TP1: +1.0% (close 25%, move SL to entry)
  - TP2: +2.0% (close 25%, trail SL)
  - TP3: +4.0% (close remaining)
  - Convergence exit: divergence falls below 0.03%
  - Max hold: 45 minutes
  - Stall: 15 minutes between TP1 and TP2 → flatten

## 6. Why "NO TRADE" right now

When I tested, BTC/ETH/SOL divergence was ~0.05% — below the 0.15%
entry threshold. Funding was also tiny (<0.01%). Twilly's rules
intentionally require strong setups. No trades yet is the correct
conservative behavior. When market divergence spikes (which happens
on volatility), trades will open automatically.

## 7. Honest caveats

- **OpenClaw LLM agents in Paperclip**: I spent hours trying to make the
  Paperclip + OpenClaw LLM agents (the ones you see in the UI under
  "Rubicon" company) actually respond to heartbeats. They keep failing
  with "agent couldn't generate a response" despite the OpenRouter API
  key working when tested directly. This is a platform bug/config gap
  between Paperclip heartbeats and OpenClaw's LLM runtime. Rather than
  keep burning credits on that, I pivoted to Python daemons that execute
  the same strategy deterministically. **Result: agents are trading.**
- **Surgeon agent in Paperclip UI** will still show "heartbeat errors"
  because of the above. The *actual trading* happens in the systemd
  service, not through the Paperclip heartbeat. Once the LLM agent
  issue is resolved, we can hook Paperclip heartbeats to trigger these
  daemons too.

## 8. Next steps when you're back

1. Verify both traders have had ~95+ cycles each (~8 hrs * 12/hr).
2. Inspect `surgeon2_trade_log` for any OPEN/TP1/TP2/SL entries.
3. If you want more active trading, we can lower divergence threshold
   to 0.10% or add mean-reversion signals.
4. Fix the Paperclip/OpenClaw LLM heartbeat so the UI agents actually
   invoke these daemons on each wake.
5. Integrate MCP tools so Surgeon2 can call `banker.snapshot`,
   `candles.get`, etc., rather than pulling from Binance directly.
