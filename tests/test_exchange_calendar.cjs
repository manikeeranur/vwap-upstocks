const test = require('node:test');
const assert = require('node:assert/strict');
const {iso, session} = require('../src/upstocks/static/exchange-calendar.js');

test('date-only keys stay correct in timezones behind and ahead of IST', () => {
  const old = process.env.TZ;
  try {
    for (const zone of ['America/Los_Angeles','Asia/Kolkata','Pacific/Auckland']) {
      process.env.TZ = zone;
      assert.equal(iso(new Date('2026-11-01T12:00:00')), '2026-11-01');
      assert.equal(session('2026-11-01').badge, 'Weekend');
      assert.equal(session('2026-11-02').kind, 'unknown');
    }
  } finally { if (old === undefined) delete process.env.TZ; else process.env.TZ = old; }
});
test('special weekend sessions override the closed-weekend label', () => {
  const special = {kind:'special',label:'Diwali',session:'14:20–15:30 IST'};
  const data = {exchanges:{NSE:special,NFO:special,BSE:special,BFO:special}};
  assert.equal(session('2026-11-08',data).kind, 'special');
  assert.equal(session('2026-11-08',data).badge, 'Special session');
  assert.equal(session('2026-11-08',data).weekend, true);
});
test('different exchange schedules stay distinct', () => {
  const closed = {kind:'holiday',label:'Exchange holiday'};
  const open = {kind:'open',label:'Trading day'};
  const data = {exchanges:{NSE:closed,NFO:closed,BSE:open,BFO:open}};
  assert.equal(session('2026-11-10',data).kind,'mixed');
  assert.equal(session('2026-11-10',data,'NSE').badge,'Holiday');
  assert.equal(session('2026-11-10',data,'BSE').badge,'');
});

