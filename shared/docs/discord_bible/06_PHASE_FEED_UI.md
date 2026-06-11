# PHASE 6 — FEED UI (the scrolling Discord-identical river)

> Prereq: Phase 5 DONE (skin served). Read rules in `00_MASTER_INDEX.md`.
> Goal: build the actual page — left tree (server→group→channel, unread white/gray +
> counters, search filter), right river with slow auto-refresh, grouped messages,
> reply-on-top, role-colored usernames, click-image-fullscreen lightbox, and
> click-card-to-expand showing OUR AI analysis. Vanilla JS, no framework, SES-safe
> (addEventListener only — NEVER inline onclick=, per dashboard gotchas).

## CRITICAL DASHBOARD GOTCHAS (from project skill — obey or it silently breaks)
- **SES lockdown strips inline handlers.** Use `el.addEventListener(...)`. NEVER
  `onclick="..."` in HTML strings.
- **Browser cache is stubborn.** When you wire the script tag, use a cache-buster query
  like `?v=bible1` and bump it drastically if you iterate.
- **No IIFE wrapper that hides globals** if the page expects globals; follow how
  `app.js` exposes its entry points.

Back up the page shell before editing:
```bash
# find the main template/index the dashboard serves:
search_files pattern="index.html|handle_index|<div id=\"app\"|tab-" path=/opt/tickles/shared/dashboard
```
The dashboard SPA is driven by `static/app.js` + an index served by `handle_index`
(server.py ~line 918). You will ADD a Discord tab/page — do not replace the app.

## STEP 1 — Create `shared/dashboard/static/js/discord-feed.js`
```javascript
/* BIBLE-P6 — Discord-identical feed. Vanilla JS, SES-safe (addEventListener only). */
(function () {
  "use strict";
  const API = (location.pathname.startsWith("/dashboard") ? "/dashboard" : "") + "/api/discord";
  const state = { source: "discord", channel: null, tree: [], seenTopId: 0, timer: null, filter: "" };

  function el(tag, cls, txt) { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; }
  function esc(s) { return (s == null ? "" : String(s)); }

  // ---------- TREE (left) ----------
  async function loadTree() {
    const r = await fetch(`${API}/tree?source=${state.source}`); const j = await r.json();
    state.tree = j.tree || []; renderTree();
  }
  function renderTree() {
    const side = document.getElementById("dscSidebar"); if (!side) return;
    side.innerHTML = "";
    // group by parent_id: entity_type 'category'/'group' are headers, 'channel' are rows
    const byParent = {}; state.tree.forEach(n => { (byParent[n.parent_id] = byParent[n.parent_id] || []).push(n); });
    const cats = state.tree.filter(n => (n.entity_type || "").match(/group|category|server/));
    const orphanChans = state.tree.filter(n => (n.entity_type || "") === "channel" && !cats.find(c => c.id === n.parent_id));
    function chanRow(n) {
      if (state.filter && !((n.name || "").toLowerCase().includes(state.filter))) return null;
      const row = el("div", "dsc-chan" + (state.channel === n.name ? " dsc-selected" : ""));
      row.appendChild(el("span", "dsc-hash", "#"));
      row.appendChild(el("span", null, (n.name || "").replace(/^[^a-z0-9]+/i, "")));
      // unread/count comes from feed freshness vs last_hwm; simple version: hwm-based dot
      row.style.position = "relative";
      row.addEventListener("click", () => selectChannel(n.name));
      return row;
    }
    cats.forEach(c => {
      const kids = (byParent[c.id] || []).filter(k => (k.entity_type || "") === "channel");
      const visible = kids.map(chanRow).filter(Boolean);
      if (state.filter && visible.length === 0) return;
      side.appendChild(el("div", "dsc-cat", (c.name || "").toUpperCase()));
      visible.forEach(v => side.appendChild(v));
    });
    orphanChans.map(chanRow).filter(Boolean).forEach(v => side.appendChild(v));
  }

  // ---------- FEED (right) ----------
  function selectChannel(name) { state.channel = name; state.seenTopId = 0; renderTree(); loadFeed(true); }
  async function loadFeed(replace) {
    if (!state.channel) return;
    const r = await fetch(`${API}/feed?source=${state.source}&channel=${encodeURIComponent(state.channel)}&limit=100`);
    const j = await r.json(); const items = (j.items || []).slice().reverse(); // oldest→newest for render
    const river = document.getElementById("dscRiver"); if (!river) return;
    if (replace) river.innerHTML = "";
    let prevAuthor = null, prevId = 0;
    items.forEach(it => {
      if (it.id <= state.seenTopId) return;            // only append new on refresh
      const groupStart = it.author !== prevAuthor;
      river.appendChild(renderMsg(it, groupStart, replace ? false : true));
      prevAuthor = it.author; prevId = Math.max(prevId, it.id);
    });
    if (prevId) state.seenTopId = prevId;
    river.scrollTop = river.scrollHeight;               // stick to bottom like Discord
  }
  function renderMsg(it, groupStart, isNew) {
    const wrap = el("div", "dsc-msg" + (groupStart ? " dsc-groupstart" : "") + (isNew ? " dsc-new" : ""));
    // reply-on-top
    if (it.reply_to_msg_id) {
      const rep = el("div", "dsc-reply");
      rep.appendChild(el("span", "dsc-reply-author", "@" + esc(it.reply_to_author || "unknown")));
      rep.appendChild(el("span", "dsc-reply-text", esc(it.reply_to_content || "")));
      wrap.appendChild(rep);
    }
    if (groupStart) {
      const av = el("img", "dsc-avatar"); av.src = avatarFor(it); av.alt = ""; wrap.appendChild(av);
      const hl = el("div", "dsc-header-line");
      const un = el("span", "dsc-username", esc(it.author || "unknown"));
      if (it.author_role_color) un.style.color = it.author_role_color;
      hl.appendChild(un);
      hl.appendChild(el("span", "dsc-timestamp", fmtTime(it.collected_at)));
      wrap.appendChild(hl);
    }
    if (it.content) wrap.appendChild(el("div", "dsc-content", esc(it.content)));
    // attachments → clickable images
    (it.local_media_paths || []).forEach(p => {
      const box = el("div", "dsc-attach");
      const img = el("img"); img.src = `${API}/media/${encodeURI(p)}`; img.loading = "lazy";
      img.addEventListener("click", (e) => { e.stopPropagation(); openLightbox(img.src); });
      box.appendChild(img); wrap.appendChild(box);
    });
    // click row → expand AI analysis
    const ai = el("div", "dsc-ai"); ai.innerHTML = renderAi(it); wrap.appendChild(ai);
    wrap.addEventListener("click", () => wrap.classList.toggle("dsc-expanded"));
    return wrap;
  }
  function avatarFor(it) { // deterministic placeholder if no avatar stored
    const n = (it.author || "?").charCodeAt(0) % 5;
    return `https://cdn.discordapp.com/embed/avatars/${n}.png`;
  }
  function fmtTime(iso) { try { return new Date(iso).toLocaleString(); } catch (e) { return ""; } }
  function renderAi(it) {
    // Use OUR existing analysis (enrichment jsonb). Show reason/confidence + status.
    const enr = it.enrichment || {};
    const parts = [];
    if (it.enrichment_status) parts.push(`<b>Status:</b> ${esc(it.enrichment_status)}`);
    if (enr.summary) parts.push(`<div>${esc(enr.summary)}</div>`);
    if (enr.reason)  parts.push(`<div><b>Reason:</b> ${esc(enr.reason)}</div>`);
    if (it.instruments) parts.push(`<div><b>Symbols:</b> ${esc(JSON.stringify(it.instruments))}</div>`);
    return parts.join("") || "<i>No AI analysis yet.</i>";
  }

  // ---------- LIGHTBOX ----------
  function openLightbox(src) {
    let lb = document.getElementById("dscLightbox");
    if (!lb) { lb = el("div", "dsc-lightbox"); lb.id = "dscLightbox";
      const i = el("img"); lb.appendChild(i);
      lb.addEventListener("click", () => lb.classList.remove("dsc-open"));
      document.body.appendChild(lb); }
    lb.querySelector("img").src = src; lb.classList.add("dsc-open");
  }

  // ---------- SLOW AUTO-REFRESH ----------
  function startRefresh() { stopRefresh(); state.timer = setInterval(() => loadFeed(false), 15000); } // 15s slow river
  function stopRefresh() { if (state.timer) clearInterval(state.timer); state.timer = null; }

  // ---------- BOOT ----------
  function boot() {
    const search = document.getElementById("dscSearch");
    if (search) search.addEventListener("input", () => { state.filter = search.value.trim().toLowerCase(); renderTree(); });
    loadTree(); startRefresh();
  }
  // expose for the SPA tab switch to call
  window.DiscordFeed = { boot, loadTree, selectChannel, stop: stopRefresh };
})();
```

## STEP 2 — Add the page shell + script/style includes
In the SPA index (served by `handle_index`), add a Discord tab panel. Find how other
tabs are declared (grep `tab-` and the panel `<div>`s). Add a panel:
```html
<div id="tab-discord" class="tab-panel discord-skin" style="display:none;">
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
```
Add the includes (with cache-buster) near the other static includes in the index:
```html
<link rel="stylesheet" href="static/css/discord-skin.css?v=bible1">
<script src="static/js/discord-feed.js?v=bible1"></script>
```
Wire the tab button so switching to Discord calls `window.DiscordFeed.boot()` once and
`window.DiscordFeed.stop()` when leaving (match how the app toggles `tab-panel`
display + how it activates other tabs — use addEventListener, not onclick).

## STEP 3 — Restart + cache-bust
```bash
systemctl restart tickles-dashboard
sleep 5
```

---

## VERIFY (all GREEN before Phase 7) — use the browser tool
1. Open `http://127.0.0.1:3101/`, click the Discord tab.
2. Left sidebar shows categories + channels in order; clicking a channel loads its river.
3. A message row shows: avatar (group start only), role-colored username, timestamp,
   content. A reply shows "@author …" ABOVE the message with the curved spine.
4. Click a chart image → it opens FULLSCREEN in the lightbox; click again → closes.
5. Click a message row → the AI-analysis block expands beneath it.
6. Leave the tab ~20s, post (or wait for) a new message; on refresh it fades in at the
   bottom (slow 15s river). Console has NO SES "inline handler" errors:
```
browser_console  (check for errors)
```
7. Type "BTC" in the channel search → sidebar filters to matching channels.

Side-by-side: open real Discord (`discord1.png` as reference) next to the tab; the row
anatomy, spacing, colors, and reply-on-top must match.

## DOWNSTREAM SAFETY
- Other tabs still work (positions, signals, news): click through them — no JS errors,
  no layout break. `browser_console` clean.
- `systemctl is-active tickles-dashboard` = active.

## ON SUCCESS
Append to PROGRESS.md:
`Phase 6 — DONE <iso> — Discord feed tab live: tree+search, grouped river, reply-on-top, role colors, click-image lightbox, click-expand AI, 15s slow refresh. SES-safe.`
Then open `07_PHASE_CONTROL_ROOM.md`.
