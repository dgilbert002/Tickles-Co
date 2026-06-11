# Surgeon + Guru Removal — 2026-05-29

## Why
The CEO directed that **all surgeon agents** and the **chart_hacker_guru** be removed
completely. A prior agent's attempt deleted the daemon *code* but left orphans
(systemd units, OpenClaw state, Qdrant collections, a live MCP tool, dormant reports).
The only agent that performs real work is **chart_hacker** (the AI vision identity
inside `tickles-interpretation.service`), which is **kept and untouched**.

This removal is **reversible** — everything except 4 points of Qdrant test data was
**archived, not hard-deleted**, to:

    /opt/tickles/.archive/surgeon_guru_2026_05_29/

## What was already gone (prior attempt)
These daemon files did NOT exist at removal time (deleted earlier):
`shared/daemons/surgeon_trader.py`, `surgeon2_trader.py`,
`shared/intelligence/surgeon_position_bridge.py`, `surgeon_position_reconciler.py`,
`shared/templates/trading_agent/surgeon_llm_runner.py`.

## What this change removed

### Surgeon (Phase 1 — orphan cleanup, no code edits)
| Item | Action | Archive location |
|------|--------|------------------|
| `rubicon-surgeon-scanner.service` | stop, disable, rm unit | `.archive/.../systemd/` |
| `rubicon-surgeon-trader.service` | stop, disable, rm unit | `.archive/.../systemd/` |
| `rubicon-surgeon2-trader.service` | stop, disable, rm unit | `.archive/.../systemd/` |
| `tickles-trader-rubicon_surgeon.service` (was **failed**) | stop, disable, reset-failed, rm unit | `.archive/.../systemd/` |
| `tickles-trader-rubicon_surgeon2.service` | stop, disable, rm unit | `.archive/.../systemd/` |
| `/root/.openclaw/workspace/surgeon/` | moved | `.archive/.../openclaw/surgeon/` |
| Qdrant `tickles_rubicon_surgeon` (2 pts) | **DELETED** (meta saved) | `.archive/.../qdrant/*.meta.json` |
| Qdrant `tickles_rubicon_surgeon2` (2 pts) | **DELETED** (meta saved) | `.archive/.../qdrant/*.meta.json` |

`systemctl daemon-reload` was run after unit removal.

### Guru (Phase 2)
| Item | Action | Archive location |
|------|--------|------------------|
| MCP tool `intelligence.guru.report` (registration + `_handle_guru_report` handler + `chart_hacker_guru` import) | removed from `shared/mcp/tools/intelligence.py` | git history |
| `shared/intelligence/chart_hacker_guru.py` (dormant since 2026-04-27) | moved | `.archive/.../guru/chart_hacker_guru.py` |
| `shared/reports/chart_hacker_guru/` (119 stale files) | moved | `.archive/.../guru/reports/` |
| `logs/chart_hacker_guru.{log,pid}` | moved | `.archive/.../guru/` |
| `test_intelligence.py` registration assertion | guru assertion removed, tool count `11 → 10` | git history |

MCP daemon restarted: catalog dropped **112 → 111 tools**; `intelligence.*` group
still has 10 tools; no `guru` tool remains.

### Intentionally LEFT (cosmetic / data-integrity — removing them has risk, zero benefit)
- `entry_price_source='surgeon'` enum value in `interpretation_service.py` — a valid
  historical value on existing `tracked_positions` rows.
- "Surgeon" mentions in `shared/dashboard/snapshot.py` comments and naming logic.
- `tickles_dev` mem0 entries from `dev_chart_hacker_guru` (dev namespace cruft, harmless).

## How to roll back
1. **Surgeon units:** `cp /opt/tickles/.archive/surgeon_guru_2026_05_29/systemd/*.service /etc/systemd/system/ && systemctl daemon-reload` then `systemctl enable --now <unit>`.
   (Note: the trader units still reference daemon files that were deleted by the *prior*
   attempt — those would need to be restored from earlier git history first.)
2. **OpenClaw surgeon dir:** `mv /opt/tickles/.archive/surgeon_guru_2026_05_29/openclaw/surgeon /root/.openclaw/workspace/`.
3. **Qdrant collections:** recreate from the saved `*.meta.json` config (they held only test points).
4. **Guru:** `mv /opt/tickles/.archive/surgeon_guru_2026_05_29/guru/chart_hacker_guru.py shared/intelligence/` and revert the `intelligence.py` + `test_intelligence.py` edits via git, then restart `tickles-mcpd`.

## Verification (post-removal)
- No `*surgeon*` systemd unit files remain; `daemon-reload` clean.
- No `surgeon` Qdrant collections.
- MCP catalog = 111 tools, no `guru`.
- `intelligence.py` compiles; registration test passes (tool count 10).
- `tickles-interpretation.service` (chart_hacker) and `tickles-mcpd.service` healthy.

## OpenClaw removal (same session)
OpenClaw's **runtime was already dead** before this cleanup: no process, no systemd
unit, nothing on port 18789, and no `:8443` tailscale-serve mapping (only Paperclip
`/`→3100, dashboard→3101, code-server→8080 remain). The MemClaw skill was gone with
the emptied workspace. chart_hacker does **not** depend on OpenClaw — it runs inside
`tickles-interpretation.service`.

Removed:
| Item | Action | Archive |
|------|--------|---------|
| `/usr/bin/openclaw` symlink + `/usr/lib/node_modules/openclaw` (npm pkg "openclaw", not dpkg/npm-`ls`-tracked) | removed | `package.json` + version saved to `.archive/.../openclaw_bin/` |
| `/root/.openclaw/` (empty workspace) | moved | `.archive/.../openclaw_home/` |
| `spawn_chart_hacker.sh`, `spawn_trading_agent.sh` (obsolete OpenClaw deploy scripts) | copied to archive (originals left in repo, now inert) | `.archive/.../openclaw_bin/` |

**Rollback:** `npm i -g openclaw@<version-in-archive>`; restore `/root/.openclaw` from archive.

**Left in place (inert, no runtime, no conflict — flagged for a future doc pass):**
- `openclaw_gateway` adapter strings in `shared/provisioning/{templates,executor}.py`.
- OpenClaw pricing rows in `shared/cost_shipper/pricing.py`.
- `shared/mcp/bin/tickles_mcp_stdio.py` — generic stdio MCP transport; kept (works for
  any stdio client e.g. Hermes/Claude Desktop, not OpenClaw-specific).
- `CLAUDE.md` still documents OpenClaw/MemClaw — needs a separate doc-update pass.

## chart_analyze MCP tool fix (same session)
`intelligence.chart_analyze` crashed (`KeyError: 'context'`) because the loaded
`chart_hacker_mcp` template is `'Chart context: {context}'` but the handler only
passed `{symbol}`. Fixed in `shared/mcp/tools/intelligence.py` by formatting via a
`_BlankDefaultDict` (`str.format_map`) that renders any missing placeholder as `""`,
so future prompt-file edits can never crash the tool. All 37 tests in
`test_intelligence.py` pass.

## Note for the next session
`chart_hacker` is the sole working learning agent. All `signal_interpretations`
now write to **`tickles_shared`** (with a `company_id` column), NOT per-company DBs —
contradicts the documented per-company-DB design; revisit when wiring copy-agent learning.
Pending follow-up topics (CEO): promote postmortem lessons → MemU, wire recall
measurement (`mem0_recall_log`), give copy agents the recall+postmortem loop.
