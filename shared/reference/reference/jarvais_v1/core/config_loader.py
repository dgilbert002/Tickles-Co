"""
JarvAIs — Shared Database Configuration Loader
================================================
Centralised helpers for reading agent souls, system_config values,
and prompt templates from the database. Every service that needs
dynamic configuration should use these helpers instead of writing
its own DB query inline.

Design principle: every function has a code-level default fallback.
If the DB is unreachable or the key is missing, behavior is identical
to the previous hard-coded version. This makes migration safe.

Usage:
    from core.config_loader import (
        get_system_config, get_system_config_int, get_system_config_float,
        get_agent_soul, load_prompt, build_agent_system_prompt,
    )

    # Read a single config value
    val = get_system_config(db, "bear_confidence_hard_threshold", "75")

    # Read typed values
    max_tok = get_system_config_int(db, "auditor_max_tokens", 8192)
    temp = get_system_config_float(db, "auditor_temperature", 0.2)

    # Load an agent's soul + identity from agent_profiles
    soul = get_agent_soul(db, "bear")  # {"soul": "...", "identity_prompt": "..."}

    # Load a prompt from system_config with code fallback
    prompt = load_prompt(db, "tracker_system_prompt", TRACKER_DEFAULT)

    # Load soul with runtime variable injection
    sys_prompt = build_agent_system_prompt(db, "tracker", {"symbol": "BTCUSDT"})
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("jarvais.config_loader")


# ── system_config readers ──────────────────────────────────────────

def get_system_config(db, key: str, default: str = "",
                      duo_id: Optional[str] = None) -> str:
    """Read a single value from the system_config table.

    When *duo_id* is provided, tries ``key:duo_id`` first (e.g.
    ``dossier_stage2_prompt:optimus``). Falls back to the bare *key*
    if the duo-specific key doesn't exist. This enables per-duo
    prompt overrides without breaking any existing callers.

    Args:
        db: DatabaseManager instance.
        key: The config_key to look up.
        default: Returned if the key is missing or on DB error.
        duo_id: Optional duo ID for duo-specific key lookup.

    Returns:
        The config_value as a string, or *default*.
    """
    if duo_id:
        duo_key = f"{key}:{duo_id}"
        try:
            row = db.fetch_one(
                "SELECT config_value FROM system_config WHERE config_key = %s",
                (duo_key,))
            if row and row.get("config_value") is not None:
                logger.debug(f"[ConfigLoader] Using duo-specific key '{duo_key}'")
                return row["config_value"]
        except Exception as exc:
            logger.debug(f"[ConfigLoader] DB read failed for duo key '{duo_key}': {exc}")

    try:
        row = db.fetch_one(
            "SELECT config_value FROM system_config WHERE config_key = %s",
            (key,))
        if row and row.get("config_value") is not None:
            return row["config_value"]
    except Exception as exc:
        logger.debug(f"[ConfigLoader] DB read failed for '{key}': {exc}")
    return default


def get_system_config_int(db, key: str, default: int = 0) -> int:
    """Read an integer from system_config. Returns *default* on any error."""
    raw = get_system_config(db, key, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


def get_system_config_float(db, key: str, default: float = 0.0) -> float:
    """Read a float from system_config. Returns *default* on any error."""
    raw = get_system_config(db, key, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


# ── Agent profile readers ──────────────────────────────────────────

def get_agent_soul(db, agent_id: str) -> Optional[Dict[str, str]]:
    """Load an agent's soul and identity_prompt from agent_profiles.

    Returns:
        Dict with 'soul' and 'identity_prompt' keys, or None if the
        agent doesn't exist or is inactive.
    """
    try:
        row = db.fetch_one(
            "SELECT soul, identity_prompt FROM agent_profiles "
            "WHERE agent_id = %s AND is_active = 1",
            (agent_id,))
        if row:
            return {
                "soul": row.get("soul") or "",
                "identity_prompt": row.get("identity_prompt") or "",
            }
    except Exception as exc:
        logger.debug(f"[ConfigLoader] Could not load soul for '{agent_id}': {exc}")
    return None


def get_agent_profile(db, agent_id: str) -> Optional[Dict[str, Any]]:
    """Fetch an agent's full profile row from agent_profiles.

    Returns:
        Dict of all non-None columns, or None if not found.
    """
    try:
        row = db.fetch_one(
            "SELECT * FROM agent_profiles WHERE agent_id = %s",
            (agent_id,))
        if row:
            return {k: v for k, v in row.items() if v is not None}
    except Exception as exc:
        logger.debug(f"[ConfigLoader] Could not load profile for '{agent_id}': {exc}")
    return None


# ── Prompt loaders ─────────────────────────────────────────────────

def load_prompt(db, config_key: str, code_fallback: str,
                min_length: int = 10,
                duo_id: Optional[str] = None) -> str:
    """Load a prompt from system_config, falling back to *code_fallback*.

    When *duo_id* is provided, tries ``config_key:duo_id`` first
    (e.g. ``dossier_stage2_prompt:optimus``). If no duo-specific
    prompt exists, falls back to the global ``config_key``, then
    to *code_fallback*. This enables per-duo prompt customisation
    without duplicating prompts for duos that share the default.

    Args:
        db: DatabaseManager instance.
        config_key: The system_config key (e.g. 'tracker_system_prompt').
        code_fallback: The original hard-coded prompt text.
        min_length: Minimum chars for the DB value to be accepted.
            Prevents empty or stub values from replacing real prompts.
        duo_id: Optional duo ID for duo-specific prompt lookup.

    Returns:
        The DB prompt if it exists and meets min_length, else code_fallback.
    """
    if duo_id:
        duo_key = f"{config_key}:{duo_id}"
        try:
            row = db.fetch_one(
                "SELECT config_value FROM system_config WHERE config_key = %s",
                (duo_key,))
            if row and row.get("config_value"):
                val = row["config_value"]
                if len(val) >= min_length:
                    logger.debug(f"[ConfigLoader] Loaded duo prompt '{duo_key}' "
                                 f"from DB ({len(val)} chars)")
                    return val
        except Exception as exc:
            logger.debug(f"[ConfigLoader] DB read failed for duo prompt '{duo_key}': {exc}")

    try:
        row = db.fetch_one(
            "SELECT config_value FROM system_config WHERE config_key = %s",
            (config_key,))
        if row and row.get("config_value"):
            val = row["config_value"]
            if len(val) >= min_length:
                logger.debug(f"[ConfigLoader] Loaded prompt '{config_key}' "
                             f"from DB ({len(val)} chars)")
                return val
            logger.debug(f"[ConfigLoader] Prompt '{config_key}' too short "
                         f"({len(val)} < {min_length}), trying agent soul")
    except Exception as exc:
        logger.debug(f"[ConfigLoader] DB read failed for prompt '{config_key}': {exc}")

    # Agent-soul fallback: when duo_id is provided, resolve the prompt
    # from the linked agent's soul. Works for all trading-related keys
    # (dossier prompts, identities, tracker, auditor, comparison, etc.)
    if duo_id:
        try:
            from core.duo_config import get_duo_agent_soul
            stage = (config_key
                     .replace("dossier_", "")
                     .replace("_prompt", "")
                     .replace("_identity", "")
                     .replace("apex_", "")
                     .replace("system_", ""))
            soul = get_duo_agent_soul(db, duo_id, stage)
            if soul:
                return soul
        except Exception as exc:
            logger.debug(f"[ConfigLoader] Agent soul fallback failed: {exc}")

    return code_fallback


def build_agent_system_prompt(db, agent_id: str,
                               context_vars: Optional[Dict[str, Any]] = None,
                               fallback: str = "") -> str:
    """Load an agent's soul from DB and inject runtime variables.

    The soul text may contain ``{variable}`` placeholders (e.g.
    ``{symbol}``, ``{dossier_id}``). This function fills them from
    *context_vars* using safe string formatting.

    Args:
        db: DatabaseManager instance.
        agent_id: The agent whose soul to load (e.g. 'bear', 'tracker').
        context_vars: Dict of runtime values to inject into placeholders.
        fallback: Returned if the agent has no soul in DB.

    Returns:
        The formatted soul text, or *fallback* if the agent is missing.
    """
    soul_data = get_agent_soul(db, agent_id)
    if not soul_data or not soul_data.get("soul"):
        if fallback:
            logger.debug(f"[ConfigLoader] No soul for '{agent_id}', using fallback")
            return fallback
        logger.warning(f"[ConfigLoader] Agent '{agent_id}' has no soul and no fallback")
        return ""

    template = soul_data["soul"]
    if context_vars:
        try:
            template = template.format_map(_SafeFormatDict(context_vars))
        except Exception as exc:
            logger.warning(f"[ConfigLoader] Template formatting failed for "
                          f"'{agent_id}': {exc} — using raw soul")
    return template


class _SafeFormatDict(dict):
    """Dict subclass that returns '{key}' for missing keys instead of
    raising KeyError. This makes .format_map() safe when the soul
    template has placeholders that aren't in context_vars."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
