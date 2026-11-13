import { bind, builtinLabel, joinMessages, msg } from './i18n.js';
import { append, clear, el } from './dom.js';
import { account, api, state } from './store.js';
import { dimensionsPanel } from './dimensions.js';
import { addForm, adminController, card, editorForm, failureMessage, feedback, inputField, message, retainListPosition } from './admin-ui.js';

export function renderAdmin(root, hooks = {}) {
  const shell = el('div', { className: 'admin-page' });
  clear(root);
  append(root, shell);
  const controller = adminController(shell, hooks);
  append(shell, el('header', { className: 'admin-heading' }, [
    el('p', { className: 'eyebrow' }, msg('ui.administration')),
    el('h1', {}, msg('admin.heading')),
    el('p', {}, msg('admin.intro')),
  ]));
  const accountRegion = el('section', { id: 'admin-account-region', 'aria-label': msg('admin.account_settings') });
  let renderedAccount = null;
  const renderAccount = () => {
    if (renderedAccount === state.accountId) return;
    renderedAccount = state.accountId;
    clear(accountRegion);
    append(accountRegion, el('h2', {}, msg('admin.account_settings')));
    if (!state.accountId) {
      append(accountRegion, card(msg('admin.choose_account'), msg('admin.account_help'), 'admin-account-gate'));
      append(accountRegion.lastChild, el('button', { type: 'button', onclick: () => hooks.onSelectAccount?.() }, msg('admin.choose_account')));
    } else {
      const id = state.accountId;
      append(accountRegion, accountPanel(controller, id), columnsPanel(controller, id), optionsPanel(controller, id));
    }
  };
  controller.onRefresh(renderAccount);
  renderAccount();
  append(shell, accountRegion, inactiveAccountsPanel(controller), referencePanel(controller), dimensionsPanel(controller));
}

function inactiveAccountsPanel(controller) {
  const details = el('details', { id: 'admin-inactive-accounts', className: 'admin-picker' });
  const list = el('div');
  append(details, el('summary', {}, msg('admin.inactive_accounts')), el('p', {}, msg('admin.inactive_accounts_help')), list);
  const render = () => {
    clear(list);
    for (const model of state.metadata.accounts.filter((entry) => !entry.enabled)) {
      const editor = editorForm(controller, `admin-reactivate-${model.id}`, [], msg('admin.reactivate'));
      editor.actions.before(el('p', {}, model.display_name));
      editor.submit = () => controller.run(editor, () => api(`/accounts/${encodeURIComponent(model.id)}`, {
        method: 'PATCH', body: JSON.stringify({ enabled: true, expected_version: model.version }),
      }));
      append(list, editor.form);
    }
    if (!list.children.length) append(list, el('p', {}, msg('admin.empty_inactive_accounts')));
  };
  controller.onRefresh(render);
  render();
  return details;
}

function referencePanel(controller) {
  const panel = card(msg('admin.reference'), msg('admin.reference_help'), 'admin-reference');
  for (const [kind, title, help] of [
    ['tag', msg('ui.tags'), msg('admin.tags_help')],
    ['merchant', msg('admin.merchants'), msg('admin.merchants_help')],
  ]) {
    const section = el('section', { className: 'admin-reference-kind', id: `admin-${kind}s` });
    const list = el('ul', { className: 'admin-reference-list' });
    const count = el('p', { className: 'muted' });
    const sync = () => {
      const entries = kind === 'tag' ? state.metadata.tags : state.metadata.merchants;
      bind(count, 'textContent', msg('admin.entry_count', { count: entries.length }));
      clear(list);
      append(list, entries.length ? entries.map((entry) => el('li', {}, entry.name || entry.canonical_name)) : el('li', {}, msg('admin.empty_reference')));
    };
    append(section, el('h3', {}, title), el('p', {}, help), count, list, addForm(controller, {
      id: `admin-add-${kind}`, title: msg(kind === 'tag' ? 'ui.tag' : 'ui.merchant'),
      request: (payload) => api(`/${kind}s`, { method: 'POST', body: JSON.stringify(payload) }),
    }));
    append(panel, section);
    controller.onRefresh(sync);
    sync();
  }
  return panel;
}

function accountPanel(controller, id) {
  let model = account(id);
  let dirty = false;
  const panel = card(msg('ui.account'), msg('admin.account_edit_help'), 'admin-account');
  const name = inputField('admin-account-name', msg('ui.display_name'), msg('admin.account_name_help'), model.display_name);
  const enabled = el('input', { id: 'admin-account-active', type: 'checkbox', checked: Boolean(model.enabled) });
  const editor = editorForm(controller, 'admin-account-form', [name], msg('ui.save_account'));
  editor.actions.before(el('label', { htmlFor: enabled.id, className: 'admin-check' }, [enabled, msg('ui.active')]));
  const sync = (next = account(id), force = false) => {
    if (!next || (dirty && !force)) return;
    model = next;
    name.input.value = model.display_name;
    enabled.checked = Boolean(model.enabled);
    dirty = false;
    editor.resetConflict();
  };
  editor.form.addEventListener('input', () => { dirty = true; });
  editor.form.addEventListener('change', () => { dirty = true; });
  editor.onReload = () => sync(account(id), true);
  editor.submit = () => {
    const payload = Object.freeze({ display_name: name.input.value.trim(), enabled: enabled.checked, expected_version: model.version });
    controller.run(editor, () => api(`/accounts/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(payload) }), (result) => {
      sync({ ...model, ...payload, version: result.version }, true);
    });
  };
  controller.onRefresh(() => sync());
  append(panel, editor.form);
  return panel;
}

function loadingCard(controller, title, help, id, load) {
  const panel = card(title, help, id);
  const content = el('div');
  append(panel, content);
  let loading = false;
  const reload = async () => {
    if (loading || !controller.current()) return;
    loading = true;
    message(content, msg('admin.loading'));
    try {
      const result = await load();
      if (controller.current() && panel.isConnected) { clear(content); append(content, result); }
    } catch (error) {
      if (controller.current() && panel.isConnected) {
        message(content, msg('admin.load_failed', { title, details: failureMessage(error) }), true);
        append(content, el('button', { type: 'button', onclick: reload }, msg('admin.retry_load')));
      }
    } finally { loading = false; }
  };
  // Start after the card has been attached, so stale response checks have a stable owner.
  queueMicrotask(reload);
  return panel;
}

function columnsPanel(controller, id) {
  return loadingCard(controller, msg('ui.columns_for_this_account'), msg('ui.without_a_selection_all_active_dimensions_are_shown'), 'admin-columns', async () => {
    const selected = await api(`/accounts/${encodeURIComponent(id)}/columns`);
    let chosen = selected.map((item) => item.code);
    const editor = editorForm(controller, 'admin-columns-form', []);
    const list = el('div', { className: 'admin-column-list' });
    editor.actions.before(list);
    const render = (focusCode, control = 'input') => {
      const restore = retainListPosition(list);
      clear(list);
      const dimensions = state.metadata.dimensions;
      const entries = [...chosen.map((code) => dimensions.find((item) => item.code === code)).filter(Boolean), ...dimensions.filter((item) => !chosen.includes(item.code))];
      for (const dimension of entries) {
        const index = chosen.indexOf(dimension.code);
        const label = joinMessages([builtinLabel(dimension.code, dimension.name), dimension.active ? '' : msg('admin.inactive_suffix')]);
        const box = el('input', { type: 'checkbox', dataset: { optionCode: dimension.code }, checked: index >= 0, onchange: () => {
          if (index >= 0) chosen = chosen.filter((code) => code !== dimension.code);
          else chosen.push(dimension.code);
          render(dimension.code);
        } });
        const row = el('div', { className: 'admin-column-row', dataset: { code: dimension.code } }, [el('label', { className: 'admin-check' }, [box, label])]);
        for (const [delta, direction, glyph] of [[-1, 'up', '↑'], [1, 'down', '↓']]) append(row, el('button', {
          type: 'button', dataset: { move: direction }, disabled: index < 0 || index + delta < 0 || index + delta >= chosen.length,
          ariaLabel: msg(`admin.move_${direction}`, { name: label }), onclick: () => {
            [chosen[index], chosen[index + delta]] = [chosen[index + delta], chosen[index]];
            render(dimension.code, `[data-move="${direction}"]`);
          },
        }, glyph));
        append(list, row);
      }
      restore();
      if (focusCode) {
        const row = [...list.children].find((node) => node.dataset.code === focusCode);
        const target = row?.querySelector(control);
        (target && !target.disabled ? target : row?.querySelector('input'))?.focus();
      }
    };
    controller.onRefresh(() => { if (editor.form.isConnected) render(); });
    editor.submit = () => {
      const payload = Object.freeze({ dimension_codes: Object.freeze([...chosen]) });
      controller.run(editor, () => api(`/accounts/${encodeURIComponent(id)}/columns`, { method: 'PUT', body: JSON.stringify(payload) }));
    };
    render();
    return editor.form;
  });
}

function universes() {
  return {
    category: Object.entries(state.metadata.profiles).flatMap(([profile, categories]) => categories.map((category) => ({ code: `${profile}:${category.code}`, label: joinMessages([profile, builtinLabel(category.code, category.name)], ' · ') }))),
    tag: state.metadata.tags.map((tag) => ({ code: tag.name, label: tag.name })),
    dimension_value: state.metadata.dimensions.flatMap((dimension) => dimension.values.map((value) => ({ code: `${dimension.code}:${value.code}`, label: joinMessages([builtinLabel(dimension.code, dimension.name), ' · ', value.name, value.active && dimension.active ? '' : msg('admin.inactive_suffix')]) }))),
  };
}

function optionsPanel(controller, id) {
  return loadingCard(controller, msg('ui.available_values_for_this_account'), msg('ui.an_empty_selection_allows_all_values'), 'admin-options', async () => {
    const selected = await api(`/accounts/${encodeURIComponent(id)}/options/selected`);
    return el('div', {}, ['category', 'tag', 'dimension_value'].map((kind) => picker(controller, id, kind, selected[kind])));
  });
}

function picker(controller, id, kind, chosen) {
  const title = msg({ category: 'ui.categories', tag: 'ui.tags', dimension_value: 'ui.dimension_values' }[kind]);
  const selection = new Set(chosen);
  const details = el('details', { className: 'admin-picker', dataset: { kind } });
  const summary = el('summary');
  const editor = editorForm(controller, `admin-options-${kind}`, []);
  const list = el('div', { className: 'option-picker' });
  editor.actions.before(list);
  const summaryText = () => bind(summary, 'textContent', selection.size ? msg('admin.selection_summary', { title, count: selection.size }) : msg('admin.all_allowed', { title }));
  const render = () => {
    const restore = retainListPosition(list);
    clear(list);
    const universe = universes()[kind];
    // Preserve existing restrictions even if an entry disappeared from current metadata.
    for (const code of selection) if (!universe.some((entry) => entry.code === code)) universe.push({ code, label: code });
    for (const entry of universe) append(list, el('label', {}, [el('input', {
      type: 'checkbox', dataset: { optionCode: entry.code }, checked: selection.has(entry.code), onchange: (event) => {
        if (event.target.checked) selection.add(entry.code); else selection.delete(entry.code);
        summaryText();
      },
    }), entry.label]));
    restore();
    summaryText();
  };
  const save = (codes) => {
    const payload = Object.freeze({ field_kind: kind, codes: Object.freeze(codes) });
    controller.run(editor, () => api(`/accounts/${encodeURIComponent(id)}/options`, { method: 'PUT', body: JSON.stringify(payload) }), () => {
      selection.clear();
      codes.forEach((code) => selection.add(code));
      render();
    });
  };
  editor.submit = () => save([...selection]);
  const reset = el('button', { type: 'button', onclick: () => save([]) }, msg('ui.allow_all'));
  controller.watch(() => { reset.disabled = controller.busy(); });
  append(editor.actions, reset);
  controller.onRefresh(() => { if (details.isConnected) render(); });
  append(details, summary, editor.form);
  render();
  return details;
}
