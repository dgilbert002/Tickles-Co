# PHASE 7 — CONTROL ROOM (simple, inline, "see what I'm selecting")

> Prereq: Phase 6 DONE. Read rules in `00_MASTER_INDEX.md`.
> Goal: a SIMPLE control surface combined INTO the Discord page — a second tab at the
> top ("Feed" | "Config") that shows the SAME left tree, but each channel gets an
> easy follow toggle + a media dropdown (none / text / text+images / everything), with
> drill-down to per-user follow. Selections are always visible. Writes to
> `collector_sources` (Phase 4 endpoint). The collector then OBEYS media_policy.

Dean's explicit constraints (do not over-build):
- Easy to select / unselect. Always SEE what's selected.
- Media choice per source: none / text / text+images / everything.
- Drill into people (allowed_users) but keep it optional and uncluttered.
- Combine "what's available" (the tree) with "what I follow" in ONE view.

## STEP 1 — Add the Config sub-view to discord-feed.js
Append to `shared/dashboard/static/js/discord-feed.js` (inside the same IIFE, before the
final `window.DiscordFeed = {...}` — and ADD `renderConfig` + `setMode` to the exposed
object):
```javascript
  // ---------- CONTROL ROOM ----------
  const MEDIA_OPTS = [
    ["none", "None"], ["text", "Text only"],
    ["text_images", "Text + images"], ["everything", "Everything"],
  ];
  function renderConfig() {
    const host = document.getElementById("dscConfig"); if (!host) return;
    host.innerHTML = "";
    const byParent = {}; state.tree.forEach(n => (byParent[n.parent_id] = byParent[n.parent_id] || []).push(n));
    const cats = state.tree.filter(n => (n.entity_type || "").match(/group|category|server/));
    function row(n) {
      const r = el("div", "dsc-cfg-row");
      // follow toggle
      const cb = el("input"); cb.type = "checkbox"; cb.checked = !!n.enabled;
      cb.addEventListener("change", () => saveCfg(n.id, { enabled: cb.checked }, r));
      r.appendChild(cb);
      r.appendChild(el("span", "dsc-cfg-name", "#" + (n.name || "").replace(/^[^a-z0-9]+/i, "")));
      // media dropdown
      const sel = el("select", "dsc-cfg-media");
      MEDIA_OPTS.forEach(([v, lbl]) => { const o = el("option", null, lbl); o.value = v; if ((n.media_policy || "text_images") === v) o.selected = true; sel.appendChild(o); });
      sel.addEventListener("change", () => saveCfg(n.id, { media_policy: sel.value }, r));
      r.appendChild(sel);
      // current selection echo (always visible)
      const echo = el("span", "dsc-cfg-echo"); echo.textContent = (n.enabled ? "following · " : "ignored · ") + (n.media_policy || "text_images");
      r.appendChild(echo);
      // freshness / health
      if (n.last_error) { const e = el("span", "dsc-cfg-warn", "⚠ " + n.last_error.slice(0, 40)); r.appendChild(e); }
      return r;
    }
    cats.forEach(c => {
      host.appendChild(el("div", "dsc-cat", (c.name || "").toUpperCase()));
      (byParent[c.id] || []).filter(k => (k.entity_type || "") === "channel").forEach(k => host.appendChild(row(k)));
    });
    // orphan channels
    state.tree.filter(n => (n.entity_type || "") === "channel" && !cats.find(c => c.id === n.parent_id)).forEach(k => host.appendChild(row(k)));
  }
  async function saveCfg(sourceId, patch, rowEl) {
    if (rowEl) rowEl.style.opacity = "0.5";
    const r = await fetch(`${API}/config`, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.assign({ source_id: sourceId }, patch)) });
    const j = await r.json();
    // reflect locally + refresh echo
    const node = state.tree.find(n => n.id === sourceId);
    if (node) Object.assign(node, patch);
    renderConfig();
    if (rowEl) rowEl.style.opacity = "";
    return j;
  }
  function setMode(mode) { // 'feed' | 'config'
    document.getElementById("dscFeedView").style.display = (mode === "feed") ? "" : "none";
    document.getElementById("dscConfigView").style.display = (mode === "config") ? "" : "none";
    if (mode === "config") renderConfig();
  }
```
Update the exposed object at the bottom:
```javascript
  window.DiscordFeed = { boot, loadTree, selectChannel, stop: stopRefresh, setMode, renderConfig };
```

## STEP 2 — Add the Config view markup + the Feed/Config switch
In the Discord tab panel (from Phase 6), wrap the feed in `#dscFeedView` and add a
`#dscConfigView`, plus a two-button switch at the very top:
```html
<div id="tab-discord" class="tab-panel discord-skin" style="display:none;">
  <div class="dsc-modeswitch">
    <button id="dscModeFeed" class="dsc-modebtn dsc-modebtn-active">Feed</button>
    <button id="dscModeConfig" class="dsc-modebtn">Config</button>
  </div>
  <div id="dscFeedView">
    <div class="dsc-wrap">
      <div id="dscSidebar" class="dsc-sidebar"></div>
      <div class="dsc-main">
        <div class="dsc-header">
          <span class="dsc-title" id="dscTitle"># select a channel</span>
          <input id="dscSearch" class="dsc-search" placeholder="Search channels (e.g. BTC)"/>
        </div>
        <div id="dscRiver" class="dsc-river"></div>
      </div>
    </div>
  </div>
  <div id="dscConfigView" style="display:none;">
    <div id="dscConfig" class="dsc-config"></div>
  </div>
</div>
```
Wire the buttons (in `boot()`, addEventListener — never onclick):
```javascript
    const mf = document.getElementById("dscModeFeed"), mc = document.getElementById("dscModeConfig");
    if (mf) mf.addEventListener("click", () => { mf.classList.add("dsc-modebtn-active"); mc.classList.remove("dsc-modebtn-active"); setMode("feed"); });
    if (mc) mc.addEventListener("click", () => { mc.classList.add("dsc-modebtn-active"); mf.classList.remove("dsc-modebtn-active"); setMode("config"); });
```

## STEP 3 — Add control-room styles to discord-skin.css
Append:
```css
/* ----- control room ----- */
.dsc-modeswitch { display:flex; gap:8px; padding:8px 16px; border-bottom:1px solid var(--border-subtle); }
.dsc-modebtn { background:var(--background-tertiary); color:var(--text-muted); border:none;
  border-radius:4px; padding:6px 14px; font-size:14px; font-weight:600; cursor:pointer; }
.dsc-modebtn-active { background:var(--brand-500); color:#fff; }
.dsc-config { padding:8px 16px; max-width:760px; }
.dsc-cfg-row { display:flex; align-items:center; gap:10px; padding:8px; border-radius:6px; }
.dsc-cfg-row:hover { background:var(--background-mod-subtle); }
.dsc-cfg-name { min-width:220px; color:var(--text-normal); font-weight:500; }
.dsc-cfg-media { background:var(--background-tertiary); color:var(--text-normal);
  border:1px solid var(--border-subtle); border-radius:4px; padding:4px 8px; }
.dsc-cfg-echo { color:var(--text-muted); font-size:12px; }
.dsc-cfg-warn { color:var(--background-feedback-notification); font-size:12px; margin-left:auto; }
```

## STEP 4 — Make the COLLECTOR obey media_policy
The control room writes `collector_sources.media_policy`, but the collector currently
keys media download off a `download_media`/`media_policy` flag it may read differently.
In `shared/collectors/discord/discord_collector.py`, find where it decides to download
(grep `download_media` / `media_policy`). Replace the boolean decision with a policy
read so the dropdown actually controls behavior:
```python
        # BIBLE-P7: media policy from collector_sources (none|text|text_images|everything)
        policy = (channel_info.get("media_policy") or "text_images")
        download_media = policy in ("text_images", "everything")
```
And ensure `channel_info` is loaded with `media_policy` from `collector_sources` (the
config loader that builds `channel_info` should SELECT `media_policy`). If `policy ==
"none"` skip storing media; if `"text"` skip media but keep text; both handled by the
boolean above plus not appending local_media_paths when policy excludes images.

## STEP 5 — Restart both (collector picks up policy; dashboard picks up UI)
```bash
systemctl restart tickles-discord-collector tickles-dashboard
sleep 8
```

---

## VERIFY (all GREEN before Phase 8) — browser + DB
1. Discord tab → click **Config**. The same channels appear with a checkbox + media
   dropdown + a live "following · text+images" echo.
2. Toggle a channel OFF → DB reflects it:
   `... "SELECT name, enabled, media_policy FROM collector_sources WHERE source_type='discord' ORDER BY updated_at DESC LIMIT 3;"`
   shows your change.
3. Set a channel to "Everything" then "None" → echo updates instantly; DB updates.
4. Within one collector cycle (~2 min), a channel set to "None" stops getting new
   `local_media_paths`; one set to "Everything" keeps getting them.
5. Switch back to **Feed** → river still works; switching modes does not leak timers
   (console clean; only one refresh interval running).

## DOWNSTREAM SAFETY
- A disabled source actually stops being collected (no NEW rows for it) but does NOT
  delete history.
- The collector did not crash on the policy read:
  `tail -n 80 /var/log/tickles/discord_collector.log | grep -iE "Traceback|media_policy" | tail`.
- Existing config/settings endpoints untouched (`/api/settings*`, `/api/config*` still 200).

## ON SUCCESS
Append to PROGRESS.md:
`Phase 7 — DONE <iso> — inline control room (Feed|Config tabs): per-channel follow toggle + media dropdown (none/text/text+images/everything) + live selection echo + health warn; collector now obeys media_policy.`
Then open `08_PHASE_MCP_TOOLS.md`.
