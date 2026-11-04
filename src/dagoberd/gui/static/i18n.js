import { builtinLabels } from './builtin-labels.js';
import en from './locales/en.js';
import de from './locales/de.js';
import es from './locales/es.js';

const definitions = {
  en: {name: 'English', catalog: en},
  de: {name: 'Deutsch', catalog: de},
  es: {name: 'Español', catalog: es},
};
export const languages = Object.fromEntries(Object.entries(definitions).map(([code, definition]) => [code, definition.name]));
export const catalogs = Object.fromEntries(Object.entries(definitions).map(([code, definition]) => [code, definition.catalog]));
const STORAGE_KEY = 'account-eval.gui-language';
let language = 'en';
try {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (Object.hasOwn(languages, stored)) language = stored;
} catch { /* Browser storage can be disabled. */ }

export function getLanguage() { return language; }

export function setLanguage(value) {
  language = Object.hasOwn(languages, value) ? value : 'en';
  try { localStorage.setItem(STORAGE_KEY, language); } catch { /* Keep the session usable. */ }
  document.documentElement.lang = language;
  document.dispatchEvent(new Event('dagoberd:language'));
}

class Message {
  constructor(render) { this.render = render; }
  toString() { return this.render(); }
}

export function msg(key, parameters = {}, pluralCount = parameters.count) {
  return new Message(() => {
    let pattern = Object.hasOwn(catalogs[language], key) ? catalogs[language][key] : Object.hasOwn(en, key) ? en[key] : key;
    if (typeof pattern === 'object') {
      pattern = pattern[new Intl.PluralRules(language).select(Number(pluralCount))] ?? pattern.other;
    }
    return pattern.replace(/\{(\w+)\}/g, (_, name) => typeof parameters[name] === 'number' ? new Intl.NumberFormat(language).format(parameters[name]) : String(parameters[name] ?? `{${name}}`));
  });
}

export function joinMessages(values, separator = '') {
  return new Message(() => values.map(String).join(separator));
}

export function number(value, options = {}) {
  return new Message(() => new Intl.NumberFormat(language, options).format(value));
}

export function amount(value, currency) {
  return new Message(() => new Intl.NumberFormat(language, {
    style: 'currency', currency, minimumFractionDigits: 2, maximumFractionDigits: 2,
  }).format(Number(value) / 100));
}

export function date(value, options = {}) {
  return new Message(() => {
    if (!value) return '—';
    const parsed = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T12:00:00` : value);
    return Number.isNaN(parsed.valueOf()) ? String(value) : new Intl.DateTimeFormat(language, options).format(parsed);
  });
}

const bindings = new WeakMap();
export function bind(node, property, value) {
  const apply = () => {
    if (property === 'ariaLabel' && node.namespaceURI === 'http://www.w3.org/2000/svg') {
      node.setAttribute('aria-label', String(value));
    } else if (property === 'textContent') {
      node.textContent = String(value);
    } else if (property in node) {
      node[property] = String(value);
    } else {
      node.setAttribute(property, String(value));
    }
  };
  let properties = bindings.get(node);
  if (!properties) { properties = new Map(); bindings.set(node, properties); }
  properties.set(property, apply);
  apply();
}

export function isMessage(value) { return value instanceof Message; }

document.addEventListener('dagoberd:language', () => {
  const visit = (node) => {
    for (const apply of bindings.get(node)?.values() || []) apply();
    for (const child of node.childNodes) visit(child);
  };
  visit(document.body);
});
document.documentElement.lang = language;

export function builtinLabel(code, name) {
  const definition = builtinLabels[code]?.find(item => item.names.includes(name));
  return definition ? msg(definition.key) : name;
}

export function statusLabel(code) {
  return Object.hasOwn(en, `status.${code}`) ? msg(`status.${code}`) : code;
}

export function diagnostic(value) {
  if (typeof value !== 'string') return value;
  for (const [key, variants] of Object.entries(en)) {
    const pattern = typeof variants === 'object' ? variants.other : variants;
    if (!key.startsWith('diagnostic.') || typeof pattern !== 'string') continue;
    const names = [...pattern.matchAll(/\{(\w+)\}/g)].map(match => match[1]);
    const source = pattern.split(/\{\w+\}/).map(part => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('(.*?)');
    const match = value.match(new RegExp(`^${source}$`));
    if (match) return msg(key, Object.fromEntries(names.map((name, index) => [name, match[index + 1]])));
  }
  return value;
}

export function errorMessage(code, details) {
  const key = `error.${code}`;
  return msg(Object.hasOwn(en, key) ? key : 'error.generic', {details});
}
