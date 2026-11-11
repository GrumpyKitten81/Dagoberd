import { builtinLabel, msg, joinMessages } from './i18n.js';
import { append, clear, el, notify } from './dom.js';
import { account, api, loadMetadata, loadOptions, state } from './store.js';
import { dimensionsPanel } from './dimensions.js';

export async function renderAdmin(root) {
  clear(root);
  append(root, createPanel(), dimensionsPanel(() => renderAdmin(root)));
  if (!state.accountId) {
    append(root, el('p', {}, msg("ui.select_an_account_above_to_configure_its_options")));
    return;
  }
  append(root, accountPanel(), await columnsPanel(), await optionsPanel());
}

async function columnsPanel() {
  const selected = await api(`/accounts/${state.accountId}/columns`);
  const chosen = selected.length ? selected.map((item) => item.code) : state.metadata.dimensions.map((item) => item.code);
  const list = el('div', { className: 'option-picker' });
  const render = () => {
    list.replaceChildren(...chosen.map((code, index) => {
      const dimension = state.metadata.dimensions.find((item) => item.code === code);
      const enabled = el('input', { type: 'checkbox', checked: true, onchange: (event) => {
        if (!event.target.checked) chosen.splice(index, 1);
        render();
      } });
      return el('div', { className: 'toolbar' }, [enabled, builtinLabel(dimension.code, dimension.name),
        el('button', { type: 'button', disabled: index === 0, onclick: () => { [chosen[index - 1], chosen[index]] = [chosen[index], chosen[index - 1]]; render(); } }, '▲'),
        el('button', { type: 'button', disabled: index === chosen.length - 1, onclick: () => { [chosen[index + 1], chosen[index]] = [chosen[index], chosen[index + 1]]; render(); } }, '▼'),
      ]);
    }));
    for (const dimension of state.metadata.dimensions.filter((item) => !chosen.includes(item.code))) {
      append(list, el('label', {}, [el('input', { type: 'checkbox', onchange: (event) => { if (event.target.checked) chosen.push(dimension.code); render(); } }), ` ${dimension.name}`]));
    }
  };
  render();
  return el('section', { className: 'ui-card panel' }, [
    el('h2', {}, msg("ui.columns_for_this_account")),
    el('p', {}, msg("ui.without_a_selection_all_active_dimensions_are_shown")), list,
    el('button', { type: 'button', onclick: async () => {
      try {
        await api(`/accounts/${state.accountId}/columns`, { method: 'PUT', body: JSON.stringify({ dimension_codes: chosen }) });
        await loadOptions();
        notify(msg("ui.columns_saved"));
      } catch (error) { notify(error.message, 'error'); }
    } }, msg("ui.save")),
  ]);
}

function accountPanel() {
  const current = account(state.accountId);
  const name = el('input', { value: current.display_name });
  const enabled = el('input', { type: 'checkbox', checked: Boolean(current.enabled) });
  const save = el('button', {
    type: 'button',
    onclick: async () => {
      try {
        await api(`/accounts/${current.id}`, {
          method: 'PATCH',
          body: JSON.stringify({
            display_name: name.value,
            enabled: enabled.checked,
            expected_version: current.version,
          }),
        });
        await loadMetadata();
        notify(msg("ui.account_saved"));
      } catch (error) {
        notify(error.message, 'error');
      }
    },
  }, msg("ui.save_account"));
  return el('section', { className: 'ui-card panel' }, [
    el('h2', {}, msg("ui.account")),
    el('div', { className: 'toolbar' }, [
      el('label', {}, [msg("ui.display_name"), name]),
      el('label', {}, [enabled, msg("ui.active")]),
      save,
    ]),
  ]);
}

async function optionsPanel() {
  const selected = await api(`/accounts/${state.accountId}/options/selected`);
  const panel = el('section', { className: 'ui-card panel' }, [
    el('h2', {}, msg("ui.available_values_for_this_account")),
    el('p', {}, msg("ui.an_empty_selection_allows_all_values")),
  ]);
  append(panel,
    picker('category', msg("ui.categories"), categoryUniverse(), selected.category),
    picker('tag', msg("ui.tags"), tagUniverse(), selected.tag),
    picker('dimension_value', msg("ui.dimension_values"), dimensionUniverse(), selected.dimension_value),
  );
  return panel;
}

function categoryUniverse() {
  return Object.entries(state.metadata.profiles).flatMap(([profile, categories]) =>
    categories.map((category) => ({
      code: `${profile}:${category.code}`,
      label: joinMessages([profile, builtinLabel(category.code, category.name)], ' · '),
    })),
  );
}

function tagUniverse() {
  return state.metadata.tags.map((tag) => ({ code: tag.name, label: tag.name }));
}

function dimensionUniverse() {
  return state.metadata.dimensions.flatMap((dimension) =>
    dimension.values.map((value) => ({
      code: `${dimension.code}:${value.code}`,
      label: joinMessages([builtinLabel(dimension.code, dimension.name), value.name], ' · '),
    })),
  );
}

function picker(fieldKind, title, universe, chosen) {
  const selection = new Set(chosen);
  const list = el('div', { className: 'option-picker' });
  for (const entry of universe) {
    const box = el('input', {
      type: 'checkbox',
      checked: selection.has(entry.code),
      onchange: (event) => {
        if (event.target.checked) {
          selection.add(entry.code);
        } else {
          selection.delete(entry.code);
        }
      },
    });
    append(list, el('label', {}, [box, ' ', entry.label]));
  }
  const save = el('button', {
    type: 'button',
    onclick: async () => {
      try {
        await api(`/accounts/${state.accountId}/options`, {
          method: 'PUT',
          body: JSON.stringify({ field_kind: fieldKind, codes: [...selection] }),
        });
        await loadOptions();
        notify(msg("ui.saved", { title: title }));
      } catch (error) {
        notify(error.message, 'error');
      }
    },
  }, msg("ui.save"));
  const reset = el('button', {
    type: 'button',
    onclick: async () => {
      await api(`/accounts/${state.accountId}/options`, {
        method: 'PUT',
        body: JSON.stringify({ field_kind: fieldKind, codes: [] }),
      });
      await loadOptions();
      notify(msg("ui.restriction_removed", { title: title }));
      renderAdmin(document.querySelector('#app'));
    },
  }, msg("ui.allow_all"));
  return el('details', { open: false }, [
    el('summary', {}, msg("admin.selection_summary", { title: title, count: selection.size || msg('ui.all_time') })),
    list,
    el('div', { className: 'toolbar' }, [save, reset]),
  ]);
}

function createPanel() {
  const forms = [
    creator(msg("ui.tag"), [msg("ui.name")], async ([name]) => api('/tags', {
      method: 'POST',
      body: JSON.stringify({ name }),
    })),
    creator(msg("ui.dimension"), [msg("ui.code"), msg("ui.name")], async ([code, name]) => api('/dimensions', {
      method: 'POST',
      body: JSON.stringify({ code, name }),
    })),
    creator(msg("ui.dimension_value"), [msg("ui.dimension"), msg("ui.code"), msg("ui.name")], async ([dimension, code, name]) =>
      api(`/dimensions/${dimension}/values`, {
        method: 'POST',
        body: JSON.stringify({ code, name }),
      }),
    ),
    creator(msg("ui.merchant"), [msg("ui.name")], async ([name]) => api('/merchants', {
      method: 'POST',
      body: JSON.stringify({ name }),
    })),
  ];
  return el('section', { className: 'ui-card panel' }, [el('h2', {}, msg("ui.create_reference_data")), ...forms]);
}

function creator(title, fields, submit) {
  const inputs = fields.map((placeholder) => el('input', { placeholder }));
  const button = el('button', {
    type: 'button',
    onclick: async () => {
      try {
        await submit(inputs.map((input) => input.value.trim()));
        await loadMetadata();
        await loadOptions();
        for (const input of inputs) {
          input.value = '';
        }
        notify(msg("ui.created", { title: title }));
        renderAdmin(document.querySelector('#app'));
      } catch (error) {
        notify(error.message, 'error');
      }
    },
  }, msg("ui.create", { title: title }));
  return el('div', { className: 'toolbar' }, [...inputs, button]);
}
