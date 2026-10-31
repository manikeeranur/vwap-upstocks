/* Intraday presentation only: retained broker history remains available for recovery. */
(function(root) {
  'use strict';
  const dayFormat = new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'});
  function orderDay(order) {
    // Reservation time can date a carried position's exit; reconciliation time cannot.
    const value=order?.created_at;
    if(!value || !(order.filled_qty>0) || !/(Z|[+-]\d{2}:\d{2})$/.test(value))return null;
    const timestamp=new Date(value);if(!Number.isFinite(timestamp.getTime()))return null;
    const parts=Object.fromEntries(dayFormat.formatToParts(timestamp).map(part=>[part.type,part.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }
  function executions(rows,session) {
    const day=session?.day;
    return rows.filter(row=>day && (row.date===day||orderDay(row.buy)===day||orderDay(row.sell)===day)||row.stats.open>0||row.stats.pending>0)
      .map(row=>({...row,carryover:row.date!==day&&(row.stats.open>0||row.stats.pending>0)}));
  }
  function resetDelay(session) {
    const next=Date.parse(session?.next_reset_at),now=Date.parse(session?.server_now);
    return Number.isFinite(next)&&Number.isFinite(now)?Math.max(0,next-now):null;
  }
  function rollover(snapshot) {
    const reset=snapshot?.intraday_session?.next_reset_at;
    if(!reset)return snapshot;
    const day=reset.slice(0,10);
    return {...snapshot,view:{day,scanned_at:null,results:{},errors:{},running:false},intraday_session:{
      ...snapshot.intraday_session,day,today:day,phase:'current',server_now:reset,next_reset_at:null
    }};
  }
  const api={executions,resetDelay,rollover};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.IntradayView=api;
})(typeof window==='object'?window:globalThis);
