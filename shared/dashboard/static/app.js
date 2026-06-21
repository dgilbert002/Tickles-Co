/* Tickles v5 — competition inline expand, chart tabs in Discord drawer, trader usernames, compact rows. */
(function(){const t=localStorage.getItem('tickles.theme');if(t==='light')document.body.classList.add('light');updateThemeBtn()})();
function toggleTheme(){document.body.classList.toggle('light');localStorage.setItem('tickles.theme',document.body.classList.contains('light')?'light':'dark');updateThemeBtn()}
function updateThemeBtn(){const b=document.getElementById('theme-btn');if(b)b.textContent=document.body.classList.contains('light')?'☀️ Light':'🌙 Dark'}
window.toggleTheme=toggleTheme;window.updateThemeBtn=updateThemeBtn;
document.addEventListener('DOMContentLoaded',function(){var b=document.getElementById('theme-btn');if(b){b.addEventListener('click',function(){var bd=document.body;bd.classList.toggle('light');var m=bd.classList.contains('light')?'light':'dark';localStorage.setItem('tickles.theme',m);b.textContent=m==='light'?'☀️ Light':'🌙 Dark'})}});
const state={tab:'floor',company:'all',snap:null,competitions:null,learning:null,news:null,agentPerf:null,sort:{},compSort:null,charts:{},timer:null,expandedAgent:null,timeSpacing:localStorage.getItem('tickles.replay.spacing')||'contiguous'};
const $=s=>document.querySelector(s), $$=s=>Array.from(document.querySelectorAll(s));
const esc=v=>v==null?'':String(v).replace(/[&<>\"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[m]));
const Z=v=>{const x=Number(v);return Number.isFinite(x)?x:0};
const fmt=(v,d=2)=>{const x=Number(v);return Number.isFinite(x)?x.toFixed(d):'—'};
// Level formatter: prefers the first POSITIVE value (a 0 from a NUMERIC column
// means "no level recorded" — ?? would wrongly keep it, || drops it but also
// drops the JSONB fallback). Renders '—' when neither is a positive price.
const lvl=(primary,fallback,d=4)=>{const a=Number(primary);if(Number.isFinite(a)&&a>0)return a.toFixed(d);const b=Number(fallback);return Number.isFinite(b)&&b>0?b.toFixed(d):'—'};
const usd=v=>`${Z(v)>=0?'+':'-'}$${Math.abs(Z(v)).toFixed(2)}`;
const pct=v=>`${Z(v)>=0?'+':''}${fmt(v,2)}%`;
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
async function load(){try{const needsSnap=['floor','radar','signals','positions','traders','ops'].includes(state.tab)||!state.snap;if(needsSnap)state.snap=await api('/api/snapshot');if(state.tab==='competition'||state.tab==='floor')state.competitions=await api('/api/competitions');if(state.tab==='learning'||state.tab==='floor'||state.tab==='traders')state.learning=await fetchLearning();if(state.tab==='news')state.news=await api('/api/news/feed?window=30d&source=discord&limit=40');else if(state.tab==='floor')state.news=await api('/api/news/feed?window=30d&limit=40');
      if(state.tab==='telegram')state.telegram=await api('/api/news/feed?window=30d&limit=40&source=telegram');if(state.tab==='traders'||state.tab==='floor')state.agentPerf=await api('/api/agent-performance');if(state.tab==='settings'){state.settings=null;await fetchSettings();}if(state.tab==='strategies')state.strategies=await api('/api/strategies');render();$('#updated-at').textContent='updated now';$('#status-text').textContent='live'}catch(e){console.error(e);$('#status-text').textContent='API issue'}}
async function fetchLearning(){const [skill,feed,brain,traders]=await Promise.all([api('/api/learning/skill-summary?window=1m'),api('/api/learning/memory-feed?window=1m'),api('/api/learning/agent-brain?window=1m'),api('/api/traders-intel')]);return{skill,feed,brain,traders}}
function switchTab(tab){state.tab=tab;state.expandedAgent=null;state._agentCache=null;$$('.nav-link').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));$$('.tab').forEach(t=>t.classList.toggle('active',t.id===`tab-${tab}`));const names={floor:['TRADING COMPANY','Trading Floor'],unified:['LIVE ENTRY WATCH','Signals & Radar'],competition:['CONTEST MODE','Competition'],positions:['RISK MONITOR','Positions'],traders:['DISCORD ALPHA','Trader Intel'],news:['SOCIAL TAPE','Discord Feed'],telegram:['TELEGRAM','Telegram Feed'],learning:['MEMORY + SKILL','AI Learning'],strategies:['EDGE LIBRARY','Strategies'],ops:['RUN COST','Ops & Cost'],settings:['CONFIGURATION','Settings'],exchanges:['EXCHANGE MANAGEMENT','Exchange Accounts'],paperdemo:['PAPER · DEMO · LIVE','Paper vs Demo vs Live'],discord:['DISCORD RIVER','Discord Feed v2']};if(!names[tab])return;$('#eyebrow').textContent=names[tab][0];$('#page-title').textContent=names[tab][1];load()}
function render(){renderStats();if(state.tab==='floor')renderFloor();if(state.tab==='unified'||state.tab==='radar'||state.tab==='signals')renderUnifiedPage();if(state.tab==='competition')renderCompetition();if(state.tab==='positions')renderPositionsPage();if(state.tab==='traders')renderTradersPage();if(state.tab==='news')renderNewsPage();if(state.tab==='telegram')renderTelegramPage();if(state.tab==='learning')renderLearningPage();if(state.tab==='ops')renderOpsPage();if(state.tab==='settings')renderSettingsPage();if(state.tab==='exchanges'){renderExchangeAccounts();renderMirrorConfig()}if(state.tab==='paperdemo')renderPaperDemo();if(state.tab==='strategies')renderStrategiesPage();if(state.tab==='discord')renderDiscordPage()}function renderDiscordPage(){if(window.DiscordFeed){DiscordFeed.boot()}else{setTimeout(function(){if(window.DiscordFeed)DiscordFeed.boot()},500)}}
function renderStats(){const s=state.snap||{};const pnl=Z(s.open_positions_unrealized_pnl);$('#stat-pnl').textContent=usd(pnl);$('#stat-pnl').className=pnl>=0?'success':'danger';$('#stat-open-count').textContent=`${s.open_positions_count||0} open positions`;$('#stat-signals').textContent=s.signals_today_count||0;$('#stat-ingest').textContent=`${s.ingest_depth||0} ingest depth`;$('#stat-top').textContent=s.top_actor_name||'—';$('#stat-edge').textContent=s.top_actor_score?`$${fmt(s.top_actor_score,2)} equity`:'—';$('#stat-cost').textContent=`$${fmt(s.api_cost_today_usd,4)}`;$('#stat-budget').textContent=`$${fmt(s.budget_remaining_usd||s.budget_limit_usd||100,0)} remaining`}

/* ─── row helpers ─── */
function rowMain(sym,sub){return`<div class="main-cell"><div class="token-dot">${esc(initials(sym))}</div><div><div class="primary">${esc(sym)}</div><div class="secondary">${esc(sub||'')}</div></div></div>`}

/* ─── competition — INLINE expand, no drawer ─── */
const COMP_SORT_LS='tickles.comp.sort';
const COMP_COLS=[
  {key:'rank',label:'#',num:true,sortable:false},
  {key:'agent',label:'Agent',sortable:true},
  {key:'strategy',label:'Strategy',sortable:true},
  {key:'balance',label:'Balance',num:true,sortable:true},
  {key:'liveEq',label:'Live Eq',num:true,sortable:true},
  {key:'pnl',label:'P&L',num:true,sortable:true},
  {key:'return',label:'Return',num:true,sortable:true},
  {key:'winRate',label:'Win%',num:true,sortable:true},
  {key:'wl',label:'W/L',num:true,sortable:true},
  {key:'open',label:'Opn',num:true,sortable:true},
];
function loadCompSort(){try{const raw=localStorage.getItem(COMP_SORT_LS);if(!raw)return{key:'liveEq',dir:-1};const o=JSON.parse(raw);return{key:o.key||'liveEq',dir:o.dir===1?1:-1}}catch(e){return{key:'liveEq',dir:-1}}}
function saveCompSort(sort){try{localStorage.setItem(COMP_SORT_LS,JSON.stringify(sort))}catch(e){}}
function compMetrics(p){
  const eq=Z(p.scores?.equity),unreal=Z(p.scores?.unrealized_pnl_usd),liveEq=eq+unreal;
  const starting=Z(p.scores?.starting_balance_usd)||1000;
  const wins=Z(p.scores?.winning_trades??p.scores?.wins);
  const losses=Z(p.scores?.losing_trades??p.scores?.losses);
  const closed=wins+losses;
  return{agent:p.agent_id||'',strategy:p.strategy_ref||'',eq,liveEq,unreal,
    rpct:Z(p.scores?.return_pct),liveReturn:starting>0?((liveEq/starting)-1)*100:0,
    pnl:Z(p.scores?.total_realized_pnl_usd),winRate:Z(p.scores?.win_rate),
    wins,losses,totalTrades:Z(p.scores?.total_trades)||closed,open:Z(p.scores?.open_positions),meta:p};
}
function compSortValue(m,key){
  switch(key){
    case 'agent':return m.agent.toLowerCase();
    case 'strategy':return m.strategy.toLowerCase();
    case 'balance':return m.eq;case 'liveEq':return m.liveEq;case 'pnl':return m.pnl;
    case 'return':return m.rpct;case 'winRate':return m.winRate;
    case 'wl':return m.wins/(m.wins+m.losses||1);case 'open':return m.open;
    default:return m.liveEq;
  }
}
function compWlCell(w,l){if(!w&&!l)return'<span class="secondary">—</span>';return`<span class="success">${w}W</span><span class="secondary"> · </span><span class="danger">${l}L</span>`}
function sortCompParticipants(participants,sort){
  const key=sort?.key||'liveEq',dir=sort?.dir??-1;
  return(participants||[]).slice().sort((a,b)=>{
    const ma=compMetrics(a),mb=compMetrics(b),va=compSortValue(ma,key),vb=compSortValue(mb,key);
    if(typeof va==='string')return dir*va.localeCompare(vb);
    return dir*(va-vb);
  });
}
function wireCompSort(){
  const sort=state.compSort||loadCompSort();state.compSort=sort;
  $$('#competition-body .comp-table th.sortable').forEach(th=>{
    th.classList.remove('sort-asc','sort-desc');
    if(th.dataset.key===sort.key)th.classList.add(sort.dir===1?'sort-asc':'sort-desc');
    th.onclick=e=>{e.stopPropagation();const key=th.dataset.key;let dir=-1;
      const cur=state.compSort||loadCompSort();
      if(cur.key===key)dir=cur.dir===-1?1:-1;
      const next={key,dir};state.compSort=next;saveCompSort(next);renderCompetition()};
  });
}
async function renderCompetition(){
  const c=state.competitions?.competitions?.[0];
  if(!c){$('#competition-body').innerHTML='<div class="empty">No active competition</div>';return}
  const sort=state.compSort||loadCompSort();state.compSort=sort;
  const sorted=sortCompParticipants(c.participants||[],sort);
  const head=COMP_COLS.map(col=>{
    const cls=[col.num?'num':'',col.sortable?'sortable':''].filter(Boolean).join(' ');
    const style=col.key==='balance'?' style="min-width:70px"':col.key==='liveEq'?' style="min-width:80px"':col.key==='pnl'?' style="min-width:90px"':col.key==='return'?' style="min-width:80px"':col.key==='winRate'?' style="min-width:55px"':'';
    return`<th class="${cls}"${style} data-key="${col.sortable?col.key:''}">${esc(col.label)}</th>`;
  }).join('');
  const rows=sorted.map((p,i)=>{
    const m=compMetrics(p);
    return `<tr class="comp-row" data-agent="${esc(p.agent_id)}" id="comp-tr-${esc(p.agent_id)}">
      <td class="num rank" data-col="rank">#${i+1}</td>
      <td data-col="agent" title="${esc(p.metadata?.description||'')}">${rowMain(p.agent_id,(p.metadata?.description||p.strategy_ref||'').split('.')[0])}</td>
      <td data-col="strategy">${esc(p.strategy_ref||'—')}</td>
      <td class="num mono" data-col="balance">$${fmt(m.eq,2)}</td>
      <td class="num mono" data-col="liveEq"><strong>$${fmt(m.liveEq,2)}</strong></td>
      <td class="num" data-col="pnl">${usd(m.pnl)}<br><span class="secondary small">unreal ${usd(m.unreal)}</span></td>
      <td class="num ${m.rpct>=0?'success':'danger'}" data-col="return">${pct(m.rpct)}<br><span class="secondary small">${pct(m.liveReturn)} live</span></td>
      <td class="num" data-col="winRate">${fmt(m.winRate*100,1)}%</td>
      <td class="num" data-col="wl">${compWlCell(m.wins,m.losses)}</td>
      <td class="num" data-col="open">${m.open}</td>
    </tr>`;
  });
  $('#competition-body').innerHTML=`<div class="comp-scroll"><table class="data-table comp-table"><thead><tr>${head}</tr></thead><tbody>${rows.join('')}</tbody></table><div id="comp-expand-zone"></div></div>`;
  wireCompSort();
  $$('#competition-body .comp-row').forEach(tr=>tr.onclick=()=>toggleAgentExpand(tr.dataset.agent));
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
  let posTab=state._lastAgentSubTab||'live';

  function buildLive(){
    if(!open.length)return'<tr><td colspan="11" class="empty">No open positions — waiting for new signals</td></tr>';
    return open.map((p,i)=>{
      const upnl=Z(p.unrealized_pnl), upct=Z(p.unrealized_pnl_pct);
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
      const pnl=Z(p.pnl); const dir=(p.direction||'long').toLowerCase();
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

  const eq=Z(a.equity_usd), unreal=Z(a.unrealized_pnl_usd), liveEq=eq+unreal;
  const starting=Z(a.starting_balance_usd)||1000;
  const liveReturn=((liveEq/starting)-1)*100;
  const sc=safeJson(a.scores)||{};
  const wins=Z(sc.winning_trades??sc.wins);
  const losses=Z(sc.losing_trades??sc.losses);

  const html=`<div class="comp-detail">
    <div class="comp-detail-tabs">
      <button class="comp-dtab active" data-ctab="live">Live · ${open.length}</button>
      <button class="comp-dtab" data-ctab="history">History · ${hist.length}</button>
    </div>
    <div class="comp-stats">
      <div><label>Balance</label><strong>$${fmt(eq,2)}</strong></div>
      <div><label>Live Equity</label><strong>$${fmt(liveEq,2)}</strong></div>
      <div><label>Realized P&L</label><strong class="${Z(a.realized_pnl_usd)>=0?'success':'danger'}">${usd(a.realized_pnl_usd)}</strong></div>
      <div><label>Unrealized</label><strong class="${unreal>=0?'success':'danger'}">${usd(unreal)}</strong></div>
      <div><label>Return</label><strong>${fmt(a.return_pct,1)}%</strong></div>
      <div><label>Live Return</label><strong class="${liveReturn>=0?'success':'danger'}">${fmt(liveReturn,1)}%</strong></div>
      <div><label>Win Rate</label><strong>${fmt(Z(a.win_rate)*100,1)}%</strong></div>
      <div><label>W / L</label><strong>${wins} / ${losses}</strong></div>
      <div><label>Open</label><strong>${a.open_positions||open.length||0}</strong></div>
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
  // Restore active sub-tab
  const activeTab=posTab;
  $$('#comp-expand-zone .comp-dtab').forEach(b=>{
    b.classList.toggle('active',b.dataset.ctab===activeTab);
  });
  $('#comp-live-panel').classList.toggle('hidden',activeTab!=='live');
  $('#comp-history-panel').classList.toggle('hidden',activeTab!=='history');
  // tab switching
  $$('#comp-expand-zone .comp-dtab').forEach(b=>b.onclick=()=>{
    $$('#comp-expand-zone .comp-dtab').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    const tab=b.dataset.ctab;
    state._lastAgentSubTab=tab;
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

/* ─── Discord feed — paginated ─── */
const _newsState={offset:0,allRows:[],hasMore:false};
async function renderNewsPage(reset){
  if(reset){_newsState.offset=0;_newsState.allRows=[];_newsState.hasMore=false}
  if(!_newsState.allRows.length){
    try{
      const d=await api('/api/news/feed?window=30d&source=discord&limit=40&offset='+_newsState.offset);
      _newsState.allRows=(d.rows||[]).filter(r=>r.source==='discord');
      _newsState.hasMore=_newsState.allRows.length>=40;
      _newsState.offset+=40;
    }catch(e){console.error(e);return}
  }
  let rows=_newsState.allRows;
  rows=filterRows(rows,$('#news-filter')?.value,['author','content','headline','channel_name']);
  if($('#news-media')?.value==='media')rows=rows.filter(r=>r.has_media);
  $('#news-list').innerHTML=rows.map(newsMsg).join('')||'<div class="empty">No messages</div>';
  if(_newsState.hasMore&&!$('#news-filter')?.value&&!$('#news-media')?.value){
    $('#news-list').innerHTML+='<div class="load-more"><button class="btn small" onclick="loadMoreNews()">Load more…</button></div>';
  }
  $$('[data-newsrow]').forEach(el=>el.onclick=()=>openDiscordDrawer(el.dataset.newsrow,el.dataset.newssrc));
}
async function loadMoreNews(){
  try{
    const d=await api('/api/news/feed?window=30d&source=discord&limit=40&offset='+_newsState.offset);
    const more=(d.rows||[]).filter(r=>r.source==='discord');
    _newsState.allRows=_newsState.allRows.concat(more);
    _newsState.hasMore=more.length>=40;
    _newsState.offset+=40;
    renderNewsPage();
  }catch(e){console.error(e)}
}

/* ─── Unified Signals & Radar — single view with list/card toggle ─── */
let _unifiedView=localStorage.getItem('tickles.unified.view')||'list';
async function renderUnifiedPage(){
  const view=_unifiedView;
  $$('#unified-toggle .view-btn').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
  $('#unified-list-panel').classList.toggle('hidden',view!=='list');
  $('#unified-card-panel').classList.toggle('hidden',view!=='card');
  try{
    const d=await api('/api/unified-signals?limit=120&status=pending');
    let rows=d.signals||[];
    const statusEl=$('#unified-status');const sf=statusEl?.value||'';
    // Always exclude unclear + no-entry signals (commentary, not trade setups)
    rows=rows.filter(r=>r.direction&&r.direction!=='unclear'&&r.entry_price>0);
    // Sanity: exclude mispriced entries (LLM read wrong decimal, e.g. $0.74 vs $656)
    // 2026-05-27: tightened from 0.01→0.1 (100x→10x) to catch stale signals like
    // SOLUSDT entry=0.8754 vs live=83.73 (ratio 0.0105, still passed old filter)
    rows=rows.filter(r=>{
      if(!r.current_price||r.current_price<=0)return true; // no live price, can't check
      const ratio=r.entry_price/r.current_price;
      return ratio>0.1&&ratio<10; // within 1 order of magnitude
    });
    // Phase 3B (2026-05-29): Signals & Radar is now APPROACHING-ONLY. Closed /
    // cancelled / expired / invalidated history lives in the Positions tab. The
    // status dropdown offers "Approaching entry" (default) and "All active".
    const DEAD=['closed','cancelled','expired','invalidated'];
    if(sf==='all'){
      // All still-active signals (open + pending + signal/pre-entry).
      rows=rows.filter(r=>!DEAD.includes(r.position_status));
    }else{
      // Approaching = pre-entry (pending/signal) + just-filled flash. No open
      // positions that filled long ago, no history.
      rows=rows.filter(r=>!DEAD.includes(r.position_status)&&(isPreEntry(r)||isJustFilled(r)));
    }
    if(view==='card'){
      // Phase 3 (2026-05-29): the CARD view is the Entry Radar — it shows
      // pre-entry setups (pending / signal, waiting to trigger) plus anything
      // that JUST FILLED in the last 30 min. Long-ago open positions live in
      // the Positions tab, not the radar. The LIST view (renderUnifiedTable)
      // remains the full browse surface and is unchanged.
      const radarRows=rows.filter(s=>isPreEntry(s)||isJustFilled(s))
        .sort((a,b)=>signalDistance(a)-signalDistance(b));
      $('#unified-card-panel').innerHTML=radarRows.map(radarCard).join('')||radarEmptyState(d.radar_meta);
      // Phase 3B (2026-05-29): the card branch never wired clicks, so radar
      // cards were dead. wireRows() binds [data-call] -> openCall (the unified
      // drawer). drawMiniRadar fills any card that carries candle data.
      wireRows();
      radarRows.forEach((s,i)=>{const cs=s.mini_candles||s._candles;if(cs&&cs.length)drawMiniRadar(`mini-radar-${i}`,cs,s)});
    }else{
      // B (2026-05-29): in "Approaching entry" mode with nothing armed, show the
      // honest empty-state instead of a blank table (matches the card view).
      if(sf!=='all'&&rows.length===0){
        const el=$('#unified-list-table');if(el)el.innerHTML=radarEmptyState(d.radar_meta);
      }else{
        renderUnifiedTable(rows);
      }
    }
  }catch(e){console.warn('unified signals failed',e)}
}
// Phase 3 (2026-05-29): radar membership helpers.
// Pre-entry = waiting to trigger (pending/signal/no-position-yet).
// B (2026-05-29): "approaching" = ARMED only. An armed setup is a real pending
// tracked_position (confidence>=0.4, demo order placed) that WILL fire when
// price reaches entry. Read-but-not-armed interpretations (position_status
// 'signal'/'' — low conviction, deduped, no levels) are NOT approaching; they
// live under "All active signals" so the radar stops looking falsely full.
function isPreEntry(s){return s.armed===true||String(s.position_status||'').toLowerCase()==='pending';}
// Just-filled = activated (pending->open) within the last 30 min.
function isJustFilled(s){if(String(s.position_status||'').toLowerCase()!=='open')return false;const t=s.position_activated_at?Date.parse(s.position_activated_at):NaN;return Number.isFinite(t)&&(Date.now()-t)<30*60*1000;}
// Honest empty-state: explains WHY nothing is waiting instead of "No signals".
function radarEmptyState(m){
  m=m||{};
  const lsm=Number(m.last_signal_min);
  let fresh='';
  if(Number.isFinite(lsm)&&lsm>=0){
    const txt=lsm<90?`${lsm}m ago`:(lsm<1440?`${Math.round(lsm/60)}h ago`:`${Math.round(lsm/1440)}d ago`);
    fresh=`<div class="secondary" style="margin-top:6px">Interpreter last read a setup <b>${txt}</b> · <b>${m.signals_24h||0}</b> read in the last 24h.</div>`;
  }
  return `<div class="empty radar-empty"><strong>No armed setups approaching entry right now.</strong>`+
    `<div class="secondary" style="margin-top:6px">Only high-conviction setups (a pending order is placed) appear here. Read-but-not-armed signals are under <b>All active signals</b>.</div>`+
    fresh+
    `<div class="secondary" style="margin-top:6px">Last 24h: <b>${m.filled_24h||0}</b> reached a trade · <b>${m.cancelled_24h||0}</b> filtered out (${m.no_setup_24h||0} no-setup AI charts, ${m.dupes_24h||0} duplicates, ${m.unsupported_24h||0} unsupported symbols).</div>`+
    `</div>`;
}
function renderUnifiedTable(rows){
  let r=rows;const f=$('#unified-filter')?.value;if(f)r=filterRows(r,f,['symbol','trader_display_name','trader_handle_normalized','position_status','headline']);
  table('#unified-list-table',[
    {key:'symbol',label:'Signal',sortable:true},{key:'side',label:'Side',sortable:true},{key:'entry',label:'Entry',num:1,sortable:true},{key:'sl',label:'SL',num:1,sortable:true},{key:'tp',label:'TP1',num:1,sortable:true},
    {label:'Live',num:1},{label:'Δ entry',num:1},{label:'Win',num:1},{label:'Status'},{label:'Source'}
  ],[sigRows(r)]);wireRows()
}
function setUnifiedView(v){_unifiedView=v;localStorage.setItem('tickles.unified.view',v);renderUnifiedPage()}
/* ─── Landing pages ─── */
function renderSignalsPage(){renderUnifiedPage()}
// Round 11 (2026-05-24): Positions tab now has Live + Historic sub-tabs.
// Live  = open + partial_exit (+ broker fills) from /api/positions/live.
// Historic = closed + expired + cancelled + invalidated, paginated, from
// /api/positions/historic with keyset cursor + 30d default window.
// Each sub-tab has its own column set + filter UI. The drawer click path is
// unchanged: row → openCall(signal_interpretation_id, news_item_id).
const _positionsState={
  sub: 'live',
  view: (()=>{try{return localStorage.getItem('tickles.live.view')||'agent'}catch(e){return 'agent'}})(),
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
  rows=filterRows(rows,$('#positions-live-filter')?.value,['instrument_symbol','actor_display','actor_handle','status','signal_source','origin']);
  const st=$('#positions-live-status')?.value;
  if(st)rows=rows.filter(r=>r.status===st);
  const view=_positionsState.view||'agent';
  $$('#positions-live-view .view-btn').forEach(b=>b.classList.toggle('active',b.dataset.pview===view));
  if(view==='grouped'){
    renderLiveGrouped(rows);
  }else{
    const cols=[
      {label:'Position'},{label:'Side'},{label:'Status'},{label:'Origin'},
      {label:'Entry',num:1},{label:'Now',num:1},
      {label:'P&L',num:1},{label:'P&L %',num:1},
      {label:'Dist SL',num:1},{label:'Dist TP1',num:1},
      {label:'Age',num:1},{label:'Source'},
    ];
    table('#positions-live-table',cols,[livePosRows(rows)]);
  }
  wireRows();
  flashLivePnl();
}
function setLiveView(v){_positionsState.view=v;try{localStorage.setItem('tickles.live.view',v)}catch(e){}renderPositionsLive()}
// Phase 4 (2026-05-29): grouped view — one row per (symbol, direction) showing
// how many agents hold it, aggregate P&L/notional, and the list of holders.
function renderLiveGrouped(rows){
  const groups={};
  rows.forEach(p=>{const k=(dispSymbol(p.instrument_symbol)||'')+'|'+(p.direction||'');(groups[k]=groups[k]||[]).push(p)});
  const cols=[
    {label:'Position'},{label:'Side'},{label:'Holders',num:1},
    {label:'Avg entry',num:1},{label:'Now',num:1},
    {label:'Total P&L',num:1},{label:'Notional',num:1},{label:'Who'},
  ];
  const body=Object.values(groups).sort((a,b)=>b.length-a.length).map(ps=>{
    const sym=ps[0].instrument_symbol, dir=ps[0].direction, n=ps.length;
    const avgEnt=ps.reduce((s,p)=>s+Z(p.entry_price),0)/n;
    const cur=Z((ps.find(p=>Z(p.current_price))||{}).current_price);
    const totPnl=ps.reduce((s,p)=>s+Z(p.unrealized_pnl_usd??p.pnl_usd),0);
    const totNot=ps.reduce((s,p)=>s+Z(p.notional_usd),0);
    const who=ps.map(p=>esc(p.origin||p.actor_display||traderName(p))).join(', ');
    const callId=(ps.find(p=>p.signal_interpretation_id)||{}).signal_interpretation_id||'';
    return `<tr data-call="${esc(callId)}"><td>${rowMain(dispSymbol(sym),n+' holder'+(n>1?'s':''))}</td>`
      +`<td>${pill(dir,dir)}</td><td class="num">${n}</td>`
      +`<td class="num mono">${fmt(avgEnt,6)}</td><td class="num mono">${fmt(cur,6)}</td>`
      +`<td class="num ${totPnl>=0?'success':'danger'}">${usd(totPnl)}</td>`
      +`<td class="num">$${fmt(totNot,0)}</td><td class="secondary small">${who}</td></tr>`;
  }).join('');
  table('#positions-live-table',cols,[body]);
}
// Competition-style flash: highlight P&L cells whose value changed since the
// last 5s tick (green = improved, red = worsened).
let _livePnlPrev={};
function flashLivePnl(){
  const next={};
  $$('#positions-live-table tr[data-posid]').forEach(tr=>{
    const id=tr.dataset.posid; const v=Number(tr.dataset.pnl);
    if(!id||!Number.isFinite(v))return;
    next[id]=v;
    const prev=_livePnlPrev[id];
    if(prev!=null&&v!==prev){
      const cell=tr.querySelector('.pnl-cell');
      if(cell){const cls=v>prev?'flash-up':'flash-down';cell.classList.remove('flash-up','flash-down');void cell.offsetWidth;cell.classList.add(cls)}
    }
  });
  _livePnlPrev=next;
}
function livePosRows(rows){return rows.map(p=>{
  const pnl=Z(p.unrealized_pnl_usd??p.pnl_usd);
  const pnlPct=Z(p.pnl_pct);
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
  // Phase 4 (2026-05-29): origin label + kind tag (paper agent / broker /
  // signal) so multi-agent rows are distinguishable; data-posid/data-pnl power
  // the flash-on-change pulse.
  const origin=p.origin||p.actor_display||'signal';
  const okind=p.origin_kind||'signal';
  const originPill=`<span class="origin-tag origin-${esc(okind)}" title="${esc(okind)}">${esc(origin)}</span>`;
  const posKey=`${p._source||''}:${p.id||p.signal_interpretation_id||''}`;
  const hasPnl=(p.unrealized_pnl_usd!=null)||(p.pnl_usd!=null);
  return `<tr data-call="${esc(p.signal_interpretation_id||'')}" data-news="${esc(p.news_item_id||'')}" data-posid="${esc(posKey)}" data-pnl="${hasPnl?pnl:''}">`
    +`<td>${rowMain(dispSymbol(p.instrument_symbol),`${traderName(p)} · ${rel(p.signal_timestamp||p.opened_at)}`)}</td>`
    +`<td>${pill(p.direction,p.direction)}</td>`
    +`<td>${pill(p.status,p.status)}</td>`
    +`<td>${originPill}</td>`
    +`<td class="num mono">${fmt(p.entry_price,6)}</td>`
    +`<td class="num mono ${stale?'secondary':''}">${fmt(p.current_price,6)}${stale?' <span title="stale price >5min">·</span>':''}</td>`
    +`<td class="num pnl-cell ${pnl>=0?'success':'danger'}">${hasPnl?usd(pnl):'—'}</td>`
    +`<td class="num ${pnlPct>=0?'success':'danger'}">${pnlPct?pct(pnlPct*100):'—'}</td>`
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
  const pnl=Z(p.realized_pnl_usd_final??p.realized_pnl_usd??p.pnl_usd);
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
function renderLearningPage(){const l=state.learning||{feed:{rows:[]},skill:{rows:[]},brain:{rows:[]}};$('#learn-total').textContent=l.feed.rows.length;$('#learn-actors').textContent=l.skill.rows.length;$('#learn-brains').textContent=l.brain.rows.length;$('#learn-focus').textContent=(l.feed.rows[0]?.raw&&safeJson(l.feed.rows[0].raw)?.instrument_symbol)||'execution';const traderMap={};(l.traders?.traders||[]).forEach(t=>{traderMap[t.actor_id]=t.display_name||t.actor_id;if(t.handle_normalized)traderMap[t.handle_normalized]=t.display_name||t.handle_normalized});const resolveTrader=actor=>{if(!actor)return'trader';const bare=String(actor).replace(/^(jarvais_trader_|trader_)/i,'');if(traderMap[actor])return traderMap[actor];if(traderMap[bare])return traderMap[bare];return bare};$('#learning-lessons').innerHTML=l.feed.rows.map(r=>{const actorName=resolveTrader(r.actor);const kindLabel=r.source_kind==='postmortem_actor'?'POSTMORTEM':r.source_kind==='postmortem_company'?'COMPANY POSTMORTEM':r.source_kind||'';const raw=r.raw?safeJson(r.raw):{};const sym=raw.instrument_symbol||'';return'<div class="lesson" data-actor="'+esc(r.actor||'')+'" data-lesson-id="'+esc(r.id||'')+'"><div class="lesson-kind">'+esc(kindLabel)+' · <a class="trader-link" onclick="event.stopPropagation();openTrader(\''+esc(r.actor||'')+'\')">'+esc(actorName)+'</a>'+(sym?' · '+esc(dispSymbol(sym)):'')+'</div><div class="lesson-body">'+esc(r.body)+'</div><div class="lesson-foot">'+esc(r.company)+' · '+rel(r.ts)+'</div></div>'}).join('');$$('#learning-lessons .lesson').forEach(el=>el.onclick=()=>{const actor=el.dataset.actor;if(actor)openTrader(actor)});const body=l.skill.rows.map(r=>`<tr><td>${rowMain(r.display_name||r.actor_id,r.actor_id)}</td><td class="num">${r.skill_pct??'—'}%</td><td class="num">${r.trade_count||0}</td></tr>`).join('');$('#learning-models').innerHTML=`<table class="data-table"><thead><tr><th>Actor</th><th class="num">Skill</th><th class="num">Trades</th></tr></thead><tbody>${body||'<tr><td colspan="3" class="empty">No data</td></tr>'}</tbody></table>`}

/* ─── misc renderers ─── */
// Round 11 (2026-05-24): the Floor positions panel previously included
// `r.status==='active'` which is a legacy frontend-only enum that the DB
// CHECK constraint does not allow — dead branch. The canonical "live" set
// is open + partial_exit. Keeping it minimal here; full live/historic split
// lives on the Positions tab in phase 11.3.
const POS_LIVE_STATUSES=new Set(['open','partial_exit']);
async function renderFloor(){
  renderCompetitionMini();renderNewsMini();
  // Placeholders so panels never sit blank while the (slower) signal fetch runs.
  if(!$('#floor-signals').children.length)$('#floor-signals').innerHTML='<div class="empty">Loading…</div>';
  if(!$('#floor-positions').children.length)$('#floor-positions').innerHTML='<div class="empty">Loading…</div>';
  // Run both sections independently/parallel — the slow signals fetch must NOT
  // block the fast positions render.
  renderFloorPositions();
  renderFloorSignals();
}
// ── Signals approaching entry (mirror the radar) ──
// Phase 5 (2026-05-29): only setups APPROACHING entry, closest-first. If none,
// show the last few that HIT and which exchange they routed to.
async function renderFloorSignals(){
  try{
    const d=await api('/api/unified-signals?limit=60');
    const all=(d.signals||[]).filter(r=>r.direction&&r.direction!=='unclear'&&Number(r.entry_price)>0);
    let appr=all.filter(r=>isPreEntry(r)||isJustFilled(r)).sort((a,b)=>signalDistance(a)-signalDistance(b));
    appr=filterRows(appr,$('#floor-signal-filter')?.value,['symbol','instrument_symbol','trader_display_name','headline']);
    if(appr.length){
      table('#floor-signals',[{key:'symbol',label:'Signal',sortable:true},{key:'side',label:'Side',sortable:true},{key:'entry',label:'Entry',num:1,sortable:true},{key:'sl',label:'SL',num:1,sortable:true},{key:'tp',label:'TP1',num:1,sortable:true},{label:'Δ entry',num:1},{label:'Status'},{label:'Call'}],[sigRows(appr,12)]);
    }else{
      const hit=all.filter(r=>String(r.position_status||'').toLowerCase()==='open'&&r.position_activated_at)
        .sort((a,b)=>Date.parse(b.position_activated_at)-Date.parse(a.position_activated_at)).slice(0,3);
      const hitHtml=hit.length?'<div class="secondary" style="margin-top:8px">Last to hit: '+hit.map(h=>`<b>${esc(dispSymbol(h.symbol||h.instrument_symbol))}</b> ${esc(h.direction||'')} → ${esc(h.exchange||h.instrument_exchange||'?')} <span class="secondary">(${rel(h.position_activated_at)})</span>`).join(' · ')+'</div>':'';
      $('#floor-signals').innerHTML=`<div class="empty" style="text-align:left">Nothing approaching entry right now.${hitHtml}</div>`;
    }
    wireRows();
  }catch(e){console.warn('floor signals failed',e)}
}
// ── Live positions = everything in play across ALL agents/exchanges ──
async function renderFloorPositions(){
  const cols=[{label:'Position'},{label:'Side'},{label:'Status'},{label:'Origin'},{label:'Entry',num:1},{label:'Now',num:1},{label:'P&L',num:1},{label:'P&L %',num:1},{label:'Notional',num:1}];
  try{
    const pdata=await api('/api/positions/live?company=all&limit=200');
    let ps=filterRows(pdata.positions||[],$('#floor-position-filter')?.value,['instrument_symbol','actor_display','actor_handle','status','origin']);
    table('#floor-positions',cols,[floorPosRows(ps,30)]);
  }catch(e){
    console.warn('floor positions failed',e);
    const live_positions=((state.snap||{}).positions||[]).filter(r=>POS_LIVE_STATUSES.has(String(r.status||'').toLowerCase()));
    table('#floor-positions',cols,[floorPosRows(live_positions,30)]);
  }
  wireRows();flashLivePnl();
}
function floorPosRows(rows,limit){return rows.slice(0,limit||rows.length).map(p=>{
  const closed=POS_CLOSED_STATUSES.has(String(p.status||'').toLowerCase());
  const pnl=Z(closed?(p.realized_pnl_usd_final??p.realized_pnl_usd??p.pnl_usd):(p.unrealized_pnl_usd??p.pnl_usd));
  const hasPnl=(p.unrealized_pnl_usd!=null)||(p.pnl_usd!=null);
  const origin=p.origin||p.actor_display||'signal';const okind=p.origin_kind||'signal';
  const posKey=`${p._source||''}:${p.id||p.signal_interpretation_id||''}`;
  return `<tr data-call="${esc(p.signal_interpretation_id||'')}" data-news="${esc(p.news_item_id||'')}" data-posid="${esc(posKey)}" data-pnl="${hasPnl?pnl:''}">`
    +`<td>${rowMain(dispSymbol(p.instrument_symbol),`${traderName(p)} · ${rel(p.signal_timestamp||p.opened_at)}`)}</td>`
    +`<td>${pill(p.direction,p.direction)}</td><td>${pill(p.status,p.status)}</td>`
    +`<td><span class="origin-tag origin-${esc(okind)}">${esc(origin)}</span></td>`
    +`<td class="num mono">${fmt(p.entry_price,6)}</td><td class="num mono">${fmt(p.current_price,6)}</td>`
    +`<td class="num pnl-cell ${pnl>=0?'success':'danger'}">${hasPnl?usd(pnl):'—'}</td>`
    +`<td class="num ${Z(p.pnl_pct)>=0?'success':'danger'}">${p.pnl_pct?pct(Z(p.pnl_pct)*100):'—'}</td>`
    +`<td class="num">$${fmt(p.notional_usd,0)}</td></tr>`;
}).join('')}
// Phase 6 (2026-05-29): Competition leaders ↔ Demo accounts toggle.
// The floor "Competition leaders" panel can now flip between the paper
// competition standings and the mapped DEMO accounts ranked by profit, so the
// operator can watch real demo execution alongside the paper contest.
function wireLeadersToggle(){
  document.querySelectorAll('#leaders-toggle button').forEach(function(b){
    b.classList.toggle('active',b.dataset.lboard===(state.leaderBoard||'paper'));
    b.onclick=function(){state.leaderBoard=b.dataset.lboard;
      document.querySelectorAll('#leaders-toggle button').forEach(x=>x.classList.toggle('active',x===b));
      var t=$('#leaders-title'); if(t)t.textContent=(state.leaderBoard==='demo'?'Demo accounts':'Competition leaders');
      renderCompetitionMini();};
  });
}
async function renderLeadersDemo(){
  var box=$('#floor-competition'); if(!box)return;
  try{
    // exchange-accounts carries last synced balance; mirror-config maps agents.
    var ad=await api('/api/exchange-accounts',{skipCompany:true});
    var accts=(ad.accounts||[]).filter(a=>a.accountType==='demo');
    var map={};
    try{var mc=await api('/api/mirror-config',{skipCompany:true});(mc.mappings||[]).forEach(m=>{map[m.exchange+'/'+m.account_name]=m.agent_id;});}catch(e){}
    if(!accts.length){box.innerHTML='<div class="empty">No demo accounts mapped</div>';return;}
    var rows=accts.map(function(a){
      var bal=Z(a.lastBalance);var start=1000;var pnl=bal-start;
      var agent=map[a.exchange+'/'+a.accountName]||'—';
      return {bal:bal,pnl:pnl,agent:agent,name:a.exchange+'/'+a.accountName};
    }).sort((x,y)=>y.bal-x.bal);
    box.innerHTML='<div class="competition-card"><div class="secondary">Demo accounts · '+rows.length+' mapped · ranked by balance</div><div class="leader-mini scroll">'+rows.map(function(r,i){
      return '<div class="leader-row"><div class="rank-badge">#'+(i+1)+'</div><div><div class="primary">'+esc(r.name)+'</div><div class="secondary">agent: '+esc(r.agent)+'</div></div><div class="num '+(r.pnl>=0?'success':'danger')+'" title="balance vs $1000 start">$'+fmt(r.bal,2)+'<div class="secondary small">'+(r.pnl>=0?'+':'')+'$'+fmt(r.pnl,2)+'</div></div></div>';
    }).join('')+'</div></div>';
  }catch(e){box.innerHTML='<div class="empty">Failed to load demo accounts</div>';}
}
function renderCompetitionMini(){
  wireLeadersToggle();
  if(state.leaderBoard==='demo'){renderLeadersDemo();return;}
  const c=state.competitions?.competitions?.[0];if(!c){$('#floor-competition').innerHTML='<div class="empty">No competition</div>';return}
  // Phase 5 (2026-05-29): show ALL participants (scrollable), ranked by LIVE
  // EQUITY (closed balance + unrealized), and surface both closed + equity.
  const ps=(c.participants||[]).slice().sort((a,b)=>{
    const ea=Z(a.scores?.equity)+Z(a.scores?.unrealized_pnl_usd);
    const eb=Z(b.scores?.equity)+Z(b.scores?.unrealized_pnl_usd);
    return eb-ea;
  });
  $('#floor-competition').innerHTML=`<div class="competition-card"><div class="secondary">${esc(c.name)} · ${esc(c.status)} · ${ps.length} agents</div><div class="leader-mini scroll">${ps.map((p,i)=>{
    const eq=Z(p.scores?.equity);const unreal=Z(p.scores?.unrealized_pnl_usd);const liveEq=eq+unreal;
    const start=Z(p.scores?.starting_balance_usd)||1000;const closedPnl=eq-start;
    return `<div class="leader-row"><div class="rank-badge">#${i+1}</div><div><div class="primary">${esc(p.agent_id)}</div><div class="secondary">closed ${closedPnl>=0?'+':''}$${fmt(closedPnl,2)} · unreal ${unreal>=0?'+':''}$${fmt(unreal,2)}</div></div><div class="num ${liveEq>=start?'success':'danger'}" title="live equity = balance + unrealized">$${fmt(liveEq,2)}</div></div>`;
  }).join('')}</div></div>`}
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
function sigRows(rows,limit){const fmtOrDash=(v,d=6)=>{const x=Number(v);return Number.isFinite(x)&&x!==0?x.toFixed(d):'—'};return rows.slice(0,limit||rows.length).filter(s=>{const e=s.entry_price??s.levels?.entry;return e!=null&&Number(e)>0}).map(s=>{const sym=s.symbol||s.instrument_symbol||'';const dir=s.direction||s.consensus_direction;const trader=traderName(s);const liveRaw=s.current_price;const entryRaw=s.entry_price??s.levels?.entry;const distRaw=s.distance_to_entry_pct;const hasLive=liveRaw!=null&&Number.isFinite(Number(liveRaw))&&Number(liveRaw)>0;const hasEntry=entryRaw!=null&&Number.isFinite(Number(entryRaw))&&Number(entryRaw)>0;const hasStoredDist=distRaw!=null&&Number.isFinite(Number(distRaw))&&Number(distRaw)!==0;const computedDist=hasLive&&hasEntry?((Number(liveRaw)-Number(entryRaw))/Number(entryRaw)*100):null;const dist=computedDist!=null?computedDist:(hasStoredDist?Number(distRaw):null);const distCell=dist==null?'<span class="secondary">—</span>':`<span class="${dist>=0?'success':'danger'}">${pct(dist)}</span>`;const liveCell=hasLive?fmt(liveRaw,6):'<span class="secondary">—</span>';const status=s.position_status||s.status||'signal';const callId=s.signal_interpretation_id||s.id;return`<tr data-call="${esc(callId)}" data-news="${esc(s.news_item_id||'')}"><td>${rowMain(dispSymbol(sym),`${trader} · ${rel(s.signal_timestamp||s.created_at)}`)}</td><td>${pill(dir,dir)}</td><td class="num mono">${fmt(entryRaw,6)}</td><td class="num mono">${fmtOrDash(s.stop_loss||s.levels?.stop_loss,6)}</td><td class="num mono">${fmtOrDash(s.take_profit_1||s.levels?.take_profit_1,6)}</td><td class="num mono">${liveCell}</td><td class="num">${distCell}</td><td class="num">${fmt(Z(s.consensus_confidence)*100,0)}%</td><td>${pill(status,status)}</td><td><div class="secondary">${esc((s.headline||s.raw_signal_text||s.news_content||s.news_headline||'').slice(0,60))}</div></td></tr>`}).join('')}
// Round 11 (2026-05-24): closed/expired/cancelled rows have realized_pnl_usd_final
// populated and unrealized_pnl_usd null/zero. Branch on status so the P&L column
// shows the correct number for both live and historic positions.
const POS_CLOSED_STATUSES=new Set(['closed','expired','cancelled','invalidated','deleted']);
function posRows(rows,limit){return rows.slice(0,limit||rows.length).map(p=>{const closed=POS_CLOSED_STATUSES.has(String(p.status||'').toLowerCase());const pnl=Z(closed?(p.realized_pnl_usd_final??p.realized_pnl_usd??p.pnl_usd):(p.unrealized_pnl_usd??p.pnl_usd));return`<tr data-call="${esc(p.signal_interpretation_id||'')}" data-news="${esc(p.news_item_id||'')}"><td>${rowMain(dispSymbol(p.instrument_symbol),`${traderName(p)} · ${rel(p.signal_timestamp||p.opened_at)}`)}</td><td>${pill(p.direction,p.direction)}</td><td>${pill(p.status,p.status)}</td><td class="num mono">${fmt(p.entry_price,6)}</td><td class="num mono">${fmt(p.current_price,6)}</td><td class="num ${pnl>=0?'success':'danger'}">${usd(pnl)}</td><td class="num ${Z(p.pnl_pct)>=0?'success':'danger'}">${pct(Z(p.pnl_pct)*100)}</td><td class="num">$${fmt(p.notional_usd,0)}</td><td>${esc(p.signal_source||p._source||'—')}</td></tr>`}).join('')}
function renderOpsPage(){const s=state.snap||{};const c=chart('cost-chart');if(c)c.setOption({backgroundColor:'transparent',grid:{left:55,right:20,top:20,bottom:35},xAxis:{type:'category',data:['Spent','Remaining','Limit'],axisLabel:{color:'#8f8f9b'}},yAxis:{type:'value',axisLabel:{color:'#8f8f9b',formatter:v=>'$'+v},splitLine:{lineStyle:{color:'#2b2b34'}}},series:[{type:'bar',barWidth:34,data:[Z(s.api_cost_today_usd),Z(s.budget_remaining_usd||100),Z(s.budget_limit_usd||100)],itemStyle:{borderRadius:[10,10,0,0],color:p=>['#fc72ff','#35d07f','#7a5cff'][p.dataIndex]}}]});$('#services-grid').innerHTML=(s.services||[]).map(x=>{const hb=x.heartbeat;let st='disabled';if(hb){st=hb.is_stale?'stale':((hb.status&&hb.status!=='ok')?'degraded':'live')}else if(x.enabled_on_vps)st='enabled';const warn=(st==='stale'||st==='degraded');const tip=hb&&hb.message?` title="${esc(hb.message)}"`:'';return`<div class="service${warn?' service-warn':''}"${tip}><strong>${esc(x.name)}</strong><span>${esc(x.kind||'daemon')} · ${st}</span></div>`}).join('')||'<div class="empty">Service registry not available</div>'}

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

/* ─── Telegram feed — paginated ─── */
const _teleState={offset:0,allRows:[],hasMore:false};
async function renderTelegramPage(reset){
  if(reset){_teleState.offset=0;_teleState.allRows=[];_teleState.hasMore=false}
  if(!_teleState.allRows.length){
    try{
      const d=await api('/api/news/feed?window=30d&limit=40&source=telegram&offset='+_teleState.offset);
      _teleState.allRows=d.rows||[];
      _teleState.hasMore=_teleState.allRows.length>=40;
      _teleState.offset+=40;
    }catch(e){console.error(e);return}
  }
  let rows=_teleState.allRows;
  const ch=$('#telegram-channel')?.value;
  if(ch)rows=rows.filter(r=>r.channel_name===ch||r.author===ch);
  rows=filterRows(rows,$('#telegram-filter')?.value,['author','content','headline','channel_name']);
  if($('#telegram-media')?.value==='media')rows=rows.filter(r=>r.has_media);
  $('#telegram-list').innerHTML=rows.map(newsMsg).join('')||'<div class="empty">No Telegram messages</div>';
  if(_teleState.hasMore&&!$('#telegram-filter')?.value&&!$('#telegram-media')?.value){
    $('#telegram-list').innerHTML+='<div class="load-more"><button class="btn small" onclick="loadMoreTelegram()">Load more…</button></div>';
  }
  $$('#telegram-list [data-newsrow]').forEach(el=>el.onclick=()=>openDiscordDrawer(el.dataset.newsrow,el.dataset.newssrc));
  if(!document.getElementById('telegram-channel')._populated){
    const channels=new Set((_teleState.allRows||[]).map(r=>r.channel_name||r.author).filter(Boolean));
    const sel=document.getElementById('telegram-channel');
    channels.forEach(c=>{const o=document.createElement('option');o.value=c;o.textContent=c;sel.appendChild(o)});
    sel._populated=true;
  }
}
async function loadMoreTelegram(){
  try{
    const d=await api('/api/news/feed?window=30d&limit=40&source=telegram&offset='+_teleState.offset);
    const more=d.rows||[];
    _teleState.allRows=_teleState.allRows.concat(more);
    _teleState.hasMore=more.length>=40;
    _teleState.offset+=40;
    renderTelegramPage();
  }catch(e){console.error(e)}
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
    const [src,pr,grp]=await Promise.all([
      api('/api/settings/sources',{skipCompany:true}),
      api('/api/settings/prompts',{skipCompany:true}),
      api('/api/settings/source-groups',{skipCompany:true}),
    ]);
    state.sources=src.sources||[];
    state.prompts=pr.prompts||[];
    state.sourceGroups=(grp.groups||[]);
  }catch(e){console.error('fetchSourcesAndPrompts error',e);state.sources=[];state.prompts=[];state.sourceGroups=[];}
  renderSourcesTree();
}

function renderSourcesTree(){
  const el=$('#sources-tree');
  if(!el)return;
  const groups=state.sourceGroups||[];
  const prompts=state.prompts||[];

  // If no groups yet, show old flat view as fallback
  if(!groups.length){
    const sources=state.sources||[];
    if(!sources.length){el.innerHTML='<div class="empty">Loading sources…</div>';return;}
    renderFlatSources(el, sources, prompts);
    return;
  }

  let h='';

  groups.forEach(grp=>{
    const srcLabel=grp.source.toUpperCase();
    const srcClass=grp.source==='telegram'?'src-telegram':grp.source==='discord'?'src-discord':'src-api';
    const channels=grp.channels||[];
    const totalTraders=channels.reduce((s,c)=>s+(c.trader_count||0),0);
    const totalTracked=channels.reduce((s,c)=>s+(c.tracked_count||0),0);
    const allTracked=totalTracked===totalTraders&&totalTraders>0;
    const someTracked=totalTracked>0&&!allTracked;
    const chWithTraders=channels.filter(c=>(c.trader_count||0)>0);

    // Group header panel
    h+=`<div class="panel full" style="margin-top:0;border-left:4px solid var(--c-accent)">
      <div class="panel-head">
        <div>
          <h2>${esc(grp.name)} <span class="src-tag ${srcClass}">${esc(srcLabel)}</span></h2>
          <p>${channels.length} channel${channels.length!==1?'s':''} · ${totalTraders} trader${totalTraders!==1?'s':''} ·
            <label class="check-inline">
              <input type="checkbox" class="group-all-cb" data-group-id="${grp.id}"
                ${allTracked?'checked':''}> Track all
            </label>
          </p>
        </div>
      </div>
      <div class="settings-body" style="padding:0;grid-template-columns:1fr">`;

    // Channel sub-panels for channels with traders
    chWithTraders.forEach(ch=>{
      const chAllTracked=ch.tracked_count===ch.trader_count&&ch.trader_count>0;
      const chSomeTracked=ch.tracked_count>0&&!chAllTracked;
      const traders=ch.traders||[];

      h+=`<div class="panel" style="margin:0;border-radius:0;border-left:none;border-right:none">
        <div class="panel-head" style="background:var(--c-bg2)">
          <div>
            <h3 style="margin:0;font-size:13px">${esc(ch.name)}</h3>
            <p style="margin:2px 0 0;font-size:11px">${ch.trader_count} trader${ch.trader_count!==1?'s':''} ·
              ${ch.tracked_count} tracked ·
              <label class="check-inline">
                <input type="checkbox" class="channel-all-cb" data-channel-id="${ch.id}"
                  data-group-id="${grp.id}" ${chAllTracked?'checked':''}> Track channel
              </label>
            </p>
          </div>
        </div>
        <div class="table-wrap">
        <table class="data-table dense">
          <thead><tr>
            <th style="width:36px">On</th>
            <th>Trader</th>
            <th>Type</th>
            <th>Media</th>
            <th>Prompt</th>
            <th style="width:60px">Score</th>
          </tr></thead>
          <tbody>`;

      traders.forEach(u=>{
        const score=u.accuracy_score!=null&&u.accuracy_samples>0
          ? `${(u.accuracy_score*100).toFixed(0)}% <span class="secondary">n=${u.accuracy_samples}</span>`
          : '<span class="secondary">—</span>';
        const typeBadge=u.trader_type==='pro'?'<span class="badge badge-pro">pro</span>':
                         u.trader_type==='bot'?'<span class="badge badge-bot">bot</span>':
                         `<span class="badge neutral">${esc(u.trader_type||'—')}</span>`;

        h+=`<tr class="user-row" data-trader-id="${u.id}" data-channel-id="${ch.id}" data-group-id="${grp.id}">
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

    // Channels without traders (informational only)
    const emptyChannels=channels.filter(c=>(c.trader_count||0)===0);
    if(emptyChannels.length){
      h+=`<div class="panel" style="margin:0;border-radius:0;border-left:none;border-right:none;opacity:0.6">
        <div class="panel-head" style="background:var(--c-bg2)">
          <h3 style="margin:0;font-size:12px;color:var(--c-secondary)">${emptyChannels.length} channel${emptyChannels.length!==1?'s':''} with no tracked traders</h3>
        </div>
        <div class="table-wrap">
        <table class="data-table dense">
          <thead><tr><th>Channel</th></tr></thead>
          <tbody>`;
      emptyChannels.forEach(ch=>{
        h+=`<tr><td class="secondary">${esc(ch.name)}</td></tr>`;
      });
      h+=`</tbody></table></div></div>`;
    }

    h+=`</div></div>`;
  });

  // Prompt management section
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
  h+=`</tbody></table></div></div>`;

  el.innerHTML=h;

  // Wire events — three levels
  document.querySelectorAll('#sources-tree .group-all-cb').forEach(cb=>cb.onchange=()=>toggleGroupAll(cb));
  document.querySelectorAll('#sources-tree .channel-all-cb').forEach(cb=>cb.onchange=()=>toggleChannelAllNew(cb));
  document.querySelectorAll('#sources-tree .user-cb').forEach(cb=>cb.onchange=()=>toggleUserNew(cb));
  document.querySelectorAll('#sources-tree .media-types').forEach(sel=>sel.onchange=()=>updateUserTrack(sel));
  document.querySelectorAll('#sources-tree .prompt-pick').forEach(sel=>sel.onchange=()=>updateUserTrack(sel));
  // Prompt editor buttons
  document.querySelectorAll('#sources-tree [data-prompt-key]').forEach(btn=>btn.onclick=()=>openPromptModal(btn.dataset.promptKey));
  const newBtn=$('#btn-new-prompt');
  if(newBtn)newBtn.onclick=()=>openPromptModal('new');
}

/* ─── Flat source view (fallback when source-groups API unavailable) ─── */
function renderFlatSources(el, sources, prompts){
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
        <div class="table-wrap"><table class="data-table">
          <thead><tr><th style="width:40px">On</th><th>Trader</th><th>Type</th><th>Media</th><th>Prompt</th><th style="width:60px">Score</th></tr></thead>
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
    <div class="panel-head"><h2>Prompt Library</h2><button class="pill-btn" id="btn-new-prompt">+ New Prompt</button></div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>Key</th><th>Preview</th><th style="width:80px"></th></tr></thead>
      <tbody>`;
  prompts.forEach(p=>{
    h+=`<tr><td class="mono">${esc(p.key)}</td><td class="secondary">${esc(p.preview||'')}</td>
      <td><button class="pill-btn" data-prompt-key="${esc(p.key)}" style="padding:4px 12px;font-size:11px">Edit</button></td></tr>`;
  });
  h+=`</tbody></table></div></div>`;
  el.innerHTML=h;
  document.querySelectorAll('#sources-tree .channel-all-cb').forEach(cb=>cb.onchange=()=>toggleChannelAll(cb));
  document.querySelectorAll('#sources-tree .user-cb').forEach(cb=>cb.onchange=()=>toggleUser(cb));
  document.querySelectorAll('#sources-tree .media-types').forEach(sel=>sel.onchange=()=>updateUserTrack(sel));
  document.querySelectorAll('#sources-tree .prompt-pick').forEach(sel=>sel.onchange=()=>updateUserTrack(sel));
  document.querySelectorAll('#sources-tree [data-prompt-key]').forEach(btn=>btn.onclick=()=>openPromptModal(btn.dataset.promptKey));
  const newBtn=$('#btn-new-prompt');
  if(newBtn)newBtn.onclick=()=>openPromptModal('new');
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

/* ─── Group-level toggle — batch tracks all channels + traders in group ─── */
async function toggleGroupAll(cb){
  const groupId=cb.dataset.groupId;
  const checked=cb.checked;
  // Toggle all channel checkboxes in this group
  document.querySelectorAll(`#sources-tree .channel-all-cb[data-group-id="${groupId}"]`).forEach(chCb=>{
    chCb.checked=checked;
  });
  // Toggle all trader checkboxes in this group
  document.querySelectorAll(`#sources-tree .user-row[data-group-id="${groupId}"]`).forEach(row=>{
    const userCb=row.querySelector('.user-cb');
    if(userCb){userCb.checked=checked;}
  });
  // Batch API call
  try{
    await _putAPI('/api/settings/source-groups',{action:'track_group',id:parseInt(groupId),tracked:checked});
  }catch(e){console.error('group track failed',e);}
}

/* ─── Channel-level toggle — batch tracks all traders in channel ─── */
async function toggleChannelAllNew(cb){
  const channelId=cb.dataset.channelId;
  const groupId=cb.dataset.groupId;
  const checked=cb.checked;
  // Toggle all trader checkboxes in this channel
  document.querySelectorAll(`#sources-tree .user-row[data-channel-id="${channelId}"]`).forEach(row=>{
    const userCb=row.querySelector('.user-cb');
    if(userCb){userCb.checked=checked;}
  });
  // Update group-level checkbox state
  updateGroupCbState(groupId);
  // Batch API call
  try{
    await _putAPI('/api/settings/source-groups',{action:'track_channel',id:parseInt(channelId),tracked:checked});
  }catch(e){console.error('channel track failed',e);}
}

/* ─── Trader-level toggle (new tree) — individual + cascade up ─── */
async function toggleUserNew(cb){
  await updateUserTrack(cb);
  // Update channel all checkbox
  const row=cb.closest('.user-row');
  if(row){
    const channelId=row.dataset.channelId;
    const groupId=row.dataset.groupId;
    if(channelId){
      const userCbs=document.querySelectorAll(`#sources-tree .user-row[data-channel-id="${channelId}"] .user-cb`);
      const allChecked=Array.from(userCbs).every(c=>c.checked);
      const someChecked=Array.from(userCbs).some(c=>c.checked);
      const chAllCb=document.querySelector(`#sources-tree .channel-all-cb[data-channel-id="${channelId}"]`);
      if(chAllCb){
        chAllCb.checked=allChecked;
        chAllCb.indeterminate=someChecked&&!allChecked;
      }
    }
    if(groupId)updateGroupCbState(groupId);
  }
}

/* ─── Update group-level checkbox based on all channels ─── */
function updateGroupCbState(groupId){
  const chCbs=document.querySelectorAll(`#sources-tree .channel-all-cb[data-group-id="${groupId}"]`);
  const allChecked=Array.from(chCbs).every(c=>c.checked);
  const someChecked=Array.from(chCbs).some(c=>c.checked);
  const grpCb=document.querySelector(`#sources-tree .group-all-cb[data-group-id="${groupId}"]`);
  if(grpCb){
    grpCb.checked=allChecked;
    grpCb.indeterminate=someChecked&&!allChecked;
  }
}

/* ─── Prompt modal ─── */
function openPromptModal(key){
  const isNew=key==='new';
  const title=isNew?'New Prompt':`Edit Prompt`;
  const subtitle=isNew?'Create a new ChartHacker prompt':'';
  const body=isNew
    ? `<label>Key <input class="input" id="prompt-key" placeholder="e.g. telegram/rose/prompt"></label>
       <label>System Prompt <textarea class="input" id="prompt-system" rows="12" placeholder="You are ChartHacker..."></textarea></label>
       <label>User Prompt Template <textarea class="input" id="prompt-user" rows="3" placeholder="Analyze this chart for {symbol}..."></textarea></label>
       <div class="btn-row"><button class="pill-btn" id="btn-save-prompt">Save</button><button class="pill-btn" id="btn-cancel-prompt">Cancel</button></div>`
    : `<div class="empty" style="padding:60px">Loading…</div>`;

  // Build modal HTML
  const modal=document.createElement('div');
  modal.className='modal-backdrop';
  modal.id='prompt-modal';
  modal.innerHTML=`
    <div class="modal">
      <div class="modal-head">
        <div><h2>${esc(title)}${isNew?'':` <span class="mono secondary">${esc(key)}</span>`}</h2>
          ${subtitle?`<p class="secondary">${esc(subtitle)}</p>`:''}</div>
        <div class="modal-actions">
          ${!isNew?`<button class="pill-btn" id="btn-rename-prompt" style="padding:4px 12px;font-size:11px">Rename</button>
                    <button class="pill-btn" id="btn-delete-prompt" style="padding:4px 12px;font-size:11px;color:var(--red)">Delete</button>`:''}
          <button class="drawer-close" id="btn-close-modal">×</button>
        </div>
      </div>
      <div class="modal-body" id="prompt-modal-body">${body}</div>
    </div>`;

  document.body.appendChild(modal);

  const close=()=>modal.remove();
  modal.onclick=e=>{if(e.target===modal)close()};
  modal.querySelector('#btn-close-modal').onclick=close;
  modal.querySelector('#btn-cancel-prompt')?.addEventListener('click',close);

  if(!isNew){
    api(`/api/settings/prompts/versions/${encodeURIComponent(key)}`,{skipCompany:true}).then(data=>{
      const bodyEl=modal.querySelector('#prompt-modal-body');
      if(!bodyEl)return;
      bodyEl.innerHTML=`
        <label>System Prompt <textarea class="input" id="prompt-system" rows="12">${esc(data.system_prompt||'')}</textarea></label>
        <label>User Prompt Template <textarea class="input" id="prompt-user" rows="3">${esc(data.user_prompt_template||'')}</textarea></label>
        <div class="btn-row"><button class="pill-btn" id="btn-save-prompt">Save</button><button class="pill-btn" id="btn-cancel-prompt">Cancel</button></div>`;
      modal.querySelector('#btn-cancel-prompt')?.addEventListener('click',close);
      modal.querySelector('#btn-save-prompt').onclick=()=>savePrompt(key,close);
    }).catch(e=>{modal.querySelector('#prompt-modal-body').innerHTML='<div class="empty">Failed to load prompt</div>';});
  }else{
    modal.querySelector('#btn-save-prompt').onclick=()=>savePrompt('new',close);
  }

  // Delete handler
  modal.querySelector('#btn-delete-prompt')?.addEventListener('click',async()=>{
    if(!confirm(`Delete prompt "${key}"? This cannot be undone.`))return;
    try{
      await _delAPI(`/api/settings/prompts/versions/${encodeURIComponent(key)}`);
      close();
      fetchSourcesAndPrompts();
      loadPromptsTab();
    }catch(e){alert('Delete failed: '+(e.error||e));}
  });

  // Rename handler
  modal.querySelector('#btn-rename-prompt')?.addEventListener('click',()=>{
    const newKey=prompt('Rename prompt to:',key);
    if(!newKey||newKey===key)return;
    _putAPI(`/api/settings/prompts/versions/rename`,{old_version:key,new_version:newKey}).then(()=>{
      close();
      fetchSourcesAndPrompts();
      loadPromptsTab();
    }).catch(e=>alert('Rename failed: '+(e.error||e)));
  });
}

async function _delAPI(path){
  const u=new URL(path.replace(/^\//,''),document.baseURI);
  const r=await fetch(u,{method:'DELETE'});
  const j=await r.json();
  if(!r.ok)throw j;
  return j;
}

async function savePrompt(key,closeFn){
  const keyInput=$('#prompt-key');
  const actualKey=key==='new'?keyInput?.value?.trim():key;
  if(!actualKey){alert('Key is required');return;}
  const sp=$('#prompt-system')?.value||'';
  const ut=$('#prompt-user')?.value||'';
  if(!sp||!ut){alert('System prompt and user template are required');return;}
  try{
    // Save to prompt_versions with computed hash
    await _putAPI('/api/settings/prompts/versions/save',{
      version:actualKey,
      system_prompt:sp,
      user_prompt_template:ut,
      source:'manual'
    });
    closeFn();
    fetchSourcesAndPrompts();
    loadPromptsTab();
  }catch(e){alert('Save failed: '+(e.error||e));}
}

/* ─── Round 14 (2026-05-29): provider-aware all-slots model picker ───────── */
const _mpCatalogue={};  // slot -> {policies:[], models:[]}
const _mpFilter={};     // slot -> {provider:'all', q:'', selected:'<model_id>'}

function _provBadge(p){
  const cls=p==='requesty'?'prov-requesty':'prov-openrouter';
  return `<span class="mp-badge ${cls}">${p==='requesty'?'Requesty':'OpenRouter'}</span>`;
}
function _costLabel(m){
  const i=m.input_cost_per_mtok, o=m.output_cost_per_mtok;
  if(i==null&&o==null)return '<span class="mp-cost mp-cost-na">cost varies</span>';
  const f=v=>v==null?'?':('$'+Number(v).toFixed(v<1?3:2));
  return `<span class="mp-cost">${f(i)} / ${f(o)} per M</span>`;
}
function _ctxLabel(m){
  if(!m.context_length)return '';
  const k=m.context_length>=1000?Math.round(m.context_length/1000)+'k':m.context_length;
  return `<span class="mp-ctx">${k} ctx</span>`;
}

async function renderModelPicker(){
  const body=$('#settings-body');
  if(!body)return;
  body.innerHTML='<div class="empty">Loading model slots\u2026</div>';
  let slots=[];
  try{
    const r=await api('/api/settings/slots',{skipCompany:true});
    slots=r.slots||[];
  }catch(e){
    body.innerHTML=`<div class="empty">Could not load slots: ${esc(String(e.error||e.message||e))}</div>`;
    return;
  }
  body.style.display='block';  // break out of the inherited 3-col grid
  let h=`<div class="mp-topbar">
    <button class="pill-btn" id="mp-refresh">\u21bb Refresh model list from providers</button>
    <span class="secondary small" id="mp-refresh-status"></span>
  </div><div class="mp-grid">`;
  h+=slots.map(s=>{
    const kindTag=s.vision
      ?'<span class="mp-badge kind-vision">vision</span>'
      :'<span class="mp-badge kind-text">text</span>';
    return `<div class="mp-card" data-slot="${s.slot}">
      <div class="mp-head">
        <h3>${esc(s.label)} ${kindTag}</h3>
        <div class="mp-current">${_provBadge(s.provider||'openrouter')}
          <span class="mono">${esc(s.model||'\u2014')}</span>
          ${sourceBadge(s.provider_source)} ${sourceBadge(s.model_source)}</div>
      </div>
      <div class="mp-controls">
        <div class="mp-providers">
          <button class="mp-prov active" data-prov="all">All</button>
          <button class="mp-prov" data-prov="openrouter">OpenRouter</button>
          <button class="mp-prov" data-prov="requesty">Requesty</button>
        </div>
        <input class="input mp-search" placeholder="Search ${s.vision?'vision ':''}models\u2026">
      </div>
      <div class="mp-list" id="mp-list-${s.slot}"><div class="empty">Loading models\u2026</div></div>
      <div class="mp-status" id="mp-status-${s.slot}"></div>
    </div>`;
  }).join('');
  h+='</div>';
  body.innerHTML=h;

  const refreshBtn=$('#mp-refresh');
  if(refreshBtn)refreshBtn.onclick=async ()=>{
    const st=$('#mp-refresh-status'); if(st)st.textContent='Refreshing\u2026';
    try{
      const r=await fetch(new URL('api/settings/catalogue/refresh',document.baseURI),{method:'POST'});
      const j=await r.json(); if(!r.ok||!j.ok)throw j;
      const s=j.summary||{};
      if(st)st.textContent=`Synced OpenRouter ${s.openrouter||0} + Requesty ${s.requesty||0}.`;
      Object.keys(_mpCatalogue).forEach(k=>delete _mpCatalogue[k]);
      slots.forEach(s=>_loadSlotCatalogue(s.slot));
    }catch(e){if(st)st.textContent='\u26a0 '+(e.error||e.message||'refresh failed');}
  };

  slots.forEach(s=>{
    _mpFilter[s.slot]={provider:'all',q:'',selected:s.model};
    const card=$(`.mp-card[data-slot="${s.slot}"]`);
    if(!card)return;
    card.querySelectorAll('.mp-prov').forEach(btn=>{
      btn.onclick=()=>{
        card.querySelectorAll('.mp-prov').forEach(b=>b.classList.remove('active'));
        btn.classList.add('active');
        _mpFilter[s.slot].provider=btn.dataset.prov;
        _renderSlotList(s.slot);
      };
    });
    const search=card.querySelector('.mp-search');
    if(search)search.oninput=()=>{_mpFilter[s.slot].q=search.value.trim().toLowerCase();_renderSlotList(s.slot);};
    _loadSlotCatalogue(s.slot);
  });
}

async function _loadSlotCatalogue(slot){
  if(_mpCatalogue[slot]){_renderSlotList(slot);return;}
  try{
    const r=await api(`/api/settings/catalogue?slot=${encodeURIComponent(slot)}`,{skipCompany:true});
    _mpCatalogue[slot]={policies:r.policies||[],models:r.models||[]};
  }catch(e){
    const el=$(`#mp-list-${slot}`);
    if(el)el.innerHTML=`<div class="empty">Could not load: ${esc(String(e.error||e.message||e))}</div>`;
    return;
  }
  _renderSlotList(slot);
}

function _mpItem(slot,m,selected){
  const sel=(m.model_id===selected)?' selected':'';
  const pol=m.is_policy?'<span class="mp-badge policy">policy</span>':'';
  return `<button class="mp-item${sel}" data-slot="${slot}" data-prov="${m.provider}" data-model="${esc(m.model_id)}" title="${esc(m.model_id)}">
    <span class="mp-item-name">${esc(m.label||m.model_id)}</span>
    <span class="mp-item-meta">${_provBadge(m.provider)}${pol}${_ctxLabel(m)}${_costLabel(m)}</span>
  </button>`;
}

function _renderSlotList(slot){
  const el=$(`#mp-list-${slot}`); if(!el)return;
  const cat=_mpCatalogue[slot]; if(!cat){el.innerHTML='<div class="empty">Loading\u2026</div>';return;}
  const f=_mpFilter[slot]||{provider:'all',q:''};
  const match=m=>{
    if(f.provider!=='all'&&m.provider!==f.provider)return false;
    if(f.q&&!(`${m.label} ${m.model_id}`.toLowerCase().includes(f.q)))return false;
    return true;
  };
  const pols=(cat.policies||[]).filter(match), mods=(cat.models||[]).filter(match);
  let h='';
  if(pols.length)h+='<div class="mp-group">Routing policies</div>'+pols.map(m=>_mpItem(slot,m,f.selected)).join('');
  if(mods.length)h+='<div class="mp-group">Models</div>'+mods.map(m=>_mpItem(slot,m,f.selected)).join('');
  if(!h)h='<div class="empty">No matching models.</div>';
  el.innerHTML=h;
  el.querySelectorAll('.mp-item').forEach(btn=>{
    btn.onclick=()=>_selectModel(slot,btn.dataset.prov,btn.dataset.model);
  });
}

async function _selectModel(slot,provider,model){
  const st=$(`#mp-status-${slot}`); if(st)st.textContent='Saving\u2026';
  try{
    const r=await fetch(new URL('api/settings/slot',document.baseURI),{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({slot,provider,model})
    });
    const j=await r.json(); if(!r.ok||!j.ok)throw j;
    if(st)st.textContent=`Saved \u2014 ${provider} / ${model}. Effective within 60 s.`;
    _mpFilter[slot].selected=model;
    const card=$(`.mp-card[data-slot="${slot}"]`);
    if(card){
      const cur=card.querySelector('.mp-current');
      if(cur)cur.innerHTML=`${_provBadge(provider)}<span class="mono">${esc(model)}</span> ${sourceBadge('db')} ${sourceBadge('db')}`;
    }
    _renderSlotList(slot);
  }catch(e){if(st)st.textContent='\u26a0 '+(e.error||e.message||'save failed');}
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
  const {history}=state.settings;

  /* Round 14 (2026-05-29): provider-aware, all-slots model picker.
     Replaces the old 3 fixed dropdowns. Pulls live OpenRouter + Requesty
     catalogues, pins routing policies on top, filters by provider + search,
     and shows provider / context / $-per-Mtok per row. */
  renderModelPicker();

  /* Agent sizing knobs (Phase 1.5) */
  renderSizingPanel();

  /* Sources & Prompts section */
  fetchSourcesAndPrompts();

  /* History table */
  const histBody=history.length
    ? history.map(h=>`<tr><td class="mono small">${esc((h.changed_at||'').replace('T',' ').slice(0,19))}</td><td>${pill(h.slot,h.slot)}</td><td class="mono small">${esc(h.model_old||'\u2014')}</td><td class="mono small">${esc(h.model_new)}</td><td class="secondary small">${esc(h.actor_label||'')}</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">No model changes recorded yet.</td></tr>';
  $('#settings-history').innerHTML=`<table class="data-table"><thead><tr><th>When</th><th>Slot</th><th>Old</th><th>New</th><th>Actor</th></tr></thead><tbody>${histBody}</tbody></table>`;

  wireSettings();
}

/* Phase 1.5 (2026-05-29): operator-tunable agent sizing knobs.
   Reads /api/settings/copy-sizing, renders one number input per knob, and
   POSTs the change back. Mirrors the vision-model save UX (60s effect). */
async function renderSizingPanel(){
  const el=$('#settings-sizing-body');
  if(!el)return;
  el.innerHTML='<div class="empty">Loading sizing knobs\u2026</div>';
  let knobs={};
  try{
    const r=await fetch(new URL('api/settings/copy-sizing',document.baseURI));
    const j=await r.json();
    if(!r.ok||!j.ok)throw j;
    knobs=j.knobs||{};
  }catch(e){
    el.innerHTML=`<div class="empty">Could not load sizing knobs: ${esc(String(e.error||e.message||e))}</div>`;
    return;
  }
  const order=['risk_pct_5','risk_pct_3','max_concurrent_5','max_concurrent_3','leverage_cap','spot_lev_3x'];
  const cards=order.filter(k=>knobs[k]).map(k=>{
    const m=knobs[k];
    const step=(m.type==='int')?'1':'0.1';
    return `<div class="settings-card" data-knob="${k}">
      <div class="settings-card-head">
        <h3>${esc(m.label)} ${sourceBadge(m.source)}</h3>
        <p class="secondary">${esc(m.description)}</p>
      </div>
      <label class="settings-label">Value
        <input class="input wide sizing-input" type="number" step="${step}" data-knob="${k}" value="${esc(String(m.value))}">
      </label>
      <div class="settings-actions">
        <button class="pill-btn sizing-save-btn" data-knob="${k}">Save</button>
        <span class="settings-test-status" id="sizing-status-${k}"></span>
        <span class="secondary small">default ${esc(String(m.default))}</span>
      </div>
    </div>`;
  }).join('');
  el.innerHTML=cards||'<div class="empty">No sizing knobs available.</div>';
  $$('.sizing-save-btn').forEach(btn=>{
    btn.onclick=async ()=>{
      const key=btn.dataset.knob;
      const input=$(`input.sizing-input[data-knob="${key}"]`);
      const value=input?parseFloat(input.value):null;
      const status=$(`#sizing-status-${key}`);
      if(value==null||isNaN(value)){if(status)status.textContent='\u26a0 enter a number';return;}
      if(status)status.textContent='Saving\u2026';
      btn.disabled=true;
      try{
        const r=await fetch(new URL('api/settings/copy-sizing',document.baseURI),{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({key,value})
        });
        const j=await r.json();
        if(!r.ok||!j.ok)throw j;
        if(status)status.textContent='Saved \u2014 effective within 60 s.';
        setTimeout(()=>renderSizingPanel(),1500);
      }catch(e){
        if(status)status.textContent=`\u26a0 ${(e.error||e.message||'save failed')}`;
      }finally{btn.disabled=false;}
    };
  });
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
function renderSignalsTable(rows){table('#signals-table',[{key:'symbol',label:'Signal',sortable:true},{key:'side',label:'Side',sortable:true},{key:'entry',label:'Entry',num:1,sortable:true},{key:'sl',label:'SL',num:1,sortable:true},{key:'tp',label:'TP1',num:1,sortable:true},{label:'Δ entry',num:1},{label:'Status'},{label:'Call text'}],[sigRows(rows)]);wireRows()}
/* Round 12 (2026-05-24): renderPositionsTable removed.
   Positions tab now uses Live/Historic split via renderPositionsLive() and
   renderPositionsHistoric(). The legacy single-table renderer had no callers
   after the Round 11 split. */
async function renderTradersPage(){try{state.tradersIntel=await api('/api/traders-intel');}catch(e){console.warn('traders-intel failed',e);state.tradersIntel=null}let rows=state.tradersIntel?.traders||[];const q=$('#traders-filter')?.value;rows=filterRows(rows,q,['actor_id','display_name','platform','handle_normalized']);const sort=$('#traders-sort')?.value||'win_rate';rows.sort((a,b)=>{if(sort==='trades')return Z(b.total_trades)-Z(a.total_trades);if(sort==='pnl')return Z(b.total_pnl)-Z(a.total_pnl);if(sort==='freq')return Z(b.trade_frequency_weekly)-Z(a.trade_frequency_weekly);return Z(b.win_rate)-Z(a.win_rate)});const coinChips=(coins,limit=3)=>(coins||[]).slice(0,limit).map(c=>`<span class="coin-chip-mini ${Z(c.pnl)>=0?'win':'loss'}" title="${esc(dispSymbol(c.symbol))}: ${c.trades}t, ${c.wins}w, ${usd(c.pnl)}">${esc(dispSymbol(c.symbol))}</span>`).join('')||'<span class="secondary">—</span>';const body=rows.map(t=>{const name=t.display_name||t.actor_id;const wr=t.win_rate;const wrDisplay=wr!=null?`${fmt(wr,1)}%`:'—%';return`<tr data-trader="${esc(t.actor_id)}"><td>${rowMain(name,`${t.platform||'discord'} · ${t.trader_type||'unknown'} · ${t.total_trades||0}t`)}</td><td class="num">${wrDisplay}</td><td class="num">${t.total_trades||0}</td><td class="num">${t.trade_frequency_weekly||'—'}/wk</td><td class="num success">${t.avg_win?usd(t.avg_win):'—'}</td><td class="num danger">${t.avg_loss?usd(t.avg_loss):'—'}</td><td class="num ${Z(t.total_pnl)>=0?'success':'danger'}">${t.total_pnl!=null?usd(t.total_pnl):'—'}</td><td>${coinChips(t.most_traded_coins)}</td><td>${coinChips(t.most_profitable_coins)}</td></tr>`}).join('');table('#traders-table',[{label:'Discord / actor'},{label:'Win Rate',num:1},{label:'Trades',num:1},{label:'Freq',num:1},{label:'Avg Win',num:1},{label:'Avg Loss',num:1},{label:'Total P&L',num:1},{label:'Top Coins'},{label:'Best Coins'}],[body]);$$('#traders-table tr[data-trader]').forEach(tr=>tr.onclick=()=>openTrader(tr.dataset.trader))}

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
    if(res)res.source=src;
    if(!allSignals.length){
      // Show message content + raw chart gallery even when no interpretations exist yet
      const news=res.news_item||res.news||{};
      const gallery=res.media_gallery||[];
      const author=news.author||'unknown';
      let html=`<div class="discord-drawer-msg">
        <div class="discord-avatar">${initials(author)}</div>
        <div><div class="discord-user">${esc(author)}</div>
        <div class="discord-text big">${esc(news.content||news.headline||'No message content')}</div>
        <div class="discord-meta"><span class="secondary">${esc(news.source||'discord')} · ${rel(news.published_at||news.collected_at)}</span>${news.has_media?` · ${news.media_count||0} chart${(news.media_count||1)>1?'s':''}`:''}</div></div>
      </div>`;
      if(gallery.length){
        html+=`<div class="drawer-panel"><h3>Charts (${gallery.length})</h3>`;
        gallery.forEach(m=>{
          html+=`<div class="posted-chart" style="margin-bottom:12px"><img class="chart-img xl" src="api/media/${m.id}" loading="lazy" decoding="async" onerror="this.onerror=null;this.parentElement.innerHTML='<div class=empty>Chart image not available</div>'"></div>`;
        });
        html+=`</div>`;
      }
      html+=`<div class="secondary" style="margin-top:12px">AI interpretation pending — check back soon</div>`;
      $('#drawer-body').innerHTML=html;
      return;
    }
    drawDiscordTabs(allSignals,res);
  }catch(e){
    console.error('Discord drawer failed',e);
    $('#drawer-body').innerHTML='<div class="empty">Could not load charts</div>';
  }
}

function drawDiscordTabs(signals,res){
  const msg=res.news_item||res.news||signals[0]||{};
  const trader=traderName(signals[0])||msg.author||'trader';
  const src=res.source||signals[0]?.source||msg.source||'discord';
  const isTelegram=src==='telegram';
  let html=`<div class="discord-drawer-msg">
    <div class="discord-avatar">${initials(trader)}</div>
    <div><div class="discord-user">${esc(trader)}</div>
    <div class="discord-text big">${esc(msg.content||msg.headline||signals[0]?.raw_signal_text||'')}</div>
    ${isTelegram&&signals.length?`<div class="discord-meta" style="margin-top:6px">${signals.map(s=>`<span class=\"pill ${s.consensus_direction||'long'}\">${esc(dispSymbol(s.instrument_symbol))} ${esc(s.consensus_direction||'')} · E ${lvl(s.entry_price,s.levels?.entry,4)} · SL ${lvl(s.stop_loss,s.levels?.stop_loss,4)} · TP ${lvl(s.take_profit_1,s.levels?.take_profit_1,4)}</span>`).join(' ')}</div>`:''}
    </div>
  </div>`;

  // Build tabs grouped by COIN from ALL media items
  const gallery=res.media_gallery||[];
  // Collect coins from signals + map media to best-guess coins
  const coinGroups={}; // coin -> {signals:[], media:[]}
  signals.forEach(s=>{
    const sym=dispSymbol(s.instrument_symbol)||'Chart';
    if(!coinGroups[sym])coinGroups[sym]={signals:[],media:[]};
    coinGroups[sym].signals.push(s);
  });
  // Assign gallery-only media to best-guess coin or 'All Charts'
  gallery.forEach(m=>{
    let assigned=false;
    for(const sym of Object.keys(coinGroups)){
      if(coinGroups[sym].signals.some(s=>s.media?.id==m.id)){coinGroups[sym].media.push(m);assigned=true;break}
    }
    if(!assigned){
      if(!coinGroups['All Charts'])coinGroups['All Charts']={signals:[],media:[]};
      coinGroups['All Charts'].media.push(m);
    }
  });

  const coinKeys=Object.keys(coinGroups);
  if(coinKeys.length>0){
    html+=`<div class="chart-tabs">`;
    let activeIdx=0;
    coinKeys.forEach((sym,i)=>{
      const total=coinGroups[sym].signals.length+coinGroups[sym].media.length;
      html+=`<button class="chart-tab ${i===0?'active':''}" data-coin-idx="${i}">${esc(sym)}${total>1?` · ${total}`:''}</button>`;
    });
    html+=`</div>`;
    html+=`<div class="chart-tab-content" id="chart-tab-content">Loading…</div>`;
  }
  $('#drawer-body').innerHTML=html;

  // Wire coin tabs
  $$('#drawer-body .chart-tab').forEach(b=>b.onclick=()=>{
    $$('#drawer-body .chart-tab').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    const idx=parseInt(b.dataset.coinIdx);
    const sym=coinKeys[idx];
    renderCoinTab(coinGroups[sym],sym);
  });

  // Render first coin tab
  if(coinKeys.length)renderCoinTab(coinGroups[coinKeys[0]],coinKeys[0]);
}

async function renderCoinTab(group,sym){
  const el=$('#chart-tab-content'); if(!el)return;
  const allItems=[];
  // Signals first (with replays), then gallery media
  group.signals.forEach(s=>allItems.push({type:'sig',data:s}));
  group.media.forEach(m=>allItems.push({type:'media',data:m}));
  if(!allItems.length){el.innerHTML='<div class="empty">No charts for this coin</div>';return}
  let h='';
  for(const item of allItems){
    if(item.type==='sig'){
      const s=item.data;
      const mid=s.media?.id;
      // Rich interpretation panel: trader's logic, AI's read, techniques.
      const ch=s.chart_hacker||{};
      const th=s.thesis||{};
      const tags=s.tags||{};
      const traderTrades=Array.isArray(ch.trader_trades)?ch.trader_trades:[];
      const aiTrades=Array.isArray(ch.chart_hacker_trades)?ch.chart_hacker_trades:[];
      const patterns=Array.isArray(tags.pattern)?tags.pattern:[];
      const ca=(ch.chart_analysis&&typeof ch.chart_analysis==='object')?ch.chart_analysis:{};
      const commentary=ca.market_commentary||'';
      const tradeRow=(t,who)=>`<div class="drawer-trade-row"><span class="pill ${esc(t.direction||'')}">${esc(who)} ${esc(t.direction||'?')}</span> E ${fmt(t.entry,4)} · SL ${fmt(t.stop_loss||t.sl,4)} · TP ${fmt(t.tp1||t.take_profit,4)}${t.confidence?` · ${fmt(t.confidence,2)} conf`:''}${t.rationale?`<div class="secondary" style="margin-top:2px">${esc(String(t.rationale).slice(0,220))}</div>`:''}</div>`;
      h+=`<div class="drawer-panel chart-panel">
        <div class="chart-toolbar"><div><h3>${esc(dispSymbol(s.instrument_symbol))} · ${esc(s.consensus_direction||'')} · ${s.consensus_confidence?fmt(s.consensus_confidence,3):''} conf</h3>
        <p>Entry ${lvl(s.entry_price,s.levels?.entry,4)} · SL ${lvl(s.stop_loss,s.levels?.stop_loss,4)} · TP ${lvl(s.take_profit_1,s.levels?.take_profit_1,4)}</p></div></div>
        <div class="chart-split">
          ${mid?`<div class="posted-chart"><img class="chart-img xl" src="api/media/${mid}" loading="lazy" decoding="async" onerror="this.onerror=null;this.parentElement.innerHTML='<div class=empty>Chart image not available</div>'"></div>`:''}
          <div id="disc-chart-${esc(s.id)}" class="replay-chart"></div>
        </div>
        ${traderTrades.length?`<div class="drawer-section"><h4>Trader's setup${traderTrades.length>1?'s':''}</h4>${traderTrades.map(t=>tradeRow(t,'Trader')).join('')}</div>`:''}
        ${th.trader_stated?`<div class="drawer-section"><h4>Trader's logic</h4><div class="secondary">${esc(String(th.trader_stated).slice(0,400))}</div></div>`:''}
        ${aiTrades.length?`<div class="drawer-section"><h4>ChartHacker would trade</h4>${aiTrades.map(t=>tradeRow(t,'AI')).join('')}</div>`:''}
        ${s.llm&&s.llm.reasoning?`<div class="drawer-section"><h4>AI interpretation</h4><div class="secondary">${esc(String(s.llm.reasoning).slice(0,400))}</div></div>`:''}
        ${commentary?`<div class="drawer-section"><h4>Market commentary</h4><div class="secondary">${esc(String(commentary).slice(0,300))}</div></div>`:''}
        ${patterns.length?`<div class="drawer-section"><h4>Techniques observed</h4>${patterns.slice(0,8).map(p=>`<span class="pill" style="margin-right:4px">${esc(p)}</span>`).join('')}</div>`:''}
        ${ch.ai_agreement_score!=null?`<div class="drawer-section secondary">AI ↔ Trader agreement: ${fmt(ch.ai_agreement_score,2)}${ch.ai_comment?` — ${esc(String(ch.ai_comment).slice(0,200))}`:''}</div>`:''}
      </div>`;
    }else{
      const m=item.data;
      h+=`<div class="drawer-panel"><div class="posted-chart" style="border:0;background:transparent">
        <img class="chart-img xl" src="api/media/${m.id}" loading="lazy" decoding="async" onerror="this.onerror=null;this.parentElement.innerHTML='<div class=empty>Chart image not available</div>'">
      </div></div>`;
    }
  }
  el.innerHTML=h;
  // Load replays for signals
  group.signals.forEach(s=>{
    if(s.id)setTimeout(async()=>{
      try{const r=await api(`/api/signal-replay?id=${encodeURIComponent(s.id)}`);drawReplayInline(r,document.getElementById('disc-chart-'+s.id)?.parentElement?.parentElement||el)}catch(e){}
    },100);
  });
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

function drawReplayInline(raw,parentEl){
  if(!raw||!raw.ok){parentEl.innerHTML='<div class="empty">No replay data</div>';return}
  const base=raw;
  const legIdx=raw.selected_leg??raw._selectedLeg??0;
  const r=mergeReplayLeg(base,legIdx);
  const sig=r.signal||{}, pos=r.position||{}, levels=sig.levels||{}, trader=r.trader||{}, news=r.news||{};
  const legTabs=replayLegTabsHtml(base,legIdx);
  const traderName=trader.handle_raw||trader.display_name||trader.handle_normalized||news.author||'trader';
  const outcome=pos.outcome||pos.status||'tracking';
  const pnl=Z(pos.realized_pnl_usd_final??pos.realized_pnl_usd??pos.unrealized_pnl_usd);
  const media=r.media_url||r.annotated_chart_url;
  // Render the trader's call timestamp in the chart header so the viewer can
  // see exactly which moment the post lined up with on the candle replay.
  const callIso=r.call_ts?String(r.call_ts):'';
  const callPretty=callIso?callIso.slice(0,16).replace('T',' ')+' UTC':'';
  parentEl.innerHTML=`
    ${legTabs}
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
    wireReplayLegTabs(base,r.id,(payload)=>drawReplayInline(payload,parentEl));
  },100);
}

/* ─── Radar / Entry watch ─── */
// Phase 2 (2026-05-29): single source of truth for distance-to-entry.
// signalDistanceSigned() returns the SIGNED % (live-entry)/entry — positive =
// price ABOVE entry, negative = BELOW. signalDistance() is its absolute value,
// used purely for "closest first" sorting. Display everywhere (Floor + Radar)
// uses the signed value so the same symbol shows the same number on every tab.
function signalDistanceSigned(s){const live=Z(s.current_price||s._livePrice);const entry=Z(s.entry_price||s.levels?.entry);if(entry&&live)return (live-entry)/entry*100;const d=Z(s.distance_to_entry_pct);return Number.isFinite(d)&&d!==0?d:null}
function signalDistance(s){const sd=signalDistanceSigned(s);return sd==null?999999:Math.abs(sd)}
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
function radarCard(s,i){const id=s.signal_interpretation_id||s.id;const sym=dispSymbol(s.symbol||s.instrument_symbol);const dir=(s.consensus_direction||s.direction||'').toLowerCase();const entry=Z(s.entry_price||s.levels?.entry), sl=Z(s.stop_loss||s.levels?.stop_loss), tp=Z(s.take_profit_1||s.levels?.take_profit_1);const distSigned=signalDistanceSigned(s);const ageDays=(Date.now()-new Date(s.signal_timestamp||s.created_at))/86400000;const trader=traderName(s);const stale=ageDays>7?' stale':'';const rr=entry&&sl&&tp?Math.abs((tp-entry)/(entry-sl)):0;const live=(s.current_price||s._livePrice)?` · live ${fmt(s.current_price||s._livePrice,6)}`:'';const aiBadge=aiInferredBadge(s);const filledBadge=isJustFilled(s)?'<span class="badge just-filled" title="Entry was reached in the last 30 minutes — this setup just became an open position">JUST FILLED</span>':'';return `<div class="radar-card${stale}${filledBadge?' just-filled-card':''}" data-call="${esc(id)}" data-news="${esc(s.news_item_id||'')}" data-radar-idx="${i}"><div class="radar-top"><div>${rowMain(sym,`${trader} · ${(s.timeframe||'1m')} · ${rel(s.signal_timestamp||s.created_at)} old${live}`)}</div><div class="radar-top-right">${filledBadge}${aiBadge}${pill(dir,dir)}</div></div><div class="mini-tv" id="mini-radar-${i}">${(!s._candles?.length&&s.media_url)?`<img class="mini-chart-img" src="${esc(s.media_url)}">`:''}<div class="riskbox ${dir==='short'?'short':'long'}"><i class="reward"></i><i class="risk"></i><b class="entry-line"></b></div><span class="mini-loading">${s._candles?.length?'':'chart snapshot / no local candles'}</span></div><div class="radar-metrics"><div><label>to entry</label>${distSigned==null?'<strong class="secondary">—</strong>':`<strong class="${distSigned>=0?'success':'danger'}">${pct(distSigned)}</strong>`}</div><div><label>entry</label><strong>${fmt(entry,6)}</strong></div><div><label>SL</label><strong>${fmt(sl,6)}</strong></div><div><label>TP1</label><strong>${fmt(tp,6)}</strong></div><div><label>R:R</label><strong>${rr?fmt(rr,2):'—'}</strong></div><div><label>TF</label><strong>${esc(s.timeframe||'1m')}</strong></div><div><label>age</label><strong>${fmt(ageDays,1)}d</strong></div></div></div>`}
// Phase 2 (2026-05-29): LEGACY / UNUSED. The standalone Entry-Radar tab was
// folded into the unified Signals & Radar tab (renderUnifiedPage card view).
// This function targets #entry-radar / #radar-filter / #radar-age, which no
// longer exist in index.html, and is no longer wired to any event or the tab
// router (render() routes the 'radar' tab to renderUnifiedPage). Kept intact
// for rollback and as a reference for the /api/entry-radar endpoint contract.
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
  const maxAge=Z($('#radar-age')?.value);
  if(maxAge)rows=rows.filter(s=>(Date.now()-new Date(s.signal_timestamp||s.created_at))/86400000<=maxAge);
  rows.sort((a,b)=>signalDistance(a)-signalDistance(b));
  $('#entry-radar').innerHTML=rows.map(radarCard).join('')||`<div class="empty">No actionable waiting entries. Filtered out ${payload.excluded_count||0} calls that already hit entry / SL / TP.</div>`;
  const info=document.createElement('div'); info.className='radar-summary'; info.textContent=`${rows.length} actionable · ${payload.excluded_count||0} already triggered/expired removed`;
  $('#entry-radar').prepend(info);
  wireRows(); rows.forEach((s,i)=>drawMiniRadar(`mini-radar-${i}`,s.mini_candles||s._candles||[],s));
}
function drawMiniRadar(id,candles,s){const el=document.getElementById(id);if(!el||!candles.length||!window.echarts)return;const loading=el.querySelector('.mini-loading'); if(loading)loading.remove();const c=echarts.init(el);const labels=candles.map(x=>String(x.timestamp).slice(11,16));const data=candles.map(x=>[Z(x.open),Z(x.close),Z(x.low),Z(x.high)]);const levels=s.levels||{};const entry=Z(s.entry_price||levels.entry), sl=Z(s.stop_loss||levels.stop_loss), tp=Z(s.take_profit_1||levels.take_profit_1);const lines=[];if(entry)lines.push({yAxis:entry,name:'E',lineStyle:{color:'#fc72ff',width:1}});if(sl)lines.push({yAxis:sl,name:'SL',lineStyle:{color:'#ff5f72',width:1,type:'dashed'}});if(tp)lines.push({yAxis:tp,name:'TP',lineStyle:{color:'#35d07f',width:1,type:'dashed'}});c.setOption({animation:false,grid:{left:0,right:0,top:4,bottom:0},xAxis:{type:'category',data:labels,show:false},yAxis:{scale:true,show:false},series:[{type:'candlestick',data,itemStyle:{color:'#35d07f',color0:'#ff5f72',borderColor:'#35d07f',borderColor0:'#ff5f72'},markLine:{symbol:'none',label:{show:false},data:lines}}]})}

/* ─── Signal intelligence drawer (standalone call) ─── */
function levelCards(levels){
  return ['entry','stop_loss','take_profit_1','take_profit_2','take_profit_3','take_profit_4','take_profit_5','take_profit_6']
    .filter(k=>{const v=Number(levels?.[k]);return Number.isFinite(v)&&v>0})
    .map(k=>`<div class="level"><label>${k.replaceAll('_',' ')}</label><strong>${fmt(levels?.[k],6)}</strong></div>`).join('')
    ||'<div class="empty">No levels extracted from this chart</div>'
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
function replayLegLabel(leg){
  const dir=String(leg.direction||'').toUpperCase()||'?';
  const src=leg.source==='chart_hacker'?'CH':'TR';
  const entry=leg.levels?.entry;
  let label=`${src} ${dir} @ ${entry?fmt(entry,2):'—'}`;
  if(!leg.armed)label+=' (unarmed)';
  else if(leg.chart_hacker_endorsed)label+=' ✓';
  return label;
}
function mergeReplayLeg(r,legIdx){
  const legs=r?.legs||[];
  const leg=legs[legIdx];
  if(!leg)return r;
  const pos=leg.position?{...leg.position,id:leg.position_id||leg.position.id,signal_source:leg.source,chart_hacker_endorsed:leg.chart_hacker_endorsed}: {};
  return {...r,_selectedLeg:legIdx,symbol:leg.symbol||r.symbol,exchange:leg.exchange||r.exchange,timeframe:leg.timeframe||r.timeframe,candles:leg.candles||[],coverage:leg.coverage||r.coverage,position:pos,signal:{...(r.signal||{}),direction:leg.direction,levels:leg.levels||{},signal_source:leg.source}};
}
function replayLegTabsHtml(base,activeIdx){
  const legs=base.legs||[];
  if(legs.length<=1)return '';
  const st=base.stats||{};
  const statLine=`<p class="replay-leg-stats">${st.legs_armed||0} armed · ${st.legs_total||legs.length} legs · ${st.trader_trades_extracted||0} trader · ${st.chart_hacker_trades_extracted||0} CH extracted</p>`;
  const tabs=legs.map((leg,i)=>{
    const cls=['replay-leg-tab'];
    if(i===activeIdx)cls.push('active');
    if(!leg.armed)cls.push('unarmed');
    if(leg.chart_hacker_endorsed)cls.push('endorsed');
    return `<button type="button" class="${cls.join(' ')}" data-replay-leg="${i}">${esc(replayLegLabel(leg))}</button>`;
  }).join('');
  return `${statLine}<div class="replay-legs">${tabs}</div>`;
}
function wireReplayLegTabs(base,interpId,onSelect){
  const render=onSelect||((payload)=>drawReplay(payload));
  document.querySelectorAll('[data-replay-leg]').forEach(btn=>{
    btn.addEventListener('click',async()=>{
      const idx=parseInt(btn.getAttribute('data-replay-leg'),10);
      if(!Number.isFinite(idx))return;
      if(base.legs&&base.legs[idx]?.candles?.length){render({...base,selected_leg:idx});return}
      try{const fresh=await api(`/api/signal-replay?id=${encodeURIComponent(interpId)}&leg=${idx}`);render(fresh)}catch(e){console.error('leg switch failed',e)}
    });
  });
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
function drawReplay(raw){
  if(!raw||!raw.ok){$('#drawer-body').innerHTML='<div class="empty">No replay data available</div>';return}
  const base=raw;
  const legIdx=raw.selected_leg??raw._selectedLeg??0;
  const r=mergeReplayLeg(base,legIdx);
  const sig=r.signal||{}, pos=r.position||{}, levels=sig.levels||{}, trader=r.trader||{}, news=r.news||{};
  const legTabs=replayLegTabsHtml(base,legIdx);
  $('#drawer-title').textContent=`${r.symbol} ${sig.direction||''}`;
  const src=String(pos.signal_source||sig.signal_source||'').toLowerCase();
  const endorsed=!!pos.chart_hacker_endorsed;
  const srcTag=src==='chart_hacker'?' · AI INFERRED (ChartHacker)':(src==='trader'?(endorsed?' · TRADER CALL · CH ENDORSED':' · TRADER CALL'):'');
  $('#drawer-kicker').textContent=`CALL DETAIL | INTERP ID: ${r.id}${pos.id ? ` · POS ID: ${pos.id}` : ''}${srcTag}`;
  const traderName=trader.handle_raw||trader.display_name||trader.handle_normalized||news.author||'trader';
  const outcome=pos.outcome||pos.status||'tracking';
  const pnl=Z(pos.realized_pnl_usd_final??pos.realized_pnl_usd??pos.unrealized_pnl_usd);
  const media=r.media_url||r.annotated_chart_url;
  $('#drawer-body').innerHTML=`
    <div class="replay-layout">
      ${legTabs}
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
  wireReplayLegTabs(base,r.id);
  $('#replay-play')?.addEventListener('click',()=>animateReplay(r));
  $('#replay-reset')?.addEventListener('click',()=>renderReplayChart(r));
  $('#replay-spacing')?.addEventListener('click',()=>{
    state.timeSpacing=state.timeSpacing==='realtime'?'contiguous':'realtime';
    try{localStorage.setItem('tickles.replay.spacing',state.timeSpacing)}catch(e){}
    const btn=$('#replay-spacing'); if(btn) btn.textContent=`Spacing: ${state.timeSpacing==='realtime'?'Time':'Bars'}`;
    renderReplayChart(r);
  });
}
function candleSeries(candles){return (candles||[]).map(c=>[c.timestamp,Z(c.open),Z(c.close),Z(c.low),Z(c.high)])}
function markLines(levels){
  const data=[]; const add=(name,val,color)=>{if(Z(val))data.push({yAxis:Z(val),name,lineStyle:{color,type:'dashed',width:1.4},label:{formatter:name,color}})};
  add('ENTRY',levels?.entry,'#fc72ff'); add('SL',levels?.stop_loss,'#ff5f72');
  ['take_profit_1','take_profit_2','take_profit_3','take_profit_4','take_profit_5','take_profit_6'].forEach((k,i)=>add(`TP${i+1}`,levels?.[k],'#35d07f'));
  return data;
}
function renderReplayChart(r,chartId='replay-chart',upto=null){
  try {
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
  let lvlVals=[];
  try{lvlVals=Object.values(levels).map(n).filter(x=>x>0)}catch(e){lvlVals=[]}
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
    const lo=Z(c.low),hi=Z(c.high),op=Z(c.open),cl=Z(c.close);
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
  } catch (e) { console.error("renderReplayChart blocked by SES:", e); }
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
async function openTrader(actor){openDrawer(actor,'TRADER INTEL','<div class="empty">Loading trader intelligence…</div>');try{const d=await api(`/api/trader-drill?trader_id=${encodeURIComponent(actor)}`);renderTraderDrawer(d,actor)}catch(e){console.error('trader drill failed',e);$('#drawer-body').innerHTML=`<div class="empty">Failed to load trader data: ${esc(e?.message||e)}</div>`}}

/* ─── Trader drawer — stats + Active/History tabs + AI learnings ─── */
function renderTraderDrawer(d,actor){
  const s=d.stats||{};
  const pnlOk=Z(s.total_pnl)>=0;
  let html='';

  // ── Stats summary grid ──
  html+=`<div class="trader-stats-grid">`;
  html+=`<div><label>Total Trades</label><strong>${s.total_trades||0}</strong></div>`;
  html+=`<div><label>Wins</label><strong class="success">${s.wins||0}</strong></div>`;
  html+=`<div><label>Losses</label><strong class="danger">${s.losses||0}</strong></div>`;
  html+=`<div><label>Win Rate</label><strong>${fmt(s.win_rate,1)}%</strong></div>`;
  html+=`<div><label>Total P&L</label><strong class="${pnlOk?'success':'danger'}">${usd(s.total_pnl)}</strong></div>`;
  html+=`<div><label>Avg Win</label><strong class="success">${usd(s.avg_win)}</strong></div>`;
  html+=`<div><label>Avg Loss</label><strong class="danger">${usd(s.avg_loss)}</strong></div>`;
  html+=`<div><label>Best Trade</label><strong class="success">${usd(s.best_trade)}</strong></div>`;
  html+=`<div><label>Worst Trade</label><strong class="danger">${usd(s.worst_trade)}</strong></div>`;
  html+=`<div><label>Unique Coins</label><strong>${s.unique_coins||0}</strong></div>`;
  html+=`<div><label>Closed</label><strong>${s.closed_trades||0}</strong></div>`;
  html+=`<div><label>Breakeven</label><strong>${s.breakeven||0}</strong></div>`;
  html+=`</div>`;

  // ── Most traded / Most profitable coins ──
  if((s.most_traded||[]).length||(s.most_profitable||[]).length){
    html+=`<div class="trader-coins-grid">`;
    html+=`<div><h3>Most Traded</h3><div class="coin-list">${(s.most_traded||[]).map(c=>`<span class="coin-chip ${Z(c.pnl)>=0?'win':'loss'}">${esc(dispSymbol(c.symbol))} <em>${c.trades}t · ${c.wins}w · ${usd(c.pnl)}</em></span>`).join('')||'<span class="secondary">—</span>'}</div></div>`;
    html+=`<div><h3>Most Profitable</h3><div class="coin-list">${(s.most_profitable||[]).map(c=>`<span class="coin-chip ${Z(c.pnl)>=0?'win':'loss'}">${esc(dispSymbol(c.symbol))} <em>${usd(c.pnl)} · ${c.win_rate}%</em></span>`).join('')||'<span class="secondary">—</span>'}</div></div>`;
    html+=`</div>`;
  }

  // ── Trade tabs ──
  const hasActive=(d.trades_active||[]).length>0;
  const hasHistory=(d.trades_history||[]).length>0;
  if(hasActive||hasHistory){
    html+=`<div class="trader-trade-tabs" id="trader-trade-tabs">`;
    html+=`<button class="comp-dtab active" data-ttab="active">Active (${d.trades_active.length})</button>`;
    html+=`<button class="comp-dtab" data-ttab="history">History (${d.trades_history.length})</button>`;
    html+=`</div>`;
    html+=`<div class="trader-trades-panel" id="trader-trades-panel"></div>`;
  }

  // ── AI Learnings ──
  const learnings=d.ai_learnings||[];
  if(learnings.length){
    html+=`<div class="trader-learnings"><h3>AI Learnings <span class="secondary">(${learnings.length})</span></h3>`;
    html+=learnings.slice(0,30).map(l=>{
      if(l.source==='postmortem'){
        return `<div class="lesson"><div class="lesson-kind">POSTMORTEM · ${esc(dispSymbol(l.symbol))} · ${esc(l.direction||'')}</div><div class="lesson-body">${esc(l.lesson||l.what_happened||'')}</div>${l.why_it_worked?`<div class="lesson-body"><b>Why it worked:</b> ${esc(l.why_it_worked)}</div>`:''}${l.why_it_failed?`<div class="lesson-body"><b>Why it failed:</b> ${esc(l.why_it_failed)}</div>`:''}<div class="lesson-foot">${rel(l.created_at)}</div></div>`;
      }
      return `<div class="lesson"><div class="lesson-kind">${esc(l.persona_name||l.mode||'AI')} · ${esc(l.verdict||'')}</div><div class="lesson-body">${esc(l.rationale||'')}</div><div class="lesson-foot">${rel(l.decided_at)}</div></div>`;
    }).join('');
    html+=`</div>`;
  }

  $('#drawer-body').innerHTML=html;

  // ── Wire trade tabs ──
  const activeData=d.trades_active||[];
  const historyData=d.trades_history||[];
  function _renderTrades(rows){
    if(!rows.length) return '<div class="empty">No trades</div>';
    return `<div class="table-wrap"><table class="data-table"><thead><tr><th>Symbol</th><th>Direction</th><th>Entry</th><th>Exit</th><th>Status</th><th class="num">P&L</th><th>Outcome</th><th>Opened</th></tr></thead><tbody>${rows.map(r=>{
      const sym=dispSymbol(r.instrument_symbol);
      const dir=r.direction||'';
      const entry=Z(r.entry_price);
      const exit=Z(r.exit_price);
      const status=r.status||'';
      const pnl=Z(r.realized_pnl_usd||r.unrealized_pnl_usd);
      const outcome=r.outcome||'';
      const opened=r.signal_timestamp||r.created_at||'';
      return `<tr><td>${esc(sym)}</td><td>${dirPill(dir)}</td><td>${fmt(entry,4)}</td><td>${exit?fmt(exit,4):'—'}</td><td>${pill(status,status)}</td><td class="num ${pnl>=0?'success':'danger'}">${pnl!==0?usd(pnl):'—'}</td><td>${esc(outcome||status)}</td><td>${fmtDate(opened)}</td></tr>`;
    }).join('')}</tbody></table></div>`;
  }
  const panel=$('#trader-trades-panel');
  if(panel){
    const defaultTab=hasActive?'active':'history';
    panel.innerHTML=_renderTrades(defaultTab==='active'?activeData:historyData);
    $$('#trader-trade-tabs .comp-dtab').forEach(b=>b.onclick=()=>{
      $$('#trader-trade-tabs .comp-dtab').forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
      panel.innerHTML=_renderTrades(b.dataset.ttab==='active'?activeData:historyData);
    });
  }
}

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
function perfMap(){const m={};(state.agentPerf?.agent_performance||[]).forEach(p=>{m[p.actor_id]=p;if(p.handle_normalized)m[p.handle_normalized]=p;if(p.display_name)m[p.display_name]=p});return m}
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
  try{const c=echarts.init(el);state.charts[id]=c;setTimeout(()=>c.resize(),50);return c}catch(e){console.error('echarts init blocked by SES:', e);return null}}

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
        const t=String(arr[0].axisValue||'');
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
  // Phase 2 (2026-05-29): DEAD WIRING REMOVED. The standalone Radar tab
  // (#entry-radar / #radar-filter / #radar-age DOM) was merged into the
  // unified Signals & Radar tab (card view) months ago; those element ids no
  // longer exist in index.html, so this wiring was a permanent no-op pointing
  // at the legacy renderRadarPage(). Left commented for rollback reference.
  // ['radar-filter','radar-age'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderRadarPage()});
  ['signals-filter','signals-status'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderSignalsPage()});
  const uf=$('#unified-filter'); if(uf) uf.oninput=()=>renderUnifiedPage();
  const ust=$('#unified-status'); if(ust) ust.onchange=()=>renderUnifiedPage();
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
  ['news-filter','news-media'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderNewsPage(true)});
  ['telegram-filter','telegram-media','telegram-channel'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderTelegramPage(true)});
  // Settings sub-tabs
  $$('#tab-settings .subtab').forEach(b=>b.onclick=()=>{
    $$('#tab-settings .subtab').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    $$('#tab-settings .subtab-panel').forEach(p=>p.classList.remove('active'));
    const panel=document.getElementById('subtab-'+b.dataset.subtab);
    if(panel){panel.classList.add('active');
      if(b.dataset.subtab==='prompts')loadPromptsTab();
    }
  });
  $('#btn-new-prompt-prompts')?.addEventListener('click',()=>openPromptModal('new'));
}

async function loadPromptsTab(){
  try{
    const data=await api('/api/settings/prompts/versions',{skipCompany:true});
    const rows=data.versions||[];
    const tbody=$('#prompts-tbody');
    if(!tbody)return;
    if(!rows.length){tbody.innerHTML='<tr><td colspan="6" class="empty">No prompts found</td></tr>';return;}
    tbody.innerHTML=rows.map(p=>`<tr>
      <td class="primary">${esc(p.name)}</td>
      <td class="mono">${esc(p.version)}</td>
      <td><span class="badge ${p.source==='db'?'badge-pro':'neutral'}">${esc(p.source)}</span></td>
      <td class="secondary">${p.times_used||0}</td>
      <td class="mono small">${esc(p.prompt_hash||'—')}</td>
      <td><button class="pill-btn" data-prompt-version="${esc(p.version)}" style="padding:4px 12px;font-size:11px">Edit</button></td>
    </tr>`).join('');
    tbody.querySelectorAll('[data-prompt-version]').forEach(btn=>btn.onclick=()=>openPromptModal(btn.dataset.promptVersion));
  }catch(e){console.error('loadPromptsTab',e);}
}
  window.addEventListener('DOMContentLoaded',()=>{loadDrawerWidth();wire();attachDrawerResizer();load();state.timer=setInterval(()=>{const ts=new Date();$('#updated-at').textContent=`${ts.toLocaleTimeString()}`; pulseUpdate()},5000)});
async function pulseUpdate(){
  try{
    // Lightweight data refresh — no DOM rebuild
    if(['competition','floor'].includes(state.tab)){
      state.competitions=await api('/api/competitions');
      updateCompNumbers();
    }
    if(['positions'].includes(state.tab)){
      // Only re-render if not in historic sub-tab (historic is manual pagination)
      if(_positionsState.sub==='live') await renderPositionsLive();
    }
    if(['floor','radar','signals'].includes(state.tab)||!state.snap){
      state.snap=await api('/api/snapshot');
      if(state.tab==='floor'){renderStats();renderCompetitionMini();renderFloorPositions();if((state._floorTick=(state._floorTick||0)+1)%4===0)renderFloorSignals();}
      if(state.tab==='unified'||state.tab==='radar'||state.tab==='signals')renderUnifiedPage();
    }
    // Refresh expanded agent if present
    if(state.expandedAgent&&state._agentCache){
      try{
        const d=await api('/api/competition-agent?agent='+encodeURIComponent(state.expandedAgent));
        state._agentCache={agentId:state.expandedAgent,data:d};
        renderAgentInline(state.expandedAgent,d);
      }catch(e){}
    }
  }catch(e){/* silent pulse failure */}
}
function updateCompNumbers(){
  const c=state.competitions?.competitions?.[0];
  if(!c)return;
  (c.participants||[]).forEach(p=>{
    const tr=document.getElementById('comp-tr-'+p.agent_id);
    if(!tr)return;
    const m=compMetrics(p);
    const set=(col,html)=>{const el=tr.querySelector(`td[data-col="${col}"]`);if(el)el.innerHTML=html};
    set('balance','$'+fmt(m.eq,2));
    set('liveEq','<strong>$'+fmt(m.liveEq,2)+'</strong>');
    set('pnl',usd(m.pnl)+'<br><span class="secondary small">unreal '+usd(m.unreal)+'</span>');
    set('return',`<span class="${m.rpct>=0?'success':'danger'}">${pct(m.rpct)}</span><br><span class="secondary small">${pct(m.liveReturn)} live</span>`);
    const wr=tr.querySelector('td[data-col="winRate"]');
    if(wr)wr.textContent=fmt(m.winRate*100,1)+'%';
    set('wl',compWlCell(m.wins,m.losses));
    const op=tr.querySelector('td[data-col="open"]');
    if(op)op.textContent=String(m.open);
  });
  $('#updated-at').textContent=new Date().toLocaleTimeString();
}


/* ─── Strategies tab — the graded technique/edge library ─── */
function renderStrategiesPage(){
  const data=state.strategies||{strategies:[]};
  const list=data.strategies||[];
  const q=($('#strat-filter')?.value||'').toLowerCase();
  const rows=list.filter(s=>{
    if(!q)return true;
    return s.technique.includes(q)||(s.description||'').toLowerCase().includes(q)
      ||(s.symbols||[]).some(x=>x.toLowerCase().includes(q))
      ||(s.traders||[]).some(t=>(t.trader||'').toLowerCase().includes(q));
  });
  const proven=list.filter(s=>s.maturity==='proven').length;
  const est=list.filter(s=>s.maturity==='established').length;
  const best=list.filter(s=>s.sample_count>=3).sort((a,b)=>b.win_rate-a.win_rate)[0];
  const stats=$('#strat-stats');
  if(stats)stats.innerHTML=
    '<div class="stat"><label>Strategies</label><strong>'+list.length+'</strong><span>'+proven+' proven · '+est+' established</span></div>'+
    '<div class="stat"><label>Best (n≥3)</label><strong>'+(best?esc(best.technique):'—')+'</strong><span>'+(best?fmt(best.win_rate*100,0)+'% over '+best.sample_count+' trades':'need more closed trades')+'</span></div>'+
    '<div class="stat"><label>Total graded</label><strong>'+list.reduce((a,s)=>a+s.sample_count,0)+'</strong><span>technique-trade outcomes</span></div>';
  const mBadge=m=>({proven:'<span class="pill long">PROVEN</span>',established:'<span class="pill">ESTABLISHED</span>',emerging:'<span class="pill short">EMERGING</span>',candidate:'<span class="pill" style="opacity:.5">CANDIDATE</span>'}[m]||'');
  const html=rows.map(s=>{
    const wr=fmt(s.win_rate*100,0)+'%';
    const wrCls=s.win_rate>=0.55?'success':(s.win_rate<=0.4?'danger':'');
    const pnlCls=s.total_pnl_usd>=0?'success':'danger';
    const traders=(s.traders||[]).map(t=>'<span class="pill" title="'+esc(t.trader)+': '+t.wins+'W/'+t.losses+'L'+(t.symbol?' on '+esc(t.symbol):'')+'">'+esc(t.trader)+' '+fmt(t.win_rate*100,0)+'%</span>').join(' ');
    return '<tr>'+
      '<td><div class="row-main"><strong>'+esc(s.technique)+'</strong> '+mBadge(s.maturity)+'</div>'+
      '<div class="secondary small" style="max-width:420px">'+esc(s.description||'(no description yet — auto-described as samples accrue)')+'</div></td>'+
      '<td class="num"><strong class="'+wrCls+'">'+wr+'</strong><div class="secondary small">'+s.wins+'W / '+s.losses+'L</div></td>'+
      '<td class="num mono">'+(s.avg_rr?fmt(s.avg_rr,2):'—')+'</td>'+
      '<td class="num mono"><span class="success">+'+fmt(s.avg_win_pct,1)+'%</span> / <span class="danger">-'+fmt(s.avg_loss_pct,1)+'%</span></td>'+
      '<td class="num mono '+pnlCls+'">'+(s.total_pnl_usd>=0?'+':'')+'$'+fmt(Math.abs(s.total_pnl_usd),2)+'</td>'+
      '<td class="num">'+s.sample_count+'</td>'+
      '<td>'+((s.symbols||[]).slice(0,5).map(x=>'<span class="pill">'+esc(x)+'</span>').join(' ')||'<span class="secondary">—</span>')+'</td>'+
      '<td>'+(traders||'<span class="secondary">—</span>')+'</td>'+
    '</tr>';
  }).join('');
  $('#strategies-body').innerHTML=rows.length
    ?'<table class="data-table"><thead><tr><th>Strategy / Technique</th><th class="num">Win rate</th><th class="num">Avg RR</th><th class="num">Avg win/loss</th><th class="num">P&L</th><th class="num">Trades</th><th>Coins</th><th>Traders</th></tr></thead><tbody>'+html+'</tbody></table>'
    :'<div class="empty">No graded techniques yet — they appear as tracked positions close.</div>';
  const f=$('#strat-filter');
  if(f&&!f._wired){f._wired=true;f.addEventListener('input',()=>renderStrategiesPage());}
}
