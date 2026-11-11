import { bind, builtinLabel, joinMessages, msg } from './i18n.js';
import { el, append } from './dom.js';
import { api, state } from './store.js';
import { addForm, card, editorForm, feedback, inputField, message } from './admin-ui.js';

const pathFor = (dimension, value) => `/dimensions/${encodeURIComponent(dimension)}${value === undefined ? '' : `/values/${encodeURIComponent(value)}`}`;

export function dimensionsPanel(controller) {
  const panel = card(msg('ui.dimensions_and_values'), msg('admin.dimensions_help'), 'admin-dimensions');
  append(panel, el('p', {}, msg('ui.inactive_values_are_no_longer_offered_for_new')));
  const status = feedback();
  const list = el('div', { className: 'admin-dimension-list' });
  const rows = new Map();
  append(panel, addForm(controller, {
    id: 'admin-add-dimension', title: msg('ui.dimension'), coded: true,
    request: (payload) => api('/dimensions', { method: 'POST', body: JSON.stringify(payload) }),
  }), status, list);
  const sync = () => {
    const dimensions = state.metadata.dimensions;
    for (const dimension of dimensions) {
      if (rows.has(dimension.code) && !rows.get(dimension.code).node.isConnected) rows.delete(dimension.code);
      if (!rows.has(dimension.code)) {
        const row = dimensionCard(controller, dimension, () => message(status, msg('admin.deleted')));
        rows.set(dimension.code, row);
        append(list, row.node);
      }
      rows.get(dimension.code).sync(dimension);
    }
    for (const [code, row] of rows) if (!dimensions.some((item) => item.code === code)) {
      if (row.dirty()) row.missing();
      else { row.node.remove(); rows.delete(code); }
    }
    let empty = list.querySelector('.admin-empty');
    if (!dimensions.length && !empty) append(list, el('p', { className: 'admin-empty' }, msg('admin.no_dimensions')));
    else if (dimensions.length) empty?.remove();
  };
  controller.onRefresh(sync);
  sync();
  return panel;
}

function editableRow(controller, initial, path, id, deleted) {
  let model = initial;
  let dirty = false;
  const name = inputField(`${id}-name`, msg('ui.display_name'), msg('admin.name_help'), model.name);
  const active = el('input', { id: `${id}-active`, type: 'checkbox', checked: Boolean(model.active) });
  const editor = editorForm(controller, id, [name]);
  const statusLabel = el('span', { className: 'ui-badge' });
  editor.fields.prepend(el('p', { className: 'admin-code' }, [el('code', {}, model.code), statusLabel]));
  editor.actions.before(el('label', { className: 'admin-check', htmlFor: active.id }, [active, msg('ui.active')]));
  const setModel = (next, force = false) => {
    if (!dirty || force) {
      model = next;
      name.input.value = next.name;
      active.checked = Boolean(next.active);
      dirty = false;
      editor.resetConflict();
    }
    bind(statusLabel, 'textContent', msg(next.active ? 'ui.active' : 'admin.inactive'));
  };
  editor.form.addEventListener('input', () => { dirty = true; });
  editor.form.addEventListener('change', () => { dirty = true; });
  editor.submit = () => {
    const payload = Object.freeze({ name: name.input.value.trim(), active: active.checked, expected_version: model.version });
    controller.run(editor, () => api(path, { method: 'PATCH', body: JSON.stringify(payload) }), (result) => setModel(result, true));
  };
  const remove = el('button', { type: 'button', onclick: () => {
    if (controller.busy()) return;
    const count = state.metadata.dimensions.find((item) => item.code === initial.code)?.values.length || 0;
    const question = initial.values ? msg('admin.delete_dimension', { name: model.name, count }) : msg('ui.delete_value', { name: model.name });
    if (!window.confirm(String(question))) return;
    controller.run({ ...editor, valid: () => true }, () => api(path, { method: 'DELETE' }), () => {
      dirty = false;
      deleted();
    });
  } }, msg('ui.delete'));
  append(editor.actions, remove);
  controller.watch(() => { remove.disabled = controller.busy(); });
  return { editor, active, setModel, dirty: () => dirty, missing: () => editor.conflict(), get: () => model };
}

function dimensionCard(controller, initial, deleted) {
  let removed = false;
  const node = el('details', { className: 'admin-dimension', dataset: { dimension: initial.code } });
  const summary = el('summary');
  const summaryLabel = el('span');
  const count = el('span', { className: 'ui-badge' });
  append(summary, summaryLabel, count);
  const row = editableRow(controller, initial, pathFor(initial.code), `admin-dimension-${initial.id}`, () => {
    const focus = node.nextElementSibling?.querySelector('summary') || node.previousElementSibling?.querySelector('summary') || document.querySelector('#admin-add-dimension > summary');
    node.remove();
    removed = true;
    focus?.focus({ preventScroll: true });
    deleted();
  });
  const values = el('div', { className: 'admin-value-list' });
  const valueRows = new Map();
  const valueStatus = feedback();
  const adder = addForm(controller, {
    id: `admin-add-value-${initial.id}`, title: msg('ui.dimension_value'), coded: true,
    request: (payload) => api(pathFor(initial.code) + '/values', { method: 'POST', body: JSON.stringify(payload) }),
  });
  append(node, summary, row.editor.form, el('h3', {}, msg('ui.dimension_values')), adder, valueStatus, values);
  const sync = (dimension) => {
    row.setModel(dimension);
    bind(summaryLabel, 'textContent', joinMessages([builtinLabel(dimension.code, dimension.name), ` (${dimension.code})`, dimension.active ? '' : msg('admin.inactive_suffix')]));
    bind(count, 'textContent', msg('admin.value_count', { count: dimension.values.length }));
    for (const value of dimension.values) {
      if (valueRows.has(value.code) && !valueRows.get(value.code).editor.form.isConnected) valueRows.delete(value.code);
      if (!valueRows.has(value.code)) {
        const valueRow = editableRow(controller, value, pathFor(dimension.code, value.code), `admin-value-${value.id}`, () => {
          valueRows.get(value.code).editor.form.remove();
          summary.focus({ preventScroll: true });
          message(valueStatus, msg('admin.deleted'));
        });
        valueRow.editor.form.classList.add('admin-value');
        valueRow.editor.onReload = () => {
          const next = state.metadata.dimensions.find((item) => item.code === dimension.code)?.values.find((item) => item.code === value.code);
          if (next) valueRow.setModel(next, true);
          else valueRow.editor.form.remove();
        };
        valueRows.set(value.code, valueRow);
        append(values, valueRow.editor.form);
      }
      valueRows.get(value.code).setModel(value);
    }
    for (const [code, valueRow] of valueRows) if (!dimension.values.some((value) => value.code === code)) {
      if (valueRow.dirty()) valueRow.missing();
      else { valueRow.editor.form.remove(); valueRows.delete(code); }
    }
    let empty = values.querySelector('.admin-empty');
    if (!dimension.values.length && !empty) append(values, el('p', { className: 'admin-empty' }, msg('admin.no_values')));
    else if (dimension.values.length) empty?.remove();
  };
  row.editor.onReload = () => {
    const next = state.metadata.dimensions.find((item) => item.code === initial.code);
    if (next) row.setModel(next, true);
    else { removed = true; node.remove(); }
  };
  return { node, sync, missing: row.missing, dirty: () => !removed && (row.dirty() || [...valueRows.values()].some((value) => value.dirty())) };
}
