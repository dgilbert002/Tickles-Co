/* Mirror Config — agent to demo account assignment + Exchange Accounts */
function renderMirrorConfig(){
  var agents=["copy_spot_seq","copy_lev_parallel","copy_lev_be_lock","copy_opt_spot_seq","copy_opt_lev_parallel","copy_opt_lev_be_lock","copy_ch_ai_vision","copy_spot_lev_3x","copy_rose_a","copy_rose_b","copy_rose_c","copy_lev_3pct"];
  (async function(){
    var mappings={};
    try{
      var u=new URL("api/mirror-config",document.baseURI);
      var resp=await fetch(u); var d=await resp.json();
      if(d.ok&&d.mappings){d.mappings.forEach(function(m){if(!mappings[m.agent_id])mappings[m.agent_id]=[];mappings[m.agent_id].push(m.account_name);});}
    }catch(e){}
    var acctData=await api("/api/exchange-accounts",{skipCompany:true});
    var accts=acctData.accounts||[];
    var demoAccts=accts.filter(function(a){return a.accountType==='demo'&&a.isActive;});
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
  modal.innerHTML='<div class="modal-head"><h2>'+(id?'Edit':'Add')+' Exchange Account</h2><button class="drawer-close">&times;</button></div><div class="modal-body"><div class="settings-label">Exchange</div><select class="select wide" id="acct-exch"><option value="bybit">Bybit</option><option value="blofin">BloFin</option><option value="bitget">Bitget</option></select><div class="settings-label">Account Name</div><input class="input wide" id="acct-name" placeholder="e.g. DEMO"><div class="settings-label">Description</div><input class="input wide" id="acct-desc" placeholder="Label"><div class="settings-label">Account Type</div><select class="select wide" id="acct-type"><option value="demo">Demo</option><option value="live">Live</option></select><div class="settings-label">API Key</div><input class="input wide mono" id="acct-key" placeholder="API key"><div class="settings-label">API Secret</div><input class="input wide mono" id="acct-secret" placeholder="API secret" type="password"><div class="settings-label">API Passphrase</div><input class="input wide mono" id="acct-pass" placeholder="Optional" type="password"></div><div class="modal-foot"><button class="pill-btn" id="acct-cancel">Cancel</button><button class="pill-btn" id="acct-save" style="background:var(--purple);color:#fff">Save</button></div>';
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

/* Paper vs Demo comparison */
async function renderPaperDemo(){
  var data=await api('/api/paper-vs-demo',{skipCompany:true});
  var s=data.summary||{};
  var trades=data.trades||[];
  
  // Stats strip
  // Phase 1 (2026-05-29): "Demo Orders $" and "Drift" were hardcoded stubs
  // ($0 / —). They now reflect real backend data: demo_notional = filled demo
  // order notional, drift = paper-minus-demo P&L over matched filled pairs
  // (null until a demo order actually fills).
  var demoNotional=(s.demo_notional!=null)?('$'+fmt(s.demo_notional,2)):'$0';
  var driftCell='—', driftCls='';
  if(s.drift!=null){driftCls=(s.drift>=0?'success':'danger');driftCell=(s.drift>=0?'+':'')+'$'+fmt(Math.abs(s.drift),2);}
  var stats=document.getElementById('paperdemo-stats');
  if(stats)stats.innerHTML=
    '<div class="stat"><label>Paper P&L</label><strong class="'+(s.paper_pnl>=0?'success':'danger')+'">$'+fmt(s.paper_pnl,2)+'</strong><span>'+s.paper_total+' trades</span></div>'+
    '<div class="stat"><label>Demo Orders</label><strong>'+demoNotional+'</strong><span>'+s.demo_total+' orders, '+s.demo_filled+' filled</span></div>'+
    '<div class="stat"><label>Rejected</label><strong>'+(s.demo_rejected||0)+'</strong><span>'+(s.demo_pending||0)+' pending</span></div>'+
    '<div class="stat"><label>Drift</label><strong class="'+driftCls+'">'+driftCell+'</strong><span>paper vs demo P&amp;L</span></div>';
  
  // Table
  var rows=trades.map(function(t){
    var demoCell='<span class="secondary">—</span>';
    if(t.demo_status==='pending')demoCell='<span class="badge">pending</span>';
    else if(t.demo_status==='filled')demoCell='<span class="badge badge-pro">filled</span>';
    else if(t.demo_status==='rejected')demoCell='<span class="badge" style="color:#d44">rejected</span><br><span class="secondary small">'+esc(t.demo_error||'')+'</span>';
    
    var entryDiff='';
    if(t.paper_entry&&t.demo_entry){
      var d=((t.demo_entry-t.paper_entry)/t.paper_entry*100);
      entryDiff='<span class="'+(Math.abs(d)<0.5?'success':'danger')+'">'+(d>=0?'+':'')+d.toFixed(3)+'%</span>';
    }
    
    // Phase 1 (2026-05-29): orphan = demo order with no paper trade (the bridge
    // mirrored a signal no paper agent took). Tag it so it's not confused with a
    // matched comparison row.
    var sub=t.orphan?('<span class="src-tag">demo-only</span> '+t.direction):(t.direction+' · '+(t.agent_id||''));
    return '<tr>'+
      '<td>'+rowMain(t.symbol||'?',sub)+'</td>'+
      '<td class="num mono">$'+(t.paper_entry?fmt(t.paper_entry,4):'—')+'</td>'+
      '<td class="num mono">$'+(t.demo_entry?fmt(t.demo_entry,4):'—')+'</td>'+
      '<td class="num">'+entryDiff+'</td>'+
      '<td class="num mono">'+(t.paper_pnl!=null?usd(t.paper_pnl):'—')+'</td>'+
      '<td>'+demoCell+'</td>'+
    '</tr>';
  }).join('');
  
  var el=document.getElementById('paperdemo-table');
  if(el)table('#paperdemo-table',[
    {label:'Trade'},{label:'Paper Entry',num:true},{label:'Demo Entry',num:true},
    {label:'Slippage',num:true},{label:'Paper P&L',num:true},{label:'Demo'}
  ],rows);
}
