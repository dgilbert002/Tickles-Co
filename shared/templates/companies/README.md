# Company Templates

Each JSON file in this directory defines a **template** that the Tickles MCP
`company.create` tool uses when the Paperclip "Provision company workspace"
checkbox is ticked.

**We deliberately keep this down to two templates** so the wizard stays
obvious — one for "I just want a workspace with a DB + memory + vector" and
one for "I want that plus the trading stack". Anything more specialised
(media pipeline, research-only, mentor/observer, etc.) is layered on *after*
creation by installing skills from the Skills tab. That way the skill
catalog stays the source of truth for capabilities and the template picker
never drifts out of sync with it.

| Template | File | Layer 1 (DB + mem0 + Qdrant + MemU) | Layer 2 (Treasury + venues + Rule-1) | Ships with |
|----------|------|-------------------------------------|--------------------------------------|------------|
| `blank`   | `blank.json`   | yes | no  | 0 agents, 0 skills, 0 routines |
| `trading` | `trading.json` | yes | yes | 1 CEO agent (Claude Sonnet 4), ccxt-pro + indicator-library + backtest-submit, autopsy/post-mortem/feedback routines, Bybit-demo venue |

Templates decide **which provisioning steps run and how** — they never
bypass the platform. Steps are always executed in the same order and with
the same underlying code; templates just toggle which agents/skills/
routines get pre-populated and which MemU subscriptions to enable.

## Template schema

```jsonc
{
  "id": "surgeon_co",                 // kebab/snake case, matches filename
  "name": "SurgeonCo",                // human-friendly
  "description": "…",                 // shown in the wizard dropdown
  "category": "trading",              // "general" (media/research/admin) or "trading"
  "layer2_trading": true,             // adds Treasury/venue/Rule-1 on top of Layer 1

  "rule_one_mode": "advisory",        // "advisory" | "strict" | "off"
  "memu_subscriptions": [             // MemU broadcast topics to subscribe to
    "trade_insights",
    "risk_events"
  ],

  "venues": ["bybit_demo"],           // venue slugs (only if layer2_trading)

  "skills": [                         // ClawHub skill slugs (Phase-4 will install)
    "ccxt-pro",
    "indicator-library"
  ],

  "agents": [                         // pre-hired agents (Phase-3 installs)
    {
      "urlKey": "ceo",
      "name": "CEO",
      "role": "ceo",                  // see "Agent roles" below
      "model": "openrouter/anthropic/claude-sonnet-4",
      "soul": "apex",                 // links to shared/souls/personas/apex
      "skills": ["ccxt-pro"],         // subset of template.skills
      "budgetMonthlyCents": 20000,    // USD cents per month
      "clone_openclaw_from": "cody"   // template OpenClaw agent dir to copy
    }
  ],

  "routines": [                       // scheduled MCP tool invocations
    { "kind": "autopsy",      "trigger": "on_trade_close" },
    { "kind": "postmortem",   "trigger": "daily_23_utc" },
    { "kind": "feedback_loop","trigger": "weekly_sunday_20_utc" }
  ]
}
```

## The 9 provisioning steps

| # | Layer | Step                         | Skipped when |
|---|-------|------------------------------|--------------|
| 1 | 1 | Create Paperclip company row    | Never (already done before MCP call) |
| 2 | 1 | Create Postgres DB (`tickles_<slug>`) with full schema | Never |
| 3 | 1 | Create Qdrant collection        | Never |
| 4 | 1 | Register mem0 scopes (agent+company)  | Never |
| 5 | 1 | Register MemU subscriptions     | `memu_subscriptions == []` |
| 6 | 2 | Treasury registration + venue allow-list | `layer2_trading == false` |
| 7 | — | Install skills via ClawHub      | `skills == []` |
| 8 | — | Hire agents (Paperclip + OpenClaw dir) | `agents == []` |
| 9 | — | Register routines               | `routines == []` |

Layer 1 steps **always run** when the checkbox is ticked. Layer 2 + skills/
agents/routines depend on template contents.

## Agent roles

Paperclip's `AGENT_ROLES` enum is **fixed** to:
`ceo | cto | cmo | cfo | engineer | designer | pm | qa | devops | researcher | general`.

Any other value in `template.agents[].role` — e.g. `analyst`, `observer`,
`member`, `quant`, `ledger` — is **translated at hire time** by
`shared/provisioning/executor.py::_map_role_for_paperclip` to a canonical
one, and the original is preserved under the agent's
`metadata.templateRole` so analytics and the UI can still see it. Mappings:

| Template value | Sent to Paperclip |
|----------------|-------------------|
| `analyst`      | `researcher`      |
| `quant`        | `researcher`      |
| `observer`     | `general`         |
| `ledger`       | `general`         |
| `member`       | `general`         |
| (any canonical)| passthrough       |

## Adapter type

All template-hired agents use `adapterType: "openclaw_gateway"` (underscore —
the canonical Paperclip enum value). The provisioning executor also injects
`adapterConfig.url` and `adapterConfig.headers["x-openclaw-token"]` from env
vars `OPENCLAW_GATEWAY_URL` / `OPENCLAW_GATEWAY_TOKEN` when they are set,
which ensures template-hired agents can talk to the OpenClaw gateway on
their very first run without any manual API-key dance. Paperclip itself
will also auto-fill these via `/etc/paperclip/openclaw-gateway.env` (see
Phase A of the provisioning roadmap) — the executor setting them is
belt-and-braces for portability.

## Adding a new template

1. Copy `blank.json` to `your_template.json`.
2. Edit fields, keeping `id` equal to the filename stem.
3. Drop the file here — the MCP tool discovers templates at runtime
   (no restart needed).
4. Appear in the wizard dropdown automatically after page refresh.

## Rollback

Rolling back a provisioned company is handled by `company.delete` (MCP
tool), which reverses steps 9 → 1 in order. Partial failures during
`company.create` roll back automatically (the executor records each
completed step and only reverses those).
