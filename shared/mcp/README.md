# Tickles MCP Server (Phase 2)

JSON-RPC 2.0 MCP control-plane on `http://127.0.0.1:7777/mcp` exposing 33
tools grouped into 5 domains: provisioning, data, memory, trading, learning.

## How agents use this

Any client that speaks MCP (Cursor, OpenClaw skill, Paperclip adapter,
Claude Desktop, direct HTTP) can:

```
POST /mcp  body={"jsonrpc":"2.0","id":1,"method":"tools/list"}
POST /mcp  body={"jsonrpc":"2.0","id":1,"method":"tools/call",
                 "params":{"name":"banker.snapshot",
                           "arguments":{"companyId":"<uuid>"}}}
```

Healthcheck: `GET /healthz` → `{"ok": true}`.

## Tool catalog (33 tools)

| Group         | Tools |
|---------------|-------|
| diagnostic    | `ping` |
| provisioning  | `company.list/get/create/delete/pause/resume`, `agent.list/get/create/delete/pause/resume` |
| data          | `catalog.list/get`, `md.quote`, `md.candles`, `altdata.search` |
| memory        | `memory.add`, `memory.search`, `memu.broadcast`, `memu.search`, `learnings.read_last_3` |
| trading       | `banker.snapshot/positions`, `treasury.evaluate`, `execution.submit/cancel/status` |
| learning      | `autopsy.run` (Twilly-01), `postmortem.run` (Twilly-02), `feedback.loop` (Twilly-03), `feedback.prompts` |

Tools marked `status=stub` in their `tags` return a typed
`{status: "not_implemented", feature: ..., message: ...}` envelope. The
LLM can still plan against them; the backend wiring lands in Phase 2.5.

## Deploy on the VPS

```bash
sudo rsync -a /opt/tickles/shared/mcp/ /opt/tickles/shared/mcp/  # already synced
sudo cp /opt/tickles/shared/mcp/systemd/tickles-mcpd.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tickles-mcpd.service
curl -s http://127.0.0.1:7777/healthz   # {"ok":true}
bash /opt/tickles/shared/mcp/tools/_smoketest.sh
```

## Adding a production bearer token

For non-loopback bindings (e.g. exposed over Tailscale for Paperclip
agents on other hosts) put the token in `/etc/tickles/mcp.env`:

```
TICKLES_MCP_HOST=0.0.0.0
TICKLES_MCP_TOKEN=$(openssl rand -hex 24)
```

Then restart: `sudo systemctl restart tickles-mcpd.service`.

## Roll back

```bash
sudo systemctl disable --now tickles-mcpd.service
sudo rm /etc/systemd/system/tickles-mcpd.service
sudo systemctl daemon-reload
```
