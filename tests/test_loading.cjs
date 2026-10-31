
const test=require('node:test'),assert=require('node:assert/strict');
const ui=require('../src/upstocks/static/loading.js');
class Node {
  constructor(tag='DIV'){this.tagName=tag;this.dataset={};this.attrs={};this.children=[];this.innerHTML='old content';this.classes=new Set();this.listeners={};this.disabled=false;this.classList={add:x=>this.classes.add(x),remove:x=>this.classes.delete(x)};}
  setAttribute(k,v){this.attrs[k]=v;} getAttribute(k){return this.attrs[k]??null;} removeAttribute(k){delete this.attrs[k];}
  append(n){this.children.push(n);} replaceChildren(...nodes){this.innerHTML='';this.children=nodes;}
  addEventListener(type,fn){this.listeners[type]=fn;}
  closest(){return this.tagName==='BUTTON'?this:null;} matches(){return this.tagName==='BUTTON';}
}
global.document={documentElement:{dataset:{}},createElement:tag=>new Node(tag.toUpperCase())};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};
test('simultaneous snapshot reads share a request but never persist a financial result',async()=>{
 let calls=0,gate=deferred();global.fetch=()=>{calls++;return gate.promise;};
 const a=ui.request('http://localhost/api/paper',{quiet:true}),b=ui.request('http://localhost/api/paper',{quiet:true});
 gate.resolve(new Response('{"revision":1}',{headers:{'content-type':'application/json'}}));
 assert.equal(calls,1);const [ra,rb]=await Promise.all([a,b]);assert.notEqual(ra,rb);assert.deepEqual(await ra.json(),await rb.json());
 global.fetch=async()=>{calls++;return new Response('{"revision":2}');};
 assert.equal((await (await ui.request('http://localhost/api/paper',{quiet:true})).json()).revision,2);assert.equal(calls,2);
});
test('mutations are neither coalesced nor automatically retried',async()=>{
 let calls=0;global.fetch=async()=>{calls++;throw new Error('offline');};
 await Promise.allSettled([ui.request('/api/paper/config',{method:'POST',quiet:true}),ui.request('/api/paper/config',{method:'POST',quiet:true})]);
 assert.equal(calls,2);
});
test('a caller cancellation remains effective with the request deadline',async()=>{
 const c=new AbortController();let received;
 global.fetch=async(url,init)=>{received=init.signal;return new Response('{}');};
 await ui.request('http://localhost/api/journal',{quiet:true,signal:c.signal});assert.notEqual(received,c.signal);
 c.abort();assert.equal(received.aborted,true);
});
test('refresh preserves existing content and stale completion cannot clear a newer load',()=>{
 const el=new Node();const first=ui.begin(el),second=ui.begin(el);
 assert.equal(el.innerHTML,'old content');first();assert.equal(el.getAttribute('aria-busy'),'true');second();assert.equal(el.getAttribute('aria-busy'),'false');
});
test('initial table failures clear skeletons and provide a safe retry control',()=>{
 const el=new Node('TBODY');let retries=0;
 const done=ui.begin(el,{initial:true,kind:'table',retry:()=>retries++});assert.match(el.innerHTML,/<tr/);
 done(new Error('<script>offline</script>'));done();
 assert.equal(el.getAttribute('aria-busy'),'false');
 const box=el.children[0].children[0].children[0];
 assert.equal(box.children[0].textContent,'<script>offline</script>');
 box.children[1].listeners.click();assert.equal(retries,1);
});
test('repeated action clicks run once; loading cleanup preserves application disabled state',async()=>{
 const button=new Node('BUTTON'),gate=deferred();let calls=0;
 ui.on(button,'click',async()=>{calls++;await gate.promise;button.disabled=true;});
 const event={target:button,preventDefault(){}};
 const task=button.listeners.click(event);await button.listeners.click(event);
 assert.equal(calls,1);assert.equal(button.getAttribute('aria-busy'),'true');
 gate.resolve();await task;assert.equal(button.getAttribute('aria-busy'),null);assert.equal(button.disabled,true);assert.equal(button.classes.has('action-pending'),false);
});
test('failed actions clear busy state and permit a deliberate retry',async()=>{
 const button=new Node('BUTTON');let calls=0;global.notifyUI=()=>{};
 ui.on(button,'click',async()=>{calls++;throw new Error('offline');});
 await button.listeners.click({target:button});await button.listeners.click({target:button});
 assert.equal(calls,2);assert.equal(button.getAttribute('aria-busy'),null);
});

test('selection changes can supersede pending read actions',async()=>{
 const select=new Node('SELECT'),gate=deferred();let calls=0;
 ui.on(select,'change',async()=>{calls++;await gate.promise;});
 const first=select.listeners.change({target:select}),second=select.listeners.change({target:select});
 assert.equal(calls,2);gate.resolve();await Promise.all([first,second]);
});
