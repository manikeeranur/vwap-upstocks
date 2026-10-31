/* Account-scoped stock workspace. Polling never overwrites unsaved inputs. */
(function () {
  'use strict';
  const el = id => document.getElementById(id);
  const money = value => '₹' + (Number(value) / 100).toLocaleString('en-IN', {minimumFractionDigits:2,maximumFractionDigits:2});
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let state = null, dirty = false, saving = false, refreshing = false, draftRevision = null;
  let intraday = null, statement = null;
  const dataView = WorkspaceView.bind({single:true,descriptions:{
    paper:'Separate simulated portfolio · no broker orders',
    live:'Real broker portfolio · usable capital only'
  },beforeChange:async()=>{
    if (saving) return false;
    if (dirty && !await confirmUI({title:'Discard unsaved settings?',message:'The current portfolio has unsaved changes.',confirmText:'Discard changes'})) return false;
    return true;
  },onChange:value=>{
    el('portfolio').value = value.mode === 'paper' ? 'paper' : value.broker;
    statement = null; el('statement-preview').textContent = ''; el('apply-statement').hidden = true;
    dirty = false; draftRevision = null; el('stock-save-status').textContent = 'No unsaved changes'; error('');
    render();
  }});
  el('portfolio').value = dataView.get().mode === 'paper' ? 'paper' : dataView.get().broker;
  const orderFilters=WorkspaceUI.bindFilters({fields:{q:'order-search',status:'order-status',side:'order-side',day:'order-day'},prefix:'orders_',reset:'clear-order-filters',onChange:()=>{if(state) render();}});
  const selected = () => state?.portfolios.find(p => p.broker === el('portfolio').value);
  function error(message) {
    el('stock-error').hidden = !message;
    el('stock-error').textContent = message || '';
  }
  async function api(path, body) {
    const response = await appFetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed. Refresh status before trying again.');
    return data;
  }
  function eventText(event) {
    let d; try { d = JSON.parse(event.details); } catch { return 'Activity details unavailable'; }
    switch (event.kind) {
      case 'CREATED': return 'Portfolio opened with ' + money(d.cash) + ' allocated.';
      case 'SETTINGS': return 'Usable capital ' + money(d.usable_capital) + ' · automation ' + (d.enabled ? 'enabled' : 'disabled') + ' · entries ' + (d.paused ? 'paused' : 'allowed');
      case 'DECISION': return d.reason;
      case 'BLOCKED': return d.reason;
      case 'LEGACY_BLOCK': return 'Existing live records need verified ownership and fills before activation.';
      case 'ORDER_INTENT': return d.side + ' · ' + d.qty + ' shares requested · reference ' + d.id;
      case 'ORDER_SUBMISSION': return 'Order ' + d.status + ' · ' + (d.broker_id || d.id) + (d.error ? ' · ' + d.error : '');
      case 'ORDER_STATUS': return 'Order ' + d.status + ' · reference ' + d.order_id;
      case 'FILL_APPLIED': return d.qty + ' additional shares filled · value ' + money(d.notional) + ' · cost provision ' + money(d.fee_provision);
      case 'RECONCILED_PAUSED': return 'Broker evidence reconciled. New entries remain paused.';
      case 'RECOVERED_UNKNOWN': return 'Submission was interrupted. Verify order ' + d.order_id + ' with the broker.';
      default: return event.kind.replaceAll('_',' ');
    }
  }
  function render() {
    const p = selected(); if (!p) return;
    const live = p.broker !== 'paper';
    el('automation-label').textContent = live ? 'Enable live orders for this broker' : 'Enable paper simulation';
    el('automation-help').textContent = live ? 'Disabling automation stops new buys and sells. Existing orders continue to reconcile. Global Pause entries also applies.' : 'Runs this simulated portfolio only. No broker funds or manual holdings are used.';
    el('capital-help').textContent = live ? 'Uses at most this limit, available strategy cash and broker cash, allowing for costs. Profits never raise the limit automatically.' : 'The simulation uses only this capital limit and its own paper cash balance. Profits do not raise your limit automatically.';
    el('holding-heading').textContent = live ? 'Live holding & next action' : 'Paper holding & next action';
    el('orders-heading').textContent = live ? 'Live orders & fills' : 'Paper trades & simulated fills';
    el('orders-help').textContent = live ? 'Net results deduct saved charges; broker estimates are labeled. Cash retains conservative cost provisions until final charges are reconciled.' : 'Simulated entries and exits belong only to this paper portfolio. Costs are simulated.';
    el('entry-source-help').textContent = live ? 'Follow the current paper holding or a saved tuned backtest to enter during the next verified trading session, using only your usable capital. A saved backtest must be refreshed through the latest completed session. Monthly exits and rotations continue afterward.' : 'Start at the next monthly decision, or use a saved tuned backtest when available. All entries and exits stay in this simulated portfolio.';
    const preview = state.preview;
    if (preview?.status === 'done') el('ranking-preview').textContent = 'Close of ' + preview.ranking_date + ': ' + preview.top.map(r => r.rank + '. ' + r.symbol).join(' · ');
    else if (preview?.status === 'error') el('ranking-preview').textContent = preview.error;
    else if (preview?.status === 'running') el('ranking-preview').textContent = 'Refreshing completed daily history in the background…';
    el('stock-status').textContent = (live ? p.broker.toUpperCase() + ' · ' + (p.client_id || 'Account not bound') : 'Paper portfolio') +
      ' · ' + (p.enabled ? p.paused || live && state.global_entries_paused ? 'Entries paused; exits continue' : 'Automation enabled' : 'Automation disabled') +
      ' · Last checked: ' + (p.last_checked ? new Date(p.last_checked).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'}) + ' IST' : 'Awaiting first cycle');
    const reserved = p.orders.filter(o => !['complete','cancelled','rejected'].includes(o.status)).reduce((sum,o) => sum + Math.max(0,o.reserved - o.notional - o.fee_provision),0);
    StableDOM.html(el('portfolio-summary'),'<div>' + (live ? 'Live usable-capital limit' : 'Paper capital limit') + '<b>' + money(p.usable_capital) + '</b></div><div>' + (live ? 'Strategy cash after cost provisions' : 'Simulated cash') + '<b>' +
      money(p.cash) + (p.cash_shortfall ? ' · shortfall ' + money(p.cash_shortfall) : '') + '</b></div><div>Outstanding order reservation<b>' + money(reserved) + '</b></div><div>' + (!live ? 'Paper realized · simulated costs' : p.net_realized != null ? 'Live realized net · final charges' : p.estimated_net_realized != null ? 'Live realized net · estimated charges' : 'Live realized net · charges pending') + '<b>' + (live ? (p.net_realized ?? p.estimated_net_realized) == null ? 'Unavailable' : money(p.net_realized ?? p.estimated_net_realized) : money(p.realized)) + '</b></div>');
    if (!dirty) {
      el('usable-capital').value = (p.usable_capital / 100).toFixed(2);
      el('stock-enabled').checked = !!p.enabled;
      el('stock-paused').checked = !!p.paused;
      el('entry-source').innerHTML = (state.entry_sources || []).filter(s => live || s.id !== 'paper').map(s => '<option value="' + esc(s.id) + '">' + esc(s.label) + '</option>').join('');
      el('entry-source').value = p.entry_source;
    }
    const h = p.position;
    el('stock-position').textContent = h ? h.symbol + ' · ' + h.qty + (live ? ' confirmed shares · recorded cost ' : ' simulated shares · cost ') + money(h.cost) : live ? 'No strategy-owned live holding' : 'No simulated holding';
    const loginError = 'Broker login expired. Reconnect this account to reconcile.';
    const visibleIssue = value => value === loginError && !state.market_calendar?.login_alerts_required ? '' : value;
    const issue = (p.blocked === loginError && !state.market_calendar?.login_alerts_required ? 'Unresolved orders need verified broker evidence before new entries.' : p.blocked) || visibleIssue(p.last_error) || state.worker.error || (!state.worker.running || state.worker.stale ? 'Automation worker is unavailable; scheduled trading is stopped.' : '');
    el('stock-attention').hidden=!issue; el('stock-attention').textContent=issue;
    el('stock-plan').textContent = issue || visibleIssue(p.fee_error) || (p.alignment_pending ? 'Current-holding entry is pending · source: ' + p.entry_source + '. Requires a fresh ranking, verified session and entries enabled.' : '') || (p.plan ? p.plan.reason + ' · Decision date ' + p.plan.day : 'Awaiting the first eligible monthly session');
    el('stock-plan').setAttribute('role', issue ? 'alert' : 'status');
    const orders=p.orders.filter(o=>WorkspaceUI.matches({search:o.symbol+' '+o.id+' '+(o.broker_id || ''),side:o.side,state:['complete','cancelled','rejected'].includes(o.status)?o.status:'pending',date:WorkspaceUI.dayIST(o.created_at)},orderFilters.get()));
    el('order-filter-summary').textContent=orders.length+' of '+p.orders.length+' orders · Portfolio totals above are not filtered.';
    StableDOM.html(el('stock-orders'),orders.length ? orders.slice().reverse().map(o => '<tr data-key="'+esc(o.id)+'"><td>' + esc(new Date(o.created_at).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'})) +
      '</td><td>' + esc(o.side) + '</td><td>' + esc(o.symbol) + '</td><td>' + o.filled_qty + ' / ' + o.qty +
      '</td><td>' + esc(o.status) + (o.error ? '<br>' + esc(o.error) : '') + '</td><td>' + money(o.limit_paise) +
      '</td><td><details><summary>Order reference</summary>' + esc(o.id) + '<br>' + esc(live ? o.broker_id || 'Awaiting broker confirmation' : 'Simulated fill') + '</details></td></tr>').join('') :
      '<tr><td colspan="7">'+(live ? 'No live orders match these filters.' : 'No simulated trades match these filters.')+'</td></tr>');
    StableDOM.html(el('stock-events'),p.events.map(e => '<li data-key="'+esc(e.id || e.created_at+'|'+e.kind)+'"><b>' + esc(e.kind.replaceAll('_',' ')) + '</b> · ' +
      esc(new Date(e.created_at).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'})) + '<br>' + esc(eventText(e)) + '</li>').join(''));
    el('save-stock-settings').disabled = saving || !dirty;
    el('reconcile-stocks').disabled = saving || dirty;
    renderRecovery(p);
    for (const id of ['usable-capital','stock-enabled','stock-paused','entry-source']) el(id).disabled = saving;
  }
  async function refresh(clearError = false) {
    if (refreshing || saving) return;
    refreshing = true;
    const done=LoadingUI.begin(['#portfolio-summary','#stock-position'],{initial:!state,kind:'metrics',label:'Updating…',retry:()=>refresh(true)});
    try {
      state = await api('/api/stocks'); if (clearError) error(''); render(); done();
      try { intraday = await api('/api/paper'); renderRecovery(selected()); }
      catch(e) { el('intraday-recoveries').textContent='Intraday recovery status unavailable. Refresh to try again.'; }
    }
    catch (e) { done(e); error('Status refresh failed: ' + e.message + ' Previously shown data may be stale.'); }
    finally { done(); refreshing = false; }
  }
  el('stock-settings').addEventListener('input', () => {
    if (!dirty) draftRevision = selected()?.revision;
    dirty = true; el('reconcile-stocks').disabled = true; el('stock-save-status').textContent = 'Unsaved changes'; el('save-stock-settings').disabled = false;
  });
  LoadingUI.on(el('stock-settings'),'submit', async event => {
    event.preventDefault(); if (saving || !selected()) return;
    const p = selected();
    const body = {broker:p.broker,revision:draftRevision ?? p.revision,usable_capital:Number(el('usable-capital').value),
      enabled:el('stock-enabled').checked,paused:el('stock-paused').checked,entry_source:el('entry-source').value};
    if (p.broker !== 'paper' && body.enabled && !body.paused) {
      const confirmed = await confirmUI({title:'Enable live stock entries?', message:body.entry_source === 'monthly' ? 'Real limit orders can be placed on the next eligible monthly decision.' : 'Real limit orders can enter the current strategy holding during the next verified trading session. Later exits and entries follow the monthly rules.',
        confirmText:'Enable live entries',danger:true,facts:[['Broker',p.broker],['Account',p.client_id || 'Verified at save'],
          ['Entry source',el('entry-source').selectedOptions[0]?.textContent],['Usable-capital limit',money(body.usable_capital * 100)],['Maximum planned purchase',money(Math.min(p.cash,body.usable_capital * 100))],
          ['Global entries',state.global_entries_paused ? 'Paused; must be resumed separately' : 'Allowed']]});
      if (!confirmed) return; body.confirm = 'LIVE';
    }
    saving = true; el('portfolio').disabled = true; render();
    try { state = await api('/api/stocks/config',body); dirty = false; draftRevision = null; error(''); el('stock-save-status').textContent = 'Settings saved'; }
    catch (e) { error(e.message); el('stock-save-status').textContent = 'Not saved'; }
    finally { saving = false; el('portfolio').disabled = false; render(); }
  });
  LoadingUI.on(el('reconcile-stocks'),'click', async () => {
    if (saving || dirty || !selected()) return;
    saving = true; el('portfolio').disabled = true; render();
    try { state = await api('/api/stocks/reconcile',{broker:selected().broker}); dirty = false; error(''); el('stock-save-status').textContent = 'Reconciled; entries remain paused'; }
    catch(e) { error(e.message); }
    finally { saving = false; el('portfolio').disabled = false; render(); }
  });
  LoadingUI.on(el('preview-stocks'),'click', async () => {
    const button = el('preview-stocks'); button.disabled = true; el('ranking-preview').textContent = 'Refreshing completed daily history…';
    try {
      const result = await api('/api/momentum/tuned/preview',{});
      el('ranking-preview').textContent = result.status === 'running' ? 'Refreshing in the background. Status updates automatically.' : result.error || 'Ranking refreshed';
      await refresh();
    } catch(e) { el('ranking-preview').textContent = e.message; }
    finally { button.disabled = false; }
  });
  LoadingUI.on(el('refresh-stocks'),'click', async () => {
    if (dirty && !await confirmUI({title:'Reload saved settings?',message:'This discards the unsaved settings in this portfolio.',confirmText:'Reload settings'})) return;
    dirty = false; draftRevision = null; el('stock-save-status').textContent = 'No unsaved changes'; await refresh(true);
  });

  function renderRecovery(p) {
    const queued = p.recoveries.filter(r => r.status === 'queued');
    const parents = new Set(p.orders.map(o => o.parent_id).filter(Boolean));
    const eligible = p.orders.filter(o => o.side === 'SELL' && ['cancelled','rejected'].includes(o.status) &&
      p.position?.isin === o.isin && !parents.has(o.id) && !queued.some(r => r.parent_id === o.id));
    StableDOM.html(el('stock-recoveries'),queued.map(r => '<p data-key="queue:'+esc(r.id)+'">Queued exit · expires ' + esc(new Date(r.expires_at).toLocaleString('en-IN')) +
      ' <button class="btn ghost" data-cancel-recovery="' + esc(r.id) + '">Cancel queued recovery</button></p>').join('') +
      eligible.map(o => '<p data-key="stock:'+esc(o.id)+'">' + esc(o.symbol) + ' · ' + p.position.qty + ' shares remaining <button class="btn ghost" data-stock-recovery="' + esc(o.id) + '">Review replacement exit</button></p>').join('') ||
      'No cancelled or rejected stock exit needs a replacement.');
    const entries = Object.entries(intraday?.state?.orders?.[p.id] || {}).filter(([,s]) =>
      s.buy?.filled_qty > (s.sell?.filled_qty || 0) && ['cancelled','rejected'].includes(s.sell?.execution_status));
    StableDOM.html(el('intraday-recoveries'),entries.map(([tid,s]) => '<p data-key="intraday:'+esc(tid)+'">' + esc(s.buy.broker_symbol || s.buy.symbol) +
      ' · ' + (s.buy.filled_qty - (s.sell.filled_qty || 0)) + ' units remaining <button class="btn ghost" data-intraday-recovery="' + esc(tid) +
      '" data-order="' + esc(s.sell.order_id) + '">Review replacement exit</button></p>').join('') || 'No cancelled or rejected intraday exit needs a replacement.');
    for (const b of el('execution-recovery').querySelectorAll('button')) b.disabled = saving || dirty;
    el('preview-statement').disabled = saving || dirty || p.broker === 'paper';
  }
  LoadingUI.on(el('execution-recovery'),'click', async event => {
    const button = event.target.closest('button'); if (!button || saving || dirty) return;
    const p = selected(), stock = button.dataset.stockRecovery, cancel = button.dataset.cancelRecovery, tid = button.dataset.intradayRecovery;
    if (!stock && !cancel && !tid) return;
    const remaining = stock ? p.position.qty : tid ? intraday.state.orders[p.id][tid].buy.filled_qty - (intraday.state.orders[p.id][tid].sell.filled_qty || 0) : null;
    if (!await confirmUI({title:cancel ? 'Cancel queued recovery?' : 'Replace the remaining exit?',danger:!cancel,
      message:cancel ? 'This cancels a queued request only. It cannot cancel a submitted broker order.' :
        stock ? 'A fresh bounded sell will be queued for a verified market session. Entries remain paused.' : 'A real sell order can be submitted now after verifying the remaining broker position.',
      facts:[['Broker',p.broker],['Remaining quantity',remaining ?? 'Queued request']],confirmText:cancel ? 'Cancel request' : 'Approve exit'})) return;
    saving = true; el('portfolio').disabled = true; render();
    try {
      if (stock) state = await api('/api/stocks/recovery',{broker:p.broker,revision:p.revision,order_id:stock,confirm:'EXIT'});
      else if (cancel) state = await api('/api/stocks/recovery/cancel',{broker:p.broker,revision:p.revision,recovery_id:cancel});
      else {
        await api('/api/paper/recover-exit',{broker:p.broker,trade_id:tid,order_id:button.dataset.order,confirm:'EXIT'});
        intraday = await api('/api/paper');
      }
      error('');
    } catch(e) { error(e.message); }
    finally { saving = false; el('portfolio').disabled = false; render(); }
  });
  el('statement-form').addEventListener('input', () => { statement = null; el('apply-statement').hidden = true; el('statement-preview').textContent = ''; });
  LoadingUI.on(el('statement-form'),'submit', async event => {
    event.preventDefault(); if (saving || dirty || selected()?.broker === 'paper') return;
    const file = el('statement-file').files[0];
    if (!file || file.size > 262144) { error('Choose a CSV file no larger than 256 KB.'); return; }
    saving = true; el('portfolio').disabled = true; render();
    try {
      statement = await api('/api/reconciliation/preview',{broker:selected().broker,reference:el('statement-reference').value,csv_text:await file.text()});
      el('statement-preview').innerHTML = statement.status === 'applied' ? 'This exact statement was already applied.' :
        '<p>Review ' + statement.rows.length + ' matched strategy orders · ' + esc(statement.reference) + '</p><div class="stock-table"><table><thead><tr><th>Order</th><th>Stock / contract</th><th>Side</th><th>Filled / ordered</th><th>Status</th><th>Fill value</th><th>Final charges</th></tr></thead><tbody>' +
        statement.rows.map(r => '<tr><td>' + esc(r.order_id) + '</td><td>' + esc(r.symbol) + '</td><td>' + esc(r.side) + '</td><td>' + r.filled_qty + ' / ' + r.qty +
          '</td><td>' + esc(r.status) + '</td><td>' + money(r.notional) + '</td><td>' + (r.charges_paise == null ? 'Unavailable' : money(r.charges_paise)) + '</td></tr>').join('') + '</tbody></table></div>';
      el('apply-statement').hidden = statement.status !== 'preview'; error('');
    } catch(e) { statement = null; el('apply-statement').hidden = true; error(e.message); }
    finally { saving = false; el('portfolio').disabled = false; render(); }
  });
  LoadingUI.on(el('apply-statement'),'click', async () => {
    if (!statement || saving || dirty) return;
    if (!await confirmUI({title:'Apply reviewed broker evidence?',message:'Confirm that every previewed row matches your broker statement. This changes recorded fills and charges, leaves stock entries paused, and keeps an audit record.',confirmText:'Apply evidence',danger:true})) return;
    saving = true; el('portfolio').disabled = true; el('apply-statement').disabled = true; render();
    try {
      await api('/api/reconciliation/apply',{document_id:statement.document_id,confirm:'IMPORT'});
      statement = null; el('apply-statement').hidden = true;
      el('statement-preview').textContent = 'Evidence applied. Reconcile with your broker to verify current ownership before resuming entries.'; error('');
      [state,intraday] = await Promise.all([api('/api/stocks'),api('/api/paper')]);
    } catch(e) { error(e.message); }
    finally { saving = false; el('apply-statement').disabled = false; el('portfolio').disabled = false; render(); }
  });
  refresh(); setInterval(() => { if (!document.hidden) refresh(); },10000);
})();
