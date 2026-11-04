import { readPreference, writePreference } from './browser-storage.js';
import { msg, bind, languages, getLanguage, setLanguage } from './i18n.js';
import { append, clear, el, notify } from './dom.js';
import { renderAdmin } from './admin.js';
import { renderClassification } from './classification.js';
import { renderData } from './data.js';
import { renderOverview } from './dashboard.js';
import { loadMetadata, loadOptions, state } from './store.js';
import { renderTransactions } from './transactions.js';
import { initializeWorkspaces } from './workspaces.js';

const SCOPE_STORAGE_KEY = 'account-eval.gui-scope-v1';
const TABS = {
  overview: renderOverview,
  transactions: renderTransactions,
  admin: renderAdmin,
  data: renderData,
  classification: renderClassification,
};
const app = document.querySelector('#app');

function restoreView() {
  const parameters = new URLSearchParams(location.search);
  const tab = parameters.get('tab');
  if (tab in TABS) {
    state.tab = tab;
  }
  const urlAccounts = parameters.getAll('account').filter(Boolean);
  if (parameters.get('scope') === 'all') {
    state.accountIds = [];
  } else if (urlAccounts.length) {
    state.accountIds = [...new Set(urlAccounts)];
  } else {
    try {
      const stored = JSON.parse(readPreference(SCOPE_STORAGE_KEY) || '[]');
      state.accountIds = Array.isArray(stored) ? stored.filter((item) => typeof item === 'string') : [];
    } catch {
      state.accountIds = [];
    }
  }
  syncSingleAccount();
}

function rememberView() {
  const parameters = new URLSearchParams();
  if (state.workspaceId) parameters.set('workspace', state.workspaceId);
  parameters.set('tab', state.tab);
  if (!state.accountIds.length) parameters.set('scope', 'all');
  else for (const accountId of state.accountIds) parameters.append('account', accountId);
  history.replaceState(null, '', `${location.pathname}?${parameters}`);
}

function syncSingleAccount() {
  state.accountId = state.accountIds.length === 1 ? state.accountIds[0] : '';
}

async function render() {
  rememberView();
  try {
    await TABS[state.tab](app);
  } catch (error) {
    notify(error.message, 'error');
  }
}

function scopeLabel() {
  if (!state.accountIds.length) {
    return msg("ui.all_accounts");
  }
  const selected = new Set(state.accountIds);
  const exactGroup = state.metadata.account_groups.find((group) =>
    group.account_ids.length === selected.size && group.account_ids.every((id) => selected.has(id))
  );
  if (exactGroup) {
    return exactGroup.name;
  }
  if (state.accountIds.length === 1) {
    return state.metadata.accounts.find((item) => item.id === state.accountIds[0])?.display_name || msg("ui.1_account");
  }
  return msg("scope.account_count", { count: state.accountIds.length });
}

function updateScopeButton() {
  const button = document.querySelector('#account-scope');
  bind(button, 'textContent', scopeLabel());
  bind(button, 'title', msg('ui.select_account_scope'));
}

function showScopeDialog() {
  const activeAccounts = state.metadata.accounts.filter((item) => item.enabled);
  const activeAccountIds = activeAccounts.map((item) => item.id);
  const activeAccountIdSet = new Set(activeAccountIds);
  const selected = new Set(
    state.accountIds.length ? state.accountIds : activeAccountIds,
  );
  const node = el('dialog', { className: 'modal scope-dialog' });
  const accountList = el('div', { className: 'scope-list' });
  const applyButton = el('button', {
    type: 'button',
    className: 'primary',
    disabled: selected.size === 0,
    onclick: async () => {
      const allActiveSelected = selected.size === activeAccountIds.length
        && activeAccountIds.every((id) => selected.has(id));
      state.accountIds = allActiveSelected ? [] : [...selected];
      syncSingleAccount();
      writePreference(SCOPE_STORAGE_KEY, JSON.stringify(state.accountIds));
      node.close();
      updateScopeButton();
      await loadOptions();
      await render();
    },
  }, msg("ui.apply"));
  const renderAccounts = () => {
    clear(accountList);
    for (const account of activeAccounts) {
      append(accountList, el('label', {}, [
        el('input', {
          type: 'checkbox',
          checked: selected.has(account.id),
          onchange: (event) => {
            if (event.target.checked) selected.add(account.id);
            else selected.delete(account.id);
            applyButton.disabled = selected.size === 0;
          },
        }),
        el('span', {}, [account.display_name, el('small', {}, account.provider_name)]),
      ]));
    }
  };
  const selectAccounts = (accountIds) => {
    selected.clear();
    accountIds.filter((id) => activeAccountIdSet.has(id)).forEach((id) => selected.add(id));
    applyButton.disabled = selected.size === 0;
    renderAccounts();
  };
  renderAccounts();
  const presets = el('div', { className: 'scope-presets' }, [
    el('button', {
      type: 'button',
      onclick: () => selectAccounts(activeAccountIds),
    }, msg("ui.all_active_accounts")),
    el('button', {
      type: 'button',
      onclick: () => selectAccounts([]),
    }, msg("ui.clear_selection")),
    ...state.metadata.account_groups.map((group) => el('button', {
      type: 'button',
      onclick: () => selectAccounts(group.account_ids),
    }, group.name)),
  ]);
  append(node,
    el('h2', {}, msg("ui.account_scope")),
    el('p', { className: 'muted' }, msg("ui.the_selection_applies_to_the_overview_and_transactions")),
    presets,
    accountList,
    el('footer', {}, [
      el('button', { type: 'button', onclick: () => node.close() }, msg("ui.cancel")),
      applyButton,
    ]),
  );
  node.addEventListener('close', () => node.remove());
  append(document.body, node);
  node.showModal();
}

function fillDatalists() {
  const tags = clear(document.querySelector('#tag-options'));
  for (const tag of state.metadata.tags) {
    append(tags, el('option', { value: tag.name }));
  }
  const merchants = clear(document.querySelector('#merchant-options'));
  for (const merchant of state.metadata.merchants) {
    append(merchants, el('option', { value: merchant.canonical_name }));
  }
}

function restoreAccountSelection() {
  const valid = new Set(state.metadata.accounts.filter((item) => item.enabled).map((item) => item.id));
  state.accountIds = state.accountIds.filter((id) => valid.has(id));
  syncSingleAccount();
}

function setActiveTab() {
  for (const button of document.querySelectorAll('[data-tab]')) {
    button.classList.toggle('active', button.dataset.tab === state.tab);
  }
}

async function refreshMetadata() {
  await loadMetadata();
  restoreAccountSelection();
  await loadOptions();
  updateScopeButton();
  fillDatalists();
}

async function refreshPage() {
  const button = document.querySelector('#refresh');
  button.disabled = true;
  try {
    await refreshMetadata();
    setActiveTab();
    await render();
    notify(msg("ui.view_refreshed"));
  } catch (error) {
    notify(error.message, 'error');
  } finally {
    button.disabled = false;
  }
}

document.querySelector('#account-scope').addEventListener('click', showScopeDialog);
document.querySelector('#refresh').addEventListener('click', refreshPage);

for (const button of document.querySelectorAll('[data-tab]')) {
  button.addEventListener('click', async () => {
    state.tab = button.dataset.tab;
    setActiveTab();
    await render();
  });
}

window.addEventListener('dagoberd:navigate', async (event) => {
  const detail = event.detail || {};
  if (detail.accountIds) {
    state.accountIds = [...new Set(detail.accountIds)];
    syncSingleAccount();
    writePreference(SCOPE_STORAGE_KEY, JSON.stringify(state.accountIds));
    updateScopeButton();
    await loadOptions();
  }
  if (detail.filters) {
    state.transactionFilters = detail.filters;
  }
  state.tab = detail.tab in TABS ? detail.tab : state.tab;
  setActiveTab();
  await render();
});

for (const node of document.querySelectorAll('[data-i18n]')) {
  bind(node, 'textContent', msg(node.dataset.i18n));
}
for (const attribute of ['title', 'aria-label']) {
  for (const node of document.querySelectorAll(`[data-i18n-${attribute}]`)) {
    bind(node, attribute, msg(node.getAttribute(`data-i18n-${attribute}`)));
  }
}
const languagePicker = document.querySelector('#language');
for (const [code, label] of Object.entries(languages)) {
  languagePicker.append(el('option', { value: code }, label));
}
languagePicker.value = getLanguage();
bind(languagePicker, 'aria-label', msg('language.label'));
languagePicker.addEventListener('change', () => setLanguage(languagePicker.value));

restoreView();
for (const button of document.querySelectorAll('[data-tab], #account-scope, #refresh')) button.disabled = true;
try {
  if (await initializeWorkspaces(app)) {
    await refreshMetadata();
    for (const button of document.querySelectorAll('[data-tab], #account-scope, #refresh')) button.disabled = false;
    setActiveTab();
    await render();
  }
} catch (error) {
  notify(msg("ui.reopen_the_gui_using_the_link_printed_by", { details: error.message }), 'error');
}
