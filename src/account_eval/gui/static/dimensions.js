import { builtinLabel, joinMessages, msg } from './i18n.js';
import { el, notify, append } from './dom.js';
import { api, loadMetadata, loadOptions, state } from './store.js';

export function dimensionsPanel(reload) {
  const panel = el('section', { className: 'panel' }, [
    el('h2', {}, msg("ui.dimensions_and_values")),
    el('p', {}, msg("ui.inactive_values_are_no_longer_offered_for_new")),
  ]);
  for (const dimension of state.metadata.dimensions) {
    append(panel, dimensionRow(dimension, reload));
  }
  return panel;
}

function dimensionRow(dimension, reload) {
  const name = el('input', { value: dimension.name });
  const active = el('input', { type: 'checkbox', checked: Boolean(dimension.active) });
  const save = action(msg("ui.save"), async () => api(`/dimensions/${dimension.code}`, {
    method: 'PATCH', body: JSON.stringify({ name: name.value, active: active.checked, expected_version: dimension.version }),
  }), reload);
  const remove = action(msg("ui.delete"), async () => {
    if (!confirm(msg("ui.delete_dimension", { name: dimension.name }))) return;
    await api(`/dimensions/${dimension.code}`, { method: 'DELETE' });
  }, reload);
  const values = el('div', { className: 'option-picker' });
  for (const value of dimension.values) append(values, valueRow(dimension, value, reload));
  return el('details', { open: false }, [
    el('summary', {}, joinMessages([builtinLabel(dimension.code, dimension.name), ` (${dimension.code})`])),
    el('div', { className: 'toolbar' }, [name, el('label', {}, [active, msg("ui.active")]), save, remove]),
    values,
  ]);
}

function valueRow(dimension, value, reload) {
  const name = el('input', { value: value.name });
  const active = el('input', { type: 'checkbox', checked: Boolean(value.active) });
  const save = action(msg("ui.save"), async () => api(`/dimensions/${dimension.code}/values/${value.code}`, {
    method: 'PATCH', body: JSON.stringify({ name: name.value, active: active.checked, expected_version: value.version }),
  }), reload);
  const remove = action(msg("ui.delete"), async () => {
    if (!confirm(msg("ui.delete_value", { name: value.name }))) return;
    await api(`/dimensions/${dimension.code}/values/${value.code}`, { method: 'DELETE' });
  }, reload);
  return el('div', { className: 'toolbar' }, [el('span', {}, value.code), name, el('label', {}, [active, msg("ui.active")]), save, remove]);
}

function action(label, operation, reload) {
  return el('button', { type: 'button', onclick: async () => {
    try {
      await operation();
      await loadMetadata();
      await loadOptions();
      notify(msg("ui.successful", { label: label }));
      reload();
    } catch (error) {
      notify(error.message, 'error');
    }
  } }, label);
}
