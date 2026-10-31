/* Read-only activity explorer. No trading or account mutations. */
(function(root) {
  'use strict';
  const LEVELS=['','INFO','WARNING','ERROR','CRITICAL'];
  const sourceName=value=>value==='http'?'HTTP requests':String(value || '').replaceAll('_',' ').replace(/^./,c=>c.toUpperCase());
  function queryFor(filters,before=null) {
    const params=new URLSearchParams({day:filters.day,include_http:String(filters.include_http || filters.component==='http'),limit:String(filters.limit || 50)});
    for(const key of ['level','component','q'])if(filters[key])params.set(key,String(filters[key]).trim());
    if(before!==null)params.set('before',String(before));
    return params;
  }
  function shiftDay(day,offset) {
    const value=new Date(day+'T12:00:00Z');value.setUTCDate(value.getUTCDate()+offset);
    return value.toISOString().slice(0,10);
  }
  class ActivityFeed {
    constructor(fetchPage) {
      this.fetchPage=fetchPage;this.rows=[];this.cursor=null;this.total=0;this.filters=null;
      this.pageIndex=0;this.pageCursors=[null];this.sequence=0;this.busy=false;
    }
    async load(filters,{direction='first'}={}) {
      const navigating=direction==='next'||direction==='previous';
      if(navigating&&(this.busy||!this.filters))return null;
      if(direction==='next'&&this.cursor===null||direction==='previous'&&this.pageIndex===0)return null;
      this.abort?.abort();const abort=this.abort=new AbortController(),sequence=++this.sequence;
      const selected={...(direction==='first'?filters:this.filters || filters)};
      const pageIndex=direction==='next'?this.pageIndex+1:direction==='previous'?this.pageIndex-1:direction==='refresh'?this.pageIndex:0;
      const before=direction==='next'?this.cursor:direction==='first'?null:this.pageCursors[pageIndex];
      const cursors=direction==='first'?[null]:[...this.pageCursors.slice(0,pageIndex),before];
      this.busy=true;
      try {
        const data=await this.fetchPage(queryFor(selected,before),abort.signal);
        if(sequence!==this.sequence)return null;
        const seen=new Set(),rows=data.events.filter(row=>!seen.has(row.id)&&seen.add(row.id));
        // Commit the page and its cursor history together; failed requests retain the visible page.
        this.rows=rows;this.cursor=data.next_cursor;this.total=data.total ?? rows.length;this.filters=selected;
        this.pageIndex=pageIndex;this.pageCursors=cursors;
        return {rows,cursor:this.cursor,total:this.total,filters:selected,pageIndex};
      } catch(error) {if(sequence!==this.sequence)return null;throw error;}
      finally {if(sequence===this.sequence)this.busy=false;}
    }
  }
  if(typeof module==='object'&&module.exports){module.exports={ActivityFeed,queryFor,shiftDay};return;}
  const $=id=>document.getElementById(id),main=$('main');if(!main?.classList.contains('activity-page'))return;
  const today=main.dataset.today,clock=new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
  const dateLabel=new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',day:'numeric',month:'short',year:'numeric'});
  const number=value=>Number(value).toLocaleString('en-IN');
  const node=(tag,text,className)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(className)el.className=className;return el;};
  const button=(text,callback,className='text-button')=>{const el=node('button',text,className);el.type='button';el.addEventListener('click',callback);return el;};
  async function get(path,signal) {
    const response=await appFetch(path,{signal}),data=await response.json();
    if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Unable to load activity. Please try again.');
    return data;
  }
  const feed=new ActivityFeed((query,signal)=>get('/api/activity?'+query,signal));
  let level='',viewSequence=0,summaryAbort=null,summarySequence=0,searchTimer=null,loadedDay=null,lastFailedDirection='first',healthSequence=0;
  const records=new Map();
  function filters() {return {day:$('day').value,level,component:$('component').value,q:$('search').value.trim(),include_http:$('include-http').checked,limit:Number($('page-size').value)};}
  function hasFilters(value) {return Boolean(value.level || value.component || value.q || value.include_http);}
  function syncControls() {
    document.querySelectorAll('[data-level]').forEach(el=>el.setAttribute('aria-pressed',String(el.dataset.level===level)));
    $('next-day').disabled=$('day').value>=today;$('today').disabled=$('day').value===today;
    $('reset-filters').hidden=!hasFilters(filters());
  }
  function clearFilters() {level='';$('component').value='';$('search').value='';$('include-http').checked=false;syncControls();load();}
  function chips(value) {
    $('filter-chips').replaceChildren();
    for(const [key,label] of [['q','Search'],['level','Severity'],['component','Source'],['include_http','HTTP requests']]) {
      if(!value[key])continue;
      const text=key==='include_http'?'HTTP requests included':label+': '+(key==='component'?sourceName(value[key]):value[key]);
      const chip=button('',()=>{if(key==='level')level='';else if(key==='include_http'){$('include-http').checked=false;if($('component').value==='http')$('component').value='';}else $(key==='q'?'search':'component').value='';syncControls();load();},'filter-chip');
      chip.setAttribute('aria-label','Remove '+text);chip.append(node('span',text),node('b','×'));$('filter-chips').append(chip);
    }
    $('filter-chips').hidden=!$('filter-chips').childElementCount;
  }
  function eventDetails(row,host) {
    const toolbar=node('div',undefined,'detail-toolbar'),meta=node('dl',undefined,'detail-meta'),actions=node('div',undefined,'detail-actions');
    for(const [label,value] of [['Event ID',String(row.id)],['Recorded at',dateLabel.format(new Date(row.created_at))+' · '+clock.format(new Date(row.created_at))+' IST'],['Request ID',row.request_id]]) {
      if(!value)continue;const pair=node('div');pair.append(node('dt',label),node('dd',value));meta.append(pair);
    }
    async function copy(text,label) {
      try {await navigator.clipboard.writeText(text);$('activity-announcement').textContent=label+' copied.';root.notifyUI?.(label+' copied.');}
      catch {root.notifyUI?.('Could not copy. Select the event details and copy them manually.',true);}
    }
    const data={id:row.id,created_at:row.created_at,level:row.level,source:row.component,event:row.kind,message:row.message,request_id:row.request_id,details:row.details};
    actions.append(button('Copy event',()=>copy(JSON.stringify(data,null,2),'Event')));
    if(row.request_id) {
      actions.append(button('Copy request ID',()=>copy(row.request_id,'Request ID')));
      actions.append(button('Find related events',()=>{level='';$('component').value='';$('include-http').checked=true;$('search').value=row.request_id;syncControls();load();}));
    }
    toolbar.append(meta,actions);host.append(toolbar,node('pre',JSON.stringify(row.details ?? {},null,2)));
  }
  function eventRecord(row) {
    const record=node('details',undefined,'event-record');record.dataset.eventId=String(row.id);
    const summary=node('summary',undefined,'event-row'),time=node('time',clock.format(new Date(row.created_at)),'event-time');time.dateTime=row.created_at;time.title=dateLabel.format(new Date(row.created_at))+' · IST';
    const severity=node('span',row.level,'event-level '+(LEVELS.includes(row.level)?row.level:''));
    const content=node('div',undefined,'event-content');content.append(node('p',row.message,'event-message'),node('div',String(row.kind || '').replaceAll('_',' '),'event-kind'));
    const chevron=document.createElementNS('http://www.w3.org/2000/svg','svg');chevron.setAttribute('viewBox','0 0 16 16');chevron.setAttribute('class','event-chevron');chevron.setAttribute('aria-hidden','true');
    const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d','m6 3 5 5-5 5');path.setAttribute('fill','none');path.setAttribute('stroke','currentColor');path.setAttribute('stroke-width','1.5');chevron.append(path);
    summary.append(time,severity,node('span',sourceName(row.component),'event-source'),content,chevron);
    const details=node('div',undefined,'event-detail');record.append(summary,details);
    record.addEventListener('toggle',()=>{if(record.open&&!details.childElementCount)eventDetails(row,details);});
    return record;
  }
  function render(result) {
    const activeIds=new Set(result.rows.map(row=>String(row.id)));let position=$('events').firstElementChild;
    // Keep existing details nodes so a refresh preserves expanded records and focus.
    for(const row of result.rows) {
      const id=String(row.id);let item=records.get(id);
      if(!item){item=eventRecord(row);records.set(id,item);}
      if(item!==position)$('events').insertBefore(item,position);
      position=item.nextElementSibling;
    }
    for(const child of [...$('events').children])if(!activeIds.has(child.dataset.eventId))child.remove();
    for(const id of records.keys())if(!activeIds.has(id))records.delete(id);
    if(!result.rows.length) {
      const empty=node('div',undefined,'event-empty');empty.append(node('div','⌕','empty-symbol'),node('h3',hasFilters(result.filters)?'No matching events':'No events for this day'),node('p',hasFilters(result.filters)?'Try a different search or clear the filters to see more activity.':'Choose another session date to explore your workspace activity.'));
      if(hasFilters(result.filters))empty.append(button('Clear filters',clearFilters,'btn ghost'));$('events').append(empty);
    }
    const first=result.pageIndex*result.filters.limit+1,last=first+result.rows.length-1;
    $('event-count').textContent=result.rows.length?(first===last?number(first):number(first)+'–'+number(last))+' of '+number(result.total):'0 events';
    $('export').href='/api/activity/session.zip?day='+encodeURIComponent(result.filters.day);
    chips(result.filters);
    const url=new URL(location.href);url.search=queryFor(result.filters).toString();history.replaceState(null,'',url);
  }
  function summaryState(day) {
    if(loadedDay!==day)for(const key of ['events','evidence','orders','trades'])$(key+'-total').textContent='—';
    $('summary-day').textContent=dateLabel.format(new Date(day+'T12:00:00Z'))+' · IST';
    $('session-metrics').setAttribute('aria-busy',String(loadedDay!==day));
    $('summary-note').classList.remove('error');$('summary-note').textContent='';$('summary-note').hidden=true;
  }
  function showSummary(data) {
    for(const [id,key] of [['events','events'],['evidence','evidence'],['orders','order_attempts'],['trades','journal_trades']])$(id+'-total').textContent=number(data[key]);
    $('events-caption').textContent=number(data.http_events || 0)+' HTTP requests included';
    const selected=$('component').value,sources=new Set(['application','trading','scanner','research','reconciliation','http',...(data.sources || [])]);if(selected)sources.add(selected);
    $('component').replaceChildren(new Option('All sources',''),...[...sources].sort().map(value=>new Option(sourceName(value),value)));$('component').value=selected;
    loadedDay=data.day;$('session-metrics').setAttribute('aria-busy','false');
  }
  function syncPagination(busy=false) {
    $('previous-page').disabled=busy||!feed.filters||feed.pageIndex===0;
    $('next-page').disabled=busy||!feed.filters||feed.cursor===null;
  }
  async function load(direction='first') {
    clearTimeout(searchTimer);if(!$('filters').reportValidity())return;
    const selected=filters();
    if(!feed.filters||queryFor(selected).toString()!==queryFor(feed.filters).toString())direction='first';
    const navigating=direction==='next'||direction==='previous';
    if(navigating&&(feed.busy||direction==='next'&&feed.cursor===null||direction==='previous'&&feed.pageIndex===0))return;
    const resetScroll=direction!=='refresh';
    const scrollTop=$('event-panel').scrollTop;
    const sequence=++viewSequence;syncControls();lastFailedDirection=direction;
    $('error').hidden=true;$('event-panel').setAttribute('aria-busy','true');syncPagination(true);
    const done=LoadingUI.begin('#events',{initial:!feed.filters,retry:()=>load()});
    if(!navigating) {
      summaryAbort?.abort();summaryAbort=new AbortController();const summaryVersion=++summarySequence;summaryState(selected.day);
      $('export').href='/api/activity/session.zip?day='+encodeURIComponent(selected.day);
      get('/api/activity/session?day='+encodeURIComponent(selected.day),summaryAbort.signal).then(data=>{if(summaryVersion===summarySequence)showSummary(data);}).catch(error=>{
        if(summaryVersion!==summarySequence)return;
        for(const key of ['events','evidence','orders','trades'])$(key+'-total').textContent='—';
        loadedDay=null;$('session-metrics').setAttribute('aria-busy','false');$('summary-note').classList.add('error');$('summary-note').hidden=false;$('summary-note').textContent='Session totals unavailable. Refresh to retry; event browsing is still available.';
      });
    }
    try {
      const result=await feed.load(selected,{direction});if(sequence!==viewSequence || !result)return;
      render(result);$('event-panel').scrollTop=resetScroll?0:scrollTop;
      $('activity-announcement').textContent=$('event-count').textContent+' events loaded.';
    } catch(error) {
      if(sequence!==viewSequence)return;
      done(error);$('error-message').textContent=(feed.rows.length?'Could not update the list. Previously loaded events are still shown. ':'')+(error.message || 'Unable to load activity.');$('error').hidden=false;
      if(!feed.filters)$('event-count').textContent='Unable to load events';
    } finally {
      done();if(sequence===viewSequence){$('event-panel').setAttribute('aria-busy','false');syncPagination();}
    }
  }
  function healthItem(id,value,state,description) {
    $(id+'-health').dataset.state=state;
    $(id+'-health').title=description;
    $(id+'-health-value').textContent=value;
  }
  function renderHealth(data,responseOk) {
    const available=Boolean(data && typeof data.status==='string');
    const warning=!responseOk||data?.status!=='ok'||data?.orders_needing_review>0||data?.audit_failures>0;
    healthItem('system',!available?'Unavailable':warning?'Needs attention':'Healthy',!available||warning?'warning':'ok',
      !available?'Could not retrieve system status. Use Refresh to retry.':warning?'One or more health checks need attention.':'Application health checks passed. Broker connections are shown under My brokers.');
    healthItem('database',data?.database==='ok'?'Available':'Unknown',data?.database==='ok'?'ok':'neutral',
      data?.database==='ok'?'Database health check passed.':'Database health could not be confirmed.');
    const scanner=data?.scanner;
    const scannerLabels={running:'Running',disabled:'Disabled',stopped:'Stopped'};
    healthItem('scanner',scannerLabels[scanner] || 'Unknown',scanner==='running'?'active':scanner==='stopped'?'warning':'neutral',
      scanner==='disabled'?'Automatic scanning is disabled in this environment.':scanner==='running'?'Automatic scanning is running.':scanner==='stopped'?'The scanner is expected to run but is stopped.':'Scanner status could not be confirmed.');
    const jobs=data?.active_backtests,knownJobs=typeof jobs==='number'&&Number.isFinite(jobs)&&jobs>=0;
    healthItem('backtests',knownJobs?number(jobs)+' active':'Unknown',knownJobs&&jobs>0?'active':'neutral',
      !knownJobs?'Backtest status could not be confirmed.':jobs===0?'No backtests are currently running.':number(jobs)+' backtests are currently running.');
    $('health-notices').replaceChildren();
    if(data?.orders_needing_review)$('health-notices').append(node('span',number(data.orders_needing_review)+' orders need review','health-notice'));
    if(data?.audit_failures)$('health-notices').append(node('span',number(data.audit_failures)+' log write failures','health-notice'));
    $('health-notices').hidden=!$('health-notices').childElementCount;
    $('health').setAttribute('aria-busy','false');
  }
  async function health() {
    const sequence=++healthSequence;$('health').setAttribute('aria-busy','true');
    try {
      const response=await appFetch('/health/ready'),data=await response.json();if(sequence!==healthSequence)return;
      renderHealth(data,response.ok);
    } catch {if(sequence===healthSequence)renderHealth(null,false);}
  }
  const saved=new URLSearchParams(location.search),savedDay=saved.get('day');
  if(savedDay&&/^\d{4}-\d{2}-\d{2}$/.test(savedDay)&&savedDay<=today&&!isNaN(Date.parse(savedDay+'T12:00:00Z'))&&new Date(savedDay+'T12:00:00Z').toISOString().slice(0,10)===savedDay)$('day').value=savedDay;
  level=LEVELS.includes(saved.get('level'))?saved.get('level'):'';
  $('search').value=(saved.get('q') || '').slice(0,200);
  const source=(saved.get('component') || '').slice(0,40);if(source&&![...$('component').options].some(o=>o.value===source))$('component').add(new Option(sourceName(source),source));$('component').value=source;
  $('include-http').checked=saved.get('include_http')==='true'||source==='http';
  if(['25','50','100'].includes(saved.get('limit')))$('page-size').value=saved.get('limit');
  $('filters').addEventListener('submit',event=>{event.preventDefault();load();});
  $('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>load(),350);});
  document.querySelectorAll('[data-level]').forEach(el=>el.addEventListener('click',()=>{level=el.dataset.level;syncControls();load();}));
  $('component').addEventListener('change',()=>{if($('component').value==='http')$('include-http').checked=true;load();});
  $('include-http').addEventListener('change',()=>{if(!$('include-http').checked&&$('component').value==='http')$('component').value='';load();});
  for(const id of ['day','page-size'])$(id).addEventListener('change',()=>load());
  $('previous-day').addEventListener('click',()=>{if(!$('day').checkValidity())return;$('day').value=shiftDay($('day').value,-1);load();});
  $('next-day').addEventListener('click',()=>{if(!$('day').checkValidity())return;const next=shiftDay($('day').value,1);if(next<=today){$('day').value=next;load();}});
  $('today').addEventListener('click',()=>{$('day').value=today;load();});
  $('reset-filters').addEventListener('click',clearFilters);
  LoadingUI.on($('refresh'),'click',()=>Promise.all([load('refresh'),health()]));
  LoadingUI.on($('previous-page'),'click',()=>load('previous'));
  LoadingUI.on($('next-page'),'click',()=>load('next'));
  LoadingUI.on($('retry'),'click',()=>load(lastFailedDirection));
  LoadingUI.on($('export'),'click',event=>{event.preventDefault();return LoadingUI.download(event.currentTarget.href);});
  document.addEventListener('keydown',event=>{if(event.key==='/'&&!event.ctrlKey&&!event.metaKey&&!event.altKey&&!event.target.closest('input,textarea,select,[contenteditable]')){event.preventDefault();$('search').focus();}});
  syncControls();load();health();
})(typeof window==='object'?window:globalThis);
