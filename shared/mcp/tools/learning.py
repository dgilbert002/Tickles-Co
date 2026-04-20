"""MCP tools: Learning loops (Twilly Templates 01 / 02 / 03).

These tools expose the canonical learning-loop prompts so every agent — in
every company — can run autopsies, postmortems and feedback reviews with the
exact same rubric. Phase 2 ships the prompt scaffolding plus a hook that
emits the resulting memory-write payload (so the caller can forward it to
mem0 via ``memory.add``) without requiring a trade-ledger integration.

Tools registered:
    autopsy.run         (Twilly Template 01 — per-trade reflection)
    postmortem.run      (Twilly Template 02 — per-session review)
    feedback.loop       (Twilly Template 03 — weekly/cycle summary)
    feedback.prompts    (read the raw template strings)
"""

from __future__ import annotations

from typing import Any, Dict

from ..protocol import McpTool
from ..registry import ToolRegistry
from .context import ToolContext


# ---- Twilly Template Prompts (verbatim from shared/docs) ---------------

_AUTOPSY_PROMPT = """\
# Twilly Template 01 — Trade Autopsy

You have just closed trade {trade_id} ({symbol} {side}). Before moving on,
produce a short autopsy in this exact schema:

1. What I expected to happen (pre-trade thesis, in one sentence).
2. What actually happened (price path + PnL).
3. Which signals confirmed or contradicted the thesis in real-time.
4. What I would do differently with hindsight (1-3 bullets).
5. One learning to commit to mem0 (<=80 words, actionable, not emotional).

Keep each section to 2 sentences or fewer. Write in first person.
"""

_POSTMORTEM_PROMPT = """\
# Twilly Template 02 — Session Postmortem

Session {session_id} wrapped with {n_trades} trades. Write a postmortem:

1. Scorecard: wins vs losses, realised PnL, biggest winner, biggest loser.
2. Two regimes the market traversed during the session.
3. Which playbook fired best? Which misfired?
4. One systemic issue (data, latency, emotional, sizing).
5. One commitment for next session (<=60 words).

This is read by the Strategy Council Moderator at the next board meeting.
"""

_FEEDBACK_LOOP_PROMPT = """\
# Twilly Template 03 — Cycle Feedback Loop

Over the last {period} you ran {n_sessions} sessions and closed {n_trades}
trades. Before your next decision, MUST:

1. Read `learnings.read_last_3` output — call out any pattern.
2. Identify 1 playbook drifting from intent (if any).
3. Suggest 1 guardrail adjustment (or explicit "keep current").
4. Rank curiosities for next cycle (top 3, one line each).

If no drift is detected, write "STABLE — keep current" and stop.
"""


def _build_tools(ctx: ToolContext) -> list[tuple[McpTool, Any]]:
    t_autopsy = McpTool(
        name="autopsy.run",
        description=(
            "Render the Twilly Template 01 prompt for a single closed trade. "
            "Caller is expected to forward the rendered prompt to their LLM "
            "and then ship the resulting learning via memory.add (scope=agent)."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "tradeId": {"type": "string"},
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
            },
            "required": ["tradeId", "symbol", "side", "companyId", "agentId"],
        },
        read_only=True,
        tags={"phase": "2", "group": "learning", "template": "twilly-01"},
    )

    async def _autopsy(p: Dict[str, Any]) -> Dict[str, Any]:
        prompt = _AUTOPSY_PROMPT.format(
            trade_id=p["tradeId"],
            symbol=p["symbol"],
            side=p["side"],
        )
        return {
            "template": "twilly-01",
            "prompt": prompt,
            "memory_write_hint": {
                "tool": "memory.add",
                "scope": "agent",
                "companyId": p["companyId"],
                "agentId": p["agentId"],
                "metadataTemplate": {
                    "memory_types": ["autopsy", "learning"],
                    "tradeId": p["tradeId"],
                    "symbol": p["symbol"],
                },
            },
        }

    t_postmortem = McpTool(
        name="postmortem.run",
        description=(
            "Render the Twilly Template 02 prompt for a completed session. "
            "Postmortem is stored at scope=company so the Strategy Council "
            "Moderator can read it."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "sessionId": {"type": "string"},
                "nTrades": {"type": "integer", "minimum": 0},
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
            },
            "required": ["sessionId", "nTrades", "companyId", "agentId"],
        },
        read_only=True,
        tags={"phase": "2", "group": "learning", "template": "twilly-02"},
    )

    async def _postmortem(p: Dict[str, Any]) -> Dict[str, Any]:
        prompt = _POSTMORTEM_PROMPT.format(
            session_id=p["sessionId"],
            n_trades=p["nTrades"],
        )
        return {
            "template": "twilly-02",
            "prompt": prompt,
            "memory_write_hint": {
                "tool": "memory.add",
                "scope": "company",
                "companyId": p["companyId"],
                "agentId": p["agentId"],
                "metadataTemplate": {
                    "memory_types": ["postmortem"],
                    "sessionId": p["sessionId"],
                },
            },
        }

    t_feedback_loop = McpTool(
        name="feedback.loop",
        description=(
            "Render the Twilly Template 03 prompt for the agent's last cycle. "
            "Agents MUST run this before starting a new routine to satisfy the "
            "universal learning-loop contract."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "companyId": {"type": "string"},
                "agentId": {"type": "string"},
                "period": {
                    "type": "string",
                    "enum": ["1d", "1w", "2w", "1mo"],
                    "default": "1w",
                },
                "nSessions": {"type": "integer", "minimum": 0, "default": 0},
                "nTrades": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["companyId", "agentId"],
        },
        read_only=True,
        tags={"phase": "2", "group": "learning", "template": "twilly-03"},
    )

    async def _feedback_loop(p: Dict[str, Any]) -> Dict[str, Any]:
        prompt = _FEEDBACK_LOOP_PROMPT.format(
            period=p.get("period", "1w"),
            n_sessions=p.get("nSessions", 0),
            n_trades=p.get("nTrades", 0),
        )
        return {
            "template": "twilly-03",
            "prompt": prompt,
            "precondition": {
                "tool": "learnings.read_last_3",
                "arguments": {
                    "companyId": p["companyId"],
                    "agentId": p["agentId"],
                },
            },
            "memory_write_hint": {
                "tool": "memory.add",
                "scope": "agent",
                "companyId": p["companyId"],
                "agentId": p["agentId"],
                "metadataTemplate": {
                    "memory_types": ["feedback_loop"],
                    "period": p.get("period", "1w"),
                },
            },
        }

    t_feedback_prompts = McpTool(
        name="feedback.prompts",
        description="Return all three raw Twilly template strings (for debugging).",
        version="1",
        input_schema={"type": "object", "properties": {}},
        read_only=True,
        tags={"phase": "2", "group": "learning"},
    )

    async def _feedback_prompts(_: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "twilly-01-autopsy": _AUTOPSY_PROMPT,
            "twilly-02-postmortem": _POSTMORTEM_PROMPT,
            "twilly-03-feedback": _FEEDBACK_LOOP_PROMPT,
        }

    return [
        (t_autopsy, _autopsy),
        (t_postmortem, _postmortem),
        (t_feedback_loop, _feedback_loop),
        (t_feedback_prompts, _feedback_prompts),
    ]


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)
