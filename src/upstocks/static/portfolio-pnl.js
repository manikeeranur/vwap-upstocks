/* Pure reporting: broker quantities determine ownership; quotes only mark open exposure. */
(function (root) {
  'use strict';
  function settled(o) {
    const terminal = ['complete','cancelled','rejected'];
    return o.qty > 0 && o.qty === o.sold_qty && terminal.includes(o.buy_status) && terminal.includes(o.sell_status) &&
      Number.isFinite(o.buy) && Number.isFinite(o.sell) && Number.isFinite(o.buy_charges) && Number.isFinite(o.sell_charges);
  }
  function summarize(trades, orders, marks) {
    const totals = {realized:0, valuation:0, closed:0, open:0, pending:0, charges:0, chargesComplete:true};
    for (const trade of trades) {
      const sides = orders[trade.id] || trade.orders || {};
      const buy = sides.buy, sell = sides.sell;
      if (!buy) continue;
      if (buy.filled_qty == null || !['complete','cancelled','rejected'].includes(buy.execution_status)) totals.pending++;
      if (sell && (sell.filled_qty == null || !['complete','cancelled','rejected'].includes(sell.execution_status))) totals.pending++;
      const bought = Number(buy.filled_qty || 0), sold = Math.min(bought, Number(sell?.filled_qty || 0));
      if (!bought || buy.fill_price == null) continue;
      if (sold && sell.fill_price != null) {
        totals.realized += sold * (sell.fill_price - buy.fill_price);
        totals.closed++;
      }
      const remaining = bought - sold, mark = marks[trade.id];
      if (remaining) {
        totals.open += remaining;
        if (Number.isFinite(mark)) totals.valuation += remaining * (mark - buy.fill_price);
      }
      if (buy.charges == null || buy.charges_source !== "operator_statement" || (sold && (sell.charges == null || sell.charges_source !== "operator_statement"))) totals.chargesComplete = false;
      totals.charges += Number(buy.charges || 0) + Number(sell?.charges || 0);
    }
    totals.net = totals.chargesComplete && !totals.pending && !totals.open && totals.closed ? totals.realized - totals.charges : null;
    return totals;
  }
  if (typeof module === 'object' && module.exports) module.exports = {summarize, settled};
  else root.UpstocksPnl = {summarize, settled};
})(typeof window === 'object' ? window : globalThis);
