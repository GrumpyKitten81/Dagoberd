import { el, clear, notify } from './dom.js';
import { msg } from './i18n.js';
import { createWorkspacePicker, workspaceLabel } from './workspace-picker.js';
import { api, state } from './store.js';

function navigateWorkspace(id) {
  if (!window.confirm(String(msg('workspace.leave')))) return;
  const parameters = new URLSearchParams();
  if (id) parameters.set('workspace', id);
  location.assign(`${location.pathname}?${parameters}`);
}

function managementDialog(entries, returnFocus) {
  const dialog = el('dialog', { className: 'ui-dialog modal' });
  const action = el('select');
  for (const code of ['create', 'init', 'add', 'status', 'remove', 'sync-status', 'sync-configure', 'sync-disconnect', 'sync-publish', 'sync-restore', 'snapshot-inspect', 'snapshot-adopt', 'git-init', 'git-status', 'git-remote', 'git-disconnect', 'git-trust', 'git-pull', 'git-commit', 'git-push', 'clone', 'adopt-credentials', 'cloud-preview', 'cloud-recover']) {
    action.append(el('option', { value: code }, msg(`workspace.action.${code}`)));
  }
  const fields = el('div');
  const output = el('pre', { style: 'white-space:pre-wrap;overflow-wrap:anywhere' });
  const controls = {};
  let preview = null;
  const addField = (key, label, value = '') => {
    const input = el(key === 'files' ? 'textarea' : 'input', { value });
    if (key === 'files') input.value = value;
    controls[key] = input;
    fields.append(el('label', {}, [msg(label), input]));
  };
  const run = el('button', { type: 'button', onclick: async () => {
    if (!preview || !window.confirm(String(msg(preview.execution === 'sync-run' ? 'workspace.confirm_snapshot' : preview.execution === 'cloud-run' ? 'workspace.confirm_cloud' : 'workspace.confirm_git')))) return;
    try {
      const execution = preview.execution;
      const result = await api('/workspace-actions', { management: true, method: 'POST', body: JSON.stringify({
        action: preview.execution, name: controls.name.value, preview_id: preview.preview_id,
        fingerprint: preview.fingerprint, confirmed: true,
      }) });
      output.textContent = JSON.stringify(result, null, 2);
      preview = null;
      run.hidden = true;
      if (result.cloud_workspace || execution === 'sync-run') location.reload();
    } catch (error) { notify(error.message, 'error'); }
  } }, msg('workspace.execute'));
  run.hidden = true;
  const renderFields = () => {
    clear(fields);
    output.textContent = '';
    preview = null;
    run.hidden = true;
    addField('name', 'workspace.name', entries.find((entry) => entry.id === state.workspaceId)?.name || '');
    if (['add', 'init', 'create', 'clone', 'snapshot-adopt'].includes(action.value)) addField('path', 'workspace.path');
    if (['init', 'create', 'clone', 'cloud-preview'].includes(action.value)) addField('cloud_root', 'workspace.cloud_root');
    if (action.value === 'sync-configure') addField('cloud_root', 'workspace.snapshot_root');
    if (['snapshot-inspect', 'snapshot-adopt'].includes(action.value)) addField('snapshot', 'workspace.snapshot');
    if (action.value === 'snapshot-adopt') addField('fingerprint', 'workspace.fingerprint');
    if (['clone', 'git-remote'].includes(action.value)) addField('remote', 'workspace.remote');
    if (['git-commit', 'adopt-credentials'].includes(action.value)) addField('files', action.value === 'git-commit' ? 'workspace.files' : 'workspace.credentials');
    if (action.value === 'git-commit') addField('message', 'workspace.message');
  };
  action.addEventListener('change', renderFields);
  const submit = el('button', { type: 'button', onclick: async () => {
    const payload = { action: action.value, name: controls.name.value };
    for (const key of ['path', 'remote', 'cloud_root', 'message', 'snapshot', 'fingerprint']) {
      if (controls[key]?.isConnected && controls[key].value) payload[key] = controls[key].value;
    }
    if (controls.files?.isConnected) payload.files = controls.files.value.split('\n').map((value) => value.trim()).filter(Boolean);
    if (['git-trust', 'adopt-credentials', 'cloud-recover', 'git-init', 'git-remote', 'git-disconnect', 'sync-configure', 'sync-disconnect', 'snapshot-adopt'].includes(action.value)) {
      const key = action.value === 'git-trust' ? 'workspace.confirm_private' : action.value === 'cloud-recover' ? 'workspace.confirm_recovery' : action.value === 'adopt-credentials' ? 'workspace.confirm_credentials' : action.value.startsWith('git-') ? 'workspace.confirm_git' : 'workspace.confirm_snapshot';
      if (!window.confirm(String(msg(key)))) return;
      payload.confirmed = true;
    }
    submit.disabled = true;
    try {
      const result = await api('/workspace-actions', { management: true, method: 'POST', body: JSON.stringify(payload) });
      output.textContent = JSON.stringify(result, null, 2);
      preview = result.preview_id ? { ...result, execution: action.value.startsWith('sync-') ? 'sync-run' : result.cloud_root ? 'cloud-run' : 'git-run' } : null;
      run.hidden = !preview;
      if (['add', 'init', 'create', 'clone', 'remove', 'cloud-recover', 'snapshot-adopt'].includes(action.value)) {
        dialog.close();
        location.reload();
      }
    } catch (error) { notify(error.message, 'error'); }
    finally { submit.disabled = false; }
  } }, msg('workspace.apply'));
  dialog.append(el('h2', {}, msg('workspace.manage')), el('p', {}, msg('workspace.local_first')), action, fields, submit, run, output,
    el('button', { type: 'button', onclick: () => dialog.close() }, msg('workspace.close')));
  dialog.addEventListener('close', () => { dialog.remove(); returnFocus?.focus(); });
  document.body.append(dialog);
  renderFields();
  dialog.showModal();
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
  const active = entries.find((entry) => entry.id === state.workspaceId);
  const standalone = legacy && !state.workspaceId;
  const label = standalone ? msg('workspace.standalone') : active ? workspaceLabel(active, entries)
    : state.workspaceId ? msg('workspace.unavailable') : msg('workspace.choose');
  const picker = createWorkspacePicker({
    entries, activeId: active?.id, label,
    onSelect: navigateWorkspace,
    onManage: (trigger) => managementDialog(entries, trigger),
  });
  document.querySelector('.brand').insertAdjacentElement('afterend', picker);
  if (standalone || active) return true;
  clear(app);
  app.append(el('h1', {}, msg('workspace.choose')));
  if (state.workspaceId) app.append(el('p', { role: 'alert' }, msg('workspace.unavailable_hint')));
  if (!entries.length) app.append(el('p', {}, msg('workspace.empty')));
  for (const entry of entries) app.append(el('button', {
    type: 'button', onclick: () => navigateWorkspace(entry.id),
  }, workspaceLabel(entry, entries)));
  app.append(el('button', {
    type: 'button', onclick: () => managementDialog(entries, picker),
  }, msg('workspace.manage')));
  for (const button of document.querySelectorAll('[data-tab], #account-scope, #refresh')) button.disabled = true;
  return false;
}
