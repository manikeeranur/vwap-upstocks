// Patch display fragments in place so polling preserves focus, details and scroll containers.
(function(root) {
  'use strict';
  const key = node => node.nodeType === 1 ? node.getAttribute('data-key') || node.id || null : null;
  function compatible(a,b) { return a.nodeType === b.nodeType && a.nodeName === b.nodeName && key(a) === key(b); }
  function patch(current,next) {
    if (current.nodeType === 3 || current.nodeType === 8) {
      if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
      return;
    }
    if (current.nodeType !== 1) return;
    const focused = current === document.activeElement;
    for (const attr of [...current.attributes]) {
      if (attr.name === 'open' && current.tagName === 'DETAILS' || focused && ['value','checked'].includes(attr.name)) continue;
      if (!next.hasAttribute(attr.name)) current.removeAttribute(attr.name);
    }
    for (const attr of next.attributes) {
      if (attr.name === 'open' && current.tagName === 'DETAILS' || focused && ['value','checked'].includes(attr.name)) continue;
      if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name,attr.value);
    }
    if (focused && ['INPUT','TEXTAREA','SELECT'].includes(current.tagName)) return;
    children(current,next);
    // Attribute changes alone do not reset dirty form-control properties.
    if (current.tagName === 'INPUT') {
      if (current.type !== 'file' && current.value !== next.value) current.value = next.value;
      current.checked = next.checked;
    } else if (current.tagName === 'TEXTAREA' || current.tagName === 'SELECT') {
      if (current.value !== next.value) current.value = next.value;
    }
  }
  function children(parent,next) {
    let current = parent.firstChild;
    for (const desired of [...next.childNodes]) {
      let match = current;
      if (!match || !compatible(match,desired)) {
        match = key(desired) ? [...parent.childNodes].find(n=>compatible(n,desired)) : null;
        if (match) parent.insertBefore(match,current);
        else { match = desired.cloneNode(true); parent.insertBefore(match,current); }
      }
      patch(match,desired);
      current = match.nextSibling;
    }
    while (current) { const remove=current; current=current.nextSibling; remove.remove(); }
  }
  function html(element,value) {
    const template=document.createElement('template'); template.innerHTML=String(value);
    children(element,template.content);
  }
  root.StableDOM={html};
})(window);
