# Tickles Provisioning Executor (Phase 3)

Nine-step atomic company provisioner used by the Paperclip "Provision
company workspace" checkbox. Lives inside the Tickles MCP daemon so the
same code path runs for both UI-driven and CLI/MCP-driven creates.

## Files

| File            | Purpose |
|-----------------|---------|
| `templates.py`  | Load + validate template JSON under `shared/templates/companies/` |
| `executor.py`   | Nine-step orchestrator + per-step implementations + rollback |
| `jobs.py`       | Emit progress events to Paperclip (best-effort, fire-and-forget) |

## The nine steps

| # | Step                    | What it does                                           | Layer | Reversible |
|---|-------------------------|--------------------------------------------------------|-------|------------|
| 1 | `paperclip_row`         | Verify the Paperclip `companies` row already exists   | 1     | — (we didn't create it) |
| 2 | `postgres_db`           | `CREATE DATABASE tickles_<slug>` + apply schema       | 1     | `DROP DATABASE` |
| 3 | `qdrant_collection`     | `PUT /collections/tickles_<slug>` (384-dim, Cosine)   | 1     | `DELETE /collections/…` |
| 4 | `mem0_scopes`           | Stash tier-1/2/3 scope identifiers in company.metadata | 1     | metadata left (harmless) |
| 5 | `memu_subscriptions`    | Stash MemU broadcast topics in company.metadata        | 1     | metadata left |
| 6 | `treasury_registration` | Register venues + rule-1 mode (Layer 2 only)          | 2     | metadata left |
| 7 | `install_skills`        | **Phase-4 stub** — records intent, skill install later | —     | metadata left |
| 8 | `hire_agents`           | POST /companies/:id/agents per template + clone OpenClaw dir from `cody` | —     | DELETE agent + rm dir |
| 9 | `register_routines`     | **Phase-6 stub** — records intent                     | —     | metadata left |

## Required vs best-effort

- **Required** (abort + rollback on failure): steps 2, 3, 4.
- **Best-effort** (log + continue): steps 1, 5, 6, 7, 8, 9.

A trading tenant that lands in "partial" state still has its DB, Qdrant
collection and mem0 scopes — you can re-run provisioning or add agents
manually without cleaning up first. Nothing breaks.

## Dependencies (runtime)

- `sudo -u postgres psql` (peer auth) — for `CREATE DATABASE` and schema
- System Postgres on `127.0.0.1:5432`
- Qdrant on `127.0.0.1:6333`
- Paperclip on `127.0.0.1:3100`
- Zero new Python packages (uses stdlib `urllib` + `subprocess` only)

## Env vars

| Name | Default | Purpose |
|------|---------|---------|
| `PAPERCLIP_URL` | `http://127.0.0.1:3100` | Paperclip API base |
| `PAPERCLIP_API_TOKEN` | unset | Bearer for non-loopback Paperclip calls |
| `QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant API base |
| `QDRANT_VECTOR_SIZE` | `384` | Matches mem0's `all-MiniLM-L6-v2` embedder |
| `TICKLES_COMPANY_SCHEMA_SQL` | `/opt/tickles/shared/migration/tickles_company_pg.sql` | Per-company schema template |
| `OPENCLAW_AGENTS_DIR` | `/root/.openclaw/agents` | OpenClaw agent root (for clone) |

## Usage

From the MCP tool:

```python
from shared.provisioning import executor

result = await executor.run(
    company_id="1def5087-…-069685fff525",
    slug="surgeonco",
    template_id="surgeon_co",
)
print(result.overall_status, len(result.steps))
```

Returns `ProvisionResult(overall_status="ok"|"partial"|"failed", steps=[…])`.

## Rollback

On failure of a required step, the executor calls `_rollback_sync` which
reverses in LIFO order using `UNDO_MAP`. Metadata-only steps are left in
place (harmless; a full `company.delete` cleans them).

Manual rollback (e.g. to reset a broken tenant):

```bash
curl -sX POST http://127.0.0.1:7777/mcp \
    -H content-type:application/json \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
          "params":{"name":"company.delete","arguments":{"companyId":"<uuid>"}}}'
```

## Idempotency

Each step checks "does this already exist?" before acting:

- Step 2 skips `CREATE DATABASE` when the db exists
- Step 3 skips `PUT /collections` when Qdrant returns 200
- Steps 4-9 are metadata writes — replaying them is a no-op

Re-running `company.create` on an existing company therefore does not
break anything; it just returns `ok` with `detail` noting what was
already present.
