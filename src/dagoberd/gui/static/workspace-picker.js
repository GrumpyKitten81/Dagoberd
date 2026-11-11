import { el, append, svg } from './dom.js';
import { msg } from './i18n.js';

export function workspaceLabel(entry, entries) {
  return entries.filter((item) => item.display_name === entry.display_name).length > 1
    ? `${entry.display_name} (${entry.name})` : entry.display_name;
}

export function createWorkspacePicker({ entries, activeId, label, onSelect, onManage }) {
  const panel = el('dialog', {
    id: 'workspace-picker', className: 'ui-dialog workspace-picker',
    'aria-labelledby': 'workspace-picker-title',
  });
  const button = el('button', {
    id: 'workspace-switcher', type: 'button', className: 'workspace-button',
    ariaHasPopup: 'dialog', ariaExpanded: 'false',
    ariaControls: panel.id, ariaLabel: msg('workspace.current', { name: label }), title: label,
  }, [
    svg('svg', { className: 'workspace-icon', viewBox: '0 0 24 24', 'aria-hidden': 'true' }, [
      svg('path', { d: 'M3 6h7l2 2h9v12H3z', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.8', 'stroke-linejoin': 'round' }),
    ]),
    el('span', { className: 'workspace-button-label' }, label),
    el('span', { 'aria-hidden': 'true' }, '⌄'),
  ]);
  const menu = el('div', { className: 'workspace-list', role: 'menu', ariaLabel: msg('workspace.choose') });
  const close = () => { panel.close(); button.ariaExpanded = 'false'; };
  for (const entry of entries) {
    const active = entry.id === activeId;
    append(menu, el('button', {
      type: 'button', role: 'menuitemradio', ariaChecked: String(active),
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
  const manage = el('button', {
    type: 'button', onclick: () => { close(); onManage(button); },
  }, msg('workspace.manage'));
  append(panel, el('h2', { id: 'workspace-picker-title' }, msg('workspace.choose')),
    !entries.length ? el('p', { className: 'muted' }, msg('workspace.empty')) : null,
    menu, el('footer', {}, [manage, el('button', { type: 'button', onclick: close }, msg('workspace.close'))]));
  document.body.append(panel);
  const items = [...menu.querySelectorAll('button')];
  const open = (last = false) => {
    panel.showModal();
    button.ariaExpanded = 'true';
    const initial = last ? items.at(-1) : items.find((item) => item.ariaChecked === 'true') || items[0];
    (initial || manage).focus();
  };
  button.addEventListener('click', () => open());
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
    else return;
    event.preventDefault();
    items[next]?.focus();
  });
  panel.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const controls = [...panel.querySelectorAll('button')];
    const first = controls[0];
    const last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  });
  panel.addEventListener('close', () => { button.ariaExpanded = 'false'; });
  panel.addEventListener('click', (event) => {
    const rect = panel.getBoundingClientRect();
    if (event.target === panel && (event.clientX < rect.left || event.clientX > rect.right
      || event.clientY < rect.top || event.clientY > rect.bottom)) close();
  });
  return button;
}
