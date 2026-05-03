# Environment Variable Reference — Tickles & Co Intelligence Pipeline

> **Last updated:** 2026-04-30  
> **Scope:** Phases 0–7 of the Intelligence Unified Plan  
> **Restart semantics:** Keys marked with **[AC]** require a **daemon restart** to take effect. They are read at module import time, not on every call.

---

## 1. LLM Gateway (provider-driven)

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `LLM_GATEWAY_DEFAULT` | `gateway_config.py` | `openrouter` | Active provider: `openrouter` or `requesty` | **[AC]** |
| `LLM_GATEWAY_INTERPRETATION` | `gateway_config.py` | *(inherits DEFAULT)* | Per-service override for interpretation | **[AC]** |
| `LLM_GATEWAY_MCP` | `gateway_config.py` | *(inherits DEFAULT)* | Per-service override for MCP tools | **[AC]** |
| `LLM_GATEWAY_GURU` | `gateway_config.py` | *(inherits DEFAULT)* | Per-service override for guru | **[AC]** |
| `LLM_GATEWAY_VISION` | `gateway_config.py` | *(inherits DEFAULT)* | Per-service override for vision pipeline | **[AC]** |
| `LLM_GATEWAY_PREFILTER` | `gateway_config.py` | *(inherits DEFAULT)* | Per-service override for pre-filter | **[AC]** |
| `LLM_GATEWAY_TEXT_EXTRACT` | `gateway_config.py` | *(inherits DEFAULT)* | Per-service override for text extraction | **[AC]** |
| `LLM_GATEWAY_POSTMORTEM` | `gateway_config.py` | *(inherits DEFAULT)* | Per-service override for post-mortem | **[AC]** |

### 1a. OpenRouter block (active when `LLM_GATEWAY_DEFAULT=openrouter`)

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `OPENROUTER_API_KEY` | `gateway_config.py` | *(required)* | API key for OpenRouter | **[AC]** |
| `OPENROUTER_BASE_URL` | `gateway_config.py` | `https://openrouter.ai/api/v1` | OpenRouter API base URL | **[AC]** |
| `OPENROUTER_DEFAULT_MODEL` | `gateway_config.py` | `anthropic/claude-sonnet-4` | Default primary model | **[AC]** |
| `OPENROUTER_FALLBACK_MODEL` | `gateway_config.py` | `google/gemini-2.0-flash-001` | Fallback model | **[AC]** |
| `OPENROUTER_TEMPERATURE` | `gateway_config.py` | `0.1` | Default temperature | **[AC]** |

### 1b. Requesty block (active when `LLM_GATEWAY_DEFAULT=requesty`)

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `REQUESTY_API` | `gateway_config.py` | *(required)* | **Canonical** API key for Requesty | **[AC]** |
| `REQUESTY_API_KEY` | `gateway_config.py` | *(alias of REQUESTY_API)* | Plan alias — recognised by gateway | **[AC]** |
| `REQUESTY_BASE_URL` | `gateway_config.py` | `https://router.requesty.ai/v1` | Requesty router URL (was `api.requesty.ai`) | **[AC]** |
| `TICKLES_APP_REQUESTY_URL` | `gateway_config.py` | *(alias of REQUESTY_BASE_URL)* | Legacy alias | **[AC]** |
| `REQUESTY_DEFAULT_MODEL` | `gateway_config.py` | `tickles-vision` | Default primary model | **[AC]** |
| `REQUESTY_FALLBACK_MODEL` | `gateway_config.py` | `google/gemini-2.5-flash` | Fallback model | **[AC]** |
| `REQUESTY_TEMPERATURE` | `gateway_config.py` | `0.1` | Default temperature | **[AC]** |

### 1c. Per-service model overrides

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `CHART_HACKER_MODEL_INTERPRETATION_PRIMARY` | `gateway_config.py` | *(inherits)* | Primary model for interpretation | **[AC]** |
| `CHART_HACKER_MODEL_INTERPRETATION_FALLBACK` | `gateway_config.py` | *(inherits)* | Fallback model for interpretation | **[AC]** |
| `CHART_HACKER_MODEL_MCP_PRIMARY` | `gateway_config.py` | *(inherits)* | Primary model for MCP | **[AC]** |
| `CHART_HACKER_MODEL_MCP_FALLBACK` | `gateway_config.py` | *(inherits)* | Fallback model for MCP | **[AC]** |
| `CHART_HACKER_MODEL_PRIMARY` | `gateway_config.py` | *(provider default)* | Global primary model fallback | **[AC]** |
| `CHART_HACKER_MODEL_FALLBACK` | `gateway_config.py` | *(provider default)* | Global fallback model fallback | **[AC]** |
| `TICKLES_APP_VISION_API_MODEL` | `gateway_config.py` | `tickles-vision` | Legacy vision model alias | **[AC]** |
| `TICKLES_APP_PRE_VISION_API_MODEL` | `gateway_config.py` | `google/gemini-2.5-flash` | Legacy pre-filter model alias | **[AC]** |

### 1d. Per-service temperature overrides

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `LLM_TEMPERATURE_INTERPRETATION` | `gateway_config.py` | *(inherits)* | Temperature for interpretation | **[AC]** |
| `LLM_TEMPERATURE_MCP` | `gateway_config.py` | *(inherits)* | Temperature for MCP | **[AC]** |
| `LLM_TEMPERATURE_GURU` | `gateway_config.py` | *(inherits)* | Temperature for guru | **[AC]** |

---

## 2. Cost Logging & Budget (G5)

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `LLM_COST_LOG_ENABLED` | `api_cost_log.py` | `true` | Master switch for cost logging | **[AC]** |
| `API_COST_LOG_ENABLED` | `api_cost_log.py` | `true` | Alias of above | **[AC]** |
| `LLM_BUDGET_USD_DAILY_SOFT` | `budget_guard.py` | `50.0` | Soft warning threshold (non-blocking) | **[AC]** |
| `LLM_BUDGET_USD_DAILY_HARD` | `budget_guard.py` | `200.0` | Hard block threshold (raises BudgetExceededError) | **[AC]** |

> **[AC] Restart-semantics warning:** All `LLM_BUDGET_USD_DAILY_*` keys are read at module import. Changing them in `.env` without restarting the daemon has **no effect**.

### 2a. Loop / burst detection thresholds

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `LLM_LOOP_WINDOW_SECONDS` | `loop_detector.py` | `300` | Lookback window for loop detection | **[AC]** |
| `LLM_LOOP_IDENTICAL_THRESHOLD` | `loop_detector.py` | `3` | Block after N identical calls in window | **[AC]** |
| `LLM_LOOP_SIMILAR_THRESHOLD` | `loop_detector.py` | `5` | Warn after N similar calls in window | **[AC]** |
| `LLM_FREQUENCY_BURST_THRESHOLD` | `loop_detector.py` | `10` | Warn after N calls/minute for same role | **[AC]** |

---

## 3. Database

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `DB_HOST` | `db.py` | `127.0.0.1` | Postgres host | **[AC]** |
| `DB_PORT` | `db.py` | `5432` | Postgres port | **[AC]** |
| `DB_USER` | `db.py` | `admin` | Postgres user | **[AC]** |
| `DB_PASSWORD` | `db.py` | *(required)* | Postgres password | **[AC]** |
| `DB_NAME_SHARED` | `db.py` | `tickles_shared` | Shared database name | **[AC]** |
| `DB_NAME_COMPANY` | `db.py` | `tickles_jarvais` | Default company database name | **[AC]** |

---

## 4. Exchange API Keys

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `BYBIT_API_KEY` | `collectors/ccxt` | *(optional)* | Bybit live API key | **[AC]** |
| `BYBIT_SECRET` | `collectors/ccxt` | *(optional)* | Bybit live secret | **[AC]** |
| `BYBIT_DEMO_API_KEY` | `collectors/ccxt` | *(optional)* | Bybit demo API key | **[AC]** |
| `BYBIT_DEMO_API_SECRET` | `collectors/ccxt` | *(optional)* | Bybit demo secret | **[AC]** |
| `BLOFIN_API_KEY` | `collectors/ccxt` | *(optional)* | Blofin live API key | **[AC]** |
| `BLOFIN_API_SECRET` | `collectors/ccxt` | *(optional)* | Blofin live secret | **[AC]** |
| `BLOFIN_API_PHRASE` | `collectors/ccxt` | *(optional)* | Blofin live passphrase | **[AC]** |
| `BITGET_API_KEY` | `collectors/ccxt` | *(optional)* | Bitget API key | **[AC]** |
| `BITGET_API_SECRET` | `collectors/ccxt` | *(optional)* | Bitget secret | **[AC]** |
| `BITGET_API_PHASE` | `collectors/ccxt` | *(optional)* | Bitget passphrase | **[AC]** |
| `CAPITAL_EMAIL` | `collectors/capital` | *(optional)* | Capital.com email | **[AC]** |
| `CAPITAL_PASSWORD` | `collectors/capital` | *(optional)* | Capital.com password | **[AC]** |
| `CAPITAL_API_KEY` | `collectors/capital` | *(optional)* | Capital.com API key | **[AC]** |

---

## 5. Discord & Telegram Collectors

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `DISCORD_BOT_TOKEN` | `discord_collector.py` | *(optional)* | Discord bot token | **[AC]** |
| `TELEGRAM_API_ID` | `telegram_collector.py` | *(optional)* | Telegram API ID | **[AC]** |
| `TELEGRAM_API_HASH` | `telegram_collector.py` | *(optional)* | Telegram API hash | **[AC]** |
| `TELEGRAM_PHONE` | `telegram_collector.py` | *(optional)* | Telegram phone number | **[AC]** |

---

## 6. Memory Backends

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `MEMU_ENABLED` | `memu/client.py` | `true` | Enable MemU semantic memory | **[AC]** |
| `MEMU_DB_NAME` | `memu/client.py` | `memu` | MemU database name | **[AC]** |
| `FELO_API_KEY` | `mem0_config.py` | *(optional)* | Felo / MemClaw API key | **[AC]** |

---

## 7. Infrastructure

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `REDIS_HOST` | `gateway` | `127.0.0.1` | Redis host | **[AC]** |
| `REDIS_PORT` | `gateway` | `6379` | Redis port | **[AC]** |
| `CH_HOST` | `clickhouse` | `127.0.0.1` | ClickHouse host | **[AC]** |
| `CH_PORT` | `clickhouse` | `9000` | ClickHouse port | **[AC]** |
| `CH_USER` | `clickhouse` | `admin` | ClickHouse user | **[AC]** |
| `CH_PASSWORD` | `clickhouse` | *(required)* | ClickHouse password | **[AC]** |
| `CH_DATABASE` | `clickhouse` | `backtests` | ClickHouse database | **[AC]** |

---

## 8. Signal Review & Manage Panel

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `SIGNAL_REVIEW_LOOKBACK_H` | `signal_review_export.py` | `24` | Hours of history to export | **[AC]** |
| `SIGNAL_REVIEW_REPORT_DIR` | `signal_review_export.py` | `shared/reports/signal_review` | Output directory | **[AC]** |
| `SIGNAL_REVIEW_PUBLIC_BASE_URL` | `signal_review_export.py` | *(required)* | Public URL base | **[AC]** |
| `TICKLES_TUI_READONLY` | `tui_manager.py` | `1` | TUI read-only gate (1=readonly) | **[AC]** |
| `MANAGE_PANEL_PUBLIC_BASE_URL` | `manage_panel` | *(required)* | Manage panel public URL | **[AC]** |
| `MANAGE_RATE_READ` | `rate_limit.py` | `600` | Read rate limit (req/min) | **[AC]** |
| `MANAGE_RATE_WRITE` | `rate_limit.py` | `30` | Write rate limit (req/min) | **[AC]** |

---

## 9. Phase 6 — Reason Embedding & Tag Clustering

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `REASON_EMBED_MODEL` | `embed.py` | `sentence-transformers/all-MiniLM-L6-v2` | Sentence-transformer model | **[AC]** |
| `REASON_EMBED_DIM` | `embed.py` | `384` | Embedding dimension | **[AC]** |
| `TAG_CLUSTER_COSINE_THRESHOLD` | `tag_normaliser.py` | `0.78` | Tag cluster merge threshold | **[AC]** |
| `TAG_BORDERLINE_BAND` | `tag_normaliser.py` | `0.05` | Borderline band for human review | **[AC]** |

---

## 10. Phase 7 — Post-Mortem

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `SIGNAL_POSTMORTEM_MODEL` | `postmortem_service.py` | `openrouter/openai/gpt-4.1` | LLM for post-mortem analysis | **[AC]** |

---

## 11. Payload Storage

| Key | Owner | Default | Description | Restart |
|-----|-------|---------|-------------|---------|
| `PAYLOAD_BASE_DIR` | `payload_store.py` | `/opt/tickles/shared/reports/signal_payloads` | Raw JSON storage path | **[AC]** |
| `PAYLOAD_MAX_BYTES` | `payload_store.py` | `5242880` (5 MB) | Max payload size per file | **[AC]** |
| `PAYLOAD_RETENTION_COMPRESS_DAYS` | `payload_retention.py` | `30` | Compress payloads older than N days | **[AC]** |
| `PAYLOAD_RETENTION_DELETE_DAYS` | `payload_retention.py` | `365` | Delete archives older than N days | **[AC]** |
| `PAYLOAD_DISK_BUDGET_GB` | `payload_retention.py` | `50` | Max disk budget for payloads | **[AC]** |

---

## Validation

Run the built-in validator to check your environment:

```bash
python -m shared.utils._validate_env
```

This exits 0 if all required keys for the active provider are present, 1 otherwise.
