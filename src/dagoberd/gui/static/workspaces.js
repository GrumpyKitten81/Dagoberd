import { el, clear, notify } from './dom.js';
import { msg, bind } from './i18n.js';
import { api, state } from './store.js';

function navigateWorkspace(id) {
  if (!window.confirm(String(msg('workspace.leave')))) return;
  const parameters = new URLSearchParams();
  if (id) parameters.set('workspace', id);
  location.assign(`${location.pathname}?${parameters}`);
}

function managementDialog(entries) {
  const dialog = el('dialog', { className: 'modal' });
  const action = el('select');
  for (const code of ['add', 'init', 'create', 'clone', 'remove', 'adopt-credentials', 'cloud-preview', 'cloud-recover', 'git-status', 'git-trust', 'git-pull', 'git-commit', 'git-push']) {
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
    if (!preview || !window.confirm(String(msg(preview.cloud_root ? 'workspace.confirm_cloud' : 'workspace.confirm_git')))) return;
    try {
      const result = await api('/workspace-actions', { management: true, method: 'POST', body: JSON.stringify({
        action: preview.cloud_root ? 'cloud-run' : 'git-run', name: controls.name.value, preview_id: preview.preview_id,
        fingerprint: preview.fingerprint, confirmed: true,
      }) });
      output.textContent = JSON.stringify(result, null, 2);
      preview = null;
      run.hidden = true;
      if (result.cloud_workspace) location.reload();
    } catch (error) { notify(error.message, 'error'); }
  } }, msg('workspace.execute'));
  run.hidden = true;
  const renderFields = () => {
    clear(fields);
    output.textContent = '';
    preview = null;
    run.hidden = true;
    addField('name', 'workspace.name', entries.find((entry) => entry.id === state.workspaceId)?.name || '');
    if (['add', 'init', 'create', 'clone'].includes(action.value)) addField('path', 'workspace.path');
    if (['init', 'create', 'clone', 'cloud-preview'].includes(action.value)) addField('cloud_root', 'workspace.cloud_root');
    if (action.value === 'clone') addField('remote', 'workspace.remote');
    if (['git-commit', 'adopt-credentials'].includes(action.value)) addField('files', action.value === 'git-commit' ? 'workspace.files' : 'workspace.credentials');
    if (action.value === 'git-commit') addField('message', 'workspace.message');
  };
  action.addEventListener('change', renderFields);
  const submit = el('button', { type: 'button', onclick: async () => {
    const payload = { action: action.value, name: controls.name.value };
    for (const key of ['path', 'remote', 'cloud_root', 'message']) {
      if (controls[key]?.isConnected && controls[key].value) payload[key] = controls[key].value;
    }
    if (controls.files?.isConnected) payload.files = controls.files.value.split('\n').map((value) => value.trim()).filter(Boolean);
    if (['git-trust', 'adopt-credentials', 'cloud-recover'].includes(action.value)) {
      const key = action.value === 'git-trust' ? 'workspace.confirm_private' : action.value === 'cloud-recover' ? 'workspace.confirm_recovery' : 'workspace.confirm_credentials';
      if (!window.confirm(String(msg(key)))) return;
      payload.confirmed = true;
    }
    submit.disabled = true;
    try {
      const result = await api('/workspace-actions', { management: true, method: 'POST', body: JSON.stringify(payload) });
      output.textContent = JSON.stringify(result, null, 2);
      preview = result.preview_id ? result : null;
      run.hidden = !preview;
      if (['add', 'init', 'create', 'clone', 'remove', 'cloud-recover'].includes(action.value)) {
        dialog.close();
        location.reload();
      }
    } catch (error) { notify(error.message, 'error'); }
    finally { submit.disabled = false; }
  } }, msg('workspace.apply'));
  dialog.append(el('h2', {}, msg('workspace.manage')), action, fields, submit, run, output,
    el('button', { type: 'button', onclick: () => dialog.close() }, msg('workspace.close')));
  dialog.addEventListener('close', () => dialog.remove());
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
  const picker = el('select', { onchange: () => navigateWorkspace(picker.value) });
  bind(picker, 'aria-label', msg('workspace.choose'));
  picker.append(el('option', { value: '' }, msg('workspace.choose')));
  for (const entry of entries) picker.append(el('option', { value: entry.id }, entry.display_name));
  picker.value = state.workspaceId;
  document.querySelector('.app-header').prepend(picker,
    el('button', { type: 'button', onclick: () => managementDialog(entries) }, msg('workspace.manage')));
  if (legacy || (state.workspaceId && entries.some((entry) => entry.id === state.workspaceId))) return true;
  clear(app);
  app.append(el('h1', {}, msg('workspace.choose')));
  for (const entry of entries) app.append(el('button', { type: 'button', onclick: () => navigateWorkspace(entry.id) }, entry.display_name));
  app.append(el('button', { type: 'button', onclick: () => managementDialog(entries) }, msg('workspace.manage')));
  for (const button of document.querySelectorAll('[data-tab], #account-scope, #refresh')) button.disabled = true;
  return false;
}
