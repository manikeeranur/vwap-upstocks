/* Pure reporting: broker quantities determine ownership; quotes only mark open exposure. */
(function (root) {
  'use strict';
  function closed(o) {
    const terminal = ['complete','cancelled','rejected'];
    return o.qty > 0 && o.qty === o.sold_qty && terminal.includes(o.buy_status) && terminal.includes(o.sell_status) &&
      Number.isFinite(o.buy) && Number.isFinite(o.sell);
  }
  function hasFees(o) {
    return Number.isFinite(o.buy_charges) && Number.isFinite(o.sell_charges);
  }
  function settled(o) {
    return closed(o) && hasFees(o) && !['estimated','missing'].includes(o.buy_fee_quality) && !['estimated','missing'].includes(o.sell_fee_quality);
  }
  function orderFee(o) {
    if (o?.charges_source === 'operator_statement' && Number.isFinite(o.charges)) return {amount:o.charges,quality:'final'};
    if (Number.isFinite(o?.estimated_charges) && o.estimated_charges_qty === o.filled_qty &&
        o.estimated_charges_notional === Math.round((o.fill_price || 0)*o.filled_qty*100)) return {amount:o.estimated_charges,quality:'estimated'};
    return {amount:null,quality:'missing'};
  }
  function historySummary(rows) {
    const pendingCharges = rows.filter(t => t.mode === 'live' && !t.feesAvailable).length;
    const estimatedCharges = rows.filter(t => t.feeQuality === 'estimated').length;
    const gross = rows.reduce((sum,t) => sum + (t.gross ?? t.rs),0);
    const charges = pendingCharges ? null : rows.reduce((sum,t) => sum + (t.ch ?? 0),0);
    return {gross,charges,net:charges === null ? null : gross-charges,pendingCharges,estimatedCharges};
  }
  function summarize(trades, orders, marks) {
    const totals = {realized:0, valuation:0, closed:0, open:0, pending:0, charges:0, chargesComplete:true, chargesFinal:true, valuationMissing:0, positions:0};
    for (const trade of trades) {
      const sides = orders[trade.id] || trade.orders || {};
      const buy = sides.buy, sell = sides.sell;
      if (!buy) continue;
      if (buy.filled_qty == null || !['complete','cancelled','rejected'].includes(buy.execution_status)) totals.pending++;
      if (sell && (sell.filled_qty == null || !['complete','cancelled','rejected'].includes(sell.execution_status))) totals.pending++;
      const bought = Number(buy.filled_qty || 0), sold = Math.min(bought, Number(sell?.filled_qty || 0));
      if (!bought) continue;
      if (!Number.isFinite(buy.fill_price)) {
        totals.open += bought-sold; totals.positions += bought > sold ? 1 : 0;
        totals.valuationMissing += bought > sold ? 1 : 0; totals.chargesComplete = false;
        if (['complete','cancelled','rejected'].includes(buy.execution_status)) totals.pending++;
        continue;
      }
      if (sold && !Number.isFinite(sell.fill_price)) {
        totals.chargesComplete = false;
        if (['complete','cancelled','rejected'].includes(sell.execution_status)) totals.pending++;
      }
      if (sold && Number.isFinite(sell.fill_price)) {
        totals.realized += sold * (sell.fill_price - buy.fill_price);
        totals.closed++;
      }
      const remaining = bought - sold, mark = marks[trade.id];
      if (remaining) {
        totals.open += remaining; totals.positions++;
        if (Number.isFinite(mark)) totals.valuation += remaining * (mark - buy.fill_price);
        else totals.valuationMissing++;
      }
      const buyFee = orderFee(buy), sellFee = sold ? orderFee(sell) : {amount:0,quality:'final'};
      if (buyFee.amount === null || sellFee.amount === null) totals.chargesComplete = false;
      if (buyFee.quality !== 'final' || sellFee.quality !== 'final') totals.chargesFinal = false;
      totals.charges += (buyFee.amount ?? 0) + (sellFee.amount ?? 0);
    }
    totals.net = totals.chargesComplete && !totals.pending && !totals.open && totals.closed ? totals.realized - totals.charges : null;
    return totals;
  }
  function executionRows(state, trades = [], marks = {}, broker = 'all') {
    const lookup = Object.fromEntries(trades.map(t => [t.id,t])), rows = [];
    for (const [account, orders] of Object.entries(state.orders || {})) {
      const owner = state.accounts?.[account];
      if (!owner || broker !== 'all' && owner.broker !== broker) continue;
      for (const [id,sides] of Object.entries(orders)) {
        if (!sides.buy && !sides.sell) continue;
        const [preset,date] = id.split('|'), trade = lookup[id] || {};
        rows.push({id,preset,date,account,broker:owner.broker,buy:sides.buy,sell:sides.sell,
          symbol:sides.buy?.broker_symbol || sides.buy?.symbol || trade.symbol || 'Instrument unavailable',
          client_id:sides.buy?.client_id || '', stats:summarize([{id}],{[id]:sides},marks)});
      }
    }
    return rows.sort((a,b)=>(b.buy?.created_at || b.date || '').localeCompare(a.buy?.created_at || a.date || ''));
  }
  function executionSummary(rows) {
    const total = {realized:0,valuation:0,closed:0,open:0,pending:0,charges:0,chargesComplete:true,chargesFinal:true,valuationMissing:0,positions:0};
    for (const {stats} of rows) {
      for (const k of ['realized','valuation','closed','open','pending','charges','valuationMissing','positions']) total[k] += stats[k] || 0;
      total.chargesComplete &&= stats.chargesComplete;
      total.chargesFinal &&= stats.chargesFinal;
    }
    total.net = total.closed && total.chargesComplete && !total.pending && !total.open ? total.realized-total.charges : null;
    return total;
  }
  function historyRows(journal, {mode='paper',broker='all',strategy='',day=''} = {}) {
    const signals = {};
    for (const [date, presets] of Object.entries(journal?.days || {})) {
      for (const [preset, session] of Object.entries(presets)) {
        for (const trade of session.trades) signals[trade.id] = {...trade,date,preset,qty:session.lots * session.lot_size};
      }
    }
    const rows = [];
    if (mode === 'paper') {
      for (const t of Object.values(signals)) {
        if (strategy && t.preset !== strategy || day && t.date !== day) continue;
        rows.push({...t,rs:t.pnl*t.qty,mode:'paper'});
      }
    } else {
      for (const [id,accounts] of Object.entries(journal?.live || {})) {
        const [preset,date] = id.split('|');
        if (strategy && preset !== strategy || day && date !== day) continue;
        for (const [account,o] of Object.entries(accounts)) {
          const source = account.split(':')[1];
          if (broker !== 'all' && source !== broker || !closed(o)) continue;
          const realCh = settled(o), feesAvailable = hasFees(o), gross = (o.sell-o.buy)*o.qty, ch = feesAvailable ? o.buy_charges+o.sell_charges : null;
          rows.push({id,preset,date,account,broker:source,symbol:o.symbol || signals[id]?.symbol || 'Instrument unavailable',
            type:signals[id]?.type,qty:o.qty,buyFill:o.buy,sellFill:o.sell,client_id:o.client_id || '',holder:o.holder || '',
            gross,ch,net:feesAvailable ? gross-ch : null,open:false,status:'FILLED',real:true,realCh,feesAvailable,feeQuality:realCh ? 'final' : feesAvailable ? 'estimated' : 'missing',mode:'live'});
        }
      }
    }
    if (mode === 'live') {
      // Keep one basis across the selection; never add a mixture of gross and net.
      const basis = rows.some(t => !t.feesAvailable) ? 'gross' : 'net';
      for (const row of rows) { row.pnlBasis = basis; row.rs = basis === 'gross' ? row.gross : row.net; }
    }
    return rows.sort((a,b)=>(a.date+(a.entry_time || '')).localeCompare(b.date+(b.entry_time || '')));
  }
  const api = {summarize, closed, settled, orderFee, executionRows, executionSummary, historyRows, historySummary};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.UpstocksPnl = api;
})(typeof window === 'object' ? window : globalThis);
