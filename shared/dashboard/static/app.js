(()=>{
/* Tickles v5 — competition inline expand, chart tabs in Discord drawer, trader usernames, compact rows. */
console.log('Tickles Dashboard v5 loading');
const state={tab:'floor',company:'all',snap:null,competitions:null,learning:null,news:null,agentPerf:null,sort:{},charts:{},timer:null,expandedAgent:null};
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

async function api(path,opt={}){const u=new URL(path.replace(/^\//,''),document.baseURI);if(!opt.skipCompany&&state.company!=='all')u.searchParams.set('company',state.company);const r=await fetch(u);const j=await r.json();if(!r.ok)throw j;return j}
async function load(){try{const needsSnap=['floor','radar','signals','positions','traders','ops'].includes(state.tab)||!state.snap;if(needsSnap)state.snap=await api('/api/snapshot');if(state.tab==='competition'||state.tab==='floor')state.competitions=await api('/api/competitions');if(state.tab==='learning'||state.tab==='floor'||state.tab==='traders')state.learning=await fetchLearning();if(state.tab==='news'||state.tab==='floor')state.news=await api('/api/news/feed?window=30d&limit=120');if(state.tab==='traders'||state.tab==='floor')state.agentPerf=await api('/api/agent-performance');render();$('#updated-at').textContent='updated now';$('#status-text').textContent='live'}catch(e){console.error(e);$('#status-text').textContent='API issue'}}
async function fetchLearning(){const [skill,feed,brain]=await Promise.all([api('/api/learning/skill-summary?window=1m'),api('/api/learning/memory-feed?window=1m'),api('/api/learning/agent-brain?window=1m')]);return{skill,feed,brain}}
function switchTab(tab){state.tab=tab;state.expandedAgent=null;state._agentCache=null;$$('.nav-link').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));$$('.tab').forEach(t=>t.classList.toggle('active',t.id===`tab-${tab}`));const names={floor:['TRADING COMPANY','Trading Floor'],radar:['LIVE ENTRY WATCH','Entry Radar'],competition:['CONTEST MODE','Competition'],signals:['ENTRY WATCH','Signals Watch'],positions:['RISK MONITOR','Positions'],traders:['DISCORD ALPHA','Trader Intel'],news:['SOCIAL TAPE','Discord Feed'],learning:['MEMORY + SKILL','AI Learning'],ops:['RUN COST','Ops & Cost']};$('#eyebrow').textContent=names[tab][0];$('#page-title').textContent=names[tab][1];load()}
function render(){renderStats();if(state.tab==='floor')renderFloor();if(state.tab==='radar')renderRadarPage();if(state.tab==='competition')renderCompetition();if(state.tab==='signals')renderSignalsPage();if(state.tab==='positions')renderPositionsPage();if(state.tab==='traders')renderTradersPage();if(state.tab==='news')renderNewsPage();if(state.tab==='learning')renderLearningPage();if(state.tab==='ops')renderOpsPage()}
function renderStats(){const s=state.snap||{};const pnl=n(s.open_positions_unrealized_pnl);$('#stat-pnl').textContent=usd(pnl);$('#stat-pnl').className=pnl>=0?'success':'danger';$('#stat-open-count').textContent=`${s.open_positions_count||0} open positions`;$('#stat-signals').textContent=s.signals_today_count||0;$('#stat-ingest').textContent=`${s.ingest_depth||0} ingest depth`;$('#stat-top').textContent=s.top_actor_name||'—';$('#stat-edge').textContent=s.top_actor_score?`edge ${fmt(s.top_actor_score,3)}`:'edge —';$('#stat-cost').textContent=`$${fmt(s.api_cost_today_usd,4)}`;$('#stat-budget').textContent=`$${fmt(s.budget_remaining_usd||s.budget_limit_usd||100,0)} remaining`}

/* ─── row helpers ─── */
function rowMain(sym,sub){return`<div class="main-cell"><div class="token-dot">${esc(initials(sym))}</div><div><div class="primary">${esc(sym)}</div><div class="secondary">${esc(sub||'')}</div></div></div>`}

/* ─── competition — INLINE expand, no drawer ─── */
async function renderCompetition(){
  const c=state.competitions?.competitions?.[0];
  if(!c){$('#competition-body').innerHTML='<div class="empty">No active competition</div>';return}
  const rows=(c.participants||[]).sort((a,b)=>(a.rank||99)-(b.rank||99)).map(p=>{
    const eq=n(p.scores?.equity), rpct=n(p.scores?.return_pct);
    return `<tr class="comp-row" data-agent="${esc(p.agent_id)}" id="comp-tr-${esc(p.agent_id)}">
      <td class="num rank">#${p.rank}</td>
      <td>${rowMain(p.agent_id,p.metadata?.description||p.strategy_ref||'')}</td>
      <td>${esc(p.strategy_ref||'—')}</td>
      <td class="num mono">$${fmt(eq,2)}</td>
      <td class="num">${usd(p.scores?.total_realized_pnl_usd)}<br><span class="secondary small">unreal ${usd(p.scores?.unrealized_pnl_usd)}</span></td>
      <td class="num ${rpct>=0?'success':'danger'}">${pct(rpct)}</td>
      <td class="num">${fmt(n(p.scores?.win_rate)*100,1)}%</td>
      <td class="num">${p.scores?.total_trades||0}</td>
      <td class="num">${p.scores?.open_positions||0}</td>
    </tr>`;
  });
  $('#competition-body').innerHTML=`<table class="data-table comp-table"><thead><tr>
    <th class="num">Rank</th><th>Agent / description</th><th>Strategy</th>
    <th class="num">Equity</th><th class="num">P&L</th><th class="num">Return</th>
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
  if(fromCache&&state._agentCache&&state._agentCache.agentId===agentId){
    renderAgentInline(agentId,state._agentCache.data);
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

  const html=`<div class="comp-detail">
    <div class="comp-detail-tabs">
      <button class="comp-dtab active" data-ctab="live">Live · ${open.length}</button>
      <button class="comp-dtab" data-ctab="history">History · ${hist.length}</button>
    </div>
    <div class="comp-stats">
      <div><label>Equity</label><strong>$${fmt(a.equity_usd,2)}</strong></div>
      <div><label>Realized P&L</label><strong class="${n(a.realized_pnl_usd)>=0?'success':'danger'}">${usd(a.realized_pnl_usd)}</strong></div>
      <div><label>Unrealized</label><strong class="${n(a.unrealized_pnl_usd)>=0?'success':'danger'}">${usd(a.unrealized_pnl_usd)}</strong></div>
      <div><label>Return</label><strong>${fmt(a.return_pct,1)}%</strong></div>
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
  rows=filterRows(rows,$('#news-filter')?.value,['author','content','headline','channel_name']);
  if($('#news-media')?.value==='media')rows=rows.filter(r=>r.has_media);
  $('#news-list').innerHTML=rows.map(newsMsg).join('')||'<div class="empty">No messages</div>';
  $$('[data-newsrow]').forEach(el=>el.onclick=()=>openDiscordDrawer(el.dataset.newsrow));
}

/* ─── Landing pages ─── */
function renderSignalsPage(){let rows=state.snap?.signals||[];rows=filterRows(rows,$('#signals-filter')?.value,['instrument_symbol','trader_display_name','trader_handle_normalized','status','raw_signal_text']);const st=$('#signals-status')?.value;if(st)rows=rows.filter(r=>r.status===st);renderSignalsTable(rows)}
function renderPositionsPage(){let rows=state.snap?.positions||[];rows=filterRows(rows,$('#positions-filter')?.value,['instrument_symbol','actor_display','actor_handle','status','signal_source']);const st=$('#positions-status')?.value;if(st)rows=rows.filter(r=>r.status===st);renderPositionsTable(rows)}
function renderLearningPage(){const l=state.learning||{feed:{rows:[]},skill:{rows:[]},brain:{rows:[]}};$('#learn-total').textContent=l.feed.rows.length;$('#learn-actors').textContent=l.skill.rows.length;$('#learn-brains').textContent=l.brain.rows.length;$('#learn-focus').textContent=(l.feed.rows[0]?.raw&&safeJson(l.feed.rows[0].raw)?.instrument_symbol)||'execution';$('#learning-lessons').innerHTML=l.feed.rows.map(r=>`<div class="lesson"><div class="lesson-kind">${esc(r.source_kind)} · ${esc(r.actor)}</div><div class="lesson-body">${esc(r.body)}</div><div class="lesson-foot">${esc(r.company)} · ${rel(r.ts)}</div></div>`).join('');const body=l.skill.rows.map(r=>`<tr><td>${rowMain(r.display_name||r.actor_id,r.actor_id)}</td><td class="num">${r.skill_pct??'—'}%</td><td class="num">${r.trade_count||0}</td></tr>`).join('');$('#learning-models').innerHTML=`<table class="data-table"><thead><tr><th>Actor</th><th class="num">Skill</th><th class="num">Trades</th></tr></thead><tbody>${body||'<tr><td colspan="3" class="empty">No data</td></tr>'}</tbody></table>`}

/* ─── misc renderers ─── */
function renderFloor(){const s=state.snap||{};const sigs=filterRows(s.signals||[],$('#floor-signal-filter')?.value,['instrument_symbol','trader_display_name','trader_handle_normalized','status']);table('#floor-signals',[{label:'Signal'},{label:'Side'},{label:'Entry',num:1},{label:'SL',num:1},{label:'TP1',num:1},{label:'Δ entry',num:1},{label:'Status'},{label:'Call'}],[sigRows(sigs,10)]);const live_positions=(s.positions||[]).filter(r=>r.status==='open'||r.status==='partial_exit'||r.status==='active');const ps=filterRows(live_positions,$('#floor-position-filter')?.value,['instrument_symbol','actor_display','actor_handle','status']);table('#floor-positions',[{label:'Position'},{label:'Side'},{label:'Status'},{label:'Entry',num:1},{label:'Now',num:1},{label:'P&L',num:1},{label:'P&L %',num:1},{label:'Notional',num:1},{label:'Source'}],[posRows(ps,10)]);renderCompetitionMini();renderNewsMini();wireRows()}
function renderCompetitionMini(){const c=state.competitions?.competitions?.[0];if(!c){$('#floor-competition').innerHTML='<div class="empty">No competition</div>';return}const ps=(c.participants||[]).sort((a,b)=>(a.rank||99)-(b.rank||99)).slice(0,6);$('#floor-competition').innerHTML=`<div class="competition-card"><div class="secondary">${esc(c.name)} · ${esc(c.status)}</div><div class="leader-mini">${ps.map(p=>`<div class="leader-row"><div class="rank-badge">#${p.rank}</div><div><div class="primary">${esc(p.agent_id)}</div><div class="secondary">${esc(p.strategy_ref||'')}</div></div><div class="num ${n(p.scores?.total_realized_pnl_usd)>=0?'success':'danger'}">${usd(p.scores?.total_realized_pnl_usd)}</div></div>`).join('')}</div></div>`}
function renderNewsMini(){const rows=state.news?.rows||[];$('#floor-news').innerHTML=rows.slice(0,8).map(newsMsg).join('')||'<div class="empty">No Discord messages</div>'}
function newsMsg(r){return`<div class="discord-msg" data-newsrow="${r.id}"><div class="discord-avatar">${initials(r.author)}</div><div><div class="discord-head"><span class="discord-user">${esc(r.author||'unknown')}</span><span class="discord-time">${rel(r.published_at||r.collected_at)}</span></div><div class="discord-text">${esc(r.content||r.headline||'')}</div><div class="discord-meta">${r.has_media?pill(`${r.media_count||1} chart${(r.media_count||1)>1?'s':''}`,'pending'):''}${(r.instruments||[]).map(x=>pill(x)).join('')}</div></div></div>`}
// 2026-05-22 — Δ entry "honesty" fix. The previous formula rendered missing
// data (current_price=null AND distance_to_entry_pct=null) as "+0.00%" in
// the success/green class, which the user reads as "exactly at entry, in
// your favour" — wrong. Now we detect the no-data case explicitly and
// render '—' in the muted .secondary class. Real zero stays "+0.00%" green.
function sigRows(rows,limit){return rows.slice(0,limit||rows.length).map(s=>{const dir=s.consensus_direction||s.direction;const trader=traderName(s);const liveRaw=s.current_price;const entryRaw=s.entry_price??s.levels?.entry;const distRaw=s.distance_to_entry_pct;const hasLive=liveRaw!=null&&Number.isFinite(Number(liveRaw))&&Number(liveRaw)>0;const hasEntry=entryRaw!=null&&Number.isFinite(Number(entryRaw))&&Number(entryRaw)>0;const hasStoredDist=distRaw!=null&&Number.isFinite(Number(distRaw))&&Number(distRaw)!==0;const computedDist=hasLive&&hasEntry?((Number(liveRaw)-Number(entryRaw))/Number(entryRaw)*100):null;const dist=computedDist!=null?computedDist:(hasStoredDist?Number(distRaw):null);const distCell=dist==null?'<span class="secondary">—</span>':`<span class="${dist>=0?'success':'danger'}">${pct(dist)}</span>`;return`<tr data-call="${esc(s.signal_interpretation_id||s.id)}" data-news="${esc(s.news_item_id||'')}"><td>${rowMain(s.instrument_symbol||'UNKNOWN',`${trader} · ${rel(s.signal_timestamp||s.created_at)}`)}</td><td>${pill(dir,dir)}</td><td class="num mono">${fmt(entryRaw,6)}</td><td class="num mono">${fmt(s.stop_loss||s.levels?.stop_loss,6)}</td><td class="num mono">${fmt(s.take_profit_1||s.levels?.take_profit_1,6)}</td><td class="num">${distCell}</td><td>${pill(s.status||'signal',s.status)}</td><td><div class="secondary">${esc((s.raw_signal_text||s.news_content||s.news_headline||'').slice(0,90))}</div></td></tr>`}).join('')}
function posRows(rows,limit){return rows.slice(0,limit||rows.length).map(p=>{const pnl=n(p.unrealized_pnl_usd??p.pnl_usd);return`<tr data-call="${esc(p.signal_interpretation_id||'')}" data-news="${esc(p.news_item_id||'')}"><td>${rowMain(p.instrument_symbol||'UNKNOWN',`${traderName(p)} · ${rel(p.signal_timestamp||p.opened_at)}`)}</td><td>${pill(p.direction,p.direction)}</td><td>${pill(p.status,p.status)}</td><td class="num mono">${fmt(p.entry_price,6)}</td><td class="num mono">${fmt(p.current_price,6)}</td><td class="num ${pnl>=0?'success':'danger'}">${usd(pnl)}</td><td class="num ${n(p.pnl_pct)>=0?'success':'danger'}">${pct(n(p.pnl_pct)*100)}</td><td class="num">$${fmt(p.notional_usd,0)}</td><td>${esc(p.signal_source||p._source||'—')}</td></tr>`}).join('')}
function renderOpsPage(){const s=state.snap||{};const c=chart('cost-chart');if(c)c.setOption({backgroundColor:'transparent',grid:{left:55,right:20,top:20,bottom:35},xAxis:{type:'category',data:['Spent','Remaining','Limit'],axisLabel:{color:'#8f8f9b'}},yAxis:{type:'value',axisLabel:{color:'#8f8f9b',formatter:v=>'$'+v},splitLine:{lineStyle:{color:'#2b2b34'}}},series:[{type:'bar',barWidth:34,data:[n(s.api_cost_today_usd),n(s.budget_remaining_usd||100),n(s.budget_limit_usd||100)],itemStyle:{borderRadius:[10,10,0,0],color:p=>['#fc72ff','#35d07f','#7a5cff'][p.dataIndex]}}]});$('#services-grid').innerHTML=(s.services||[]).map(x=>{const hb=x.heartbeat;let st='disabled';if(hb)st=hb.is_stale?'stale':'live';else if(x.enabled_on_vps)st='enabled';return`<div class="service"><strong>${esc(x.name)}</strong><span>${esc(x.kind||'daemon')} · ${st}</span></div>`}).join('')}
function renderSignalsTable(rows){table('#signals-table',[{label:'Signal'},{label:'Side'},{label:'Entry',num:1},{label:'SL',num:1},{label:'TP1',num:1},{label:'Δ entry',num:1},{label:'Status'},{label:'Call text'}],[sigRows(rows)]);wireRows()}
function renderPositionsTable(rows){table('#positions-table',[{label:'Position'},{label:'Side'},{label:'Status'},{label:'Entry',num:1},{label:'Now',num:1},{label:'P&L',num:1},{label:'P&L %',num:1},{label:'Notional',num:1},{label:'Source'}],[posRows(rows)]);wireRows()}
function renderTradersPage(){const pm=perfMap();let rows=(state.snap?.leaderboard||[]).map(t=>({...t,perf:pm[t.actor_id]||pm[`jarvais_${t.actor_id}`]}));const q=$('#traders-filter')?.value;rows=filterRows(rows,q,['actor_id','display_name','platform']);const sort=$('#traders-sort')?.value||'success';rows.sort((a,b)=>sort==='trades'?n(b.perf?.total_trades||b.closed_position_count)-n(a.perf?.total_trades||a.closed_position_count):sort==='pnl'?n(b.perf?.total_pnl)-n(a.perf?.total_pnl):sort==='edge'?n(b.edge_score)-n(a.edge_score):n(b.perf?.win_rate||0)-n(a.perf?.win_rate||0));const body=rows.map(t=>{const p=t.perf||{}, name=t.display_name||t.actor_id;return`<tr data-trader="${esc(t.actor_id)}"><td>${rowMain(name,`${t.platform||'discord'} · ${t.actor_type||'unknown'}`)}</td><td class="num">${fmt(p.win_rate,1)}%</td><td class="num">${p.total_trades??t.closed_position_count??0}</td><td class="num success">${p.wins??'—'}</td><td class="num danger">${p.losses??'—'}</td><td class="num ${n(p.total_pnl)>=0?'success':'danger'}">${p.total_pnl!=null?usd(p.total_pnl):'—'}</td><td class="num">${fmt(t.edge_score,3)}</td><td>${esc(t._company||'—')}</td></tr>`}).join('');table('#traders-table',[{label:'Discord / actor'},{label:'Success',num:1},{label:'Trades',num:1},{label:'Wins',num:1},{label:'Losses',num:1},{label:'P&L',num:1},{label:'Edge',num:1},{label:'Company'}],[body]);$$('#traders-table tr[data-trader]').forEach(tr=>tr.onclick=()=>openTrader(tr.dataset.trader))}

/* ─── Discord drawer with per-chart tabs ─── */
async function openDiscordDrawer(newsItemId){
  if(!newsItemId)return;
  openDrawer('Discord message','DISCORD SIGNAL','<div class="empty">Loading charts from this message…</div>');
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
  const first=signals[0];
  const msg=res.news||first||{};
  const trader=traderName(first)||msg.author||'trader';
  let html=`<div class="discord-drawer-msg">
    <div class="discord-avatar">${initials(trader)}</div>
    <div><div class="discord-user">${esc(trader)}</div>
    <div class="discord-text big">${esc(msg.content||msg.headline||first.raw_signal_text||'')}</div></div>
  </div>`;
  if(signals.length>1){
    html+=`<div class="chart-tabs">`;
    signals.forEach((s,i)=>{
      html+=`<button class="chart-tab ${i===0?'active':''}" data-chart-idx="${i}">${esc(s.instrument_symbol||'Chart')} ${s.consensus_direction||''}</button>`;
    });
    html+=`</div>`;
  }
  html+=`<div class="chart-tab-content" id="chart-tab-content">Loading chart #1…</div>`;
  $('#drawer-body').innerHTML=html;

  // Load first chart immediately
  loadChartTab(signals[0],0);
  // Wire tab clicks
  $$('.chart-tab').forEach(b=>b.onclick=()=>{
    $$('.chart-tab').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    const idx=parseInt(b.dataset.chartIdx);
    loadChartTab(signals[idx],idx);
  });
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
  parentEl.innerHTML=`
    <section class="drawer-panel chart-panel">
      <div class="chart-toolbar"><div><h3>${esc(r.symbol)} · ${esc(r.timeframe)} · ${r.coverage?.candle_count||0} candles</h3><p>${esc(sig.direction||'')} · ${sig.confidence?fmt(sig.confidence,3):''} confidence</p></div><div class="replay-actions"><button class="mini" id="replay-play">Replay</button><button class="mini" id="replay-reset">Reset</button></div></div>
      <div class="chart-split">
        ${media?`<div class="posted-chart"><img class="chart-img xl" src="${esc(media)}" loading="lazy" decoding="async" onerror="this.onerror=null;this.style.display='none'"></div>`:''}
        <div id="disc-chart-${esc(r.id)}" class="replay-chart"></div>
      </div>
    </section>
    <section class="drawer-panel levels-panel"><h3>Entry / stop / targets</h3><div class="level-grid wide">${levelCards(levels)}</div></section>
    <div class="drawer-grid bottom-grid">
      <section class="drawer-panel"><h3>Trade state</h3><div class="state-grid"><div><label>Outcome</label><strong>${esc(outcome)}</strong></div><div><label>P&L</label><strong class="${pnl>=0?'success':'danger'}">${usd(pnl)}</strong></div><div><label>Max profit</label><strong>${fmt(pos.max_profit_pct,2)}%</strong></div><div><label>Max drawdown</label><strong>${fmt(pos.max_drawdown_pct,2)}%</strong></div><div><label>Distance entry</label><strong>${fmt(pos.distance_to_entry_pct,2)}%</strong></div><div><label>Distance TP1</label><strong>${fmt(pos.distance_to_tp1_pct,2)}%</strong></div></div></section>
      <section class="drawer-panel"><h3>Reasoning</h3><p class="call-text big"><b>Trader:</b> ${esc(sig.trader_stated_thesis||'—')}</p><p class="call-text big"><b>AI thesis:</b> ${esc(sig.llm_inferred_thesis||'—')}</p><p class="call-text big"><b>LLM:</b> ${esc(sig.llm_reasoning||'—')}</p><p class="call-text big"><b>ChartHacker:</b> ${esc(sig.ai_comment||'—')}</p></section>
      <section class="drawer-panel"><h3>Tags</h3><div class="tag-row">${Object.entries(sig.tags||{}).flatMap(([k,v])=>Array.isArray(v)?v.map(x=>pill(`${k}:${x}`)):[]).join('')||'<span class="secondary">No tags</span>'}</div><p class="secondary">Agreement: reason ${fmt(sig.reason_agreement_score,3)} · AI ${fmt(sig.ai_agreement_score,2)}</p></section>
    </div>`;
  // Init chart
  const chartId='disc-chart-'+r.id;
  setTimeout(()=>{
    state._discReplay=r;
    state._discChartId=chartId;
    renderReplayChart(r,chartId);
    const playBtn=parentEl.querySelector('#replay-play');
    const resetBtn=parentEl.querySelector('#replay-reset');
    if(playBtn)playBtn.onclick=()=>animateReplay(r,chartId);
    if(resetBtn)resetBtn.onclick=()=>renderReplayChart(r,chartId);
  },100);
}

/* ─── Radar / Entry watch ─── */
function signalDistance(s){if(Number.isFinite(s._liveDist))return s._liveDist;const live=n(s.current_price||s._livePrice);const entry=n(s.entry_price||s.levels?.entry);if(!entry)return 999999;if(live)return Math.abs((live-entry)/entry*100);const d=Math.abs(n(s.distance_to_entry_pct));return d?d:999999}
function radarCard(s,i){const id=s.signal_interpretation_id||s.id;const sym=s.symbol||s.instrument_symbol||'UNKNOWN';const dir=(s.consensus_direction||s.direction||'').toLowerCase();const entry=n(s.entry_price||s.levels?.entry), sl=n(s.stop_loss||s.levels?.stop_loss), tp=n(s.take_profit_1||s.levels?.take_profit_1);const dist=signalDistance(s);const ageDays=(Date.now()-new Date(s.signal_timestamp||s.created_at))/86400000;const trader=traderName(s);const stale=ageDays>7?' stale':'';const rr=entry&&sl&&tp?Math.abs((tp-entry)/(entry-sl)):0;const live=(s.current_price||s._livePrice)?` · live ${fmt(s.current_price||s._livePrice,6)}`:'';return `<div class="radar-card${stale}" data-call="${esc(id)}" data-news="${esc(s.news_item_id||'')}" data-radar-idx="${i}"><div class="radar-top"><div>${rowMain(sym,`${trader} · ${(s.timeframe||'1m')} · ${rel(s.signal_timestamp||s.created_at)} old${live}`)}</div>${pill(dir,dir)}</div><div class="mini-tv" id="mini-radar-${i}">${(!s._candles?.length&&s.media_url)?`<img class="mini-chart-img" src="${esc(s.media_url)}">`:''}<div class="riskbox ${dir==='short'?'short':'long'}"><i class="reward"></i><i class="risk"></i><b class="entry-line"></b></div><span class="mini-loading">${s._candles?.length?'':'chart snapshot / no local candles'}</span></div><div class="radar-metrics"><div><label>to entry</label><strong>${dist===999999?'—':fmt(dist,2)+'%'}</strong></div><div><label>entry</label><strong>${fmt(entry,6)}</strong></div><div><label>SL</label><strong>${fmt(sl,6)}</strong></div><div><label>TP1</label><strong>${fmt(tp,6)}</strong></div><div><label>R:R</label><strong>${rr?fmt(rr,2):'—'}</strong></div><div><label>TF</label><strong>${esc(s.timeframe||'1m')}</strong></div><div><label>age</label><strong>${fmt(ageDays,1)}d</strong></div></div></div>`}
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
  $('#drawer-kicker').textContent=`CALL DETAIL | Interp ID: ${r.id}${pos.id ? ` · Pos ID: ${pos.id}` : ''}`;
  const traderName=trader.handle_raw||trader.display_name||trader.handle_normalized||news.author||'trader';
  const outcome=pos.outcome||pos.status||'tracking';
  const pnl=n(pos.realized_pnl_usd_final??pos.realized_pnl_usd??pos.unrealized_pnl_usd);
  const media=r.media_url||r.annotated_chart_url;
  $('#drawer-body').innerHTML=`
    <div class="replay-layout">
      <section class="drawer-panel chart-panel">
        <div class="chart-toolbar"><div><h3>Trader chart / reconstructed replay</h3><p>${esc(r.symbol)} · ${esc(r.exchange)} · ${esc(r.timeframe)} · ${r.coverage?.candle_count||0} candles</p></div><div class="replay-actions"><button class="mini" id="replay-play">Replay</button><button class="mini" id="replay-reset">Reset</button></div></div>
        <div class="chart-split">
          <div class="posted-chart">${media?`<img class="chart-img xl" src="${esc(media)}" loading="lazy" decoding="async" onerror="this.onerror=null;this.src='${esc(r.annotated_chart_url||'')}'">`:'<div class="empty">No downloaded/remote chart found</div>'}</div>
          <div id="replay-chart" class="replay-chart"></div>
        </div>
      </section>
      <section class="drawer-panel levels-panel"><h3>Entry / stop / targets</h3><div class="level-grid wide">${levelCards(levels)}</div></section>
      <div class="drawer-grid bottom-grid">
        <section class="drawer-panel"><h3>Discord call</h3><div class="discord-msg large"><div class="discord-avatar">${initials(traderName)}</div><div><div class="discord-head"><span class="discord-user">${esc(traderName)}</span><span class="discord-time">${rel(news.published_at||sig.created_at||r.call_ts)}</span></div><div class="discord-text big">${esc(news.content||news.headline||'')}</div></div></div></section>
        <section class="drawer-panel"><h3>Trade state</h3><div class="state-grid"><div><label>Outcome</label><strong>${esc(outcome)}</strong></div><div><label>P&L</label><strong class="${pnl>=0?'success':'danger'}">${usd(pnl)}</strong></div><div><label>Max profit</label><strong>${fmt(pos.max_profit_pct,2)}%</strong></div><div><label>Max drawdown</label><strong>${fmt(pos.max_drawdown_pct,2)}%</strong></div><div><label>Distance entry</label><strong>${fmt(pos.distance_to_entry_pct,2)}%</strong></div><div><label>Distance TP1</label><strong>${fmt(pos.distance_to_tp1_pct,2)}%</strong></div></div></section>
        <section class="drawer-panel"><h3>Reasoning / intelligence</h3><p class="call-text big"><b>Trader thesis:</b> ${esc(sig.trader_stated_thesis||'—')}</p><p class="call-text big"><b>AI thesis:</b> ${esc(sig.llm_inferred_thesis||'—')}</p><p class="call-text big"><b>LLM reasoning:</b> ${esc(sig.llm_reasoning||'—')}</p><p class="call-text big"><b>ChartHacker:</b> ${esc(sig.ai_comment||'—')}</p></section>
        <section class="drawer-panel"><h3>Memory / tags</h3><div class="tag-row">${Object.entries(sig.tags||{}).flatMap(([k,v])=>Array.isArray(v)?v.map(x=>pill(`${k}:${x}`)):[]).join('')||'<span class="secondary">No tags captured</span>'}</div><p class="secondary">Agreement: reason ${fmt(sig.reason_agreement_score,3)} · AI ${fmt(sig.ai_agreement_score,2)}</p></section>
      </div>
    </div>`;
  renderReplayChart(r);
  $('#replay-play')?.addEventListener('click',()=>animateReplay(r));
  $('#replay-reset')?.addEventListener('click',()=>renderReplayChart(r));
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
  const series=candleSeries(candles); const labels=candles.map(x=>String(x.timestamp).slice(5,16).replace('T',' '));
  c.setOption({backgroundColor:'transparent',animation:false,grid:{left:58,right:28,top:22,bottom:36},tooltip:{trigger:'axis',axisPointer:{type:'cross'},backgroundColor:'#111',borderColor:'#333',textStyle:{color:'#fff'}},xAxis:{type:'category',data:labels,axisLabel:{color:'#8f8f9b',fontSize:10},axisLine:{lineStyle:{color:'#2b2b34'}}},yAxis:{scale:true,axisLabel:{color:'#8f8f9b'},splitLine:{lineStyle:{color:'#2b2b34'}}},dataZoom:[{type:'inside'},{type:'slider',height:18,bottom:6,borderColor:'#2b2b34',textStyle:{color:'#8f8f9b'}}],series:[{type:'candlestick',data:series.map(x=>[x[1],x[2],x[3],x[4]]),itemStyle:{color:'#35d07f',color0:'#ff5f72',borderColor:'#35d07f',borderColor0:'#ff5f72'},markLine:{symbol:'none',data:markLines(r.signal?.levels)}}]});
}
function animateReplay(r,chartId='replay-chart'){
  const total=(r.candles||[]).length; if(!total)return;
  let i=Math.max(10,Math.floor(total*.08));
  const step=Math.max(1,Math.floor(total/90));
  const timer=setInterval(()=>{i+=step;renderReplayChart(r,chartId,Math.min(i,total));if(i>=total)clearInterval(timer)},90);
}
function drawCallFallback(res,id){const rows=res.rows||[];if(!rows.length){$('#drawer-body').innerHTML='<div class="empty">No interpretation available yet</div>';return}const r=rows[0];const levels=r.levels||r.llm?.levels||{};const chartUrl=r.media?.url||`api/charts/${r.id}`;$('#drawer-title').textContent=`${r.instrument?.symbol||'Signal'} ${r.consensus_direction||''}`;$('#drawer-kicker').textContent=`CALL DETAIL | Interp ID: ${r.id}`;$('#drawer-body').innerHTML=`<div class="drawer-panel chart-panel"><img class="chart-img xl" src="${chartUrl}" loading="lazy" decoding="async"><div class="level-grid wide">${levelCards(levels)}</div></div>`}
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
function chart(id){const el=document.getElementById(id);if(!el||!window.echarts)return null;if(state.charts[id])state.charts[id].dispose();const c=echarts.init(el);state.charts[id]=c;setTimeout(()=>c.resize(),50);return c}
function wireRows(){$$('[data-call]').forEach(tr=>{if(tr._wired)return;tr._wired=true;tr.onclick=()=>openCall(tr.dataset.call,tr.dataset.news)})}
function wire(){
  $$('.nav-link').forEach(b=>b.onclick=()=>switchTab(b.dataset.tab));
  $$('[data-open]').forEach(b=>b.onclick=()=>switchTab(b.dataset.open));
  $('#company-filter').onchange=e=>{state.company=e.target.value;load()};
  $('#refresh-btn').onclick=()=>load();
  $('#drawer-close').onclick=closeDrawer; $('#drawer-backdrop').onclick=closeDrawer;
  ['floor-signal-filter','floor-position-filter','radar-filter','radar-age'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=()=>renderFloor()});
  ['signals-filter','signals-status'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderSignalsPage()});
  ['positions-filter','positions-status'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderPositionsPage()});
  ['traders-filter','traders-sort'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderTradersPage()});
  ['news-filter','news-media'].forEach(id=>{const el=$('#'+id); if(el) el.oninput=el.onchange=()=>renderNewsPage()});
}
window.addEventListener('DOMContentLoaded',()=>{loadDrawerWidth();wire();attachDrawerResizer();load();state.timer=setInterval(()=>{const ts=new Date();$('#updated-at').textContent=`${ts.toLocaleTimeString()}`; if(['floor','radar','signals','positions','competition'].includes(state.tab)) load()},30000)});
})();
