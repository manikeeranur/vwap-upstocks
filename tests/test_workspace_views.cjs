
const test = require('node:test');
const assert = require('node:assert/strict');
const {normalize,read} = require('../src/upstocks/static/workspace-view.js');
const {historyRows,executionRows,executionSummary} = require('../src/upstocks/static/portfolio-pnl.js');
const id = 'frontend_live|2026-10-30|09:30|NSE_FO|1';
const order = {qty:10,sold_qty:10,buy:100,sell:110,buy_status:'complete',sell_status:'complete',buy_charges:2,sell_charges:3,symbol:'BROKER CONTRACT'};
const journal = {days:{'2026-10-30':{frontend_live:{lots:1,lot_size:65,trades:[{id,pnl:1000,entry:10,exit:1010,symbol:'PAPER CONTRACT'}]}}},live:{[id]:{'1:kite':order,'1:upstox':{...order,sell:120}}}};
test('mode preference is explicit, validated, and defaults to paper',()=>{
  assert.deepEqual(read('',null),{mode:'paper',broker:'all'});
  assert.deepEqual(read('?mode=live&broker=kite',{mode:'paper'}),{mode:'live',broker:'kite'});
  assert.deepEqual(read('?mode=paper',{mode:'live',broker:'kite'}),{mode:'paper',broker:'kite'});
  assert.deepEqual(normalize({mode:'invalid',broker:'invalid'}),{mode:'paper',broker:'all'});
  assert.deepEqual(normalize({mode:'live',broker:'all'},true),{mode:'live',broker:'upstox'});
});
test('paper results do not use live broker quantities or fill values',()=>{
  const rows=historyRows(journal,{mode:'paper'});
  assert.equal(rows.length,1);assert.equal(rows[0].rs,65000);assert.equal(rows[0].qty,65);
  assert.equal(rows[0].mode,'paper');
});
test('live history uses broker fills, fees, and account filter only',()=>{
  const rows=historyRows(journal,{mode:'live',broker:'kite'});
  assert.equal(rows.length,1);assert.equal(rows[0].rs,95);
  assert.equal(rows[0].buyFill,100);assert.equal(rows[0].sellFill,110);
  assert.equal(rows[0].qty,10);assert.equal(rows[0].symbol,'BROKER CONTRACT');
  assert.equal(rows[0].entry,undefined);assert.equal(rows[0].entry_time,undefined);
  assert.equal(historyRows(journal,{mode:'live'}).length,2);
});
test('live history survives a missing or replaced paper scan',()=>{
  const rows=historyRows({...journal,days:{}},{mode:'live',broker:'kite'});
  assert.equal(rows.length,1);assert.equal(rows[0].rs,95);
  assert.equal(rows[0].date,'2026-10-30');
});
test('live historical totals exclude partial fills and unresolved execution',()=>{
  for(const changed of [{sold_qty:5},{sell:null},{buy_status:'open'}]){
    assert.equal(historyRows({live:{[id]:{'1:kite':{...order,...changed}}}},{mode:'live'}).length,0);
  }
});
test('live overview keeps old open positions even without any paper result',()=>{
  const state={accounts:{'1:kite':{broker:'kite'},'1:upstox':{broker:'upstox'}},orders:{
    '1:kite':{[id]:{buy:{filled_qty:10,fill_price:100,execution_status:'complete',symbol:'REAL'}}},
    '1:upstox':{[id]:{buy:{filled_qty:100,fill_price:100,execution_status:'complete'}}},
    '2:kite':{[id]:{buy:{filled_qty:999,fill_price:1,execution_status:'complete'}}}
  }};
  const rows=executionRows(state,[],{},'kite'), total=executionSummary(rows);
  assert.equal(rows.length,1);assert.equal(total.open,10);assert.equal(total.positions,1);
  assert.equal(total.valuationMissing,1);assert.equal(total.net,null);
  assert.equal(rows[0].symbol,'REAL');
});
test('missing average price does not hide confirmed holdings',()=>{
  const state={accounts:{'1:kite':{broker:'kite'}},orders:{'1:kite':{[id]:{buy:{filled_qty:10,execution_status:'complete'}}}}};
  const total=executionSummary(executionRows(state));
  assert.equal(total.open,10);assert.equal(total.valuationMissing,1);assert.equal(total.pending,1);
});
test('missing sell prices remain unresolved instead of producing net zero',()=>{
  const state={accounts:{'1:kite':{broker:'kite'}},orders:{'1:kite':{[id]:{
    buy:{filled_qty:10,fill_price:100,execution_status:'complete'},
    sell:{filled_qty:10,execution_status:'complete'}
  }}}};
  const total=executionSummary(executionRows(state));
  assert.equal(total.closed,0);assert.equal(total.pending,1);assert.equal(total.net,null);
});


function viewHarness(saved={mode:'paper',broker:'kite'}) {
  const fs=require('node:fs'),vm=require('node:vm');
  const node = (dataset={}) => ({dataset,attrs:{},events:{},setAttribute(k,v){this.attrs[k]=v;},addEventListener(k,v){this.events[k]=v;}});
  const paper=node({viewMode:'paper'}),live=node({viewMode:'live'}),broker=node();
  const items={'[data-view-title]':node(),'[data-view-description]':node(),'[data-view-broker-label]':node(),'[data-view-broker]':broker};
  const bar={querySelectorAll:()=>[paper,live],querySelector:s=>items[s]};
  const links=[{href:'https://example.test/stocks#execution-recovery'},{href:'https://example.test/account'}];
  const location={href:'https://example.test/paper?mode=paper',search:'?mode=paper',origin:'https://example.test'};
  const context={module:{exports:{}},URL,URLSearchParams,location,fetch:()=>{throw new Error('Changing views must not call an API');},
    document:{documentElement:{dataset:{}},querySelector:()=>bar,querySelectorAll:()=>links},
    history:{replaceState:(_state,_title,url)=>{location.href=String(url);location.search=new URL(url).search;}},
    sessionStorage:{getItem:()=>JSON.stringify(saved),setItem:(_key,value)=>{saved=JSON.parse(value);}}};
  vm.runInNewContext(fs.readFileSync(require.resolve('../src/upstocks/static/workspace-view.js'),'utf8'),context);
  return {api:context.module.exports,paper,live,broker,links,location,items,context};
}

test('switching views changes display and navigation without enabling orders',async()=>{
  const h=viewHarness();let changes=0;
  const view=h.api.bind({onChange:()=>{changes++;}});
  await h.live.events.click();
  assert.equal(view.get().mode,'live');assert.equal(view.get().broker,'kite');
  assert.equal(h.context.document.documentElement.dataset.workspaceMode,'live');
  assert.equal(h.live.attrs['aria-pressed'],'true');
  assert.equal(new URL(h.links[0].href).searchParams.get('broker'),'kite');
  assert.equal(new URL(h.links[0].href).hash,'#execution-recovery');
  assert.equal(h.links[1].href,'https://example.test/account');
  await h.paper.events.click();
  assert.equal(view.get().mode,'paper');assert.equal(view.get().broker,'kite');
  assert.equal(h.items['[data-view-broker-label]'].hidden,true);
  assert.equal(new URL(h.location.href).searchParams.has('broker'),false);
  assert.equal(changes,2);
});

test('cancelled unsaved-settings confirmation preserves mode and broker',async()=>{
  const h=viewHarness();let changes=0;
  const view=h.api.bind({beforeChange:async()=>false,onChange:()=>{changes++;}});
  await h.live.events.click();
  assert.equal(view.get().mode,'paper');assert.equal(view.get().broker,'kite');
  assert.equal(h.paper.attrs['aria-pressed'],'true');
  assert.equal(h.live.disabled,false);assert.equal(changes,0);
});

test('closed trades remain visible with gross P&L while charges are pending',()=>{
  const {historySummary} = require('../src/upstocks/static/portfolio-pnl.js');
  const rows=historyRows({live:{[id]:{'1:kite':{...order,sell_charges:null}}}},{mode:'live'});
  assert.equal(rows.length,1);assert.equal(rows[0].rs,100);
  assert.equal(rows[0].pnlBasis,'gross');assert.equal(rows[0].net,null);assert.equal(rows[0].ch,null);
  assert.deepEqual(historySummary(rows),{gross:100,charges:null,net:null,pendingCharges:1,estimatedCharges:0});
});

test('mixed fee availability never mixes gross and net in calendar or journal totals',()=>{
  const {historySummary} = require('../src/upstocks/static/portfolio-pnl.js');
  const mixed={live:{[id]:{'1:kite':{...order,sell_charges:null},'1:upstox':{...order,sell:120}}}};
  const rows=historyRows(mixed,{mode:'live'});
  assert.deepEqual(rows.map(t=>t.rs),[100,200]);
  assert.ok(rows.every(t=>t.pnlBasis==='gross'));
  assert.equal(rows[1].net,195);assert.equal(historySummary(rows).net,null);
  const filtered=historyRows(mixed,{mode:'live',broker:'upstox'});
  assert.equal(filtered[0].pnlBasis,'net');assert.equal(filtered[0].rs,195);
  assert.equal(historySummary(filtered).net,195);
});

test('confirmed October Kite trades appear without paper scans or final fees',()=>{
  const {historySummary} = require('../src/upstocks/static/portfolio-pnl.js');
  const rows=historyRows({live:{
    'harmonic|2026-10-28|10:01|NSE_FO|53636':{'1:kite':{...order,qty:65,sold_qty:65,buy:439.1,sell:543.8,buy_charges:null,sell_charges:null}},
    'harmonic|2026-10-30|11:01|NSE_FO|54045':{'1:kite':{...order,qty:65,sold_qty:65,buy:403.9,sell:349.15,buy_charges:null,sell_charges:null}}
  }},{mode:'live',broker:'kite'});
  assert.equal(rows.length,2);
  assert.deepEqual(rows.map(t=>[t.date,Math.round(t.rs*100)/100]),[['2026-10-28',6805.5],['2026-10-30',-3558.75]]);
  assert.equal(Math.round(historySummary(rows).gross*100)/100,3246.75);
  assert.equal(historySummary(rows).net,null);
});
