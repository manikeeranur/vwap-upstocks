// Trading settings: a Paper tab (simulations) and a Live tab (real broker orders).
const $ = id => document.getElementById(id);
const LABELS = { ...window.SETTINGS_CONFIG.labels, ...STRATEGY_META.labels }, PRESETS = window.SETTINGS_CONFIG.presets;
const DESCRIPTIONS = STRATEGY_META.descriptions, STRATEGY_ICONS = STRATEGY_META.icons;
const BROKER_NAMES = {paper:'Paper', upstox:'Upstox', kite:'Kite', indmoney:'INDmoney'}, BROKER_ORDER = ['upstox','kite','indmoney'];
// Same order as the Intraday trading page.
const FIRST = ['harmonic','harmonic_2y','harmonic_power','ema_pullback','sensex_swing','sensex_bigleg'];
const ORDERED = [...FIRST.filter(p => PRESETS.includes(p)), ...PRESETS.filter(p => !FIRST.includes(p))];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = n => `${n < 0 ? '−' : ''}₹${Math.abs(n).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}`;
const rupees = paise => '₹' + (Number(paise) / 100).toLocaleString('en-IN', {maximumFractionDigits:0});
const icon = p => `<span class="strategy-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="${STRATEGY_ICONS[PRESETS.indexOf(p)] || STRATEGY_ICONS[0]}"/></svg></span>`;
const toggle = (attrs,checked,label,extra='') => `<label class="check tog"><input type="checkbox" role="switch" ${attrs} aria-label="${esc(label)}" ${checked ? 'checked' : ''} ${extra}><span class="sw"></span></label>`;

let P = null, ST = null, B = null, saving = false, refreshing = false;
async function api(path, body) {
  const r = await appFetch(path, body === undefined ? {quiet:!!P} : {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const error = new Error(typeof j.detail === 'string' ? j.detail : j.error || `Request failed (${r.status}). Please try again.`); error.state = j.state; throw error; }
  return j;
}
const accounts = () => BROKER_ORDER.map(b => { const e = Object.entries(P.state.accounts || {}).find(([,a]) => a.broker === b); return e && {id:e[0], ...e[1]}; }).filter(Boolean);
const account = b => accounts().find(a => a.broker === b);
const paperOnly = p => (P.state.paper_only || []).includes(p);
const loginNeeded = b => P.market_calendar?.login_alerts_required && B && !B[b]?.logged_in;
const lotSize = p => P.state.lot_sizes?.[p] ?? P.state.lot_size;
const liveStrategies = () => ORDERED.filter(p => accounts().some(a => a.live[p]));
const livePortfolios = () => (ST?.portfolios || []).filter(p => p.broker !== 'paper');
const status = text => { $('saveState').textContent = text; };

/* Tabs */
function showTab(tab, focus=false) {
  for (const button of document.querySelectorAll('[data-tab]')) {
    const on = button.dataset.tab === tab;
    button.setAttribute('aria-selected',String(on)); button.tabIndex = on ? 0 : -1; $('panel-' + button.dataset.tab).hidden = !on;
    if (on && focus) button.focus();
  }
  try { history.replaceState(null,'','#' + tab); } catch { /* hash is a convenience */ }
}
document.querySelector('.mode-tabs').addEventListener('click',e => { const b = e.target.closest('[data-tab]'); if (b) showTab(b.dataset.tab); });
document.querySelector('.mode-tabs').addEventListener('keydown',e => {
  if (!['ArrowLeft','ArrowRight'].includes(e.key)) return;
  showTab(document.querySelector('[data-tab][aria-selected=true]').dataset.tab === 'paper' ? 'live' : 'paper', true); e.preventDefault();
});
showTab(location.hash === '#live' ? 'live' : 'paper');

/* Paper tab */
function renderPaper() {
  const cfg = P.state.config, results = P.view?.results || {}, on = ORDERED.filter(p => cfg[p]?.on);
  const trades = on.flatMap(p => (results[p] || []).map(t => ({...t, p})));
  const pnl = trades.reduce((a,t) => a + t.pnl * lotSize(t.p),0), open = trades.filter(t => t.open).length;
  $('paperTabNote').textContent = `${on.length} of ${ORDERED.length} strategies on`;
  StableDOM.html($('paperSummary'),`<div class="summary-stat"><span>Strategies scanning</span><b>${on.length}<small> / ${ORDERED.length}</small></b><meter min="0" max="${ORDERED.length}" value="${on.length}" aria-label="Strategies scanning"></meter></div>
    <div class="summary-stat"><span>Trades today</span><b>${trades.length}</b><small>${open ? `<span class="pulse-text">${open} open</span>` : P.view?.day ? 'None open' : 'No session data'}</small></div>
    <div class="summary-stat"><span>Paper P&amp;L today</span><b class="${pnl >= 0 ? 'up' : 'down'}">${trades.length ? money(pnl) : '—'}</b><small>1 lot per trade · before charges</small></div>
    <div class="summary-stat"><span>Order size</span><b>1 lot</b><small>Broker lots never resize simulations</small></div>`);
  $('paperSummary').setAttribute('aria-busy','false');
  $('paperBulk').innerHTML = `<button class="btn ghost" type="button" data-bulk="on" ${saving || on.length === ORDERED.length ? 'disabled' : ''}>Turn all on</button><button class="btn ghost" type="button" data-bulk="off" ${saving || !on.length ? 'disabled' : ''}>Turn all off</button>`;
  StableDOM.html($('paperIntraday'),'<div class="paper-grid">' + ORDERED.map(p => {
    const name = LABELS[p] || p, isOn = !!cfg[p]?.on, list = results[p] || [], open = list.filter(t => t.open).length;
    const total = list.reduce((a,t) => a + t.pnl,0) * lotSize(p), wins = list.filter(t => !t.open && t.pnl > 0).length, closed = list.filter(t => !t.open).length;
    const live = accounts().filter(a => a.live[p]).map(a => BROKER_NAMES[a.broker]);
    return `<article class="paper-set${isOn ? ' on' : ''}" data-key="${esc(p)}">
      <div class="paper-set-head">${icon(p)}<div class="paper-set-name"><h3>${esc(name)}</h3><p>${DESCRIPTIONS[p] || ''}</p></div>
        <div class="paper-set-switch">${toggle(`data-on="${esc(p)}"`,isOn,`Paper scanning for ${name}`)}<span class="tog-text">${isOn ? 'Scanning' : 'Off'}</span></div></div>
      <div class="paper-set-foot"><div class="chips"><span class="chip">Lot ${esc(lotSize(p))}</span>${paperOnly(p) ? '<span class="chip note">Paper only</span>' : ''}${live.length ? `<button type="button" class="chip live" data-go-live>Live on ${esc(live.join(', '))}</button>` : ''}</div>
        <div class="today">${list.length ? `<span>${list.length} trade${list.length === 1 ? '' : 's'}${closed ? ` · ${wins}W/${closed - wins}L` : ''}</span>${open ? `<span class="pulse-text">${open} open</span>` : ''}<b class="${total >= 0 ? 'up' : 'down'}">${money(total)}</b>` : `<span>${isOn ? 'No trades today' : 'Not scanning'}</span>`}</div></div>
    </article>`;
  }).join('') + '</div>');
  $('paperIntraday').setAttribute('aria-busy','false');
  const paper = (ST?.portfolios || []).filter(p => p.broker === 'paper');
  StableDOM.html($('paperStocks'),ST ? (paper.length ? `<div class="stock-grid">${paper.map(stockCard).join('')}</div>` : '<div class="settings-card empty">No paper stock portfolio yet.</div>') : '<div class="settings-card empty">Stock portfolio settings unavailable.</div>');
}

/* Live tab */
function renderLive() {
  const paused = !!P.state.entries_paused, accs = accounts(), liveCount = liveStrategies().length + livePortfolios().filter(p => p.enabled).length;
  $('liveTabNote').textContent = paused ? 'Entries paused' : liveCount ? `${liveCount} on · real orders` : 'All off';
  $('tab-live').classList.toggle('has-live',!paused && !!liveCount);
  const brokers = accs.map(a => { const off = B && !B[a.broker]?.logged_in;
    return `<a class="broker-pill${off ? ' off' : ''}" href="/brokers">${brokerLogo(a.broker,16)}${BROKER_NAMES[a.broker]}<span>${!B ? 'Checking…' : off ? 'Not logged in' : 'Logged in'}</span></a>`; }).join('');
  StableDOM.html($('master'),`<div class="master-main"><span class="master-dot${paused ? ' paused' : liveCount ? ' live' : ''}" aria-hidden="true"></span>
      <div><h2>Live entries ${paused ? 'paused' : 'allowed'}</h2><p>${paused ? 'No new real orders are placed. Exits, paper scans and simulations continue.' : liveCount ? `${liveCount} live ${liveCount === 1 ? 'setting is' : 'settings are'} on. New signals send real orders.` : 'No live orders are switched on. Only paper simulations run.'}</p></div></div>
    <div class="master-side"><div class="broker-pills">${brokers || '<span class="muted">No broker accounts</span>'}</div>
      <button class="btn ${paused ? 'primary' : 'ghost danger'}" type="button" id="pauseAll" ${saving || typeof P.state.revision !== 'number' ? 'disabled' : ''} aria-pressed="${paused}">${paused ? 'Resume live entries' : 'Pause all live entries'}</button></div>`);
  $('master').classList.toggle('paused',paused); $('master').setAttribute('aria-busy','false');
  $('liveCounts').innerHTML = accs.map(a => `<span class="count-chip${ORDERED.some(p => a.live[p]) ? ' live' : ''}">${brokerLogo(a.broker,14)}${BROKER_NAMES[a.broker]} <b>${ORDERED.filter(p => a.live[p]).length}</b> live</span>`).join('');
  if (!accs.length) { StableDOM.html($('liveIntraday'),'<div class="settings-card empty">No broker accounts. <a href="/brokers">Connect a broker</a> to place live orders.</div>'); }
  else {
    const head = `<div class="intra-row intra-head" aria-hidden="true"><span>Strategy</span>${accs.map(a => `<span class="th-broker">${brokerLogo(a.broker,16)}${BROKER_NAMES[a.broker]} · live &amp; lots${loginNeeded(a.broker) ? '<small>Not logged in today</small>' : ''}</span>`).join('')}</div>`;
    const rows = ORDERED.map(p => {
      const name = LABELS[p] || p, locked = paperOnly(p), anyLive = accs.some(a => a.live[p]);
      const cells = accs.map(a => { const n = BROKER_NAMES[a.broker], lv = !!a.live[p];
        return `<div class="intra-cell broker-cell${lv ? ' on' : ''}"><span class="cell-name">${brokerLogo(a.broker,18)}${n}</span>
          ${toggle(`data-live="${p}" data-broker="${a.broker}"`,lv,`${n} live orders for ${name}`,locked ? 'disabled data-locked' : '')}
          <label class="lots-field"><input type="number" class="lots" aria-label="${n} lots for ${esc(name)}" data-lots="${p}" data-broker="${a.broker}" min="1" max="10000" step="1" inputmode="numeric" value="${a.lots[p] ?? 1}" required ${locked ? 'disabled data-locked' : ''}><span>lot${(a.lots[p] ?? 1) === 1 ? '' : 's'} · ${(a.lots[p] ?? 1) * lotSize(p)} qty</span></label>
          ${lv ? '<span class="live-tag">LIVE</span>' : ''}</div>`; }).join('');
      return `<article class="intra-row${anyLive ? ' live-on' : ''}${locked ? ' locked' : ''}" data-key="${esc(p)}">
        <div class="intra-name">${icon(p)}<div><h3>${esc(name)}${locked ? '<span class="tag-note">Paper only</span>' : ''}</h3><p>${DESCRIPTIONS[p] || ''}</p></div></div>${cells}</article>`;
    }).join('');
    $('liveIntraday').style.setProperty('--brokers',accs.length);
    StableDOM.html($('liveIntraday'),`<div class="intra-table">${head}${rows}</div>`);
  }
  $('liveIntraday').setAttribute('aria-busy','false');
  const live = livePortfolios();
  StableDOM.html($('liveStocks'),ST ? (live.length ? `<div class="stock-grid">${live.map(stockCard).join('')}</div>` : '<div class="settings-card empty">No live stock portfolios.</div>') : '<div class="settings-card empty">Stock portfolio settings unavailable.</div>');
}

function stockCard(p) {
  const live = p.broker !== 'paper', state = !p.enabled ? 'Automation off' : p.paused || live && ST.global_entries_paused ? 'Entries paused · exits continue' : live ? 'Live orders on' : 'Simulating';
  return `<article class="stock-set${p.enabled ? ' on' : ''}${live && p.enabled && !p.paused ? ' live-on' : ''}" data-key="${esc(p.id)}">
    <header><div class="stock-name">${live ? brokerLogo(p.broker,20) : '<span class="paper-mark" aria-hidden="true">P</span>'}<div><h3>${live ? BROKER_NAMES[p.broker] || esc(p.broker) : 'Paper portfolio'}</h3><p>${p.client_id ? esc(p.client_id) + ' · ' : ''}${state}</p></div></div>
      ${live && p.enabled && !p.paused ? '<span class="live-tag">LIVE</span>' : ''}</header>
    <div class="stock-toggles">
      <div class="stock-toggle"><div><b>Automation</b><small>${live ? 'Place real limit orders on monthly decisions' : 'Run the simulated portfolio'}</small></div>${toggle(`data-stock="${esc(p.broker)}" data-field="enabled"`,p.enabled,`Automation for ${BROKER_NAMES[p.broker]} portfolio`)}</div>
      <div class="stock-toggle"><div><b>Pause new entries</b><small>Exits keep running</small></div>${toggle(`data-stock="${esc(p.broker)}" data-field="paused"`,p.paused,`Pause new entries for ${BROKER_NAMES[p.broker]} portfolio`)}</div>
    </div>
    <footer><span>Usable capital <b>${rupees(p.usable_capital)}</b></span><a href="/stocks">Edit capital &amp; entry source →</a></footer>
  </article>`;
}

function render() {
  if (!P) return;
  renderPaper(); renderLive();
  document.querySelectorAll('#main input').forEach(i => { if (!i.hasAttribute('data-locked')) i.disabled = saving; });
}

async function refresh() {
  if (refreshing || saving || document.activeElement?.matches('#main input[type=number]')) return;
  refreshing = true;
  try {
    const [paper, stocks] = await Promise.allSettled([api('/api/paper'), api('/api/stocks')]);
    if (paper.status === 'rejected') throw paper.reason;
    if (saving) return;
    P = paper.value; ST = stocks.status === 'fulfilled' ? stocks.value : ST;
    $('loadError').hidden = stocks.status === 'fulfilled';
    if (stocks.status === 'rejected') $('loadErrorText').textContent = 'Stock portfolio settings unavailable: ' + stocks.reason.message;
    if ($('saveState').textContent === 'Loading…') status('All changes saved');
    render();
  } catch (e) {
    $('loadErrorText').textContent = 'Unable to load settings. ' + e.message; $('loadError').hidden = false;
    if (!P) status('Not loaded');
  } finally { refreshing = false; }
}
async function loadBrokers() { try { B = (await api('/api/brokers')).brokers; if (P) render(); } catch { /* login status is advisory */ } }

async function save(run, success) {
  saving = true; status('Saving…'); render();
  try { await run(); status('Saved'); notifyUI(success); }
  catch (e) { if (e.state && P) P.state = e.state; status('Not saved: ' + e.message); notifyUI(e.message,true); }
  finally { saving = false; render(); }
}
const configure = async body => { P.state = (await api('/api/paper/config',{...body, revision:P.state.revision})).state; };
const saveIntraday = (body, message) => save(() => configure(body), message);

async function onChange(e) {
  const input = e.target;
  if (!P || saving) { render(); return; }
  const {on, live, lots, broker, stock, field} = input.dataset;
  if (on) return saveIntraday({preset:on, on:input.checked}, `${LABELS[on]} paper scanning ${input.checked ? 'on' : 'off'}.`);
  if (lots) {
    if (!input.reportValidity()) return;
    return saveIntraday({preset:lots, broker, lots:Number(input.value)}, `${LABELS[lots]} · ${BROKER_NAMES[broker]} set to ${input.value} lot${input.value === '1' ? '' : 's'}.`);
  }
  if (live) {
    const enable = input.checked, acc = account(broker); input.checked = !enable;
    if (enable && !await confirmUI({title:'Enable live orders?', message:`New signals will send real buy orders to ${BROKER_NAMES[broker]}. Positions will be sold when the strategy exits.`,
      confirmText:'Enable live orders', danger:true, facts:[['Strategy',LABELS[live]],['Broker',BROKER_NAMES[broker]],['Order size',`${acc?.lots[live] || 1} lot(s) · ${(acc?.lots[live] || 1) * lotSize(live)} qty`],
        ...(P.state.entries_paused ? [['Live entries','Paused; resume them separately']] : []), ...(loginNeeded(broker) ? [['Login','Not logged in today — orders will be skipped until you log in']] : [])]})) return;
    return saveIntraday({preset:live, broker, live:enable, ...(enable ? {confirm:'LIVE'} : {})}, `${LABELS[live]} · ${BROKER_NAMES[broker]} live orders ${enable ? 'on' : 'off'}.`);
  }
  if (stock) {
    const p = ST.portfolios.find(x => x.broker === stock); if (!p) return;
    const next = {enabled:p.enabled, paused:p.paused, [field]:input.checked}; input.checked = p[field];
    const body = {broker:p.broker, revision:p.revision, usable_capital:p.usable_capital / 100, entry_source:p.entry_source, ...next};
    if (p.broker !== 'paper' && next.enabled && !next.paused) {
      if (!await confirmUI({title:'Enable live stock entries?', message:'Real limit orders can be placed on the next eligible decision for this portfolio.', confirmText:'Enable live entries', danger:true,
        facts:[['Broker',BROKER_NAMES[p.broker]],['Account',p.client_id || 'Verified at save'],['Usable-capital limit',rupees(p.usable_capital)],['Global entries',P.state.entries_paused ? 'Paused; must be resumed separately' : 'Allowed']]})) return;
      body.confirm = 'LIVE';
    }
    return save(async () => { ST = await api('/api/stocks/config',body); }, `${p.broker === 'paper' ? 'Paper' : BROKER_NAMES[p.broker]} stock portfolio settings saved.`);
  }
}

$('main').addEventListener('change',onChange);
LoadingUI.on($('main'),'click', async e => {
  if (e.target.closest('[data-go-live]')) { showTab('live',true); return; }
  const bulk = e.target.closest('[data-bulk]');
  if (bulk && P && !saving) {
    const turnOn = bulk.dataset.bulk === 'on', targets = ORDERED.filter(p => !!P.state.config[p]?.on !== turnOn);
    if (!turnOn && !await confirmUI({title:'Turn off all paper strategies?', message:'Paper scanning stops for every intraday strategy. Live settings are not changed.', confirmText:'Turn all off', facts:[['Strategies',String(targets.length)]]})) return;
    return save(async () => { for (const p of targets) await configure({preset:p, on:turnOn}); }, `Paper scanning ${turnOn ? 'on' : 'off'} for ${targets.length} strateg${targets.length === 1 ? 'y' : 'ies'}.`);
  }
  if (!e.target.closest('#pauseAll') || !P || saving) return;
  const pause = !P.state.entries_paused;
  if (!pause && !await confirmUI({title:'Resume live entries?', message:'Enabled intraday strategies and stock portfolios can place new real orders again. Existing exits continue.', confirmText:'Resume entries', danger:true,
    facts:[['Intraday live',liveStrategies().map(p => LABELS[p]).join(', ') || 'None'],['Live stock portfolios',livePortfolios().filter(p => p.enabled).map(p => BROKER_NAMES[p.broker]).join(', ') || 'None']]})) return;
  await saveIntraday({entries_paused:pause, ...(!pause ? {confirm:'LIVE'} : {})}, pause ? 'New live entries paused. Paper scans and exits continue.' : 'Live entries resumed for enabled strategies.');
  if (ST) ST.global_entries_paused = P.state.entries_paused; render();
});
$('retryBtn').addEventListener('click',refresh);
document.addEventListener('visibilitychange',() => { if (!document.hidden) refresh(); });
refresh(); loadBrokers();
setInterval(() => { if (!document.hidden) refresh(); },15000);
setInterval(() => { if (!document.hidden) loadBrokers(); },60000);
