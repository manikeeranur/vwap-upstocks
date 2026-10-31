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
