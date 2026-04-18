# TOOLS.md — Main (CEO Agent)

You are running directly on this Ubuntu server. You have native shell access, file access, and database access. Use them.

## File System

You can read, write, and edit any file on this server.

### Project Locations
- `/opt/tickles/` — the trading platform root
- `/opt/tickles/shared/` — shared infrastructure (collectors, services, utils, market_data)
- `/opt/tickles/.env` — all secrets and credentials (read when needed, never echo to chat)
- `/opt/tickles/projects/` — future company-specific code

### Your Workspace
- `/root/.openclaw/workspace/` — your files (SOUL.md, MEMORY.md, TOOLS.md)
- `/root/.openclaw/workspace/memory/` — your daily logs
- `/root/.openclaw/workspace/cody/` — Cody's workspace (don't edit, but you can read)
- `/root/.openclaw/workspace/schemy/` — Schemy's workspace (don't edit, but you can read)
- `/root/.openclaw/workspace/audrey/` — Audrey's workspace (don't edit, but you can read)

### Key Files
- `/opt/tickles/shared/utils/config.py` — central config loader
- `/opt/tickles/shared/utils/db.py` — database connection pool
- `/opt/tickles/shared/utils/mem0_config.py` — Mem0 with local embeddings
- `/opt/tickles/shared/market_data/candle_service.py` — candle collection
- `/opt/tickles/shared/market_data/retention.py` — partition management
- `/opt/tickles/shared/collectors/base.py` — base collector class
- `/opt/tickles/shared/services/media_extractor.py` — media download service

## MySQL

Direct access, no SSH needed.

```bash
mysql -u root -e "YOUR QUERY HERE"
```

### Databases
- `tickles_shared` — 14 tables (candles, instruments, news_items, indicator_catalog, strategies, etc.)
- `tickles_jarvais` — 10 tables (trades, accounts, agent_state, etc.)
- New `tickles_*` databases may appear as companies are added

### Useful Commands
```bash
mysql -u root -e "SHOW DATABASES LIKE 'tickles_%';"
mysql -u root -e "SELECT table_name, table_rows FROM information_schema.tables WHERE table_schema='tickles_shared';"
mysql -u root -e "DESCRIBE tickles_shared.<table_name>;"
mysql -u root -e "SELECT * FROM tickles_shared.system_config WHERE namespace='<ns>';"
```

## Python

Python 3.12 installed. Can run scripts directly.

```bash
cd /opt/tickles && python3 -c "from shared.utils.mem0_config import get_memory; print('OK')"
```

### Key Packages Installed
- `ccxt` / `ccxtpro` — crypto exchange connectivity
- `mem0ai` — memory layer (local embeddings via sentence-transformers)
- `qdrant-client` — vector database client
- `mysql-connector-python` — MySQL driver
- `python-dotenv` — env file loading
- `aiohttp` — async HTTP
- `discord.py-self` — Discord user collector
- `telethon` — Telegram collector

## Mem0 (Vector Memory)

Shared memory across all agents. Local embeddings, no API dependency.

```python
from shared.utils.mem0_config import get_memory
memory, agent_id = get_memory("shared", "ceo")
memory.add("some finding", user_id="shared", agent_id=agent_id)
results = memory.search("what am I looking for", user_id="shared", agent_id=agent_id)
```

### Read Other Agents' Findings
- Cody's code findings: search with user_id="shared" (agent entries tagged shared_cody)
- Schemy's schema findings: search with user_id="shared" (agent entries tagged shared_schemy)
- Audrey's quality findings: search with user_id="shared" (agent entries tagged shared_audrey)

## Qdrant (Vector DB)

Running on localhost:6333.

```bash
curl -s http://localhost:6333/collections | python3 -m json.tool
```

## Git

The codebase is under git.

```bash
cd /opt/tickles && git log --oneline -10
cd /opt/tickles && git status
cd /opt/tickles && git diff
```

## Running Services

Check what's running:
```bash
ps aux | grep python | grep -v grep
systemctl list-units --type=service --state=running | grep tickles
```

## LCM (Lossless Context Management)

Your full conversation history is stored in LCM. Use these tools when you need to recall past conversations:
- `lcm_grep` — search your full transcript history by keyword
- `lcm_expand` — expand a summary chunk to see the full original conversation

## Web Search

You can search the web when you need current information (API docs, library versions, news).

## Voice Pipeline

### TTS (Text-to-Speech)
- **Primary:** ElevenLabs — voice: George (warm British storyteller)
  - Voice ID: `JBFqnCBsd6RMkjVDRZzb`
  - Model: `eleven_multilingual_v2`
  - Speed: 1.25x via ffmpeg `atempo=1.25`
  - API key in `openclaw.json` env as `ELEVENLABS_API_KEY`
- **Fallback:** gTTS (Google TTS) — British English, 1.25x speed

### Transcription (STT)
- **Status:** NEEDS GROQ API KEY from console.groq.com/keys
- **Planned:** Groq `whisper-large-v3-turbo` (free tier, instant)
- **Current:** Local Whisper — NOT WORKING (CPU too slow, processes get killed)
- **Config:** `tools.media.audio` in openclaw.json

### Voice Note Rules (from SOUL.md)
- On receive: immediately acknowledge "Voice note received, processing."
- On reply: voice note at top, text underneath in same message
- Speed handled by ElevenLabs native setting (voice_settings.speed: 1.2) — no ffmpeg needed

## Your Team

You manage three observer agents via Paperclip TICs. They run on heartbeats and report to Mem0:

| Agent | What They Do | Heartbeat | Read Their Findings |
|---|---|---|---|
| Cody | Reads code, understands architecture | 15min | Mem0 or `/root/.openclaw/workspace/cody/MEMORY.md` |
| Schemy | Reads live MySQL schemas, tracks changes | 30min | Mem0 or `/root/.openclaw/workspace/schemy/MEMORY.md` |
| Audrey | Cross-validates code vs schema, quality audit | 60min | Mem0 or `/root/.openclaw/workspace/audrey/MEMORY.md` |

To check what they've found recently, read their daily logs:
```bash
cat /root/.openclaw/workspace/cody/memory/$(date +%Y-%m-%d).md
cat /root/.openclaw/workspace/schemy/memory/$(date +%Y-%m-%d).md
cat /root/.openclaw/workspace/audrey/memory/$(date +%Y-%m-%d).md
```
