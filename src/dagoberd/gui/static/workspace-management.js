import { append, clear, el } from './dom.js';
import { bind, msg } from './i18n.js';
import { api, state } from './store.js';
import { WORKSPACE_ACTIONS, WORKSPACE_GROUPS, WORKSPACE_FIELDS, EXECUTION_CONFIRMATIONS, workspacePayload } from './workspace-actions.js';
import { workspaceLabel } from './workspace-picker.js';
import { workspaceResult } from './workspace-results.js';

export function managementDialog({ entries: initialEntries, returnFocus, refreshRegistry, navigate }) {
  let entries = initialEntries;
  let definition = WORKSPACE_ACTIONS[0];
  const drafts = new Map();
  let controls = {};
  let preview = null;
  let revision = 0;
  let busy = false;
  let reloadOnClose = false;
  let selectedTarget = entries.find((entry) => entry.id === state.workspaceId)?.id || entries[0]?.id || '';
  const dialog = el('dialog', {
    className: 'ui-dialog modal workspace-management',
    'aria-labelledby': 'workspace-management-title',
    'aria-describedby': 'workspace-management-intro',
  });
  const group = el('select', { id: 'workspace-management-group' }, WORKSPACE_GROUPS.map((code) =>
    el('option', { value: code }, msg(`management.group.${code}`))));
  const action = el('select', { id: 'workspace-management-action' });
  const actionHelp = el('p', { id: 'workspace-management-action-help', className: 'muted' });
  action.setAttribute('aria-describedby', actionHelp.id);
  const selection = el('fieldset', { className: 'workspace-action-selection' }, [
    el('legend', {}, msg('management.action_heading')),
    el('label', {}, [msg('management.category'), group]),
    el('label', {}, [msg('management.action'), action]), actionHelp,
  ]);
  const fields = el('fieldset', { className: 'workspace-action-fields' });
  const form = el('form', { id: 'workspace-management-form', className: 'dialog-form' }, [selection, fields]);
  const output = el('div');
  const error = el('p', { className: 'errors', role: 'alert', hidden: true });
  const progress = el('p', { role: 'status', hidden: true }, msg('management.busy'));
  const submit = el('button', { type: 'submit', form: form.id, className: 'primary' });
  const run = el('button', { type: 'button', className: 'primary', hidden: true }, msg('workspace.execute'));
  const close = el('button', { type: 'button', onclick: () => { if (!busy) dialog.close(); } }, msg('workspace.close'));
  const body = el('div', { className: 'workspace-management-body' }, [form, progress, error, output]);
  append(dialog, el('header', {}, [
    el('h2', { id: 'workspace-management-title' }, msg('workspace.manage')),
    el('p', { id: 'workspace-management-intro', className: 'muted' }, msg('workspace.local_first')),
  ]), body, el('footer', {}, [submit, run, close]));

  const values = () => Object.fromEntries(Object.entries(controls).map(([id, control]) => [id, control.value]));
  const remember = () => drafts.set(definition.id, values());
  const valid = () => form.checkValidity() && Object.entries(controls).every(([id, control]) =>
    !WORKSPACE_FIELDS[id].required || control.value.trim().length > 0);
  const updateButtons = () => {
    submit.disabled = busy || !valid();
    run.hidden = !preview;
    run.disabled = busy || !preview;
    close.disabled = busy;
  };
  const invalidate = () => {
    revision += 1;
    preview = null;
    clear(output);
    error.hidden = true;
    updateButtons();
  };
  const targetOptions = (control, preferred) => {
    clear(control);
    append(control, el('option', { value: '' }, msg('workspace.choose')),
      entries.map((entry) => el('option', { value: entry.id }, workspaceLabel(entry, entries))));
    control.value = entries.some((entry) => entry.id === preferred) ? preferred : '';
  };
  const renderFields = () => {
    clear(fields);
    controls = {};
    append(fields, el('legend', {}, msg('management.details_heading')));
    const draft = drafts.get(definition.id) || {};
    for (const id of definition.fields) {
      const spec = WORKSPACE_FIELDS[id];
      const control = el(spec.type === 'workspace' ? 'select' : spec.type === 'textarea' ? 'textarea' : 'input', {
        id: `workspace-management-${id}`, required: spec.required,
        pattern: spec.pattern, rows: spec.type === 'textarea' ? 4 : null,
      });
      if (spec.type === 'workspace') targetOptions(control, draft[id] ?? selectedTarget);
      else control.value = draft[id] || '';
      controls[id] = control;
      const help = el('small', { id: `${control.id}-help`, className: 'field-hint' }, msg(spec.help));
      control.setAttribute('aria-describedby', help.id);
      append(fields, el('div', { className: 'workspace-field' }, [
        el('label', { htmlFor: control.id }, msg(spec.label)), control, help,
      ]));
    }
    bind(actionHelp, 'textContent', msg(`management.help.${definition.id}`));
    bind(submit, 'textContent', msg(definition.execution ? 'management.preview' : 'management.apply'));
    fields.disabled = busy;
    invalidate();
  };
  const renderActions = () => {
    clear(action);
    append(action, WORKSPACE_ACTIONS.filter((item) => item.group === group.value).map((item) =>
      el('option', { value: item.id }, msg(`workspace.action.${item.id}`))));
  };
  group.addEventListener('change', () => {
    remember();
    renderActions();
    definition = WORKSPACE_ACTIONS.find((item) => item.id === action.value);
    renderFields();
  });
  action.addEventListener('change', () => {
    remember();
    definition = WORKSPACE_ACTIONS.find((item) => item.id === action.value);
    renderFields();
  });
  const inputChanged = (event) => {
    if (event.target === group || event.target === action) return;
    if (event.target === controls.workspace) selectedTarget = event.target.value;
    remember();
    invalidate();
  };
  form.addEventListener('input', inputChanged);
  form.addEventListener('change', inputChanged);

  const perform = async (executing = false) => {
    if (busy || !valid() || (executing && !preview)) return;
    const operation = definition;
    const source = executing ? preview.payload : workspacePayload(operation, values());
    const confirmation = executing ? EXECUTION_CONFIRMATIONS[operation.execution] : operation.confirm;
    if (confirmation && !window.confirm(String(msg(confirmation)))) return;
    const request = executing ? Object.freeze({
      action: operation.execution, name: source.name,
      preview_id: preview.id, fingerprint: preview.fingerprint, confirmed: true,
    }) : Object.freeze({ ...source, ...(operation.confirm ? { confirmed: true } : {}) });
    const target = operation.creates ? source.name
      : entries.find((entry) => entry.id === source.name)?.display_name || '';
    invalidate();
    const requestRevision = revision;
    busy = true;
    selection.disabled = true;
    fields.disabled = true;
    progress.hidden = false;
    dialog.setAttribute('aria-busy', 'true');
    updateButtons();
    let completed = false;
    try {
      const result = await api('/workspace-actions', { management: true, method: 'POST', body: JSON.stringify(request) });
      completed = true;
      const isPreview = Boolean(operation.execution && !executing);
      if (!isPreview && operation.reload && (source.name === state.workspaceId || result.id === state.workspaceId)) reloadOnClose = true;
      let refreshError = null;
      if (!isPreview && (operation.refresh || executing)) {
        try {
          entries = await refreshRegistry({ operation: operation.id, targetId: source.name, result });
          if (controls.workspace) targetOptions(controls.workspace, selectedTarget);
        } catch (failure) { refreshError = failure; }
      }
      if (!dialog.open || revision !== requestRevision) return;
      if (isPreview) {
        if (!result.preview_id || !result.fingerprint || (result.workspace_id && result.workspace_id !== source.name)) throw new Error(String(msg('management.invalid_preview')));
        preview = Object.freeze({ id: result.preview_id, fingerprint: result.fingerprint, payload: source });
      }
      const workspaceId = operation.creates ? result.workspace_id || result.id : null;
      clear(output);
      append(output, workspaceResult(operation, result, {
        preview: isPreview, target,
        onOpen: workspaceId ? () => navigate(workspaceId) : null,
      }));
      if (refreshError) {
        append(output, el('p', { className: 'errors', role: 'alert' }, msg('management.refresh_failed', { details: refreshError.message })));
      }
    } catch (failure) {
      if (dialog.open && revision === requestRevision) {
        clear(error);
        append(error, failure.message);
        error.hidden = false;
        if (executing && !completed) append(error, ' ', msg('management.execution_failed'));
      }
    } finally {
      busy = false;
      selection.disabled = false;
      fields.disabled = false;
      progress.hidden = true;
      dialog.setAttribute('aria-busy', 'false');
      updateButtons();
    }
  };
  form.addEventListener('submit', (event) => { event.preventDefault(); perform(); });
  run.addEventListener('click', () => perform(true));
  dialog.addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
  dialog.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab' || busy) return;
    const focusable = [...dialog.querySelectorAll('button, select, input, textarea, summary')]
      .filter((node) => !node.disabled && !node.hidden && node.getClientRects().length);
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });
  dialog.addEventListener('close', () => {
    dialog.remove();
    returnFocus?.focus();
    if (reloadOnClose) location.reload();
  });
  group.value = 'local';
  renderActions();
  document.body.append(dialog);
  renderFields();
  dialog.showModal();
  return dialog;
}
