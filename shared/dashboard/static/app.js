/* shared/dashboard/static/app.js */
console.log("Dashboard JS loading...");

const state = {
    token: localStorage.getItem("tickles_token") || null,
    currentTab: 'overview',
    companyFilter: 'all',
    autoRefresh: true,
    refreshInterval: null,
    selectedTraderId: null,
    queueWS: null,
    learningWindow: '1m',     // PHASE_Y §11 Q3: default 30d shown as "1M"
    learningDimension: '',    // memory-feed dimension filter (empty = all)
    newsWindow: '24h',        // PHASE_X.4 — news feed window: 24h / 7d / 30d
    newsSource: '',           // news source filter (empty = all)
    newsHasMedia: ''          // tri-state: '' (any), 'true', 'false'
};

async function api(path, opts = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (state.token) headers["Authorization"] = "Bearer " + state.token;
    
    // Use relative paths (base tag handles the prefix)
    const cleanPath = path.startsWith('/') ? path.slice(1) : path;
    const url = new URL(cleanPath, document.baseURI);
    if (state.companyFilter && state.companyFilter !== 'all') {
        url.searchParams.set('company', state.companyFilter);
    }

    const resp = await fetch(url, Object.assign({ headers }, opts));
    if (resp.status === 401) {
        setToken(null);
        showSection('login');
        throw new Error("Unauthorized");
    }
    const body = await resp.json();
    if (!resp.ok) throw body;
    return body;
}

function setToken(t) {
    state.token = t;
    if (t) localStorage.setItem("tickles_token", t);
    else localStorage.removeItem("tickles_token");
}

function showSection(id) {
    console.log(`Showing section: ${id}`);
    document.getElementById('login').classList.add('hidden');
    document.getElementById('app').classList.add('hidden');
    const target = document.getElementById(id);
    if (target) target.classList.remove('hidden');
    else console.error(`Section not found: ${id}`);
}

function switchTab(tabId, params = {}) {
    state.currentTab = tabId;
    
    // Show/hide Trader Drill tab link
    const drillLink = document.querySelector('nav a[data-tab="trader-drill"]');
    if (tabId === 'trader-drill') {
        drillLink.classList.remove('hidden');
        if (params.traderId) state.selectedTraderId = params.traderId;
    } else if (!state.selectedTraderId) {
        drillLink.classList.add('hidden');
    }

    document.querySelectorAll('nav a').forEach(a => {
        a.classList.toggle('active', a.dataset.tab === tabId);
    });
    document.querySelectorAll('.tab-content').forEach(content => {
        content.classList.toggle('active', content.id === `tab-${tabId}`);
    });
    
    // Handle WebSocket for Queue tab
    if (tabId === 'queue') {
        connectQueueWS();
    } else if (state.queueWS) {
        state.queueWS.close();
        state.queueWS = null;
    }

    refresh();
}

async function refresh() {
    if (!state.token) return;
    
    try {
        const snap = await api('/api/snapshot');
        renderStats(snap);
        renderTab(state.currentTab, snap);
        
        // Handle anchor highlighting if present
        handleAnchors();
    } catch (e) {
        console.error("Refresh failed", e);
    }
}

function renderStats(snap) {
    const numOrZero = (v) => {
        const n = Number(v);
        return isFinite(n) ? n : 0;
    };
    const signals = snap.signals_today_count ?? 0;
    const cost = numOrZero(snap.api_cost_today_usd);
    const posCount = snap.open_positions_count ?? 0;
    const pnl = numOrZero(snap.open_positions_unrealized_pnl);
    const ingest = snap.ingest_depth ?? 0;

    document.getElementById('stat-signals').textContent = signals;
    document.getElementById('stat-cost').textContent = `$${cost.toFixed(2)}`;
    document.getElementById('stat-pos').textContent = posCount;
    document.getElementById('stat-pnl').textContent = `$${pnl.toFixed(2)}`;
    document.getElementById('stat-pnl').className = 'stat-value ' + (pnl >= 0 ? 'pnl pos' : 'pnl neg');
    document.getElementById('stat-ingest').textContent = ingest;
}

function renderTab(tabId, snap) {
    const container = document.getElementById(`tab-${tabId}`);
    if (!container) return;

    switch (tabId) {
        case 'overview':
            renderOverview(snap);
            break;
        case 'leaderboard':
            renderLeaderboard(snap.leaderboard);
            break;
        case 'signals':
            renderSignals(snap.signals);
            break;
        case 'positions':
            renderPositions(snap.positions);
            break;
        case 'interpretations':
            renderInterpretations(snap.interpretations);
            break;
        case 'trader-drill':
            renderTraderDrill();
            break;
        case 'queue':
            // Handled by WebSocket
            break;
        case 'learning':
            renderLearning();
            break;
        case 'news':
            renderNews();
            break;
    }
}

// ---------------------------------------------------------------------------
// Phase Y — Learning tab
// ---------------------------------------------------------------------------

// UI label → numeric window-days. Mirrors shared/dashboard/learning_routes.py
// _WINDOW_LABELS so the JS and the server agree on "1M" === 30.
const LEARNING_WINDOW_DAYS = { '7d': 7, '14d': 14, '1m': 30 };

function _learningWindowDays() {
    return LEARNING_WINDOW_DAYS[state.learningWindow] || 30;
}

async function renderLearning() {
    // Hash round-trip — accept #window=14d and update state before fetching.
    const hash = window.location.hash || '';
    const m = hash.match(/window=([a-zA-Z0-9]+)/);
    if (m && LEARNING_WINDOW_DAYS[m[1].toLowerCase()]) {
        state.learningWindow = m[1].toLowerCase();
    }
    _highlightLearningWindow();

    // Fetch the four data sources in parallel — providers run under a 250ms
    // budget server-side so this whole render typically completes <300ms.
    const winLabel = state.learningWindow;
    const winDays = _learningWindowDays();
    const dim = state.learningDimension || '';
    const dimQs = dim ? `&dimension=${encodeURIComponent(dim)}` : '';

    let skillRes, brainRes, feedRes, failedRes, guardRes, promptRes;
    try {
        [skillRes, brainRes, feedRes, failedRes, guardRes, promptRes] = await Promise.all([
            api(`/api/learning/skill-summary?window=${winLabel}`),
            api(`/api/learning/agent-brain?window=${winLabel}`),
            api(`/api/learning/memory-feed?window=${winLabel}${dimQs}`),
            api(`/api/learning/failed-trades?window=${winLabel}`),
            api(`/api/learning/guard-activity`),
            api(`/api/learning/prompt-evolution?window=${winDays}&limit=50`)
        ]);
    } catch (e) {
        console.error('Learning tab fetch failed', e);
        const strip = document.getElementById('learning-header-strip');
        if (strip) strip.innerHTML = `<div class="muted">Unable to load learning data.</div>`;
        // Clear stale data from prior render so the user isn't misled by old numbers.
        const feedTbody = document.querySelector('#learning-feed-table tbody');
        if (feedTbody) feedTbody.innerHTML = `<tr><td colspan="5" class="muted">—</td></tr>`;
        const brainBox = document.getElementById('learning-brain-cards');
        if (brainBox) brainBox.innerHTML = `<div class="muted">—</div>`;
        const guardBox = document.getElementById('learning-guard-list');
        if (guardBox) guardBox.innerHTML = `<div class="muted">—</div>`;
        const promptBox = document.getElementById('learning-prompt-timeline');
        if (promptBox) promptBox.innerHTML = `<div class="muted">—</div>`;
        return;
    }

    renderLearningHeader(skillRes.rows || [], failedRes.rows || [], winDays);
    renderMemoryFeed(feedRes.rows || []);
    renderAgentBrainCards(brainRes.rows || []);
    renderGuardActivity(guardRes.rows || []);
    renderPromptEvolution(promptRes.rows || []);
}

function _guardSeverityClass(severity) {
    // Map provider severity to pill class. Unknown values fall back to neutral.
    const s = (severity || '').toLowerCase();
    if (s === 'error') return 'bad';
    if (s === 'warn' || s === 'warning') return 'warn';
    if (s === 'info') return 'ok';
    return '';
}

function renderGuardActivity(rows) {
    const container = document.getElementById('learning-guard-list');
    if (!container) return;
    if (!Array.isArray(rows) || !rows.length) {
        container.innerHTML = `<div class="muted">No guard warnings — all systems nominal.</div>`;
        return;
    }
    // Sort by ts DESC (newest first); rows without ts sort last.
    const sorted = rows.slice().sort((a, b) => {
        const ta = a && a.ts ? Date.parse(a.ts) : 0;
        const tb = b && b.ts ? Date.parse(b.ts) : 0;
        return (tb || 0) - (ta || 0);
    });
    container.innerHTML = sorted.map(r => {
        const sev = _guardSeverityClass(r.severity);
        const kind = r.kind || '—';
        const company = r._company || r.company || '';
        const message = r.message || '';
        const tsRaw = r.ts;
        const ts = tsRaw ? new Date(tsRaw).toLocaleString() : '';
        return `
        <div class="guard-row">
            <div class="guard-head">
                <span class="pill ${sev}">${_esc(r.severity || 'info')}</span>
                ${company ? `<span class="company-tag">${_esc(company)}</span>` : ''}
                <span class="guard-kind">${_esc(kind)}</span>
                ${ts ? `<span class="guard-ts">${_esc(ts)}</span>` : ''}
            </div>
            ${message ? `<div class="guard-msg">${_esc(message)}</div>` : ''}
        </div>`;
    }).join('');
}

function _fmtScore(v) {
    const n = Number(v);
    return isFinite(n) ? n.toFixed(3) : '—';
}

function _fmtDelta(v) {
    const n = Number(v);
    if (!isFinite(n)) return '—';
    const sign = n > 0 ? '+' : '';
    return `${sign}${n.toFixed(3)}`;
}

function _deltaClass(v) {
    const n = Number(v);
    if (!isFinite(n) || n === 0) return '';
    return n > 0 ? 'ok' : 'bad';
}

function renderPromptEvolution(rows) {
    const container = document.getElementById('learning-prompt-timeline');
    if (!container) return;
    if (!Array.isArray(rows) || !rows.length) {
        container.innerHTML = `<div class="muted">No prompt promotions in this window.</div>`;
        return;
    }
    // Sort by logged_at DESC; rows without logged_at sort last.
    const sorted = rows.slice().sort((a, b) => {
        const ta = a && a.logged_at ? Date.parse(a.logged_at) : 0;
        const tb = b && b.logged_at ? Date.parse(b.logged_at) : 0;
        return (tb || 0) - (ta || 0);
    });
    container.innerHTML = sorted.map(r => {
        const actor = r.actor_id || '—';
        const actorType = r.actor_type || '';
        const company = r._company || r.company || '';
        const before = _fmtScore(r.score_before);
        const after = _fmtScore(r.score_after);
        const delta = _fmtDelta(r.delta);
        const deltaCls = _deltaClass(r.delta);
        const note = r.note || '';
        const ts = r.logged_at ? new Date(r.logged_at).toLocaleString() : '';
        const period = r.period_end ? new Date(r.period_end).toLocaleDateString() : '';
        const label = company
            ? `<span class="company-tag">${_esc(company)}</span>${_esc(actor)}`
            : _esc(actor);
        return `
        <div class="prompt-event">
            <div class="prompt-marker ${deltaCls}"></div>
            <div class="prompt-body">
                <div class="prompt-head">
                    <span class="prompt-actor">${label}</span>
                    ${actorType ? `<span class="muted">${_esc(actorType)}</span>` : ''}
                    ${ts ? `<span class="prompt-ts">${_esc(ts)}</span>` : ''}
                </div>
                <div class="prompt-scores">
                    <span class="muted">${_esc(before)}</span>
                    <span class="prompt-arrow">→</span>
                    <span>${_esc(after)}</span>
                    <span class="pill ${deltaCls}">${_esc(delta)}</span>
                    ${period ? `<span class="muted">period ${_esc(period)}</span>` : ''}
                </div>
                ${note ? `<div class="prompt-note">${_esc(note)}</div>` : ''}
            </div>
        </div>`;
    }).join('');
}

function _highlightLearningWindow() {
    document.querySelectorAll('#learning-window-tabs .window-tab').forEach(el => {
        el.classList.toggle('active', el.dataset.window === state.learningWindow);
    });
}

function _scoreClass(score) {
    if (!isFinite(score)) return '';
    if (score >= 0.6) return '';        // ok / accent
    if (score >= 0.4) return 'warn';
    return 'bad';
}

function _outcomePill(outcome) {
    // Map post-mortem outcome strings to pill colour classes. Distinct from
    // _dirPill (which classifies long/short). Empty string = neutral pill.
    const o = (outcome || '').toLowerCase();
    if (o === 'win' || o === 'tp' || o === 'profit') return 'ok';
    if (o === 'loss' || o === 'sl' || o === 'stop' || o === 'failed') return 'bad';
    if (o === 'breakeven' || o === 'be' || o === 'flat') return 'warn';
    return '';
}

function renderLearningHeader(skillRows, failedRows, winDays) {
    const strip = document.getElementById('learning-header-strip');
    if (!strip) return;

    if (!skillRows.length && !failedRows.length) {
        strip.innerHTML = `<div class="muted">No skill or failed-trade data for the last ${winDays} days.</div>`;
        return;
    }

    // Top-3 actors by skill_score (drop nulls).
    const ranked = skillRows
        .map(r => ({
            actor_id: r.actor_id || r.actor || '—',
            actor_type: r.actor_type || '',
            company: r._company || r.company || '',
            score: Number(r.skill_score),
            n: Number(r.n_closed_trades || r.closed_position_count || 0)
        }))
        .filter(r => isFinite(r.score))
        .sort((a, b) => b.score - a.score)
        .slice(0, 3);

    // Failed-trade total across companies.
    let failedTotal = 0;
    let closedTotal = 0;
    for (const r of failedRows) {
        const f = Number(r.failed_count);
        const c = Number(r.total_closed);
        if (isFinite(f)) failedTotal += f;
        if (isFinite(c)) closedTotal += c;
    }
    const hasClosed = closedTotal > 0;
    const failedRatio = hasClosed ? failedTotal / closedTotal : 0;
    let failedClass = 'ok';
    if (hasClosed) {
        if (failedRatio >= 0.5) failedClass = 'bad';
        else if (failedRatio >= 0.2) failedClass = 'warn';
    }
    const failedHtml = hasClosed
        ? `<span class="failed-badge ${failedClass}">⚠ ${failedTotal} / ${closedTotal} failed (${(failedRatio * 100).toFixed(1)}%)</span>`
        : `<span class="muted">No closed trades in window.</span>`;

    const actorsHtml = ranked.length
        ? ranked.map(r => {
            const klass = _scoreClass(r.score);
            const label = r.company
                ? `<span class="company-tag">${_esc(r.company)}</span>${_esc(r.actor_id)}`
                : _esc(r.actor_id);
            return `
                <div class="skill-actor">
                    <div class="name">${label}</div>
                    <div class="score ${klass}">${r.score.toFixed(3)}</div>
                    <div class="meta">${_esc(r.actor_type || '—')} · ${r.n} trades</div>
                </div>`;
        }).join('')
        : `<div class="muted">No actor skill scores yet.</div>`;

    strip.innerHTML = `
        <div class="skill-strip">
            ${actorsHtml}
            <div style="margin-left: auto;">${failedHtml}</div>
        </div>`;
}

function renderMemoryFeed(rows) {
    const tbody = document.querySelector('#learning-feed-table tbody');
    if (!tbody) return;
    if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="5" class="muted">No feed entries.</td></tr>`;
        return;
    }
    tbody.innerHTML = rows.map(r => {
        const ts = r.ts ? new Date(r.ts).toLocaleString() : '—';
        const kind = r.kind || r.event_kind || '—';
        const dim = r.dimension || r.dim || '—';
        const outcome = r.outcome || r.result || '—';
        const source = r.source_id || r.actor_id || r.source || '—';
        const company = r._company || r.company || '';
        const headline = r.headline || r.summary || r.content || '';
        const dirClass = _outcomePill(outcome);
        return `
        <tr class="feed-row">
            <td class="muted" style="white-space: nowrap;">${_esc(ts)}</td>
            <td><span class="pill">${_esc(kind)}</span></td>
            <td>${_esc(dim)}</td>
            <td><span class="pill ${dirClass}">${_esc(outcome)}</span></td>
            <td>
                ${company ? `<span class="company-tag">${_esc(company)}</span>` : ''}
                <span class="feed-meta">${_esc(source)}</span>
                ${headline ? `<div class="feed-content">${_esc(headline)}</div>` : ''}
            </td>
        </tr>`;
    }).join('');
}

function renderAgentBrainCards(rows) {
    const container = document.getElementById('learning-brain-cards');
    if (!container) return;
    if (!rows.length) {
        container.innerHTML = `<div class="muted">No closed trades in this window.</div>`;
        return;
    }
    container.innerHTML = rows.map(r => {
        const wins = Number(r.wins || 0);
        const breakeven = Number(r.breakeven || r.be || 0);
        const losses = Number(r.losses || 0);
        const total = wins + breakeven + losses;
        const winPct = total ? (wins / total) * 100 : 0;
        const bePct = total ? (breakeven / total) * 100 : 0;
        const lossPct = total ? (losses / total) * 100 : 0;
        const actor = r.actor_id || r.actor || '—';
        const actorType = r.actor_type || '';
        const company = r._company || r.company || '';
        const label = company
            ? `<span class="company-tag">${_esc(company)}</span>${_esc(actor)}`
            : _esc(actor);
        return `
        <div class="brain-card">
            <div class="head">
                <span class="actor-id">${label}</span>
                <span class="totals">${total} trades · ${actorType ? _esc(actorType) : ''}</span>
            </div>
            <div class="brain-bar" title="${wins}W / ${breakeven}BE / ${losses}L">
                <span class="seg-win" style="width: ${winPct.toFixed(2)}%;"></span>
                <span class="seg-be"  style="width: ${bePct.toFixed(2)}%;"></span>
                <span class="seg-loss" style="width: ${lossPct.toFixed(2)}%;"></span>
            </div>
            <div class="totals" style="margin-top: 4px;">
                <span style="color: var(--accent);">${wins}W</span> ·
                <span>${breakeven}BE</span> ·
                <span style="color: var(--bad);">${losses}L</span>
            </div>
        </div>`;
    }).join('');
}

// ---------------------------------------------------------------------------
// Phase X.4 — News Feed tab
// ---------------------------------------------------------------------------

// UI label → query value. Mirrors shared/dashboard/news_routes.py
// _WINDOW_LABEL_TO_DAYS so the JS and the server agree on labels.
const NEWS_WINDOW_LABELS = ['24h', '7d', '30d'];
const NEWS_DEFAULT_LIMIT = 100;       // mirrors news_provider.NEWS_DEFAULT_LIMIT
const NEWS_TABLE_COLSPAN = 6;         // matches index.html news-feed-table cols

function _highlightNewsWindow() {
    document.querySelectorAll('#news-window-tabs .window-tab').forEach(el => {
        el.classList.toggle('active', el.dataset.window === state.newsWindow);
    });
}

function _newsSourcePill(source) {
    // Map source label to a pill class for visual differentiation.
    const s = (source || '').toLowerCase();
    if (s === 'discord' || s === 'telegram') return 'ok';
    if (s === 'twitter' || s === 'rss') return 'warn';
    if (s === 'manual') return 'bad';
    return '';
}

function _sentimentClass(sentiment) {
    // Optional sentiment scoring → pill colour. Provider returns either a
    // numeric score or a string label (positive/negative/neutral) — handle both.
    if (sentiment === null || sentiment === undefined || sentiment === '') return '';
    const n = Number(sentiment);
    if (isFinite(n)) {
        if (n > 0.15) return 'ok';
        if (n < -0.15) return 'bad';
        return 'warn';
    }
    const s = String(sentiment).toLowerCase();
    if (s === 'positive' || s === 'bullish') return 'ok';
    if (s === 'negative' || s === 'bearish') return 'bad';
    return 'warn';
}

function _fmtSentiment(sentiment) {
    if (sentiment === null || sentiment === undefined || sentiment === '') return '—';
    const n = Number(sentiment);
    if (isFinite(n)) {
        const sign = n > 0 ? '+' : '';
        return `${sign}${n.toFixed(2)}`;
    }
    return String(sentiment);
}

function _newsHeadlineCell(row) {
    // Combine headline + truncated content snippet into one cell. The
    // provider already truncates content server-side; we just escape and wrap.
    const headline = _esc(row.headline || '—');
    const snippet = row.content ? _esc(row.content) : '';
    const channel = row.channel_name ? `<span class="muted">${_esc(row.channel_name)}</span>` : '';
    const author = row.author ? `<span class="muted">${_esc(row.author)}</span>` : '';
    const meta = [channel, author].filter(Boolean).join(' · ');
    return `
        <div class="feed-headline">${headline}</div>
        ${snippet ? `<div class="feed-content">${snippet}</div>` : ''}
        ${meta ? `<div class="feed-meta">${meta}</div>` : ''}`;
}

function renderNewsRows(rows) {
    // Populate the news-feed table body and the row-count badge.
    const tbody = document.querySelector('#news-feed-table tbody');
    const countEl = document.getElementById('news-row-count');
    if (!tbody) return;
    if (!Array.isArray(rows) || !rows.length) {
        tbody.innerHTML = `<tr><td colspan="${NEWS_TABLE_COLSPAN}" class="muted">No news in this window.</td></tr>`;
        if (countEl) countEl.textContent = '0 items';
        return;
    }
    tbody.innerHTML = rows.map(r => {
        const ts = r.collected_at ? new Date(r.collected_at).toLocaleString() : '—';
        const source = r.source || '—';
        const srcCls = _newsSourcePill(source);
        const instruments = Array.isArray(r.instruments) && r.instruments.length
            ? r.instruments.slice(0, 6).map(s => `<span class="pill">${_esc(s)}</span>`).join(' ')
            : '<span class="muted">—</span>';
        const sentCls = _sentimentClass(r.sentiment);
        const sentTxt = _fmtSentiment(r.sentiment);
        const mediaCount = Number(r.media_count || 0);
        const mediaTxt = r.has_media
            ? `<span class="pill ok">${mediaCount || 1}</span>`
            : '<span class="muted">—</span>';
        return `
        <tr class="feed-row">
            <td class="muted" style="white-space: nowrap;">${_esc(ts)}</td>
            <td><span class="pill ${srcCls}">${_esc(source)}</span></td>
            <td>${_newsHeadlineCell(r)}</td>
            <td>${instruments}</td>
            <td><span class="pill ${sentCls}">${_esc(sentTxt)}</span></td>
            <td>${mediaTxt}</td>
        </tr>`;
    }).join('');
    if (countEl) countEl.textContent = `${rows.length} items`;
}

async function renderNews() {
    _highlightNewsWindow();
    const tbody = document.querySelector('#news-feed-table tbody');
    // Always replace the body with "Loading…" before fetching — the default
    // markup ships one placeholder row, so a `children.length` check would
    // never fire on subsequent refreshes and stale rows would linger.
    if (tbody) {
        tbody.innerHTML = `<tr><td colspan="${NEWS_TABLE_COLSPAN}" class="muted">Loading…</td></tr>`;
    }
    const params = new URLSearchParams();
    params.set('window', state.newsWindow);
    if (state.newsSource) params.set('source', state.newsSource);
    if (state.newsHasMedia !== '') params.set('has_media', state.newsHasMedia);
    params.set('limit', String(NEWS_DEFAULT_LIMIT));
    let res;
    try {
        res = await api(`/api/news/feed?${params.toString()}`);
    } catch (e) {
        console.error('News feed fetch failed', e);
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="${NEWS_TABLE_COLSPAN}" class="muted">Unable to load news.</td></tr>`;
        }
        const countEl = document.getElementById('news-row-count');
        if (countEl) countEl.textContent = '—';
        return;
    }
    renderNewsRows((res && res.rows) || []);
}

function renderOverview(snap) {
    const svcList = document.getElementById('svc-list');
    svcList.innerHTML = (snap.services || []).map(s => {
        const kindClass = `svc-${s.kind || 'daemon'}`;
        const hb = s.heartbeat;
        let hbDisplay = '<span class="muted">—</span>';
        if (hb) {
            const statusClass = hb.is_stale ? 'bad' : 'ok';
            const timeStr = hb.last_seen_seconds < 60
                ? `${hb.last_seen_seconds}s`
                : `${Math.floor(hb.last_seen_seconds / 60)}m`;
            hbDisplay = `<span class="pill ${statusClass}">${timeStr}</span>`;
        }
        const phase = s.tags?.phase || s.phase || '—';
        return `<tr>
            <td>${s.name}</td>
            <td><span class="pill ${kindClass}">${s.kind || 'daemon'}</span></td>
            <td>${phase}</td>
            <td>${s.enabled_on_vps ? '✅' : '❌'}</td>
            <td>${hbDisplay}</td>
        </tr>`;
    }).join('');
}

function renderLeaderboard(data) {
    const tbody = document.querySelector('#tab-leaderboard tbody');
    tbody.innerHTML = (data || []).map(r => {
        const actorDisplay = r.actor_type === 'trader'
            ? `<a href="#" onclick="switchTab('trader-drill', {traderId: ${r.trader_profile_id}}); return false;">${r.actor_id}</a>`
            : r.actor_id;
        return `
        <tr>
            <td>${r.rank}</td>
            <td><span class="company-tag">${r._company}</span>${actorDisplay}</td>
            <td>${r.actor_type}</td>
            <td>${(r.edge_score || 0).toFixed(4)}</td>
            <td>${r.closed_position_count}</td>
        </tr>
    `}).join('');
}

function _fmtNum(v, digits = 2) {
    if (v === null || v === undefined || v === '') return '—';
    const n = Number(v);
    if (!isFinite(n)) return '—';
    return n.toFixed(digits);
}

function _esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '\u0026amp;')
        .replace(/</g, '\u0026lt;')
        .replace(/>/g, '\u0026gt;')
        .replace(/"/g, '\u0026quot;')
        .replace(/'/g, '\u0026#39;');
}

function _dirPill(dir) {
    const d = (dir || '').toLowerCase();
    if (d === 'long' || d === 'buy') return 'ok';
    if (d === 'short' || d === 'sell') return 'bad';
    return '';
}

function _sourceUrlFromMeta(meta) {
    if (!meta) return null;
    let m = meta;
    if (typeof m === 'string') {
        try { m = JSON.parse(m); } catch { return null; }
    }
    return m.discord_url || m.url || m.source_url || null;
}

function renderSignals(data) {
    const container = document.querySelector('#tab-signals .grid');
    container.innerHTML = (data || []).map(s => {
        const company = s._company || 'shared';
        const direction = s.consensus_direction || s.llm_direction || '—';
        const symbol = s.instrument_symbol || '—';
        const created = s.created_at ? new Date(s.created_at).toLocaleString() : '—';
        const tags = s.pattern_tags || s.setup_tags || [];
        const headline = s.news_headline || '';
        const srcUrl = _sourceUrlFromMeta(s.news_metadata);
        const srcLink = srcUrl
            ? `<a href="${_esc(srcUrl)}" target="_blank" class="btn secondary" style="font-size: 11px; padding: 4px 8px;">View Source</a>`
            : '';
        return `
        <div class="card" id="sig-${s.id}">
            <h2>
                <span><span class="company-tag">${_esc(company)}</span>Signal #${s.id}</span>
                <a href="#sig-${s.id}" class="anchor">⚓</a>
            </h2>
            <div class="row">
                <span class="pill ${_dirPill(direction)}">${_esc(direction)}</span>
                <strong>${_esc(symbol)}</strong>
                <span class="muted">${_esc(created)}</span>
            </div>
            ${headline ? `<div style="font-size: 12px; margin-top: 6px;">${_esc(headline)}</div>` : ''}
            <div class="json-block">${_esc(JSON.stringify(tags))}</div>
            ${srcLink ? `<div style="margin-top: 8px;">${srcLink}</div>` : ''}
        </div>`;
    }).join('');
}

function renderPositions(data) {
    const container = document.querySelector('#tab-positions .grid');
    container.innerHTML = (data || []).map(p => {
        const company = p._company || p.company_id || 'shared';
        const symbol = p.instrument_symbol || '—';
        const direction = p.direction || '—';
        const pnlRaw = p.unrealized_pnl_usd;
        const pnlNum = pnlRaw === null || pnlRaw === undefined ? null : Number(pnlRaw);
        const pnlClass = pnlNum === null ? '' : (pnlNum >= 0 ? 'pos' : 'neg');
        const pnlText = pnlNum === null ? '—' : `$${pnlNum.toFixed(2)}`;
        const actorId = p.actor_id || '—';
        const actorType = p.actor_type || '—';
        const entry = p.entry_price !== null && p.entry_price !== undefined ? _fmtNum(p.entry_price, 4) : '—';
        const sl = p.stop_loss !== null && p.stop_loss !== undefined ? _fmtNum(p.stop_loss, 4) : '—';
        const tp1 = p.take_profit_1 !== null && p.take_profit_1 !== undefined ? _fmtNum(p.take_profit_1, 4) : '—';
        const status = p.status || '—';
        return `
        <div class="card" id="pos-${p.id}">
            <h2>
                <span><span class="company-tag">${_esc(company)}</span>Pos #${p.id}</span>
                <a href="#pos-${p.id}" class="anchor">⚓</a>
            </h2>
            <div class="row">
                <strong>${_esc(symbol)}</strong>
                <span class="pill ${_dirPill(direction)}">${_esc(direction)}</span>
                <span class="pill">${_esc(status)}</span>
                <span class="pnl ${pnlClass}">${pnlText}</span>
            </div>
            <div style="font-size: 12px; margin-top: 8px;">
                <div><strong>Entry:</strong> ${entry} &nbsp; <strong>SL:</strong> ${sl} &nbsp; <strong>TP1:</strong> ${tp1}</div>
                <div><strong>Entry Reason:</strong> ${_esc(p.entry_reason_trader || p.entry_reason_llm || p.entry_reason_agent || '—')}</div>
                <div class="muted">Actor: ${_esc(actorId)} (${_esc(actorType)})</div>
            </div>
        </div>`;
    }).join('');
}

function renderInterpretations(data) {
    const container = document.querySelector('#tab-interpretations .grid');
    container.innerHTML = (data || []).map(i => {
        const company = i._company || 'shared';
        const symbol = i.instrument_symbol || '—';
        const direction = i.consensus_direction || i.llm_direction || '—';
        const version = i.prompt_version || '0';
        const tags = i.pattern_tags || i.setup_tags || [];
        const mediaUrl = i.media_url || '';
        const headline = i.news_headline || '';
        const chartUrl = `api/charts/${i.id}${company && company !== 'shared' ? `?company=${encodeURIComponent(company)}` : ''}`;
        const fallbackImg = mediaUrl
            ? `<img src="${_esc(mediaUrl)}" alt="Original Chart" style="width: 100%;">`
            : `<div class="muted" style="padding: 20px; text-align: center;">No image available</div>`;
        return `
        <div class="card" id="interp-${i.id}">
            <h2>
                <span><span class="company-tag">${_esc(company)}</span>Interp #${i.id}</span>
                <a href="#interp-${i.id}" class="anchor">⚓</a>
            </h2>
            <div class="row">
                <strong>${_esc(symbol)}</strong>
                <span class="pill ${_dirPill(direction)}">${_esc(direction)}</span>
                <span class="muted">v${_esc(version)}</span>
            </div>
            ${headline ? `<div style="font-size: 12px; margin: 6px 0;">${_esc(headline)}</div>` : ''}
            <div class="chart-container">
                <object data="${_esc(chartUrl)}" type="image/svg+xml" style="width: 100%; height: auto; min-height: 200px;">
                    ${fallbackImg}
                </object>
            </div>
            <div class="json-block">${_esc(JSON.stringify(tags, null, 2))}</div>
        </div>`;
    }).join('');
}

async function renderTraderDrill() {
    if (!state.selectedTraderId) return;
    try {
        const data = await api(`/api/trader-drill?trader_id=${state.selectedTraderId}`);
        if (!data.ok) return console.error(data.error);

        const p = data.profile;
        document.getElementById('drill-name').textContent = p.display_name || p.handle_raw;
        document.getElementById('drill-platform').textContent = p.platform;
        document.getElementById('drill-handle').textContent = p.handle_raw;

        const perfBody = document.querySelector('#drill-perf-table tbody');
        perfBody.innerHTML = (data.performance || []).map(r => `
            <tr>
                <td>${r.period}</td>
                <td>${(r.accuracy * 100).toFixed(1)}%</td>
                <td>${(r.sharpe || 0).toFixed(2)}</td>
                <td>${r.trade_count}</td>
            </tr>
        `).join('');

        const tradesBody = document.querySelector('#drill-trades-table tbody');
        tradesBody.innerHTML = (data.trades || []).map(t => `
            <tr>
                <td>${t.symbol}</td>
                <td><span class="pill">${t.direction}</span></td>
                <td class="${t.realized_pnl >= 0 ? 'pos' : 'neg'}">$${(t.realized_pnl || 0).toFixed(2)}</td>
                <td class="muted">${new Date(t.signal_timestamp).toLocaleDateString()}</td>
            </tr>
        `).join('');

        const sigGrid = document.getElementById('drill-signals-grid');
        sigGrid.innerHTML = (data.signals || []).map(s => `
            <div class="card">
                <div class="row">
                    <strong>${s.instrument_symbol}</strong>
                    <span class="pill">${s.consensus_direction}</span>
                </div>
                <div class="muted" style="font-size: 11px;">${new Date(s.created_at).toLocaleString()}</div>
            </div>
        `).join('');

    } catch (e) {
        console.error("Trader drill failed", e);
    }
}

function connectQueueWS() {
    if (state.queueWS || !state.token) return;

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Use relative path to handle potential reverse proxy prefixes (e.g. /dashboard/)
    const pathPrefix = window.location.pathname.endsWith('/') ? window.location.pathname.slice(0, -1) : window.location.pathname;
    const wsUrl = `${protocol}//${window.location.host}${pathPrefix}/ws/queue?token=${state.token}`;
    
    state.queueWS = new WebSocket(wsUrl);
    
    state.queueWS.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === 'queue_update') {
            renderQueue(msg.data);
        }
    };
    
    state.queueWS.onclose = () => {
        state.queueWS = null;
        if (state.currentTab === 'queue') {
            setTimeout(connectQueueWS, 5000);
        }
    };
    
    state.queueWS.onerror = (err) => {
        console.error("WS Error", err);
        state.queueWS.close();
    };
}

function renderQueue(data) {
    const newsBody = document.querySelector('#queue-news-table tbody');
    newsBody.innerHTML = (data.news_pending || []).map(n => `
        <tr>
            <td>${n.source}</td>
            <td>${n.headline || '—'}</td>
            <td><span class="pill">Pending</span></td>
            <td class="muted">${new Date(n.collected_at).toLocaleTimeString()}</td>
        </tr>
    `).join('');

    const mediaBody = document.querySelector('#queue-media-table tbody');
    mediaBody.innerHTML = (data.media_pending || []).map(m => `
        <tr>
            <td>${m.media_type}</td>
            <td><span class="pill">${m.processing_status}</span></td>
            <td class="muted">${new Date(m.created_at).toLocaleTimeString()}</td>
        </tr>
    `).join('');

    const posBody = document.querySelector('#queue-pos-table tbody');
    posBody.innerHTML = (data.positions_active || []).map(p => `
        <tr>
            <td><strong>${p.instrument_symbol}</strong></td>
            <td><span class="pill">${p.direction}</span></td>
            <td>${p.actor_type}</td>
            <td><span class="company-tag">${p.company_id}</span></td>
            <td class="muted">${new Date(p.signal_timestamp).toLocaleString()}</td>
        </tr>
    `).join('');
}

function handleAnchors() {
    // Hash-anchor highlight helper. Hashes that aren't simple id refs
    // (e.g. Phase Y's `#window=14d` state token) are intentionally skipped —
    // they're consumed by tab-specific renderers, not by this scroll-to logic.
    const hash = window.location.hash;
    if (!hash) return;
    if (!/^#[A-Za-z][\w:.-]*$/.test(hash)) return;
    let target = null;
    try {
        target = document.getElementById(hash.slice(1));
    } catch (err) {
        console.warn("handleAnchors: lookup failed for", hash, err);
        return;
    }
    if (target && !target.dataset.highlighted) {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        target.classList.add('highlight');
        target.dataset.highlighted = "true";
        setTimeout(() => {
            target.classList.remove('highlight');
            delete target.dataset.highlighted;
        }, 3000);
    }
}

// Init
window.addEventListener('DOMContentLoaded', () => {
    console.log("DOM Content Loaded. Initializing...");

    // Auth Handlers (wrapped in null checks for robustness)
    const btnRequest = document.getElementById('requestOtp');
    if (btnRequest) {
        btnRequest.addEventListener('click', async () => {
            const chat = document.getElementById('chatId').value.trim();
            if (!chat) return alert("Enter chat ID");
            try {
                await api('/api/auth/request-otp', { method: 'POST', body: JSON.stringify({ chat_id: chat }) });
                document.getElementById('otpStage').classList.remove('hidden');
            } catch (e) { alert(e.error || "Failed to send OTP"); }
        });
    }

    const btnVerify = document.getElementById('verifyOtp');
    if (btnVerify) {
        btnVerify.addEventListener('click', async () => {
            const chat = document.getElementById('chatId').value.trim();
            const code = document.getElementById('otpCode').value.trim();
            try {
                const res = await api('/api/auth/verify-otp', { method: 'POST', body: JSON.stringify({ chat_id: chat, code }) });
                setToken(res.token);
                showSection('app');
                refresh();
            } catch (e) { alert(e.error || "Invalid code"); }
        });
    }

    const btnLogout = document.getElementById('logout');
    if (btnLogout) {
        btnLogout.addEventListener('click', async () => {
            try { await api('/api/auth/logout', { method: 'POST' }); } catch {}
            setToken(null);
            showSection('login');
        });
    }

    const btnSkip = document.getElementById('skipLogin');
    if (btnSkip) {
        btnSkip.addEventListener('click', () => {
            console.log("Bypassing auth...");
            setToken("dev_token");
            showSection('app');
            refresh();
        });
    }

    document.querySelectorAll('nav a').forEach(a => {
        a.addEventListener('click', (e) => {
            e.preventDefault();
            switchTab(a.dataset.tab);
        });
    });

    // Phase Y — Learning tab controls.
    document.querySelectorAll('#learning-window-tabs .window-tab').forEach(el => {
        el.addEventListener('click', (e) => {
            e.preventDefault();
            const w = el.dataset.window;
            if (!LEARNING_WINDOW_DAYS[w]) return;
            state.learningWindow = w;
            // Round-trip into the URL hash so reloads/copy-link preserve it.
            try {
                const newHash = `#window=${w}`;
                if (window.location.hash !== newHash) {
                    history.replaceState(null, '', newHash);
                }
            } catch {}
            _highlightLearningWindow();
            if (state.currentTab === 'learning') renderLearning();
        });
    });
    const dimSel = document.getElementById('learning-feed-dim');
    if (dimSel) {
        dimSel.addEventListener('change', () => {
            state.learningDimension = dimSel.value || '';
            if (state.currentTab === 'learning') renderLearning();
        });
    }

    // Phase X.4 — News tab controls.
    document.querySelectorAll('#news-window-tabs .window-tab').forEach(el => {
        el.addEventListener('click', (e) => {
            e.preventDefault();
            const w = el.dataset.window;
            if (!NEWS_WINDOW_LABELS.includes(w)) return;
            state.newsWindow = w;
            _highlightNewsWindow();
            if (state.currentTab === 'news') renderNews();
        });
    });
    const newsSrcSel = document.getElementById('news-source-filter');
    if (newsSrcSel) {
        newsSrcSel.addEventListener('change', () => {
            state.newsSource = newsSrcSel.value || '';
            if (state.currentTab === 'news') renderNews();
        });
    }
    const newsMediaSel = document.getElementById('news-media-filter');
    if (newsMediaSel) {
        newsMediaSel.addEventListener('change', () => {
            state.newsHasMedia = newsMediaSel.value || '';
            if (state.currentTab === 'news') renderNews();
        });
    }

    const companyFilter = document.getElementById('company-filter');
    if (companyFilter) {
        companyFilter.addEventListener('change', (e) => {
            state.company = e.target.value === 'all' ? null : e.target.value;
            refresh();
        });
    }

    // AUTH DISABLED PER USER REQUEST
    setToken("dev_token");
    showSection('app');
    refresh();
    state.refreshInterval = setInterval(refresh, 10000);
});

window.addEventListener('hashchange', handleAnchors);
