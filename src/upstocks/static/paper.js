
const $ = id => document.getElementById(id);
const LABELS = { ...window.PAPER_CONFIG.labels, ema25_vwap: "VWAP + EMA25", ema_pullback: "EMA pullback", smc_ema_combo: "SMC + EMA (1/day)" }, PRESETS = window.PAPER_CONFIG.presets;
const DESCRIPTIONS = { frontend_live:'Opening range · VWAP signals', smc_live:'Smart money concepts', ema25_vwap:'Trend confirmation · EMA25', ema_pullback:'Momentum pullback · EMA9 / EMA26', smc_tuned:'SMC · tuned, 1 trade a day', smc_ema_combo:'SMC or EMA pullback · first signal, 1 trade a day', harmonic:'0.58 Fib pullback · ITM-400 · 1 trade a day · no trades on expiry day', harmonic_2y:'0.60 Fib pullback · 0.20% swings · ITM-400 · 1 trade a day 09:45–14:30 · SL 20% · profitable in both test years', sensex_swing:'SENSEX · 0.45 Fib · 0.25% swings · ITM-200 · leg ≥ 200 · 09:30–10:45 · paper only', sensex_bigleg:'SENSEX · 0.45 Fib · 0.15% swings · ITM-200 · leg ≥ 400 · 09:30–11:15 · paper only', harmonic_power:'Harmonic Optimized entry · Elliott-wave trail: exit if wave 4 overlaps wave 1, book at wave 5 = wave 1' };
const STRATEGY_ICONS = ['M3 17l5-7 5 4 8-10M16 4h5v5','M4 8h16M4 16h16M8 4v8m8 0v8','M3 16l5-5 5 2 8-8M3 20h18','M3 8l5 7 5-4 4 3 4-9','M4 8h16M4 16h16M8 4v8m8 0v8M18 3l3 3-3 3','M4 7h7v10H4zM13 7h7v10h-7z','M3 18l5-12 4 8 3-5 6 9', 'M3 17l4-9 4 6 3-4 4 7M15 5h6v6', 'M3 12h4l3-7 4 14 3-7h4', 'M3 19l6-14 4 9 3-5 5 10', 'M3 18l4-8 3 4 4-9 3 6 4-5'];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => Number(n || 0).toFixed(2);
const money = n => `${n < 0 ? '−' : ''}₹${Math.abs(n).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}`;
const ampm = s => { if (!s) return '—'; const [h,m] = s.split(':').map(Number); return `${h % 12 || 12}:${String(m).padStart(2,'0')} ${h >= 12 ? 'PM' : 'AM'}`; };
const todayIST = () => { const p = Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date()).map(v => [v.type,v.value])); return `${p.year}-${p.month}-${p.day}`; };
const BROKER_NAMES = {upstox:'Upstox', kite:'Kite', indmoney:'INDmoney'}, BROKER_ORDER = ['upstox','kite','indmoney'];
let B = null, brokerError = '', fillsError = '';
function showBrokerErrors() { const errors = [brokerError,fillsError,...Object.values(S?.reconciliation || {}).map(v => v.error)].filter(Boolean); $('brokerRefreshError').hidden = !errors.length; $('brokerRefreshError').textContent = errors.join(' · '); }
const accounts = () => BROKER_ORDER.map(b => { const e = Object.entries(S.state.accounts || {}).find(([,a]) => a.broker === b); return e && {id:e[0], ...e[1]}; }).filter(Boolean);
const account = b => accounts().find(a => a.broker === b);
const isLive = p => accounts().some(a => a.live[p]);
const lotSize = p => S.state.lot_sizes?.[p] ?? S.state.lot_size;
const paperOnly = p => (S.state.paper_only || []).includes(p);
const paperLots = p => { const on = accounts().filter(a => a.live[p]); return on.length ? Math.max(...on.map(a => a.lots[p])) : account('upstox')?.lots[p] || 1; };
const openLive = (acc,p) => Object.entries(S.state.orders?.[acc] || {}).filter(([id,o]) => id.startsWith(p + '|') && (o.buy?.filled_qty || 0) > (o.sell?.filled_qty || 0)).length;
async function loadBrokers() { try { B = (await api('/api/brokers')).brokers; brokerError = ''; if (S && !confirming) render(); } catch(e) { brokerError = 'Broker status unavailable: ' + e.message; } finally { showBrokerErrors(); } }
let S = null, busy = false, saving = false, confirming = false, refreshing = false, refreshVersion = 0;
let Q = null, quoting = false, quoteFailed = false, F = null;
async function refreshFills() { try { F = await api('/api/paper/fills'); fillsError = ''; updateLiveValues(); } catch(e) { fillsError = 'Fill reconciliation unavailable: ' + e.message; } finally { showBrokerErrors(); } }
async function api(path, body) {
  const r = await appFetch(path, body === undefined ? {} : {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const error = new Error(typeof j.detail === 'string' ? j.detail : j.error || `Request failed (${r.status}). Please try again.`); error.state = j.state; throw error; }
  return j;
}
function ordersCell(t, side) {
  const list = Object.entries(t.orders || {}).filter(([,o]) => o[side]);
  if (!list.length) return '—';
  return list.map(([acc,o]) => `<span class="muted">${BROKER_NAMES[acc.split(':')[1]] || ''}</span> ${orderCell(o[side])}`).join('<br>');
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
  const marks = new Map(), totals = {}; let total = 0, open = 0, delayed = false;
  PRESETS.forEach(p => {
    const qty = paperLots(p) * lotSize(p);
    totals[p] = 0;
    for (const t of S.view.results?.[p] || []) {
      const m = {...tradeValues(t), qty}; marks.set(t.id,m); totals[p] += m.pnl * qty;
      if (m.open) { open++; delayed ||= m.stale; }
    }
    total += totals[p];
  });
  const text = (node,value) => { if (node.textContent !== value) node.textContent = value; };
  const color = (node,pnl) => { node.classList.toggle('up',pnl >= 0); node.classList.toggle('down',pnl < 0); };
  for (const node of document.querySelectorAll('[data-trade-price]')) {
    const m = marks.get(node.dataset.tradePrice); if (!m) continue;
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
    const pnl = totals[node.dataset.strategyPnl]; text(node,S.view.day ? money(pnl) : '—'); color(node,pnl);
  }
  if (S.view.day) { text($('totalPnl'),money(total)); color($('totalPnl'),total); }
  const reporting = {}, allTrades = Object.values(S.view.results || {}).flat(), prices = {};
  for (const t of allTrades) prices[t.id] = tradeValues(t).price;
  let realized = 0, valuation = 0, pending = 0, closed = 0, charges = 0, complete = true;
  for (const acc of accounts()) {
    const result = UpstocksPnl.summarize(allTrades, S.state.orders?.[acc.id] || {}, prices);
    reporting[acc.broker] = result; realized += result.realized; valuation += result.valuation;
    pending += result.pending; closed += result.closed; charges += result.charges;
    complete &&= result.chargesComplete && !result.open && !result.pending;
  }
  text($('livePnl'),closed ? money(realized) : '—'); color($('livePnl'),realized);
  text($('liveCaption'),closed + ' trades with confirmed exit fills · ' + pending + ' unresolved orders · ' +
    (complete && closed ? 'net after reported charges ' + money(realized - charges) : !closed && !pending && !open ? 'No confirmed fills yet' : 'net unavailable until positions, orders and charges are resolved'));
  text($('openValuation'),'Open-position valuation (estimated): ' + money(valuation));
  $('liveBrokers').innerHTML = Object.entries(reporting).map(([broker,r]) => '<div class="live-broker"><span>' +
    BROKER_NAMES[broker] + '</span><b>' + (r.closed ? money(r.realized) : '—') + '</b><small>' + r.pending +
    ' unresolved · ' + r.open + ' open units</small></div>').join('');
  const replay = S.view.day && S.view.day !== todayIST();
  const label = replay ? 'Replay · prices fixed' : !S.market_open ? 'Market closed' : open ? (delayed ? 'Prices delayed · retrying' : 'Live prices · 1s') : 'Prices · waiting for entry';
  text($('refreshHint'),label); $('refreshHint').classList.toggle('quote-delayed',!!open && delayed);
  $('refreshHint').title = open && delayed ? Q?.error || 'Waiting for a fresh price from Upstox.' : 'Open trade prices and running P&L refresh every second.';
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
function tradeCard(t, qty) {
  const st = t.open ? 'OPEN' : t.status, pc = t.pnl >= 0 ? 'up' : 'down';
  const field = (label,value) => `<div><dt>${label}</dt><dd>${value}</dd></div>`;
  return `<article class="trade-card"><div class="trade-card-head"><b style="color:${t.type === 'CE' ? 'var(--ce)' : 'var(--pe)'}">${esc(t.symbol)}</b><span class="st ${esc(st)}">${esc(st === 'TIME_EXIT' ? 'TIME EXIT' : st)}</span></div>
    <dl>${field('Entry',`${ampm(t.entry_time)} · ₹${fmt(t.entry)}`)}${field(t.open ? 'Last price' : 'Exit',`${t.open ? '' : ampm(t.exit_time) + ' · '}${priceMarkup(t)}`)}${field('Stop-loss', t.sl > 0 ? `₹${fmt(t.sl)}` : 'None (trailing / time exit)')}${field('Target',`₹${fmt(t.target)}`)}${field('Max profit', peakCell(t.peak_high, t, qty))}${field('Max loss', peakCell(t.peak_low, t, qty))}</dl>
    <div class="trade-net"><span data-trade-points="${esc(t.id)}" data-unit=" pts" class="muted">${fmt(t.pnl)} pts</span><b data-trade-pnl="${esc(t.id)}" class="${pc}">${money(t.pnl * qty)}</b></div>
    ${Object.keys(t.orders || {}).length ? `<div class="ord">Live BUY: ${ordersCell(t,'buy')}<br>Live SELL: ${ordersCell(t,'sell')}</div>` : ''}</article>`;
}
function render() {
  if (confirming) return;
  const {state,view} = S;
  $('sessionContext').textContent = 'Results: ' + (view.day || 'no session') + ' · ' +
    (!view.day ? 'NO SCANS YET' : view.day !== todayIST() ? 'HISTORICAL SESSION — replay prices' : S.market_open ? 'TODAY — market open' : 'TODAY — market closed') +
    ' · Paper simulations and confirmed broker execution are reported separately.';
  showBrokerErrors();
  const liveCount = PRESETS.filter(isLive).length;
  const missing = B && S.market_calendar?.login_alerts_required ? accounts().filter(a => PRESETS.some(p => a.live[p]) && !B[a.broker]?.logged_in).map(a => BROKER_NAMES[a.broker]) : [];
  $('loginBanner').hidden = !missing.length;
  $('loginText').textContent = missing.length ? `Live is ON but you are not logged in to ${missing.join(' and ')} today — those orders will be skipped.` : '';
  $('modeBadge').textContent = liveCount ? `Live enabled · ${liveCount} ${liveCount === 1 ? 'strategy' : 'strategies'}` : 'Paper mode';
  $('modeBadge').className = 'mode-badge' + (liveCount ? ' live' : '');
  if (state.entries_paused) $('modeBadge').textContent = 'Live entries paused';
  $('pauseEntries').hidden = typeof state.revision !== 'number';
  $('pauseEntries').textContent = state.entries_paused ? 'Resume entries' : 'Pause entries';
  $('pauseEntries').disabled = saving;
  $('pauseEntries').setAttribute('aria-pressed', String(!!state.entries_paused));
  let pnl = 0, tradesCount = 0, openCount = 0;
  PRESETS.forEach(p => { const trades = view.results?.[p] || []; pnl += trades.reduce((a,t) => a + t.pnl * paperLots(p) * lotSize(p),0); tradesCount += trades.length; openCount += trades.filter(t => t.open).length; });
  $('totalPnl').textContent = view.day ? money(pnl) : '—'; $('totalPnl').className = 'metric-value' + (view.day ? (pnl >= 0 ? ' up' : ' down') : '');
  $('totalTrades').textContent = view.day ? tradesCount : '—'; $('openTrades').textContent = view.day ? openCount : '—';
  $('tradeCaption').textContent = view.day ? `${view.day}${view.day !== todayIST() ? ' · Replay' : ' · Today'}` : 'No scans yet';
  $('activeStrats').textContent = `${PRESETS.filter(p => state.config[p].on || isLive(p)).length} / ${PRESETS.length}`;
  $('activeCaption').textContent = liveCount ? `${liveCount} with live orders enabled` : PRESETS.some(p => state.config[p].on) ? 'Paper trading enabled' : 'All strategies paused';
  const errorCount = Object.keys(view.errors || {}).length;
  $('pnlCaption').textContent = errorCount ? `Incomplete scan · ${errorCount} ${errorCount === 1 ? 'error' : 'errors'}` : 'Simulated · before charges';
  if (!$('strats').contains(document.activeElement)) {
    // Harmonic, then EMA pullback, then the rest; each card keeps its own icon (icons follow PRESETS order).
    const shown = shownPresets();
    $('strats').innerHTML = shown.map(p => { const index = PRESETS.indexOf(p);
      const cfg = state.config[p], trades = view.results?.[p] || [], err = view.errors?.[p] || view.errors?.scan;
      const qty = paperLots(p) * lotSize(p), live = isLive(p), pts = trades.reduce((a,t) => a + t.pnl,0), open = trades.filter(t => t.open).length;
      const rows = trades.map((t,i) => { const st = t.open ? 'OPEN' : t.status, pc = t.pnl >= 0 ? 'up' : 'down';
        return `<tr><td>${i+1}</td><td>${ampm(t.entry_time)}</td><td><b style="color:${t.type === 'CE' ? 'var(--ce)' : 'var(--pe)'}">${esc(t.symbol)}</b></td><td>₹${fmt(t.entry)}</td><td>₹${fmt(t.sl)}</td><td>₹${fmt(t.target)}</td><td>${peakCell(t.peak_high, t, qty)}</td><td>${peakCell(t.peak_low, t, qty)}</td><td class="st ${esc(st)}">${esc(st === 'TIME_EXIT' ? 'TIME EXIT' : st)}</td><td>${t.open ? '—' : ampm(t.exit_time)}</td><td>${priceMarkup(t)}</td><td data-trade-points="${esc(t.id)}" class="${pc}">${fmt(t.pnl)}</td><td class="${pc}"><b data-trade-pnl="${esc(t.id)}">${money(t.pnl * qty)}</b></td><td class="ord">${ordersCell(t,'buy')}</td><td class="ord">${ordersCell(t,'sell')}</td></tr>`;
      }).join('');
      return `<section class="card strat ${live ? 'islive' : ''}" aria-label="${esc(LABELS[p])}">
        <div class="strategy-top"><div class="strategy-title"><span class="strategy-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="${STRATEGY_ICONS[index]}"/></svg></span><div><h3>${esc(LABELS[p])}</h3><p>${DESCRIPTIONS[p]}</p></div></div><div class="strategy-pnl"><span class="pnl-label">P&amp;L</span><b data-strategy-pnl="${p}" class="${pts >= 0 ? 'up' : 'down'}">${view.day ? money(pts * qty) : '—'}</b><small>${trades.length} trades${open ? ` · ${open} open` : ''}</small></div></div>
        ${(sq => sq ? `<div class="strategy-controls">${sq}</div>` : '')(accounts().map(a => { const pos = openLive(a.id,p); return pos ? `<button class="btn ghost sq" type="button" data-sq="${p}" data-broker="${a.broker}">Square off ${pos} · ${BROKER_NAMES[a.broker]}</button>` : ''; }).join(''))}
        ${err ? `<p class="strategy-error" role="status">${esc(err)}</p>` : ''}
        ${trades.length ? `<div class="scroll trade-scroll" tabindex="0" role="region" aria-label="${esc(LABELS[p])} trades; scroll for more columns"><table><thead><tr><th>#</th><th>Entry</th><th>Option</th><th>Entry ₹</th><th>SL</th><th>Target</th><th>Max profit</th><th>Max loss</th><th>Status</th><th>Exit</th><th>Exit / LTP</th><th>P&amp;L pts</th><th>P&amp;L ₹</th><th>Live BUY</th><th>Live SELL</th></tr></thead><tbody>${rows}</tbody></table></div><div class="trade-cards">${trades.map(t => tradeCard(t,qty)).join('')}</div>` : `<div class="empty-state"><span class="empty-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/></svg></span><div><b>${!cfg.on && !live ? 'Strategy paused' : err ? 'Market data unavailable' : 'Awaiting signal'}</b><p>${!cfg.on && !live ? 'Enable Paper to start tracking signals.' : err ? 'Check the message above and try scanning again.' : view.day ? `No trades for ${esc(view.day)}.` : ''}</p></div></div>`}
      </section>`;
    }).join('');
  }
  $('strats').setAttribute('aria-busy','false');
  renderSettings();
  $('log').textContent = (state.log || []).slice().reverse().join('\n') || 'No live orders yet.';
  updateLiveValues();
}
const editingConfig = () => S && document.activeElement?.matches('#strats input, #stratDialog input');
// Harmonic, then EMA pullback, then the rest — for the cards and the settings table.
const shownPresets = () => { const first = ['harmonic','harmonic_2y','harmonic_power','ema_pullback','sensex_swing','sensex_bigleg'].filter(p => PRESETS.includes(p)); return [...first, ...PRESETS.filter(p => !first.includes(p))]; };
function renderSettings() {
  if (!S || !$('stratDialog').open) return;
  if (document.activeElement?.matches('#stratDialog input[type=number]')) return;   // don't wipe what is being typed
  const accs = accounts();
  $('stratDialogSub').textContent = `Paper, live orders and lots for every strategy · lot size ${S.state.lot_size} (SENSEX ${lotSize('sensex_swing')})`;
  const head = accs.map(a => `<th><span class="th-broker">${brokerLogo(a.broker,16)}${BROKER_NAMES[a.broker]} Live · lots</span>${S.market_calendar?.login_alerts_required && B && !B[a.broker]?.logged_in ? '<small class="th-warn">Not logged in today</small>' : ''}</th>`).join('');
  const rows = shownPresets().map(p => {
    const cfg = S.state.config[p];
    return `<tr><td><b>${esc(LABELS[p])}</b><small>${DESCRIPTIONS[p]}</small></td>
      <td data-label="Paper"><label class="check tog"><input type="checkbox" role="switch" aria-label="Paper scanning for ${esc(LABELS[p])}" data-on="${p}" ${cfg.on ? 'checked' : ''}><span class="sw"></span></label></td>
      ${accs.map(a => { const n = BROKER_NAMES[a.broker];
        return `<td data-label="${n} Live"><div class="set-cell ${a.live[p] ? 'on' : ''}">${brokerLogo(a.broker,20)}<label class="check tog livechk"><input type="checkbox" role="switch" aria-label="${n} live orders for ${esc(LABELS[p])}" data-live="${p}" data-broker="${a.broker}" ${a.live[p] ? 'checked' : ''} ${paperOnly(p) ? 'disabled title="Paper only for now"' : ''}><span class="sw"></span></label>
          <input type="number" class="lots" aria-label="${n} lots for ${esc(LABELS[p])}" title="Lots" data-lots="${p}" data-broker="${a.broker}" min="1" step="1" inputmode="numeric" value="${a.lots[p]}" required>${a.live[p] ? '<span class="live-dot" title="Live orders on">LIVE</span>' : ''}</div></td>`; }).join('')}</tr>`;
  }).join('');
  $('stratDialogBody').innerHTML = '<div class="set-scroll"><table class="set-table"><thead><tr><th>Strategy</th><th>Paper</th>' + head + '</tr></thead><tbody>' + rows + '</tbody></table></div>';

}

async function refresh() {
  if (refreshing || busy || saving || confirming || editingConfig()) return;
  refreshing = true; const version = refreshVersion;
  try { const next = await api('/api/paper'); if (version !== refreshVersion || confirming || editingConfig()) return; S = next; $('connection').hidden = true; render(); }
  catch (e) { if (version !== refreshVersion) return; $('connectionText').textContent = 'Unable to update. Showing the last available data. ' + e.message; $('connection').hidden = false; $('refreshHint').textContent = 'Connection interrupted'; }
  finally { refreshing = false; if (version !== refreshVersion && !busy && !saving) refresh(); }
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
  saving = true; refreshVersion++; $('settingsSaveState').textContent = 'Saving…';
  try {
    const result = await api('/api/paper/config', {...body, revision:S.state.revision}); if (S) S.state = result.state;
    if (input?.type === 'checkbox') restore(body,input);
    $('settingsSaveState').textContent = 'Saved';
    notifyUI(body.entries_paused === true ? 'New live entries paused. Paper scans and exits continue.' : body.entries_paused === false ? 'Live entries resumed for enabled strategies.' : 'Strategy settings saved.');
  } catch(e) {
    if (e.state && S) S.state = e.state;
    restore(body,input);
    $('settingsSaveState').textContent = 'Not saved: ' + e.message;
    notifyUI(e.message,true);
  } finally { saving = false; if (S) render(); }
}
$('pauseEntries').addEventListener('click', async () => {
  if (!S || saving || confirming) return;
  const paused = !S.state.entries_paused;
  const enabled = PRESETS.filter(isLive);
  if (!paused && enabled.length && !await confirmAction({
    title:'Resume live entries?', message:'Enabled intraday and stock portfolios can place new real orders again. Existing exits continue.',
    confirmText:'Resume entries', danger:true, facts:[['Strategies',enabled.map(p => LABELS[p]).join(', ')]]
  })) return;
  saveConfig({entries_paused:paused, ...(!paused ? {confirm:'LIVE'} : {})});
});
$('retryBtn').addEventListener('click',refresh);
$('strats').addEventListener('click',async e => {
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
async function onConfigChange(e) {
  const input = e.target, {on,lots,live:lv,broker,mlive,mon} = input.dataset;
  if(lv) {
    const enabled = input.checked, acc = account(broker), cfg = {live:!!acc?.live[lv], lots:acc?.lots[lv] || 1};
    input.checked = cfg.live;
    if (busy || saving || confirming) { notifyUI('Please wait for the current action to finish.'); return; }
    if(enabled && !await confirmAction({
      title:'Enable live orders?', message:`New signals will send real buy orders to ${BROKER_NAMES[broker]}. Positions will be sold when the strategy exits.`,
      confirmText:'Enable live orders', danger:true,
      facts:[['Strategy',LABELS[lv]],['Broker',BROKER_NAMES[broker]],['Order size',`${cfg.lots} ${cfg.lots === 1 ? 'lot' : 'lots'}`],...(S.market_calendar?.login_alerts_required && B && !B[broker]?.logged_in ? [['Login','Not logged in today — orders will be skipped until you log in']] : [])]
    })) return;
    saveConfig({preset:lv,broker,live:enabled,...(enabled ? {confirm:'LIVE'} : {})},input);
  } else if(on) saveConfig({preset:on,on:input.checked},input);
  else if(lots && input.reportValidity()) saveConfig({preset:lots,broker,lots:Number(input.value)},input);
}
$('strats').addEventListener('change',onConfigChange);
$('stratDialogBody').addEventListener('change',onConfigChange);
$('stratDialogBody').addEventListener('focusout',() => setTimeout(renderSettings,0));
for (const id of ['stratDialogClose','stratDialogDone']) $(id).addEventListener('click',() => $('stratDialog').close());
$('stratDialog').addEventListener('click',e => {   // a click on the backdrop, outside the box, closes it
  if (e.target !== $('stratDialog')) return;
  const r = $('stratDialog').getBoundingClientRect();
  if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) $('stratDialog').close();
});
$('settingsBtn').addEventListener('click',() => { $('stratDialog').showModal(); renderSettings(); });
$('strats').addEventListener('focusout',() => { setTimeout(() => { if(S && !saving && !confirming && !$('strats').contains(document.activeElement)) render(); },0); });
document.addEventListener('visibilitychange',() => { if(!document.hidden) { refresh(); refreshQuotes(); } });
refresh().then(refreshQuotes); loadBrokers(); refreshFills();
setInterval(() => { if(!document.hidden) refreshFills(); },20000);
setInterval(() => { if(!document.hidden) loadBrokers(); },60000);
setInterval(() => { if(!document.hidden) refresh(); },10000);
setInterval(() => { if(!document.hidden) { updateLiveValues(); refreshQuotes(); } },1000);
