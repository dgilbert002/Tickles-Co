# Step 1: Reconcile Naming — Implementation Plan

> **Status**: Ready for implementation  
> **Blocks**: Everything else (Steps 2-12)  
> **Last updated**: 2026-04-12

---

## Problem Statement

CONTEXT_V3.md Section 16, Step 1 requires: "Update all docs, configs, and VPS files to `tickles_shared` / `tickles_[company]`". A full audit of the `/opt/tickles/` workspace reveals that most naming is already correct, but several files have inconsistencies, security violations, or outdated duplicates that must be resolved before proceeding to Step 2 (Database Schema DDL).

---

## Audit Results

### ✅ Already Correct (no changes needed)

| File | Status |
|------|--------|
| `CLAUDE.md` | Uses `tickles_shared` / `tickles_[company]` throughout |
| `shared/migration/tickles_shared.sql` | Correct naming, 14 tables |
| `shared/migration/tickles_company.sql` | Correct naming, 10 tables, `COMPANY_NAME` placeholder |
| `shared/utils/mem0_config.py` | Uses `tickles_{company}` collection naming |
| `.roo/rules/global.md` | Mentions `tickles_shared` |
| `.roo/rules-architect/rules.md` | Clean, no old naming |
| `shared/reference/reference/jarvais_v1/` | LEGACY — preserved for porting reference |
| `shared/reference/reference/capital2/` | LEGACY — preserved for porting reference |

### ❌ Issues Found (6 items)

---

### Issue 1: `new-project.sh` — Uses toy schema instead of V2 DDL

**File**: `/opt/tickles/new-project.sh`  
**Problem**: Creates a minimal `trades` table with 12 columns (no V2 schema at all). The V2 company schema has 10 tables with full column definitions, indexes, FKs, and seed data.  
**Fix**: Rewrite `new-project.sh` to:
1. Accept a company name argument (already does this)
2. Run `shared/migration/tickles_shared.sql` if `tickles_shared` database doesn't exist yet
3. Run `sed 's/COMPANY_NAME/{company}/g' shared/migration/tickles_company.sql | mysql` to create the company database
4. Create the project directory structure under `/opt/tickles/projects/{company}/`
5. Print success message with database names

**Priority**: HIGH — this is the primary tool for creating new company databases.

---

### Issue 2: `shared/reference/v2_build/` — Stale duplicate directory

**Directory**: `/opt/tickles/shared/reference/v2_build/`  
**Contents**:
- `CONTEXT_V3.md` — duplicate of `shared/migration/CONTEXT_V3.md`
- `mem0_bridge.py` — outdated, uses cloud `MemoryClient` instead of self-hosted Qdrant
- `db/tickles_shared.sql` — duplicate of `shared/migration/tickles_shared.sql`
- `db/tickles_company.sql` — duplicate of `shared/migration/tickles_company.sql`

**Problem**: Two copies of the same DDL and blueprint exist. The canonical versions are in `shared/migration/`. The `v2_build` copies may diverge and cause confusion.  
**Fix**: Delete the entire `shared/reference/v2_build/` directory. The canonical files are in `shared/migration/`.

**Priority**: HIGH — stale duplicates will cause confusion during implementation.

---

### Issue 3: `shared/migration/memclaw_update_v3.py` — Hardcoded API key

**File**: `/opt/tickles/shared/migration/memclaw_update_v3.py`  
**Line 7**: `API_KEY = os.environ.get("FELO_API_KEY") or "fk-yoadxZalJUqEQFwtMfg3lfCPp6wE2R5O5Jytj50mK26lOrkJ"`  
**Problem**: Hardcoded API key fallback violates the global security rule: "Never hardcode API keys, passwords, or tokens in code."  
**Fix**: Remove the hardcoded fallback. Change line 7 to:
```python
API_KEY = os.environ.get("FELO_API_KEY")
if not API_KEY:
    print("[memclaw_update] ERROR: FELO_API_KEY environment variable not set")
    sys.exit(1)
```

**Priority**: HIGH — security violation.

---

### Issue 4: `.roo/skills/schema-designer/SKILL.md` — Inconsistencies with actual SQL

**File**: `/opt/tickles/.roo/skills/schema-designer/SKILL.md`  
**Problems**:
1. Says `VARCHAR(64)` for hashes — actual SQL uses `CHAR(64)` (SHA-256 is always exactly 64 hex chars)
2. Says `BIGINT UNSIGNED NOT NULL AUTO_INCREMENT` — actual SQL uses `BIGINT AUTO_INCREMENT PRIMARY KEY`
3. Missing `updated_at` requirement for mutable tables
4. Says `DECIMAL(10,4)` for percentages — actual SQL uses `DECIMAL(10,6)` for most percentages and `DECIMAL(5,2)` for halt_threshold_pct
5. Missing candle partitioning guidance (Rule 3)
6. Missing multi-tenancy guidance (shared vs per-company databases)

**Fix**: Update SKILL.md to match the actual canonical DDL in `shared/migration/tickles_shared.sql` and `shared/migration/tickles_company.sql`.

**Priority**: MEDIUM — this affects future schema design work by AI agents.

---

### Issue 5: `shared/reference/v2_build/mem0_bridge.py` — Outdated Mem0 pattern

**File**: `/opt/tickles/shared/reference/v2_build/mem0_bridge.py`  
**Problem**: Uses `MemoryClient` (cloud API) with `MEM0_API_KEY`. The canonical implementation in `shared/utils/mem0_config.py` uses self-hosted Qdrant with `ScopedMemory` class.  
**Fix**: This file is deleted as part of Issue 2 (entire `v2_build/` directory removed). No separate fix needed.

**Priority**: Resolved by Issue 2.

---

### Issue 6: `CLAUDE.md` — Needs V2 project structure documentation

**File**: `/opt/tickles/CLAUDE.md`  
**Problem**: CLAUDE.md documents the current infrastructure but doesn't describe the V2 project structure that will be created during implementation. It also doesn't note that `shared/reference/v2_build/` is superseded.  
**Fix**: Add a "V2 Project Structure" section to CLAUDE.md documenting:
1. The canonical file locations (`shared/migration/` for DDL, `shared/utils/` for shared libraries)
2. The `shared/reference/v2_build/` directory is REMOVED (superseded by `shared/migration/`)
3. The V2 codebase directory structure that will be created during Steps 2-12
4. The database naming convention (`tickles_shared` + `tickles_[company]`)

**Priority**: MEDIUM — important for onboarding but not blocking.

---

## Implementation Order

| # | Task | Mode | Files |
|---|------|------|-------|
| 1 | Delete `shared/reference/v2_build/` directory | code | Entire directory |
| 2 | Fix hardcoded API key in `memclaw_update_v3.py` | code | `shared/migration/memclaw_update_v3.py` line 7 |
| 3 | Rewrite `new-project.sh` to use V2 DDL templates | code | `new-project.sh` |
| 4 | Update `schema-designer/SKILL.md` to match actual SQL | code | `.roo/skills/schema-designer/SKILL.md` |
| 5 | Update `CLAUDE.md` with V2 project structure | code | `CLAUDE.md` |

---

## What Could Go Wrong

1. **Deleting v2_build/**: If any other scripts reference `shared/reference/v2_build/` paths, they'll break. Search found no such references — safe to delete.

2. **new-project.sh rewrite**: The script currently creates a toy `trades` table. If any existing project databases were created with this script, they have the old schema. Those projects (e.g., `projects/jarvais/`) should be recreated with the new DDL. Check if `tickles_jarvais` database already exists on MySQL.

3. **memclaw_update_v3.py**: Removing the hardcoded key means the script won't run without `FELO_API_KEY` set. This is correct behavior — API keys must come from environment variables.

4. **SKILL.md update**: Must be careful to match the ACTUAL DDL exactly. Any deviation will cause future schema designs to be inconsistent.

---

## Verification Checklist

After implementation, verify:
- [ ] `shared/reference/v2_build/` no longer exists
- [ ] `shared/migration/memclaw_update_v3.py` has no hardcoded API key
- [ ] `new-project.sh` creates both `tickles_shared` and `tickles_[company]` databases with full V2 schema
- [ ] `.roo/skills/schema-designer/SKILL.md` matches actual DDL types (CHAR not VARCHAR for hashes, etc.)
- [ ] `CLAUDE.md` documents V2 project structure and canonical file locations
- [ ] No file in `/opt/tickles/` (outside `shared/reference/reference/`) contains `jarvais_shared`, `capital2_`, `apex_`, or `ea_` as V2 naming