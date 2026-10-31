/* Viewing preferences never call a trading or configuration API. */
(function(root) {
  'use strict';
  const BROKERS = ['upstox','kite','indmoney'];
  function normalize(value, single = false) {
    return {mode:value?.mode === 'live' ? 'live' : 'paper',
      broker:BROKERS.includes(value?.broker) ? value.broker : single ? 'upstox' : 'all'};
  }
  function read(search, saved, single = false) {
    const params = new URLSearchParams(search);
    return normalize(params.has('mode') ? {mode:params.get('mode'),broker:params.has('broker') ? params.get('broker') : saved?.broker} : saved,single);
  }
  function bind(options = {}) {
    const bar = document.querySelector('[data-view-toolbar]');
    const single = !!options.single;
    let saved; try { saved = JSON.parse(sessionStorage.getItem('upstocks.view.v1')); } catch {}
    let value = read(location.search,saved,single), changing = false;
    const descriptions = options.descriptions || {
      paper:'Simulated strategy results · before charges',
      live:'Confirmed broker execution · strategy orders only'
    };
    function paint() {
      document.documentElement.dataset.workspaceMode = value.mode;
      bar.querySelectorAll('[data-view-mode]').forEach(b => {
        b.setAttribute('aria-pressed',String(b.dataset.viewMode === value.mode)); b.disabled = changing;
      });
      bar.querySelector('[data-view-title]').textContent = value.mode === 'paper' ? 'Paper mode' : 'Live mode';
      bar.querySelector('[data-view-description]').textContent = descriptions[value.mode];
      bar.querySelector('[data-view-broker-label]').hidden = value.mode !== 'live';
      const broker = bar.querySelector('[data-view-broker]'); broker.value = value.broker; broker.disabled = changing;
      const url = new URL(location.href);
      url.searchParams.set('mode',value.mode);
      if (value.mode === 'live') url.searchParams.set('broker',value.broker); else url.searchParams.delete('broker');
      history.replaceState(null,'',url);
      try { sessionStorage.setItem('upstocks.view.v1',JSON.stringify(value)); } catch {}
      document.querySelectorAll('a[href]').forEach(link => {
        if (link.getAttribute?.('href')?.startsWith('#')) return;
        const target = new URL(link.href,location.href);
        if (target.origin !== location.origin || !['/paper','/stocks','/journal','/calendar'].includes(target.pathname)) return;
        target.searchParams.set('mode',value.mode);
        if (value.mode === 'live') target.searchParams.set('broker',value.broker); else target.searchParams.delete('broker');
        link.href = target.href;
      });
    }
    async function change(next) {
      if (changing) return;
      next = normalize(next,single);
      if (next.mode === value.mode && next.broker === value.broker) return;
      changing = true; paint();
      try {
        if (options.beforeChange && !await options.beforeChange(next,value)) return;
        value = next; paint();
        await options.onChange?.(value);
      } finally { changing = false; paint(); }
    }
    bar.querySelectorAll('[data-view-mode]').forEach(b => b.addEventListener('click',()=>change({...value,mode:b.dataset.viewMode})));
    bar.querySelector('[data-view-broker]').addEventListener('change',e=>change({...value,broker:e.target.value}));
    paint();
    return {get:()=>({...value}), legacy:()=>value.mode === 'paper' ? 'paper' : value.broker === 'all' ? 'live' : 'live:'+value.broker};
  }
  const api = {normalize,read,bind};
  if (typeof module === 'object' && module.exports) module.exports = api; else root.WorkspaceView = api;
})(typeof window === 'object' ? window : globalThis);

