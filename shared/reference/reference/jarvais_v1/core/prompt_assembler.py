"""
JarvAIs Prompt Assembler
Dynamically constructs the full system prompt for any agent at call time.
Combines: soul + identity + company policies + operational prompt + tools manifest
         + subordinate registry (for delegators) + relevant memories (RAG).

Usage:
    from core.prompt_assembler import assemble_system_prompt
    prompt = assemble_system_prompt("atlas", db, rag_engine=rag)
"""

import json
import logging
from typing import Optional, Dict, List, Any

logger = logging.getLogger("jarvais.prompt_assembler")


# ─── Tool Definitions ────────────────────────────────────────────────────────
# Each tool the agent can call via structured JSON in its response.
# The manifest is injected into the system prompt so the model knows what's available.

TOOL_DEFINITIONS = {
    "delegate": {
        "name": "delegate",
        "description": "Delegate a task to another agent. Use when the task falls outside your expertise.",
        "schema": {
            "action": "delegate",
            "target_agent": "<agent_id of the delegate>",
            "task": "<clear description of what they should do>",
            "reason": "<why you're delegating to this agent>"
        }
    },
    "search_memory": {
        "name": "search_memory",
        "description": "Search the company's institutional memory (vector store) for relevant past events, conversations, trades, or analysis.",
        "schema": {
            "action": "tool_call",
            "tool": "search_memory",
            "arguments": {"query": "<natural language query>", "collections": ["<optional: trades, signals, conversations, etc.>"]}
        }
    },
    "query_db": {
        "name": "query_db",
        "description": "Run a read-only SQL SELECT query against the company database. Use to look up structured data like trades, balances, agent profiles, or performance metrics.",
        "schema": {
            "action": "tool_call",
            "tool": "query_db",
            "arguments": {"sql": "<SELECT ... FROM ... WHERE ...>"}
        }
    },
    "edit_db": {
        "name": "edit_db",
        "description": "Propose a database change. This creates an action point for CEO approval -- it does NOT execute immediately.",
        "schema": {
            "action": "tool_call",
            "tool": "edit_db",
            "arguments": {"table": "<table_name>", "record_id": "<int>", "updates": {"<field>": "<value>"}}
        }
    },
    "search_internet": {
        "name": "search_internet",
        "description": "Search the internet for current information. Use sparingly and cross-reference with internal data.",
        "schema": {
            "action": "tool_call",
            "tool": "search_internet",
            "arguments": {"query": "<search query>", "reason": "<why you need this>"}
        }
    },
    "schedule": {
        "name": "schedule",
        "description": "Schedule a future task for yourself or another agent. Creates a cron job or one-time task.",
        "schema": {
            "action": "tool_call",
            "tool": "schedule",
            "arguments": {"task_name": "<descriptive name>", "target_agent": "<agent_id>", "cron_schedule": "<cron expression or 'once'>", "payload": {}}
        }
    },
    "update_action_point": {
        "name": "update_action_point",
        "description": "Update the status of an action point you are responsible for.",
        "schema": {
            "action": "tool_call",
            "tool": "update_action_point",
            "arguments": {"action_point_id": "<int>", "status": "<in_progress|pending_approval>", "notes": "<progress details>"}
        }
    },
    "edit_agent_profile": {
        "name": "edit_agent_profile",
        "description": "Propose changes to an agent's profile (soul, identity, tools, models). Creates a proposal for CEO approval.",
        "schema": {
            "action": "tool_call",
            "tool": "edit_agent_profile",
            "arguments": {"target_agent": "<agent_id>", "field": "<soul|identity_prompt|tools|...>", "new_value": "<proposed value>", "reason": "<justification>"}
        }
    },
    "analyze_content": {
        "name": "analyze_content",
        "description": (
            "Analyze a feed/alpha item using the Source Model Matrix (the optimal model for that source + media type). "
            "Provide a news_item_id to analyze. Supports text, image, voice, and video. "
            "Can re-analyze items that already have analysis — the new result overwrites the previous one. "
            "Returns the full analysis result. Use this instead of guessing about content."
        ),
        "schema": {
            "action": "tool_call",
            "tool": "analyze_content",
            "arguments": {
                "news_item_id": "<int: ID of the news_items row>",
                "focus": "<optional: specific aspect to focus on, e.g. 'trade signals', 'sentiment', 'entry/exit levels'>"
            }
        }
    },
}


def assemble_system_prompt(agent_id: str, db, rag_engine=None,
                           conversation_context: str = "",
                           extra_context: str = "",
                           user_query: str = "",
                           preloaded_rag_results: Optional[list] = None,
                           rag_profile: Optional[str] = None) -> str:
    """
    Build the full system prompt for an agent.

    Args:
        agent_id: The agent's unique ID (e.g., 'atlas', 'apex')
        db: Database connection (db.database.get_db())
        rag_engine: Optional RagSearchEngine for memory retrieval
        conversation_context: Pre-loaded context from chain snapshots
        extra_context: Any additional context (e.g., current task details)
        user_query: The user's actual question (used for targeted memory search)
        preloaded_rag_results: Pre-fetched RAG results (skips internal generic search)
        rag_profile: RAG budget profile name ('chat', 'trade_analysis', etc.)

    Returns:
        Complete system prompt string ready for model call
    """
    logger.debug(f"[PromptAssembler] Assembling prompt for agent={agent_id}")

    # ── 1. Load agent profile ────────────────────────────────────────────
    profile = db.fetch_one(
        "SELECT * FROM agent_profiles WHERE agent_id = %s AND is_active = 1",
        (agent_id,)
    )
    if not profile:
        logger.error(f"[PromptAssembler] Agent profile not found: {agent_id}")
        return f"You are agent '{agent_id}'. Your profile could not be loaded. Answer helpfully."

    # ── 2. Load operational prompt from prompt_versions ──────────────────
    prompt_role = profile.get("prompt_role") or agent_id
    prompt_version = db.fetch_one(
        "SELECT system_prompt, user_prompt_template, model, model_provider "
        "FROM prompt_versions WHERE role = %s AND is_active = 1 "
        "ORDER BY version DESC LIMIT 1",
        (prompt_role,)
    )

    # ── 3. Load company charter ──────────────────────────────────────────
    company_charter = ""
    cc_row = db.fetch_one(
        "SELECT config_value FROM system_config WHERE config_key = 'company_charter'"
    )
    if cc_row and cc_row.get("config_value"):
        company_charter = cc_row["config_value"]

    # ── 4. Load department charter ───────────────────────────────────────
    dept_charter = ""
    agent_dept = profile.get("department")
    if agent_dept:
        dc_row = db.fetch_one(
            "SELECT charter_text FROM department_charters WHERE department = %s",
            (agent_dept,)
        )
        if dc_row and dc_row.get("charter_text"):
            dept_charter = dc_row["charter_text"]

    # ── 5. Load company policies ─────────────────────────────────────────
    policies = db.fetch_all(
        "SELECT policy_key, policy_value, description, category "
        "FROM company_policies WHERE is_active = 1 ORDER BY category, policy_key"
    )

    # ── 6. Build tools manifest ──────────────────────────────────────────
    agent_tools = _parse_json_field(profile.get("tools"))
    tools_manifest = _build_tools_manifest(agent_tools, db=db)

    # ── 7. Build delegation registry (all agents can delegate) ──────────
    subordinate_registry = _build_subordinate_registry(agent_id, profile, db)

    # ── 8. Load relevant memories via RAG (if engine available) ──────────
    memory_text = ""
    if preloaded_rag_results:
        memory_text = _format_rag_as_context(preloaded_rag_results, profile=rag_profile)
    elif rag_engine and hasattr(rag_engine, "search"):
        memory_text = _load_relevant_memories(
            agent_id, profile, rag_engine, user_query=user_query,
            rag_profile=rag_profile
        )

    # ── 9. Assemble all sections ─────────────────────────────────────────
    sections = []

    if company_charter:
        sections.append(_section("COMPANY CHARTER", company_charter))

    sections.append(_section("YOUR IDENTITY",
        f"You are **{profile.get('display_name', agent_id)}** "
        f"({profile.get('emoji', '')} {profile.get('title', '')}).\n"
        f"Department: {profile.get('department', 'unassigned').title()}\n"
        f"You report to: {profile.get('reports_to') or 'DeanDogDXB (Human CEO)'}\n"
        f"Autonomy level: {profile.get('autonomy', 'propose')}"
    ))

    if dept_charter:
        sections.append(_section(
            f"YOUR DEPARTMENT CHARTER ({(agent_dept or 'unassigned').replace('_', ' ').title()})",
            dept_charter
        ))

    if profile.get("soul"):
        sections.append(_section("YOUR SOUL", profile["soul"]))

    if profile.get("identity_prompt"):
        sections.append(_section("YOUR ROLE & RESPONSIBILITIES", profile["identity_prompt"]))

    if prompt_version and prompt_version.get("system_prompt"):
        sections.append(_section("YOUR OPERATIONAL INSTRUCTIONS", prompt_version["system_prompt"]))

    if policies:
        policy_text = _format_policies(policies)
        sections.append(_section("COMPANY POLICIES YOU MUST FOLLOW", policy_text))

    if tools_manifest:
        sections.append(_section("YOUR TOOLS", tools_manifest))

    if subordinate_registry:
        sections.append(_section("YOUR TEAM (agents you can delegate to)", subordinate_registry))

    if memory_text:
        sections.append(_section("RELEVANT MEMORIES & CONTEXT", memory_text))

    if conversation_context:
        sections.append(_section("CONVERSATION CONTEXT (from previous sessions)", conversation_context))

    if extra_context:
        sections.append(_section("CURRENT TASK CONTEXT", extra_context))

    sections.append(_section("RESPONSE FORMAT",
        _build_response_format_instructions(agent_tools, profile.get("delegation_policy", "none"))
    ))

    assembled = "\n\n".join(sections)
    logger.debug(f"[PromptAssembler] Assembled {len(assembled)} chars for {agent_id} "
                 f"({len(sections)} sections)")
    return assembled


def get_agent_model(agent_id: str, db, role: str = "primary") -> Dict[str, str]:
    """
    Resolve which model an agent should use.

    Returns dict with: model_id, provider, api_key_env
    Falls back to config.json primary/fallback if agent has no model set.
    """
    profile = db.fetch_one(
        "SELECT model_mode, free_model_primary, free_model_fallback, "
        "paid_model_primary, paid_model_fallback, prompt_role "
        "FROM agent_profiles WHERE agent_id = %s",
        (agent_id,)
    )
    if not profile:
        return {"model_id": None, "provider": None}

    mode = profile.get("model_mode", "free")

    if role == "primary":
        model_id = profile.get(f"{mode}_model_primary")
    else:
        model_id = profile.get(f"{mode}_model_fallback")

    if not model_id:
        if role == "primary":
            model_id = profile.get("free_model_primary") or profile.get("paid_model_primary")
        else:
            model_id = profile.get("free_model_fallback") or profile.get("paid_model_fallback")

    if not model_id:
        return {"model_id": None, "provider": None}

    provider = _resolve_provider(model_id, db)
    return {"model_id": model_id, "provider": provider}


# ─── Private Helpers ──────────────────────────────────────────────────────────

def _section(title: str, content: str) -> str:
    """Format a named section with markdown header."""
    return f"## {title}\n{content}"


def _parse_json_field(value) -> list:
    """Safely parse a JSON field that might be a string, list, or None."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def _build_tools_manifest(agent_tools: list, db=None) -> str:
    """Build a tools manifest string for the system prompt.
    If db is provided and agent has query_db, loads the schema reference
    from system_config so agents know the actual table/column names."""
    if not agent_tools:
        return ""

    schema_ref = ""
    if db and "query_db" in agent_tools:
        try:
            row = db.fetch_one(
                "SELECT config_value FROM system_config "
                "WHERE config_key = 'db_schema_reference'"
            )
            if row and row.get("config_value"):
                schema_ref = row["config_value"]
        except Exception as e:
            logger.debug(f"[PromptAssembler] Could not load db_schema_reference: {e}")

    lines = ["You have access to the following tools. To use a tool, include a JSON block "
             "in your response wrapped in ```json\\n...\\n``` tags:\n"]

    for tool_name in agent_tools:
        tool_def = TOOL_DEFINITIONS.get(tool_name)
        if tool_def:
            lines.append(f"### {tool_def['name']}")
            description = tool_def['description']
            if tool_name == "query_db" and schema_ref:
                description += f"\n\n{schema_ref}"
            lines.append(description)
            lines.append(f"Usage: ```json\n{json.dumps(tool_def['schema'], indent=2)}\n```\n")

    lines.append("CRITICAL TOOL RULES:\n"
                 "1. When you need data, ALWAYS include the tool call JSON block in your response immediately — "
                 "never just announce intent to query without the actual tool call.\n"
                 "2. Put your brief reasoning first, then the tool call block. Only use ONE tool per response.\n"
                 "3. If a query fails, fix the SQL and try again in your next response with a corrected tool call.\n"
                 "4. After receiving tool results, synthesize the data into a clear, analytical answer.")
    return "\n".join(lines)


def _build_subordinate_registry(agent_id: str, profile: dict, db) -> str:
    """Build a registry of agents this agent can delegate to.
    All agents can delegate -- direct reports, peers, or anyone in the company."""
    can_delegate_to = _parse_json_field(profile.get("can_delegate_to"))

    if can_delegate_to:
        placeholders = ",".join(["%s"] * len(can_delegate_to))
        agents = db.fetch_all(
            f"SELECT agent_id, display_name, emoji, title, department, expertise_tags "
            f"FROM agent_profiles WHERE agent_id IN ({placeholders}) AND is_active = 1",
            tuple(can_delegate_to)
        )
    else:
        agents = db.fetch_all(
            "SELECT agent_id, display_name, emoji, title, department, expertise_tags, reports_to "
            "FROM agent_profiles WHERE is_active = 1 AND agent_id != %s",
            (agent_id,)
        )

    if not agents:
        return ""

    direct_reports = [a for a in agents if a.get("reports_to") == agent_id]
    superior = profile.get("reports_to", "")
    peers_and_others = [a for a in agents if a.get("reports_to") != agent_id]

    lines = []

    if direct_reports:
        lines.append("**Your direct reports** (delegate work that falls in their expertise):\n")
        for agent in direct_reports:
            tags = _parse_json_field(agent.get("expertise_tags"))
            tag_str = ", ".join(tags[:6]) if tags else "general"
            lines.append(
                f"- **{agent.get('emoji', '')} {agent['display_name']}** "
                f"(`{agent['agent_id']}`) — {agent.get('title', 'Agent')} "
                f"| Expertise: {tag_str}"
            )

    lines.append("\n**Other agents you can ask for help** (delegate when you need info or assistance):\n")
    by_dept = {}
    for agent in peers_and_others:
        dept = agent.get("department", "other").title()
        by_dept.setdefault(dept, []).append(agent)

    for dept, dept_agents in sorted(by_dept.items()):
        lines.append(f"  *{dept}*:")
        for agent in dept_agents:
            tags = _parse_json_field(agent.get("expertise_tags"))
            tag_str = ", ".join(tags[:4]) if tags else "general"
            lines.append(
                f"  - `{agent['agent_id']}` ({agent.get('display_name', '')}) — {tag_str}"
            )

    if superior:
        lines.append(f"\nYou report to `{superior}`. Escalate when you need authority or approval.")

    lines.append(
        "\nTo delegate, include a ```json block using the delegate format. "
        "You can delegate to MULTIPLE agents simultaneously. "
        "Delegation is for getting help, finding information, or coordinating work -- "
        "NOT for avoiding your own responsibilities."
    )
    return "\n".join(lines)


_ALPHA_AGENTS = {"atlas", "apex", "warren", "quant", "lens", "scribe", "vox", "reel", "mentor", "elon", "curiosity"}
_ALPHA_COLLECTIONS = ["alpha_analysis", "signals", "alpha_timeframes", "feed_items"]


_PROFILE_DEFAULTS = {
    "max_context_chars": 40000,
    "per_item_budget": 3000,
    "min_relevance_score": 0.35,
    "rag_result_limit": 15,
    "tool_search_budget": 2000,
}


def _load_assembler_config() -> dict:
    """Load prompt_assembler section from config.json."""
    try:
        import os
        config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
        with open(config_path, "r") as f:
            cfg = json.load(f)
        return cfg.get("global", {}).get("prompt_assembler", {})
    except Exception:
        return {}


def _resolve_profile(profile_name: Optional[str] = None) -> dict:
    """Resolve a named RAG profile to its parameter dict.
    Falls back to default_profile from config, then to built-in defaults."""
    cfg = _load_assembler_config()
    profiles = cfg.get("profiles", {})
    if not profile_name:
        profile_name = cfg.get("default_profile", "chat")
    resolved = dict(_PROFILE_DEFAULTS)
    if profile_name in profiles:
        resolved.update(profiles[profile_name])
    elif not profiles:
        for k in _PROFILE_DEFAULTS:
            if k in cfg:
                resolved[k] = cfg[k]
    return resolved


def _get_assembler_param(key: str, default, profile: Optional[str] = None):
    """Get a prompt_assembler config parameter for a given profile."""
    resolved = _resolve_profile(profile)
    return resolved.get(key, default)


def get_available_profiles() -> dict:
    """Return all available RAG profile names and their settings."""
    cfg = _load_assembler_config()
    profiles = cfg.get("profiles", {})
    result = {}
    for name, params in profiles.items():
        merged = dict(_PROFILE_DEFAULTS)
        merged.update(params)
        result[name] = merged
    return result


def _format_rag_as_context(results: list, profile: Optional[str] = None) -> str:
    """
    Format RAG search results as rich, full-text content blocks.
    Limits come from the active RAG profile (chat, trade_analysis, etc.).
    """
    if not results:
        return ""

    params = _resolve_profile(profile)
    max_context_chars = params["max_context_chars"]
    per_item_budget = params["per_item_budget"]
    min_score = params["min_relevance_score"]

    blocks = []
    total_chars = 0
    for hit in results:
        if total_chars >= max_context_chars:
            break
        score = getattr(hit, "score", 0)
        if score < min_score:
            continue

        text = getattr(hit, "text", str(hit))
        collection = getattr(hit, "collection", "unknown")
        source_table = getattr(hit, "source_table", "")
        source_id = getattr(hit, "source_id", 0)
        ts = getattr(hit, "timestamp", "") or ""
        meta = getattr(hit, "metadata", {}) or {}

        source = meta.get("source", collection)
        author = meta.get("author", "")

        budget = min(per_item_budget, max_context_chars - total_chars)
        content = text[:budget]

        attribution = f"[Source: {source}"
        if author:
            attribution += f", {author}"
        if ts:
            attribution += f", {str(ts)[:19]}"
        if source_id:
            attribution += f", #{source_id}"
        attribution += "]"

        block = f"{attribution}\n{content}"
        blocks.append(block)
        total_chars += len(block)

    if not blocks:
        return ""
    return (
        f"The following {len(blocks)} items were retrieved from company knowledge "
        f"(news feeds, analyses, conversations, signals). Use ALL of this information "
        f"to answer comprehensively.\n\n"
        + "\n\n---\n\n".join(blocks)
    )


def _load_relevant_memories(agent_id: str, profile: dict, rag_engine,
                            user_query: str = "",
                            rag_profile: Optional[str] = None) -> str:
    """
    Load relevant memories from the vector store.
    Uses the user's actual query when available, falls back to a generic search.
    rag_profile selects the RAG budget profile (chat, trade_analysis, etc.).
    """
    try:
        if user_query:
            query = user_query[:500]
        else:
            tags = _parse_json_field(profile.get("expertise_tags"))
            query = f"Recent important events relevant to {profile.get('display_name', agent_id)}"
            if tags:
                query += f" (expertise: {', '.join(tags[:3])})"

        params = _resolve_profile(rag_profile)
        rag_limit = params["rag_result_limit"]
        results = rag_engine.search(query, collections=None, limit=rag_limit, hybrid=True)
        return _format_rag_as_context(results, profile=rag_profile)
    except Exception as e:
        logger.debug(f"[PromptAssembler] Memory load failed for {agent_id}: {e}")
        return ""


def _format_policies(policies: list) -> str:
    """Format company policies grouped by category."""
    by_category = {}
    for p in policies:
        cat = p.get("category", "general").title()
        if cat not in by_category:
            by_category[cat] = []
        desc = f" — {p['description']}" if p.get("description") else ""
        by_category[cat].append(f"- **{p['policy_key']}**: {p['policy_value']}{desc}")

    lines = []
    for cat, items in by_category.items():
        lines.append(f"### {cat}")
        lines.extend(items)
    return "\n".join(lines)


def _build_response_format_instructions(agent_tools: list, delegation_policy: str) -> str:
    """Build structural response instructions. Governance rules come from company_policies."""
    lines = [
        "## HOW TO USE COMPANY MEMORY CONTEXT\n"
        "When you receive context from company memory (news, analyses, signals, conversations), "
        "you MUST synthesize it into a comprehensive, well-structured response:\n"
        "- Do NOT list raw data as bullet points. Analyze the information, connect themes, "
        "identify patterns, and present insights as a clear narrative.\n"
        "- Cite sources naturally in your text: (Reuters, Feb 24), (geopolitics_prime, Telegram), "
        "(JoeChampion, TradingView #12345).\n"
        "- If the topic is complex, organize with clear sections and headings.\n"
        "- Draw conclusions and highlight what matters most.\n"
        "- If you have context on a topic, USE IT — do not say 'I don't have information' "
        "when the context is right in front of you.",
    ]

    if "delegate" in agent_tools:
        lines.append(
            "\n## DELEGATION FORMAT\n"
            "To delegate, include a ```json block:\n"
            "```json\n"
            '{"action": "delegate", "target_agent": "agent_id_here", '
            '"task": "clear description", "reason": "why"}\n'
            "```\n"
            "You can include MULTIPLE ```json blocks for parallel delegations."
        )

    if agent_tools:
        lines.append(
            "\n## TOOL FORMAT\n"
            "Include your reasoning first, then the tool call ```json block. "
            "After a tool returns results, incorporate the ACTUAL data into your response."
        )

    return "\n".join(lines)


def _resolve_provider(model_id: str, db) -> str:
    """Resolve the provider for a model_id from the known_models table."""
    if not model_id:
        return "openrouter"

    row = db.fetch_one(
        "SELECT provider FROM known_models WHERE model_id = %s LIMIT 1",
        (model_id,)
    )
    if row:
        return row["provider"]

    if "/" in model_id:
        return "openrouter"
    if "claude" in model_id.lower():
        return "anthropic"
    if "gpt" in model_id.lower() or "o1" in model_id.lower():
        return "openai"
    if "gemini" in model_id.lower():
        return "google"
    return "openrouter"
