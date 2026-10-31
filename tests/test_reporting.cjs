const assert = require('node:assert/strict');
const {test} = require('node:test');
const {summarize} = require('../src/upstocks/static/portfolio-pnl.js');
const trade = {id:'t'};
test('acknowledgements never count as realized profit', () => {
  const r = summarize([trade], {t:{buy:{order_id:'b',qty:100},sell:{order_id:'s',qty:100}}}, {t:200});
  assert.equal(r.realized,0); assert.equal(r.closed,0); assert.equal(r.pending,2); assert.equal(r.net,null);
});
test('partial fills use actual quantities and keep remaining exposure visible', () => {
  const r = summarize([trade], {t:{buy:{filled_qty:10,fill_price:100,execution_status:'complete',charges:5,charges_source:'operator_statement'},sell:{filled_qty:4,fill_price:110,execution_status:'open'}}}, {t:120});
  assert.equal(r.realized,40); assert.equal(r.open,6); assert.equal(r.valuation,120); assert.equal(r.net,null);
});
test('net is available only with complete fills and both charge values', () => {
  const r = summarize([trade], {t:{buy:{filled_qty:10,fill_price:100,execution_status:'complete',charges:5,charges_source:'operator_statement'},sell:{filled_qty:10,fill_price:110,execution_status:'complete',charges:7,charges_source:'operator_statement'}}}, {});
  assert.equal(r.realized,100); assert.equal(r.net,88);
});
test('strategy prices never substitute for missing broker fills', () => {
  const r = summarize([{id:'t',pnl:500}], {t:{buy:{order_id:'b',qty:100}}}, {t:1000});
  assert.equal(r.realized,0); assert.equal(r.valuation,0);
});


test('historical net excludes incomplete quantities and unavailable charges', () => {
  const {settled} = require('../src/upstocks/static/portfolio-pnl.js');
  const order = {qty:10,sold_qty:10,buy:100,sell:110,buy_status:'complete',sell_status:'complete',buy_charges:1,sell_charges:1};
  assert.equal(settled(order),true);
  for (const change of [{sold_qty:5},{buy_status:'open'},{buy:null},{sell_charges:null},{qty:0,sold_qty:0}]) {
    assert.equal(settled({...order,...change}),false);
  }
});

test('fee estimates never appear as final net profit', () => {
  const r = summarize([trade], {t:{buy:{filled_qty:10,fill_price:100,execution_status:'complete',charges:5},sell:{filled_qty:10,fill_price:110,execution_status:'complete',charges:7}}}, {});
  assert.equal(r.net,null);
});

test('broker-calculated fees produce labeled net and stale fees are rejected',()=>{
  const {orderFee} = require('../src/upstocks/static/portfolio-pnl.js');
  const buy={filled_qty:65,fill_price:439.1,execution_status:'complete',estimated_charges:36.60,estimated_charges_qty:65,estimated_charges_notional:2854150};
  const sell={filled_qty:65,fill_price:543.8,execution_status:'complete',estimated_charges:91.48,estimated_charges_qty:65,estimated_charges_notional:3534700};
  const total=summarize([trade],{t:{buy,sell}},{});
  assert.equal(Math.round(total.net*100)/100,6677.42);assert.equal(total.chargesFinal,false);
  assert.equal(orderFee({...buy,filled_qty:64}).amount,null);
  assert.equal(orderFee({...buy,fill_price:440}).amount,null);
  assert.deepEqual(orderFee({...buy,charges:40,charges_source:'operator_statement'}),{amount:40,quality:'final'});
});

test('live history uses the same saved net fees as the overview',()=>{
  const {historyRows,historySummary,settled} = require('../src/upstocks/static/portfolio-pnl.js');
  const orders={qty:65,sold_qty:65,buy_status:'complete',sell_status:'complete',buy_fee_quality:'estimated',sell_fee_quality:'estimated'};
  const rows=historyRows({live:{
    'harmonic|2026-10-28|a':{'1:kite':{...orders,buy:439.1,sell:543.8,buy_charges:36.60,sell_charges:91.48}},
    'harmonic|2026-10-30|b':{'1:kite':{...orders,buy:403.9,sell:349.15,buy_charges:35.64,sell_charges:67.18}}
  }},{mode:'live',broker:'kite'});
  const summary=historySummary(rows);
  assert.equal(rows.length,2);assert.equal(summary.estimatedCharges,2);
  assert.equal(Math.round(summary.charges*100)/100,230.90);
  assert.equal(Math.round(summary.net*100)/100,3015.85);
  assert.equal(rows[0].pnlBasis,'net');assert.equal(rows[0].realCh,false);
  assert.equal(settled({...orders,buy:100,sell:110,buy_charges:1,sell_charges:1}),false);
});
