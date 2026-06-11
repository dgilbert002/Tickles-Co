# PHASE 8 — MCP TOOLS (make the reusable bits agent-callable)

> Prereq: Phase 7 DONE. Read rules in `00_MASTER_INDEX.md`.
> Goal: expose the reusable collector/control capabilities as MCP tools so agents and
> other skills can browse the source tree, flip media policy, force a channel backfill,
> and read collector health — without re-implementing any of it.

## CONTEXT (verified live — extend, do not rewrite)
`shared/mcp/tools/collector.py` ALREADY exists and ALREADY registers:
- `collector.channels` — list channels + HWM + health (`_handle_channels`)
- `collector.hwm.reset` — reset a channel HWM (`_handle_hwm_reset`)
- plus `collector.health`, `collector.messages` may exist — grep to confirm.

Registration pattern (verified) — a `_build_tools(ctx)` returns a list of
`(McpTool(...), handler)` tuples, and a loop does `registry.register(tool, handler)`
(line ~428). You ADD new tuples to that list and add the handler functions. Every MCP
call is auto-audited to `public.mcp_invocations` — no extra work.

Back up:
```bash
cp /opt/tickles/shared/mcp/tools/collector.py /opt/tickles/shared/mcp/tools/collector.py.bak-bible
```

## STEP 1 — Add three handlers (place near the other `_handle_*` functions)
```python
async def _handle_source_tree(ctx: "ToolContext", args: dict) -> dict:
    """BIBLE-P8: return the server→group→channel tree with follow/media/health."""
    src = (args.get("platform") or "discord").lower()
    if src not in ("discord", "telegram"):
        src = "discord"
    pool = await ctx.get_pool()  # USE the same accessor other handlers in this file use
    rows = await pool.fetch(
        "SELECT id, parent_id, entity_type, name, enabled, media_policy, "
        "       last_collected_at, last_error, error_count, "
        "       (platform_config->>'last_hwm') AS last_hwm "
        "FROM collector_sources WHERE source_type=$1 "
        "ORDER BY priority NULLS LAST, name", src,
    )
    return {"source": src, "count": len(rows), "tree": [dict(r) for r in rows]}

async def _handle_set_media_policy(ctx: "ToolContext", args: dict) -> dict:
    """BIBLE-P8: set media_policy for one source (none|text|text_images|everything)."""
    sid = args.get("sourceId")
    policy = (args.get("policy") or "").lower()
    if not isinstance(sid, int):
        return {"error": "sourceId (int) required"}
    if policy not in ("none", "text", "text_images", "everything"):
        return {"error": "policy must be none|text|text_images|everything"}
    pool = await ctx.get_pool()
    await pool.execute(
        "UPDATE collector_sources SET media_policy=$2, updated_at=now() WHERE id=$1",
        sid, policy,
    )
    return {"updated": True, "sourceId": sid, "policy": policy}

async def _handle_backfill_channel(ctx: "ToolContext", args: dict) -> dict:
    """BIBLE-P8: force a channel to re-scan by clearing its HWM (next poll drains it).

    This reuses the SAME mechanism as collector.hwm.reset but scoped to ONE channel and
    returns the prior HWM so the action is auditable/reversible."""
    sid = args.get("sourceId")
    if not isinstance(sid, int):
        return {"error": "sourceId (int) required"}
    pool = await ctx.get_pool()
    row = await pool.fetchrow(
        "SELECT platform_id, platform_config->>'last_hwm' AS prior_hwm "
        "FROM collector_sources WHERE id=$1", sid,
    )
    if not row:
        return {"error": f"source {sid} not found"}
    # Clear the live HWM in system_config so the collector re-scans from latest.
    await pool.execute(
        "DELETE FROM system_config WHERE namespace='discord_hwm' AND config_key=$1",
        str(row["platform_id"]),
    )
    return {"backfillQueued": True, "sourceId": sid,
            "channelId": row["platform_id"], "priorHwm": row["prior_hwm"]}
```
> If `ToolContext` exposes the pool differently (e.g. `ctx.db`, `ctx.shared_pool`),
> match what `_handle_channels`/`_handle_hwm_reset` already use. Do NOT invent.

## STEP 2 — Register the three tools (add tuples to `_build_tools`'s returned list)
Inside `_build_tools(ctx)`'s `return [ ... ]`, add (match indentation/style):
```python
        (
            McpTool(
                name="collector.source_tree",
                description=("Return the collector source hierarchy "
                             "(server→group→channel) with follow state, media policy, "
                             "last-seen freshness, and health for discord or telegram."),
                input_schema={"type": "object", "properties": {
                    "platform": {"type": "string", "description": "discord|telegram"}}},
                tags={"group": "collector", "status": "live"},
            ),
            _handle_source_tree,
        ),
        (
            McpTool(
                name="collector.set_media_policy",
                description=("Set what media a source ingests: "
                             "none | text | text_images | everything."),
                input_schema={"type": "object", "required": ["sourceId", "policy"],
                    "properties": {
                        "sourceId": {"type": "integer"},
                        "policy": {"type": "string",
                                   "enum": ["none","text","text_images","everything"]}}},
                tags={"group": "collector", "status": "live"},
            ),
            _handle_set_media_policy,
        ),
        (
            McpTool(
                name="collector.backfill_channel",
                description=("Force a single channel to re-scan its backlog by clearing "
                             "its high-water mark; next poll drains it oldest-first. "
                             "Returns the prior HWM for auditability."),
                input_schema={"type": "object", "required": ["sourceId"],
                    "properties": {"sourceId": {"type": "integer"}}},
                tags={"group": "collector", "status": "live"},
            ),
            _handle_backfill_channel,
        ),
```

## STEP 3 — Restart MCP daemon
```bash
systemctl restart tickles-mcpd
sleep 5
```

---

## VERIFY (all GREEN before Phase 9)
The MCP daemon is JSON-RPC 2.0 over HTTP at `127.0.0.1:7777`, POST `/mcp`. List + call:
```bash
# tools are registered:
curl -s -X POST http://127.0.0.1:7777/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' \
  | python3 -c "import sys,json; t=[x['name'] for x in json.load(sys.stdin)['result']['tools']]; \
print('source_tree:', 'collector.source_tree' in t); \
print('set_media_policy:', 'collector.set_media_policy' in t); \
print('backfill_channel:', 'collector.backfill_channel' in t)"
echo "   ^ all MUST be True"

# call source_tree:
curl -s -X POST http://127.0.0.1:7777/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"collector.source_tree","arguments":{"platform":"discord"}}}' \
  | head -c 400; echo
echo "   ^ MUST return a tree with count>0"
```
Confirm the call was audited:
```bash
PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared -tAc \
"SELECT tool_name FROM mcp_invocations WHERE tool_name LIKE 'collector.%' ORDER BY id DESC LIMIT 5;"
```

## DOWNSTREAM SAFETY
- Existing collector tools still work: call `collector.channels` — returns as before.
- `tools/list` count did not DROP (you added, didn't replace): compare to ~111 baseline.
- `systemctl is-active tickles-mcpd` = active; no Traceback on startup.

## ON SUCCESS
Append to PROGRESS.md:
`Phase 8 — DONE <iso> — MCP tools added: collector.source_tree, collector.set_media_policy, collector.backfill_channel; registered + audited; existing tools intact.`
Then open `09_PHASE_CODE_REVIEW.md`.
