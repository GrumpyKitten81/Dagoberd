import { readSession, writeSession } from './browser-storage.js';
import { errorMessage, msg } from './i18n.js';
const TOKEN_STORAGE_KEY = 'dagoberd.gui-token';
const fragmentToken = new URLSearchParams(location.hash.slice(1)).get('t') || '';
if (fragmentToken) {
  writeSession(TOKEN_STORAGE_KEY, fragmentToken);
}
const token = fragmentToken || readSession(TOKEN_STORAGE_KEY) || '';
history.replaceState(null, '', location.pathname + location.search);

export const state = {
  workspaceId: new URLSearchParams(location.search).get('workspace') || '',
  metadata: null,
  metadataRevision: 0,
  metadataLoadedRevision: 0,
  options: null,
  accountId: '',
  accountIds: [],
  tab: 'overview',
  transactionFilters: null,
};

export class ApiError extends Error {
  constructor(kind, { status = null, code = null, details = null, message = null } = {}) {
    super();
    this.name = 'ApiError';
    this.kind = kind;
    this.status = status;
    this.code = code;
    this.details = details;
    this.message = message || msg(`error.${kind}`, { status });
  }
}

async function request(path, options) {
  const prefix = options.management ? '' : state.workspaceId ? `/workspaces/${encodeURIComponent(state.workspaceId)}` : '';
  try {
    return await fetch(`/api${prefix}${path}`, options);
  } catch {
    throw new ApiError('network');
  }
}

async function readPayload(response) {
  try {
    return await response.json();
  } catch {
    throw new ApiError(response.ok ? 'invalid_response' : 'http', { status: response.status });
  }
}

function responseError(response, payload) {
  const error = payload?.error;
  const code = typeof error?.code === 'string' ? error.code : null;
  return new ApiError('http', {
    status: response.status,
    code,
    details: error?.details,
    message: code ? errorMessage(code, typeof error.message === 'string' ? error.message : '') : null,
  });
}

export async function api(path, options = {}) {
  const headers = { 'X-Dagoberd-Token': token, ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
  }
  const response = await request(path, { ...options, headers });
  const payload = await readPayload(response);
  if (!response.ok || payload?.ok === false) throw responseError(response, payload);
  if (payload?.ok !== true || !Object.hasOwn(payload, 'data')) {
    throw new ApiError('invalid_response', { status: response.status });
  }
  return payload.data;
}

export async function apiUpload(path, file) {
  const body = new FormData();
  body.set('file', file);
  return api(path, { method: 'POST', body });
}

export async function apiBlob(path) {
  const response = await request(path, { headers: { 'X-Dagoberd-Token': token } });
  if (!response.ok) {
    throw responseError(response, await readPayload(response));
  }
  try {
    return await response.blob();
  } catch {
    throw new ApiError('network', { status: response.status });
  }
}

export async function loadMetadata() {
  state.metadata = await api('/meta');
  return state.metadata;
}

export async function loadOptions(isCurrent = () => true) {
  const accountId = state.accountId;
  const options = state.accountIds.length === 1
    ? await api(`/accounts/${state.accountId}/options`)
    : globalOptions();
  if (isCurrent() && state.accountId === accountId) state.options = options;
  return options;
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
