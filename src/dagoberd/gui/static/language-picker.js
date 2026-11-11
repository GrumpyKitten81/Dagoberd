import { append, clear, el, svg } from './dom.js';
import { bind, getLanguage, languages, msg, setLanguage } from './i18n.js';

// Local SVGs keep the language cues consistent without network or emoji fonts.
function flag(code) {
  const rectangle = (y, height, fill) => svg('rect', { y, width: 24, height, fill });
  const shapes = code === 'de' ? [rectangle(0, 16, '#ffce00'), rectangle(0, 10.67, '#dd0000'), rectangle(0, 5.33, '#000000')]
    : code === 'es' ? [rectangle(0, 16, '#aa151b'), rectangle(4, 8, '#f1bf00')]
      : [rectangle(0, 16, '#012169'),
        svg('path', { d: 'M0 0L24 16M24 0L0 16', stroke: '#ffffff', 'stroke-width': 4 }),
        svg('path', { d: 'M0 0L24 16M24 0L0 16', stroke: '#c8102e', 'stroke-width': 1.5 }),
        svg('path', { d: 'M12 0V16M0 8H24', stroke: '#ffffff', 'stroke-width': 5.4 }),
        svg('path', { d: 'M12 0V16M0 8H24', stroke: '#c8102e', 'stroke-width': 3.2 })];
  return svg('svg', {
    viewBox: '0 0 24 16', className: 'language-flag',
    'aria-hidden': 'true', focusable: 'false',
  }, shapes);
}

export function initializeLanguagePicker() {
  const button = document.querySelector('#language-switcher');
  const menu = el('div', {
    id: 'language-menu', className: 'language-menu', popover: 'auto',
    role: 'menu', ariaLabel: msg('language.label'),
  });
  const items = Object.entries(languages).map(([code, name]) => el('button', {
    type: 'button', role: 'menuitemradio', tabIndex: -1, lang: code,
    dataset: { language: code }, onclick: () => { setLanguage(code); close(); },
  }, [flag(code), el('span', {}, name), el('span', { className: 'language-check', 'aria-hidden': 'true' }, '✓')]));
  append(menu, items);
  button.after(menu);
  const isOpen = () => menu.matches(':popover-open');
  const close = (restoreFocus = true) => {
    if (isOpen()) menu.hidePopover();
    if (restoreFocus) button.focus();
  };
  const position = () => {
    const bounds = button.getBoundingClientRect();
    const gap = 8;
    menu.style.left = `${Math.max(gap, Math.min(bounds.right - menu.offsetWidth, innerWidth - menu.offsetWidth - gap))}px`;
    menu.style.top = `${Math.max(gap, Math.min(bounds.bottom + gap, innerHeight - menu.offsetHeight - gap))}px`;
  };
  const update = () => {
    const code = getLanguage();
    clear(button);
    append(button, flag(code), el('span', { 'aria-hidden': 'true' }, code.toUpperCase()),
      el('span', { 'aria-hidden': 'true' }, '⌄'));
    bind(button, 'ariaLabel', msg('language.current', { name: languages[code] }));
    bind(button, 'title', msg('language.current', { name: languages[code] }));
    for (const item of items) item.ariaChecked = String(item.dataset.language === code);
    if (isOpen()) position();
  };
  const open = (initial) => {
    if (!isOpen()) menu.showPopover();
    button.ariaExpanded = 'true';
    position();
    (initial || items.find((item) => item.ariaChecked === 'true')).focus();
  };
  button.addEventListener('click', () => { if (isOpen()) close(); else open(); });
  button.addEventListener('keydown', (event) => {
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    event.preventDefault();
    open(event.key === 'ArrowUp' ? items.at(-1) : items[0]);
  });
  menu.addEventListener('keydown', (event) => {
    const index = items.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') next = (index + 1) % items.length;
    else if (event.key === 'ArrowUp') next = (index - 1 + items.length) % items.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = items.length - 1;
    else if (event.key === 'Escape') { event.preventDefault(); close(); return; }
    else if (event.key === 'Tab') { close(); return; }
    else return;
    event.preventDefault();
    items[next].focus();
  });
  menu.addEventListener('toggle', () => { button.ariaExpanded = String(isOpen()); });
  window.addEventListener('resize', () => { if (isOpen()) position(); });
  window.addEventListener('scroll', () => { if (isOpen()) close(false); }, { passive: true });
  document.addEventListener('dagoberd:language', update);
  update();
  button.disabled = false;
}
