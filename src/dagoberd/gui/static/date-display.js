// Display policy is supplied by /meta; storage, filters and language stay independent.
const fallback = '%d.%m.%Y';
let settings = { locale: null, date_format: 'locale' };
const validPattern = (value) => typeof value === 'string'
  && JSON.stringify((value.match(/%[A-Za-z]/g) || []).sort()) === JSON.stringify(['%Y', '%d', '%m'])
  && !value.replace(/%[Ymd]/g, '').replace(/[./_\- ]/g, '');

export function configureDateDisplay(value) {
  const next = {
    locale: typeof value?.locale === 'string' ? value.locale : null,
    date_format: value?.date_format === undefined || value.date_format === 'locale' ? 'locale'
      : validPattern(value.date_format) ? value.date_format : fallback,
  };
  if (JSON.stringify(next) === JSON.stringify(settings)) return;
  settings = next;
  document.dispatchEvent(new Event('dagoberd:date-display'));
}

function supportedLocale() {
  let candidates = [];
  if (settings.locale) candidates = [settings.locale];
  else {
    try { if (Array.isArray(navigator.languages)) candidates.push(...navigator.languages); } catch { /* Use the fallback candidate. */ }
    try { candidates.push(navigator.language); } catch { /* Use the European fallback. */ }
  }
  for (const value of candidates) {
    if (typeof value !== 'string' || !value) continue;
    try {
      const supported = Intl.DateTimeFormat.supportedLocalesOf([value]);
      if (supported.length) return supported[0];
    } catch { /* Try the next browser preference. */ }
  }
  return null;
}

function validCalendar(value) {
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.valueOf()) && parsed.getUTCFullYear() > 0 && parsed.toISOString().slice(0, 10) === value;
}

export function formatDateDisplay(value, options = {}, language = 'en') {
  if (value === null || value === undefined || value === '') return '—';
  const raw = String(value);
  const dateOnly = /^\d{4}-\d{2}-\d{2}$/.test(raw);
  const prefix = raw.slice(0, 10);
  if (/^\d{4}-\d{2}-\d{2}/.test(raw) && !validCalendar(prefix)) return raw;
  if (!dateOnly && !/^\d{4}-\d{2}-\d{2}[T ]/.test(raw)) return raw;
  const clock = raw.match(/[T ](\d{2}):(\d{2})(?::(\d{2}))?/);
  if (clock && (Number(clock[1]) > 23 || Number(clock[2]) > 59 || Number(clock[3] || 0) > 59)) return raw;
  const parsed = new Date(dateOnly ? `${raw}T00:00:00Z` : raw);
  if (Number.isNaN(parsed.valueOf())) return raw;
  const timeZone = dateOnly ? 'UTC' : options.timeZone;
  const locale = supportedLocale();
  const pattern = settings.date_format === 'locale' ? locale ? null : fallback : settings.date_format;
  let rendered;
  if (pattern) {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {
      calendar: 'gregory', numberingSystem: 'latn', year: 'numeric', month: '2-digit', day: '2-digit', timeZone,
    }).formatToParts(parsed).map((part) => [part.type, part.value]));
    rendered = pattern.replace(/%[Ymd]/g, (token) => ({ '%Y': parts.year.padStart(4, '0'), '%m': parts.month, '%d': parts.day })[token]);
  } else {
    rendered = new Intl.DateTimeFormat(locale, { dateStyle: 'short', calendar: 'gregory', numberingSystem: 'latn', timeZone }).format(parsed);
  }
  if (!dateOnly && options.timeStyle) {
    rendered += ' ' + new Intl.DateTimeFormat(language, { timeStyle: options.timeStyle, timeZone: options.timeZone }).format(parsed);
  }
  return rendered;
}
