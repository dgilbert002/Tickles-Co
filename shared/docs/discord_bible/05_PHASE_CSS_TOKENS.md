# PHASE 5 — CSS TOKENS & DISCORD SKIN (pixel-faithful styling)

> Prereq: Phase 4 DONE. Read rules in `00_MASTER_INDEX.md`.
> Goal: produce ONE stylesheet, `discord-skin.css`, that reproduces Discord's dark theme
> exactly — design tokens, message row, reply-on-top spine, channel sidebar, unread
> counter, embed/chart card, and the fullscreen image lightbox. Everything is built on
> CSS variables so a color tweak is a one-line change.

> The real Discord CSS (1.66 MB, 14,208 rules) was captured during analysis to
> `/tmp/discord_css_full.css`. If that file is gone, the exact values you need are
> reproduced inline below — they are verified from that capture. You do NOT need to
> re-fetch anything.

Create `shared/dashboard/static/css/discord-skin.css` with EXACTLY this content. These
values are lifted from Discord's real stylesheet (font sizes, line-heights, spacings,
radii) and the documented dark palette primitives.

```css
/* BIBLE-P5 — Discord-identical dark skin. Built on CSS vars; tweak a token, not a rule. */
:root, .discord-skin {
  /* ---- palette primitives (Discord dark theme) ---- */
  --background-primary:   #313338;
  --background-secondary: #2b2d31;
  --background-secondary-alt: #232428;
  --background-tertiary:  #1e1f22;
  --background-base-lower:#2b2d31;
  --background-base-lowest:#1e1f22;
  --background-mod-subtle: rgba(78,80,88,0.3);
  --background-mod-normal: rgba(78,80,88,0.48);
  --background-message-hover: rgba(2,2,2,0.06);
  --channels-default:     #949ba4;
  --channel-icon:         #80848e;
  --text-normal:          #dbdee1;
  --text-muted:           #949ba4;
  --text-strong:          #f2f3f5;
  --header-primary:       #f2f3f5;
  --header-secondary:     #b5bac1;
  --interactive-normal:   #b5bac1;
  --interactive-hover:    #dbdee1;
  --interactive-active:   #ffffff;
  --interactive-text-active:#ffffff;
  --border-subtle:        rgba(78,80,88,0.32);
  --spine-default:        #4e5058;
  --brand-500:            #5865f2;
  --text-link:            #00a8fc;
  --background-feedback-notification: #f23f43;  /* mention red */
  --white:#fff; --black:#000;
  --font-primary: "gg sans","Noto Sans",Helvetica,Arial,sans-serif;
  --font-display: "gg sans","Noto Sans",Helvetica,Arial,sans-serif;
  --font-weight-normal:400; --font-weight-medium:500;
  --font-weight-semibold:600; --font-weight-bold:700;
  /* spacing scale (Discord uses --space-N) */
  --space-4:4px; --space-8:8px; --space-12:12px; --space-16:16px; --space-24:24px;
  --radius-xs:4px; --radius-sm:8px; --radius-md:12px;
}

/* ================= LAYOUT: tree (left) + river (right) ================= */
.dsc-wrap { display:flex; height:100%; background:var(--background-primary);
  color:var(--text-normal); font-family:var(--font-primary); }
.dsc-sidebar { width:240px; flex:0 0 240px; background:var(--background-secondary);
  overflow-y:auto; padding:8px 0; }
.dsc-main { flex:1 1 auto; display:flex; flex-direction:column; min-width:0;
  background:var(--background-primary); }

/* ================= CHANNEL SIDEBAR ================= */
.dsc-cat { color:var(--channels-default); font-size:12px; font-weight:600;
  text-transform:uppercase; letter-spacing:.02em; padding:18px 8px 4px 16px; cursor:pointer; }
.dsc-chan { display:flex; align-items:center; gap:6px; margin:1px 8px; padding:6px 8px;
  border-radius:4px; color:var(--channels-default); font-size:16px; font-weight:500;
  line-height:20px; cursor:pointer; white-space:nowrap; }
.dsc-chan:hover { background:var(--background-mod-subtle); color:var(--interactive-hover); }
.dsc-chan.dsc-selected { background:var(--background-mod-normal); color:var(--interactive-active); }
.dsc-chan .dsc-hash { color:var(--channel-icon); flex:0 0 auto; }
/* read = faded; unread = white+bold (Discord behavior) */
.dsc-chan.dsc-unread { color:var(--interactive-active); font-weight:600; }
.dsc-chan.dsc-unread::before { content:""; position:absolute; left:0; width:8px; height:8px;
  border-radius:50%; background:var(--interactive-active); margin-left:-12px; }
/* numbered counter pill (the "14") */
.dsc-count { margin-left:auto; min-width:16px; height:16px; padding:0 4px; border-radius:999px;
  background:var(--interactive-text-active); color:var(--black); font-size:12px;
  font-weight:600; text-align:center; line-height:16px; box-sizing:border-box; }
.dsc-count.dsc-mention { background:var(--background-feedback-notification); color:var(--white); }

/* ================= CHANNEL HEADER + SEARCH ================= */
.dsc-header { display:flex; align-items:center; gap:8px; height:48px; flex:0 0 48px;
  padding:0 16px; border-bottom:1px solid var(--border-subtle); box-shadow:0 1px 0 rgba(0,0,0,.2); }
.dsc-header .dsc-title { font-size:16px; font-weight:600; color:var(--header-primary); }
.dsc-search { margin-left:auto; background:var(--background-tertiary); border:none;
  border-radius:4px; color:var(--text-normal); font-size:14px; padding:6px 8px; width:180px; }
.dsc-search::placeholder { color:var(--text-muted); }

/* ================= MESSAGE RIVER ================= */
.dsc-river { flex:1 1 auto; overflow-y:auto; padding:16px 0 24px; }
.dsc-msg { position:relative; padding:2px 48px 2px 72px; min-height:22px; }
.dsc-msg:hover { background:var(--background-message-hover); }
.dsc-msg.dsc-groupstart { margin-top:17px; }
.dsc-avatar { position:absolute; left:16px; top:2px; width:40px; height:40px;
  border-radius:50%; object-fit:cover; background:var(--background-mod-subtle); }
.dsc-msg:not(.dsc-groupstart) .dsc-avatar { display:none; }
.dsc-header-line { display:flex; align-items:baseline; gap:8px; }
.dsc-username { font-size:1rem; font-weight:500; color:var(--text-strong); }  /* role color set inline */
.dsc-timestamp { color:var(--text-muted); font-size:.75rem; font-weight:400;
  line-height:1.375rem; margin-inline-start:6px; }
.dsc-content { font-size:1rem; font-weight:400; line-height:1.375rem;
  color:var(--text-normal); white-space:pre-wrap; word-wrap:break-word; }

/* ----- reply-on-top (the replied message sits ABOVE, with a spine) ----- */
.dsc-reply { position:relative; display:flex; align-items:center; gap:6px;
  margin-left:72px; margin-bottom:2px; color:var(--text-muted); font-size:.875rem; }
.dsc-reply::before { content:""; position:absolute; left:-36px; top:50%; bottom:-2px;
  width:24px; border-left:2px solid var(--spine-default);
  border-top:2px solid var(--spine-default); border-top-left-radius:8px; }
.dsc-reply-avatar { width:16px; height:16px; border-radius:50%; }
.dsc-reply-author { font-weight:500; color:var(--interactive-active); }
.dsc-reply-text { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:60ch; }

/* ----- chart / image attachment (click to fullscreen) ----- */
.dsc-attach { margin-top:8px; max-width:520px; }
.dsc-attach img { max-width:100%; max-height:350px; border-radius:8px; cursor:pointer;
  display:block; }
.dsc-embed { margin-top:8px; max-width:520px; background:var(--background-secondary);
  border-left:4px solid var(--spine-default); border-radius:4px; padding:8px 12px; }

/* ----- AI analysis (revealed on click-to-expand) ----- */
.dsc-ai { margin:8px 0 4px 72px; padding:10px 12px; border-radius:8px;
  background:var(--background-secondary-alt); border:1px solid var(--border-subtle);
  font-size:14px; display:none; }
.dsc-msg.dsc-expanded .dsc-ai { display:block; }

/* ================= FULLSCREEN LIGHTBOX ================= */
.dsc-lightbox { position:fixed; inset:0; background:rgba(0,0,0,.85); display:none;
  align-items:center; justify-content:center; z-index:9999; cursor:zoom-out; }
.dsc-lightbox.dsc-open { display:flex; }
.dsc-lightbox img { max-width:92vw; max-height:92vh; border-radius:8px; }

/* reduce-motion friendly slow-refresh fade for newly arrived messages */
.dsc-msg.dsc-new { animation: dsc-fadein .6s ease-out; }
@keyframes dsc-fadein { from { opacity:0; transform:translateY(4px);} to {opacity:1;transform:none;} }
```

## STEP 2 — Make sure the page loads this stylesheet
Phase 6 builds the page; for now just confirm the file is servable. The dashboard
serves `static/` already (that's where app.js lives). Confirm:
```bash
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" \
  http://127.0.0.1:3101/static/css/discord-skin.css
```
MUST be `200 text/css`. If 404, find how `static/` is mounted in server.py
(grep `static`) and place the file where other static assets resolve (it may be
`static/discord-skin.css` without the `css/` subdir — match what app.js/app.css use).

---

## VERIFY (all GREEN before Phase 6)
```bash
echo "=== stylesheet served ==="
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://127.0.0.1:3101/static/css/discord-skin.css
echo "   ^ MUST be 200 text/css"
echo "=== contains the key classes ==="
for c in dsc-msg dsc-reply dsc-count dsc-lightbox dsc-chan dsc-ai; do
  grep -q "\.$c" /opt/tickles/shared/dashboard/static/css/discord-skin.css && echo "  OK $c" || echo "  MISS $c"
done
```
Pixel sanity (manual but quick): create a throwaway `/tmp/skin_sample.html` that
includes the stylesheet and hand-writes ONE message row + one reply + one unread channel
with a count badge, open it in the browser tool, screenshot, and eyeball against
`discord1.png`. Spacing/colors must match. (This is a static check; the live feed is
Phase 6.)

## DOWNSTREAM SAFETY
- Existing `app.css`/`app.js` untouched: `curl -s -o /dev/null -w "%{http_code}\n"
  http://127.0.0.1:3101/static/app.js` = 200.
- Dashboard still healthy: `systemctl is-active tickles-dashboard` = active.

## ON SUCCESS
Append to PROGRESS.md:
`Phase 5 — DONE <iso> — discord-skin.css created from real Discord values (tokens, msg row, reply spine, sidebar, unread counter, embed, lightbox); served at /static/css/.`
Then open `06_PHASE_FEED_UI.md`.
