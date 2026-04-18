"""
Updates MemClaw workspace with CONTEXT_V3.md, SQL DDL, and updated README.
Replaces V2 content with the definitive V3 blueprint.
"""
import os, sys, json, requests, time

API_KEY = os.environ.get("FELO_API_KEY")
if not API_KEY:
    print("[memclaw_update] ERROR: FELO_API_KEY environment variable not set")
    sys.exit(1)
API_BASE = "https://openapi.felo.ai/v2"
SHORT_ID = os.environ.get("MEMCLAW_SHORT_ID")
if not SHORT_ID:
    print("[memclaw_update] ERROR: MEMCLAW_SHORT_ID environment variable not set")
    sys.exit(1)
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json", "Accept": "application/json"}
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def api(method, path, body=None):
    url = f"{API_BASE}{path}"
    r = requests.request(method, url, headers=HEADERS, json=body, timeout=120)
    if not r.ok:
        print(f"  ERROR {r.status_code}: {r.text[:300]}")
        return None
    return r.json()


def add_doc(title, content):
    print(f"[add-doc] {title} ({len(content):,} chars)...")
    result = api("POST", f"/livedocs/{SHORT_ID}/resources/doc", {"title": title, "content": content})
    if result and result.get("data"):
        rid = result["data"].get("id", "?")
        print(f"  OK: resource_id={rid}")
        return rid
    return None


def update_readme(summary, content):
    print(f"[update-readme] ({len(content):,} chars)...")
    result = api("PUT", f"/livedocs/{SHORT_ID}/readme", {"summary": summary, "content": content})
    if result:
        print("  OK")
    return result


def upload_file(filepath):
    fname = os.path.basename(filepath)
    print(f"[upload] {fname}...")
    url = f"{API_BASE}/livedocs/{SHORT_ID}/resources/upload"
    h = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
    with open(filepath, "rb") as f:
        r = requests.post(url, headers=h, files={"file": (fname, f)}, timeout=120)
    if r.ok:
        data = r.json().get("data", {})
        print(f"  OK: resource_id={data.get('id', '?')}")
        return data.get("id")
    else:
        print(f"  ERROR {r.status_code}: {r.text[:300]}")
        return None


def create_task(title, status=2):
    result = api("POST", f"/livedocs/{SHORT_ID}/tasks", {"title": title, "status": status, "sort": 0})
    if result and result.get("data"):
        print(f"  [task] {title} -> {'DONE' if status==2 else 'TODO'}")


README_SUMMARY = "JarvAIs V2.0: AI crypto/CFD trading. 3 Rules: Backtest parity (99.9%), execution accuracy (track everything), memory efficiency. 6 LLM agents + 11 Python services. 24 normalized tables. 250 indicators. Start $500 -> $5M+. CONTEXT_V3.md is the definitive build blueprint."

README_CONTENT = r"""# JarvAIs V2.0 — Definitive Build Blueprint

## What This Is

CONTEXT_V3.md (attached) is the ONLY document needed to build V2.0 from scratch. It merges:
- CONTEXT_V2.md (1082-line encyclopedia)
- Gemini architectural review (13 issues verified against code)
- Normalized 24-table database schema
- Unified indicator architecture (~250 indicators)
- Complete file-by-file porting reference

## The Three Non-Negotiable Rules

1. **Rule 1: Backtest-to-Live Parity (99.9%)** — If backtest profits but live doesn't, system is BROKEN. Track candle_data_hash, signal_params_hash, expected vs actual prices, data drift.
2. **Rule 2: Execution Accuracy** — DECIMAL(20,8) for prices, DATETIME(3) for timestamps, individual fee tracking in trade_cost_entries table, slippage tracking.
3. **Rule 3: Memory Efficiency** — Bounded caches (max entries + TTL), connection pool limits, candle partitioning (monthly RANGE), max 6 backtest workers, max 4 LLM calls.

## Architecture

- **6 True LLM Agents**: CEO, ContextSynthesizer, NewsIntel, PerformanceAuditor, Scout, StrategyOptimizer
- **11 Shared Python Services**: candle_service, indicator_engine, regime_service, risk_service, position_sizing, pnl_engine, exchange_connector, backtest_service, validation_service, instrument_service, notification_service
- **3 Observer Agents**: CodeEngineer, DBSpecialist, QualityAuditor (scan codebase every 15 min during build)
- **LLM Budget**: ~$195/month (down from $300+)

## Database

- `tickles_shared` (14 tables): instruments, candles, indicator_catalog, indicators, strategies, strategy_dna_strands, strategy_windows, backtest_results, backtest_trade_details, backtest_queue, news_items, derivatives_snapshots, system_config, api_cost_log
- `tickles_[company]` (10 tables): accounts, trades, trade_cost_entries, order_events, trade_validations, balance_snapshots, leverage_history, agent_state, strategy_lifecycle, company_config
- **24 tables total** replacing 80+ legacy tables

## Companies

1. **JarvAIs Trading Co** (START HERE) — Crypto, $500, tickles_jarvais
2. **Capital CFD Co** (WHEN READY) — CFDs, tickles_capital
3. **Explorer/Sandbox** (FUTURE) — Experiments, tickles_explorer

## Build Steps (12 steps in CONTEXT_V3.md)

1. Reconcile naming (tickles_shared/tickles_[company])
2. Database schema DDL (tickles_shared.sql + tickles_company.sql attached)
3. VPS infrastructure
4. Data collection services
5. Indicator engine (250 indicators)
6. Backtest engine
7. Strategy system (DNA strands, windows, conflict resolution)
8. Validation engine (Rule 1)
9. Trading pipeline (CCXT, P&L, fees)
10. AI decision layer
11. Paper trading validation
12. Go live with $500

## Key Files

- **CONTEXT_V3.md** — The definitive blueprint (1400+ lines)
- **db/tickles_shared.sql** — Production DDL with partitioning
- **db/tickles_company.sql** — Template DDL (replace COMPANY_NAME)
- **V2_Build_Bundle.zip** — All reference files from both legacy systems

Last updated: 2026-04-12
"""


if __name__ == "__main__":
    print("=" * 60)
    print("Updating MemClaw workspace: JarvAIs V2.0 -> V3")
    print(f"Workspace: {SHORT_ID}")
    print("=" * 60)

    print("\n[1/6] Updating README with V3 overview...")
    update_readme(README_SUMMARY, README_CONTENT)
    time.sleep(1)

    print("\n[2/6] Reading and uploading CONTEXT_V3.md...")
    context_path = os.path.join(SCRIPT_DIR, "CONTEXT_V3.md")
    with open(context_path, "r", encoding="utf-8") as f:
        v3_content = f.read()

    if len(v3_content) > 50000:
        chunk_size = 45000
        chunk_num = 1
        for i in range(0, len(v3_content), chunk_size):
            chunk = v3_content[i:i+chunk_size]
            title = f"CONTEXT_V3.md (Part {chunk_num})" if len(v3_content) > chunk_size else "CONTEXT_V3.md — Complete Build Blueprint"
            add_doc(title, chunk)
            chunk_num += 1
            time.sleep(1)
    else:
        add_doc("CONTEXT_V3.md — Complete Build Blueprint", v3_content)
    time.sleep(1)

    print("\n[3/6] Reading and uploading tickles_shared.sql...")
    shared_sql_path = os.path.join(SCRIPT_DIR, "tickles_shared.sql")
    with open(shared_sql_path, "r", encoding="utf-8") as f:
        shared_sql = f.read()
    add_doc("tickles_shared.sql — Shared Database DDL (14 tables)", shared_sql)
    time.sleep(1)

    print("\n[4/6] Reading and uploading tickles_company.sql...")
    company_sql_path = os.path.join(SCRIPT_DIR, "tickles_company.sql")
    with open(company_sql_path, "r", encoding="utf-8") as f:
        company_sql = f.read()
    add_doc("tickles_company.sql — Per-Company Database DDL (10 tables)", company_sql)
    time.sleep(1)

    print("\n[5/6] Uploading V2_Build_Bundle.zip...")
    bundle_path = os.path.join(SCRIPT_DIR, "V2_Build_Bundle.zip")
    if os.path.exists(bundle_path):
        upload_file(bundle_path)
    else:
        print(f"  SKIPPED: V2_Build_Bundle.zip not found at {bundle_path}")
    time.sleep(1)

    print("\n[6/6] Updating task list...")
    create_task("CONTEXT_V3.md written (1400+ lines, merged V2+Gemini+schema) -- DONE", 2)
    create_task("tickles_shared.sql DDL (14 tables, partitioned, indexed) -- DONE", 2)
    create_task("tickles_company.sql DDL template (10 tables) -- DONE", 2)
    create_task("V2_Build_Bundle.zip created (131 files) -- DONE", 2)
    create_task("Gemini review verified (13 issues, all incorporated) -- DONE", 2)
    create_task("250 indicators cataloged from both systems -- DONE", 2)
    create_task("Execute tickles_shared.sql on VPS MySQL", 0)
    create_task("Execute tickles_company.sql for tickles_jarvais", 0)
    create_task("Port candle collector with data_hash + retention", 0)
    create_task("Port indicator engine with IndicatorResult interface", 0)
    create_task("Build confluence evaluation service", 0)
    create_task("Port backtest engine to V2 schema", 0)
    create_task("Build unified P&L engine with fee ledger", 0)
    create_task("Build Rule 1 validation engine", 0)
    create_task("Port CCXT execution with waterfall routing", 0)
    create_task("Build outbound notification service (Telegram)", 0)

    print("\n" + "=" * 60)
    print("DONE! MemClaw workspace updated to V3.")
    print(f"View: https://felo.ai/livedoc/{SHORT_ID}?from=claw")
    print("=" * 60)
