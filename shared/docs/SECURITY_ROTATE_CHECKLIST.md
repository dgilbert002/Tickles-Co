# Phase 0 — Security Rotation Checklist

**Owner:** CEO (you). **Why:** plaintext credentials exist on the live VPS
and in a local reference doc. Nothing is publicly leaked (git never tracked
the doc), but if laptop/VPS access ever escaped, these grant real-world
financial/messaging capability. Rotate them at source.

## 1. Telegram bot — HIGHEST URGENCY

**Where found:** `~/.openclaw/openclaw.json` → `channels.telegram.botToken`
(VPS). Token currently begins `8587868857:AA…` (already chmod 600 and
backed up to `~/.openclaw/openclaw.json.phase0bak`).

**Actions:**
1. Open Telegram → `@BotFather` → `/mybots` → select the bot → `API Token`
   → `Revoke current token`.
2. Copy the new token BotFather returns.
3. On the VPS:
   ```bash
   sudo -u root vi ~/.openclaw/openclaw.json     # edit channels.telegram.botToken
   systemctl --user restart openclaw-gateway      # or: pkill -HUP openclaw-gateway
   ```
4. Confirm a test Telegram message still reaches the agent.

## 2. ElevenLabs API key

**Where found:** `openclaw.json` in TWO places:
- `env.ELEVENLABS_API_KEY`
- `messages.tts.providers.elevenlabs.apiKey`
Both hold the same value starting `sk_b7130efa…`.

**Actions:**
1. <https://elevenlabs.io/app/settings/api-keys> → revoke existing key →
   create a new one.
2. Replace in BOTH places in `openclaw.json`, save, restart gateway.

## 3. Felo API key

**Where found:** `openclaw.json` → `env.FELO_API_KEY` (prefix `fk-yoadx…`).

**Actions:**
1. Log into Felo dashboard → rotate key → update `openclaw.json`.

## 4. OpenClaw gateway auth token

**Where found:** `openclaw.json` → `gateway.auth.token`
(48-hex starting `dde36a…`). Anyone on your LAN / Tailscale with this token
can drive the gateway.

**Actions:**
1. Generate a new 48-hex token:
   ```bash
   openssl rand -hex 24
   ```
2. Replace `gateway.auth.token`, save, restart openclaw.
3. Update any Paperclip or external client that references the old token.

## 5. Aster DEX keys (4 accounts)

**Where found:** `shared/docs/OpenRouter_Chats.md` (now gitignored).
Four `aster_api_key` / `aster_api_secret` pairs beginning at line 6168.

**Actions:**
1. Log into each of the 4 Aster sub-accounts → API Management → revoke.
2. If any were real, issue new keys and drop them into the VPS env file only
   (e.g. `/root/.openclaw/secrets.env`, chmod 600). Do NOT paste into chat
   logs, docs, or `openclaw.json`.

## 6. Paperclip agent JWT secret

**Where found:** `/home/paperclip/.paperclip/instances/default/.env` on VPS.
Not leaked, but will be referenced by `tickles-cost-shipper` (Phase 1) and
by Tickles MCP (Phase 2) → keep chmod 600 and back up before edits.

## Repository guards (already in place)

- `.gitignore` now excludes `_audit_*.txt`, `_audit_*.md`,
  `OpenRouter_Chats.md`, `OpenClaw Trading Systems.txt`,
  `Top wallets capture 86% of the move.txt`, `PHASE_39_DRILL*.json`.
- Verified: none of those paths has ever been tracked by git.

## Done when

- [ ] Telegram token rotated and working
- [ ] ElevenLabs key rotated (both fields updated)
- [ ] Felo key rotated
- [ ] Gateway auth token rotated
- [ ] Aster keys revoked (rotate only if any were live)
- [ ] Paperclip JWT secret confirmed chmod 600
