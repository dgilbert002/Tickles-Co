"""Company template loader.

Templates are JSON files under ``shared/templates/companies/*.json``. Loaded
at call time (no caching) so edits take effect without restarting the MCP
daemon — template changes feel like reloading a config file.

See ``shared/templates/companies/README.md`` for the schema.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger("tickles.provisioning.templates")

# shared/provisioning/templates.py -> shared/templates/companies
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "companies"

VALID_CATEGORIES = {"general", "trading"}
VALID_RULE_ONE_MODES = {"advisory", "strict", "off"}
# Superset of Paperclip's AGENT_ROLES enum plus a few historical/friendly
# aliases (analyst, observer, member, quant, ledger). The executor maps any
# non-canonical value to a canonical one via shared.provisioning.executor
# ._map_role_for_paperclip before POSTing to Paperclip, preserving the
# original value under metadata.templateRole.
VALID_AGENT_ROLES = {
    # Paperclip canonical
    "ceo", "cto", "cmo", "cfo",
    "engineer", "designer", "pm", "qa", "devops",
    "researcher", "general",
    # Historical friendly aliases (still supported; mapped at hire time)
    "member", "analyst", "observer", "quant", "ledger",
}


@dataclass
class TemplateAgent:
    url_key: str
    name: str
    role: str
    model: str
    soul: Optional[str]
    skills: List[str]
    budget_monthly_cents: int
    clone_openclaw_from: str = "cody"


@dataclass
class TemplateRoutine:
    kind: str
    trigger: str


@dataclass
class Template:
    """Validated, in-memory representation of a company template."""

    id: str
    name: str
    description: str
    category: str
    layer2_trading: bool
    rule_one_mode: str
    memu_subscriptions: List[str]
    venues: List[str]
    skills: List[str]
    agents: List[TemplateAgent] = field(default_factory=list)
    routines: List[TemplateRoutine] = field(default_factory=list)

    def is_trading(self) -> bool:
        return self.category == "trading" or self.layer2_trading

    def to_public_dict(self) -> Dict[str, Any]:
        """Shape safe to return to the Paperclip UI dropdown."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "layer2Trading": self.layer2_trading,
            "ruleOneMode": self.rule_one_mode,
            "memuSubscriptions": list(self.memu_subscriptions),
            "venues": list(self.venues),
            "agentCount": len(self.agents),
            "skillCount": len(self.skills),
            "routineCount": len(self.routines),
        }


def _parse(data: Dict[str, Any], template_id: str) -> Template:
    """Validate raw JSON and return a Template. Raises ValueError on issues."""

    def _require(key: str) -> Any:
        if key not in data:
            raise ValueError(f"template {template_id!r}: missing required field {key!r}")
        return data[key]

    category = _require("category")
    if category not in VALID_CATEGORIES:
        raise ValueError(f"template {template_id!r}: category must be one of {VALID_CATEGORIES}")

    rule_one_mode = data.get("rule_one_mode", "off")
    if rule_one_mode not in VALID_RULE_ONE_MODES:
        raise ValueError(
            f"template {template_id!r}: rule_one_mode must be one of {VALID_RULE_ONE_MODES}"
        )

    agents_raw = data.get("agents", [])
    agents: List[TemplateAgent] = []
    for idx, a in enumerate(agents_raw):
        role = a.get("role", "member")
        if role not in VALID_AGENT_ROLES:
            raise ValueError(
                f"template {template_id!r}: agent[{idx}].role must be one of {VALID_AGENT_ROLES}"
            )
        agents.append(
            TemplateAgent(
                url_key=a["urlKey"],
                name=a["name"],
                role=role,
                model=a["model"],
                soul=a.get("soul"),
                skills=list(a.get("skills", [])),
                budget_monthly_cents=int(a.get("budgetMonthlyCents", 0)),
                clone_openclaw_from=a.get("clone_openclaw_from", "cody"),
            )
        )

    routines_raw = data.get("routines", [])
    routines = [TemplateRoutine(kind=r["kind"], trigger=r["trigger"]) for r in routines_raw]

    return Template(
        id=_require("id"),
        name=_require("name"),
        description=data.get("description", ""),
        category=category,
        layer2_trading=bool(data.get("layer2_trading", False)),
        rule_one_mode=rule_one_mode,
        memu_subscriptions=list(data.get("memu_subscriptions", [])),
        venues=list(data.get("venues", [])),
        skills=list(data.get("skills", [])),
        agents=agents,
        routines=routines,
    )


def load(template_id: str, *, template_dir: Optional[Path] = None) -> Template:
    """Load + validate a single template by id.

    Raises FileNotFoundError if no such template, ValueError if invalid.
    """
    root = template_dir or _TEMPLATE_DIR
    path = root / f"{template_id}.json"
    LOG.debug("[templates.load] id=%s path=%s", template_id, path)
    if not path.exists():
        raise FileNotFoundError(f"template {template_id!r} not found at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("id") and raw["id"] != template_id:
        # filename is the source of truth for id
        LOG.warning(
            "[templates.load] id mismatch: filename=%s json.id=%s (using filename)",
            template_id, raw.get("id"),
        )
        raw["id"] = template_id
    else:
        raw.setdefault("id", template_id)
    return _parse(raw, template_id)


def list_available(*, template_dir: Optional[Path] = None) -> List[Template]:
    """Discover every JSON file in the templates dir and return as Templates.

    Skips files that fail to parse (logs a warning) so a single bad template
    doesn't break the whole wizard.
    """
    root = template_dir or _TEMPLATE_DIR
    if not root.exists():
        LOG.warning("[templates.list_available] template dir missing: %s", root)
        return []
    out: List[Template] = []
    for path in sorted(root.glob("*.json")):
        tid = path.stem
        try:
            out.append(load(tid, template_dir=root))
        except Exception as err:
            LOG.warning("[templates.list_available] skip %s: %s", tid, err)
    LOG.info("[templates.list_available] found=%d path=%s", len(out), root)
    return out
