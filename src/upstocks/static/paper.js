
const $ = id => document.getElementById(id);
const LABELS = { ...window.PAPER_CONFIG.labels, ...STRATEGY_META.labels }, PRESETS = window.PAPER_CONFIG.presets;
const DESCRIPTIONS = STRATEGY_META.descriptions;
const STRATEGY_ICONS = STRATEGY_META.icons;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => Number(n || 0).toFixed(2);
const money = n => `${n < 0 ? '−' : ''}₹${Math.abs(n).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}`;
const ampm = s => { if (!s) return '—'; const [h,m] = s.split(':').map(Number); return `${h % 12 || 12}:${String(m).padStart(2,'0')} ${h >= 12 ? 'PM' : 'AM'}`; };
const todayIST = () => { const p = Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).map(v => [v.type,v.value])); return `${p.year}-${p.month}-${p.day}`; };
const BROKER_NAMES = {upstox:'Upstox', kite:'Kite', indmoney:'INDmoney'}, BROKER_ORDER = ['upstox','kite','indmoney'];
let B = null, brokerError = '', fillsError = '';
function showBrokerErrors() { const errors = [brokerError,fillsError,...(S ? selectedAccounts().flatMap(a => [S.reconciliation?.[a.id]?.error,S.fee_status?.[a.id]?.error]) : [])].filter(Boolean); $('brokerRefreshError').hidden = !errors.length; $('brokerRefreshError').textContent = errors.join(' · '); }
const accounts = () => BROKER_ORDER.map(b => { const e = Object.entries(S.state.accounts || {}).find(([,a]) => a.broker === b); return e && {id:e[0], ...e[1]}; }).filter(Boolean);
const account = b => accounts().find(a => a.broker === b);
const anyLive = p => accounts().some(a => a.live[p]);
const lotSize = p => S.state.lot_sizes?.[p] ?? S.state.lot_size;
const paperOnly = p => (S.state.paper_only || []).includes(p);
const paperLots = () => 1; // Match the paper journal; broker lots never resize simulations.
const openLive = (acc,p) => Object.entries(S.state.orders?.[acc] || {}).filter(([id,o]) => id.startsWith(p + '|') && (o.buy?.filled_qty || 0) > (o.sell?.filled_qty || 0)).length;
async function loadBrokers() { try { B = (await api('/api/brokers')).brokers; brokerError = ''; if (S && !confirming) render(); } catch(e) { brokerError = 'Broker status unavailable: ' + e.message; } finally { showBrokerErrors(); } }
let S = null, busy = false, saving = false, confirming = false, refreshing = false, refreshVersion = 0;
let Q = null, quoting = false, quoteFailed = false;
const lastPrices = new Map();
let sessionTimer=null,sessionDeadline=null;
let executionDetails=null,detailsLoading=false,detailsFetchedAt=0;
async function api(path, body) {
  const r = await appFetch(path, body === undefined ? {quiet:!!S} : {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const error = new Error(typeof j.detail === 'string' ? j.detail : j.error || `Request failed (${r.status}). Please try again.`); error.state = j.state; throw error; }
  return j;
}
const dataView = WorkspaceView.bind({onChange:()=>{ if(S) { render();loadExecutionDetails(); } }});
const tradeFilters = WorkspaceUI.bindFilters({fields:{strategy:'strategy-filter',status:'position-filter',q:'trade-search'},reset:'clear-trade-filters',onChange:()=>{if(S) render();}});
const matchPaper = (t,p) => WorkspaceUI.matches({...t,preset:p,state:t.open?'open':'closed',search:(t.symbol || '')+' '+(LABELS[p] || p)},tradeFilters.get());
const matchExecution = r => WorkspaceUI.matches({...r,state:[...(r.stats.pending?['pending']:[]),...(r.stats.open?['open']:r.buy?.filled_qty>0?['closed']:[])],search:r.symbol+' '+(LABELS[r.preset] || r.preset)},tradeFilters.get());
const liveView = () => dataView.get().mode === 'live';
const selectedAccounts = () => accounts().filter(a => dataView.get().broker === 'all' || a.broker === dataView.get().broker);
const executions = () => {
  const prices = {};
  for (const t of Object.values(S.view.results || {}).flat()) {
    const m = tradeValues(t);
    if (m.quote && !m.stale && S.view.day === todayIST()) prices[t.id] = m.price;
  }
  return IntradayView.executions(UpstocksPnl.executionRows(S.state,Object.values(S.view.results || {}).flat(),prices,dataView.get().broker),S.intraday_session);
};
function renderLiveSummary() {
  const rows = executions(), total = UpstocksPnl.executionSummary(rows);
  const closed = rows.filter(r=>r.stats.closed && !r.stats.open && !r.stats.pending);
  const realized = UpstocksPnl.executionSummary(closed);
  const write = (id,value) => { $(id).textContent = value == null ? '—' : money(value); $(id).className = 'metric-value ' + (value == null ? '' : value >= 0 ? 'up' : 'down'); };
  const emptySession=!rows.length&&S.intraday_session?.day;
  write('livePnl',realized.closed ? realized.realized : emptySession ? 0 : null); write('liveNet',emptySession ? 0 : realized.net);
  write('liveOpen',total.open && !total.valuationMissing ? total.valuation : null);
  $('liveCaption').textContent = realized.closed + (realized.closed === 1 ? ' closed trade' : ' closed trades') + ' · ' + sessionLabel();
  $('netCaption').textContent = (realized.net != null ? (realized.chargesFinal ? 'After final statement charges' : 'Net estimate · after broker-calculated charges') : realized.closed ? 'Waiting for broker charges' : 'No closed trades this session') + (rows.length > closed.length ? ' · open/unresolved trades excluded' : '');
  $('openValuation').textContent = total.positions ? total.positions + (total.positions === 1 ? ' open position · ' : ' open positions · ') + (total.valuationMissing ? 'current quotes unavailable' : 'current quote estimate · before charges') : 'No confirmed open positions';
  $('livePending').textContent = total.pending;
  $('pendingCaption').textContent = total.pending ? 'Unresolved orders · verify with the broker' : 'No unresolved saved orders';
  StableDOM.html($('liveBrokers'),selectedAccounts().map(a => {
    const r = UpstocksPnl.executionSummary(closed.filter(x=>x.account === a.id));
    const activity = UpstocksPnl.executionSummary(rows.filter(x=>x.account === a.id));
    return '<div class="live-broker"><span>'+BROKER_NAMES[a.broker]+'</span><b>'+(r.net != null ? money(r.net) : r.closed ? money(r.realized) : '—')+'</b><small>'+(r.net != null ? (r.chargesFinal ? 'net' : 'net estimate') : r.closed ? 'gross; fees pending' : 'No closed trades')+' · '+activity.positions+' open · '+activity.pending+' unresolved</small></div>';
  }).join(''));
}
function executionClock(value) {
  if (!value || !Number.isFinite(Date.parse(value))) return 'Time unavailable';
  return new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value));
}
function savedOrderTime(order,day,last=false) {
  const times=[...(order?.attempts || []),order].filter(a=>a && (a.attempt_filled_qty ?? a.filled_qty)>0).map(a=> {
    let value=a.created_at || (a.at ? day+'T'+a.at+'+05:30' : null);
    if(value && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) value+='+05:30';
    return value && Number.isFinite(Date.parse(value)) ? value : null;
  }).filter(Boolean).sort((a,b)=>Date.parse(a)-Date.parse(b));
  return times.length ? times[last ? times.length-1 : 0] : null;
}
function executionCard(r) {
  const bought = Number(r.buy?.filled_qty || 0), sold = Number(r.sell?.filled_qty || 0);
  const state = r.stats.pending ? 'pending' : r.stats.open ? 'open' : bought ? 'closed' : 'empty';
  const status = {pending:'Needs reconciliation',open:'Open position',closed:'Closed',empty:'No confirmed fills'}[state];
  const net = r.stats.net, netTone = net == null ? 'muted' : net >= 0 ? 'up' : 'down';
  const estimated = net != null && !r.stats.chargesFinal;
  const info = executionDetails?.day===S.intraday_session?.day ? executionDetails.accounts?.[r.account]?.[r.id] : null;
  const validInfo = info && info.buy_qty===bought && info.sell_qty===sold && info.buy_price===r.buy?.fill_price && info.sell_price===(r.sell?.fill_price ?? null) ? info : null;
  const start = validInfo?.entry_at || savedOrderTime(r.buy,r.date), end = validInfo?.exit_at || savedOrderTime(r.sell,r.date,true);
  const points = bought && sold && Number.isFinite(r.buy?.fill_price) && Number.isFinite(r.sell?.fill_price) ? r.sell.fill_price-r.buy.fill_price : null;
  const pointText = value => (value>0?'+':value<0?'−':'')+fmt(Math.abs(value))+' pts';
  const field = (label,value,sub='',tone='') => `<div><dt>${label}</dt><dd class="${tone}">${value}</dd>${sub ? `<dd class="execution-sub">${sub}</dd>` : ''}</div>`;
  const price = (o) => o?.filled_qty>0 && Number.isFinite(o.fill_price) ? money(o.fill_price) : '—';
  const peak = (label,value) => field(label,value ? money(value.price) : '—', value ? `<span>${esc(executionClock(value.at))}${value.source==='entry' ? ' · Entry' : ''}</span><span class="${value.points>=0?'up':'down'}">${pointText(value.points)}</span>` : validInfo?.status==='incomplete' ? 'Incomplete candles' : (detailsLoading || !detailsFetchedAt) && !validInfo ? 'Loading…' : 'Candles unavailable');
  return `<article data-key="${esc(r.account+'|'+r.id)}" class="execution-card">
    <header class="execution-head"><div class="execution-identity"><h4>${esc(r.symbol)}</h4><div class="execution-meta"><span class="execution-broker">${brokerLogo(r.broker,16)}${esc(BROKER_NAMES[r.broker] || r.broker)}</span><span>${esc(r.date)}</span>${r.carryover ? '<span class="execution-carryover">Prior-session position</span>' : ''}${r.client_id ? `<span class="execution-account">${esc(r.client_id)}</span>` : ''}</div></div><span class="execution-status ${state}">${status}</span></header>
    <dl class="execution-facts">
      ${field('Entry',price(r.buy),start ? 'Order · '+esc(executionClock(start)) : 'Time unavailable')}
      ${field(r.stats.open && sold ? 'Partial exit' : 'Exit',price(r.sell),end ? 'Order · '+esc(executionClock(end)) : r.stats.open ? 'Not exited' : 'Time unavailable')}
      ${peak('Peak high · 1m',validInfo?.high)}${peak('Peak low · 1m',validInfo?.low)}
      ${field('Points',points==null?'—':pointText(points),sold ? 'Realized · before charges' : 'Available after exit',points==null?'muted':points>=0?'up':'down')}
      ${field('Bought / sold',bought+' / '+sold,'Remaining '+r.stats.open)}
      ${field('Net P&L',net==null?'—':money(net),estimated ? 'Estimated · after charges' : net!=null ? 'After final charges' : r.stats.pending ? 'Awaiting reconciliation' : r.stats.open ? 'Available after exit' : 'Charges pending',netTone)}
    </dl>
    <details class="order-evidence"><summary><span>Details <span class="details-chevron" aria-hidden="true">›</span></span><span class="execution-fees">Gross ${r.stats.closed?money(r.stats.realized):'—'}<span>Charges ${r.stats.chargesComplete?money(r.stats.charges):'Pending'}${!r.stats.chargesFinal&&r.stats.chargesComplete?' · est.':''}</span></span></summary>
      <p class="execution-basis">Times in IST. Entry and exit use saved order times; exact fill times are unavailable. Peaks include the entry and exit minute candles.${validInfo?.through ? ' Candles through '+esc(executionClock(validInfo.through))+'.' : ''}</p>
      <div class="execution-orders"><section><h5>Buy order</h5><p>${orderCell(r.buy)}</p></section><section><h5>Sell order</h5><p>${orderCell(r.sell)}</p></section></div>
    </details>
  </article>`;
}
async function loadExecutionDetails() {
  if(!S || !liveView() || detailsLoading || document.hidden || Date.now()-detailsFetchedAt<30000) return;
  detailsLoading=true;
  const day=S.intraday_session?.day;
  try {
    const next=await api('/api/paper/execution-details');
    if(S?.intraday_session?.day===day && next.day===day) executionDetails=next;
  } catch { /* Keep saved fills visible when candle analytics are unavailable. */ }
  finally { detailsLoading=false;detailsFetchedAt=Date.now();if(S && liveView() && !confirming) render(); }
}

function liveStrategy(p) {
  const rows = executions().filter(r=>r.preset === p && matchExecution(r)), summary = UpstocksPnl.executionSummary(rows.filter(r=>r.stats.closed && !r.stats.open && !r.stats.pending));
  const enabled = selectedAccounts().some(a=>a.live[p]);
  const entryStatus = paperOnly(p) ? 'Paper only' : !enabled ? 'New entries disabled' : S.state.entries_paused ? 'Live entries paused' : 'Live automation enabled';
  const active = enabled && !paperOnly(p) && !S.state.entries_paused;
  const total = summary.net != null ? summary.net : summary.closed ? summary.realized : null;
  const totalTone = total == null ? 'muted' : total >= 0 ? 'up' : 'down';
  const totalLabel = summary.net != null ? (summary.chargesFinal ? 'Closed net P&L' : 'Closed net P&L · estimated') : 'Closed gross P&L · charges pending';
  const icon = STRATEGY_ICONS[PRESETS.indexOf(p)] || STRATEGY_ICONS[0];
  const cards = rows.map(executionCard).join('');
  const exits = selectedAccounts().map(a => { const n = openLive(a.id,p); return n ? '<button class="btn ghost sq" type="button" data-sq="'+p+'" data-broker="'+a.broker+'">Close all '+n+' live position'+(n===1?'':'s')+' · '+BROKER_NAMES[a.broker]+'</button>' : ''; }).join('');
  return `<section data-key="${esc(p)}" class="card strat live-strategy" aria-label="${esc(LABELS[p] || p)} live execution">
    <header class="strategy-top"><div class="strategy-title"><span class="strategy-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="${icon}"/></svg></span><div><h3>${esc(LABELS[p] || p)}</h3><div class="strategy-meta"><span>${rows.length} broker trade${rows.length===1?'':'s'}</span><span class="entry-status${active ? ' enabled' : ''}">${entryStatus}</span></div></div></div>
      <div class="strategy-pnl"><span class="pnl-label">${totalLabel}</span><b class="${totalTone}">${total == null ? '—' : money(total)}</b></div>
    </header>
    ${exits ? '<div class="strategy-controls">'+exits+'</div>' : ''}
    ${cards ? '<div class="execution-cards">'+cards+'</div>' : '<div class="empty-state"><div><b>No trades this session</b><p>New fills will appear here.</p></div></div>'}
  </section>`;
}
function orderCell(o) {
  if (!o) return '—';
  const status = o.execution_status || (o.order_id ? 'Acknowledged; awaiting fills' : 'Pending confirmation');
  return '<span' + (o.error ? ' class="err"' : '') + '>' + esc(status) + ' · ' +
    Number(o.filled_qty || 0) + ' / ' + Number(o.total_qty || o.qty || 0) + ' filled</span>' +
    (o.error ? '<br>' + esc(o.error) : '') + '<br><span class="muted">' + esc(o.order_id || o.client_tag || '') + '</span>' + (['cancelled','rejected'].includes(o.execution_status) ? ' <a href="/stocks#execution-recovery">Review recovery</a>' : '');
}
function tradeValues(t) {
  const open = t.open && S.market_open && S.view.day === todayIST();
  const q = open && Q?.day === S.view.day ? Q.trades?.[t.id] : null;
  const stale = open && (!q || q.stale || quoteFailed || Date.now() - Date.parse(q.received_at) > 4000);
  return {price:q?.ltp ?? t.exit, pnl:q?.pnl ?? t.pnl, open, quote:q, stale};
}
function priceMarkup(t) {
  const m = tradeValues(t);
  return `<span data-trade-price="${esc(t.id)}">₹${fmt(m.price)}</span>${t.open ? `<small data-quote-tag="${esc(t.id)}" class="quote-tag${m.stale ? ' delayed' : ''}">${m.quote ? (m.stale ? 'Delayed' : 'LTP') : 'Candle'}</small>` : ''}`;
}
function updateLiveValues() {
  if (!S) return;
  const marks = new Map(), totals = {}; let total = 0, open = 0, delayed = false, running = 0, wins = 0, losses = 0;
  PRESETS.forEach(p => {
    const qty = paperLots(p) * lotSize(p);
    totals[p] = 0;
    for (const t of S.view.results?.[p] || []) {
      const m = {...tradeValues(t), qty, trade:t}; marks.set(t.id,m); totals[p] += m.pnl * qty;
      if (m.open) { open++; delayed ||= m.stale; running += m.pnl * qty; }
      else if (t.pnl > 0) wins++; else losses++;
    }
    total += totals[p];
  });
  const text = (node,value) => { if (node.textContent !== value) node.textContent = value; };
  const color = (node,pnl) => { node.classList.toggle('up',pnl >= 0); node.classList.toggle('down',pnl < 0); };
  for (const node of document.querySelectorAll('[data-trade-price]')) {
    const m = marks.get(node.dataset.tradePrice); if (!m) continue;
    const before = lastPrices.get(node.dataset.tradePrice); lastPrices.set(node.dataset.tradePrice,m.price);
    if (m.open && before != null && before !== m.price) {   // restart the flash on every tick
      node.classList.remove('tick-up','tick-down'); void node.offsetWidth; node.classList.add(m.price > before ? 'tick-up' : 'tick-down');
    }
    text(node,`₹${fmt(m.price)}`);
    node.title = m.quote ? `${m.stale ? 'Delayed quote. ' : ''}Received ${new Date(m.quote.received_at).toLocaleTimeString('en-IN',{timeZone:'Asia/Kolkata',hour12:false})} IST` : 'Price from the latest strategy candle';
  }
  for (const node of document.querySelectorAll('[data-quote-tag]')) {
    const m = marks.get(node.dataset.quoteTag); if (!m) continue;
    text(node,m.quote ? (m.stale ? 'Delayed' : 'LTP') : 'Candle'); node.classList.toggle('delayed',m.stale);
  }
  for (const node of document.querySelectorAll('[data-trade-points],[data-trade-pnl]')) {
    const m = marks.get(node.dataset.tradePoints || node.dataset.tradePnl); if (!m) continue;
    text(node,node.hasAttribute('data-trade-points') ? `${fmt(m.pnl)}${node.dataset.unit || ''}` : money(m.pnl * m.qty)); color(node,m.pnl);
  }
  for (const node of document.querySelectorAll('[data-strategy-pnl]')) {
    const p=node.dataset.strategyPnl; const pnl=(S.view.results?.[p] || []).filter(t=>matchPaper(t,p)).reduce((sum,t)=>sum+tradeValues(t).pnl*paperLots(p)*lotSize(p),0); text(node,Object.hasOwn(S.view.results || {},node.dataset.strategyPnl) ? money(pnl) : '—'); color(node,pnl);
  }
  for (const node of document.querySelectorAll('[data-trade-range]')) {
    const m = marks.get(node.dataset.tradeRange); if (!m) continue;
    const pos = rangePos(m.trade,m.price); if (pos && node.style.getPropertyValue('--pos') !== pos) node.style.setProperty('--pos',pos); color(node,m.pnl);
  }
  if (S.view.day) { text($('totalPnl'),money(total)); color($('totalPnl'),total); }
  const errorCount = Object.keys(S.view.errors || {}).length;
  text($('pnlCaption'),errorCount ? `Incomplete scan · ${errorCount} ${errorCount === 1 ? 'error' : 'errors'}` : open ? `Booked ${money(total - running)} · Running ${money(running)}` : 'Simulated · before charges');
  text($('tradeCaption'),sessionLabel() + (wins + losses ? ` · ${wins}W / ${losses}L` : ''));
  text($('openCaption'),open ? `Running ${money(running)}${delayed ? ' · delayed' : ''}` : 'Across all strategies');
  $('openCaption').className = open ? (running >= 0 ? 'up' : 'down') : '';
  renderLiveSummary();
  const previous = S.intraday_session?.phase==='previous';
  const label = !S.intraday_session?.day ? 'Calendar unavailable' : liveView() || previous ? sessionLabel() : !S.market_open ? 'Today · Market closed' : open ? (delayed ? 'Prices delayed · retrying' : 'Market prices · 1s') : 'Today · Awaiting entry';
  text($('refreshHint'),label); $('refreshHint').classList.toggle('quote-delayed',!liveView() && !!open && delayed);
  $('refreshHint').classList.toggle('feed-live',!liveView() && !!open && !delayed);
  $('refreshHint').title = liveView() ? 'Active trading session. Resets at 08:30 IST on trading days. Prior open or unresolved positions remain visible.' : open && delayed ? Q?.error || 'Waiting for a fresh price from Upstox.' : 'Open trade prices and running P&L refresh every second.';
}
async function refreshQuotes() {
  if (!S || quoting || document.hidden) return;
  quoting = true;
  try {
    const next = await api('/api/paper/quotes'); Q = next; quoteFailed = false;
    if (next.view_revision !== S.view_revision || (next.status === 'market_closed' && S.market_open)) await refresh();
  } catch { quoteFailed = true; }
  finally { quoting = false; updateLiveValues(); }
}
// Max profit / max loss: the P&L at the option's highest / lowest price between entry and exit.
const peakCell = (price, t, qty) => price == null ? '—' : `<b class="${price >= t.entry ? 'up' : 'down'}">${money((price - t.entry) * qty)}</b> <span class="muted">@ ₹${fmt(price)}</span>`;
const STATUS_TEXT = {OPEN:'Open', SL:'Stop-loss hit', TARGET:'Target hit', TIME_EXIT:'Time exit', EOD:'Session close'};
// Where a price sits between stop-loss (0%) and target (100%); null when the trade has no fixed stop.
const rangePos = (t,price) => t.sl > 0 && t.target > t.sl ? Math.min(100,Math.max(0,(price - t.sl) / (t.target - t.sl) * 100)).toFixed(1) + '%' : null;
// Open trades first, then the most recent entries.
const tradeOrder = (a,b) => (b.open ? 1 : 0) - (a.open ? 1 : 0) || String(b.entry_time || '').localeCompare(String(a.entry_time || ''));
function paperTrade(t, qty) {
  const m = tradeValues(t), st = t.open ? 'OPEN' : t.status, pc = m.pnl >= 0 ? 'up' : 'down', id = esc(t.id);
  const field = (label,value,sub='') => `<div><dt>${label}</dt><dd>${value}</dd>${sub ? `<dd class="paper-sub">${sub}</dd>` : ''}</div>`;
  const entryPos = t.open && rangePos(t,t.entry);
  const range = entryPos ? `<div class="trade-range ${pc}" data-trade-range="${id}" style="--entry:${entryPos};--pos:${rangePos(t,m.price)}" role="img" aria-label="Price between stop-loss ₹${fmt(t.sl)} and target ₹${fmt(t.target)}"><span>SL ₹${fmt(t.sl)}</span><div class="range-track"><i class="range-fill"></i><i class="range-entry" title="Entry ₹${fmt(t.entry)}"></i><i class="range-mark"></i></div><span>Target ₹${fmt(t.target)}</span></div>` : '';
  return `<article class="paper-trade${t.open ? ' is-open' : ''}" data-key="${id}">
    <header class="paper-trade-head"><div class="paper-trade-id"><h4 class="${t.type === 'PE' ? 'opt-pe' : 'opt-ce'}">${esc(t.symbol)}</h4><span class="paper-status ${esc(st)}">${esc(STATUS_TEXT[st] || st)}</span></div>
      <div class="paper-trade-pnl"><b data-trade-pnl="${id}" class="${pc}">${money(m.pnl * qty)}</b><span data-trade-points="${id}" data-unit=" pts" class="${pc}">${fmt(m.pnl)} pts</span></div></header>
    <dl class="paper-facts">${field('Entry',`₹${fmt(t.entry)}`,ampm(t.entry_time))}${field(t.open ? 'Last price' : 'Exit',priceMarkup(t),t.open ? 'Updates every second' : ampm(t.exit_time))}${field('Stop-loss',t.sl > 0 ? `₹${fmt(t.sl)}` : 'None',t.sl > 0 ? '' : 'Trailing / time exit')}${field('Target',`₹${fmt(t.target)}`)}${field('Max profit',peakCell(t.peak_high,t,qty))}${field('Max loss',peakCell(t.peak_low,t,qty))}</dl>
    ${range}
  </article>`;
}
function render() {
  if (confirming) return;
  const {state,view} = S;
  $('sessionContext').textContent = liveView() ? '' : sessionLabel()+' · 1 lot per trade · Before charges';
  showBrokerErrors();
  const liveCount = PRESETS.filter(p=>selectedAccounts().some(a=>a.live[p])).length;
  const missing = B && S.market_calendar?.login_alerts_required ? selectedAccounts().filter(a => PRESETS.some(p => a.live[p]) && !B[a.broker]?.logged_in).map(a => BROKER_NAMES[a.broker]) : [];
  $('loginBanner').hidden = !missing.length;
  $('loginText').textContent = missing.length ? `Live is ON but you are not logged in to ${missing.join(' and ')} today — those orders will be skipped.` : '';
  $('modeBadge').textContent = liveView() ? state.entries_paused ? 'Live entries paused' : liveCount+(liveCount===1?' strategy':' strategies')+' enabled for live orders' : PRESETS.filter(p=>state.config[p].on).length+' paper strategies enabled';
  $('modeBadge').className = 'mode-badge'+(liveView() ? ' live' : '');
  $('strategy-heading').textContent = liveView() ? 'Live strategy execution' : 'Paper strategies';
  $('settingsLabel').textContent = liveView() ? 'Live trading settings' : 'Paper settings';
  $('settingsBtn').href = '/settings#' + (liveView() ? 'live' : 'paper');
  $('pauseEntries').hidden = typeof state.revision !== 'number';
  $('pauseEntries').textContent = state.entries_paused ? 'Resume all live entries' : 'Pause all live entries';
  $('pauseEntries').disabled = saving;
  $('pauseEntries').setAttribute('aria-pressed', String(!!state.entries_paused));
  let pnl = 0, tradesCount = 0, openCount = 0;
  PRESETS.forEach(p => { const trades = view.results?.[p] || []; pnl += trades.reduce((a,t) => a + t.pnl * paperLots(p) * lotSize(p),0); tradesCount += trades.length; openCount += trades.filter(t => t.open).length; });
  $('totalPnl').textContent = view.day ? money(pnl) : '—'; $('totalPnl').className = 'metric-value' + (view.day ? (pnl >= 0 ? ' up' : ' down') : '');
  $('totalTrades').textContent = view.day ? tradesCount : '—'; $('openTrades').textContent = view.day ? openCount : '—';
  $('activeStrats').textContent = `${PRESETS.filter(p => state.config[p].on).length} / ${PRESETS.length}`;
  $('activeCaption').textContent = 'Paper scanning · 1 simulated lot per trade';
  {
    // Put trades and errors first; keep idle strategies available without filling the overview.
    const known = executions().map(r=>r.preset);
    let shown = liveView() ? [...new Set([...shownPresets(),...known])].filter(p=>known.includes(p) || selectedAccounts().some(a=>a.live[p])) : shownPresets().sort((a,b)=>{ const openCount = p => (view.results?.[p] || []).filter(t=>t.open).length; return openCount(b)-openCount(a) || (view.results?.[b]?.length || 0)-(view.results?.[a]?.length || 0); });
    const filter=tradeFilters.get(), filtering=Object.values(filter).some(Boolean);
    if (filtering) shown=shown.filter(p=>liveView() ? executions().some(r=>r.preset===p && matchExecution(r)) : (view.results?.[p] || []).some(t=>matchPaper(t,p)) || !filter.q && !filter.status && p===filter.strategy);
    $('trade-filter-summary').textContent = shown.length+(shown.length===1?' strategy':' strategies')+' shown'+(filtering?' · Filters apply to the list; summary totals above cover the full selected mode and broker.':' · Summary totals cover the selected mode and broker.');
    const cards = liveView() ? shown.map(liveStrategy).join('') || '<div class="card empty-state"><div><b>No trades this session</b><p>New strategy fills will appear here.</p></div></div>' : shown.map(p => { const index = PRESETS.indexOf(p);
      const cfg = state.config[p], trades = (view.results?.[p] || []).filter(t=>matchPaper(t,p)), err = view.errors?.[p] || view.errors?.scan;
      const qty = paperLots(p) * lotSize(p), pts = trades.reduce((a,t) => a + t.pnl,0), open = trades.filter(t => t.open).length;
      const closed = trades.filter(t => !t.open), wins = closed.filter(t => t.pnl > 0).length;
      const meta = [`${trades.length} trade${trades.length===1?'':'s'}`, ...(open ? [`<span class="paper-live">${open} open</span>`] : []), ...(closed.length ? [`${wins}W / ${closed.length - wins}L`] : [])].map(s => `<span>${s}</span>`).join('');
      return `<section class="card strat${trades.length ? ' paper-strategy' : ''}" data-key="${p}" aria-label="${esc(LABELS[p] || p)}">
        <div class="strategy-top"><div class="strategy-title"><span class="strategy-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="${STRATEGY_ICONS[index]}"/></svg></span><div><h3>${esc(LABELS[p] || p)}</h3><p>${DESCRIPTIONS[p]}</p>${trades.length ? `<div class="strategy-meta">${meta}</div>` : ''}</div></div><div class="strategy-pnl"><span class="pnl-label">Paper P&amp;L</span><b data-strategy-pnl="${p}" class="${pts >= 0 ? 'up' : 'down'}">${Object.hasOwn(view.results || {},p) ? money(pts * qty) : '—'}</b>${trades.length ? '' : '<small>No trades</small>'}</div></div>

        ${err ? `<p class="strategy-error" role="status">${esc(err)}</p>` : ''}
        ${trades.length ? `<div class="paper-trades">${[...trades].sort(tradeOrder).map(t => paperTrade(t,qty)).join('')}</div>` : `<div class="empty-state"><span class="empty-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/></svg></span><div><b>${!cfg.on ? 'Strategy paused' : err ? 'Market data unavailable' : !Object.hasOwn(view.results || {},p) ? 'No trades this session' : 'Awaiting signal'}</b><p>${!cfg.on ? 'Enable Paper to start tracking signals.' : err ? 'Check the message above and try scanning again.' : !Object.hasOwn(view.results || {},p) ? 'Waiting for the next scan.' : view.day ? `No trades for ${esc(view.day)}.` : ''}</p></div></div>`}
      </section>`;
    });
    if (liveView()) StableDOM.html($('strats'),cards);
    else {
      const hasActivity = i => view.results?.[shown[i]]?.length || view.errors?.[shown[i]] || view.errors?.scan;
      const active = cards.filter((_,i)=>hasActivity(i)), idle = cards.filter((_,i)=>!hasActivity(i));
      const expanded = $('strats').querySelector('.idle-strategies')?.open;
      StableDOM.html($('strats'),active.join('') + (idle.length ? '<details class="idle-strategies"' + (expanded ? ' open' : '') + '><summary>Strategies without trades ('+idle.length+')</summary><div class="strategy-list">'+idle.join('')+'</div></details>' : '') || '<div class="card">No trades match these filters. Clear filters to see all strategies.</div>');
    }
  }
  $('strats').setAttribute('aria-busy','false');
  updateLiveValues();
}
const editingConfig = () => S && document.activeElement?.matches('#strats input');
// Harmonic, then EMA pullback, then the rest.
const shownPresets = () => { const first = ['harmonic','harmonic_2y','harmonic_power','ema_pullback','sensex_swing','sensex_bigleg'].filter(p => PRESETS.includes(p)); return [...first, ...PRESETS.filter(p => !first.includes(p))]; };
function sessionLabel() {
  const session=S?.intraday_session;
  if(!session?.day)return 'Trading calendar unavailable';
  const date=new Intl.DateTimeFormat('en-IN',{day:'numeric',month:'short',timeZone:'Asia/Kolkata'}).format(new Date(session.day+'T12:00:00+05:30'));
  return (session.phase==='current'?'Today':'Previous session')+' · '+date;
}
function scheduleSessionReset() {
  clearTimeout(sessionTimer);
  const delay=IntradayView.resetDelay(S?.intraday_session);
  sessionDeadline=delay===null?null:performance.now()+delay;
  if(delay!==null)sessionTimer=setTimeout(checkSessionReset,Math.min(delay,2147483647));
}
function checkSessionReset() {
  if(sessionDeadline===null||performance.now()<sessionDeadline)return false;
  const next=IntradayView.rollover(S);
  clearTimeout(sessionTimer);sessionDeadline=null;
  if(next===S)return false;
  refreshVersion++;S=next;Q=null;executionDetails=null;detailsFetchedAt=0;
  render();refresh();
  return true;
}
async function refresh() {
  if (refreshing || busy || saving || confirming || editingConfig()) return;
  refreshing = true; const version = refreshVersion;
  const done=LoadingUI.begin('#strats',{initial:!S,kind:'cards',label:'Updating…',retry:refresh});
  try { const next = await api('/api/paper?intraday=true'); if (version !== refreshVersion || confirming || editingConfig()) return; S = next; scheduleSessionReset(); $('connection').hidden = true; render(); }
  catch (e) { done(e); if (version !== refreshVersion) return; $('connectionText').textContent = 'Unable to update. Showing the last available data. ' + e.message; $('connection').hidden = false; $('refreshHint').textContent = 'Connection interrupted'; }
  finally { done(); refreshing = false; if (version !== refreshVersion && !busy && !saving) refresh(); }
}
async function confirmAction(options) {
  if (confirming) return false;
  confirming = true; refreshVersion++;
  try { return await confirmUI(options); }
  finally { confirming = false; }
}
function restore(body, input) {
  if (!input || !S) return;
  const acc = body.broker && account(body.broker);
  if (input.type === 'checkbox') input.checked = body.live !== undefined ? !!acc?.live[body.preset] : S.state.config[body.preset].on;
  else input.value = body.lots !== undefined ? acc?.lots[body.preset] ?? 1 : S.state.lot_size;
}
async function saveConfig(body, input) {
  if (!S) return;
  if (saving || confirming || (busy && body.entries_paused === undefined)) { restore(body,input); notifyUI('Please wait for the current action to finish.'); return; }
  const done=LoadingUI.button(input || $('pauseEntries'));
  saving = true; refreshVersion++;
  try {
    const result = await api('/api/paper/config', {...body, revision:S.state.revision}); if (S) S.state = result.state;
    if (input?.type === 'checkbox') restore(body,input);
    notifyUI(body.entries_paused === true ? 'New live entries paused. Paper scans and exits continue.' : body.entries_paused === false ? 'Live entries resumed for enabled strategies.' : 'Strategy settings saved.');
  } catch(e) {
    if (e.state && S) S.state = e.state;
    restore(body,input);
    notifyUI(e.message,true);
  } finally { done();saving = false; if (S) render(); }
}
LoadingUI.on($('pauseEntries'),'click', async () => {
  if (!S || saving || confirming) return;
  const paused = !S.state.entries_paused;
  const enabled = PRESETS.filter(anyLive);
  if (!paused && enabled.length && !await confirmAction({
    title:'Resume live entries?', message:'Enabled intraday and stock portfolios can place new real orders again. Existing exits continue.',
    confirmText:'Resume entries', danger:true, facts:[['Strategies',enabled.map(p => LABELS[p]).join(', ')]]
  })) return;
  await saveConfig({entries_paused:paused, ...(!paused ? {confirm:'LIVE'} : {})});
});
$('retryBtn').addEventListener('click',refresh);
LoadingUI.on($('strats'),'click', async e => {
  const button = e.target.closest('[data-sq]'); if(!button || busy || saving || confirming) return;
  const p = button.dataset.sq, broker = button.dataset.broker, positions = openLive(account(broker)?.id,p);
  if(!await confirmAction({
    title:'Close live positions?', message:'Send market sell orders for all open live positions in this strategy.',
    confirmText:'Send sell orders', danger:true, facts:[['Strategy',LABELS[p]],['Broker',BROKER_NAMES[broker]],['Open positions',String(positions)]]
  })) return;
  saving = true; refreshVersion++; button.disabled = true;
  try { const result = await api('/api/paper/squareoff',{preset:p,broker}); notifyUI(`${result.sold} sell order(s) sent. See live order activity.`); }
  catch(err) { notifyUI(err.message,true); }
  finally { saving = false; button.disabled = false; await refresh(); }
});
$('strats').addEventListener('focusout',() => { setTimeout(() => { if(S && !saving && !confirming && !$('strats').contains(document.activeElement)) render(); },0); });
document.addEventListener('visibilitychange',() => { if(!document.hidden) { checkSessionReset(); refresh(); refreshQuotes(); } });
refresh().then(()=>{ refreshQuotes();loadExecutionDetails(); }); loadBrokers();

setInterval(() => { if(!document.hidden) loadBrokers(); },60000);
setInterval(() => { if(!document.hidden) { refresh();loadExecutionDetails(); } },10000);
setInterval(() => { if(!document.hidden) { checkSessionReset(); updateLiveValues(); refreshQuotes(); } },1000);
