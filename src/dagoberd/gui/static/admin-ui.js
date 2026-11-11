import { append, clear, el } from './dom.js';
import { bind, msg } from './i18n.js';
import { api, ApiError, loadOptions, state } from './store.js';

let pendingWrite = false;
const controllers = new Set();
const updateAll = () => {
  for (const controller of controllers) {
    if (!controller.root.isConnected) controllers.delete(controller);
    else controller.update();
  }
};

export const card = (title, help, id) => el('section', { className: 'ui-card panel admin-card', id }, [
  el('h2', {}, title), el('p', { className: 'muted' }, help),
]);
export const feedback = () => el('div', { className: 'admin-feedback', ariaLive: 'polite' });
export function message(node, text, error = false) {
  clear(node);
  append(node, el('p', { role: error ? 'alert' : 'status', className: error ? 'errors' : '' }, text));
}
export const failureMessage = (error) => error instanceof ApiError ? error.message : msg('admin.request_failed');

export function retainListPosition(list) {
  const scroll = list.scrollTop;
  const focused = list.contains(document.activeElement) ? document.activeElement.dataset.optionCode : null;
  return () => {
    if (focused) [...list.querySelectorAll('input')].find((input) => input.dataset.optionCode === focused)?.focus({ preventScroll: true });
    list.scrollTop = scroll;
  };
}

export function inputField(id, label, help, value = '', code = false) {
  const input = el('input', { id, required: true, value });
  const hint = el('small', { id: `${id}-help`, className: 'field-hint' }, help);
  const error = el('small', { id: `${id}-error`, className: 'errors', hidden: true });
  input.setAttribute('aria-describedby', `${hint.id} ${error.id}`);
  const valid = () => input.value.trim().length > 0 && (!code || /^[\p{L}\p{N} ._-]+$/u.test(input.value.trim()));
  input.addEventListener('input', () => {
    const invalid = !valid();
    input.setAttribute('aria-invalid', String(invalid));
    error.hidden = !invalid;
    bind(error, 'textContent', msg(code && input.value.trim() ? 'admin.invalid_code' : 'admin.required'));
  });
  return { input, valid, node: el('div', { className: 'admin-field' }, [el('label', { htmlFor: id }, label), input, hint, error]) };
}

export function adminController(root, hooks) {
  let scope = state.accountIds.join('|');
  const workspace = state.workspaceId;
  const updates = [];
  const listeners = [];
  const current = () => root.isConnected && state.tab === 'admin'
    && state.workspaceId === workspace && state.accountIds.join('|') === scope;
  const update = () => updates.forEach((fn) => fn());
  const controller = { root, current, update, busy: () => pendingWrite,
    watch: (fn) => { updates.push(fn); fn(); },
    onRefresh: (fn) => listeners.push(fn),
    async refresh() {
      const revision = state.metadataRevision;
      const metadata = await api('/meta');
      if (!current()) return;
      state.metadata = metadata;
      hooks.onMetadata?.();
      scope = state.accountIds.join('|');
      await loadOptions(current);
      if (current()) {
        state.metadataLoadedRevision = revision;
        listeners.forEach((fn) => fn());
      }
    },
    async run(editor, request, saved) {
      if (pendingWrite || !current() || !editor.valid()) return;
      pendingWrite = true;
      editor.fields.disabled = true;
      editor.form.setAttribute('aria-busy', 'true');
      updateAll();
      message(editor.status, msg('admin.saving'));
      let succeeded = false;
      try {
        const result = await request();
        succeeded = true;
        state.metadataRevision += 1;
        if (current()) {
          saved?.(result);
          message(editor.status, result.created === false ? msg('admin.already_exists') : result.code ? msg('admin.saved_code', { code: result.code }) : msg('admin.saved'));
        }
        if (current()) await controller.refresh();
      } catch (error) {
        if (current()) {
          message(editor.status, succeeded ? msg('admin.refresh_failed', { details: failureMessage(error) }) : failureMessage(error), true);
          if (succeeded) append(editor.status, el('button', { type: 'button', onclick: () => controller.reload(editor.status) }, msg('admin.retry_load')));
          else if (error.code === 'version_conflict') editor.conflict();
        }
      } finally {
        pendingWrite = false;
        editor.fields.disabled = false;
        editor.form.setAttribute('aria-busy', 'false');
        updateAll();
      }
    },
    async reload(status, apply) {
      if (pendingWrite || !current()) return;
      const buttons = [...status.querySelectorAll('button')];
      buttons.forEach((button) => { button.disabled = true; });
      try {
        await controller.refresh();
        if (current()) { apply?.(); message(status, msg('admin.reloaded')); }
      } catch (error) {
        if (current()) {
          message(status, failureMessage(error), true);
          append(status, el('button', { type: 'button', onclick: () => controller.reload(status, apply) }, msg('admin.retry_load')));
        }
      }
    },
  };
  for (const old of controllers) if (!old.root.isConnected) controllers.delete(old);
  controllers.add(controller);
  return controller;
}

export function editorForm(controller, id, fields, saveLabel = msg('ui.save')) {
  const form = el('form', { id, className: 'admin-form' });
  const fieldset = el('fieldset', { className: 'admin-fields' }, fields.map((field) => field.node));
  const status = feedback();
  const actions = el('div', { className: 'admin-actions' });
  const save = el('button', { type: 'submit', className: 'primary' }, saveLabel);
  append(actions, save);
  append(fieldset, actions);
  append(form, fieldset, status);
  let conflicted = false;
  const editor = { form, fields: fieldset, status, actions, save, valid: () => !conflicted && fields.every((field) => field.valid()),
    submit: null, resetConflict: () => { conflicted = false; reload.hidden = true; controller.update(); },
    conflict: () => { conflicted = true; reload.hidden = false; controller.update(); },
    onReload: null,
  };
  const reload = el('button', { type: 'button', hidden: true, onclick: () => {
    if (window.confirm(String(msg('admin.discard_editor')))) controller.reload(status, () => { editor.onReload?.(); editor.resetConflict(); });
  } }, msg('admin.reload_editor'));
  // Keep the conflict action outside status text, which is replaced after requests.
  append(form, reload);
  controller.watch(() => { save.disabled = controller.busy() || !editor.valid(); reload.disabled = controller.busy(); });
  form.addEventListener('input', () => controller.update());
  form.addEventListener('change', () => controller.update());
  form.addEventListener('submit', (event) => { event.preventDefault(); if (!controller.busy() && editor.valid()) editor.submit?.(); });
  return editor;
}

export function addForm(controller, { id, title, coded = false, request, saved }) {
  const fields = [];
  if (coded) fields.push(inputField(`${id}-code`, msg('ui.code'), msg('admin.code_help'), '', true));
  fields.push(inputField(`${id}-name`, msg('ui.name'), msg('admin.name_help')));
  const editor = editorForm(controller, `${id}-form`, fields, msg('ui.create', { title }));
  editor.submit = () => {
    const values = fields.map((field) => field.input.value.trim());
    const payload = coded ? { code: values[0], name: values[1] } : { name: values[0] };
    controller.run(editor, () => request(payload), (result) => {
      fields.forEach((field) => { field.input.value = ''; });
      saved?.(result);
    });
  };
  return el('details', { id, className: 'admin-add' }, [el('summary', {}, msg('admin.add', { title })), editor.form]);
}
