const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('src/upstocks/static/paper.js','utf8');
const functions=source.slice(source.indexOf('function executionClock'),source.indexOf('async function loadExecutionDetails'));
const day='2026-10-30';
function context(details=null) {
  const ctx={Intl,Date,Number,executionDetails:details,S:{intraday_session:{day}},detailsLoading:false,detailsFetchedAt:1,
    fmt:n=>Number(n).toFixed(2),money:n=>'INR '+n.toFixed(2),esc:s=>String(s??'').replace(/[<>&"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c])),
    brokerLogo:()=>'',BROKER_NAMES:{kite:'Kite'},orderCell:()=>''};
  vm.createContext(ctx);vm.runInContext(functions,ctx);return ctx;
}
function row() { return {account:'1:kite',id:'trade',date:day,broker:'kite',symbol:'TEST',
  buy:{at:'11:02:29',filled_qty:65,fill_price:403.9},sell:{at:'15:01:34',filled_qty:65,fill_price:349.15},
  stats:{pending:0,open:0,closed:1,net:-3661.57,realized:-3558.75,charges:102.82,chargesComplete:true,chargesFinal:false}}; }
test('compact row shows actual filled prices, IST order clocks, points and fee quality',()=>{
  const c=context(); const html=c.executionCard(row());
  for(const label of ['11:02:29','15:01:34','INR 403.90','INR 349.15','−54.75 pts','Estimated · after charges','INR 102.82','Remaining 0']) assert.ok(html.includes(label),label);
});
test('absent timestamps and candles remain unavailable, never synthesized from a strategy signal',()=>{
  const c=context();const r=row();r.buy={filled_qty:65,fill_price:403.9};
  const html=c.executionCard(r);assert.ok(html.includes('Time unavailable'));assert.ok(html.includes('Candles unavailable'));
  assert.equal(c.savedOrderTime({created_at:day+'T05:32:29Z',filled_qty:65},day),day+'T05:32:29Z');
  assert.equal(c.executionClock(day+'T05:32:29Z'),'11:02:29');
});
test('peak results are accepted only for the same session, account and current fills',()=>{
  const info={buy_qty:65,sell_qty:65,buy_price:403.9,sell_price:349.15,status:'available',high:{price:416.15,at:day+'T11:03:00+05:30',points:12.25,source:'candle'}};
  const c=context({day,accounts:{'1:kite':{trade:info}}});
  assert.ok(c.executionCard(row()).includes('+12.25 pts'));
  const changed=row();changed.buy.fill_price=410;
  assert.ok(!c.executionCard(changed).includes('+12.25 pts'));
  c.executionDetails.day='2026-10-28';assert.ok(!c.executionCard(row()).includes('+12.25 pts'));
});
