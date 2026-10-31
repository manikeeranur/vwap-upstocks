const test=require('node:test'),assert=require('node:assert/strict');
const {matches,dateRange,dayIST}=require('../src/upstocks/static/workspace-ui.js');
test('filters intersect without changing rows or financial values',()=>{
  const row={search:'NIFTY sample CE',preset:'harmonic',state:'open',side:'BUY',date:'2026-10-31',rs:500};
  assert.equal(matches(row,{q:'  nifty ',strategy:'harmonic',status:'open',side:'BUY',result:'profit',day:'2026-10-31'}),true);
  for(const filter of [{q:'sensex'},{status:'closed'},{side:'SELL'},{result:'loss'},{from:'2026-11-01'},{to:'2026-10-30'},{day:'2026-10-30'}]) assert.equal(matches(row,filter),false);
  assert.equal(row.rs,500);
});
test('unknown profit never becomes a break-even result',()=>{
  for(const pnl of [null,undefined,NaN]) assert.equal(matches({pnl},{result:'flat'}),false);
  assert.equal(matches({rs:0},{result:'flat'}),true);
  assert.equal(matches({rs:-10,pnl:100},{result:'loss'}),true);
});
test('date shortcuts are inclusive and work across year and leap-day boundaries',()=>{
  assert.deepEqual(dateRange('30d','2026-01-10'),{from:'2025-12-12',to:'2026-01-10'});
  assert.deepEqual(dateRange('month','2024-02-29'),{from:'2024-02-01',to:'2024-02-29'});
  assert.deepEqual(dateRange('year','2026-10-31'),{from:'2026-01-01',to:'2026-10-31'});
  assert.deepEqual(dateRange('today','2026-10-31'),{from:'2026-10-31',to:'2026-10-31'});
  assert.equal(dateRange('custom','2026-10-31'),null);
});
test('stock order dates are filtered in IST across UTC midnight',()=>{
  assert.equal(dayIST('2026-10-30T20:00:00Z'),'2026-10-31');
  assert.equal(dayIST('2026-10-30T17:00:00Z'),'2026-10-30');
  assert.equal(dayIST('invalid'),'');
});

test('an open position needing reconciliation appears in either relevant filter',()=>{
  const row={state:['open','pending']};
  assert.equal(matches(row,{status:'open'}),true);
  assert.equal(matches(row,{status:'pending'}),true);
  assert.equal(matches(row,{status:'closed'}),false);
});
