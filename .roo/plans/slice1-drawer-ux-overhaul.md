# Slice 1 — Drawer UX Overhaul (PLAN, REVISED)

> Status: PLAN phase, post devil's-advocate revision. No code yet. This document is the spec the IMPLEMENT phase will follow.
> Scope: Make the interpretation drawer wide, render external chart images, show ALL media for a news_item, format `chart_hacker_trades` as a table, and stop empty drawers on chart-only news_items.
> Out of scope (deferred): always-on quant pipeline reorder (Slice 2), position tracking (Slice 3), feed hygiene (Slice 4).
> Revision notes: 17 changes from the devil's-advocate review have been folded in. Each edit is annotated with `(REVISED per critique #N)` so the orchestrator can verify them.

---

## 1. Problem Statement

The interpretation drawer is the primary inspection surface for traders' signals on the dashboard. Today it has six concrete defects:

1. **Drawer too narrow** — fixed 600px panel hides chart detail.
2. **External chart images broken** — TradingView S3 URLs are rejected by the browser when loaded as `<img>` (referrer/CORS), so the drawer falls back to a bare hyperlink.
3. **Multi-media posts only show one image** — a news_item with 4 charts produces 1 interpretation card linked to 1 media row; the other 3 media rows are invisible.
4. **`chart_hacker_trades` is a JSON dump** — rendered with `JSON.stringify` inside a `<pre>` block, unreadable.
5. **Empty drawer on chart-only posts** — a news_item with media but no interpretation rows opens a blank drawer ("No interpretation found").
6. **Quant section blank for chart-only posts** — out of scope here, see Slice 2 (called out so the design doesn't accidentally remove the quant block).

Slice 1 addresses defects 1, 2, 3, 4, 5. Defect 6 stays untouched.

---

## 2. Assumptions & Constraints

- **(REVISED per critique #1)** The actual drawer endpoint is [`GET /api/interpretations/drawer`](shared/dashboard/interpretation_drawer_routes.py:162) with query params `news_item_id` or `id` (mutually exclusive). The frontend already calls it at [`app.js:430`](shared/dashboard/static/app.js:430). There is **no** `/api/interpretations/by_news_item/{id}` route. We extend the existing endpoint's response payload; we do not break it.
- **(REVISED per critique #2)** The current response is already a JSON object — `{ok, news_item_id, id, limit, rows}` (see [`handle_drawer`](shared/dashboard/interpretation_drawer_routes.py:178)). It is NOT a bare array. Adding new top-level keys (`media_gallery`, `news_item`) is therefore additive and backward-compatible.
- We do **not** introduce a new database table for this slice. Multi-media is solved by querying [`media_items`](shared/dashboard/interpretation_drawer_provider.py:332) once more, keyed on `news_item_id`.
- The image proxy is server-side fetch only, no caching layer beyond HTTP `Cache-Control` headers in this slice. A disk cache can come later.
- The proxy must be locked down to a hostname allow-list to avoid SSRF.
- Drawer width is a CSS-only change; no JS resize handles in this slice.
- We keep [`renderDrawer()`](shared/dashboard/static/app.js:478) as the single entry point. We extend it; we do not split the file.
- Direction enum stays `long | short | unclear`; we do not introduce new values.
- All DB queries remain parameterised (project rule).

---

## 3. Design

### 3.0 Prerequisite — extend `_row_to_dict` media payload (REVISED per critique #3, #12)

**File:** [`shared/dashboard/interpretation_drawer_provider.py`](shared/dashboard/interpretation_drawer_provider.py:332)

The existing [`_row_to_dict`](shared/dashboard/interpretation_drawer_provider.py:390) emits a `media` sub-object with `id, url, local_path, thumbnail_path, source_url, media_type`. **It does not expose `mime_type`**, and `mime_type` is not in the `_select_clause` either ([`provider.py:347`](shared/dashboard/interpretation_drawer_provider.py:347)).

Required changes before any other §3 work:

1. Add `mi.mime_type AS media_mime_type` to [`_select_clause`](shared/dashboard/interpretation_drawer_provider.py:333).
2. Extend the `media` dict in [`_row_to_dict`](shared/dashboard/interpretation_drawer_provider.py:501) so it carries the full set used by the new gallery and trade-card path:
   ```python
   "media": {
       "id": media_id,
       "url": media_url,                        # api/media/<id> when local file present
       "local_path": rec["media_local_path"],
       "thumbnail_path": rec["media_thumbnail_path"],
       "source_url": rec["media_source_url"],   # already present, must remain
       "media_type": rec["media_type"],         # already present
       "mime_type": rec["media_mime_type"],     # NEW
   },
   ```
3. Update [`shared/tests/test_interpretation_drawer_provider.py`](shared/tests/test_interpretation_drawer_provider.py) fixture rows to include the new column so existing tests still pass.

**Rationale:** the gallery path (§3.3) and the per-interp media badge (§3.5b) both need `mime_type` to decide between `<img>` / `<video>` / "download" rendering. Doing it once at the provider boundary keeps the route handler dumb and avoids a second round-trip.

**Complexity:** trivial (~6 lines + one fixture update).

---

### 3.1 Wider, scrollable drawer (Defect 1)

**File:** [`shared/dashboard/static/app.css`](shared/dashboard/static/app.css:476)

**Change:** `.side-drawer { width: 600px; }` → responsive sizing.

Proposed values **(REVISED per critique #8)**:
- `width: clamp(720px, 80vw, 1600px);`
- `max-width: 100vw;`
- Mobile breakpoint stays as-is at [`app.css:567`](shared/dashboard/static/app.css:567): `@media (max-width: 900px) { .side-drawer { width: 100%; } }`.
- `.drawer-body { overflow-y: auto; overflow-x: hidden; }` — already set on [`app.css:506`](shared/dashboard/static/app.css:506); verify and keep `overflow-x: hidden` is added.
- Add `.drawer-body img { max-width: 100%; height: auto; }` so injected images can never overflow horizontally.

**Rationale (REVISED per critique #8):** `clamp(720px, 80vw, 1600px)` gives a hard floor of 720px on tiny laptops (so the trade table never gets squashed), 80vw on a normal screen, and a 1600px ceiling on ultrawides. The previous `min(1100px, 80vw)` capped too aggressively on a 4K monitor.

**Complexity:** trivial.

---

### 3.2 Image proxy for external media (Defect 2)

**File:** [`shared/dashboard/server.py`](shared/dashboard/server.py:343)

**Approach:** add a new route `GET /api/media/proxy?url=<encoded>` rather than overloading [`handle_media()`](shared/dashboard/server.py:343). Reasons:

- `handle_media(media_id)` is a path-param route — squeezing a query string into it is messy.
- A separate route gives us a clean place to enforce the host allow-list, the per-session rate limit, and SSRF guards.
- The frontend can pick `/api/media/<id>` for local files and `/api/media/proxy?url=…` for remote — same shape, different endpoint.

**Route signature:** `GET /api/media/proxy?url=<absolute https url>`

**Server behaviour:**

1. Parse `url` query param. Reject if missing, not absolute, or not `https://`.
2. **Host allow-list is the PRIMARY defence (REVISED per critique #4).** Parse hostname. Reject unless host matches the allow-list:
   - `s3.tradingview.com`
   - `*.tradingview.com`
   - `cdn.discordapp.com`
   - `media.discordapp.net`
   - `pbs.twimg.com`
   - `i.imgur.com`
   The list lives in a module-level constant `_MEDIA_PROXY_ALLOWED_HOSTS` so it's grep-able and easy to extend. Wildcard match is a strict suffix check on dot-boundaries (`.tradingview.com`).
3. **IP-resolution check is SECONDARY (REVISED per critique #4).** After host passes allow-list, resolve via `socket.getaddrinfo` and reject if any resolved IP is private/loopback/link-local/reserved (`ipaddress.ip_address().is_private | is_loopback | is_link_local | is_reserved`). **Documented limitation:** this does NOT defeat DNS-rebind attacks where the attacker controls a domain on the allow-list and points it at 127.0.0.1 between resolution and connection. Slice 1 accepts that gap because (a) every allow-listed host is owned by a third party we trust not to point at our loopback, and (b) shipping a custom `TCPConnector` that pins the resolved IP is a non-trivial aiohttp engineering task. **No custom `TCPConnector` in Slice 1 (REVISED per critique #4).** This limitation is recorded in §6.1.
4. **Rate limit (REVISED per critique #7).** Before fetching, apply a per-session token bucket: 30 requests / 60 s, identified by the dashboard session cookie (or remote IP if unauthenticated routes are ever exposed). Implementation: a small module-level `dict[str, deque[float]]` plus a 15-line check; deque entries older than 60 s are discarded on each request. On exceed, return 429 with `Retry-After: <seconds>`. This is enough to stop a hot loop scraping the proxy.
5. `aiohttp.ClientSession.get(url, timeout=ClientTimeout(total=15))` with:
   - User-Agent: a stock browser string, e.g. `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36` **(REVISED per critique #16)**.
   - Per-host `Referer` lookup table **(REVISED per critique #16)**:
     ```python
     _MEDIA_PROXY_REFERERS = {
         "tradingview.com": "https://www.tradingview.com/",
         "discordapp.com":  "https://discord.com/",
         "discordapp.net":  "https://discord.com/",
         "twimg.com":       "https://twitter.com/",
         "imgur.com":       "https://imgur.com/",
     }
     ```
     Match by registered domain suffix; default = no Referer.
   - `allow_redirects=True`, capped at 3 redirects, re-validating each redirect target against the allow-list **and** the IP check.
6. **Body size cap (REVISED per critique #6).** Disable aiohttp auto-decompression: pass `auto_decompress=False` to the session/request so the cap is applied to **raw on-the-wire bytes** rather than post-gzip-expansion bytes (a 100 KB gzipped bomb would otherwise expand past the cap). Stream-read with `resp.content.iter_chunked(64 * 1024)`; abort and 502 if cumulative raw bytes exceed **10 MB**. Re-emit the body as-is to the client; preserve the upstream `Content-Encoding` header so the browser decompresses normally.
7. **Content-Type validation (REVISED per critique #5).** Lower-case the upstream `Content-Type` and accept ONLY:
   - `image/png`
   - `image/jpeg`
   - `image/webp`
   - `image/gif`
   Reject `image/svg+xml` (SVG can carry `<script>`) and every other type with HTTP 415. The check is on the bare type token, ignoring the `; charset=…` suffix. No `image/*` wildcard.
8. Stream to client with:
   - The validated `Content-Type`
   - Upstream `Content-Encoding` (if any)
   - `Cache-Control: public, max-age=86400`
   - `X-Content-Type-Options: nosniff`
9. Wrap exceptions: any failure returns 502 with a JSON `{"error": "..."}` body. Log at warning level with the URL and the upstream status.

**Frontend impact:** [`_row_to_dict`](shared/dashboard/interpretation_drawer_provider.py:390) exposes `media.source_url` (verified — see [`provider.py:506`](shared/dashboard/interpretation_drawer_provider.py:506)). The §3.0 change additionally exposes `media.mime_type`. We change one place in [`renderDrawer()`](shared/dashboard/static/app.js:478) so the `<img>` `src` falls back to `/api/media/proxy?url=<encoded source_url>` when there's no local file. The "External media: <link>" text fallback only fires if there's neither a local file nor a `source_url`.

**Helper for URL building (frontend):**
```js
function _mediaSrc(media) {
  if (!media) return null;
  if (media.url) return media.url;                                  // local /api/media/<id>
  if (media.source_url) return `api/media/proxy?url=${encodeURIComponent(media.source_url)}`;
  return null;
}
```

**Complexity:** medium. Most of the work is in the SSRF guard, the redirect re-validation, and the rate limit, which are the easy places to ship a vulnerability.

---

### 3.3 Multi-media gallery (Defect 3)

**Files:**
- [`shared/dashboard/interpretation_drawer_provider.py`](shared/dashboard/interpretation_drawer_provider.py:1) — backend.
- [`shared/dashboard/interpretation_drawer_routes.py`](shared/dashboard/interpretation_drawer_routes.py:162) — route handler.
- [`shared/dashboard/static/app.js`](shared/dashboard/static/app.js:478) — frontend.

**Backend approach (chosen): a separate query for all media on a news_item.**

Two options were considered:
- *(A)* Extend the existing `_select_clause()` join to return one row per media. — Rejected. It would multiply rows in the result set and the route layer doesn't expect that; we'd be reshaping every consumer of the provider.
- *(B)* Fire a second query keyed on `news_item_id` that returns all media for that news_item, attach the list to the response envelope (NOT to each row). — Chosen. Cleaner separation, no row multiplication, no breaking change.

**Implementation outline:**

1. Add a new method on the provider, `fetch_media_for_news_item(news_item_id: int) -> List[Dict]`, that runs **(REVISED per critique #14)**:
   ```sql
   SELECT id, local_path, thumbnail_path, source_url,
          media_type, mime_type, processing_status,
          width, height, created_at
   FROM media_items
   WHERE news_item_id = $1
   ORDER BY id ASC
   LIMIT 24
   ```
   The `LIMIT 24` is a hard upper bound — a sane news_item has 1–6 charts; anything past 24 is almost certainly a bug or an attack and the gallery would be unusable anyway. Returns a list of dicts shaped like the existing `media` sub-object plus `processing_status`, `width`, `height`.
2. Modify [`handle_drawer`](shared/dashboard/interpretation_drawer_routes.py:162) so that when `news_item_id is not None`:
   - Call both `provider.fetch_by_news_item(news_item_id, limit=limit)` and `provider.fetch_media_for_news_item(news_item_id)` concurrently with `asyncio.gather`.
   - **Response envelope (REVISED per critique #2):** keep the existing keys (`ok, news_item_id, id, limit, rows`) and add `media_gallery`. The shape becomes:
     ```json
     {
       "ok": true,
       "news_item_id": 3445,
       "id": null,
       "limit": 5,
       "rows": [ /* existing interpretation dicts, unchanged */ ],
       "media_gallery": [ {id, url, source_url, thumbnail_path, media_type, mime_type, processing_status, width, height}, ... ],
       "news_item": { /* see §3.5 — only present when rows.length === 0 */ }
     }
     ```
3. Each item in `media_gallery` exposes both `url` (when `local_path` is set, → `api/media/<id>`) and `source_url` (when remote-only). The frontend uses `_mediaSrc(media)` to decide. **Both `media_type` and `mime_type` are present (REVISED per critique #12)** so the frontend can distinguish `image/png` from `image/svg+xml` (the latter would be rejected by the proxy anyway, but the gallery should not even attempt to render it).

**Frontend approach:**
- In `renderDrawer()`, render the gallery **once at the top of the drawer**, not per interpretation card. Reasons:
  - The 4-charts-1-interpretation case (TAO/USDT) is the canonical bug. Rendering the gallery per card would duplicate it 4× when there are 4 interpretations.
  - The empty-interpretation case (Defect 5) needs the gallery to render even when `rows.length === 0`.
- Gallery layout: a CSS grid with 1–2 columns depending on drawer width:
  ```css
  .drawer-gallery { display:grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap:12px; margin-bottom:16px; }
  .drawer-gallery img { width:100%; border-radius:6px; border:1px solid var(--border-color); cursor:zoom-in; }
  ```
- Each gallery image is wrapped in `<a target="_blank">` to the highest-res URL (local file via `/api/media/<id>` if available, else `source_url` raw — clicking should leave the proxy and go to the original).
- A small caption below each image: `media #<id> · <media_type> · <processing_status>`. This is debug-friendly and the user has been asking for it during this slice.
- **Per-interp `↳ from media #N` badge (REVISED per critique #15).** When a row has `r.media_item_id`, render a small inline badge inside that interpretation card: `<span class="muted small">↳ from media #<id></span>` linking (via `href="#gallery-<id>"`) to the matching gallery tile, which we tag with `id="gallery-<id>"`. Five extra lines. Done in this slice, not deferred.

**Defect 5 (empty drawer) is solved by this same change**: when `rows.length === 0`, instead of rendering the "No interpretation found" message alone, we render the gallery first, then the message. So a chart-only news_item with no interpretation still shows its charts.

**Complexity:** medium.

---

### 3.4 `chart_hacker_trades` table (Defect 4)

**File:** [`shared/dashboard/static/app.js`](shared/dashboard/static/app.js:535)

**Current rendering:** `JSON.stringify(ch.chart_hacker_trades, null, 2)` inside a `<pre>` ([`app.js:536`](shared/dashboard/static/app.js:536)).

**Target shape:** the field is a JSON array of trade objects. Schema observed in production:
```json
[
  {"symbol": "TAO/USDT", "direction": "long", "entry": 295.32,
   "stop_loss": 280.0, "tp1": 306.0, "tp2": 315.0, "tp3": 325.0,
   "tp4": null, "tp5": null, "tp6": null,
   "risk_reward": 2.1, "confidence": 0.78,
   "timeframe": "4h", "rationale": "broke ascending wedge..."}
]
```

**Render plan:**
- A new helper `_drawerChartHackerTrades(trades)` that:
  - Accepts an array (or a single object — wrap in array if not).
  - If empty / not an array, returns `''`.
  - Builds a `<table class="drawer-trades">` with **canonical** columns:
    `Symbol`, `Direction` (pill), `Entry`, `Stop`, `TP1`, `TP2`, `TP3`, `TP4`, `TP5`, `TP6`, `R:R`, `Conf`, `TF`. (Rationale is NOT a column — see below.)
  - Each numeric cell uses `_fmtNum` (already exists in app.js) so we get consistent decimals. Null/empty cells render as a muted `—`.
  - Direction cell uses the same pill-class trick already used for `llmDir` (long=success, short=danger, else warning).
  - **Column-hide rule (REVISED per critique #9).** Only apply the all-null column-hide when `trades.length >= 3`. When there are 1 or 2 trades, render every canonical column with `—` for nulls so the user sees the full schema. Rationale: with ≤2 rows, "this column was empty for both rows" is not statistically meaningful and hiding it loses information.
  - **Rationale rendering (REVISED per critique #10).** Rationale is rendered as a **second `<tr>` with `<td colspan=N>`** immediately after each trade's main row, NOT as a 14th column. This is the standard "details under row" pattern (think GitHub issue search result expanders) and keeps the numeric grid tight.
  - **Rationale collapse threshold (REVISED per critique #11).** If `rationale.length > 200`, wrap in `<details>` with the first 160 chars + "…" as the visible summary. Otherwise render plain text. The 200-char threshold is fixed in this plan — IMPLEMENT must NOT re-litigate it.
- Add CSS for `.drawer-trades`:
  ```css
  .drawer-trades { width: 100%; border-collapse: collapse; font-size: 12px; }
  .drawer-trades th, .drawer-trades td { padding: 6px 8px; border-bottom: 1px solid var(--border-color); text-align: left; vertical-align: top; }
  .drawer-trades th { background: rgba(255,255,255,0.03); font-weight: 600; }
  .drawer-trades td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .drawer-trades tr.rationale-row td { background: rgba(255,255,255,0.015); padding: 4px 10px 10px 10px; color: var(--text-secondary); font-size: 12px; line-height: 1.4; }
  .drawer-trades tr.rationale-row details summary { cursor: pointer; }
  ```
- Replace the current `chTrades = … JSON.stringify …` line at [`app.js:535-536`](shared/dashboard/static/app.js:535) with `chTrades = _drawerChartHackerTrades(ch.chart_hacker_trades)`.

**Complexity:** medium.

---

### 3.5 Empty drawer on chart-only news_items (Defect 5)

Already covered by §3.3: rendering the gallery before the "No interpretation found" message.

The drawer body, when `rows.length === 0` AND `media_gallery.length > 0`, renders:
1. **(REVISED per critique #13)** A `news_item` header (id + headline + author + source + collected_at + total media count). This header is **only emitted by the backend when `rows.length === 0`** (Option C from the critique). When there are rows, the frontend reads news metadata from `rows[0].news` — which is already populated by [`_row_to_dict`](shared/dashboard/interpretation_drawer_provider.py:485). This avoids denormalising the same data into two places when both are available.
2. The media gallery.
3. A muted note: "No interpretation has been generated for this post yet. The charts above were collected; analysis is pending or skipped."

When both are empty, fall through to the existing "No interpretation found" message — unchanged.

**Backend rule (REVISED per critique #13):**
```python
# in handle_drawer, after gather:
envelope = {"ok": True, "news_item_id": ..., "id": ..., "limit": ..., "rows": rows, "media_gallery": gallery}
if news_item_id is not None and len(rows) == 0:
    envelope["news_item"] = await provider.fetch_news_item_header(news_item_id)
return web.json_response(envelope)
```
The `fetch_news_item_header` provider method runs a small `SELECT id, headline, author, source, channel_name, collected_at, published_at, has_media, media_count FROM news_items WHERE id = $1`. The `news_item` key is **absent** from the envelope when `rows` is non-empty — the frontend must read `rows[0].news` in that branch.

**Complexity:** trivial (one extra small SELECT, one frontend branch).

---

## 4. Backend Payload Shape (final, REVISED per critique #1, #2, #13)

`GET /api/interpretations/drawer?news_item_id=<id>` — extended shape (additive, backward-compatible):

**Case A — `rows.length > 0`:**
```json
{
  "ok": true,
  "news_item_id": 3445,
  "id": null,
  "limit": 5,
  "rows": [ /* existing interpretation dicts, unchanged */ ],
  "media_gallery": [
    {
      "id": 1740,
      "url": "api/media/1740",
      "source_url": "https://s3.tradingview.com/snapshots/e/eAcNZ9y9.png",
      "thumbnail_path": null,
      "media_type": "image",
      "mime_type": "image/png",
      "processing_status": "analyzed",
      "width": 1920,
      "height": 1080
    }
  ]
}
```
No `news_item` key — the frontend reads `rows[0].news`.

**Case B — `rows.length === 0`:**
```json
{
  "ok": true,
  "news_item_id": 3445,
  "id": null,
  "limit": 5,
  "rows": [],
  "media_gallery": [ /* same shape as above */ ],
  "news_item": {
    "id": 3445,
    "headline": "...",
    "author": "...",
    "source": "...",
    "channel_name": "...",
    "collected_at": "...",
    "published_at": "...",
    "has_media": true,
    "media_count": 4
  }
}
```

`GET /api/interpretations/drawer?id=<interp_id>` (the by-id branch): **no shape change.** That branch is keyed on a single interpretation, not a news_item, and is not used for the gallery. `media_gallery` and `news_item` are not added to it.

---

## 5. Dependency Graph

```
3.0 _row_to_dict + mime_type   (provider)        ──► must ship FIRST, others depend on it
                                                       │
3.1 wider drawer  (CSS only)                     ──► independent of 3.0; can ship in parallel
                                                       │
3.4 chart_hacker_trades table  (JS+CSS)          ──►   │  independent
                                                       │
3.2 image proxy  (server.py + rate limit)        ──┐   │
                                                   ▼   ▼
3.3 multi-media gallery  (provider+route+JS)   ──► depends on 3.0 and 3.2
                                                       │
3.5 empty-drawer branch + news_item header     ──► depends on 3.3
```

Recommended build order:
1. §3.0 provider prerequisite — unlocks 3.3 / 3.5.
2. §3.1 wider drawer — instant visible win, zero risk, parallel-safe.
3. §3.4 chart_hacker_trades table — pure frontend, isolated helper.
4. §3.2 image proxy — backend-only, can be tested with `curl` before any frontend wiring.
5. §3.3 multi-media gallery — needs proxy + §3.0 in place.
6. §3.5 empty-drawer branch + `news_item` header — last, layers on §3.3.

---

## 6. What Could Go Wrong

### 6.1 SSRF via proxy (REVISED per critique #4)
**Risk:** an attacker hits `/api/media/proxy?url=https://evil.example/...` or abuses a DNS-rebind on an allow-listed domain.
**Mitigation, layered:**
- **Primary:** host allow-list — only six third-party domains we know serve charts.
- **Secondary:** post-resolution IP check — rejects private/loopback/link-local/reserved.
- **Tertiary:** rate limit (30 req/min/session) — caps the blast radius of any successful exploit.
**Documented residual risk:** DNS-rebind. An attacker who controls an allow-listed domain (or could MITM its DNS) could resolve it to the bind-time public IP, pass the IP check, then re-resolve to 127.0.0.1 on the actual TCP `connect()`. Slice 1 does NOT defeat this. Mitigations rejected for Slice 1: (a) custom `TCPConnector` that pins the resolved IP — non-trivial in aiohttp and would break HTTPS hostname verification; (b) running the proxy in a network-namespaced sidecar — operational overhead. Both can land in a Slice 1.x follow-up if logs ever show suspicious resolutions.
**Must be unit-tested before merge:** see §7.2.

### 6.2 Proxy DoS (REVISED per critique #7)
**Risk:** someone scripts the proxy endpoint to fetch huge images repeatedly, eating server bandwidth.
**Mitigation in this slice:** 10 MB raw-byte body cap + 15 s timeout + `Cache-Control: max-age=86400` (so a CDN/browser cache absorbs repeats) + per-session token bucket (30 req/min). A cluster-wide rate limiter is a follow-up, not Slice 1.

### 6.3 TradingView referrer/UA gating changes (REVISED per critique #16)
**Risk:** TradingView changes their hotlink protection and our hard-coded `Referer`/UA stops working.
**Mitigation:** the per-host `_MEDIA_PROXY_REFERERS` table and the stock browser UA string are the two knobs. The proxy logs the upstream status code with the URL host. If the dashboard suddenly shows broken images, the logs will say "tradingview returned 403" and we adjust. Acceptable for a dev tool.

### 6.4 Multi-media query N+1
**Risk:** if the dashboard ever loads many news_items and fetches gallery for each, we'd be doing N+1 queries.
**Mitigation:** the gallery query only fires when the drawer opens (one news_item at a time). The list view is unchanged. No N+1 risk in current call patterns. The `LIMIT 24` (REVISED per critique #14) protects against a pathological news_item with hundreds of media rows.

### 6.5 Existing consumers of the drawer endpoint (REVISED per critique #2)
**Risk:** none material. The response was already an object envelope `{ok, news_item_id, id, limit, rows}`; we are only adding `media_gallery` (always) and `news_item` (only on empty `rows`). All current consumers — only [`app.js:430`](shared/dashboard/static/app.js:430) — read `res.rows` and tolerate extra keys.

### 6.6 Long `rationale` cells blow up the table (REVISED per critique #10, #11)
**Risk:** Chart-Hacker rationale strings can be 500+ chars and would make the table unreadable.
**Mitigation:** §3.4 renders rationale as a `<tr><td colspan=N>` second row, not a 14th column. Strings >200 chars are wrapped in `<details>` with a 160-char summary. Threshold is locked in this plan; IMPLEMENT must not change it.

### 6.7 Drawer width on small laptops (REVISED per critique #8)
**Risk:** `clamp(720px, 80vw, 1600px)` on a 1280px screen = 1024px drawer = covers most of the page behind it.
**Mitigation:** that's what the user asked for ("most of the screen"). The 720px floor protects the trade table. The backdrop is already there to indicate modal-ness. If 720px is still too tight on a small window, the mobile breakpoint at `max-width: 900px` switches to `width: 100%` — this is unchanged.

### 6.8 `chart_hacker_trades` shape drift
**Risk:** the Chart-Hacker prompt could change and add/rename fields. Rendering would silently drop them.
**Mitigation:** the helper renders the fixed canonical column set, but **also** renders a small "+N extra fields" badge per row when unknown keys appear, with a click-to-expand JSON view. Belt and braces; cheap to add.

### 6.9 Gallery shows non-chart media (text attachments, audio)
**Risk:** `media_items.media_type` can be `image`, `video`, `audio`, `document`. Rendering an `<img>` for a video URL would 404 and SVG could carry script.
**Mitigation:** in the gallery loop, branch on `media_type` AND `mime_type` (REVISED per critique #5, #12):
- `mime_type` in {`image/png`, `image/jpeg`, `image/webp`, `image/gif`} → `<img>` with proxy URL.
- `mime_type === 'image/svg+xml'` → "media #N (svg, not rendered)" badge — proxy will reject anyway.
- `media_type === 'video'` → `<video controls>` with the proxy URL (if we extend the proxy's allow-list of mime types in a later slice; for Slice 1, render a "video — open in new tab" link).
- Other types → clickable filename badge.

### 6.10 Quant block accidentally hidden by drawer changes
**Risk:** while refactoring `renderDrawer()` we accidentally drop the quant section.
**Mitigation:** Slice 1 IMPLEMENT must include a smoke test: open drawer for a news_item known to have a non-empty quant payload (any text-only news_item with a resolved symbol), confirm the Quant section still renders. Slice 2 will then make it always-on.

---

## 7. Testing Plan (for IMPLEMENT phase)

1. **CSS smoke test:** open drawer in a 1920px, a 1280px, and a 768px viewport; confirm width clamps to ~1536px, ~1024px, and 100vw respectively, and that body scrolls.
2. **Proxy unit tests (REVISED per critique #17).** Drop the integration-style proxy test entirely. Keep only `_validate_url()` (and its sibling helpers) tested against ~15 hostile inputs:
   - empty string
   - `not-a-url`
   - `http://s3.tradingview.com/x.png` (wrong scheme)
   - `https://evil.example/x.png` (host not allow-listed)
   - `https://s3.tradingview.com.evil.example/x.png` (suffix-confusion attack)
   - `https://s3.tradingview.com/../etc/passwd` (path traversal harmless but should still pass URL validation; tested for non-rejection)
   - `https://localhost/x.png` (host allow-list rejects)
   - `https://127.0.0.1/x.png` (host allow-list rejects)
   - `https://169.254.169.254/x.png` (cloud metadata; host allow-list rejects, IP check is belt-and-braces)
   - `https://[::1]/x.png` (ipv6 loopback)
   - `https://s3.tradingview.com:22/x.png` (non-standard port; allow-list permits, but log it)
   - `https://s3.tradingview.com/x.svg` (allow-listed host but `image/svg+xml` content-type → 415, mocked upstream)
   - `https://s3.tradingview.com/x.png` with `Content-Length: 999999999` (cap → 502, mocked)
   - `https://s3.tradingview.com/x.png` returning 4 redirects (cap → 502, mocked)
   - `https://s3.tradingview.com/x.png` redirecting to `https://localhost/x.png` (re-validation rejects → 502, mocked)
   No real network calls; all upstream behaviour mocked with `aioresponses`. Total runtime <1 s.
3. **Rate-limit unit test:** fire 31 requests in <1 s from one mock session, assert the 31st returns 429 with `Retry-After`.
4. **Multi-media test:** open drawer for news_item #3445 (TAO/USDT) — confirm 4 images render in the gallery, 1 interpretation card below, and the `↳ from media #N` badge points to the correct gallery tile.
5. **Empty drawer test:** open drawer for the "Ta Daa" post (chart-only, no interpretation) — confirm the `news_item` envelope key is present, the gallery renders, and the "no interpretation yet" message follows.
6. **Trade table test:** open drawer for any news_item with `chart_hacker_trades` populated:
   - Single-trade row → confirm all canonical columns visible with `—` for nulls (REVISED per critique #9).
   - Three+ trade rows → confirm columns where every row is null are hidden.
   - Long rationale → confirm `<details>` collapse triggers when `>200` chars and the rationale row spans the full table width via `colspan` (REVISED per critique #10, #11).
7. **Backward-compat test:** confirm the existing single-interpretation drawer (e.g. text-only news_item with a resolved BTC symbol) still renders all current sections (Actor, LLM, Quant, Tags, footer) and that `res.rows` is still an array.
8. **`/api/interpretations/drawer?id=<interp_id>` regression:** confirm the by-id branch is byte-identical to today's response (no `media_gallery`, no `news_item`).

---

## 8. Implementation Steps (priority order)

1. **Provider — extend `_row_to_dict` + `_select_clause` with `mime_type`** ([`shared/dashboard/interpretation_drawer_provider.py`](shared/dashboard/interpretation_drawer_provider.py:332)) **(REVISED per critique #3, #12)**
   Trivial. ~6 lines + fixture update in [`shared/tests/test_interpretation_drawer_provider.py`](shared/tests/test_interpretation_drawer_provider.py).
2. **CSS — drawer width** ([`shared/dashboard/static/app.css`](shared/dashboard/static/app.css:476)) **(REVISED per critique #8)**
   Trivial. ~10 lines. Use `clamp(720px, 80vw, 1600px)`. Ship and verify visually.
3. **JS — `chart_hacker_trades` table helper** ([`shared/dashboard/static/app.js`](shared/dashboard/static/app.js:535)) **(REVISED per critique #9, #10, #11)**
   Medium. New `_drawerChartHackerTrades` function + CSS additions for `.drawer-trades` (including `.rationale-row`). Replace one existing line in `renderDrawer`. Verify with one known news_item.
4. **Backend — image proxy route** ([`shared/dashboard/server.py`](shared/dashboard/server.py:343)) **(REVISED per critique #4, #5, #6, #7, #16)**
   Medium-large. New handler + allow-list constant + Referer table + SSRF helper + token-bucket rate limit + raw-bytes cap with `auto_decompress=False` + content-type whitelist. Unit-test the URL validator and rate limiter in isolation before wiring frontend.
5. **Provider — `fetch_media_for_news_item` + `fetch_news_item_header`** ([`shared/dashboard/interpretation_drawer_provider.py`](shared/dashboard/interpretation_drawer_provider.py:332)) **(REVISED per critique #14)**
   Medium. Two new methods + two new SQL strings (`LIMIT 24` on the gallery query).
6. **Route — extend `handle_drawer` envelope** ([`shared/dashboard/interpretation_drawer_routes.py`](shared/dashboard/interpretation_drawer_routes.py:162)) **(REVISED per critique #1, #2, #13)**
   Medium. `asyncio.gather` the rows + gallery queries when `news_item_id` is set; conditionally fetch the `news_item` header only when `rows` is empty; add `media_gallery` always; preserve the existing `{ok, news_item_id, id, limit, rows}` keys verbatim.
7. **JS — `_mediaSrc` + gallery rendering + empty-drawer branch + per-interp media badge** ([`shared/dashboard/static/app.js`](shared/dashboard/static/app.js:478)) **(REVISED per critique #15)**
   Medium. New helper, new gallery section above the rows loop, gallery tiles tagged `id="gallery-<id>"`, per-card `↳ from media #N` badge, branch on `rows.length === 0 && media_gallery.length > 0`.
8. **Documentation** — append a "Drawer UX (Slice 1)" section to [`CLAUDE.md`](CLAUDE.md:1) noting the extended endpoint envelope and the new proxy route.

Estimated total complexity: **medium**, ~1 working day. The proxy (step 4) is the only step that can eat real time — the SSRF guard, rate limiter, and `auto_decompress=False` raw-bytes cap need careful unit tests.

---

## 9. What this Plan Does NOT Do (deferred)

- It does not change when `run_quant_track` fires (Slice 2).
- It does not introduce a media-cache table or persistent thumbnail store (future).
- It does not add per-image annotations, drawing tools, or chart overlays (future).
- It does not change the way news_items are listed in the feed; only the drawer rendering.
- It does not introduce auth on the proxy beyond what the dashboard already enforces; if the dashboard becomes publicly reachable, the proxy needs auth too.
- It does not defend against DNS-rebind attacks on allow-listed domains (see §6.1; Slice 1.x follow-up).
- It does not include an integration-style proxy test against real upstreams **(REVISED per critique #17)** — `_validate_url` unit tests cover the security surface.

---

## 10. Self-Review (devil's advocate, second pass)

> "What would a senior engineer critique about this revised plan?"

- **Allow-list still requires manual maintenance.** Acceptable; the alternative is a much bigger problem.
- **Rate limit is in-process and resets on restart.** True. A clustered limiter is overkill for Slice 1; the dashboard runs single-instance today.
- **`auto_decompress=False` means we forward gzipped bodies as-is.** That's the point — the cap is on raw bytes. Browsers handle the `Content-Encoding: gzip` header normally. Verified against the aiohttp docs (`ClientResponse.content` returns raw stream when `auto_decompress=False`).
- **Per-host Referer table uses suffix matching, which is fragile.** It is, but the surface is six known domains and the fallback is "no Referer." Worst case: a working host stops working and we add an entry.
- **`news_item` header only on empty rows is a slight asymmetry in the API.** Conscious choice (Option C from the critique). The alternative (always include it) duplicates `rows[0].news` data; the alternative (never include it, frontend re-fetches) costs an extra round-trip. Asymmetric but cheap.
- **`LIMIT 24` is arbitrary.** It is. 24 = 6 rows of 4 columns of charts, which covers every real-world Discord post we've seen. If a news_item ever legitimately has more, the user can still see the first 24 and we tune later.
- **Trade table column-hide threshold of `trades.length >= 3` is arbitrary.** It is, but it's the smallest number where "all-null column" is statistically meaningful. With 1 or 2 rows, hiding columns destroys schema visibility.
- **Rationale collapse threshold of 200 chars is arbitrary.** Yes — and explicitly locked here so IMPLEMENT doesn't waste time re-debating it. 200 chars ≈ 2–3 lines of body text at the drawer's font size.

The plan stands.
