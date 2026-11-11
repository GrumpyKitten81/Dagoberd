import { el, clear } from './dom.js';
import { msg } from './i18n.js';
import { createWorkspacePicker, workspaceLabel } from './workspace-picker.js';
import { managementDialog } from './workspace-management.js';
import { api, state } from './store.js';

function navigateWorkspace(id) {
  if (!window.confirm(String(msg('workspace.leave')))) return;
  const parameters = new URLSearchParams();
  if (id) parameters.set('workspace', id);
  location.assign(`${location.pathname}?${parameters}`);
}

export async function initializeWorkspaces(app) {
  let entries;
  let legacy = false;
  try {
    const payload = await api('/workspaces', { management: true });
    entries = payload.workspaces;
    legacy = payload.legacy;
  }
  catch (error) {
    if (error.status === 404) return true;
    throw error;
  }
  if (!legacy && !state.workspaceId && entries.length === 1) {
    const parameters = new URLSearchParams(location.search);
    parameters.set('workspace', entries[0].id);
    location.replace(`${location.pathname}?${parameters}`);
    return false;
  }
  const activeEntry = () => entries.find((entry) => entry.id === state.workspaceId);
  const usable = () => (legacy && !state.workspaceId) || Boolean(activeEntry());
  const label = () => legacy && !state.workspaceId ? msg('workspace.standalone')
    : activeEntry() ? workspaceLabel(activeEntry(), entries)
      : state.workspaceId ? msg('workspace.unavailable') : msg('workspace.choose');
  let picker;
  const showGate = () => {
    clear(app);
    state.metadata = null;
    state.options = null;
    app.append(el('h1', {}, msg('workspace.choose')));
    if (state.workspaceId) app.append(el('p', { role: 'alert' }, msg('workspace.unavailable_hint')));
    if (!entries.length) app.append(el('p', {}, msg('workspace.empty')));
    for (const entry of entries) app.append(el('button', {
      type: 'button', onclick: () => navigateWorkspace(entry.id),
    }, workspaceLabel(entry, entries)));
    app.append(el('button', { type: 'button', onclick: () => manage(picker.button) }, msg('workspace.manage')));
    for (const button of document.querySelectorAll('[data-tab], #account-scope, #refresh')) button.disabled = true;
  };
  const updatePicker = () => picker.update(entries, activeEntry()?.id, label());
  const refreshRegistry = async ({ operation, targetId }) => {
    if (operation === 'remove') {
      entries = entries.filter((entry) => entry.id !== targetId);
      updatePicker();
      if (!usable()) showGate();
    }
    const payload = await api('/workspaces', { management: true });
    entries = payload.workspaces;
    legacy = payload.legacy;
    updatePicker();
    if (!usable()) showGate();
    return entries;
  };
  const manage = (returnFocus) => managementDialog({
    entries, returnFocus, refreshRegistry, navigate: navigateWorkspace,
  });
  picker = createWorkspacePicker({
    entries, activeId: activeEntry()?.id, label: label(),
    onSelect: navigateWorkspace, onManage: manage,
  });
  document.querySelector('.brand').insertAdjacentElement('afterend', picker.button);
  if (usable()) return true;
  showGate();
  return false;
}
