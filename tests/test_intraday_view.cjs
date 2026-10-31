const test=require('node:test');
const assert=require('node:assert/strict');
const {executions,resetDelay,rollover}=require('../src/upstocks/static/intraday-view.js');
const {executionRows,executionSummary}=require('../src/upstocks/static/portfolio-pnl.js');
const session={day:'2026-10-30',today:'2026-11-02',phase:'previous',server_now:'2026-11-02T08:29:59+05:30',next_reset_at:'2026-11-02T08:30:00+05:30'};
const closed=(day,exit)=>({date:day,stats:{closed:1,open:0,pending:0},sell:exit});
test('old closed trades are excluded but older open and unresolved positions remain visible',()=>{
  const rows=[closed('2026-10-28'),closed('2026-10-30'),{date:'2026-10-28',stats:{open:65,pending:0}},{date:'2026-10-28',stats:{open:0,pending:1}}];
  const copy=structuredClone(rows),visible=executions(rows,session);
  assert.equal(visible.length,3);assert.equal(visible[0].date,'2026-10-30');
  assert.deepEqual(visible.map(r=>r.carryover),[false,true,true]);assert.deepEqual(rows,copy);
});
test('a carried trade closed this session is included using the exit reservation date in IST',()=>{
  const visible=executions([closed('2026-10-28',{created_at:'2026-10-29T20:00:00Z',filled_qty:65})],session);
  assert.equal(visible.length,1);
  assert.equal(executions([closed('2026-10-28',{updated_at:'2026-10-30T10:00:00+05:30',filled_qty:65})],session).length,0);
  assert.equal(executions([closed('2026-10-28',{created_at:'2026-10-30T10:00:00',filled_qty:65})],session).length,0);
});
test('rollover clears paper results and previous closed live trades without changing broker state',()=>{
  const state={orders:{saved:'retained'},config:{on:true}},view={day:'2026-10-30',results:{harmonic:[{pnl:20}]},errors:{old:'error'}};
  const snapshot={state,view,intraday_session:session},next=rollover(snapshot);
  assert.equal(next.view.day,'2026-11-02');assert.deepEqual(next.view.results,{});assert.deepEqual(next.view.errors,{});
  assert.equal(next.state,state);assert.equal(snapshot.view,view);assert.equal(next.intraday_session.phase,'current');
  assert.equal(executions([closed('2026-10-30')],next.intraday_session).length,0);
  assert.equal(rollover(next),next);
});
test('reset delay uses server timestamps rather than the browser timezone',()=>{
  assert.equal(resetDelay(session),1000);
  assert.equal(resetDelay({...session,server_now:'2026-11-02T08:30:01+05:30'}),0);
  assert.equal(resetDelay({...session,next_reset_at:'2026-11-04T08:30:00+05:30'}),172801000);
  assert.equal(resetDelay({}),null);
});
test('missing calendar keeps only positions needing attention',()=>{
  assert.equal(executions([closed('2026-10-30')],{day:null}).length,0);
  assert.equal(executions([{date:'2026-10-30',stats:{open:1,pending:0}}],{day:null}).length,1);
});
test('scoped P&L and broker filtering include only the active trading session',()=>{
  const state={accounts:{'1:kite':{broker:'kite'},'1:upstox':{broker:'upstox'}},orders:{'1:kite':{},'1:upstox':{}}};
  const sides=sell=>({buy:{filled_qty:10,fill_price:100,execution_status:'complete',charges:2,charges_source:'operator_statement'},sell:{filled_qty:10,fill_price:sell,execution_status:'complete',charges:3,charges_source:'operator_statement'}});
  state.orders['1:kite']['harmonic|2026-10-28|10:00']=sides(120);
  state.orders['1:kite']['harmonic|2026-10-30|10:00']=sides(90);
  state.orders['1:upstox']['harmonic|2026-10-30|10:00']=sides(110);
  const rows=executions(executionRows(state,[],{},'kite'),session);
  assert.equal(rows.length,1);assert.equal(executionSummary(rows).net,-105);
});
