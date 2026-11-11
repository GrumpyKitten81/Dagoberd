import { msg, bind, isMessage, amount } from './i18n.js';
// HINT: native form/list associations are read-only properties; set their attributes.
const ATTRIBUTE_ONLY = new Set(['list', 'form']);
const SVG_NAMESPACE = 'http://www.w3.org/2000/svg';

// DESIGN: every value reaches the DOM as text or as a property, never as parsed HTML.
export function el(tag, attributes = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === null || value === undefined) {
      continue;
    }
    if (key === 'dataset') {
      Object.assign(node.dataset, value);
    } else if (key.startsWith('on')) {
      node.addEventListener(key.slice(2), value);
    } else if (isMessage(value)) {
      bind(node, key, value);
    } else if (key in node && !ATTRIBUTE_ONLY.has(key)) {
      node[key] = value;
    } else {
      node.setAttribute(key, value);
    }
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) {
      continue;
    }
    append(node, child);
  }
  return node;
}

export function svg(tag, attributes = {}, children = []) {
  const node = document.createElementNS(SVG_NAMESPACE, tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === null || value === undefined) {
      continue;
    }
    if (key.startsWith('on')) {
      node.addEventListener(key.slice(2), value);
    } else if (isMessage(value)) {
      bind(node, key, value);
    } else {
      const attribute = { ariaLabel: 'aria-label', className: 'class', tabIndex: 'tabindex' }[key] || key;
      node.setAttribute(attribute, value);
    }
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) {
      continue;
    }
    append(node, child);
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function select(entries, current, onchange, { placeholder = '—' } = {}) {
  const node = el('select', { onchange });
  node.append(el('option', { value: '' }, placeholder));
  for (const entry of entries) {
    if (entry.group) {
      const group = el('optgroup', { label: entry.group });
      for (const item of entry.items) {
        group.append(el('option', { value: item.value, selected: item.value === current }, item.label));
      }
      node.append(group);
    } else {
      node.append(el('option', { value: entry.value, selected: entry.value === current }, entry.label));
    }
  }
  node.value = current || '';
  return node;
}

export function append(node, ...children) {
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    if (child instanceof Node) node.append(child);
    else {
      const text = document.createTextNode('');
      bind(text, 'textContent', child);
      node.append(text);
    }
  }
  return node;
}

export function formatAmount(minorUnits, currency) {
  return amount(minorUnits, currency);
}

export function notify(message, kind = 'info') {
  const banner = document.querySelector('#notice');
  clear(banner);
  banner.className = kind;
  append(banner, message);
  banner.hidden = false;
  if (kind !== 'error') {
    setTimeout(() => {
      banner.hidden = true;
    }, 4000);
  }
}

export function dialog(title, content, actions = []) {
  const node = el('dialog', { className: 'ui-dialog modal' }, [
    el('h3', {}, title),
    content,
    el('footer', {}, [
      ...actions,
      el('button', { onclick: () => node.close(), type: 'button' }, msg("ui.close")),
    ]),
  ]);
  node.addEventListener('close', () => node.remove());
  document.body.append(node);
  node.showModal();
  return node;
}
