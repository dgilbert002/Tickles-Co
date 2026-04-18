# agents/

Standalone long-running Python agents. Each agent runs as a systemd unit,
owns its own MemU insights, and communicates with other agents **only**
through MemU (never by polling file systems or direct RPC).

## What belongs here

- `janitor.py` *(Phase 1B)* — filesystem housekeeper with the four-tier
  safety model (see `ROADMAP_V3.md §Phase 1B` and `CORE_FILES.md`).
- `validator.py` *(Phase 9)* — continuous Rule 1 watcher; alerts on drift.
- `optimizer.py` *(Phase 9)* — weekly walk-forward sweep; proposes DNA
  strands for live strategies.
- `curiosity.py` *(Phase 9)* — autonomous 2-of-N indicator combo explorer;
  writes to MemU `strategies/candidates/`.
- `regime_watcher.py` *(Phase 9)* — market-regime classifier; writes to MemU
  `regimes/`.

## Rules

1. On startup, read `MEMORY.md` first.
2. Use MemU for **all** cross-agent communication. No polling file systems.
3. Operate inside a `container` tag (company id or `shared`).
4. `approval_mode` defaults to `human_all` — agents propose, CEO approves,
   Dean has final call.
5. Never write strategy outcomes directly to MemU `approved/`. That path is
   reserved for the Synthesizer (`memu/synthesizer.py`) which enforces the
   promotion gate (deflated_sharpe > 1.5, oos_sharpe > 1.0, num_trades > 30,
   `verified_by` non-null).

## How agents find each other

Search MemU by predicate, not filesystem. Example:

```python
from shared.memu.client import search
results = search(container="shared", category="strategies/candidates",
                 filters={"metrics.oos_sharpe": {"gt": 1.2}})
```
