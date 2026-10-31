/* Date-only calendar helpers: no UTC/browser-timezone conversion of exchange dates. */
(function (root) {
  'use strict';
  const iso = d => [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'), String(d.getDate()).padStart(2, '0')].join('-');
  function session(key, metadata, exchange) {
    const weekend = [0, 6].includes(new Date(key + 'T12:00:00').getDay());
    const codes = exchange === 'BSE' ? ['BSE', 'BFO'] : exchange === 'NSE' ? ['NSE', 'NFO'] : ['NSE', 'NFO', 'BSE', 'BFO'];
    const fallback = {kind:weekend ? 'weekend' : 'unknown', label:weekend ? 'Weekend' : 'Holiday calendar unavailable'};
    const sessions = codes.map(code => ({code, ...(metadata?.exchanges?.[code] || fallback)}));
    const kinds = new Set(sessions.map(s => s.kind)), labels = [...new Set(sessions.map(s => s.label))];
    const kind = kinds.size === 1 ? sessions[0].kind : 'mixed';
    const badge = {weekend:'Weekend', holiday:'Holiday', special:'Special session', mixed:'Check sessions', unknown:'Unverified', open:''}[kind];
    return {kind, badge, label:labels.join(' · '), weekend, sessions};
  }
  const api = {iso, session};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ExchangeCalendar = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);

