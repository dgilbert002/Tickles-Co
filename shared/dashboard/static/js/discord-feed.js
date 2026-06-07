/* BIBLE-P6 v13 — Comprehensive Discord Skin & Behavior Implementation. 
   Includes: unread counters, link/image/X.com embeds, role colors, scroll anchoring, 
   following filters, day separators, blue dots, and Slide-over Drawer integration! */
(function () {
  "use strict";
  var API = (location.pathname.startsWith("/dashboard") ? "/dashboard" : "") + "/api/discord";
  var state = { 
    source: "discord", 
    channel: null, 
    tree: [], 
    timer: null, 
    filter: "",
    unread: {}, 
    pinnedBottom: true, 
    lastChannel: null,
    followingOnly: true,
    followedTraders: [],
    seenTopId: 0,
    seenBottomId: 0,
    loadingPrev: false,
    nameMap: {},
    scrollOffset: 0
  };

  var E = function(t, c, x) { 
    var e = document.createElement(t); 
    if (c) e.className = c; 
    if (x != null) e.textContent = x; 
    return e; 
  };
  var S = function(s) { return s == null ? "" : String(s); };

  // Load saved last channel from localStorage
  try { state.lastChannel = localStorage.getItem("dsc_lastChannel") || null; } catch(e){}

  /* ---- Native Discord SVGs ---- */
  var SVG_HASHTAG = '<svg viewBox="0 0 24 24"><path d="M10.99 3.16A1 1 0 1 0 9 2.84L8.15 8H4a1 1 0 0 0 0 2h3.82l-.67 4H3a1 1 0 1 0 0 2h3.82l-.8 4.84a1 1 0 1 0 2 .32l.85-5.16h4.15l-.8 4.84a1 1 0 1 0 2 .32l.85-5.16H20a1 1 0 1 0 0-2h-3.82l.67-4H21a1 1 0 1 0 0-2h-3.82l.8-4.84a1 1 0 1 0-2-.32l-.85 5.16H10.15l.84-5.16ZM14.15 14l-.67-4H9.33l.67 4h4.15Z"/></svg>';
  var SVG_CALENDAR = '<svg viewBox="0 0 24 24"><path d="M7 1a1 1 0 0 1 1 1v.75c0 .14.11.25.25.25h7.5c.14 0 .25-.11.25-.25V2a1 1 0 1 1 2 0v.75c0 .14.11.25.25.25H19a3 3 0 0 1 3 3v12a3 3 0 0 1-3 3H5a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3h1.75c.14 0 .25-.11.25-.25V2a1 1 0 0 1 1-1Zm12 7H5v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8Z"/></svg>';
  var SVG_LOCK = '<svg viewBox="0 0 24 24"><path d="M16 4h.5v-.5a2.5 2.5 0 0 1 5 0V4h.5a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1h-6a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm4 0v-.5a1 1 0 0 0-2 0V4h2Z"/></svg>';

  function getChannelIcon(name) {
    var lower = name.toLowerCase();
    if (lower.includes("calendar-events") || lower.includes("announcements")) return SVG_CALENDAR;
    if (lower.includes("lock") || lower.includes("back-2-basics") || lower.includes("alpha-zone")) return SVG_LOCK;
    return SVG_HASHTAG;
  }

  /* ---- TradingView URL -> snapshot image ---- */
  function tvImg(u) {
    var m = u.match(/tradingview\.com\/x\/([A-Za-z0-9]+)/i);
    return m ? "https://s3.tradingview.com/snapshots/" + m[1][0].toLowerCase() + "/" + m[1] + ".png" : u;
  }
  
  /* ---- Extract both TradingView links and normal Image/Giphy URLs ---- */
  function extractMedia(t) {
    if (!t) return [];
    var media = [];
    // 1) TradingView charts
    t.replace(/https?:\/\/[^\s]*tradingview\.com\/x\/[A-Za-z0-9]+[^\s]*/gi, function(u) {
      media.push({ type: "image", src: tvImg(u) });
    });
    // 2) Direct images / Giphy
    t.replace(/https?:\/\/[^\s<]+\.(png|jpg|jpeg|gif|webp)/gi, function(u) {
      media.push({ type: "image", src: u });
    });
    // 3) Giphy embeds
    t.replace(/https?:\/\/giphy\.com\/gifs\/[A-Za-z0-9-]+/gi, function(u) {
      var m = u.match(/gifs\/.*-([A-Za-z0-9]+)$/) || u.match(/gifs\/([A-Za-z0-9]+)$/);
      if (m) media.push({ type: "image", src: "https://media.giphy.com/media/" + m[1] + "/giphy.gif" });
    });
    return media;
  }

  /* ---- Linkify, clean raw IDs, and render Twitter/X cards ---- */
  function fmtContent(t) {
    t = S(t);
    // Strip raw <@ID> Discord mentions
    t = t.replace(/<@!?(\d{15,20})>/g, '<span class="dsc-mention">@user</span>');
    // Strip custom emoji <:name:ID>
    t = t.replace(/<a?:\w+:\d+>/g, '');
    
    // Linkify other URLs (excluding Twitter/X status links, which get their own card below)
    t = t.replace(/(https?:\/\/[^\s<"'\(\)]+)/g, function(u) {
      if (u.match(/\.(png|jpg|jpeg|gif|webp)/i) || u.includes("giphy.com") || u.includes("tradingview.com/x/")) return u;
      if (u.match(/https?:\/\/(?:twitter|x)\.com\/[A-Za-z0-9_]+\/status\/\d+/i)) return u;
      return `<a href="${u}" target="_blank">${u}</a>`;
    });

    // Render Twitter/X link as custom layout, including trailing query parameters
    t = t.replace(/(https?:\/\/(?:twitter|x)\.com\/[A-Za-z0-9_]+\/status\/\d+[^\s<"'\(\)]*)/gi, function(u) {
      return `<div class="dsc-x-card"><span class="dsc-x-icon">𝕏</span> <a href="${u}" target="_blank">${u}</a></div>`;
    });

    // Highlight @mentions
    t = t.replace(/@(\w[\w]{1,30})/g, '<span class="dsc-mention">@$1</span>');
    return t;
  }

  /* ---- Dynamic Categories from Database / platform_config ---- */
  var CAT_ORDER = [
    "GETTING STARTED",
    "COMMUNITY HUB",
    "TRADING",
    "THE LORD OF ENTRY",
    "CHAOSS",
    "DEGEN DAVID",
    "SKILLED TRADERS",
    "HACKER OPS",
    "CHART PRIME",
    "MORE",
    "OTHER"
  ];

  /* ---- Unread Tracking ---- */
  function getUnread(ch) {
    var u = state.unread[ch];
    if (!u) { u = { count: 0, last: 0 }; state.unread[ch] = u; }
    return u;
  }

  /* ---------- TREE ---------- */
  async function loadTree() {
    try {
      var r = await fetch(API + "/tree?source=discord");
      var j = await r.json();
      state.tree = j.tree || [];
      
      // Load followed list
      var fRes = await fetch(API + "/followed_traders");
      var fJson = await fRes.json();
      state.followedTraders = fJson.followed || [];
      
      renderTree();
    } catch(e) { console.error("tree load failed:", e); }
  }

  function renderTree() {
    var side = document.getElementById("dscSidebar"); if (!side) return;
    side.innerHTML = "";
    var cats = {}, order = [];
    state.tree.forEach(function(n) {
      if (n.entity_type !== "channel") return;
      var c = n.discord_category || "OTHER";
      if (!cats[c]) { cats[c] = []; order.push(c); }
      cats[c].push(n);
    });
    
    order.sort(function(a, b) {
      var idxA = 99, idxB = 99;
      for (var i = 0; i < CAT_ORDER.length; i++) {
        if (a.toUpperCase().indexOf(CAT_ORDER[i]) !== -1) idxA = i;
        if (b.toUpperCase().indexOf(CAT_ORDER[i]) !== -1) idxB = i;
      }
      return idxA - idxB;
    });
    
    order.forEach(function(cn) {
      side.appendChild(E("div", "dsc-cat", cn));
      cats[cn].forEach(chanRow);
    });
  }

  function chanRow(n){
    if (state.filter && (n.name || "").toLowerCase().indexOf(state.filter) === -1) return;
    var d = (n.name || "").replace(/^[^a-zA-Z0-9]+/,"");
    var u = getUnread(n.name), has = u.count > 0;
    
    var row = E("div", "dsc-chan" + (state.channel === n.name ? " dsc-selected" : "") + (has ? " dsc-unread" : ""));
    
    // Custom SVG Icon instead of `#`
    var iconSpan = E("span", "dsc-hash");
    iconSpan.innerHTML = getChannelIcon(n.name);
    row.appendChild(iconSpan);

    row.appendChild(E("span", "", d));
    if (has) {
      var b = E("span", "dsc-count dsc-mention");
      b.textContent = u.count > 99 ? "99+" : String(u.count);
      row.appendChild(b);
    }
    row.addEventListener("click", function() { selectChan(n.name, d); });
    document.getElementById("dscSidebar").appendChild(row);
  }
  function getUnread(ch){var u=state.unread[ch];if(!u){u={count:0,last:0};state.unread[ch]=u;}return u;}

  function getProperName(authorHandle, it) {
    if (it && it.metadata) {
      try {
        var meta = typeof it.metadata === "string" ? JSON.parse(it.metadata) : it.metadata;
        if (meta.sender_name) return meta.sender_name;
      } catch(e) {}
    }
    var lower = String(authorHandle || "").toLowerCase().trim();
    if (state.nameMap && state.nameMap[lower]) {
      return state.nameMap[lower];
    }
    return authorHandle;
  }

  /* ---------- FEED ---------- */
  function selectChan(name, dis) {
    state.channel = name;
    state.pinnedBottom = true;
    state.seenTopId = 0;
    state.seenTopTime = 0;
    state.seenBottomId = 0;
    state.seenBottomTime = 0;
    if (state.unread[name]) { state.unread[name].count = 0; }
    try { localStorage.setItem("dsc_lastChannel", name); } catch(e){}
    renderTree();
    document.getElementById("dscTitle").textContent = "# " + (dis || name);
    document.getElementById("dscRiver").innerHTML = "";
    loadFeed(true);
  }

  var _busy = false;
  function loadFeed(replace, loadOlder) {
    if (!state.channel || _busy) return;
    _busy = true;

    var limit = 50;
    var url = API + "/feed?source=discord&channel=" + encodeURIComponent(state.channel) + "&limit=" + limit;
    if (state.followingOnly) url += "&following_only=true";
    
    if (loadOlder && state.seenBottomId) {
      url += "&before_id=" + state.seenBottomId;
    } else if (!replace && state.seenTopId) {
      url += "&after_id=" + state.seenTopId;
    } else if (replace) {
      // For fresh load, the backend now uses ORDER BY published_at DESC, id DESC
    }

    fetch(url).then(function(r) { return r.json(); }).then(function(j) {
      var items = j.items || [];
      
      // Update dynamic nameMap
      items.forEach(function(it) {
        var handle = String(it.author || "").toLowerCase().trim();
        var meta = {};
        if (it.metadata) {
          try {
            meta = typeof it.metadata === "string" ? JSON.parse(it.metadata) : it.metadata;
          } catch(e) {}
        }
        var nick = meta.sender_name;
        if (handle && nick) {
          state.nameMap[handle] = nick;
        }
      });

      var river = document.getElementById("dscRiver"); if (!river) { _busy = false; return; }
      
      var priorHeight = river.scrollHeight;
      var priorScroll = river.scrollTop;

      if (replace) {
        river.innerHTML = "";
        state.seenTopId = 0;
        state.seenBottomId = 0;
      }

      // Sort items by date ASC for rendering
      var renderItems = items.sort(function(a, b) {
        var da = new Date(a.published_at || a.collected_at).getTime();
        var db = new Date(b.published_at || b.collected_at).getTime();
        return da - db;
      });
      var prevAuthor = null;
      var lastDateStr = null;

      renderItems.forEach(function(it) {
        // Dynamically update AI badge on existing rendered items when interpretation arrives
        var existingEl = river.querySelector('[data-id="' + it.id + '"]');
        if (existingEl) {
          if (it.signal_interpretation_id && !existingEl.querySelector('.dsc-ai-pill')) {
            var hl = existingEl.querySelector('.dsc-header-line');
            if (hl) {
              var aiPill = E("span", "dsc-ai-pill", "🤖 AI ANALYZED");
              hl.insertBefore(aiPill, hl.querySelector('.dsc-timestamp'));
            }
            existingEl.style.cursor = "pointer";
            existingEl.className += " dsc-msg-clickable";
            if (typeof window.openCall === "function") {
              existingEl.addEventListener("click", function(e) {
                e.stopPropagation();
                window.openCall(it.signal_interpretation_id, it.id);
              });
            }
          }
          return;
        }

        // Day/Date Separator
        var msgDate = new Date(it.published_at || it.collected_at);
        var dateStr = msgDate.toLocaleDateString([], { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
        if (dateStr !== lastDateStr) {
          var sep = E("div", "dsc-date-separator");
          var line = E("div", "dsc-date-line");
          var badge = E("span", "dsc-date-badge", dateStr);
          sep.appendChild(line); sep.appendChild(badge);
          
          if (loadOlder) river.insertBefore(sep, river.firstChild);
          else river.appendChild(sep);
          lastDateStr = dateStr;
        }

        var gs = it.author !== prevAuthor;
        var wrap = renderMsg(it, gs);
        wrap.setAttribute("data-id", it.id);

        if (loadOlder) {
          river.insertBefore(wrap, river.firstChild);
        } else {
          river.appendChild(wrap);
        }

        prevAuthor = it.author;

        if (!state.seenBottomId) state.seenBottomId = it.id;
        if (!state.seenTopId) state.seenTopId = it.id;
        
        var itTime = new Date(it.published_at || it.collected_at).getTime();
        var topTime = state.seenTopTime || 0;
        var bottomTime = state.seenBottomTime || Infinity;

        if (itTime > topTime) {
          state.seenTopId = it.id;
          state.seenTopTime = itTime;
        }
        if (itTime < bottomTime) {
          state.seenBottomId = it.id;
          state.seenBottomTime = itTime;
        }
      });

      if (loadOlder) {
        river.scrollTop = priorScroll + (river.scrollHeight - priorHeight);
      } else if (state.pinnedBottom || replace) {
        river.scrollTop = river.scrollHeight;
        state.pinnedBottom = false;
      }

      _busy = false;
      state.loadingPrev = false;
      updateJumpBanner();
    }).catch(function(e) { console.error("feed failed:", e); _busy = false; state.loadingPrev = false; });
  }

  function getUserBadges(author) {
    var lower = String(author || "").toLowerCase();
    var res = { tag: "", roleIcon: "" };
    if (lower === "whatley91") {
      res.tag = "SX";
    } else if (lower === "bradda" || lower === "bradda.kom_14033") {
      res.tag = "DFTU";
    } else if (lower === "kaptain  insane-o" || lower === "kaptain insane-o" || lower === "wm28282828") {
      res.tag = "OCT";
    } else if (lower && !lower.match(/^(mee6|monitorss|charthackers predictions bot|charthackers bot)$/)) {
      res.tag = "HKRS";
    }
    if (lower === "luke" || lower === "womprat (tommy)" || lower === "womprat") {
      res.roleIcon = "⭐";
    } else if (lower === "trader j (emu)" || lower === "trader j" || lower === "emutrading" || lower === "nagelthebagel" || lower === "thenagel") {
      res.roleIcon = "🎯";
    } else if (lower === "cryptomilf") {
      res.roleIcon = "👑";
    }
    return res;
  }

  function renderMsg(it, groupStart) {
    var wrap = E("div", "dsc-msg" + (groupStart ? " dsc-groupstart" : ""));
    
    // reply block
    if (it.reply_to_msg_id) {
      var rp = E("div", "dsc-reply");
      var rsp = E("div", "dsc-reply-spine"); rp.appendChild(rsp);
      
      var displayReplyAuthor = it.reply_to_author_display_name || getProperName(it.reply_to_author);
      var rav = E("div", "dsc-reply-avatar"); rav.textContent = (displayReplyAuthor || "?")[0].toUpperCase(); rp.appendChild(rav);
      rp.appendChild(E("span", "dsc-reply-author", "@" + displayReplyAuthor));
      rp.appendChild(E("span", "dsc-reply-text", S(it.reply_to_content || "")));
      wrap.appendChild(rp);
    }

    if (!groupStart) {
      var tsCompact = E("span", "dsc-hover-timestamp", fmtTimeCompact(it.published_at || it.collected_at));
      wrap.appendChild(tsCompact);
    }

    if (groupStart) {
      var meta = {};
      if (it.metadata) {
        try {
          meta = typeof it.metadata === "string" ? JSON.parse(it.metadata) : it.metadata;
        } catch(e) {}
      }
      var displayName = it.author_display_name || meta.sender_name || getProperName(it.author, it);

      var av = E("div", "dsc-avatar");
      av.style.background = "#" + colorHash(it.author || "?");
      av.textContent = (displayName || "?")[0].toUpperCase();
      wrap.appendChild(av);
      
      var hl = E("div", "dsc-header-line");
      
      var un = E("span", "dsc-username", S(displayName));
      var lower = String(it.author || "").toLowerCase();
      if (lower === "luke") {
        un.className += " dsc-gradient";
        un.style.setProperty("--grad-1", "#096bec");
        un.style.setProperty("--grad-2", "#34b2ec");
        un.style.setProperty("--grad-3", "#096bec");
      } else if (lower === "havokk") {
        un.className += " dsc-gradient";
        un.style.setProperty("--grad-1", "#4cadd0");
        un.style.setProperty("--grad-2", "#b2f9ff");
        un.style.setProperty("--grad-3", "#4cadd0");
      } else if (lower === "trader j (emu)" || lower === "trader j" || lower === "emutrading") {
        un.className += " dsc-gradient dsc-bold-role";
        un.style.setProperty("--grad-1", "#20e3b2");
        un.style.setProperty("--grad-2", "#00bcff");
        un.style.setProperty("--grad-3", "#20e3b2");
      } else if (lower === "nagelthebagel" || lower === "thenagel") {
        un.className += " dsc-gradient";
        un.style.setProperty("--grad-1", "#ff9a00");
        un.style.setProperty("--grad-2", "#ffd200");
        un.style.setProperty("--grad-3", "#ff9a00");
      } else if (lower === "hellfire" || lower === "naum_74390") {
        un.className += " dsc-gradient dsc-bold-role";
        un.style.setProperty("--grad-1", "#ff007f");
        un.style.setProperty("--grad-2", "#b300ff");
        un.style.setProperty("--grad-3", "#ff007f");
      } else if (lower === "cryptomilf") {
        un.className += " dsc-gradient";
        un.style.setProperty("--grad-1", "#9e6bff");
        un.style.setProperty("--grad-2", "#9fc1ff");
        un.style.setProperty("--grad-3", "#9e6bff");
      } else if (lower === "kaptain insane-o" || lower === "kaptain  insane-o" || lower === "wm28282828") {
        un.className += " dsc-gradient dsc-bold-role";
        un.style.setProperty("--grad-1", "#a95bf2");
        un.style.setProperty("--grad-2", "#d9a4ff");
        un.style.setProperty("--grad-3", "#a95bf2");
      } else if (it.author_role_color) {
        un.style.color = it.author_role_color;
      }
      hl.appendChild(un);

      var badges = getUserBadges(it.author);
      if (badges.tag) {
        var chip = E("span", "dsc-tag-chiplet");
        chip.textContent = badges.tag;
        hl.appendChild(chip);
      }
      if (badges.roleIcon) {
        var rIcon = E("span", "dsc-role-icon", badges.roleIcon);
        hl.appendChild(rIcon);
      }

      // Followed Trader indicator (Blue Dot next to timestamp)
      var isFollowed = state.followedTraders.indexOf(it.author) !== -1;
      if (isFollowed) {
        var dot = E("span", "dsc-follow-dot", "•");
        hl.appendChild(dot);
      }

      // AI Analysis Badge if signal_interpretation_id is present
      if (it.signal_interpretation_id) {
        var aiPill = E("span", "dsc-ai-pill", "🤖 AI ANALYZED");
        hl.appendChild(aiPill);
      }

      var ts = E("span", "dsc-timestamp", fmtTime(it.published_at || it.collected_at));
      hl.appendChild(ts);
      wrap.appendChild(hl);
    }

    if (it.content) {
      var ct = E("div", "dsc-content");
      ct.innerHTML = fmtContent(it.content);
      wrap.appendChild(ct);
    }

    // Embed links & uploaded images
    var localPaths = it.local_media_paths || [];
    if (typeof localPaths === "string") { try { localPaths = JSON.parse(localPaths); } catch(e) { localPaths = []; } }
    if (!Array.isArray(localPaths)) localPaths = [];

    var allMedia = extractMedia(it.content || "").concat(localPaths.map(function(p){ 
      return {type:"image", src: API + "/media/" + encodeURI(p)}; 
    }));

    allMedia.forEach(function(media) {
      var bx = E("div", "dsc-attach");
      var im = E("img"); im.src = media.src; im.loading = "lazy";
      im.addEventListener("click", function(e) { e.stopPropagation(); openLB(im.src); });
      bx.appendChild(im); wrap.appendChild(bx);
    });

    // AI Drawer Slideover Bridge on click
    if (it.signal_interpretation_id && typeof window.openCall === "function") {
      wrap.addEventListener("click", function(e) {
        e.stopPropagation();
        window.openCall(it.signal_interpretation_id, it.id);
      });
      wrap.style.cursor = "pointer";
      wrap.className += " dsc-msg-clickable";
    } else if (it.enrichment && typeof window.openDrawer === "function") {
      wrap.addEventListener("click", function(e) {
        e.stopPropagation();
        openSignalDrawer(it);
      });
      wrap.style.cursor = "pointer";
    }

    return wrap;
  }

  /* ---- Slide-over Drawer Bridge ---- */
  function openSignalDrawer(it) {
    var en = it.enrichment || {};
    var title = "#" + (it.channel_name || "").replace(/^[^a-zA-Z0-9]+/i, "") + " — " + it.author;
    var kicker = it.enrichment_status ? it.enrichment_status.toUpperCase() : "SIGNAL DETAIL";
    
    var html = `
      <div class="dsc-drawer-wrap" style="color:var(--text); font-family:var(--font); padding:16px;">
        <div style="background:#1d1d23; border:1px solid #2b2b34; border-radius:12px; padding:16px; margin-bottom:16px;">
          <h3 style="margin-top:0; color:#fc72ff;">🤖 AI Signal Analysis</h3>
          <p style="font-size:14px; line-height:1.5; color:#c9c9d1;">${esc(en.summary || "Pending analysis")}</p>
          <div style="font-size:12px; color:#8f8f9b; margin-top:8px;"><b>Reasoning:</b> ${esc(en.reason || "")}</div>
        </div>
        <div style="background:#151519; border:1px solid #2b2b34; border-radius:12px; padding:16px;">
          <h3 style="margin-top:0; color:#4c82fb;">📊 Technical Data</h3>
          <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; font-size:13px; margin-top:12px;">
            <div><b>Confidence:</b> ${esc(it.zone_filter_confidence || "—")}</div>
            <div><b>Date Collected:</b> ${fmtTime(it.collected_at)}</div>
            <div><b>Author:</b> <span style="color:#fc72ff">${esc(it.author)}</span></div>
            <div><b>Message ID:</b> ${esc(it.message_id)}</div>
          </div>
          <div style="margin-top:16px;">
            <b>Original Text:</b>
            <pre style="white-space:pre-wrap; background:#0d0d10; border:1px solid #2b2b34; padding:10px; border-radius:8px; font-size:12px; color:#c9c9d1; font-family:var(--mono); margin-top:6px;">${esc(it.content)}</pre>
          </div>
        </div>
      </div>
    `;
    window.openDrawer(title, kicker, html);
  }

  function colorHash(s) {
    var h = 0, i; for (i = 0; i < s.length; i++) h = s.charCodeAt(i) + ((h << 5) - h);
    h = ((h % 360) + 360) % 360; return hslHex(h, 55, 45);
  }
  function hslHex(h, s, l) {
    s /= 100; l /= 100; var a = s * Math.min(l, 1 - l);
    var f = function (n) { var k = (n + h / 30) % 12, c = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1)); return Math.round(c * 255).toString(16).padStart(2, "0"); };
    return f(0) + f(8) + f(4);
  }
  function fmtTime(iso) {
    try {
      if (!iso) return "";
      // Ensure ISO string is treated as UTC if it doesn't have a timezone offset
      var timestamp = iso;
      if (typeof iso === "string" && !iso.endsWith("Z") && !iso.includes("+")) {
        timestamp += "Z";
      }
      var d = new Date(timestamp);
      var now = new Date();
      var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      var yesterday = new Date(today); yesterday.setDate(yesterday.getDate() - 1);
      
      var timeStr = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
      var msgDate = new Date(d.getFullYear(), d.getMonth(), d.getDate());
      
      if (msgDate.getTime() === today.getTime()) {
        return "Today at " + timeStr;
      } else if (msgDate.getTime() === yesterday.getTime()) {
        return "Yesterday at " + timeStr;
      } else {
        return d.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }) + " " + timeStr;
      }
    } catch (e) { return ""; }
  }
  function fmtTimeCompact(iso) {
    try {
      if (!iso) return "";
      var timestamp = iso;
      if (typeof iso === "string" && !iso.endsWith("Z") && !iso.includes("+")) {
        timestamp += "Z";
      }
      var d = new Date(timestamp);
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    } catch (e) { return ""; }
  }
  function openLB(src) {
    var lb = document.getElementById("dscLightbox");
    if (!lb) { lb = E("div", "dsc-lightbox"); lb.id = "dscLightbox"; var i = E("img"); lb.appendChild(i); lb.addEventListener("click", function () { lb.classList.remove("dsc-open"); }); document.body.appendChild(lb); }
    lb.querySelector("img").src = src; lb.classList.add("dsc-open");
  }

  /* ---- Infinite Scrolling & Jump Banner ---- */
  function handleScroll(e) {
    var river = e.target;
    if (river.scrollTop < 100 && !state.loadingPrev && state.seenBottomId) {
      state.loadingPrev = true;
      loadFeed(false, true);
    }
    var isScrolledUp = (river.scrollHeight - river.scrollTop - river.clientHeight) > 300;
    state.pinnedBottom = !isScrolledUp;
    updateJumpBanner();
  }

  function updateJumpBanner() {
    var b = document.getElementById("dscJumpBanner"); if (!b) return;
    if (!state.pinnedBottom && state.channel) {
      b.classList.add("active");
    } else {
      b.classList.remove("active");
    }
  }

  function jumpToBottom() {
    var river = document.getElementById("dscRiver"); if (!river) return;
    river.scrollTop = river.scrollHeight;
    state.pinnedBottom = true;
    updateJumpBanner();
  }

  /* ---- poll ---- */
  var _poll = null;
  function startPoll() { stopPoll(); _poll = setInterval(pollUnread, 3000); }
  function stopPoll() { if (_poll) clearInterval(_poll); _poll = null; }
  function pollUnread() {
    var url = API + "/feed?source=discord&limit=500";
    if (state.followingOnly) url += "&following_only=true";

    fetch(url).then(function (r) { return r.json(); }).then(function (j) {
      var items = j.items || [], counts = {};
      items.forEach(function (it) {
        var ch = it.channel_name; if (!ch) return;
        if (!counts[ch]) counts[ch] = { c: 0, l: 0 };
        counts[ch].c++; if (it.id > (counts[ch].l || 0)) counts[ch].l = it.id;

        // Update dynamic nameMap
        var handle = String(it.author || "").toLowerCase().trim();
        var meta = {};
        if (it.metadata) {
          try {
            meta = typeof it.metadata === "string" ? JSON.parse(it.metadata) : it.metadata;
          } catch(e) {}
        }
        var nick = meta.sender_name;
        if (handle && nick) {
          state.nameMap[handle] = nick;
        }
      });
      Object.keys(counts).forEach(function (ch) {
        var u = getUnread(ch);
        var c = counts[ch];
        if (u.last === 0) {
          u.last = c.l;
          u.count = 0;
          if (state.channel === ch) {
            state.seenTopId = c.l;
          }
          return;
        }
        if (state.channel === ch) {
          u.count = 0;
          u.last = c.l;
          if (state.seenTopId && c.l > state.seenTopId) {
            loadFeed(true);
          }
          return;
        }
        if (c.l > u.last) {
          var newMsgsCount = items.filter(function(it){ return it.channel_name === ch && it.id > u.last; }).length;
          u.count += newMsgsCount;
          u.last = c.l;
        }
      });
      renderTree();
    }).catch(function (e) { });
  }

  /* ---- config ---- */
  var MO = [["none", "None"], ["text", "Text"], ["text_images", "Text+img"], ["everything", "All"]];
  function renderConfig() {
    var h = document.getElementById("dscConfig"); if (!h) return; h.innerHTML = "";
    var wrap = E("div", "dsc-cfg-wrap");
    // Channels column
    var col1 = E("div", "dsc-cfg-col");
    col1.appendChild(E("div", "dsc-cat", "CHANNELS"));
    state.tree.filter(function (n) { return n.entity_type === "channel"; }).forEach(function (n) {
      var r = E("div", "dsc-cfg-row");
      var c = E("input"); c.type = "checkbox"; c.checked = !!n.enabled; c.addEventListener("change", function () { sc(n.id, { enabled: c.checked }, r); }); r.appendChild(c);
      r.appendChild(E("span", "dsc-cfg-name", "#" + (n.name || "").replace(/^[^a-zA-Z0-9]+/, "")));
      var s = E("select", "dsc-cfg-media"); MO.forEach(function (v) { var o = E("option", "", v[1]); o.value = v[0]; if ((n.media_policy || "text_images") === v[0]) o.selected = true; s.appendChild(o); });
      s.addEventListener("change", function () { sc(n.id, { media_policy: s.value }, r); }); r.appendChild(s);
      var e = E("span", "dsc-cfg-echo"); e.textContent = (n.enabled ? "on" : "off") + " \u00b7 " + (n.media_policy || "text_images"); r.appendChild(e); col1.appendChild(r);
    });
    wrap.appendChild(col1);
    // Traders column — add to DOM immediately, populate after fetch
    var col2 = E("div", "dsc-cfg-col");
    col2.appendChild(E("div", "dsc-cat", "TRADERS"));
    var tSearch = E("input", "dsc-cfg-search");
    tSearch.placeholder = "Filter traders...";
    tSearch.addEventListener("input", function() {
      var q = tSearch.value.toLowerCase();
      col2.querySelectorAll(".dsc-cfg-row").forEach(function(r) {
        r.style.display = r.textContent.toLowerCase().indexOf(q) === -1 ? "none" : "";
      });
    });
    col2.appendChild(tSearch);
    wrap.appendChild(col2);
    h.appendChild(wrap);
    fetch(API + "/user_config").then(function(r){return r.json();}).then(function(j){
      (j.traders || []).forEach(function(t){
        var r = E("div", "dsc-cfg-row");
        var c = E("input"); c.type = "checkbox"; c.checked = !!t.is_tracked; c.addEventListener("change", function () { saveTrader(t.id, { is_tracked: c.checked }, r); }); r.appendChild(c);
        r.appendChild(E("span", "dsc-cfg-name", "@" + (t.display_name || t.handle).substring(0,20)));
        var s = E("select", "dsc-cfg-media"); MO.forEach(function (v) { var o = E("option", "", v[1]); o.value = v[0]; if ((t.tracked_media_types || "all") === v[0]) o.selected = true; s.appendChild(o); });
        s.addEventListener("change", function () { saveTrader(t.id, { tracked_media_types: s.value }, r); }); r.appendChild(s);
        var originalMT = t.tracked_media_types || "all"; var e = E("span", "dsc-cfg-echo"); e.textContent = (t.is_tracked ? "on" : "off") + " \u00b7 " + originalMT; e.setAttribute("data-mt", originalMT); r.appendChild(e); col2.appendChild(r);
      });
    });
  }
  function saveTrader(id, p, re) {
    if (re) re.style.opacity = "0.5";
    fetch(API + "/user_config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(Object.assign({trader_id:id}, p))})
      .then(function(r){return r.json();}).then(function(){
        // Update echo text in-place instead of full reload
        if (re) {
          re.style.opacity = "";
          var echo = re.querySelector(".dsc-cfg-echo");
          if (echo) echo.textContent = (p.is_tracked !== undefined ? (p.is_tracked ? "on" : "off") : (re.querySelector("input")?.checked ? "on" : "off")) + " · " + (p.tracked_media_types || echo.getAttribute("data-mt") || "all");
        }
      });
  }
  function sc(id, p, re) { if (re) re.style.opacity = "0.5"; fetch(API + "/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.assign({ source_id: id }, p)) }).then(function (r) { return r.json(); }).then(function (j) { var n = state.tree.find(function (x) { return x.id === id; }); if (n) Object.assign(n, p); renderConfig(); if (re) re.style.opacity = ""; }); }
  function setMode(m) { document.getElementById("dscFeedView").style.display = (m === "feed") ? "" : "none"; document.getElementById("dscConfigView").style.display = (m === "config") ? "" : "none"; if (m === "config") renderConfig(); }

  /* ---- boot ---- */
  function boot() {
    var s = document.getElementById("dscSearch"); if (s) s.addEventListener("input", function () { state.filter = s.value.trim().toLowerCase(); renderTree(); });
    var mf = document.getElementById("dscModeFeed"), mc = document.getElementById("dscModeConfig");
    if (mf) mf.addEventListener("click", function () { mf.classList.add("dsc-modebtn-active"); mc.classList.remove("dsc-modebtn-active"); setMode("feed"); });
    if (mc) mc.addEventListener("click", function () { mc.classList.add("dsc-modebtn-active"); mf.classList.remove("dsc-modebtn-active"); setMode("config"); });
    
    // Wire Toggle Slider (Following Only vs All) — persistent
    var fl = document.getElementById("dscModeFollow");
    if (fl) {
      // Restore saved state
      try { var saved = localStorage.getItem("dsc_followingOnly"); if (saved === "true") { fl.checked = true; state.followingOnly = true; } } catch(e) {}
      fl.addEventListener("change", function() {
        state.followingOnly = fl.checked;
        try { localStorage.setItem("dsc_followingOnly", fl.checked ? "true" : "false"); } catch(e) {}
        state.seenTopId = 0;
        state.seenBottomId = 0;
        loadFeed(true);
      });
    }

    // Scroll listeners for Keyset Pagination & Pinned bottom
    var river = document.getElementById("dscRiver");
    if (river) river.addEventListener("scroll", handleScroll);

    // Jump Banner click
    var banner = document.getElementById("dscJumpBanner");
    if (banner) banner.addEventListener("click", jumpToBottom);

    loadTree().then(function () {
      var lc = null; try { lc = localStorage.getItem("dsc_lastChannel"); } catch (e) { }
      if (lc && lc !== "null") { var d = lc.replace(/^[^a-zA-Z0-9]+/, ""); selectChan(lc, d); }
    }); startPoll();
  }
  window.DiscordFeed = { boot: boot, loadTree: loadTree, selectChannel: selectChan, stop: stopPoll, setMode: setMode, renderConfig: renderConfig };
})();
