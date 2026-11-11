import { el, append, svg } from './dom.js';
import { msg } from './i18n.js';

export function workspaceLabel(entry, entries) {
  return entries.filter((item) => item.display_name === entry.display_name).length > 1
    ? `${entry.display_name} (${entry.name})` : entry.display_name;
}

export function createWorkspacePicker({ entries, activeId, label, onSelect, onManage }) {
  const nativePopover = typeof HTMLElement.prototype.showPopover === 'function';
  const panel = el(nativePopover ? 'div' : 'dialog', {
    id: 'workspace-picker', className: 'workspace-picker',
    'aria-labelledby': 'workspace-picker-title',
  });
  if (nativePopover) panel.setAttribute('popover', 'auto');
  const button = el('button', {
    id: 'workspace-switcher', type: 'button', className: 'workspace-button',
    ariaHasPopup: nativePopover ? 'menu' : 'dialog', ariaExpanded: 'false',
    ariaControls: panel.id, ariaLabel: msg('workspace.current', { name: label }), title: label,
  }, [
    svg('svg', { className: 'workspace-icon', viewBox: '0 0 24 24', 'aria-hidden': 'true' }, [
      svg('path', { d: 'M3 6h7l2 2h9v12H3z', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linejoin': 'round' }),
    ]),
    el('span', { className: 'workspace-button-label' }, label),
    el('span', { 'aria-hidden': 'true' }, '⌄'),
  ]);
  if (nativePopover) button.popoverTargetElement = panel;
  const menu = el('div', { role: 'menu', ariaLabel: msg('workspace.choose') });
  const close = (restoreFocus = true) => {
    button.ariaExpanded = 'false';
    if (nativePopover) {
      if (panel.matches(':popover-open')) panel.hidePopover();
    } else if (panel.open) panel.close();
    if (restoreFocus) button.focus();
  };
  for (const entry of entries) {
    const active = entry.id === activeId;
    append(menu, el('button', {
      type: 'button', role: 'menuitemradio', ariaChecked: String(active), tabIndex: -1,
      className: 'workspace-menu-item',
      onclick: () => {
        close();
        if (!active) onSelect(entry.id);
      },
    }, [
      el('span', { className: 'workspace-entry-label' }, workspaceLabel(entry, entries)),
      active ? el('span', { 'aria-hidden': 'true' }, '✓') : null,
    ]));
  }
  if (!entries.length) append(panel, el('p', { className: 'muted' }, msg('workspace.empty')));
  append(menu, el('button', {
    type: 'button', role: 'menuitem', tabIndex: -1,
    className: 'workspace-menu-item workspace-manage',
    onclick: () => { close(false); onManage(button); },
  }, msg('workspace.manage')));
  panel.prepend(el('h2', { id: 'workspace-picker-title' }, msg('workspace.choose')));
  append(panel, menu);
  document.body.append(panel);
  const items = [...menu.querySelectorAll('button')];
  const position = () => {
    if (!nativePopover || !panel.matches(':popover-open')) return;
    const rect = button.getBoundingClientRect();
    const gutter = 12;
    panel.style.left = `${Math.max(gutter, Math.min(rect.left, window.innerWidth - panel.offsetWidth - gutter))}px`;
    panel.style.top = `${Math.max(gutter, Math.min(rect.bottom + 8, window.innerHeight - panel.offsetHeight - gutter))}px`;
  };
  const focusInitial = (last) => {
    const initial = last ? items.at(-1) : items.find((item) => item.ariaChecked === 'true') || items[0];
    initial.focus();
  };
  const open = (last = false) => {
    if (nativePopover) panel.showPopover();
    else panel.showModal();
    button.ariaExpanded = 'true';
    position();
    focusInitial(last);
  };
  button.addEventListener('click', (event) => {
    event.preventDefault();
    if (button.ariaExpanded === 'true') close();
    else open();
  });
  button.addEventListener('keydown', (event) => {
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault();
      open(event.key === 'ArrowUp');
    }
  });
  menu.addEventListener('keydown', (event) => {
    const index = items.indexOf(document.activeElement);
    let next;
    if (event.key === 'ArrowDown') next = (index + 1) % items.length;
    else if (event.key === 'ArrowUp') next = (index - 1 + items.length) % items.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = items.length - 1;
    else if (event.key === 'Escape' || event.key === 'Tab') {
      if (event.key === 'Escape') event.preventDefault();
      close();
      return;
    } else return;
    event.preventDefault();
    items[next].focus();
  });
  if (nativePopover) {
    panel.addEventListener('beforetoggle', (event) => {
      button.ariaExpanded = String(event.newState === 'open');
    });
    window.addEventListener('resize', position);
    document.addEventListener('scroll', position, true);
    document.addEventListener('dagoberd:language', position);
  } else {
    panel.addEventListener('close', () => { button.ariaExpanded = 'false'; });
    panel.addEventListener('click', (event) => {
      const rect = panel.getBoundingClientRect();
      if (event.target === panel && (event.clientX < rect.left || event.clientX > rect.right
        || event.clientY < rect.top || event.clientY > rect.bottom)) close();
    });
  }
  return button;
}
