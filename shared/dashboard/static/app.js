(()=>{
/* Tickles v5 — competition inline expand, chart tabs in Discord drawer, trader usernames, compact rows. */
console.log('Tickles Dashboard v5 loading');
const state={tab:'floor',company:'all',snap:null,competitions:null,learning:null,news:null,agentPerf:null,sort:{},charts:{},timer:null,expandedAgent:null,timeSpacing:localStorage.getItem('tickles.replay.spacing')||'contiguous'};
const $=s=>document.querySelector(s), $$=s=>Array.from(document.querySelectorAll(s));
const esc=v=>v==null?'':String(v).replace(/[&<>\"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[m]));
const n=v=>{const x=Number(v);return Number.isFinite(x)?x:0};
const fmt=(v,d=2)=>{const x=Number(v);return Number.isFinite(x)?x.toFixed(d):'—'};
const usd=v=>`${n(v)>=0?'+':''}$${Math.abs(n(v)).toFixed(2)}`;
const pct=v=>`${n(v)>=0?'+':''}${fmt(v,2)}%`;
const rel=iso=>{if(!iso)return'—';const s=Math.max(0,Math.floor((Date.now()-new Date(iso))/1000));if(s<60)return`${s}s`;if(s<3600)return`${Math.floor(s/60)}m`;if(s<86400)return`${Math.floor(s/3600)}h`;return`${Math.floor(s/86400)}d`};
const fmtDate=iso=>{if(!iso)return'—';const d=new Date(iso);return d.toLocaleString('en-GB',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'})};
const initials=v=>(v||'?').split(/[\s._-]/).filter(Boolean).map(x=>x[0]).join('').slice(0,2).toUpperCase();
const pill=(v,k='')=>`<span class="pill ${k||String(v||'').toLowerCase()}">${esc(v||'—')}</span>`;
const dirPill=d=>`<span class="pill ${d==='short'?'short':'long'}">${esc(d||'long')}</span>`;
const traderName=p=>{if(!p)return'trader';const name=p.actor_display||p.display_name||p.trader_display_name||p.actor_handle||p.handle_raw||p.trader_handle_raw||p.handle_normalized||p.trader_handle_normalized||p.actor_id||'trader';return String(name).replace(/^(jarvais_trader_|trader_)/i,'')};
/* Round 13 (2026-05-24): tracked_positions.instrument_symbol now stores the
   CCXT perp swap form (e.g. "BTC/USDT:USDT"). Render it as Bybit-on-TradingView
   convention "BTCUSDT.P" so the trader sees a familiar ticker. Spot canonical
   ("BTC/USDT") is no longer written by the router but legacy rows still exist
   — render them without the .P. Anything not matching the slash-form (epics,
   raw text) renders unchanged. */
const dispSymbol=s=>{if(!s)return'UNKNOWN';const m=String(s).match(/^([A-Z0-9]+)\/([A-Z0-9]+)(?::([A-Z0-9]+))?$/);if(!m)return s;const base=m[1],quote=m[2],settle=m[3];return settle?`${base}${quote}.P`:`${base}${quote}`};

async function api(path,opt={}){const u=new URL(path.replace(/^\//,''),document.baseURI);if(!opt.skipCompany&&state.company!=='all')u.searchParams.set('company',state.company);const r=await fetch(u);const j=await r.json();if(!r.ok)throw j;return j}
async function load(){try{const needsSnap=['floor','radar','signals','positions','traders','ops'].includes(state.tab)||!state.snap;if(needsSnap)state.snap=await api('/api/snapshot');if(state.tab==='competition'||state.tab==='floor')state.competitions=await api('/api/competitions');if(state.tab==='learning'||state.tab==='floor'||state.tab==='traders')state.learning=await fetchLearning();if(state.tab==='news'||state.tab==='floor')state.news=await api('/api/news/feed?window=30d&limit=120');
      if(state.tab==='telegram')state.telegram=await api('/api/news/feed?window=30d&limit=120&source=telegram');if(state.tab==='traders'||state.tab==='floor')state.agentPerf=await api('/api/agent-performance');if(state.tab==='settings'){state.settings=null;await fetchSettings();}render();$('#updated-at').textContent='updated now';$('#status-text').textContent='live'}catch(e){console.error(e);$('#status-text').textContent='API issue'}}
async function fetchLearning(){const [skill,feed,brain]=await Promise.all([api('/api/learning/skill-summary?window=1m'),api('/api/learning/memory-feed?window=1m'),api('/api/learning/agent-brain?window=1m')]);return{skill,feed,brain}}
function switchTab(tab){state.tab=tab;state.expandedAgent=null;state._agentCache=null;$$('.nav-link').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));$$('.tab').forEach(t=>t.classList.toggle('active',t.id===`tab-${tab}`));const names={floor:['TRADING COMPANY','Trading Floor'],radar:['LIVE ENTRY WATCH','Entry Radar'],competition:['CONTEST MODE','Competition'],signals:['ENTRY WATCH','Signals Watch'],positions:['RISK MONITOR','Positions'],traders:['DISCORD ALPHA','Trader Intel'],news:['SOCIAL TAPE','Discord Feed'],telegram:['TELEGRAM','Telegram Feed'],learning:['MEMORY + SKILL','AI Learning'],ops:['RUN COST','Ops & Cost'],settings:['CONFIGURATION','Settings']};$('#eyebrow').textContent=names[tab][0];$('#page-title').textContent=names[tab][1];load()}
function render(){renderStats();if(state.tab==='floor')renderFloor();if(state.tab==='radar')renderRadarPage();if(state.tab==='competition')renderCompetition();if(state.tab==='signals')renderSignalsPage();if(state.tab==='positions')renderPositionsPage();if(state.tab==='traders')renderTradersPage();if(state.tab==='news')renderNewsPage();if(state.tab==='telegram')renderTelegramPage();if(state.tab==='learning')renderLearningPage();if(state.tab==='ops')renderOpsPage();if(state.tab==='settings')renderSettingsPage()}
function renderStats(){const s=state.snap||{};const pnl=n(s.open_positions_unrealized_pnl);$('#stat-pnl').textContent=usd(pnl);$('#stat-pnl').className=pnl>=0?'success':'danger';$('#stat-open-count').textContent=`${s.open_positions_count||0} open positions`;$('#stat-signals').textContent=s.signals_today_count||0;$('#stat-ingest').textContent=`${s.ingest_depth||0} ingest depth`;$('#stat-top').textContent=s.top_actor_name||'—';$('#stat-edge').textContent=s.top_actor_score?`edge ${fmt(s.top_actor_score,3)}`:'edge —';$('#stat-cost').textContent=`$${fmt(s.api_cost_today_usd,4)}`;$('#stat-budget').textContent=`$${fmt(s.budget_remaining_usd||s.budget_limit_usd||100,0)} remaining`}

/* ─── row helpers ─── */
function rowMain(sym,sub){return`<div class="main-cell"><div class="token-dot">${esc(initials(sym))}</div><div><div class="primary">${esc(sym)}</div><div class="secondary">${esc(sub||'')}</div></div></div>`}

/* ─── competition — INLINE expand, no drawer ─── */
async function renderCompetition(){
  const c=state.competitions?.competitions?.[0];
  if(!c){$('#competition-body').innerHTML='<div class="empty">No active competition</div>';return}
  const rows=(c.participants||[]).sort((a,b)=>(a.rank||99)-(b.rank||99)).map(p=>{
    const eq=n(p.scores?.equity), unreal=n(p.scores?.unrealized_pnl_usd), liveEq=eq+unreal, rpct=n(p.scores?.return_pct);
    const starting=n(p.scores?.starting_balance_usd)||1000;
    const liveReturn=((liveEq/starting)-1)*100;
    return `<tr class="comp-row" data-agent="${esc(p.agent_id)}" id="comp-tr-${esc(p.agent_id)}">
      <td class="num rank">#${p.rank}</td>
      <td>${rowMain(p.agent_id,p.metadata?.description||p.strategy_ref||'')}</td>
      <td>${esc(p.strategy_ref||'—')}</td>
      <td class="num mono">$${fmt(eq,2)}</td>
      <td class="num mono"><strong>$${fmt(liveEq,2)}</strong></td>
      <td class="num">${usd(p.scores?.total_realized_pnl_usd)}<br><span class="secondary small">unreal ${usd(p.scores?.unrealized_pnl_usd)}</span></td>
      <td class="num ${rpct>=0?'success':'danger'}">${pct(rpct)}<br><span class="secondary small">${pct(liveReturn)} live</span></td>
      <td class="num">${fmt(n(p.scores?.win_rate)*100,1)}%</td>
      <td class="num">${p.scores?.total_trades||0}</td>
      <td class="num">${p.scores?.open_positions||0}</td>
    </tr>`;
  });
  $('#competition-body').innerHTML=`<table class="data-table comp-table"><thead><tr>
    <th class="num">Rank</th><th>Agent / description</th><th>Strategy</th>
    <th class="num">Balance</th><th class="num">Live Equity</th><th class="num">P&L</th><th class="num">Return</th>
    <th class="num">Win</th><th class="num">Trades</th><th class="num">Open</th>
  </tr></thead><tbody>${rows.join('')}</tbody></table><div id="comp-expand-zone"></div>`;
  $$('#competition-body .comp-row').forEach(tr=>tr.onclick=()=>toggleAgentExpand(tr.dataset.agent));
  // Restore expanded agent after refresh
  if(state.expandedAgent&&state._agentCache&&state._agentCache.agentId===state.expandedAgent){
    setTimeout(()=>toggleAgentExpand(state.expandedAgent,true),50);
  }
}

async function toggleAgentExpand(agentId,fromCache){
  const prev=state.expandedAgent;
  if(prev===agentId&&!fromCache){state.expandedAgent=null;state._agentCache=null;$('#comp-expand-zone').innerHTML='';$$('.comp-row').forEach(r=>r.classList.remove('expanded'));return}
  $$('.comp-row').forEach(r=>r.classList.remove('expanded'));
  const tr=document.getElementById('comp-tr-'+agentId); if(tr) tr.classList.add('expanded');
  state.expandedAgent=agentId;
  // On auto-refresh: render from cache first (no flash), then silently re-fetch
  if(fromCache&&state._agentCache&&state._agentCache.agentId===agentId){
    renderAgentInline(agentId,state._agentCache.data);
    // Re-fetch in background to get latest prices
    (async()=>{
      try{
        const d=await api('/api/competition-agent?agent='+encodeURIComponent(agentId));
        state._agentCache={agentId,data:d};
        renderAgentInline(agentId,d);
      }catch(e){/* silent — keep stale cache */}
    })();
    return;
  }
  $('#comp-expand-zone').innerHTML=`<div class="comp-detail"><div class="comp-detail-loading">Loading ${esc(agentId)} positions…</div></div>`;
  try{
    const d=await api('/api/competition-agent?agent='+encodeURIComponent(agentId));
    state._agentCache={agentId,data:d};
    renderAgentInline(agentId,d);
  }catch(e){
    $('#comp-expand-zone').innerHTML=`<div class="comp-detail"><div class="empty">Failed to load agent data</div></div>`;
  }
}

function renderAgentInline(agentId,d){
  if(!d||!d.ok){$('#comp-expand-zone').innerHTML='<div class="empty">No agent data</div>';return}
  const a=d.agent||{}, open=d.open_positions||[], hist=d.history||[];
  let posTab='live';

  function buildLive(){
    if(!open.length)return'<tr><td colspan="11" class="empty">No open positions — waiting for new signals</td></tr>';
    return open.map((p,i)=>{
      const upnl=n(p.unrealized_pnl), upct=n(p.unrealized_pnl_pct);
      const dir=(p.direction||'long').toLowerCase();
      const cur=p.current_price?fmt(p.current_price,4):'…';
      const tname=traderName(p);
      const sigId=p.signal_interpretation_id;
      return `<tr class="pos-table-row ${upnl>=0?'win':'loss'} clickable" data-sig="${esc(sigId)}">
        <td><strong>${esc(p.symbol)}</strong></td>
        <td>${dirPill(dir)}</td>
        <td class="num mono">${fmt(p.entry_price,4)}</td>
        <td class="num mono">${cur}</td>
        <td class="num mono">${p.sl_price?fmt(p.sl_price,4):'—'}</td>
        <td class="num mono">${p.tp_price?fmt(p.tp_price,4):'—'}</td>
        <td class="num ${upnl>=0?'success':'danger'}"><strong>${usd(upnl)}</strong></td>
        <td class="num ${upct>=0?'success':'danger'}">${upct>=0?'+':''}${fmt(upct,2)}%</td>
        <td class="num">${fmt(p.leverage,1)}x</td>
        <td><span class="discord-user">${esc(tname)}</span><br><span class="text-muted small">chart ${fmtDate(p.chart_posted_at)}</span></td>
        <td class="text-muted small">${p.entered_at?fmtDate(p.entered_at):'—'}<br>${rel(p.entered_at)} ago</td>
      </tr>`;
    }).join('');
  }
  function buildHistory(){
    if(!hist.length)return'<tr><td colspan="10" class="empty">No trade history yet</td></tr>';
    return hist.map(p=>{
      const pnl=n(p.pnl); const dir=(p.direction||'long').toLowerCase();
      const tname=traderName(p);
      return `<tr class="pos-table-row ${pnl>=0?'win':'loss'}">
        <td><strong>${esc(p.symbol)}</strong></td>
        <td>${dirPill(dir)}</td>
        <td class="num mono">${fmt(p.entry_price,4)}</td>
        <td class="num mono">${fmt(p.exit_price,4)}</td>
        <td class="num mono">${fmt(p.sl_price,4)}</td>
        <td class="num mono">${fmt(p.tp_price,4)}</td>
        <td class="num ${pnl>=0?'success':'danger'}"><strong>${usd(pnl)}</strong></td>
        <td><span class="pill ${pnl>=0?'long':'short'} small">${esc(p.exit_reason||'closed')}</span></td>
        <td>${esc(tname)}</td>
        <td class="text-muted small">${fmtDate(p.entered_at)} → ${fmtDate(p.exited_at)}</td>
      </tr>`;
    }).join('');
  }

  const eq=n(a.equity_usd), unreal=n(a.unrealized_pnl_usd), liveEq=eq+unreal;
  const starting=n(a.starting_balance_usd)||1000;
  const liveReturn=((liveEq/starting)-1)*100;

  const html=`<div class="comp-detail">
    <div class="comp-detail-tabs">
      <button class="comp-dtab active" data-ctab="live">Live · ${open.length}</button>
      <button class="comp-dtab" data-ctab="history">History · ${hist.length}</button>
    </div>
    <div class="comp-stats">
      <div><label>Balance</label><strong>$${fmt(eq,2)}</strong></div>
      <div><label>Live Equity</label><strong>$${fmt(liveEq,2)}</strong></div>
      <div><label>Realized P&L</label><strong class="${n(a.realized_pnl_usd)>=0?'success':'danger'}">${usd(a.realized_pnl_usd)}</strong></div>
      <div><label>Unrealized</label><strong class="${unreal>=0?'success':'danger'}">${usd(unreal)}</strong></div>
      <div><label>Return</label><strong>${fmt(a.return_pct,1)}%</strong></div>
      <div><label>Live Return</label><strong class="${liveReturn>=0?'success':'danger'}">${fmt(liveReturn,1)}%</strong></div>
      <div><label>Win Rate</label><strong>${fmt(n(a.win_rate)*100,1)}%</strong></div>
      <div><label>Trades</label><strong>${a.total_trades||0}</strong></div>
    </div>
    <div id="comp-live-panel" class="comp-panel">
      <table class="pos-table"><thead><tr>
        <th>Symbol</th><th>Dir</th><th class="num">Entry</th><th class="num">Live</th><th class="num">SL</th><th class="num">TP</th><th class="num">P&L</th><th class="num">%</th><th class="num">Lev</th><th>Discord</th><th>Entered</th>
      </tr></thead><tbody>${buildLive()}</tbody></table>
    </div>
    <div id="comp-history-panel" class="comp-panel hidden">
      <table class="pos-table"><thead><tr>
        <th>Symbol</th><th>Dir</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">SL</th><th class="num">TP</th><th class="num">P&L</th><th>Reason</th><th>Trader</th><th>Period</th>
      </tr></thead><tbody>${buildHistory()}</tbody></table>
    </div>
  </div>`;
  $('#comp-expand-zone').innerHTML=html;
  // tab switching
  $$('#comp-expand-zone .comp-dtab').forEach(b=>b.onclick=()=>{
    $$('#comp-expand-zone .comp-dtab').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    const tab=b.dataset.ctab;
    $('#comp-live-panel').classList.toggle('hidden',tab!=='live');
    $('#comp-history-panel').classList.toggle('hidden',tab!=='history');
  });
  // click position rows to open signal intelligence
  $$('#comp-expand-zone .pos-table-row.clickable').forEach(tr=>tr.onclick=e=>{
    e.stopPropagation();
    const sigId=tr.dataset.sig;
    if(sigId&&sigId!=='null'&&sigId!=='undefined') openCall(sigId);
  });
}

/* ─── Discord feed — chart tabs drawer ─── */
function renderNewsPage(){
  let rows=state.news?.rows||[];
  rows=rows.filter(r=>r.source==='discord');  // exclude Telegram
  rows=filterRows(rows,$('#news-filter')?.value,['author','content','headline','channel_name']);
  if($('#news-media')?.value==='media')rows=rows.filter(r=>r.has_media);
  $('#news-list').innerHTML=rows.map(newsMsg).join('')||'<div class="empty">No messages</div>';
  $$('[data-newsrow]').forEach(el=>el.onclick=()=>openDiscordDrawer(el.dataset.newsrow,el.dataset.newssrc));
}

/* ─── Landing pages ─── */
function renderSignalsPage(){let rows=state.snap?.signals||[];rows=filterRows(rows,$('#signals-filter')?.value,['instrument_symbol','trader_display_name','trader_handle_normalized','status','raw_signal_text']);const st=$('#signals-status')?.value;if(st)rows=rows.filter(r=>r.status===st);renderSignalsTable(rows)}
// Round 11 (2026-05-24): Positions tab now has Live + Historic sub-tabs.
// Live  = open + partial_exit (+ broker fills) from /api/positions/live.
// Historic = closed + expired + cancelled + invalidated, paginated, from
// /api/positions/historic with keyset cursor + 30d default window.
// Each sub-tab has its own column set + filter UI. The drawer click path is
// unchanged: row → openCall(signal_interpretation_id, news_item_id).
const _positionsState={
  sub: 'live',
  live: { rows: null, loading: false },
  historic: {
    rows: [], cursor: null, has_more: false, loading: false,
    last_query: null,
  },
};
async function renderPositionsPage(){
  const sub=_positionsState.sub||'live';
  $$('#tab-positions .comp-dtab').forEach(b=>b.classList.toggle('active',b.dataset.ptab===sub));
  $('#positions-live-panel').classList.toggle('hidden',sub!=='live');
  $('#positions-historic-panel').classList.toggle('hidden',sub!=='historic');
  if(sub==='live') await renderPositionsLive(); else await renderPositionsHistoric(false);
}
async function renderPositionsLive(){
  const company=state.company||'all';
  if(_positionsState.live.loading)return;
  _positionsState.live.loading=true;
  try{
    const data=await api(`/api/positions/live?company=${encodeURIComponent(company)}&limit=200`);
    _positionsState.live.rows=data.positions||[];
  }catch(e){console.warn('positions/live failed',e);_positionsState.live.rows=[]}
  _positionsState.live.loading=false;
  let rows=_positionsState.live.rows||[];
  rows=filterRows(rows,$('#positions-live-filter')?.value,['instrument_symbol','actor_display','actor_handle','status','signal_source']);
  const st=$('#positions-live-status')?.value;
  if(st)rows=rows.filter(r=>r.status===st);
  const cols=[
    {label:'Position'},{label:'Side'},{label:'Status'},
    {label:'Entry',num:1},{label:'Now',num:1},
    {label:'P&L',num:1},{label:'P&L %',num:1},
    {label:'Dist SL',num:1},{label:'Dist TP1',num:1},
    {label:'Age',num:1},{label:'Source'},
  ];
  table('#positions-live-table',cols,[livePosRows(rows)]);
  wireRows();
}
function livePosRows(rows){return rows.map(p=>{
  const pnl=n(p.unrealized_pnl_usd??p.pnl_usd);
  const pnlPct=n(p.pnl_pct);
  // Round 12 (2026-05-24): PositionMonitor now mirrors distance_to_sl_pct /
  // distance_to_tp1_pct / time_in_trade_minutes from the snapshot back to
  // tracked_positions on every cycle, so the API row usually carries
  // authoritative values. We keep the client-side fallback below for two
  // reasons: (1) legacy rows written before the mirror was deployed still
  // have NULL in those columns until the next monitor tick, and (2) if
  // the monitor stalls the dashboard at least shows something rather than
  // a row of dashes. The mirror takes precedence when present.
  // (Pre-Round 12 note: distance_to_sl_pct / distance_to_tp1_pct only
  // existed in position_updates time-series; client computation was the
  // ONLY source. After Round 12 it is the fallback.) Compute from
  // entry/current/SL/TP — all four
  // values are already in the payload.
  const ent=Number(p.entry_price);
  const now=Number(p.current_price);
  const sl=Number(p.stop_loss);
  const tp=Number(p.take_profit_1);
  let distSl=p.distance_to_sl_pct;
  if((distSl==null||!Number.isFinite(distSl))&&Number.isFinite(now)&&Number.isFinite(sl)&&Number.isFinite(ent)&&ent!==0){
    distSl=(now-sl)/ent*100;
  }
  let distTp=p.distance_to_tp1_pct;
  if((distTp==null||!Number.isFinite(distTp))&&Number.isFinite(now)&&Number.isFinite(tp)&&Number.isFinite(ent)&&ent!==0){
    distTp=(tp-now)/ent*100;
  }
  const stale=p.price_updated_at?(Date.now()-new Date(p.price_updated_at))/1000>300:false;
  let ageMin=p.time_in_trade_minutes!=null?Number(p.time_in_trade_minutes):null;
  if(!ageMin||ageMin<=0){
    const ts=p.opened_at||p.signal_timestamp;
    if(ts){const m=(Date.now()-new Date(ts))/60000;if(Number.isFinite(m)&&m>=0)ageMin=m;}
  }
  const ageStr=ageMin!=null&&Number.isFinite(ageMin)?(ageMin>=1440?Math.round(ageMin/1440)+'d':ageMin>=60?Math.round(ageMin/60)+'h':Math.round(ageMin)+'m'):rel(p.signal_timestamp||p.opened_at);
  return `<tr data-call="${esc(p.signal_interpretation_id||'')}" data-news="${esc(p.news_item_id||'')}">`
    +`<td>${rowMain(dispSymbol(p.instrument_symbol),`${traderName(p)} · ${rel(p.signal_timestamp||p.opened_at)}`)}</td>`
    +`<td>${pill(p.direction,p.direction)}</td>`
    +`<td>${pill(p.status,p.status)}</td>`
    +`<td class="num mono">${fmt(p.entry_price,6)}</td>`
    +`<td class="num mono ${stale?'secondary':''}">${fmt(p.current_price,6)}${stale?' <span title="stale price >5min">·</span>':''}</td>`
    +`<td class="num ${pnl>=0?'success':'danger'}">${usd(pnl)}</td>`
    +`<td class="num ${pnlPct>=0?'success':'danger'}">${pct(pnlPct*100)}</td>`
    +`<td class="num">${distSl==null?'—':fmt(Math.abs(distSl),2)+'%'}</td>`
    +`<td class="num">${distTp==null?'—':fmt(Math.abs(distTp),2)+'%'}</td>`
    +`<td class="num secondary">${ageStr}</td>`
    +`<td>${esc(p.signal_source||p._source||'—')}</td>`
    +`</tr>`;
}).join('')}
async function renderPositionsHistoric(loadMore){
  const company=state.company||'all';
  const since=$('#positions-historic-window')?.value??'30';
  const status=$('#positions-historic-status')?.value||'';
  const outcome=$('#positions-historic-outcome')?.value||'';
  const queryKey=[company,since,status,outcome].join('|');
  if(_positionsState.historic.last_query!==queryKey){
    _positionsState.historic.rows=[];
    _positionsState.historic.cursor=null;
    _positionsState.historic.has_more=false;
    _positionsState.historic.last_query=queryKey;
    loadMore=false;
  }
  if(_positionsState.historic.loading)return;
  _positionsState.historic.loading=true;
  $('#positions-historic-meta').textContent=loadMore?'Loading more…':'Loading…';
  try{
    const params=new URLSearchParams({company,since_days:since,limit:'50'});
    if(status)params.set('status',status);
    if(outcome)params.set('outcome',outcome);
    if(loadMore&&_positionsState.historic.cursor){
      params.set('cursor_at',_positionsState.historic.cursor.closed_at||'');
      params.set('cursor_id',_positionsState.historic.cursor.id||'');
    }
    const data=await api(`/api/positions/historic?${params.toString()}`);
    const incoming=data.rows||[];
    _positionsState.historic.rows=loadMore?_positionsState.historic.rows.concat(incoming):incoming;
    _positionsState.historic.cursor=data.next_cursor||null;
    _positionsState.historic.has_more=!!data.has_more;
  }catch(e){
    console.warn('positions/historic failed',e);
    if(!loadMore)_positionsState.historic.rows=[];
  }
  _positionsState.historic.loading=false;
  let rows=_positionsState.historic.rows||[];
  rows=filterRows(rows,$('#positions-historic-filter')?.value,['instrument_symbol','actor_display','actor_handle','status','outcome','exit_reason']);
  const cols=[
    {label:'Position'},{label:'Side'},{label:'Closed'},
    {label:'Status'},{label:'Outcome'},
    {label:'Entry',num:1},{label:'Exit',num:1},
    {label:'Realized P&L',num:1},{label:'Hold'},
    {label:'Source'},
  ];
  table('#positions-historic-table',cols,[historicPosRows(rows)]);
  $('#positions-historic-meta').textContent=`${rows.length} of ${_positionsState.historic.rows.length} loaded${_positionsState.historic.has_more?' · more available':''}`;
  $('#positions-historic-more').classList.toggle('hidden',!_positionsState.historic.has_more);
  wireRows();
}
function historicPosRows(rows){return rows.map(p=>{
  const pnl=n(p.realized_pnl_usd_final??p.realized_pnl_usd??p.pnl_usd);
  const opened=p.opened_at||p.signal_timestamp;
  const closed=p.closed_at;
  let hold='—';
  if(opened&&closed){
    const sec=Math.max(0,(new Date(closed)-new Date(opened))/1000);
    hold=sec>=86400?Math.round(sec/86400)+'d':sec>=3600?Math.round(sec/3600)+'h':Math.round(sec/60)+'m';
  }
  return `<tr data-call="${esc(p.signal_interpretation_id||'')}" data-news="${esc(p.news_item_id||'')}">`
    +`<td>${rowMain(dispSymbol(p.instrument_symbol),`${traderName(p)} · ${rel(opened)}`)}</td>`
    +`<td>${pill(p.direction,p.direction)}</td>`
    +`<td class="num secondary">${rel(closed)}</td>`
    +`<td>${pill(p.status,p.status)}</td>`
    +`<td>${p.outcome?pill(p.outcome,p.outcome):'<span class="secondary">—</span>'}</td>`
    +`<td class="num mono">${fmt(p.entry_price,6)}</td>`
    +`<td class="num mono">${fmt(p.exit_price,6)}</td>`
    +`<td class="num ${pnl>=0?'success':'danger'}">${usd(pnl)}</td>`
    +`<td class="num secondary">${hold}</td>`
    +`<td>${esc(p.signal_source||p._source||'—')}</td>`
    +`</tr>`;
}).join('')}
function renderLearningPage(){const l=state.learning||{feed:{rows:[]},skill:{rows:[]},brain:{rows:[]}};$('#learn-total').textContent=l.feed.rows.length;$('#learn-actors').textContent=l.skill.rows.length;$('#learn-brains').textContent=l.brain.rows.length;$('#learn-focus').textContent=(l.feed.rows[0]?.raw&&safeJson(l.feed.rows[0].raw)?.instrument_symbol)||'execution';$('#learning-lessons').innerHTML=l.feed.rows.map(r=>`<div class="lesson"><div class="lesson-kind">${esc(r.source_kind)} · ${esc(r.actor)}</div><div class="lesson-body">${esc(r.body)}</div><div class="lesson-foot">${esc(r.company)} · ${rel(r.ts)}</div></div>`).join('');const body=l.skill.rows.map(r=>`<tr><td>${rowMain(r.display_name||r.actor_id,r.actor_id)}</td><td class="num">${r.skill_pct??'—'}%</td><td class="num">${r.trade_count||0}</td></tr>`).join('');$('#learning-models').innerHTML=`<table class="data-table"><thead><tr><th>Actor</th><th class="num">Skill</th><th class="num">Trades</th></tr></thead><tbody>${body||'<tr><td colspan="3" class="empty">No data</td></tr>'}</tbody></table>`}

/* ─── misc renderers ─── */
// Round 11 (2026-05-24): the Floor positions panel previously included
// `r.status==='active'` which is a legacy frontend-only enum that the DB
// CHECK constraint does not allow — dead branch. The canonical "live" set
// is open + partial_exit. Keeping it minimal here; full live/historic split
// lives on the Positions tab in phase 11.3.
const POS_LIVE_STATUSES=new Set(['open','partial_exit']);
function renderFloor(){const s=state.snap||{};const sigs=filterRows(s.signals||[],$('#floor-signal-filter')?.value,['instrument_symbol','trader_display_name','trader_handle_normalized','status']);table('#floor-signals',[{label:'Signal'},{label:'Side'},{label:'Entry',num:1},{label:'SL',num:1},{label:'TP1',num:1},{label:'Δ entry',num:1},{label:'Status'},{label:'Call'}],[sigRows(sigs,10)]);const live_positions=(s.positions||[]).filter(r=>POS_LIVE_STATUSES.has(String(r.status||'').toLowerCase()));const ps=filterRows(live_positions,$('#floor-position-filter')?.value,['instrument_symbol','actor_display','actor_handle','status']);table('#floor-positions',[{label:'Position'},{label:'Side'},{label:'Status'},{label:'Entry',num:1},{label:'Now',num:1},{label:'P&L',num:1},{label:'P&L %',num:1},{label:'Notional',num:1},{label:'Source'}],[posRows(ps,10)]);renderCompetitionMini();renderNewsMini();wireRows()}
function renderCompetitionMini(){const c=state.competitions?.competitions?.[0];if(!c){$('#floor-competition').innerHTML='<div class="empty">No competition</div>';return}const ps=(c.participants||[]).sort((a,b)=>(a.rank||99)-(b.rank||99)).slice(0,6);$('#floor-competition').innerHTML=`<div class="competition-card"><div class="secondary">${esc(c.name)} · ${esc(c.status)}</div><div class="leader-mini">${ps.map(p=>{const eq=n(p.scores?.equity);const start=n(p.scores?.starting_balance_usd)||1000;const livePnl=eq-start;return `<div class="leader-row"><div class="rank-badge">#${p.rank}</div><div><div class="primary">${esc(p.agent_id)}</div><div class="secondary">${esc(p.strategy_ref||'')} · ${livePnl>=0?'+':''}$${fmt(livePnl,2)}</div></div><div class="num ${eq>=start?'success':'danger'}">$${fmt(eq,2)}</div></div>`}).join('')}</div></div>`}
function renderNewsMini(){const rows=(state.news?.rows||[]).filter(r=>r.source==='discord');$('#floor-news').innerHTML=rows.slice(0,8).map(newsMsg).join('')||'<div class="empty">No Discord messages</div>'}
// Bug 12 sibling — strip the leading `[Reply to @user]: <quoted parent>`
// line so the dashboard News tab and floor mini-feed don't show the
// parent's quoted text as if it were the trader's own message.
// `news_provider.py` already strips this server-side; the JS strip is a
// defence-in-depth so any future API caller that bypasses the provider
// still renders cleanly.
function _stripReplyPrefixJS(text){
  if(!text) return '';
  if(text.indexOf('[Reply to @')===0){
    const nl=text.indexOf('\n');
    return nl>=0?text.slice(nl+1):'';
  }
  return text;
}
function newsMsg(r){
  const body=_stripReplyPrefixJS(r.content||r.headline||'');
  const src=r.source||'discord';
  return`<div class="discord-msg" data-newsrow="${r.id}" data-newssrc="${src}"><div class="discord-avatar">${initials(r.author)}</div><div><div class="discord-head"><span class="discord-user">${esc(r.author||'unknown')}</span><span class="discord-time">${rel(r.published_at||r.collected_at)}</span></div><div class="discord-text">${esc(body)}</div><div class="discord-meta">${r.has_media?pill(`${r.media_count||1} chart${(r.media_count||1)>1?'s':''}`,'pending'):''}${(r.instruments||[]).map(x=>pill(x)).join('')}</div></div></div>`
}
// 2026-05-22 — Δ entry "honesty" fix. The previous formula rendered missing
// data (current_price=null AND distance_to_entry_pct=null) as "+0.00%" in
// the success/green class, which the user reads as "exactly at entry, in
// your favour" — wrong. Now we detect the no-data case explicitly and
// render '—' in the muted .secondary class. Real zero stays "+0.00%" green.
function sigRows(rows,limit){return rows.slice(0,limit||rows.length).map(s=>{const dir=s.consensus_direction||s.direction;const trader=traderName(s);const liveRaw=s.current_price;const entryRaw=s.entry_price??s.levels?.entry;const distRaw=s.distance_to_entry_pct;const hasLive=liveRaw!=null&&Number.isFinite(Number(liveRaw))&&Number(liveRaw)>0;const hasEntry=entryRaw!=null&&Number.isFinite(Number(entryRaw))&&Number(entryRaw)>0;const hasStoredDist=distRaw!=null&&Number.isFinite(Number(distRaw))&&Number(distRaw)!==0;const computedDist=hasLive&&hasEntry?((Number(liveRaw)-Number(entryRaw))/Number(entryRaw)*100):null;const dist=computedDist!=null?computedDist:(hasStoredDist?Number(distRaw):null);const distCell=dist==null?'<span class="secondary">—</span>':`<span class="${dist>=0?'success':'danger'}">${pct(dist)}</span>`;return`<tr data-call="${esc(s.signal_interpretation_id||s.id)}" data-news="${esc(s.news_item_id||'')}"><td>${rowMain(dispSymbol(s.instrument_symbol),`${trader} · ${rel(s.signal_timestamp||s.created_at)}`)}</td><td>${pill(dir,dir)}</td><td class="num mono">${fmt(entryRaw,6)}</td><td class="num mono">${fmt(s.stop_loss||s.levels?.stop_loss,6)}</td><td class="num mono">${fmt(s.take_profit_1||s.levels?.take_profit_1,6)}</td><td class="num">${distCell}</td><td>${pill(s.status||'signal',s.status)}</td><td><div class="secondary">${esc((s.raw_signal_text||s.news_content||s.news_headline||'').slice(0,90))}</div></td></tr>`}).join('')}
// Round 11 (2026-05-24): closed/expired/cancelled rows have realized_pnl_usd_final
// populated and unrealized_pnl_usd null/zero. Branch on status so the P&L column
// shows the correct number for both live and historic positions.
const POS_CLOSED_STATUSES=new Set(['closed','expired','cancelled','invalidated','deleted']);
function posRows(rows,limit){return rows.slice(0,limit||rows.length).map(p=>{const closed=POS_CLOSED_STATUSES.has(String(p.status||'').toLowerCase());const pnl=n(closed?(p.realized_pnl_usd_final??p.realized_pnl_usd??p.pnl_usd):(p.unrealized_pnl_usd??p.pnl_usd));return`<tr data-call="${esc(p.signal_interpretation_id||'')}" data-news="${esc(p.news_item_id||'')}"><td>${rowMain(dispSymbol(p.instrument_symbol),`${traderName(p)} · ${rel(p.signal_timestamp||p.opened_at)}`)}</td><td>${pill(p.direction,p.direction)}</td><td>${pill(p.status,p.status)}</td><td class="num mono">${fmt(p.entry_price,6)}</td><td class="num mono">${fmt(p.current_price,6)}</td><td class="num ${pnl>=0?'success':'danger'}">${usd(pnl)}</td><td class="num ${n(p.pnl_pct)>=0?'success':'danger'}">${pct(n(p.pnl_pct)*100)}</td><td class="num">$${fmt(p.notional_usd,0)}</td><td>${esc(p.signal_source||p._source||'—')}</td></tr>`}).join('')}
function renderOpsPage(){const s=state.snap||{};const c=chart('cost-chart');if(c)c.setOption({backgroundColor:'transparent',grid:{left:55,right:20,top:20,bottom:35},xAxis:{type:'category',data:['Spent','Remaining','Limit'],axisLabel:{color:'#8f8f9b'}},yAxis:{type:'value',axisLabel:{color:'#8f8f9b',formatter:v=>'$'+v},splitLine:{lineStyle:{color:'#2b2b34'}}},series:[{type:'bar',barWidth:34,data:[n(s.api_cost_today_usd),n(s.budget_remaining_usd||100),n(s.budget_limit_usd||100)],itemStyle:{borderRadius:[10,10,0,0],color:p=>['#fc72ff','#35d07f','#7a5cff'][p.dataIndex]}}]});$('#services-grid').innerHTML=(s.services||[]).map(x=>{const hb=x.heartbeat;let st='disabled';if(hb)st=hb.is_stale?'stale':'live';else if(x.enabled_on_vps)st='enabled';return`<div class="service"><strong>${esc(x.name)}</strong><span>${esc(x.kind||'daemon')} · ${st}</span></div>`}).join('')}

/* ─── Round 10 (2026-05-24): Vision-model picker ─────────────────────────── */
/* The Settings tab fetches /api/settings/vision-models, builds 3 dropdowns
   (primary / fallback / prefilter), and wires a Test button that runs the
   chosen model on the most recent successfully-interpreted chart so the
   operator can eyeball quality before committing. */

const SLOT_ORDER=['primary','fallback','prefilter'];
const SLOT_LABELS={
  primary:{title:'Primary vision model',blurb:'Reads charts and extracts entry/SL/TP. The most-called slot \u2014 prioritise extraction quality.'},
  fallback:{title:'Fallback vision model',blurb:'Used only if the primary fails or rate-limits. Doesn\u2019t need to be the cheapest \u2014 it should be reliable.'},
  prefilter:{title:'Prefilter classifier',blurb:'Cheap binary \u201cis this image a chart at all?\u201d gate. Runs on every media item, so keep it cheap.'}
};

async function fetchSettings(){
  const [s,h]=await Promise.all([
    api('/api/settings/vision-models',{skipCompany:true}),
    api('/api/settings/vision-model-history?limit=20',{skipCompany:true})
  ]);
  state.settings={slots:s.slots||{},catalogue:s.catalogue||{models:[]},history:h.history||[]};
}

function priceLabel(m){
  const p=m.input_cost_per_million_tokens_usd;
  if(p==null)return '';
  return `$${Number(p).toFixed(2)}/M in`;
}

function modelOptions(slot,catalogue,selectedId){
  const models=(catalogue.models||[]).filter(m=>m.vision!==false);
  return models.map(m=>{
    const recommended=(m.recommended_for||[]).includes(slot);
    const sel=m.id===selectedId?' selected':'';
    const tag=recommended?' \u2605':'';
    return `<option value="${esc(m.id)}"${sel}>${esc(m.label||m.id)}${tag} \u2014 ${priceLabel(m)}</option>`;
  }).join('');
}

function modelNotes(catalogue,modelId){
  const m=(catalogue.models||[]).find(x=>x.id===modelId);
  if(!m)return '';
  const out=`<div class="model-notes-line"><strong>${esc(m.label||m.id)}</strong> <span class="secondary">\u00b7 ${esc(m.vendor||'?')} \u00b7 in $${Number(m.input_cost_per_million_tokens_usd||0).toFixed(2)}/M, out $${Number(m.output_cost_per_million_tokens_usd||0).toFixed(2)}/M</span></div><div class="model-notes-body">${esc(m.notes||'')}</div>`;
  return out;
}

function sourceBadge(src){
  const map={db:['cfg','from DB'],env:['env','from env var'],code_default:['def','code default'],default:['def','code default']};
  const [label,title]=map[src]||['',''];
  if(!label)return '';
  return `<span class="src-tag" title="${esc(title)}">${esc(label)}</span>`;
}

/* Round 13.6 (2026-05-24): per-trader pending uniqueness is now
   unconditional (each trader = 1 long + 1 short max per coin), so the
   tolerance/freshness/lookback knobs were retired. The Settings panel
   description in index.html documents the rule for the operator. */

/* ─── Telegram feed — channel selector + messages ─── */
function renderTelegramPage(){
  let rows=state.telegram?.rows||[];
  const ch=$('#telegram-channel')?.value;
  if(ch)rows=rows.filter(r=>r.channel_name===ch||r.author===ch);
  rows=filterRows(rows,$('#telegram-filter')?.value,['author','content','headline','channel_name']);
  if($('#telegram-media')?.value==='media')rows=rows.filter(r=>r.has_media);
  $('#telegram-list').innerHTML=rows.map(newsMsg).join('')||'<div class="empty">No Telegram messages</div>';
  $$('#telegram-list [data-newsrow]').forEach(el=>el.onclick=()=>openDiscordDrawer(el.dataset.newsrow,el.dataset.newssrc));
  if(!document.getElementById('telegram-channel')._populated){
    const channels=new Set((state.telegram?.rows||[]).map(r=>r.channel_name||r.author).filter(Boolean));
    const sel=document.getElementById('telegram-channel');
    channels.forEach(c=>{const o=document.createElement('option');o.value=c;o.textContent=c;sel.appendChild(o)});
    sel._populated=true;
  }
}

async function _putAPI(path,body){
  const u=new URL(path.replace(/^\//,''),document.baseURI);
  const r=await fetch(u,{method:'PUT',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const j=await r.json();
  if(!r.ok)throw j;
  return j;
}

async function fetchSourcesAndPrompts(){
  try{
    const [src,pr]=await Promise.all([
      api('/api/settings/sources',{skipCompany:true}),
      api('/api/settings/prompts',{skipCompany:true})
    ]);
    state.sources=src.sources||[];
    state.prompts=pr.prompts||[];
  }catch(e){console.error('fetchSourcesAndPrompts error',e);state.sources=[];state.prompts=[];}
  renderSourcesTree();
}

function renderSourcesTree(){
  const el=$('#sources-tree');
  if(!el)return;
  const sources=state.sources||[];
  const prompts=state.prompts||[];
  if(!sources.length){el.innerHTML='<div class="empty">Loading sources…</div>';return;}

  let h='';

  sources.forEach(src=>{
    const srcLabel=src.source.toUpperCase();
    const srcClass=src.source==='telegram'?'src-telegram':src.source==='discord'?'src-discord':'src-api';
    const channels=Object.values(src.channels||{});

    channels.forEach(ch=>{
      const users=ch.users||[];
      const allTracked=users.every(u=>u.is_tracked);

      h+=`<div class="panel full" style="margin-top:0">
        <div class="panel-head">
          <div>
            <h2>${esc(ch.channel_name)} <span class="src-tag ${srcClass}">${esc(srcLabel)}</span></h2>
            <p>${users.length} trader${users.length!==1?'s':''} · 
              <label class="check-inline">
                <input type="checkbox" class="channel-all-cb" data-channel="${esc(ch.channel_id)}" 
                  ${allTracked?'checked':''}> Follow all
              </label>
            </p>
          </div>
        </div>
        <div class="table-wrap">
        <table class="data-table">
          <thead><tr>
            <th style="width:40px">On</th>
            <th>Trader</th>
            <th>Type</th>
            <th>Media</th>
            <th>Prompt</th>
            <th style="width:60px">Score</th>
          </tr></thead>
          <tbody>`;

      users.forEach(u=>{
        const score=u.accuracy_score!=null&&u.accuracy_samples>0
          ? `${(u.accuracy_score*100).toFixed(0)}% <span class="secondary">n=${u.accuracy_samples}</span>`
          : '<span class="secondary">—</span>';
        const typeBadge=u.trader_type==='pro'?'<span class="badge badge-pro">pro</span>':
                         u.trader_type==='bot'?'<span class="badge badge-bot">bot</span>':
                         `<span class="badge neutral">${esc(u.trader_type||'—')}</span>`;

        h+=`<tr class="user-row" data-trader-id="${u.id}">
          <td><input type="checkbox" class="user-cb" data-trader-id="${u.id}" ${u.is_tracked?'checked':''}></td>
          <td><span class="primary">${esc(u.display_name||u.handle)}</span>
            ${u.handle!==u.display_name?`<br><span class="secondary mono">${esc(u.handle)}</span>`:''}</td>
          <td>${typeBadge}</td>
          <td><select class="select media-types" data-trader-id="${u.id}" style="width:auto;padding:4px 8px;font-size:12px">
            <option value="all" ${u.tracked_media_types==='all'?'selected':''}>All</option>
            <option value="media" ${u.tracked_media_types==='media'?'selected':''}>Media only</option>
            <option value="text" ${u.tracked_media_types==='text'?'selected':''}>Text only</option>
          </select></td>
          <td><select class="select prompt-pick" data-trader-id="${u.id}" style="width:auto;padding:4px 8px;font-size:12px">
            <option value="">Channel default</option>
            ${prompts.map(p=>`<option value="${esc(p.key)}" ${u.prompt_id===p.key?'selected':''}>${esc(p.key.replace('/prompt',''))}</option>`).join('')}
          </select></td>
          <td>${score}</td>
        </tr>`;
      });

      h+=`</tbody></table></div></div>`;
    });
  });

  // Prompt management
  h+=`<div class="panel full" style="margin-top:14px">
    <div class="panel-head">
      <h2>Prompt Library</h2>
      <button class="pill-btn" id="btn-new-prompt">+ New Prompt</button>
    </div>
    <div class="table-wrap">
    <table class="data-table">
      <thead><tr><th>Key</th><th>Preview</th><th style="width:80px"></th></tr></thead>
      <tbody>`;
  prompts.forEach(p=>{
    h+=`<tr>
      <td class="mono">${esc(p.key)}</td>
      <td class="secondary">${esc(p.preview||'')}</td>
      <td><button class="pill-btn" data-prompt-key="${esc(p.key)}" style="padding:4px 12px;font-size:11px">Edit</button></td>
    </tr>`;
  });
  h+=`</tbody></table></div></div>
    <div id="prompt-editor" class="panel full hidden" style="margin-top:14px"></div>`;

  el.innerHTML=h;

  // Wire events
  document.querySelectorAll('#sources-tree .channel-all-cb').forEach(cb=>cb.onchange=()=>toggleChannelAll(cb));
  document.querySelectorAll('#sources-tree .user-cb').forEach(cb=>cb.onchange=()=>toggleUser(cb));
  document.querySelectorAll('#sources-tree .media-types').forEach(sel=>sel.onchange=()=>updateUserTrack(sel));
  document.querySelectorAll('#sources-tree .prompt-pick').forEach(sel=>sel.onchange=()=>updateUserTrack(sel));
  // Prompt editor buttons
  document.querySelectorAll('#sources-tree [data-prompt-key]').forEach(btn=>btn.onclick=()=>showPromptEditor(btn.dataset.promptKey));
  const newBtn=$('#btn-new-prompt');
  if(newBtn)newBtn.onclick=()=>showPromptEditor('new');
}

async function toggleChannelAll(cb){
  const channel=cb.dataset.channel;
  const source=cb.dataset.source;
  const checked=cb.checked;
  // Find all user checkboxes in this channel
  const rows=document.querySelectorAll(`.user-row`);
  rows.forEach(row=>{
    const userCb=row.querySelector('.user-cb');
    if(!userCb)return;
    // Match by traversing up to channel-group
    const chGrp=row.closest('.channel-group');
    const chAll=chGrp?.querySelector('.channel-all-cb');
    if(chAll?.dataset.channel===channel){
      userCb.checked=checked;
      updateUserTrack(userCb);
    }
  });
}

async function toggleUser(cb){
  await updateUserTrack(cb);
  // Update channel all checkbox
  const row=cb.closest('.user-row');
  const chGrp=row?.closest('.channel-group');
  if(chGrp){
    const allCb=chGrp.querySelector('.channel-all-cb');
    const userCbs=chGrp.querySelectorAll('.user-cb');
    const allChecked=Array.from(userCbs).every(c=>c.checked);
    const someChecked=Array.from(userCbs).some(c=>c.checked);
    if(allCb){
      allCb.checked=allChecked;
      allCb.indeterminate=someChecked&&!allChecked;
    }
  }
}

async function updateUserTrack(el){
  const traderId=el.dataset.traderId;
  const row=el.closest('.user-row')||document.querySelector(`.user-row[data-trader-id="${traderId}"]`);
  const cb=row?.querySelector('.user-cb');
  const mediaSel=row?.querySelector('.media-types');
  const promptSel=row?.querySelector('.prompt-pick');
  const body={
    id:parseInt(traderId),
    is_tracked:cb?cb.checked:undefined,
    tracked_media_types:mediaSel?.value||undefined,
    prompt_id:promptSel?.value||null
  };
  try{
    await _putAPI('/api/settings/track',body);
  }catch(e){console.error('track update failed',e);}
}

function showPromptEditor(key){
  const ed=$('#prompt-editor');
  if(!ed)return;
  if(key==='new'){
    ed.classList.remove('hidden');
    ed.innerHTML=`
      <div class="panel-head"><h2>New Prompt</h2></div>
      <div style="padding:16px;display:flex;flex-direction:column;gap:12px">
        <label class="settings-label">Key
          <input class="input wide" id="prompt-key" placeholder="e.g. discord/charthackers/prompt">
        </label>
        <label class="settings-label">System Prompt
          <textarea class="input wide" id="prompt-system" rows="10" style="font-family:var(--mono);font-size:12px"></textarea>
        </label>
        <label class="settings-label">User Prompt Template
          <textarea class="input wide" id="prompt-user" rows="3" style="font-family:var(--mono);font-size:12px"></textarea>
        </label>
        <div style="display:flex;gap:8px">
          <button class="pill-btn" id="btn-save-prompt">Save</button>
          <button class="pill-btn" id="btn-cancel-prompt">Cancel</button>
        </div>
      </div>`;
    $('#btn-save-prompt').onclick=()=>savePrompt('new');
    $('#btn-cancel-prompt').onclick=()=>ed.classList.add('hidden');
  }else{
    api(`/api/settings/prompts/${encodeURIComponent(key)}`,{skipCompany:true}).then(data=>{
      ed.classList.remove('hidden');
      ed.innerHTML=`
        <div class="panel-head"><h2>Edit: <span class="mono">${esc(key)}</span></h2></div>
        <div style="padding:16px;display:flex;flex-direction:column;gap:12px">
          <label class="settings-label">System Prompt
            <textarea class="input wide" id="prompt-system" rows="10" style="font-family:var(--mono);font-size:12px">${esc(data.system_prompt||'')}</textarea>
          </label>
          <label class="settings-label">User Prompt Template
            <textarea class="input wide" id="prompt-user" rows="3" style="font-family:var(--mono);font-size:12px">${esc(data.user_prompt_template||'')}</textarea>
          </label>
          <div style="display:flex;gap:8px">
            <button class="pill-btn" id="btn-save-prompt">Save</button>
            <button class="pill-btn" id="btn-cancel-prompt">Cancel</button>
          </div>
        </div>`;
      $('#btn-save-prompt').onclick=()=>savePrompt(key);
      $('#btn-cancel-prompt').onclick=()=>ed.classList.add('hidden');
    }).catch(e=>{ed.innerHTML=`<div class="panel-head"><h2>Error</h2></div><div class="empty">Failed to load prompt</div>`;});
  }
}

async function savePrompt(key){
  const ed=$('#prompt-editor');
  const keyInput=$('#prompt-key');
  const actualKey=key==='new'?keyInput?.value:key;
  if(!actualKey){alert('Key is required');return;}
  const sp=$('#prompt-system')?.value||'';
  const ut=$('#prompt-user')?.value||'';
  try{
    await _putAPI(`/api/settings/prompts/${encodeURIComponent(actualKey)}`,
      {system_prompt:sp,user_prompt_template:ut});
    ed.classList.add('hidden');
    fetchSourcesAndPrompts();
  }catch(e){console.error('Save failed',e);}
}

function renderSettingsPage(){
  const body=$('#settings-body');
  if(!state.settings){
    if(body)body.innerHTML='<div class="empty">Loading model settings\u2026</div>';
    fetchSettings().then(()=>renderSettingsPage()).catch(e=>{
      if(body)body.innerHTML=`<div class="empty">Could not load settings: ${esc(String(e.error||e.message||e))}</div>`;
    });
    return;
  }
  const {slots,catalogue,history}=state.settings;
  const cards=SLOT_ORDER.map(slot=>{
    const info=slots[slot]||{};
    const meta=SLOT_LABELS[slot];
    const opts=modelOptions(slot,catalogue,info.model);
    const notes=modelNotes(catalogue,info.model);
    return `<div class="settings-card" data-slot="${slot}">
      <div class="settings-card-head">
        <h3>${esc(meta.title)} ${sourceBadge(info.source)}</h3>
        <p class="secondary">${esc(meta.blurb)}</p>
      </div>
      <label class="settings-label">Model
        <select class="select wide settings-select" data-slot="${slot}">${opts}</select>
      </label>
      <div class="model-notes" id="notes-${slot}">${notes}</div>
      <div class="settings-actions">
        <button class="pill-btn settings-test-btn" data-slot="${slot}">Test on a recent chart</button>
        <span class="settings-test-status" id="test-status-${slot}"></span>
      </div>
      <div class="settings-test-result" id="test-result-${slot}"></div>
    </div>`;
  }).join('');
  body.innerHTML=cards;

  /* Sources & Prompts section */
  fetchSourcesAndPrompts();

  /* History table */
  const histBody=history.length
    ? history.map(h=>`<tr><td class="mono small">${esc((h.changed_at||'').replace('T',' ').slice(0,19))}</td><td>${pill(h.slot,h.slot)}</td><td class="mono small">${esc(h.model_old||'\u2014')}</td><td class="mono small">${esc(h.model_new)}</td><td class="secondary small">${esc(h.actor_label||'')}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">No model changes recorded yet.</td></tr>';
  $('#settings-history').innerHTML=`<table class="data-table"><thead><tr><th>When</th><th>Slot</th><th>Old</th><th>New</th><th>Actor</th></tr></thead><tbody>${histBody}</tbody></table>`;

  wireSettings();
}

function wireSettings(){
  $$('.settings-select').forEach(sel=>{
    sel.onchange=async ()=>{
      const slot=sel.dataset.slot;
      const model=sel.value;
      const status=$(`#test-status-${slot}`);
      if(status)status.textContent='Saving\u2026';
      try{
        const r=await fetch(new URL('api/settings/vision-model',document.baseURI),{
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({slot,model})
        });
        const j=await r.json();
        if(!r.ok||!j.ok){throw j;}
        if(status)status.textContent=`Saved \u2014 ${slot} now ${model}. Effective within 60 s.`;
        state.settings.slots=j.slots;
        const notesEl=$(`#notes-${slot}`);
        if(notesEl)notesEl.innerHTML=modelNotes(state.settings.catalogue,model);
        await refreshHistory();
        setTimeout(()=>{if(status)status.textContent='';},6000);
      }catch(e){
        console.error('settings: save failed',e);
        if(status)status.textContent=`\u26a0 ${(e.error||e.message||'save failed')}`;
      }
    };
  });
  $$('.settings-test-btn').forEach(btn=>{
    btn.onclick=async ()=>{
      const slot=btn.dataset.slot;
      const sel=$(`select[data-slot="${slot}"]`);
      const model=sel?sel.value:null;
      if(!model)return;
      const status=$(`#test-status-${slot}`);
      const result=$(`#test-result-${slot}`);
      if(status)status.textContent='Testing\u2026 (this can take 5\u201320s)';
      if(result)result.innerHTML='';
      btn.disabled=true;
      try{
        const r=await fetch(new URL('api/settings/test-vision-model',document.baseURI),{
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({model})
        });
        const j=await r.json();
        if(!r.ok||!j.ok){throw j;}
        if(status)status.textContent=`Done in ${j.elapsed_seconds}s on media #${j.media_id}.`;
        const usage=j.usage||{};
        const summary=j.parsed?{
          instrument:j.parsed.instrument,
          direction:j.parsed.direction,
          confidence:j.parsed.confidence,
          trader_trades:Array.isArray(j.parsed.trader_trades)?j.parsed.trader_trades.length:'\u2014',
          chart_hacker_trades:Array.isArray(j.parsed.chart_hacker_trades)?j.parsed.chart_hacker_trades.length:'\u2014'
        }:{};
        const sumRows=Object.entries(summary).map(([k,v])=>`<tr><td class="secondary">${esc(k)}</td><td class="mono">${esc(JSON.stringify(v))}</td></tr>`).join('');
        if(result)result.innerHTML=`
          <div class="settings-test-pane">
            <div class="settings-test-head"><strong>${esc(j.model_resolved||model)}</strong> <span class="secondary">\u00b7 prompt v${esc(j.prompt_version||'?')} \u00b7 tokens in/out: ${esc(String(usage.prompt_tokens||'\u2014'))}/${esc(String(usage.completion_tokens||'\u2014'))}</span></div>
            <table class="data-table small"><tbody>${sumRows}</tbody></table>
            <details class="settings-test-raw"><summary>Raw JSON output</summary><pre class="mono small">${esc(JSON.stringify(j.parsed||j.raw_content,null,2))}</pre></details>
            ${j.parse_error?`<div class="danger small">Parse error: ${esc(j.parse_error)}</div>`:''}
          </div>`;
      }catch(e){
        console.error('settings: test failed',e);
        if(status)status.textContent=`\u26a0 ${(e.error||e.message||'test failed')}`;
      }finally{
        btn.disabled=false;
      }
    };
  });
}

async function refreshHistory(){
  try{
    const h=await api('/api/settings/vision-model-history?limit=20',{skipCompany:true});
    state.settings.history=h.history||[];
    const histBody=state.settings.history.length
      ? state.settings.history.map(r=>`<tr><td class="mono small">${esc((r.changed_at||'').replace('T',' ').slice(0,19))}</td><td>${pill(r.slot,r.slot)}</td><td class="mono small">${esc(r.model_old||'\u2014')}</td><td class="mono small">${esc(r.model_new)}</td><td class="secondary small">${esc(r.actor_label||'')}</td></tr>`).join('')
      : '<tr><td colspan="5" class="empty">No model changes recorded yet.</td></tr>';
    $('#settings-history').innerHTML=`<table class="data-table"><thead><tr><th>When</th><th>Slot</th><th>Old</th><th>New</th><th>Actor</th></tr></thead><tbody>${histBody}</tbody></table>`;
  }catch(e){console.warn('settings: history refresh failed',e);}
}
function renderSignalsTable(rows){table('#signals-table',[{label:'Signal'},{label:'Side'},{label:'Entry',num:1},{label:'SL',num:1},{label:'TP1',num:1},{label:'Δ entry',num:1},{label:'Status'},{label:'Call text'}],[sigRows(rows)]);wireRows()}
/* Round 12 (2026-05-24): renderPositionsTable removed.
   Positions tab now uses Live/Historic split via renderPositionsLive() and
   renderPositionsHistoric(). The legacy single-table renderer had no callers
   after the Round 11 split. */
function renderTradersPage(){const pm=perfMap();let rows=(state.snap?.leaderboard||[]).map(t=>({...t,perf:pm[t.actor_id]||pm[`jarvais_${t.actor_id}`]}));const q=$('#traders-filter')?.value;rows=filterRows(rows,q,['actor_id','display_name','platform']);const sort=$('#traders-sort')?.value||'success';rows.sort((a,b)=>sort==='trades'?n(b.perf?.total_trades||b.closed_position_count)-n(a.perf?.total_trades||a.closed_position_count):sort==='pnl'?n(b.perf?.total_pnl)-n(a.perf?.total_pnl):sort==='edge'?n(b.edge_score)-n(a.edge_score):n(b.perf?.win_rate||0)-n(a.perf?.win_rate||0));const body=rows.map(t=>{const p=t.perf||{}, name=t.display_name||t.actor_id;return`<tr data-trader="${esc(t.actor_id)}"><td>${rowMain(name,`${t.platform||'discord'} · ${t.actor_type||'unknown'}`)}</td><td class="num">${fmt(p.win_rate,1)}%</td><td class="num">${p.total_trades??t.closed_position_count??0}</td><td class="num success">${p.wins??'—'}</td><td class="num danger">${p.losses??'—'}</td><td class="num ${n(p.total_pnl)>=0?'success':'danger'}">${p.total_pnl!=null?usd(p.total_pnl):'—'}</td><td class="num">${fmt(t.edge_score,3)}</td><td>${esc(t._company||'—')}</td></tr>`}).join('');table('#traders-table',[{label:'Discord / actor'},{label:'Success',num:1},{label:'Trades',num:1},{label:'Wins',num:1},{label:'Losses',num:1},{label:'P&L',num:1},{label:'Edge',num:1},{label:'Company'}],[body]);$$('#traders-table tr[data-trader]').forEach(tr=>tr.onclick=()=>openTrader(tr.dataset.trader))}

/* ─── Discord drawer with per-chart tabs ─── */
async function openDiscordDrawer(newsItemId,newsSrc){
  if(!newsItemId)return;
  const src=newsSrc||'discord';
  const label=src==='telegram'?'Telegram message':'Discord message';
  const kicker=src==='telegram'?'TELEGRAM SIGNAL':'DISCORD SIGNAL';
  openDrawer(label,kicker,'<div class="empty">Loading charts from this message…</div>');
  try{
    const p=new URLSearchParams();
    p.set('news_item_id',newsItemId);
    const res=await api(`/api/interpretations/drawer?${p}`);
    const allSignals=(res.rows||[]);
    if(!allSignals.length){
      $('#drawer-body').innerHTML='<div class="empty">No signal interpretations for this message yet</div>';
      return;
    }
    drawDiscordTabs(allSignals,res);
  }catch(e){
    console.error('Discord drawer failed',e);
    $('#drawer-body').innerHTML='<div class="empty">Could not load charts</div>';
  }
}

function drawDiscordTabs(signals,res){
  const msg=res.news||signals[0]||{};
  const trader=traderName(signals[0])||msg.author||'trader';
  let html=`<div class="discord-drawer-msg">
    <div class="discord-avatar">${initials(trader)}</div>
    <div><div class="discord-user">${esc(trader)}</div>
    <div class="discord-text big">${esc(msg.content||msg.headline||signals[0]?.raw_signal_text||'')}</div></div>
  </div>`;

  // Build tabs from ALL media items (charts) — not just interpreted ones
  const gallery=res.media_gallery||[];
  const allTabs=[];
  // Add interpreted charts first
  signals.forEach((s,i)=>{
    const mid=s.media?.id;
    const label=dispSymbol(s.instrument_symbol)||'Chart';
    allTabs.push({idx:i, label, sig:s, mediaId:mid, type:'sig'});
  });
  // Add gallery charts that don't have interpretations
  gallery.forEach((m,i)=>{
    if(!signals.some(s=>s.media?.id==m.id)){
      const label=m.media_type||'Chart';
      allTabs.push({idx:signals.length+i, label, sig:null, mediaId:m.id, type:'media'});
    }
  });

  if(allTabs.length>1){
    html+=`<div class="chart-tabs">`;
    allTabs.forEach((t,i)=>{
      html+=`<button class="chart-tab ${i===0?'active':''}" data-chart-idx="${i}" data-tab-type="${t.type}" data-media-id="${t.mediaId||''}" data-sig-id="${t.sig?.id||''}">${esc(t.label)} ${t.sig?.consensus_direction||''}</button>`;
    });
    html+=`</div>`;
  }
  html+=`<div class="chart-tab-content" id="chart-tab-content">Loading chart #1…</div>`;
  $('#drawer-body').innerHTML=html;

  // Wire tabs
  $$('.chart-tab').forEach(b=>b.onclick=()=>{
    $$('.chart-tab').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    const sigId=b.dataset.sigId;
    const mediaId=b.dataset.mediaId;
    if(sigId){
      const sig=signals.find(s=>String(s.id)===sigId);
      if(sig) loadChartTab(sig,parseInt(b.dataset.chartIdx));
    }else if(mediaId){
      loadMediaTab(mediaId);
    }
  });

  // Load first tab
  const first=allTabs[0];
  if(first.type==='sig') loadChartTab(first.sig,0);
  else loadMediaTab(first.mediaId);
}

async function loadMediaTab(mediaId){
  const el=$('#chart-tab-content'); if(!el)return;
  el.innerHTML=`<div class="drawer-panel"><div class="posted-chart" style="border:0;background:transparent">
    <img class="chart-img xl" src="api/media/${mediaId}" onerror="this.onerror=null;this.parentElement.innerHTML='<div class=empty>Chart image not available</div>'">
  </div></div>`;
}

async function loadChartTab(sig,idx){
  const el=$('#chart-tab-content'); if(!el)return;
  if(!sig||!sig.id){el.innerHTML='<div class="empty">No interpretation ID for this chart</div>';return}
  el.innerHTML='<div class="empty">Loading candles & replay…</div>';
  try{
    const r=await api(`/api/signal-replay?id=${encodeURIComponent(sig.id)}`);
    drawReplayInline(r,el);
  }catch(e){
    el.innerHTML=`<div class="empty">Failed to load chart replay</div>`;
  }
}

function drawReplayInline(r,parentEl){
  if(!r||!r.ok){parentEl.innerHTML='<div class="empty">No replay data</div>';return}
  const sig=r.signal||{}, pos=r.position||{}, levels=sig.levels||{}, trader=r.trader||{}, news=r.news||{};
  const traderName=trader.handle_raw||trader.display_name||trader.handle_normalized||news.author||'trader';
  const outcome=pos.outcome||pos.status||'tracking';
  const pnl=n(pos.realized_pnl_usd_final??pos.realized_pnl_usd??pos.unrealized_pnl_usd);
  const media=r.media_url||r.annotated_chart_url;
  // Render the trader's call timestamp in the chart header so the viewer can
  // see exactly which moment the post lined up with on the candle replay.
  const callIso=r.call_ts?String(r.call_ts):'';
  const callPretty=callIso?callIso.slice(0,16).replace('T',' ')+' UTC':'';
  parentEl.innerHTML=`
    <section class="drawer-panel chart-panel">
      <div class="chart-toolbar"><div><h3>${esc(r.symbol)} · ${esc(r.timeframe)} · ${r.coverage?.candle_count||0} candles</h3><p>${esc(sig.direction||'')} · ${sig.confidence?fmt(sig.confidence,3):''} confidence${callPretty?` · call ${esc(callPretty)}`:''}</p></div><div class="replay-actions"><button class="mini" id="replay-play">Replay</button><button class="mini" id="replay-reset">Reset</button><button class="mini" id="replay-spacing">Spacing: ${state.timeSpacing==='realtime'?'Time':'Bars'}</button></div></div>
      <div class="chart-split">
        ${media?`<div class="posted-chart"><img class="chart-img xl" src="${esc(media)}" loading="lazy" decoding="async" onerror="this.onerror=function(){this.style.display='none';this.parentNode.innerHTML='<div class=&quot;empty&quot;>Original/annotated chart unavailable</div>'};this.src='${esc(r.annotated_chart_url||'')}'"></div>`:''}
        <div id="disc-chart-${esc(r.id)}" class="replay-chart"></div>
      </div>
    </section>
    <section class="drawer-panel levels-panel"><h3>Entry / stop / targets</h3><div class="level-grid wide">${levelCards(levels)}</div></section>
    ${pos&&pos.id?`<section class="drawer-panel journey-panel"><h3>Trade journey · price &amp; P&amp;L over time</h3><div id="journey-chart-${esc(pos.id)}" class="journey-chart"></div><p class="secondary journey-summary" id="journey-summary-${esc(pos.id)}">Loading…</p></section>`:''}
    <div class="drawer-grid bottom-grid">
      <section class="drawer-panel"><h3>Trade state</h3><div class="state-grid"><div><label>Outcome</label><strong>${esc(outcome)}</strong></div><div><label>P&L</label><strong class="${pnl>=0?'success':'danger'}">${usd(pnl)}</strong></div><div><label>Max profit</label><strong>${fmt(pos.max_profit_pct,2)}%</strong></div><div><label>Max drawdown</label><strong>${fmt(pos.max_drawdown_pct,2)}%</strong></div><div><label>Distance entry</label><strong>${fmt(pos.distance_to_entry_pct,2)}%</strong></div><div><label>Distance TP1</label><strong>${fmt(pos.distance_to_tp1_pct,2)}%</strong></div></div></section>
      <section class="drawer-panel"><h3>Reasoning</h3><p class="call-text big"><b>Trader:</b> ${esc(sig.trader_stated_thesis||'—')}</p><p class="call-text big"><b>AI thesis:</b> ${esc(sig.llm_inferred_thesis||'—')}</p><p class="call-text big"><b>LLM:</b> ${esc(sig.llm_reasoning||'—')}</p><p class="call-text big"><b>ChartHacker:</b> ${esc(sig.ai_comment||'—')}</p></section>
      <section class="drawer-panel"><h3>Tags</h3><div class="tag-row">${Object.entries(sig.tags||{}).flatMap(([k,v])=>Array.isArray(v)?v.map(x=>pill(`${k}:${x}`)):[]).join('')||'<span class="secondary">No tags</span>'}</div><p class="secondary">Agreement: reason ${fmt(sig.reason_agreement_score,3)} · AI ${fmt(sig.ai_agreement_score,2)}</p></section>
    </div>`;
  // Init chart
  const chartId='disc-chart-'+r.id;
  // Round 12 (2026-05-24): kick off async journey-chart fetch alongside replay.
  if(pos&&pos.id){setTimeout(()=>renderTradeJourney(pos.id),120)}
  setTimeout(()=>{
    state._discReplay=r;
    state._discChartId=chartId;
    renderReplayChart(r,chartId);
    const playBtn=parentEl.querySelector('#replay-play');
    const resetBtn=parentEl.querySelector('#replay-reset');
    const spacingBtn=parentEl.querySelector('#replay-spacing');
    if(playBtn)playBtn.onclick=()=>animateReplay(r,chartId);
    if(resetBtn)resetBtn.onclick=()=>renderReplayChart(r,chartId);
    if(spacingBtn)spacingBtn.onclick=()=>{
      state.timeSpacing=state.timeSpacing==='realtime'?'contiguous':'realtime';
      try{localStorage.setItem('tickles.replay.spacing',state.timeSpacing)}catch(e){}
      spacingBtn.textContent=`Spacing: ${state.timeSpacing==='realtime'?'Time':'Bars'}`;
      renderReplayChart(r,chartId);
    };
  },100);
}

/* ─── Radar / Entry watch ─── */
function signalDistance(s){if(Number.isFinite(s._liveDist))return s._liveDist;const live=n(s.current_price||s._livePrice);const entry=n(s.entry_price||s.levels?.entry);if(!entry)return 999999;if(live)return Math.abs((live-entry)/entry*100);const d=Math.abs(n(s.distance_to_entry_pct));return d?d:999999}
// 2026-05-24 (Round 9) — Signal-source helper.
// Returns a small inline badge that flags positions / radar cards where
// the entry was inferred independently by ChartHacker (the AI vision
// agent) rather than explicitly called by the human trader. Without this
// the dashboard couldn't visually distinguish "trader said long here" from
// "AI inferred a long from a chart with no setup boxes".
function aiInferredBadge(s){
  const src=String(s?.signal_source||s?._source||'').toLowerCase();
  if(src!=='chart_hacker')return '';
  return '<span class="badge ai-inferred" title="Trade inferred independently by ChartHacker AI — trader did not explicitly mark this setup">AI INFERRED</span>';
}
function radarCard(s,i){const id=s.signal_interpretation_id||s.id;const sym=dispSymbol(s.symbol||s.instrument_symbol);const dir=(s.consensus_direction||s.direction||'').toLowerCase();const entry=n(s.entry_price||s.levels?.entry), sl=n(s.stop_loss||s.levels?.stop_loss), tp=n(s.take_profit_1||s.levels?.take_profit_1);const dist=signalDistance(s);const ageDays=(Date.now()-new Date(s.signal_timestamp||s.created_at))/86400000;const trader=traderName(s);const stale=ageDays>7?' stale':'';const rr=entry&&sl&&tp?Math.abs((tp-entry)/(entry-sl)):0;const live=(s.current_price||s._livePrice)?` · live ${fmt(s.current_price||s._livePrice,6)}`:'';const aiBadge=aiInferredBadge(s);return `<div class="radar-card${stale}" data-call="${esc(id)}" data-news="${esc(s.news_item_id||'')}" data-radar-idx="${i}"><div class="radar-top"><div>${rowMain(sym,`${trader} · ${(s.timeframe||'1m')} · ${rel(s.signal_timestamp||s.created_at)} old${live}`)}</div><div class="radar-top-right">${aiBadge}${pill(dir,dir)}</div></div><div class="mini-tv" id="mini-radar-${i}">${(!s._candles?.length&&s.media_url)?`<img class="mini-chart-img" src="${esc(s.media_url)}">`:''}<div class="riskbox ${dir==='short'?'short':'long'}"><i class="reward"></i><i class="risk"></i><b class="entry-line"></b></div><span class="mini-loading">${s._candles?.length?'':'chart snapshot / no local candles'}</span></div><div class="radar-metrics"><div><label>to entry</label><strong>${dist===999999?'—':fmt(dist,2)+'%'}</strong></div><div><label>entry</label><strong>${fmt(entry,6)}</strong></div><div><label>SL</label><strong>${fmt(sl,6)}</strong></div><div><label>TP1</label><strong>${fmt(tp,6)}</strong></div><div><label>R:R</label><strong>${rr?fmt(rr,2):'—'}</strong></div><div><label>TF</label><strong>${esc(s.timeframe||'1m')}</strong></div><div><label>age</label><strong>${fmt(ageDays,1)}d</strong></div></div></div>`}
async function renderRadarPage(){
  const isFirstLoad = !$('#entry-radar').children.length || $('#entry-radar').querySelector('.empty');
  if(isFirstLoad) {
    $('#entry-radar').innerHTML='<div class="empty">Scanning pending calls and removing entries already triggered / stopped…</div>';
  }
  let payload;
  try{ payload=await api('/api/entry-radar?limit=120'); }
  catch(e){
    console.error('entry radar API failed',e);
    if(isFirstLoad) $('#entry-radar').innerHTML='<div class="empty">Entry radar unavailable</div>';
    return;
  }
  let rows=payload.rows||[];
  rows=filterRows(rows,$('#radar-filter')?.value,['instrument_symbol','symbol','handle_raw','handle_normalized','display_name','actor_id','news_content','news_headline']);
  const maxAge=n($('#radar-age')?.value);
  if(maxAge)rows=rows.filter(s=>(Date.now()-new Date(s.signal_timestamp||s.created_at))/86400000<=maxAge);
  rows.sort((a,b)=>signalDistance(a)-signalDistance(b));
  $('#entry-radar').innerHTML=rows.map(radarCard).join('')||`<div class="empty">No actionable waiting entries. Filtered out ${payload.excluded_count||0} calls that already hit entry / SL / TP.</div>`;
  const info=document.createElement('div'); info.className='radar-summary'; info.textContent=`${rows.length} actionable · ${payload.excluded_count||0} already triggered/expired removed`;
  $('#entry-radar').prepend(info);
  wireRows(); rows.forEach((s,i)=>drawMiniRadar(`mini-radar-${i}`,s.mini_candles||s._candles||[],s));
}
function drawMiniRadar(id,candles,s){const el=document.getElementById(id);if(!el||!candles.length||!window.echarts)return;const loading=el.querySelector('.mini-loading'); if(loading)loading.remove();const c=echarts.init(el);const labels=candles.map(x=>String(x.timestamp).slice(11,16));const data=candles.map(x=>[n(x.open),n(x.close),n(x.low),n(x.high)]);const levels=s.levels||{};const entry=n(s.entry_price||levels.entry), sl=n(s.stop_loss||levels.stop_loss), tp=n(s.take_profit_1||levels.take_profit_1);const lines=[];if(entry)lines.push({yAxis:entry,name:'E',lineStyle:{color:'#fc72ff',width:1}});if(sl)lines.push({yAxis:sl,name:'SL',lineStyle:{color:'#ff5f72',width:1,type:'dashed'}});if(tp)lines.push({yAxis:tp,name:'TP',lineStyle:{color:'#35d07f',width:1,type:'dashed'}});c.setOption({animation:false,grid:{left:0,right:0,top:4,bottom:0},xAxis:{type:'category',data:labels,show:false},yAxis:{scale:true,show:false},series:[{type:'candlestick',data,itemStyle:{color:'#35d07f',color0:'#ff5f72',borderColor:'#35d07f',borderColor0:'#ff5f72'},markLine:{symbol:'none',label:{show:false},data:lines}}]})}

/* ─── Signal intelligence drawer (standalone call) ─── */
function levelCards(levels){
  return ['entry','stop_loss','take_profit_1','take_profit_2','take_profit_3','take_profit_4','take_profit_5','take_profit_6']
    .map(k=>`<div class="level"><label>${k.replaceAll('_',' ')}</label><strong>${fmt(levels?.[k],6)}</strong></div>`).join('')
}
// Render the Discord call body with explicit handling for reply-only posts.
// When the only content is a `[Reply to @user]: <quoted text>` block, the
// replying author contributed NO own words — the trade signal therefore
// came from the chart (vision LLM), not from the text. We make that clear
// to the viewer instead of showing the parent's quoted words as if they
// were the trader's own signal text.
function renderDiscordCallBody(news,sig,r,traderName){
  const raw=String(news?.content||news?.headline||'').trim();
  const time=rel(news?.published_at||sig?.created_at||r?.call_ts);
  const head=`<div class="discord-head"><span class="discord-user">${esc(traderName)}</span><span class="discord-time">${time}</span></div>`;
  const replyMatch=raw.match(/^\[Reply to @([^\]]+)\]:\s*(.*)$/m);
  let bodyHtml;
  if(replyMatch){
    const parentName=replyMatch[1];
    const quoted=replyMatch[2].trim();
    // Find any text after the reply header (replyer's own words).
    const afterReply=raw.split('\n').slice(1).join('\n').trim();
    if(!afterReply){
      // Reply-only — no own text. Signal came from the attached chart.
      bodyHtml=`<div class="discord-text big" style="opacity:.85"><span class="secondary">↩ Replying to @${esc(parentName)}: <i>${esc(quoted)}</i></span></div><div class="discord-text big" style="margin-top:6px;color:#8f8f9b;font-style:italic">No text from ${esc(traderName)} — signal extracted from attached chart by AI vision.</div>`;
    }else{
      // Reply WITH own text after.
      bodyHtml=`<div class="discord-text big" style="opacity:.7;font-size:.92em"><span class="secondary">↩ @${esc(parentName)}: <i>${esc(quoted)}</i></span></div><div class="discord-text big" style="margin-top:6px">${esc(afterReply)}</div>`;
    }
  }else{
    bodyHtml=`<div class="discord-text big">${esc(raw||'(no text — chart only)')}</div>`;
  }
  return `<div class="discord-msg large"><div class="discord-avatar">${initials(traderName)}</div><div>${head}${bodyHtml}</div></div>`;
}
async function openCall(id,news){
  if(!id&&!news)return;
  openDrawer('Signal intelligence','CALL DETAIL','<div class="empty">Loading chart, candles, levels and memory…</div>');
  try{
    let replay=null;
    if(id){replay=await api(`/api/signal-replay?id=${encodeURIComponent(id)}`);drawReplay(replay);return}
    const p=new URLSearchParams();p.set('news_item_id',news);
    const res=await api(`/api/interpretations/drawer?${p}`);
    const first=(res.rows||[])[0];
    if(first?.id){replay=await api(`/api/signal-replay?id=${encodeURIComponent(first.id)}`);drawReplay(replay)}
    else{drawCallFallback(res,id)}
  }catch(e){console.error('openCall failed',e);$('#drawer-body').innerHTML='<div class="empty">Could not load chart/replay intelligence</div>'}
}
function drawReplay(r){
  if(!r||!r.ok){$('#drawer-body').innerHTML='<div class="empty">No replay data available</div>';return}
  const sig=r.signal||{}, pos=r.position||{}, levels=sig.levels||{}, trader=r.trader||{}, news=r.news||{};
  $('#drawer-title').textContent=`${r.symbol} ${sig.direction||''}`;
  // 2026-05-24 (Round 9) — surface signal_source in the kicker so the user
  // can immediately see whether a position was an explicit trader call or
  // an AI-inferred opinion from ChartHacker.
  const src=String(pos.signal_source||sig.signal_source||'').toLowerCase();
  const srcTag=src==='chart_hacker'?' · AI INFERRED (ChartHacker)':(src==='trader'?' · TRADER CALL':'');
  $('#drawer-kicker').textContent=`CALL DETAIL | INTERP ID: ${r.id}${pos.id ? ` · POS ID: ${pos.id}` : ''}${srcTag}`;
  const traderName=trader.handle_raw||trader.display_name||trader.handle_normalized||news.author||'trader';
  const outcome=pos.outcome||pos.status||'tracking';
  const pnl=n(pos.realized_pnl_usd_final??pos.realized_pnl_usd??pos.unrealized_pnl_usd);
  const media=r.media_url||r.annotated_chart_url;
  $('#drawer-body').innerHTML=`
    <div class="replay-layout">
      <section class="drawer-panel chart-panel">
        <div class="chart-toolbar"><div><h3>Trader chart / reconstructed replay</h3><p>${esc(r.symbol)} · ${esc(r.exchange)} · ${esc(r.timeframe)} · ${r.coverage?.candle_count||0} candles</p></div><div class="replay-actions"><button class="mini" id="replay-play">Replay</button><button class="mini" id="replay-reset">Reset</button><button class="mini" id="replay-spacing">Spacing: ${state.timeSpacing==='realtime'?'Time':'Bars'}</button></div></div>
        <div class="chart-split">
          <div class="posted-chart">${media?`<img class="chart-img xl" src="${esc(media)}" loading="lazy" decoding="async" onerror="this.onerror=function(){this.style.display='none';this.parentNode.innerHTML='<div class=&quot;empty&quot;>Original/annotated chart unavailable</div>'};this.src='${esc(r.annotated_chart_url||'')}'">`:'<div class="empty">No downloaded/remote chart found</div>'}</div>
          <div id="replay-chart" class="replay-chart"></div>
        </div>
      </section>
      <section class="drawer-panel levels-panel"><h3>Entry / stop / targets</h3><div class="level-grid wide">${levelCards(levels)}</div></section>
      ${pos&&pos.id?`<section class="drawer-panel journey-panel"><h3>Trade journey · price &amp; P&amp;L over time</h3><div id="journey-chart-${esc(pos.id)}" class="journey-chart"></div><p class="secondary journey-summary" id="journey-summary-${esc(pos.id)}">Loading…</p></section>`:''}
      <div class="drawer-grid bottom-grid">
        <section class="drawer-panel"><h3>Discord call</h3>${renderDiscordCallBody(news,sig,r,traderName)}</section>
        <section class="drawer-panel"><h3>Trade state</h3><div class="state-grid"><div><label>Outcome</label><strong>${esc(outcome)}</strong></div><div><label>P&L</label><strong class="${pnl>=0?'success':'danger'}">${usd(pnl)}</strong></div><div><label>Max profit</label><strong>${fmt(pos.max_profit_pct,2)}%</strong></div><div><label>Max drawdown</label><strong>${fmt(pos.max_drawdown_pct,2)}%</strong></div><div><label>Distance entry</label><strong>${fmt(pos.distance_to_entry_pct,2)}%</strong></div><div><label>Distance TP1</label><strong>${fmt(pos.distance_to_tp1_pct,2)}%</strong></div></div></section>
        <section class="drawer-panel"><h3>Reasoning / intelligence</h3><p class="call-text big"><b>Trader thesis:</b> ${esc(sig.trader_stated_thesis||'—')}</p><p class="call-text big"><b>AI thesis:</b> ${esc(sig.llm_inferred_thesis||'—')}</p><p class="call-text big"><b>LLM reasoning:</b> ${esc(sig.llm_reasoning||'—')}</p><p class="call-text big"><b>ChartHacker:</b> ${esc(sig.ai_comment||'—')}</p></section>
        <section class="drawer-panel"><h3>Memory / tags</h3><div class="tag-row">${Object.entries(sig.tags||{}).flatMap(([k,v])=>Array.isArray(v)?v.map(x=>pill(`${k}:${x}`)):[]).join('')||'<span class="secondary">No tags captured</span>'}</div><p class="secondary">Agreement: reason ${fmt(sig.reason_agreement_score,3)} · AI ${fmt(sig.ai_agreement_score,2)}</p></section>
      </div>
    </div>`;
  renderReplayChart(r);
  // Round 12 (2026-05-24): trade-journey chart in the standalone drawer.
  if(pos&&pos.id){setTimeout(()=>renderTradeJourney(pos.id),120)}
  $('#replay-play')?.addEventListener('click',()=>animateReplay(r));
  $('#replay-reset')?.addEventListener('click',()=>renderReplayChart(r));
  $('#replay-spacing')?.addEventListener('click',()=>{
    state.timeSpacing=state.timeSpacing==='realtime'?'contiguous':'realtime';
    try{localStorage.setItem('tickles.replay.spacing',state.timeSpacing)}catch(e){}
    const btn=$('#replay-spacing'); if(btn) btn.textContent=`Spacing: ${state.timeSpacing==='realtime'?'Time':'Bars'}`;
    renderReplayChart(r);
  });
}
function candleSeries(candles){return (candles||[]).map(c=>[c.timestamp,n(c.open),n(c.close),n(c.low),n(c.high)])}
function markLines(levels){
  const data=[]; const add=(name,val,color)=>{if(n(val))data.push({yAxis:n(val),name,lineStyle:{color,type:'dashed',width:1.4},label:{formatter:name,color}})};
  add('ENTRY',levels?.entry,'#fc72ff'); add('SL',levels?.stop_loss,'#ff5f72');
  ['take_profit_1','take_profit_2','take_profit_3','take_profit_4','take_profit_5','take_profit_6'].forEach((k,i)=>add(`TP${i+1}`,levels?.[k],'#35d07f'));
  return data;
}
function renderReplayChart(r,chartId='replay-chart',upto=null){
  const c=chart(chartId); if(!c)return;
  const candles=upto?(r.candles||[]).slice(0,upto):(r.candles||[]);
  const series=candleSeries(candles);
  const isTime=state.timeSpacing==='realtime';
  let xAxisOpt,seriesData,callXValue=null;
  // Resolve the trader-call timestamp once so we can drop a vertical marker.
  const callTs=r.call_ts?new Date(r.call_ts).getTime():null;
  if(isTime){
    xAxisOpt={type:'time',axisLabel:{color:'#8f8f9b',fontSize:10,formatter:val=>{const d=new Date(val);return`${String(d.getUTCMonth()+1).padStart(2,'0')}-${String(d.getUTCDate()).padStart(2,'0')} ${String(d.getUTCHours()).padStart(2,'0')}:${String(d.getUTCMinutes()).padStart(2,'0')}`}},axisLine:{lineStyle:{color:'#2b2b34'}}};
    seriesData=series.map(x=>[x[0],x[1],x[2],x[3],x[4]]);
    if(callTs)callXValue=callTs;
  }else{
    const labels=candles.map(x=>String(x.timestamp).slice(5,16).replace('T',' '));
    xAxisOpt={type:'category',data:labels,axisLabel:{color:'#8f8f9b',fontSize:10},axisLine:{lineStyle:{color:'#2b2b34'}}};
    seriesData=series.map(x=>[x[1],x[2],x[3],x[4]]);
    // For category axis, snap the call to the nearest candle label index so the
    // vertical marker shows the candle that contained the post.
    if(callTs&&candles.length){
      let bestIdx=0,bestDelta=Infinity;
      for(let i=0;i<candles.length;i++){
        const t=new Date(candles[i].timestamp).getTime();
        const d=Math.abs(t-callTs);
        if(d<bestDelta){bestDelta=d;bestIdx=i}
      }
      callXValue=labels[bestIdx];
    }
  }

  const levels=r.signal?.levels||r.levels||{};
  const lvlVals=Object.values(levels).map(n).filter(x=>x>0);
  // 2026-05-24: Compute y-axis bounds explicitly in JS. The previous version
  // passed `min`/`max` as ECharts CALLBACKS — that path interacted badly with
  // markLine auto-extents and produced a stuck "9999999" max tick on the
  // call-detail drawer chart (visible for INTERP 2586). Computing scalar
  // bounds here is deterministic and fixes the visual artefact.
  // We also override `axisLabel.formatter` because the default ECharts value
  // formatter — when handed a near-edge float like 82445.81999999999 — was
  // emitting "9999999" (likely an integer-overflow / pretty-print fallback
  // inside ECharts when the label width can't be measured cleanly). Forcing
  // our own `toLocaleString` formatter eliminates the artefact entirely.
  let yMin=null,yMax=null;
  let dataMin=Infinity,dataMax=-Infinity;
  for(const c of (candles||[])){
    const lo=n(c.low),hi=n(c.high),op=n(c.open),cl=n(c.close);
    if(lo>0&&lo<dataMin)dataMin=lo;
    if(op>0&&op<dataMin)dataMin=op;
    if(cl>0&&cl<dataMin)dataMin=cl;
    if(hi>0&&hi>dataMax)dataMax=hi;
    if(op>0&&op>dataMax)dataMax=op;
    if(cl>0&&cl>dataMax)dataMax=cl;
  }
  if(lvlVals.length){
    const minLvl=Math.min(...lvlVals);
    const maxLvl=Math.max(...lvlVals);
    if(isFinite(dataMin))dataMin=Math.min(dataMin,minLvl); else dataMin=minLvl;
    if(isFinite(dataMax))dataMax=Math.max(dataMax,maxLvl); else dataMax=maxLvl;
  }
  // Snap min/max to nice round numbers so the y-axis ticks are clean and the
  // edge labels never need extra horizontal room.
  if(isFinite(dataMin)&&isFinite(dataMax)&&dataMax>dataMin){
    const range=dataMax-dataMin;
    // Nice step: use the magnitude of the range to pick a 1/2/5 × 10^k step.
    const mag=Math.pow(10,Math.floor(Math.log10(range)));
    const candidates=[mag,mag*2,mag*5,mag*10];
    let step=mag;
    for(const s of candidates){if(range/s>=4&&range/s<=12){step=s;break}}
    yMin=Math.max(0,Math.floor(dataMin/step)*step);
    yMax=Math.ceil(dataMax/step)*step;
  }

  // Price markLines (entry/SL/TPs) + vertical "CALL" line at the candle that
  // contained the trader's post.
  const priceMarks=markLines(levels);
  const markData=priceMarks.slice();
  if(callXValue!==null){
    markData.push({xAxis:callXValue,name:'CALL',lineStyle:{color:'#f0b400',type:'solid',width:1.2,opacity:0.9},label:{formatter:'CALL',color:'#f0b400',position:'insideEndTop'}});
  }

  // Custom value formatter — see comment block above for why this is required.
  const valueFmt=v=>{const x=Number(v);if(!isFinite(x))return '';return x.toLocaleString(undefined,{maximumFractionDigits:2})};
  const yAxisOpt={type:'value',scale:true,boundaryGap:[0,0],axisLabel:{color:'#8f8f9b',formatter:valueFmt},splitLine:{lineStyle:{color:'#2b2b34'}}};
  if(yMin!==null&&yMax!==null){yAxisOpt.min=yMin;yAxisOpt.max=yMax}
  c.setOption({
    backgroundColor:'transparent',
    animation:false,
    grid:{left:66,right:28,top:22,bottom:48},
    tooltip:{trigger:'axis',axisPointer:{type:'cross',label:{formatter:p=>p.axisDimension==='y'?valueFmt(p.value):p.value}}},
    xAxis:xAxisOpt,
    yAxis:yAxisOpt,
    dataZoom:[{type:'inside',start:0,end:100},{type:'slider',height:14,bottom:0,start:0,end:100,labelFormatter:''}],
    series:[{
      type:'candlestick',
      data:seriesData,
      itemStyle:{color:'#35d07f',color0:'#ff5f72',borderColor:'#35d07f',borderColor0:'#ff5f72'},
      markLine:{symbol:'none',data:markData,silent:true}
    }]
  },true);
}
function animateReplay(r,chartId='replay-chart'){
  const total=(r.candles||[]).length; if(!total)return;
  let i=Math.max(10,Math.floor(total*.08));
  const step=Math.max(1,Math.floor(total/90));
  const timer=setInterval(()=>{i+=step;renderReplayChart(r,chartId,Math.min(i,total));if(i>=total)clearInterval(timer)},90);
}
function drawCallFallback(res,id){const rows=res.rows||[];if(!rows.length){$('#drawer-body').innerHTML='<div class="empty">No interpretation available yet</div>';return}const r=rows[0];const levels=r.levels||r.llm?.levels||{};const chartUrl=r.media?.url||`api/charts/${r.id}`;$('#drawer-title').textContent=`${r.instrument?.symbol||'Signal'} ${r.consensus_direction||''}`;
// 2026-05-24 (Round 9) — keep fallback kicker consistent with drawReplay's
// AI INFERRED / TRADER CALL annotation. Falls back gracefully if the
// drawer-provider response doesn't carry signal_source.
const _src=String(r.signal_source||r.position?.signal_source||r.signal?.signal_source||'').toLowerCase();
const _srcTag=_src==='chart_hacker'?' · AI INFERRED (ChartHacker)':(_src==='trader'?' · TRADER CALL':'');
$('#drawer-kicker').textContent=`CALL DETAIL | INTERP ID: ${r.id}${_srcTag}`;
$('#drawer-body').innerHTML=`<div class="drawer-panel chart-panel"><img class="chart-img xl" src="${chartUrl}" loading="lazy" decoding="async"><div class="level-grid wide">${levelCards(levels)}</div></div>`}
function openTrader(actor){const sigs=(state.snap?.signals||[]).filter(s=>[s.actor_id,s.trader_handle_normalized,s.trader_display_name].includes(actor)||JSON.stringify(s).includes(actor));const lessons=(state.learning?.feed?.rows||[]).filter(l=>String(l.actor||'').includes(actor));openDrawer(actor,'TRADER INTEL',`<div class="drawer-panel"><h3>Recent calls</h3><div class="table-wrap"><table class="data-table"><tbody>${sigRows(sigs,12)}</tbody></table></div></div><div class="drawer-panel"><h3>What AI learned about them</h3>${lessons.slice(0,8).map(r=>`<div class="lesson"><div class="lesson-body">${esc(r.body)}</div><div class="lesson-foot">${rel(r.ts)}</div></div>`).join('')||'<div class="secondary">No targeted lessons in current window.</div>'}</div>`);wireRows()}

/* ─── shared ui ─── */
function table(id,cols,rows,onSort){const head=cols.map(c=>`<th class="${c.num?'num ':''}${c.sort?'sortable':''}" data-key="${c.key||''}">${esc(c.label)}</th>`).join('');const body=rows.join('')||`<tr><td colspan="${cols.length}" class="empty">No data</td></tr>`;$(id).innerHTML=`<table class="data-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;if(onSort)$$(id+' th[data-key]').forEach(th=>th.onclick=()=>onSort(th.dataset.key))}
function filterRows(rows,q,fields){q=(q||'').toLowerCase();if(!q)return rows;return rows.filter(r=>fields.map(f=>String(r[f]??'')).join(' ').toLowerCase().includes(q)||JSON.stringify(r).toLowerCase().includes(q))}
function openDrawer(title,kicker,html){$('#drawer-title').textContent=title;$('#drawer-kicker').textContent=kicker;$('#drawer-body').innerHTML=html;$('#drawer').classList.remove('hidden');$('#drawer-backdrop').classList.remove('hidden')}
function closeDrawer(){$('#drawer').classList.add('hidden');$('#drawer-backdrop').classList.add('hidden')}
/* 2026-05-22 — drawer width persistence + resize-by-drag.
   Stored in localStorage as a px string under 'tickles.drawer.width'.
   Drag the 8px handle on the drawer's left edge LEFT to widen, RIGHT to shrink.
   Clamped to [380px, 95vw]. Touch + mouse supported. */
const DRAWER_LS_KEY='tickles.drawer.width';
function clampDrawerW(px){const max=Math.floor(window.innerWidth*0.95);return Math.max(380,Math.min(max,px))}
function applyDrawerW(px){document.documentElement.style.setProperty('--drawer-w',clampDrawerW(px)+'px')}
function loadDrawerWidth(){
  try{const v=localStorage.getItem(DRAWER_LS_KEY);if(!v)return;const px=parseInt(v,10);if(Number.isFinite(px)&&px>0)applyDrawerW(px)}catch(e){}
}
function saveDrawerWidth(px){try{localStorage.setItem(DRAWER_LS_KEY,clampDrawerW(px)+'px')}catch(e){}}
function attachDrawerResizer(){
  const handle=$('#drawer-resizer'),drawer=$('#drawer');
  if(!handle||!drawer)return;
  let startX=0,startW=0,dragging=false;
  const px=e=>e.touches?e.touches[0].clientX:e.clientX;
  const cleanup=()=>{dragging=false;document.body.classList.remove('drawer-resizing');handle.classList.remove('dragging');window.removeEventListener('mousemove',onMove);window.removeEventListener('mouseup',onUp);window.removeEventListener('touchmove',onMove);window.removeEventListener('touchend',onUp);window.removeEventListener('blur',cleanup);window.removeEventListener('mouseleave',cleanup)};
  const onDown=e=>{e.preventDefault();dragging=true;startX=px(e);startW=drawer.getBoundingClientRect().width;document.body.classList.add('drawer-resizing');handle.classList.add('dragging');window.addEventListener('mousemove',onMove);window.addEventListener('mouseup',onUp);window.addEventListener('touchmove',onMove,{passive:false});window.addEventListener('touchend',onUp);window.addEventListener('blur',cleanup);window.addEventListener('mouseleave',cleanup)};
  const onMove=e=>{if(!dragging)return;e.preventDefault();const dx=startX-px(e);applyDrawerW(startW+dx)};
  const onUp=()=>{if(!dragging)return;const finalW=drawer.getBoundingClientRect().width;cleanup();saveDrawerWidth(finalW)};
  handle.addEventListener('mousedown',onDown);
  handle.addEventListener('touchstart',onDown,{passive:false});
  // Double-click on the handle resets to the default (clears the saved width).
  handle.addEventListener('dblclick',()=>{try{localStorage.removeItem(DRAWER_LS_KEY)}catch(e){}document.documentElement.style.removeProperty('--drawer-w')});
  // Re-clamp when the window resizes so the drawer never overflows.
  window.addEventListener('resize',()=>{const cur=drawer.getBoundingClientRect().width;if(cur>window.innerWidth*0.95)applyDrawerW(cur)});
  // Hard safety net — if any page-lifecycle event hints we missed an
  // mouseup (visibility change, escape key), clear the resizing flag.
  document.addEventListener('visibilitychange',()=>{if(document.hidden)cleanup()});
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('drawer-resizing'))cleanup()});
}
function perfMap(){const m={};(state.agentPerf?.agent_performance||[]).forEach(p=>m[p.actor_id]=p);return m}
function safeJson(s){try{return typeof s==='string'?JSON.parse(s):s}catch{return{}}}
function chart(id){const el=document.getElementById(id);if(!el||!window.echarts)return null;
  // 2026-05-24: Force-clear the container before re-init. We were seeing stuck
  // y-axis labels (e.g. "9999999") when the same chart id was rendered for
  // different signals — `dispose()` alone wasn't fully clearing canvas state
  // when the DOM node had been replaced by a parentEl.innerHTML reset.
  if(state.charts[id]){try{state.charts[id].dispose()}catch(e){}delete state.charts[id]}
  // Also walk any orphaned ECharts instance hanging on this exact DOM node and
  // dispose it (catches the case where state.charts was lost but echarts still
  // tracks the dom).
  try{const stale=window.echarts.getInstanceByDom(el); if(stale){stale.dispose()}}catch(e){}
  // Wipe the inner HTML for a guaranteed clean canvas slot.
  el.innerHTML='';
  const c=echarts.init(el);state.charts[id]=c;setTimeout(()=>c.resize(),50);return c}

/* ─── Trade journey (Round 12, 2026-05-24) ───
   Async fetch /api/position-journey/{id} and render an ECharts dual-grid
   chart: top grid is price line with horizontal entry/SL/TP1 markers,
   bottom grid is signed P&L%. Both grids share the same x-axis (time).
   Stride is server-controlled (max 1500 points) — no client-side
   downsampling needed.

   Why this exists:
   * Pre-Round-12, the drawer showed Trade State numbers (max profit,
     max drawdown, distance entry/TP1) but no visual of HOW the trade
     evolved — operators had to imagine the price path from numbers.
   * The replay chart at the top of the drawer shows the chart at SIGNAL
     time, not the trade journey afterwards. Two complementary views.
   * position_updates already has minute-resolution data going back to
     opened_at; this just makes it visible.

   Failure modes:
   * No position id (sig with no tracked_position) → caller never
     invokes this; safe.
   * Empty series (position just opened) → chart shows "no journey
     samples yet" and the summary line says so.
   * API error → chart container shows the error; doesn't break drawer. */
async function renderTradeJourney(positionId){
  const chartId=`journey-chart-${positionId}`;
  const summaryId=`journey-summary-${positionId}`;
  const el=document.getElementById(chartId);
  const summaryEl=document.getElementById(summaryId);
  if(!el)return;
  let payload;
  try{payload=await api(`/api/position-journey/${encodeURIComponent(positionId)}`)}
  catch(e){
    if(summaryEl)summaryEl.textContent=`Journey unavailable: ${e?.message||e}`;
    return;
  }
  const series=payload.series||[];
  if(!series.length){
    if(summaryEl)summaryEl.textContent='No journey samples yet — position too fresh.';
    return;
  }
  const c=chart(chartId);
  if(!c)return;
  const lvl=payload.levels||{};
  const xData=series.map(p=>p.t);
  const priceData=series.map(p=>p.price);
  const pnlData=series.map(p=>p.pnl_pct);
  // Build markLine list for entry/SL/TP1/TP2 if present.
  const markData=[];
  const addLine=(name,val,color,style='dashed')=>{
    if(val==null||!Number.isFinite(val))return;
    markData.push({yAxis:val,name,lineStyle:{color,width:1.4,type:style},label:{formatter:`${name} ${fmt(val,4)}`,position:'insideEndTop',color}});
  };
  addLine('Entry',lvl.entry,'#fc72ff','solid');
  addLine('SL',lvl.stop,'#ff5f72','dashed');
  addLine('TP1',lvl.tp1,'#35d07f','dashed');
  addLine('TP2',lvl.tp2,'#22b07a','dashed');
  // Build event-line markers (signal time, opened, closed) on the price line.
  const eventLines=[];
  if(payload.signal_timestamp)eventLines.push({xAxis:payload.signal_timestamp,name:'signal',lineStyle:{color:'#7a5cff',width:1,type:'dotted'},label:{formatter:'signal',color:'#a89cff'}});
  if(payload.opened_at)eventLines.push({xAxis:payload.opened_at,name:'opened',lineStyle:{color:'#35d07f',width:1,type:'dotted'},label:{formatter:'opened',color:'#7eebb6'}});
  if(payload.closed_at)eventLines.push({xAxis:payload.closed_at,name:'closed',lineStyle:{color:'#ff5f72',width:1,type:'dotted'},label:{formatter:'closed',color:'#ff97a4'}});
  c.setOption({
    backgroundColor:'transparent',
    animation:false,
    tooltip:{trigger:'axis',axisPointer:{type:'cross'},
      formatter:(arr)=>{
        if(!arr||!arr.length)return '';
        const t=arr[0].axisValue||'';
        const lines=[`<div style="margin-bottom:4px"><b>${t.replace('T',' ').slice(0,19)}</b></div>`];
        arr.forEach(a=>{const v=Array.isArray(a.value)?a.value[1]:a.value;lines.push(`${a.marker} ${a.seriesName}: <b>${fmt(v,a.seriesName==='Price'?6:2)}${a.seriesName==='P&L %'?'%':''}</b>`)});
        return lines.join('<br/>');
      }
    },
    legend:{data:['Price','P&L %'],top:2,textStyle:{color:'#cdcde6'}},
    grid:[{left:60,right:30,top:34,height:'58%'},{left:60,right:30,top:'72%',height:'22%'}],
    axisPointer:{link:[{xAxisIndex:'all'}]},
    xAxis:[
      {type:'time',gridIndex:0,axisLabel:{color:'#8f8f9b',fontSize:10},splitLine:{lineStyle:{color:'#1a1a24'}}},
      {type:'time',gridIndex:1,axisLabel:{color:'#8f8f9b',fontSize:10},splitLine:{lineStyle:{color:'#1a1a24'}}}
    ],
    yAxis:[
      {type:'value',gridIndex:0,scale:true,axisLabel:{color:'#8f8f9b',fontSize:10},splitLine:{lineStyle:{color:'#1a1a24'}}},
      {type:'value',gridIndex:1,axisLabel:{color:'#8f8f9b',fontSize:10,formatter:'{value}%'},splitLine:{lineStyle:{color:'#1a1a24'}}}
    ],
    series:[
      {name:'Price',type:'line',xAxisIndex:0,yAxisIndex:0,showSymbol:false,smooth:0.15,
        data:xData.map((t,i)=>[t,priceData[i]]),
        lineStyle:{color:'#7a5cff',width:1.6},
        areaStyle:{color:'rgba(122,92,255,0.08)'},
        markLine:{symbol:'none',silent:true,data:[...markData,...eventLines]}
      },
      {name:'P&L %',type:'line',xAxisIndex:1,yAxisIndex:1,showSymbol:false,smooth:0.15,
        data:xData.map((t,i)=>[t,pnlData[i]]),
        lineStyle:{color:'#35d07f',width:1.4},
        areaStyle:{color:'rgba(53,208,127,0.08)'},
        markLine:{symbol:'none',silent:true,data:[{yAxis:0,lineStyle:{color:'#3d3d4d',type:'dashed',width:1}}]}
      }
    ]
  });
  if(summaryEl){
    const last=series[series.length-1];
    const minPnl=series.reduce((m,p)=>p.pnl_pct!=null&&p.pnl_pct<m?p.pnl_pct:m,Infinity);
    const maxPnl=series.reduce((m,p)=>p.pnl_pct!=null&&p.pnl_pct>m?p.pnl_pct:m,-Infinity);
    const ageMin=last?.minutes||0;
    const ageStr=ageMin>=60?`${(ageMin/60).toFixed(1)}h`:`${ageMin}m`;
    summaryEl.textContent=`${payload.samples} samples (stride ${payload.stride}, ${payload.samples_total} raw) · age ${ageStr} · max drawdown ${fmt(minPnl===Infinity?0:minPnl,2)}% · max profit ${fmt(maxPnl===-Infinity?0:maxPnl,2)}%`;
  }
}
function wireRows(){$$('[data-call]').forEach(tr=>{if(tr._wired)return;tr._wired=true;tr.onclick=()=>openCall(tr.dataset.call,tr.dataset.news)})}
function wire(){
  $$('.nav-link').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));
  $$('[data-open]').forEach(b=>b.onclick=()=>switchTab(b.dataset.open));
  $('#company-filter').onchange=e=>{state.company=e.target.value;load()};
  $('#refresh-btn').onclick=()=>load();
  $('#drawer-close').onclick=closeDrawer; $('#drawer-backdrop').onclick=closeDrawer;
  // Round 11 (2026-05-24): radar-filter and radar-age were wired to renderFloor()
  // — typing in the radar filter did nothing. Split so each tab's filters call
  // its own renderer.
  ['floor-signal-filter','floor-position-filter'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=()=>renderFloor()});
  ['radar-filter','radar-age'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderRadarPage()});
  ['signals-filter','signals-status'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderSignalsPage()});
  // Round 11: Positions tab Live/Historic sub-tabs + filters.
  $$('#tab-positions .comp-dtab').forEach(b=>b.onclick=()=>{
    _positionsState.sub=b.dataset.ptab;
    renderPositionsPage();
  });
  ['positions-live-filter','positions-live-status'].forEach(id=>{
    const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderPositionsLive();
  });
  ['positions-historic-filter','positions-historic-window','positions-historic-status','positions-historic-outcome'].forEach(id=>{
    const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderPositionsHistoric(false);
  });
  const moreBtn=$('#positions-historic-more');
  if(moreBtn) moreBtn.onclick=()=>renderPositionsHistoric(true);
  ['traders-filter','traders-sort'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderTradersPage()});
  ['news-filter','news-media'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderNewsPage()});
  ['telegram-filter','telegram-media','telegram-channel'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderTelegramPage()});
}
window.addEventListener('DOMContentLoaded',()=>{loadDrawerWidth();wire();attachDrawerResizer();load();state.timer=setInterval(()=>{const ts=new Date();$('#updated-at').textContent=`${ts.toLocaleTimeString()}`; if(['floor','radar','signals','positions','competition'].includes(state.tab)) load()},30000)});
})();
