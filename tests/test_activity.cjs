const test=require('node:test');
const assert=require('node:assert/strict');
const {ActivityFeed,queryFor,shiftDay}=require('../src/upstocks/static/activity.js');
const filters={day:'2026-10-31',level:'',component:'',q:'',include_http:false,limit:25};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
test('latest filter response wins even when an older transport ignores cancellation',async()=>{
  const first=deferred(),second=deferred();let calls=0,oldSignal;
  const feed=new ActivityFeed((q,signal)=>{if(!calls++){oldSignal=signal;return first.promise;}return second.promise;});
  const a=feed.load(filters),b=feed.load({...filters,level:'ERROR'});
  assert.equal(oldSignal.aborted,true);
  second.resolve({events:[{id:20}],next_cursor:null,total:1});await b;
  first.resolve({events:[{id:10}],next_cursor:10,total:15});assert.equal(await a,null);
  assert.deepEqual(feed.rows,[{id:20}]);assert.equal(feed.filters.level,'ERROR');assert.equal(feed.total,1);assert.equal(feed.busy,false);
});
test('failed refresh retains previously loaded rows, cursor and applied filters',async()=>{
  let fail=false;const feed=new ActivityFeed(async()=>{if(fail)throw new Error('offline');return {events:[{id:7}],next_cursor:7,total:2};});
  await feed.load(filters);fail=true;
  await assert.rejects(feed.load({...filters,day:'2026-10-30'}),/offline/);
  assert.deepEqual(feed.rows,[{id:7}]);assert.equal(feed.cursor,7);assert.equal(feed.filters.day,filters.day);assert.equal(feed.busy,false);
});
test('next page replaces rows and uses the applied filters',async()=>{
  const queries=[];const feed=new ActivityFeed(async q=>{queries.push(q.toString());return queries.length===1?{events:[{id:9},{id:8}],next_cursor:8,total:3}:{events:[{id:7},{id:7}],next_cursor:null,total:3};});
  await feed.load(filters);const page=await feed.load({...filters,q:'unapplied'},{direction:'next'});
  assert.deepEqual(feed.rows.map(r=>r.id),[7]);assert.equal(page.pageIndex,1);
  assert.equal(new URLSearchParams(queries[1]).get('before'),'8');assert.equal(new URLSearchParams(queries[1]).has('q'),false);
  assert.equal(await feed.load(filters,{direction:'next'}),null);assert.equal(queries.length,2);
});
test('previous page retraces cursors and disables navigation before the first page',async()=>{
  const queries=[];const feed=new ActivityFeed(async q=>{
    queries.push(q.get('before'));
    return q.get('before')==='8'?{events:[{id:7},{id:6}],next_cursor:6,total:5}:q.get('before')==='6'?{events:[{id:5}],next_cursor:null,total:5}:{events:[{id:9},{id:8}],next_cursor:8,total:5};
  });
  await feed.load(filters);await feed.load(filters,{direction:'next'});await feed.load(filters,{direction:'next'});
  assert.equal(feed.pageIndex,2);
  await feed.load(filters,{direction:'previous'});assert.deepEqual(feed.rows.map(r=>r.id),[7,6]);assert.equal(feed.pageIndex,1);
  await feed.load(filters,{direction:'previous'});assert.equal(feed.pageIndex,0);
  assert.equal(await feed.load(filters,{direction:'previous'}),null);
  assert.deepEqual(queries,[null,'8','6','8',null]);
});
test('refresh stays on the current page and a filter change resets cursor history',async()=>{
  const queries=[];const feed=new ActivityFeed(async q=>{queries.push(q.toString());return {events:[{id:q.has('before')?7:9}],next_cursor:q.has('before')?null:9,total:2};});
  await feed.load(filters);await feed.load(filters,{direction:'next'});await feed.load(filters,{direction:'refresh'});
  assert.equal(new URLSearchParams(queries[2]).get('before'),'9');assert.equal(feed.pageIndex,1);
  await feed.load({...filters,q:'changed',limit:50});
  assert.equal(new URLSearchParams(queries[3]).has('before'),false);assert.equal(feed.pageIndex,0);assert.deepEqual(feed.pageCursors,[null]);
  assert.equal(feed.filters.q,'changed');assert.equal(feed.filters.limit,50);
});
test('failed page navigation preserves rows and position so retry requests the same cursor',async()=>{
  const queries=[];let fail=true;
  const feed=new ActivityFeed(async q=>{queries.push(q.get('before'));if(q.has('before')&&fail)throw new Error('offline');return {events:[{id:q.has('before')?7:9}],next_cursor:q.has('before')?null:9,total:2};});
  await feed.load(filters);await assert.rejects(feed.load(filters,{direction:'next'}),/offline/);
  assert.deepEqual(feed.rows,[{id:9}]);assert.equal(feed.pageIndex,0);assert.deepEqual(feed.pageCursors,[null]);assert.equal(feed.cursor,9);assert.equal(feed.busy,false);
  fail=false;await feed.load(filters,{direction:'next'});assert.equal(feed.pageIndex,1);assert.deepEqual(queries,[null,'9','9']);
});
test('concurrent next clicks request only one page',async()=>{
  const pending=deferred();let calls=0;const feed=new ActivityFeed(async()=>++calls===1?{events:[{id:9}],next_cursor:9,total:2}:pending.promise);
  await feed.load(filters);const first=feed.load(filters,{direction:'next'});
  assert.equal(await feed.load(filters,{direction:'next'}),null);assert.equal(calls,2);
  pending.resolve({events:[{id:8}],next_cursor:null,total:2});await first;assert.equal(feed.pageIndex,1);
});
test('a newer filter can supersede pagination without mixing days',async()=>{
  const page=deferred();let calls=0;const feed=new ActivityFeed(async q=>{calls++;if(calls===1)return {events:[{id:10}],next_cursor:10,total:2};if(calls===2)return page.promise;return {events:[{id:50}],next_cursor:null,total:1};});
  await feed.load(filters);const old=feed.load(filters,{direction:'next'});await feed.load({...filters,day:'2026-10-30'});
  page.resolve({events:[{id:9}],next_cursor:null,total:2});await old;
  assert.deepEqual(feed.rows,[{id:50}]);assert.equal(feed.filters.day,'2026-10-30');
});
test('HTTP source implies request inclusion and search text is encoded literally',()=>{
  const q=queryFor({...filters,component:'http',q:'100%_ & request?id=1'});
  assert.equal(q.get('include_http'),'true');assert.equal(q.get('q'),'100%_ & request?id=1');assert.equal(new URLSearchParams(q.toString()).get('q'),'100%_ & request?id=1');
});
test('day navigation crosses month and leap-year boundaries in UTC',()=>{
  assert.equal(shiftDay('2026-11-01',-1),'2026-10-31');assert.equal(shiftDay('2024-02-28',1),'2024-02-29');assert.equal(shiftDay('2026-12-31',1),'2027-01-01');
});
