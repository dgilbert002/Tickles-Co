/* Mirror Config — agent to demo account assignment + Exchange Accounts */
function renderMirrorConfig(){
  var agents=["copy_spot_seq","copy_lev_parallel","copy_lev_be_lock","copy_opt_spot_seq","copy_opt_lev_parallel","copy_opt_lev_be_lock","copy_charthacker","copy_spot_lev_3x","copy_rose_a","copy_rose_b","copy_rose_c","copy_lev_3pct","copy_bcusa_a","copy_bcusa_b","copy_bcusa_c","copy_binance_a","copy_binance_b","copy_binance_c","copy_ufo_a","copy_ufo_b","copy_ufo_c"];
  (async function(){
    var mappings={};
    try{
      var u=new URL("api/mirror-config",document.baseURI);
      var resp=await fetch(u); var d=await resp.json();
      if(d.ok&&d.mappings){d.mappings.forEach(function(m){if(!mappings[m.agent_id])mappings[m.agent_id]=[];mappings[m.agent_id].push(m.account_name);});}
    }catch(e){}
    var acctData=await api("/api/exchange-accounts",{skipCompany:true});
    var accts=acctData.accounts||[];
    var demoAccts=accts.filter(function(a){return (a.accountType==='demo'||a.accountType==='live')&&a.isActive;});
    var rows=agents.map(function(a){
      var assigned=mappings[a]||[];
      var cells=demoAccts.map(function(acct){
        var ch=assigned.indexOf(acct.accountName)>=0?' checked':'';
        return '<label style="margin-right:12px;cursor:pointer;font-size:13px"><input type="checkbox" data-agent="'+a+'" data-acct="'+acct.id+'"'+ch+'> '+acct.exchange+'/'+acct.accountName+'</label>';
      }).join('');
      return '<tr><td style="min-width:200px"><strong>'+a+'</strong></td><td>'+(cells||'<span class="secondary">no demo accounts</span>')+'</td></tr>';
    }).join('');
    var el=document.getElementById('mirror-config-table');
    if(el)el.innerHTML='<table class="data-table"><thead><tr><th>Agent</th><th>Demo Accounts</th></tr></thead><tbody>'+rows+'</tbody></table>';
    document.querySelectorAll('#mirror-config-table input[type=checkbox]').forEach(function(cb){cb.onchange=function(){toggleMirror(cb);};});
  })();
}

function toggleMirror(cb){
  var agent=cb.dataset.agent, acct=parseInt(cb.dataset.acct), comp="copy-trade-scenarios";
  if(cb.checked){
    _postAPI("/api/mirror-config",{competitionId:comp,agentId:agent,exchangeAccountId:acct,priority:0}).then(function(r){if(!r.ok)alert(r.error||"Failed");});
  }else{
    var u=new URL("api/mirror-config?competitionId="+comp+"&agentId="+agent+"&exchangeAccountId="+acct,document.baseURI);
    fetch(u,{method:"DELETE"}).then(function(r){return r.json();}).then(function(r){if(!r.ok)alert("Failed");});
  }
}

/* Exchange Accounts */
async function renderExchangeAccounts(){
  var data=await api('/api/exchange-accounts',{skipCompany:true});
  var accts=data.accounts||[];
  var rows=accts.map(function(a){
    var bal=a.lastBalance!=null?'$'+fmt(a.lastBalance,2):'—';
    var tested=a.lastTestedAt?new Date(a.lastTestedAt).toLocaleString():'never';
    var status=a.isActive?'<span class="badge badge-pro">active</span>':'<span class="badge">inactive</span>';
    var typeTag=a.accountType==='demo'?'<span class="src-tag src-api">demo</span>':a.accountType==='live'?'<span class="src-tag" style="background:rgba(0,204,120,.14);color:#00cc78;border-color:rgba(0,204,120,.3)">live</span>':'<span class="src-tag">'+esc(a.accountType)+'</span>';
    return '<tr><td>'+rowMain(a.exchange+'/'+a.accountName,a.description||'—')+'</td><td>'+typeTag+'</td><td>'+status+'</td><td class="num mono">'+bal+'</td><td class="num">'+tested+'</td><td><button class="pill-btn small" data-test="'+a.id+'">Test</button> <button class="pill-btn small" data-sync="'+a.id+'">Sync</button> <button class="pill-btn small" data-edit="'+a.id+'">Edit</button> <button class="pill-btn small" data-remove="'+a.id+'">Del</button></td></tr>';
  });
  table('#exchange-accounts-table',[{label:'Account'},{label:'Type'},{label:'Status'},{label:'Balance',num:true},{label:'Last Tested',num:true},{label:'Actions'}],rows);
  var btns=document.querySelectorAll('#exchange-accounts-table [data-test]');btns.forEach(function(b){b.onclick=function(){testAccount(+b.dataset.test);};});
  btns=document.querySelectorAll('#exchange-accounts-table [data-sync]');btns.forEach(function(b){b.onclick=function(){syncAccount(+b.dataset.sync);};});
  btns=document.querySelectorAll('#exchange-accounts-table [data-edit]');btns.forEach(function(b){b.onclick=function(){showAccountModal(+b.dataset.edit);};});
  btns=document.querySelectorAll('#exchange-accounts-table [data-remove]');btns.forEach(function(b){b.onclick=function(){removeAccount(+b.dataset.remove);};});
}

async function testAccount(id){
  var btn=document.querySelector('[data-test="'+id+'"]');if(btn){btn.textContent='...';btn.disabled=true;}
  try{
    var r=await _postAPI('/api/exchange-accounts/'+id+'/test',{});
    if(r.ok||r.status==='ok'){var bal=r.balance||{};alert('Connected! Balance: $'+(bal.totalUsdt||'?')+'  Positions: '+(r.positions?.count||0)+'  Markets: '+(r.markets?.totalPerpetuals||'?')+' perps');}
    else{alert('Failed: '+(r.message||r.error||'unknown'));}
  }catch(e){alert('Error: '+e.message)}
  finally{if(btn){btn.textContent='Test';btn.disabled=false}}
  renderExchangeAccounts();
}

async function syncAccount(id){
  var btn=document.querySelector('[data-sync="'+id+'"]');if(btn){btn.textContent='...';btn.disabled=true;}
  try{var r=await _postAPI('/api/exchange-accounts/'+id+'/sync-markets',{});alert(r.ok||r.status==='ok'?('OK: '+r.message):('Failed: '+(r.message||r.error||'failed')));}
  catch(e){alert('Error: '+e.message)}
  finally{if(btn){btn.textContent='Sync';btn.disabled=false}}
}

async function removeAccount(id){
  if(!confirm('Delete this account?'))return;
  try{var u=new URL('api/exchange-accounts/'+id,document.baseURI);var r=await fetch(u,{method:'DELETE'});var d=await r.json();if(!r.ok){alert(d.error||'Delete failed');return}renderExchangeAccounts();}
  catch(e){alert('Error: '+e.message)}
}

function showAccountModal(id){
  var backdrop=document.createElement('div');backdrop.className='modal-backdrop';
  var modal=document.createElement('div');modal.className='modal';
  modal.innerHTML='<div class="modal-head"><h2>'+(id?'Edit':'Add')+' Exchange Account</h2><button class="drawer-close">&times;</button></div><div class="modal-body"><div class="settings-label">Exchange</div><select class="select wide" id="acct-exch"><option value="bybit">Bybit</option><option value="blofin">BloFin</option><option value="bitget">Bitget</option><option value="toobit">Toobit</option></select><div class="settings-label">Account Name</div><input class="input wide" id="acct-name" placeholder="e.g. DEMO"><div class="settings-label">Description</div><input class="input wide" id="acct-desc" placeholder="Label"><div class="settings-label">Account Type</div><select class="select wide" id="acct-type"><option value="demo">Demo</option><option value="live">Live</option></select><div class="settings-label">API Key</div><input class="input wide mono" id="acct-key" placeholder="API key"><div class="settings-label">API Secret</div><input class="input wide mono" id="acct-secret" placeholder="API secret" type="password"><div class="settings-label">API Passphrase</div><input class="input wide mono" id="acct-pass" placeholder="Optional" type="password"></div><div class="modal-foot"><button class="pill-btn" id="acct-cancel">Cancel</button><button class="pill-btn" id="acct-save" style="background:var(--purple);color:#fff">Save</button></div>';
  backdrop.appendChild(modal);document.body.appendChild(backdrop);
  var close=function(){backdrop.remove();};
  modal.querySelector('.drawer-close').onclick=close;
  backdrop.querySelector('#acct-cancel').onclick=close;
  backdrop.onclick=function(e){if(e.target===backdrop)close();};
  if(id){(async function(){var u=new URL('api/exchange-accounts/'+id,document.baseURI);var resp=await fetch(u);var d=await resp.json();var a=(d.ok&&d.account)?d.account:null;if(a){document.getElementById('acct-exch').value=a.exchange||'bybit';document.getElementById('acct-name').value=a.accountName||'';document.getElementById('acct-desc').value=a.description||'';document.getElementById('acct-type').value=a.accountType||'demo';document.getElementById('acct-key').value=a.apiKey||'';document.getElementById('acct-secret').value=a.apiSecret||'';document.getElementById('acct-pass').value=a.apiPassphrase||'';}})();}
  backdrop.querySelector('#acct-save').onclick=async function(){
    var body={exchange:document.getElementById('acct-exch').value,accountName:document.getElementById('acct-name').value.trim(),description:document.getElementById('acct-desc').value.trim(),accountType:document.getElementById('acct-type').value,apiKey:document.getElementById('acct-key').value.trim(),apiSecret:document.getElementById('acct-secret').value.trim(),apiPassphrase:document.getElementById('acct-pass').value.trim()};
    if(!body.exchange||!body.accountName||!body.apiKey||!body.apiSecret){alert('Required fields missing');return;}
    try{var r;if(id){var u2=new URL('api/exchange-accounts/'+id,document.baseURI);var resp2=await fetch(u2,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});r=await resp2.json();}else{r=await _postAPI('/api/exchange-accounts',body);}if(r.ok){close();renderExchangeAccounts()}else{alert(r.error||r.message||'Save failed')}}catch(e){alert('Error: '+e.message)}
  };
}

async function _postAPI(path,body){
  var u=new URL(path.replace(/^\//,''),document.baseURI);
  var r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  return r.json();
}

document.addEventListener('DOMContentLoaded',function(){
  var btn=document.getElementById('btn-add-account');
  if(btn)btn.onclick=function(){showAccountModal(null);};
});

/* ────────────────────────────────────────────────────────────────────────
   Paper vs Demo vs Live — forensic audit
   Phase 1 (2026-05-29): rebuilt from a 4-stat strip + 6-col table into a full
   forensic surface: accuracy score, reject-cause breakdown, per-order line
   items (entry/slippage/fees/P&L/timing across paper|demo|live lanes), an
   on-demand "what's actually on the exchange" view, and a live transaction
   log viewer. Backed by /api/paper-vs-demo, /api/paper-demo/exchange-state
   and /api/paper-demo/log.
   ──────────────────────────────────────────────────────────────────────── */
var _pdView='compare';

function _pdFmtTime(iso){ if(!iso)return '—'; try{var d=new Date(iso); return d.toLocaleString(undefined,{month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});}catch(e){return iso;} }
function _pdStatusBadge(st,err){
  if(st==='filled')return '<span class="badge badge-pro">filled</span>';
  if(st==='pending')return '<span class="badge">pending</span>';
  if(st==='cancelled')return '<span class="badge" style="color:#f5a623">cancelled</span>';
  if(st==='rejected')return '<span class="badge" style="color:#ff5470">rejected</span>';
  return '<span class="secondary">—</span>';
}

async function renderPaperDemo(){
  var data=await api('/api/paper-vs-demo',{skipCompany:true});
  var s=data.summary||{};
  var acc=data.accuracy||{};
  var trades=data.trades||[];
  // Surface rows that actually have a demo leg first (the interesting
  // comparisons) — otherwise 300+ historical paper-only trades bury them.
  trades.sort(function(a,b){
    var da=a.demo_status?1:0, db=b.demo_status?1:0;
    if(da!==db)return db-da;
    var ta=a.demo_ordered_at||a.entered_at||'', tb=b.demo_ordered_at||b.entered_at||'';
    return tb<ta?-1:(tb>ta?1:0);
  });

  // ── Accuracy badge ──
  var ab=document.getElementById('paperdemo-accuracy');
  if(ab){
    if(acc.score==null){
      ab.innerHTML='<div class="alabel">Paper→Demo accuracy</div><div class="score na">—</div><div class="asub">'+esc(acc.note||'no filled demo orders yet')+'</div>';
    }else{
      var cls=acc.score>=99.5?'good':(acc.score>=98?'warn':'bad');
      ab.innerHTML='<div class="alabel">Paper→Demo accuracy</div><div class="score '+cls+'">'+fmt(acc.score,2)+'%</div>'+
        '<div class="asub">'+acc.matched_filled+' filled · mean slip '+(acc.mean_slippage_pct!=null?fmt(acc.mean_slippage_pct,3)+'%':'—')+' · fill rate '+(acc.fill_rate_pct!=null?fmt(acc.fill_rate_pct,0)+'%':'—')+'</div>';
    }
  }

  // ── Stats strip ──
  var driftCell='—', driftCls='';
  if(s.drift!=null){driftCls=(s.drift>=0?'success':'danger');driftCell=(s.drift>=0?'+':'')+'$'+fmt(Math.abs(s.drift),2);}
  var stats=document.getElementById('paperdemo-stats');
  if(stats)stats.innerHTML=
    '<div class="stat"><label>Paper P&L</label><strong class="'+(s.paper_pnl>=0?'success':'danger')+'">$'+fmt(s.paper_pnl,2)+'</strong><span>'+s.paper_total+' trades</span></div>'+
    '<div class="stat"><label>Demo Orders</label><strong>'+s.demo_total+'</strong><span>'+s.demo_filled+' filled · '+(s.demo_pending||0)+' pending · '+(s.demo_cancelled||0)+' cancelled</span></div>'+
    '<div class="stat"><label>Rejected</label><strong class="'+((s.demo_rejected||0)>0?'danger':'')+'">'+(s.demo_rejected||0)+'</strong><span>see causes below</span></div>'+
    '<div class="stat"><label>Demo Fees</label><strong>$'+fmt(s.demo_fees||0,4)+'</strong><span>execution + funding</span></div>'+
    '<div class="stat"><label>Drift</label><strong class="'+driftCls+'">'+driftCell+'</strong><span>paper vs demo P&amp;L</span></div>'+
    '<div class="stat"><label>Live</label><strong>'+(s.live_total||0)+'</strong><span>no live account mapped yet</span></div>';

  // ── Reject-cause breakdown ──
  var rj=document.getElementById('paperdemo-rejects');
  if(rj){
    var rs=data.reject_summary||[];
    if(rs.length){
      rj.innerHTML='<div class="fx-sec" style="margin:10px 0 2px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em">Why orders were rejected</div><div class="reject-grid">'+
        rs.map(function(r){return '<div class="reject-card"><div class="rc-n">'+r.count+'</div><div class="rc-r">'+esc(r.reason)+'</div><div class="rc-raw" title="'+esc(r.raw||'')+'">'+esc(r.raw||'')+'</div></div>';}).join('')+'</div>';
    }else rj.innerHTML='';
  }

  // ── Comparison line-item table ──
  var rows=trades.map(function(t){
    var agent=t.demo_agent_id||t.agent_id||'';
    var sub=(t.orphan?'<span class="src-tag">demo-only</span> ':'')+t.direction+(agent?(' · '+agent):'');
    var acctLine=(t.demo_exchange?(t.demo_exchange+'/'+t.demo_account):'<span class="secondary">no demo order</span>');
    var slip=(t.slippage_pct!=null)?('<span class="'+(Math.abs(t.slippage_pct)<0.5?'success':'danger')+'">'+(t.slippage_pct>=0?'+':'')+fmt(t.slippage_pct,3)+'%</span>'):'—';
    var fees=(t.fees_total!=null)?('$'+fmt(t.fees_total,4)):'<span class="secondary">—</span>';
    var demoPnl=(t.demo_pnl!=null)?usd(t.demo_pnl):'<span class="secondary">—</span>';
    var paperPnl=(t.paper_pnl!=null)?usd(t.paper_pnl):'<span class="secondary">—</span>';
    var timing='<span class="secondary small">ord '+_pdFmtTime(t.demo_ordered_at)+'</span>';
    if(t.demo_filled_at)timing+='<br><span class="secondary small">fill '+_pdFmtTime(t.demo_filled_at)+'</span>';
    var statusCell=_pdStatusBadge(t.demo_status,t.demo_error);
    if(t.demo_status==='rejected'&&t.demo_error)statusCell+='<br><span class="secondary small" title="'+esc(t.demo_error)+'">'+esc(t.demo_error.slice(0,60))+'…</span>';
    var slTp=(t.sl?'SL $'+fmt(t.sl,4):'<span class="secondary">no SL</span>')+'<br>'+(t.tp?'TP $'+fmt(t.tp,4):'<span class="secondary">no TP</span>');
    return '<tr>'+
      '<td>'+rowMain(t.symbol||'?',sub)+'<div class="secondary small">'+acctLine+'</div></td>'+
      '<td class="num mono">'+(t.paper_entry?'$'+fmt(t.paper_entry,4):'—')+'</td>'+
      '<td class="num mono">'+(t.demo_entry?'$'+fmt(t.demo_entry,4):'<span class="secondary">—</span>')+'</td>'+
      '<td class="num mono small">'+slTp+'</td>'+
      '<td class="num">'+slip+'</td>'+
      '<td class="num mono">'+fees+'</td>'+
      '<td class="num mono">'+paperPnl+'</td>'+
      '<td class="num mono">'+demoPnl+'</td>'+
      '<td>'+statusCell+'</td>'+
      '<td>'+timing+'</td>'+
    '</tr>';
  });
  if(document.getElementById('paperdemo-table'))table('#paperdemo-table',[
    {label:'Trade / Account'},{label:'Paper Entry',num:true},{label:'Demo Entry',num:true},
    {label:'SL / TP',num:true},
    {label:'Slippage',num:true},{label:'Fees',num:true},{label:'Paper P&L',num:true},
    {label:'Demo P&L',num:true},{label:'Status'},{label:'Timing'}
  ],rows);

  // ── Wire sub-view toggle + action buttons (idempotent) ──
  document.querySelectorAll('#paperdemo-view button').forEach(function(b){
    b.onclick=function(){setPdView(b.dataset.pdview);};
    b.classList.toggle('active',b.dataset.pdview===_pdView);
  });
  var rl=document.getElementById('btn-refresh-log'); if(rl)rl.onclick=renderPaperLog;
  var rx=document.getElementById('btn-refresh-exchange'); if(rx)rx.onclick=renderExchangeState;
  // auto-load the log the first time so it's never empty
  if(_pdView==='log')renderPaperLog();
}

function setPdView(v){
  _pdView=v;
  document.querySelectorAll('#paperdemo-view button').forEach(function(b){b.classList.toggle('active',b.dataset.pdview===v);});
  var panes={compare:'paperdemo-pane-compare',exchange:'paperdemo-pane-exchange',log:'paperdemo-pane-log'};
  Object.keys(panes).forEach(function(k){var el=document.getElementById(panes[k]); if(el)el.style.display=(k===v?'':'none');});
  if(v==='log')renderPaperLog();
}

async function renderPaperLog(){
  var box=document.getElementById('paperdemo-log');
  if(!box)return;
  box.innerHTML='<div class="empty">Loading log…</div>';
  try{
    var d=await api('/api/paper-demo/log?lines=300',{skipCompany:true});
    var ev=d.events||[];
    if(!ev.length){box.innerHTML='<div class="empty">No forensic events yet. They appear as the demo bridge places, fills, rejects or cancels orders.</div>';return;}
    var lines=ev.map(function(e){
      var msg='';
      if(e.event==='mirror_placed')msg=e.symbol+' '+e.direction+' @ '+e.paper_entry+'  lev '+e.leverage+'x  notional $'+e.notional+'  ord '+(e.order_id||'').slice(0,8);
      else if(e.event==='fill')msg=e.symbol+' demo@'+e.demo_entry+' vs paper@'+e.paper_entry+'  slip '+(e.slippage_pct!=null?e.slippage_pct+'%':'?')+'  fee '+(e.entry_fee!=null?('$'+e.entry_fee):'?');
      else if(e.event==='mirror_rejected'||e.event==='mirror_error')msg=e.symbol+' '+(e.direction||'')+' — '+(e.reason||e.error||'');
      else if(e.event==='cancel')msg=e.symbol+' ('+(e.exchange_status||'')+')';
      else msg=JSON.stringify(e);
      var lane=e.exchange?('<span class="lane-pill demo">'+esc(e.exchange)+'/'+esc(e.account||'')+'</span>'):'';
      var ag=e.agent?(' <span class="src-tag">'+esc(e.agent)+'</span>'):'';
      return '<div class="logline ev-'+esc(e.event)+'"><span class="lt">'+_pdFmtTime(e.ts)+'</span><span class="lev">'+esc(e.event)+'</span><span class="lmsg">'+esc(msg)+lane+ag+'</span></div>';
    }).join('');
    box.innerHTML='<div class="logbox">'+lines+'</div>';
  }catch(e){box.innerHTML='<div class="empty">Failed to load log: '+esc(e.message)+'</div>';}
}

async function renderExchangeState(){
  var box=document.getElementById('paperdemo-exchange');
  if(!box)return;
  box.innerHTML='<div class="empty">Pulling live data from exchanges… (this hits the network, ~5–15s)</div>';
  try{
    var d=await api('/api/paper-demo/exchange-state',{skipCompany:true});
    var accts=d.accounts||[];
    if(!accts.length){box.innerHTML='<div class="empty">No active demo/live accounts.</div>';return;}
    box.innerHTML='<div class="fx-grid">'+accts.map(function(a){
      var head='<h3>'+esc(a.exchange)+'/'+esc(a.account)+' <span class="src-tag '+(a.type==='live'?'':'src-api')+'">'+esc(a.type)+'</span></h3>'+
        '<div class="fx-meta">agent: '+esc(a.agent||'—')+'</div>';
      if(a.error)return '<div class="fx-card">'+head+'<div class="fx-err">'+esc(a.error)+'</div></div>';
      var bal='<div class="fx-bal">$'+(a.balance!=null?fmt(a.balance,2):'—')+'<span class="secondary small"> USDT</span></div>';
      var pos=(a.positions||[]).map(function(p){return '<div class="fx-row"><span>'+esc(p.symbol)+' '+esc(p.side||'')+'</span><span>'+fmt(p.contracts,4)+' @ '+(p.entryPrice!=null?fmt(p.entryPrice,4):'?')+'  ('+usd(p.unrealizedPnl||0)+')</span></div>';}).join('')||'<div class="fx-row secondary">none</div>';
      var ord=(a.open_orders||[]).map(function(o){return '<div class="fx-row"><span>'+esc(o.symbol)+' '+esc(o.side||'')+' '+esc(o.type||'')+'</span><span>'+(o.amount!=null?fmt(o.amount,4):'?')+' @ '+(o.price!=null?fmt(o.price,4):'?')+'</span></div>';}).join('')||'<div class="fx-row secondary">none</div>';
      return '<div class="fx-card">'+head+bal+
        '<div class="fx-sec">Open positions ('+(a.positions||[]).length+')</div>'+pos+
        '<div class="fx-sec">Resting orders ('+(a.open_orders||[]).length+')</div>'+ord+'</div>';
    }).join('')+'</div>';
  }catch(e){box.innerHTML='<div class="empty">Failed: '+esc(e.message)+'</div>';}
}
