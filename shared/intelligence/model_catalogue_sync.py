"""
Module: model_catalogue_sync
Purpose: Sync the live model + routing-policy lists from OpenRouter and Requesty
         into public.model_catalogue, so the dashboard Settings picker scrolls a
         fresh list (with cost + context + vision flag) instead of a static JSON.
Location: /opt/tickles/shared/intelligence/model_catalogue_sync.py
Round:   14 (2026-05-29)

Design
------
* Two fetchers, one per provider, each returning a list of NORMALISED rows:
    {provider, model_id, label, is_policy, is_vision, context_length,
     input_cost_per_mtok, output_cost_per_mtok, raw}
* OpenRouter: GET https://openrouter.ai/api/v1/models  (no key needed).
    - vision = "image" in architecture.input_modalities
    - pricing.prompt / pricing.completion are per-TOKEN USD strings → ×1e6.
* Requesty: GET {base}/models  (Bearer key).
    - policies have id starting "policy/"; supports_vision flag; input_price /
      output_price are per-TOKEN USD floats → ×1e6. Policy prices come back as
      0 (real cost depends on the routed model) so we store NULL for them.
* sync_all() upserts everything (ON CONFLICT (provider, model_id) DO UPDATE) and
  is safe to call repeatedly. A provider that errors does NOT wipe the other's
  rows — we only delete rows for a provider we successfully re-fetched.

Resilience
----------
If a provider fetch fails we log + skip it; the previously-synced rows for that
provider stay in the table so the picker still works (just slightly stale).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("tickles.intelligence.model_catalogue_sync")

_OPENROUTER_MODELS_URL = (
    os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    + "/models"
)
_HTTP_TIMEOUT_S = int(os.environ.get("MODEL_CATALOGUE_HTTP_TIMEOUT_S", "25"))


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------
def _per_token_to_per_mtok(val: Any) -> Optional[float]:
    """Convert a per-token USD price (string or float) to USD per 1M tokens."""
    if val is None or val == "":
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        # 0 = "free" placeholder (Requesty policies). Treat as unknown.
        return None
    return round(f * 1_000_000, 6)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
async def _fetch_json(url: str, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers or {}, timeout=timeout) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"GET {url} → HTTP {resp.status}: {text[:300]}")
            return await resp.json()


# ---------------------------------------------------------------------------
# Fetchers (return normalised rows)
# ---------------------------------------------------------------------------
async def fetch_openrouter() -> List[Dict[str, Any]]:
    data = await _fetch_json(_OPENROUTER_MODELS_URL)
    out: List[Dict[str, Any]] = []
    for m in data.get("data", []):
        arch = m.get("architecture") or {}
        modalities = arch.get("input_modalities") or []
        is_vision = "image" in modalities
        pricing = m.get("pricing") or {}
        out.append({
            "provider": "openrouter",
            "model_id": m.get("id"),
            "label": m.get("name") or m.get("id"),
            "is_policy": False,
            "is_vision": bool(is_vision),
            "context_length": m.get("context_length"),
            "input_cost_per_mtok": _per_token_to_per_mtok(pricing.get("prompt")),
            "output_cost_per_mtok": _per_token_to_per_mtok(pricing.get("completion")),
            "raw": m,
        })
    return [r for r in out if r["model_id"]]


def _requesty_creds() -> Tuple[str, str]:
    api_key = (
        os.environ.get("REQUESTY_API_KEY")
        or os.environ.get("REQUESTY_API")
        or os.environ.get("TICKLES_APP_VISION_API_KEY", "")
    )
    base_url = (
        os.environ.get("REQUESTY_BASE_URL")
        or os.environ.get("TICKLES_APP_REQUESTY_URL")
        or "https://router.requesty.ai/v1"
    ).rstrip("/")
    return api_key, base_url


async def fetch_requesty() -> List[Dict[str, Any]]:
    api_key, base_url = _requesty_creds()
    if not api_key:
        raise RuntimeError("Requesty API key not set (REQUESTY_API / REQUESTY_API_KEY)")
    data = await _fetch_json(
        base_url + "/models", headers={"Authorization": f"Bearer {api_key}"}
    )
    out: List[Dict[str, Any]] = []
    for m in data.get("data", []):
        mid = m.get("id") or ""
        is_policy = mid.startswith("policy/")
        # A clean label: strip the "policy/" prefix for display but keep the id.
        label = mid[len("policy/"):] + " (policy)" if is_policy else mid
        out.append({
            "provider": "requesty",
            "model_id": mid,
            "label": label,
            "is_policy": is_policy,
            "is_vision": bool(m.get("supports_vision")),
            "context_length": m.get("context_window"),
            # Policies report 0 → stored as NULL by _per_token_to_per_mtok.
            "input_cost_per_mtok": _per_token_to_per_mtok(m.get("input_price")),
            "output_cost_per_mtok": _per_token_to_per_mtok(m.get("output_price")),
            "raw": m,
        })
    return [r for r in out if r["model_id"]]


# ---------------------------------------------------------------------------
# Upsert + sync
# ---------------------------------------------------------------------------
async def _upsert_rows(pool, rows: List[Dict[str, Any]]) -> int:
    import json as _json

    n = 0
    for r in rows:
        await pool.execute(
            """
            INSERT INTO public.model_catalogue
              (provider, model_id, label, is_policy, is_vision, context_length,
               input_cost_per_mtok, output_cost_per_mtok, raw, synced_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,NOW())
            ON CONFLICT (provider, model_id) DO UPDATE SET
              label = EXCLUDED.label,
              is_policy = EXCLUDED.is_policy,
              is_vision = EXCLUDED.is_vision,
              context_length = EXCLUDED.context_length,
              input_cost_per_mtok = EXCLUDED.input_cost_per_mtok,
              output_cost_per_mtok = EXCLUDED.output_cost_per_mtok,
              raw = EXCLUDED.raw,
              synced_at = NOW()
            """,
            (
                r["provider"], r["model_id"], r["label"], r["is_policy"],
                r["is_vision"], r["context_length"],
                r["input_cost_per_mtok"], r["output_cost_per_mtok"],
                _json.dumps(r["raw"]),
            ),
        )
        n += 1
    return n


async def _prune_stale(pool, provider: str, keep_ids: List[str]) -> int:
    """Delete rows for `provider` whose model_id is no longer offered."""
    if not keep_ids:
        return 0
    # asyncpg/our pool uses $1 placeholders; pass the array as a single param.
    res = await pool.execute(
        "DELETE FROM public.model_catalogue "
        "WHERE provider = $1 AND model_id <> ALL($2::text[])",
        (provider, keep_ids),
    )
    try:
        return int(str(res).split()[-1])
    except (ValueError, IndexError):
        return 0


async def sync_all(*, pool=None) -> Dict[str, Any]:
    """Fetch both providers and upsert into model_catalogue.

    Returns a summary dict: {"openrouter": n, "requesty": m, "pruned": k,
    "errors": [...]}. A provider that errors is reported but does not abort the
    other provider's sync.
    """
    if pool is None:
        from shared.utils.db import get_shared_pool
        pool = await get_shared_pool()

    summary: Dict[str, Any] = {"openrouter": 0, "requesty": 0, "pruned": 0, "errors": []}

    for name, fetcher in (("openrouter", fetch_openrouter), ("requesty", fetch_requesty)):
        try:
            rows = await fetcher()
            count = await _upsert_rows(pool, rows)
            pruned = await _prune_stale(pool, name, [r["model_id"] for r in rows])
            summary[name] = count
            summary["pruned"] += pruned
            logger.info("model_catalogue sync %s: upserted=%d pruned=%d", name, count, pruned)
        except Exception as exc:
            logger.warning("model_catalogue sync %s FAILED: %s", name, exc)
            summary["errors"].append(f"{name}: {exc}")

    return summary


# ---------------------------------------------------------------------------
# Read helpers for the Settings API
# ---------------------------------------------------------------------------
async def list_for_picker(
    *,
    pool=None,
    provider: Optional[str] = None,
    vision_only: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return catalogue rows grouped as {"policies": [...], "models": [...]}.

    * provider: filter to 'openrouter'|'requesty' if given, else both.
    * vision_only: if True, only vision-capable rows (used for vision slots).
      Policies are always included (they can route to a vision model) when the
      provider is requesty.
    Each row: {provider, model_id, label, is_policy, is_vision, context_length,
               input_cost_per_mtok, output_cost_per_mtok}.
    Policies sort first; then models by provider, then by input cost asc.
    """
    if pool is None:
        from shared.utils.db import get_shared_pool
        pool = await get_shared_pool()

    where = []
    params: List[Any] = []
    i = 1
    if provider in ("openrouter", "requesty"):
        where.append(f"provider = ${i}")
        params.append(provider)
        i += 1
    if vision_only:
        # vision models OR any policy (policies can route to vision models).
        where.append("(is_vision = TRUE OR is_policy = TRUE)")
    sql = (
        "SELECT provider, model_id, label, is_policy, is_vision, context_length, "
        "       input_cost_per_mtok, output_cost_per_mtok, synced_at "
        "FROM public.model_catalogue "
    )
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += (
        "ORDER BY is_policy DESC, provider ASC, "
        "input_cost_per_mtok ASC NULLS LAST, label ASC"
    )
    rows = await pool.fetch_all(sql, tuple(params))

    policies: List[Dict[str, Any]] = []
    models: List[Dict[str, Any]] = []
    for r in rows:
        item = {
            "provider": r["provider"],
            "model_id": r["model_id"],
            "label": r["label"],
            "is_policy": bool(r["is_policy"]),
            "is_vision": bool(r["is_vision"]),
            "context_length": r["context_length"],
            "input_cost_per_mtok": (
                float(r["input_cost_per_mtok"]) if r["input_cost_per_mtok"] is not None else None
            ),
            "output_cost_per_mtok": (
                float(r["output_cost_per_mtok"]) if r["output_cost_per_mtok"] is not None else None
            ),
        }
        (policies if item["is_policy"] else models).append(item)
    return {"policies": policies, "models": models}


async def catalogue_count(*, pool=None) -> int:
    if pool is None:
        from shared.utils.db import get_shared_pool
        pool = await get_shared_pool()
    row = await pool.fetch_one("SELECT COUNT(*) AS n FROM public.model_catalogue", ())
    return int(row["n"]) if row else 0
