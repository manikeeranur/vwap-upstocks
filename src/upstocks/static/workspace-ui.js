// Shared display-only filters and sections. No trading API calls.
(function(root) {
  'use strict';
  const iso = date => date.toISOString().slice(0,10);
  function dateRange(preset,today) {
    const end=new Date(today+'T12:00:00Z'),start=new Date(end);
    if (preset==='month') start.setUTCDate(1);
    else if (preset==='year') { start.setUTCMonth(0,1); }
    else if (preset==='30d') start.setUTCDate(start.getUTCDate()-29);
    else if (preset!=='today') return null;
    return {from:iso(start),to:iso(end)};
  }
  function matches(row,filter={}) {
    const q=(filter.q || '').trim().toLowerCase();
    if (q && !String(row.search ?? row.symbol ?? '').toLowerCase().includes(q)) return false;
    if (filter.strategy && row.preset!==filter.strategy || filter.status && !(Array.isArray(row.state) ? row.state.includes(filter.status) : row.state===filter.status) || filter.side && row.side!==filter.side) return false;
    if (filter.day && row.date!==filter.day) return false;
    if (filter.from && (!row.date || row.date<filter.from) || filter.to && (!row.date || row.date>filter.to)) return false;
    const pnl=row.rs ?? row.pnl;
    if (filter.result && (!Number.isFinite(pnl) || (filter.result==='profit' ? pnl<=0 : filter.result==='loss' ? pnl>=0 : pnl!==0))) return false;
    return true;
  }
  function bindFilters({fields,onChange,prefix='f_',reset}) {
    const controls=Object.fromEntries(Object.entries(fields).map(([key,id])=>[key,document.getElementById(id)]));
    const params=new URLSearchParams(location.search);
    for (const [key,input] of Object.entries(controls)) {
      const value=params.get(prefix+key);
      if (value===null) continue;
      if (input.tagName==='SELECT' && ![...input.options].some(o=>o.value===value)) continue;
      input.value=value;
    }
    const get=()=>Object.fromEntries(Object.entries(controls).map(([k,n])=>[k,n.value.trim()]));
    function apply() {
      const value=get(),url=new URL(location.href);
      for (const [k,v] of Object.entries(value)) { if(v) url.searchParams.set(prefix+k,v);else url.searchParams.delete(prefix+k); }
      history.replaceState(null,'',url); onChange?.(value);
    }
    for (const input of Object.values(controls)) input.addEventListener(input.type==='search' || input.type==='text' ? 'input' : 'change',apply);
    if (reset) document.getElementById(reset).addEventListener('click',()=>{
      Object.values(controls).forEach(input=>input.value=''); apply();
    });
    return {get,apply};
  }
  function tabs(container) {
    const name=container.dataset.tabs,buttons=[...container.querySelectorAll('[data-tab]')],panels=[...container.querySelectorAll('[data-panel]')];
    const allowed=b=>b.dataset.viewOnly!=='live' || document.documentElement.dataset.workspaceMode==='live';
    let active=new URLSearchParams(location.search).get('section') || container.dataset.defaultTab || buttons[0]?.dataset.tab;
    const hash=location.hash.slice(1),target=hash && panels.find(p=>p.id===hash || p.querySelector('[id="'+CSS.escape(hash)+'"]'));
    if(target) active=target.dataset.panel;
    function select(value,focus=false) {
      active=buttons.find(b=>b.dataset.tab===value && allowed(b))?.dataset.tab || buttons.find(allowed)?.dataset.tab;
      for (const b of buttons) {
        const selected=b.dataset.tab===active;b.setAttribute('aria-selected',String(selected));b.tabIndex=selected?0:-1;
        if(selected && focus) b.focus();
      }
      for(const p of panels) p.hidden=p.dataset.panel!==active;
      const url=new URL(location.href);url.searchParams.set('section',active);history.replaceState(null,'',url);
      container.dispatchEvent(new CustomEvent('sectionchange',{detail:active}));
    }
    buttons.forEach(b=>{
      b.id ||= name+'-tab-'+b.dataset.tab;b.setAttribute('role','tab');
      b.setAttribute('aria-controls',panels.filter(p=>p.dataset.panel===b.dataset.tab).map((p,i)=>p.id ||= name+'-panel-'+b.dataset.tab+'-'+i).join(' '));
      b.addEventListener('click',()=>select(b.dataset.tab));
      b.addEventListener('keydown',e=>{
        const list=buttons.filter(allowed),index=list.indexOf(b);
        const next=e.key==='ArrowRight'?list[(index+1)%list.length]:e.key==='ArrowLeft'?list[(index+list.length-1)%list.length]:e.key==='Home'?list[0]:e.key==='End'?list.at(-1):null;
        if(next){e.preventDefault();select(next.dataset.tab,true);}
      });
    });
    panels.forEach(p=>{p.setAttribute('role','tabpanel');p.setAttribute('aria-labelledby',name+'-tab-'+p.dataset.panel);});
    const observer=new MutationObserver(()=>{if(container.isConnected)select(active);else observer.disconnect();});
    observer.observe(document.documentElement,{attributes:true,attributeFilter:['data-workspace-mode']});
    select(active);
  }
  function dayIST(value) {
    if(!value) return '';
    const date=new Date(value); if(Number.isNaN(date.getTime())) return '';
    return new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit'}).format(date);
  }
  const api={matches,dayIST,dateRange,bindFilters,tabs};
  if(typeof module==='object' && module.exports) module.exports=api;
  else {
    root.WorkspaceUI=api;
    document.addEventListener('DOMContentLoaded',()=>document.querySelectorAll('[data-tabs]').forEach(tabs));
  }
})(typeof window==='object'?window:globalThis);
