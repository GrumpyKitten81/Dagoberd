import { readSession, writeSession } from './browser-storage.js';
import { errorMessage, msg } from './i18n.js';
const TOKEN_STORAGE_KEY = 'account-eval.gui-token';
const fragmentToken = new URLSearchParams(location.hash.slice(1)).get('t') || '';
if (fragmentToken) {
  writeSession(TOKEN_STORAGE_KEY, fragmentToken);
}
const token = fragmentToken || readSession(TOKEN_STORAGE_KEY) || '';
history.replaceState(null, '', location.pathname + location.search);

export const state = {
  workspaceId: new URLSearchParams(location.search).get('workspace') || '',
  metadata: null,
  options: null,
  accountId: '',
  accountIds: [],
  tab: 'overview',
  transactionFilters: null,
};

export async function api(path, options = {}) {
  const headers = { 'X-AccountEval-Token': token, ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
  }
  const prefix = options.management ? '' : state.workspaceId ? `/workspaces/${encodeURIComponent(state.workspaceId)}` : '';
  const response = await fetch(`/api${prefix}${path}`, { ...options, headers });
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error?.message || response.statusText);
    error.message = errorMessage(payload.error?.code, payload.error?.message || response.statusText);
    error.code = payload.error?.code;
    error.status = response.status;
    error.details = payload.error?.details;
    throw error;
  }
  return payload.data;
}

export async function apiUpload(path, file) {
  const body = new FormData();
  body.set('file', file);
  return api(path, { method: 'POST', body });
}

export async function apiBlob(path) {
  const prefix = state.workspaceId ? `/workspaces/${encodeURIComponent(state.workspaceId)}` : '';
  const response = await fetch(`/api${prefix}${path}`, { headers: { 'X-AccountEval-Token': token } });
  if (!response.ok) {
    const error = new Error();
    error.message = msg('ui.file_could_not_be_loaded', {status: response.status});
    throw error;
  }
  return response.blob();
}

export async function loadMetadata() {
  state.metadata = await api('/meta');
  return state.metadata;
}

export async function loadOptions() {
  state.options = state.accountIds.length === 1
    ? await api(`/accounts/${state.accountId}/options`)
    : globalOptions();
  return state.options;
}

function globalOptions() {
  const dimensionValues = {};
  for (const dimension of state.metadata.dimensions) {
    if (dimension.values.length) {
      dimensionValues[dimension.code] = dimension.values.map((value) => ({
        code: value.code,
        name: value.name,
      }));
    }
  }
  return {
    category: state.metadata.profiles,
    tag: state.metadata.tags.map((tag) => tag.name),
    dimension_value: dimensionValues,
  };
}

export function account(accountId) {
  return state.metadata.accounts.find((item) => item.id === accountId) || null;
}

export function isManualAccount(accountId) {
  return account(accountId)?.provider_kind === 'manual';
}
