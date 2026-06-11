# PHASE 3 — REPLY-CHAIN + CLICKABLE LOCAL MEDIA CAPTURE

> Prereq: Phase 2 DONE (columns exist). Read rules in `00_MASTER_INDEX.md`.
> Goal: persist reply-chain fields and local media paths into the new columns so the
> feed can show "replying to @X" on top and open images fullscreen on click without
> re-hitting Discord's CDN (which expires/needs auth).

Back up the two collectors again if you didn't keep the .bak-bible from Phase 1:
```bash
cp /opt/tickles/shared/collectors/discord/discord_collector.py   /opt/tickles/shared/collectors/discord/discord_collector.py.bak-bible-p3
cp /opt/tickles/shared/collectors/telegram/telegram_collector.py /opt/tickles/shared/collectors/telegram/telegram_collector.py.bak-bible-p3
```

---

## CONTEXT (what already works — do not rebuild)
- Discord collector ALREADY parses reply data in `_build_msg_data` (lines ~703–709):
  it reads `msg.reference.resolved` into `reply_to_msg_id/reply_to_content/reply_to_author`.
- Discord ALREADY downloads media via `_download_attachment` (line 346) and
  `_download_media_for_messages` (line 375), saving to `MEDIA_BASE_DIR` and adding a
  `media_path` (or `local_media_paths`) key to each message dict in-memory.
- **The gap:** these in-memory fields are NOT written to the new DB columns. The DB
  insert (`_upsert`/`store_news_item` path) doesn't include them yet.
- Telegram collector does NOT capture replies at all (Phase 1 ground truth). We add it.

## STEP 1 — Find the Discord DB-insert call and add the new fields
Locate where Discord builds the row it inserts into `news_items`. Search:
```bash
search_files pattern="reply_to_msg_id|store_news_item|INSERT INTO news_items|_upsert_news_item|local_media_paths" path=/opt/tickles/shared/collectors/discord/discord_collector.py
```
You will find a NewsItem/dict assembly (around lines 226–322 there is a group→item
mapping that already references `reply_to_*`). Ensure the object that is persisted
carries these keys mapped to the new columns. The insert helper (whatever it is —
`store_news_item`, `_upsert_news_item`, or a NewsItem dataclass written by
`shared/utils/...`) must include:
- `reply_to_msg_id`  ← group["reply_to_msg_id"]
- `reply_to_author`  ← group["reply_to_author"]
- `reply_to_content` ← group["reply_to_content"]
- `local_media_paths`← JSON array of the saved relative paths (from media_path keys)
- `author_role_color`← if available from msg.author.color (Discord gives an int → hex);
  else leave NULL.

**If the insert is a parametrised SQL string**, add the four/five columns + bind params.
**If it's a dataclass** (e.g. `NewsItem(...)`), add the fields to the dataclass and to
its INSERT mapper. Use the EXISTING insert mechanism — do not write a parallel insert.

Concrete: if you find a NewsItem construction like
```python
        item = NewsItem(
            hash_key=...,
            source="discord",
            ...
        )
```
add, alongside the existing kwargs (match indentation):
```python
            reply_to_msg_id=group.get("reply_to_msg_id"),
            reply_to_author=(group.get("reply_to_author") or "")[:255],
            reply_to_content=(group.get("reply_to_content") or "")[:2000] or None,
            local_media_paths=group.get("local_media_paths") or [],
            author_role_color=group.get("author_role_color"),
```
And ensure the NewsItem→DB writer includes those columns. If the writer lives in
`shared/utils/` (shared by both collectors), update it ONCE there and both collectors
benefit. Grep for the writer:
```bash
search_files pattern="INSERT INTO news_items|def store_news_item|def insert_news_item|def upsert" path=/opt/tickles/shared
```

## STEP 2 — Capture author role color in Discord `_build_msg_data`
File: `shared/collectors/discord/discord_collector.py`, in `_build_msg_data` (line 669).
After the author lines (`author_name = ...`), add:
```python
        # BIBLE-P3: Discord role color → hex for role-colored usernames in the feed.
        # msg.author.color is a discord.Colour; .value==0 means "no role color".
        author_role_color = None
        try:
            col = getattr(msg.author, "color", None)
            if col is not None and getattr(col, "value", 0):
                author_role_color = f"#{col.value:06x}"
        except Exception:
            author_role_color = None
```
Then add `"author_role_color": author_role_color,` into the returned `msg_data` dict,
and carry it through `_group_messages` into the group dict (use the FIRST message's
color for the group, since grouped messages share an author).

## STEP 3 — Persist local media paths as a JSON array
In `_download_media_for_messages` (line 375), it currently sets a per-message
`media_path`. Ensure each message ALSO gets a list field the group can collect:
```python
        # BIBLE-P3: accumulate relative paths so the feed can load them locally.
        msg.setdefault("local_media_paths", [])
        if saved_relative_path:  # whatever var holds the saved path in this function
            msg["local_media_paths"].append(saved_relative_path)
```
Store RELATIVE paths (relative to MEDIA_BASE_DIR) so the API (Phase 4) can serve them.
In `_group_messages`, merge each message's `local_media_paths` into the group's list.

## STEP 4 — Telegram reply capture (parity with Discord)
File: `shared/collectors/telegram/telegram_collector.py`. Telethon messages expose
`msg.reply_to` and you can fetch the replied message. In the message-building function
(grep for where it builds the per-message dict — similar to Discord's `_build_msg_data`):
```python
        # BIBLE-P3: Telegram reply-chain capture (parity with Discord).
        reply_to_msg_id = None
        reply_to_author = None
        reply_to_content = None
        try:
            if getattr(msg, "reply_to", None) and getattr(msg.reply_to, "reply_to_msg_id", None):
                reply_to_msg_id = str(msg.reply_to.reply_to_msg_id)
                # Best-effort fetch of the replied message text/author (Telethon):
                replied = await msg.get_reply_message()
                if replied:
                    reply_to_content = (replied.message or "")[:2000] or None
                    sender = await replied.get_sender()
                    if sender:
                        reply_to_author = (getattr(sender, "username", None)
                                           or getattr(sender, "first_name", None) or "")[:255] or None
        except Exception as exc:
            logger.debug("Telegram: reply capture failed: %s", exc)
```
Add `reply_to_msg_id / reply_to_author / reply_to_content` into the Telegram message
dict and carry them to the DB insert (same shared writer as Discord).

## STEP 5 — Keep collector_sources.last_hwm fresh (observability)
Wherever Discord saves the HWM (`_save_hwm` / `_save_high_water_mark`, lines 129/549),
add a best-effort mirror after the system_config write:
```python
        # BIBLE-P3: mirror latest message id into collector_sources for the control room.
        try:
            await pool.execute(
                "UPDATE collector_sources SET platform_config = "
                "COALESCE(platform_config,'{}'::jsonb) || jsonb_build_object('last_hwm', $2::text), "
                "last_collected_at = now() "
                "WHERE source_type='discord' AND platform_id = $1",
                str(channel_id), str(last_msg_id),
            )
        except Exception as exc:
            logger.debug("Discord: last_hwm mirror failed for %s: %s", channel_id, exc)
```

## STEP 6 — Restart and generate fresh rows
```bash
systemctl restart tickles-discord-collector tickles-telegram-collector
sleep 30
```

---

## VERIFY (all GREEN before Phase 4)
```bash
echo "=== fresh discord rows carry reply + media columns where applicable ==="
PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared -tAF'|' -c "
SELECT id, (reply_to_msg_id IS NOT NULL) AS has_reply,
       jsonb_array_length(COALESCE(local_media_paths,'[]'::jsonb)) AS media_n,
       (author_role_color IS NOT NULL) AS has_color
FROM news_items WHERE source='discord' ORDER BY id DESC LIMIT 10;"
echo "   ^ at least SOME recent rows should show has_reply=t and/or media_n>0"

echo "=== media files actually exist on disk ==="
PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared -tAc "
SELECT local_media_paths FROM news_items
WHERE source='discord' AND jsonb_array_length(COALESCE(local_media_paths,'[]'::jsonb))>0
ORDER BY id DESC LIMIT 1;"
echo "   ^ take a path from that array and confirm the file exists under MEDIA_BASE_DIR"

echo "=== telegram replies now captured ==="
PGPASSWORD=Tickles21! psql -h 127.0.0.1 -U admin -d tickles_shared -tAc "
SELECT count(*) FROM news_items WHERE source='telegram' AND reply_to_msg_id IS NOT NULL
AND collected_at > now() - interval '1 hour';"
echo "   ^ >0 if any Telegram replies arrived this hour"
```

## DOWNSTREAM SAFETY
- Inserts still succeed (no column/param-count mismatch):
  `... "SELECT count(*) FROM news_items WHERE collected_at > now() - interval '5 minutes';"` > 0.
- Interpretation service unaffected: `systemctl is-active tickles-interpretation` = active,
  and pending count still trends up.
- No new exception classes in the log:
  `tail -n 200 /var/log/tickles/discord_collector.log | grep -iE "Traceback|Error" | tail`
  — should be empty or only pre-existing/benign warnings.

## ON SUCCESS
Append to PROGRESS.md:
`Phase 3 — DONE <iso> — reply_to_* + local_media_paths + author_role_color now persisted (Discord+Telegram); Telegram reply capture added; collector_sources.last_hwm kept fresh.`
Then open `04_PHASE_API.md`.
