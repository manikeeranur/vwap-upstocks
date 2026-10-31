// Display-only feedback: no automatic mutation retries or stored financial data.
(function(root) {
  'use strict';
  const regions=new WeakMap(), actions=new WeakSet(), requests=new Map(), scripts=new Map();
  const node=value=>typeof value==='string'?document.querySelector(value):value;
  function skeleton(kind='rows',count=4) {
    const line='<span class="skeleton-line"></span>';
    const content=Array.from({length:count},()=>kind==='table'?'<tr aria-hidden="true"><td colspan="20"><div class="skeleton-row">'+line.repeat(4)+'</div></td></tr>':
      '<div class="skeleton-item" aria-hidden="true">'+line.repeat(kind==='calendar'?1:3)+'</div>').join('');
    return kind==='table'?content:'<div class="skeleton skeleton-'+kind+'" data-skeleton aria-hidden="true">'+content+'</div>';
  }
  function begin(targets,{initial=false,kind='rows',label='Loading…',retry}={}) {
    const token={}, list=(Array.isArray(targets)?targets:[targets]).map(node).filter(Boolean);
    for(const el of list) {
      regions.set(el,token);el.setAttribute('aria-busy','true');el.classList.add('loading-region');
      el.dataset.loadingLabel=label;el.dataset.loadingState=initial?'initial':'refresh';
      if(initial) el.innerHTML=skeleton(el.dataset.skeletonKind || kind,Number(el.dataset.skeletonCount)||4);
    }
    let ended=false;
    return error=>{
      if(ended)return;ended=true;
      for(const el of list) {
        if(regions.get(el)!==token)continue;
        regions.delete(el);el.setAttribute('aria-busy','false');delete el.dataset.loadingState;
        if(error && initial) {
          const box=document.createElement('div');box.className='loading-error';box.setAttribute('role','status');
          const text=document.createElement('span');text.textContent=error.message || 'Unable to load this section.';box.append(text);
          if(retry){const b=document.createElement('button');b.type='button';b.className='btn ghost';b.textContent='Retry';b.addEventListener('click',retry);box.append(b);}
          el.replaceChildren();
          if(el.tagName==='TBODY'){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=20;td.append(box);tr.append(td);el.append(tr);}
          else el.append(box);
        }
      }
    };
  }
  function button(value) {
    const el=node(value);if(!el)return ()=>{};
    const busy=el.getAttribute('aria-busy');
    el.setAttribute('aria-busy','true');el.classList.add('action-pending');
    return ()=>{el.classList.remove('action-pending');busy===null?el.removeAttribute('aria-busy'):el.setAttribute('aria-busy',busy);};
  }
  function on(value,event,handler) {
    const el=node(value);
    el.addEventListener(event,async e=>{
      const key=e.type==='submit'?e.target:e.target.closest?.('button,a.btn') || el;
      const lock=event!=='change';
      if(lock&&actions.has(key)){e.preventDefault();return;}
      if(lock)actions.add(key);
      const done=button(e.submitter || e.target.closest?.('button,a.btn') || (el.matches?.('button')?el:null));
      try {await handler(e);}
      catch(error){if(root.notifyUI)root.notifyUI(error.message || 'Action failed. Please try again.',true);else throw error;}
      finally {if(lock)actions.delete(key);done();}
    });
  }
  let pending=0, progressTimer;
  function progress() {
    if(++pending===1)progressTimer=setTimeout(()=>document.documentElement.dataset.requestPending='true',180);
    let done=false;
    return ()=>{if(done)return;done=true;if(--pending===0){clearTimeout(progressTimer);delete document.documentElement.dataset.requestPending;}};
  }
  async function request(url,options={}) {
    const {quiet=false,timeout=20000,...init}=options;
    const done=quiet?()=>{}:progress();
    const method=(init.method || 'GET').toUpperCase();
    const share=method==='GET'&&!init.signal&&['/api/brokers','/api/paper','/api/stocks'].includes(new URL(String(url),root.location?.href || 'http://localhost').pathname);
    const key=String(url);
    try {
      let promise=share && requests.get(key);
      if(!promise) {
        promise=fetch(url,{...init,signal:init.signal?AbortSignal.any([init.signal,AbortSignal.timeout(timeout)]):AbortSignal.timeout(timeout)});
        if(share){requests.set(key,promise);promise.finally(()=>{if(requests.get(key)===promise)requests.delete(key);}).catch(()=>{});}
      }
      const response=await promise;
      return share?response.clone():response;
    } finally {done();}
  }
  function loadScript(url) {
    if(!scripts.has(url))scripts.set(url,new Promise((resolve,reject)=>{
      const script=document.createElement('script');script.src=url;script.async=true;
      const timer=setTimeout(()=>{script.remove();scripts.delete(url);reject(new Error('Chart library timed out. Try opening the chart again.'));},15000);
      script.onload=()=>{clearTimeout(timer);resolve();};
      script.onerror=()=>{clearTimeout(timer);scripts.delete(url);script.remove();reject(new Error('Chart library could not load. Try again.'));};
      document.head.append(script);
    }));
    return scripts.get(url);
  }
  async function download(url,filename) {
    const response=await root.appFetch(url,{timeout:120000});
    if(!response.ok){const data=await response.json().catch(()=>({}));throw new Error(data.detail || 'Download failed. Try again.');}
    const blob=await response.blob(),link=document.createElement('a'),href=URL.createObjectURL(blob);
    link.href=href;link.download=filename || response.headers.get('Content-Disposition')?.match(/filename="?([^";]+)"?/)?.[1] || 'download';
    link.click();setTimeout(()=>URL.revokeObjectURL(href),1000);
  }
  const api={begin,skeleton,button,on,request,loadScript,download};
  if(typeof module==='object'&&module.exports)module.exports=api;
  else {
    root.LoadingUI=api;
    document.addEventListener('click',async event=>{
      const a=event.target.closest?.('a[href]');
      if(!a || event.defaultPrevented || event.button!==0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || a.target || a.download)return;
      const url=new URL(a.href,location.href);
      if(url.origin===location.origin && (/\.(csv|zip|pdf)$/.test(url.pathname) || url.pathname==='/download')) {
        event.preventDefault();if(actions.has(a))return;actions.add(a);const done=button(a);
        try{await download(url.href);}catch(error){root.notifyUI?.(error.message,true);}finally{actions.delete(a);done();}
        return;
      }
      if(url.origin===location.origin && !url.pathname.startsWith('/api/') && url.pathname!=='/download' && (url.pathname!==location.pathname || url.search!==location.search))document.documentElement.dataset.navigating='true';
    });
    addEventListener('pageshow',()=>{delete document.documentElement.dataset.navigating;});
  }
})(typeof window==='object'?window:globalThis);
