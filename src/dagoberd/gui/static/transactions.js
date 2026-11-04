import { builtinLabel, statusLabel, msg, joinMessages, bind, number, date } from './i18n.js';
import { append, clear, dialog, el, formatAmount, notify } from './dom.js';
import { explainDialog } from './classification.js';
import { assignDocumentDialog } from './documents.js';
import { api, isManualAccount, state } from './store.js';
import { renderBulkInspector } from './transaction-bulk.js';
import {
  allColumns, columnLabels, dimensionColumns, loadColumnPreferences, saveColumnPreferences,
  visibleColumns,
} from './transaction-columns.js';
import { renderDetail } from './transaction-detail.js';
import { mutate } from './transaction-editors.js';

const view = {
  limit: 200,
  items: [],
  total: 0,
  offset: 0,
  loading: false,
  filters: {},
  sortBy: 'date',
  sortDirection: 'desc',
  selectedId: null,
  checked: new Set(),
  lastCheckedIndex: null,
  observer: null,
  closeBulkInspector: null,
};
let preferences = loadColumnPreferences();

window.addEventListener('resize', syncTransactionPageHeight);

export async function renderTransactions(root) {
  if (view.closeBulkInspector && !view.closeBulkInspector()) return;
  view.closeBulkInspector = null;
  if (state.transactionFilters) {
    view.filters = { ...state.transactionFilters };
    state.transactionFilters = null;
  }
  view.observer?.disconnect();
  view.checked.clear();
  view.selectedId = null;
  clear(root);
  const page = el('section', { className: `transaction-page density-${preferences.density}` }, [
    toolbar(),
    activeFilters(),
    selectionBar(),
    el('div', { className: 'transaction-workspace compact-workspace' }, [
      el('section', { id: 'transaction-result', className: 'transaction-results compact-results' }, [
        el('div', { id: 'transaction-summary', className: 'summary' }, msg("ui.loading_transactions")),
        el('div', { id: 'transaction-table-wrap', className: 'table-wrap' }, [
          tableShell(),
          el('div', { id: 'transaction-sentinel', className: 'load-sentinel', ariaLive: 'polite' }),
        ]),
      ]),
      detailPanel(),
    ]),
  ]);
  append(root, page);
  requestAnimationFrame(syncTransactionPageHeight);
  await load({ reset: true });
}

function syncTransactionPageHeight() {
  const page = document.querySelector('.transaction-page');
  if (!page) return;
  const main = page.closest('main');
  const padding = main ? Number.parseFloat(getComputedStyle(main).paddingBottom) : 16;
  const available = window.innerHeight - page.getBoundingClientRect().top - padding;
  page.style.setProperty('--transaction-page-height', `${Math.max(0, Math.floor(available))}px`);
}

function toolbar() {
  const search = el('input', {
    id: 'f-text',
    type: 'search',
    placeholder: msg("ui.search_transactions"),
    value: view.filters.text || '',
    ariaLabel: msg("transactions.search_label"),
  });
  const from = el('input', {
    id: 'f-from', type: 'date', value: view.filters.booking_from || '', title: msg("ui.from"), ariaLabel: msg("ui.transactions_from"),
  });
  const to = el('input', {
    id: 'f-to', type: 'date', value: view.filters.booking_to || '', title: msg("ui.to"), ariaLabel: msg("ui.transactions_to"),
  });
  const form = el('form', {
    className: 'transaction-toolbar',
    onsubmit: (event) => {
      event.preventDefault();
      collectFilters();
      renderTransactions(document.querySelector('#app'));
    },
  }, [
    el('div', { className: 'transaction-title' }, [el('h1', {}, msg("ui.transactions"))]),
    search,
    from,
    to,
    advancedFilters(),
    el('button', { type: 'submit', className: 'primary' }, msg("filters.apply")),
    el('button', { type: 'button', className: 'icon-button', title: msg("ui.columns"), ariaLabel: msg("ui.configure_columns"), onclick: showColumnDialog }, '☷'),
    el('button', {
      type: 'button', className: 'icon-button', title: msg("ui.reset_filters"), ariaLabel: msg("ui.reset_filters"),
      onclick: () => {
        view.filters = {};
        renderTransactions(document.querySelector('#app'));
      },
    }, '×'),
  ]);
  window.setTimeout(() => search.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && search.value) {
      search.value = '';
      collectFilters();
      renderTransactions(document.querySelector('#app'));
    }
  }));
  return form;
}

function advancedFilters() {
  const category = simpleSelect('f-category', categoryEntries(), view.filters.category_code || '');
  const tag = simpleSelect(
    'f-tag', state.options.tag.map((value) => [value, value]), view.filters.tag || ''
  );
  const status = simpleSelect(
    'f-status', state.metadata.booking_statuses.map((value) => [value, statusLabel(value)]), view.filters.booking_status || ''
  );
  const documentStatus = simpleSelect('f-document', [['matched', statusLabel('matched')], ['unmatched', statusLabel('open')]], view.filters.document_status || '');
  return el('details', { className: 'filter-popover' }, [
    el('summary', {}, msg("ui.more_filters")),
    el('div', { className: 'filter-popover-content' }, [
      labelled(msg("ui.minimum_amount"), el('input', { id: 'f-amount-min', type: 'number', step: '0.01', value: view.filters.amount_min || '' })),
      labelled(msg("ui.maximum_amount"), el('input', { id: 'f-amount-max', type: 'number', step: '0.01', value: view.filters.amount_max || '' })),
      labelled(msg("ui.merchant"), el('input', { id: 'f-merchant', value: view.filters.merchant || '' })),
      labelled(msg("ui.category"), category),
      labelled(msg("ui.tag"), tag),
      labelled(msg("ui.status"), status),
      labelled(msg("ui.document"), documentStatus),
      el('label', { className: 'checkbox-label' }, [
        el('input', { id: 'f-unreviewed', type: 'checkbox', checked: Boolean(view.filters.only_unreviewed) }),
        msg("ui.unreviewed_only"),
      ]),
    ]),
  ]);
}

function activeFilters() {
  const labels = {
    text: msg("ui.search"), booking_from: msg("ui.from"), booking_to: msg("ui.to"), amount_min: msg("ui.minimum_amount"), amount_max: msg("ui.maximum_amount"),
    merchant: msg("ui.merchant"), category_code: msg("ui.category"), tag: msg("ui.tag"), booking_status: msg("ui.status"),
    document_status: msg("ui.document"), only_unreviewed: msg("ui.unreviewed"),
  };
  const node = el('div', { id: 'active-filters', className: 'active-filters' });
  for (const [key, value] of Object.entries(view.filters)) {
    if (!value) continue;
    append(node, el('button', {
      type: 'button',
      className: 'filter-chip',
      title: msg("filters.remove", { label: labels[key] || key }),
      onclick: () => {
        delete view.filters[key];
        renderTransactions(document.querySelector('#app'));
      },
    }, msg("transactions.filter_chip", { label: labels[key] || key, value: value === true ? msg("ui.yes") : value })));
  }
  node.hidden = !node.childElementCount;
  return node;
}

function selectionBar() {
  return el('div', { id: 'selection-bar', className: 'selection-bar', hidden: true }, [
    el('strong', { id: 'selection-count' }, msg("ui.0_selected")),
    el('button', { type: 'button', className: 'primary', onclick: showBulkInspector }, msg("ui.edit_selected")),
    el('button', { type: 'button', className: 'icon-button', title: msg("ui.clear_selection"), onclick: clearSelection }, '×'),
  ]);
}

function tableShell() {
  const columns = visibleColumns(state, preferences);
  const head = el('tr', {}, [selectionHeader(), ...columns.map(headerCell)]);
  return el('table', { className: 'transactions compact-grid', ariaLabel: msg("ui.transactions") }, [
    el('thead', {}, head), el('tbody', { id: 'transaction-body' }),
  ]);
}

function selectionHeader() {
  return el('th', { className: 'selection-column' }, el('input', {
    type: 'checkbox',
    ariaLabel: msg("ui.select_loaded_transactions"),
    onchange: (event) => {
      view.items.forEach((item) => event.target.checked ? view.checked.add(item.id) : view.checked.delete(item.id));
      refreshSelection();
    },
  }));
}

function headerCell(column) {
  const labels = columnLabels(state);
  const sortable = sortKey(column);
  const content = sortable ? el('button', {
    type: 'button',
    className: 'sort-button',
    onclick: () => changeSort(sortable),
    ariaLabel: msg("ui.sort_by", { label: labels[column] }),
  }, msg("transactions.sort_heading", { label: labels[column], direction: view.sortBy === sortable ? view.sortDirection === 'asc' ? ' ↑' : ' ↓' : '' })) : labels[column];
  const cell = el('th', {
    dataset: { column },
    style: preferences.widths[column] ? `width:${preferences.widths[column]}px` : '',
  }, [content, el('span', { className: 'column-resizer', onpointerdown: (event) => resizeColumn(event, column) })]);
  return cell;
}

function sortKey(column) {
  return { date: 'date', account: 'account', booking: 'counterparty', category: 'category', amount: 'amount' }[column] || '';
}

async function changeSort(sortBy) {
  if (view.sortBy === sortBy) view.sortDirection = view.sortDirection === 'asc' ? 'desc' : 'asc';
  else {
    view.sortBy = sortBy;
    view.sortDirection = sortBy === 'date' ? 'desc' : 'asc';
  }
  await renderTransactions(document.querySelector('#app'));
}

function collectFilters() {
  const value = (id) => document.querySelector(`#${id}`)?.value.trim() || '';
  const filters = {};
  for (const [id, key] of [
    ['f-text', 'text'], ['f-from', 'booking_from'], ['f-to', 'booking_to'],
    ['f-amount-min', 'amount_min'], ['f-amount-max', 'amount_max'], ['f-merchant', 'merchant'],
    ['f-category', 'category_code'], ['f-tag', 'tag'], ['f-status', 'booking_status'],
    ['f-document', 'document_status'],
  ]) if (value(id)) filters[key] = value(id);
  if (document.querySelector('#f-unreviewed')?.checked) filters.only_unreviewed = true;
  view.filters = filters;
}

async function load({ reset = false } = {}) {
  if (view.loading) return;
  if (!reset && view.items.length >= view.total) return;
  view.loading = true;
  if (reset) {
    view.items = [];
    view.offset = 0;
    view.total = 0;
    clear(document.querySelector('#transaction-body'));
    view.checked.clear();
    refreshSelection();
  }
  const parameters = requestParameters();
  const sentinel = document.querySelector('#transaction-sentinel');
  bind(sentinel, 'textContent', msg("ui.loading"));
  try {
    const result = await api(`/transactions?${parameters}`);
    view.total = result.total;
    const body = document.querySelector('#transaction-body');
    result.items.forEach((item) => {
      view.items.push(item);
      append(body, transactionRow(item, view.items.length - 1));
    });
    view.offset = view.items.length;
    bind(document.querySelector('#transaction-summary'), 'textContent', msg("ui.of_transactions", { count: view.items.length, total: view.total }, view.total));
    bind(sentinel, 'textContent', view.items.length < view.total ? msg("ui.more_transactions_load_as_you_scroll") : msg("ui.all_transactions_loaded"));
    observeSentinel();
  } catch (error) {
    bind(sentinel, 'textContent', msg("ui.more_transactions_could_not_be_loaded"));
    notify(error.message, 'error');
  } finally {
    view.loading = false;
  }
}

function requestParameters() {
  const parameters = new URLSearchParams({
    limit: String(view.limit), offset: String(view.offset), sort_by: view.sortBy, sort_direction: view.sortDirection,
  });
  state.accountIds.forEach((id) => parameters.append('account_id', id));
  for (const [key, value] of Object.entries(view.filters)) {
    if (!value) continue;
    parameters.append(key, key === 'only_unreviewed' ? 'true' : value);
  }
  return parameters;
}

function observeSentinel() {
  view.observer?.disconnect();
  const sentinel = document.querySelector('#transaction-sentinel');
  const root = document.querySelector('#transaction-table-wrap');
  if (!sentinel || !root || view.items.length >= view.total) return;
  view.observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) load();
  }, { root, rootMargin: '180px' });
  view.observer.observe(sentinel);
}

function transactionRow(item, index) {
  const node = el('tr', {
    className: item.id === view.selectedId ? 'selected' : '',
    dataset: { id: item.id, version: item.version, index },
    ariaSelected: item.id === view.selectedId ? 'true' : 'false',
    onclick: (event) => {
      if (!event.target.closest('button,input,select')) selectRow(item.id);
    },
    ondblclick: (event) => {
      if (!event.target.closest('button,input,select')) showDetail(item.id);
    },
    oncontextmenu: (event) => showRowMenu(item, event),
  });
  append(node, selectionCell(item, index));
  for (const column of visibleColumns(state, preferences)) append(node, dataCell(item, column));
  return node;
}

function selectionCell(item, index) {
  return el('td', { className: 'selection-column' }, el('input', {
    type: 'checkbox',
    checked: view.checked.has(item.id),
    ariaLabel: msg("ui.select", { purpose: item.purpose }),
    onclick: (event) => {
      event.stopPropagation();
      if (event.shiftKey && view.lastCheckedIndex !== null) {
        const [start, end] = [view.lastCheckedIndex, index].sort((a, b) => a - b);
        view.items.slice(start, end + 1).forEach((row) => event.target.checked ? view.checked.add(row.id) : view.checked.delete(row.id));
      } else if (event.target.checked) view.checked.add(item.id);
      else view.checked.delete(item.id);
      view.lastCheckedIndex = index;
      refreshSelection();
    },
  }));
}

function dataCell(item, column) {
  const cell = el('td', { dataset: { column }, tabIndex: 0 });
  renderCell(cell, item, column);
  const editable = isEditable(column, item);
  cell.addEventListener('click', (event) => {
    if (editable && !event.target.closest('input,select,button')) startEditing(cell, item, column);
  });
  cell.addEventListener('keydown', (event) => {
    if ((event.key === 'F2' || event.key === 'Enter') && editable && !cell.querySelector('input,select')) {
      event.preventDefault();
      startEditing(cell, item, column);
    } else if (event.key === 'Enter' && !editable) {
      event.preventDefault();
      showDetail(item.id);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      focusAdjacent(cell, event.key === 'ArrowDown' ? 1 : -1);
    }
  });
  return cell;
}

function renderCell(cell, item, column) {
  clear(cell);
  cell.className = '';
  if (column === 'date') append(cell, date(item.booking_date));
  else if (column === 'account') append(cell, item.account_name);
  else if (column === 'booking') {
    cell.className = 'booking-cell';
    append(cell, el('strong', { title: item.purpose }, item.purpose), el('small', {}, item.counterparty || item.reference || ''));
  } else if (column === 'category') append(cell, builtinLabel(item.category_code, item.category || item.category_code || '—'));
  else if (column === 'amount') {
    cell.className = `amount ${item.amount_minor < 0 ? 'negative' : 'positive'}`;
    append(cell, formatAmount(item.amount_minor, item.currency));
  } else if (column === 'status') append(cell, statusIndicators(item));
  else if (column === 'tags') append(cell, item.tags.length ? item.tags.join(', ') : '—');
  else if (column === 'merchant') append(cell, item.merchant || '—');
  else if (column === 'note') append(cell, item.note || '—');
  else if (column === 'booking_status') append(cell, statusLabel(item.booking_status));
  else if (column.startsWith('dimension:')) append(cell, item.dimensions[column.split(':')[1]] || '—');
}

function statusIndicators(item) {
  return el('div', { className: 'status-indicators' }, [
    el('button', {
      type: 'button', className: item.document_status === 'matched' ? 'status-icon matched' : 'status-icon',
      title: item.document_status === 'matched' ? msg("ui.document_matched") : msg("ui.match_document"),
      ariaLabel: item.document_status === 'matched' ? msg("ui.document_matched") : msg("ui.match_document"),
      onclick: () => assignDocumentDialog(item, () => reloadSingle(item.id)),
    }, '▧'),
    item.has_open_review ? el('span', { className: 'status-icon warning', title: msg("ui.open_review_item"), ariaLabel: msg("ui.open_review_item") }, '!') : null,
    item.booking_status !== 'booked' ? el('span', { className: 'status-icon pending', title: statusLabel(item.booking_status) }, '●') : null,
    el('button', { type: 'button', className: 'status-icon more', title: msg("ui.details"), ariaLabel: msg("ui.open_details"), onclick: () => showDetail(item.id) }, '⋯'),
  ]);
}

function isEditable(column, item) {
  if (column === 'category' || column === 'tags' || column === 'merchant' || column === 'note') return true;
  if (column === 'booking_status') return (state.metadata.status_transitions[item.booking_status] || []).length > 0;
  return column.startsWith('dimension:');
}

function startEditing(cell, item, column) {
  if (cell.querySelector('input,select')) return;
  if (column === 'category') editCategory(cell, item);
  else if (column === 'tags') editTags(cell, item);
  else if (column === 'merchant') editText(cell, item, column, item.merchant || '');
  else if (column === 'note') editText(cell, item, column, item.note || '');
  else if (column === 'booking_status') editStatus(cell, item);
  else if (column.startsWith('dimension:')) editDimension(cell, item, column.split(':')[1]);
}

function editCategory(cell, item) {
  const control = el('select', { className: 'cell-editor' });
  append(control, el('option', { value: '' }, '—'));
  for (const [profile, categories] of Object.entries(state.options.category)) {
    const group = el('optgroup', { label: profile });
    for (const category of categories) append(group, el('option', {
      value: `${profile}:${category.code}`,
      selected: item.classification_profile === profile && item.category_code === category.code,
    }, builtinLabel(category.code, category.name)));
    append(control, group);
  }
  activateControl(cell, control, async (value) => {
    if (!value) return false;
    const [profile_code, category_code] = value.split(':');
    const result = await saveMutation(cell, item, {
      path: `/transactions/${item.id}/category`, body: { profile_code, category_code },
    });
    if (result) {
      item.classification_profile = profile_code;
      item.category_code = category_code;
      item.category = state.options.category[profile_code].find(category => category.code === category_code)?.name || category_code;
    }
    return Boolean(result);
  });
}

function editText(cell, item, column, current) {
  const control = el('input', {
    className: 'cell-editor', value: current, maxLength: column === 'note' ? 2000 : 250,
    list: column === 'merchant' ? 'merchant-options' : undefined,
  });
  activateControl(cell, control, async (value) => {
    const normalized = value.trim();
    if (normalized === current) return true;
    if (column === 'merchant' && normalized && !state.metadata.merchants.some((entry) => entry.canonical_name === normalized)) {
      await api('/merchants', { method: 'POST', body: JSON.stringify({ name: normalized }) });
    }
    const result = await saveMutation(cell, item, {
      path: `/transactions/${item.id}/${column}`,
      body: { [column]: normalized },
    });
    if (result) item[column] = normalized;
    return Boolean(result);
  });
}

function editTags(cell, item) {
  const current = [...item.tags].sort();
  const control = el('input', { className: 'cell-editor', value: current.join(', '), placeholder: msg("ui.comma_separated_tags") });
  activateControl(cell, control, async (value) => {
    const requested = [...new Set(value.split(',').map((entry) => entry.trim()).filter(Boolean))].sort();
    for (const tag of requested.filter((tag) => !state.metadata.tags.some((entry) => entry.name.toLowerCase() === tag.toLowerCase()))) {
      await api('/tags', { method: 'POST', body: JSON.stringify({ name: tag }) });
    }
    for (const tag of current.filter((tag) => !requested.includes(tag))) {
      if (!await saveMutation(cell, item, { path: `/transactions/${item.id}/tags`, body: { tag, remove: true } })) return false;
    }
    for (const tag of requested.filter((tag) => !current.includes(tag))) {
      if (!await saveMutation(cell, item, { path: `/transactions/${item.id}/tags`, body: { tag, remove: false } })) return false;
    }
    item.tags = requested;
    return true;
  });
}

function editDimension(cell, item, code) {
  const control = simpleSelect('', (state.options.dimension_value[code] || []).map((entry) => [entry.code, entry.name]), item.dimensions[code] || '');
  control.className = 'cell-editor';
  activateControl(cell, control, async (value) => {
    const result = await saveMutation(cell, item, {
      path: `/transactions/${item.id}/dimensions`, body: { dimension_code: code, value_code: value || null },
    });
    if (result) {
      if (value) item.dimensions[code] = value;
      else delete item.dimensions[code];
    }
    return Boolean(result);
  });
}

function editStatus(cell, item) {
  const targets = state.metadata.status_transitions[item.booking_status] || [];
  const control = simpleSelect('', targets.map((status) => [status, statusLabel(status)]), '');
  control.className = 'cell-editor';
  activateControl(cell, control, async (value) => {
    if (!value) return false;
    const result = await saveMutation(cell, item, {
      path: `/transactions/${item.id}/status`, body: { status: value },
    });
    if (result) item.booking_status = value;
    return Boolean(result);
  });
}

function activateControl(cell, control, save) {
  const item = view.items.find((entry) => entry.id === cell.closest('tr').dataset.id);
  const column = cell.dataset.column;
  append(clear(cell), control);
  let finished = false;
  const finish = async (commit) => {
    if (finished) return;
    finished = true;
    if (commit) await save(control.value);
    renderCell(cell, item, column);
    cell.focus();
  };
  control.addEventListener('keydown', async (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      finish(true);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      finish(false);
    } else if (event.key === 'Tab') {
      event.preventDefault();
      await finish(true);
      focusEditable(cell, event.shiftKey ? -1 : 1);
    }
  });
  control.addEventListener('blur', () => finish(true));
  if (control.tagName === 'SELECT') control.addEventListener('change', () => finish(true));
  control.addEventListener('click', (event) => event.stopPropagation());
  control.focus();
  if (control.select) control.select();
}

async function saveMutation(cell, item, request) {
  const row = cell.closest('tr');
  const result = await mutate(row, request, () => reloadSingle(item.id));
  if (result?.version) {
    item.version = result.version;
    row.dataset.version = result.version;
  }
  return result;
}

async function reloadSingle(transactionId) {
  const current = await api(`/transactions/${transactionId}/row`);
  const index = view.items.findIndex((item) => item.id === transactionId);
  if (index < 0) return;
  view.items[index] = current;
  const existing = document.querySelector(`tr[data-id="${CSS.escape(transactionId)}"]`);
  existing?.replaceWith(transactionRow(current, index));
  if (view.selectedId === transactionId) await showDetail(transactionId, { keepSelection: true });
}

function selectRow(transactionId) {
  view.selectedId = transactionId;
  for (const row of document.querySelectorAll('table.transactions tbody tr')) {
    const selected = row.dataset.id === transactionId;
    row.classList.toggle('selected', selected);
    row.setAttribute('aria-selected', String(selected));
  }
}

function focusAdjacent(cell, offset) {
  const row = cell.closest('tr');
  const next = row.parentElement.children[Number(row.dataset.index) + offset];
  const target = next?.querySelector(`td[data-column="${CSS.escape(cell.dataset.column)}"]`);
  if (!target) return;
  selectRow(next.dataset.id);
  target.focus();
}

function focusEditable(cell, offset) {
  const cells = [...document.querySelectorAll('#transaction-body td[data-column]')].filter((candidate) => {
    const item = view.items.find((entry) => entry.id === candidate.closest('tr').dataset.id);
    return item && isEditable(candidate.dataset.column, item);
  });
  const index = cells.indexOf(cell);
  const target = cells[index + offset];
  if (!target) return;
  const item = view.items.find((entry) => entry.id === target.closest('tr').dataset.id);
  selectRow(item.id);
  target.focus();
  startEditing(target, item, target.dataset.column);
}

function showRowMenu(item, event) {
  event.preventDefault();
  document.querySelector('.row-context-menu')?.remove();
  const menu = el('div', {
    className: 'row-context-menu',
    style: `left:${Math.min(event.clientX, window.innerWidth - 210)}px;top:${Math.min(event.clientY, window.innerHeight - 160)}px`,
  }, [
    el('button', { type: 'button', onclick: () => { menu.remove(); showDetail(item.id); } }, msg("ui.open_details")),
    el('button', { type: 'button', onclick: () => { menu.remove(); explainDialog(item.id); } }, msg("ui.explain_classification")),
    el('button', { type: 'button', onclick: () => { menu.remove(); assignDocumentDialog(item, () => reloadSingle(item.id)); } }, msg("ui.match_document")),
  ]);
  const close = (closeEvent) => {
    if (!menu.contains(closeEvent.target)) menu.remove();
  };
  menu.addEventListener('keydown', (keyEvent) => {
    if (keyEvent.key === 'Escape') menu.remove();
  });
  append(document.body, menu);
  menu.querySelector('button').focus();
  window.setTimeout(() => document.addEventListener('pointerdown', close, { once: true }));
}

function detailPanel() {
  return el('aside', { id: 'detail', className: 'detail-panel compact-inspector', hidden: true, ariaLive: 'polite' });
}

async function showDetail(transactionId, { keepSelection = false } = {}) {
  if (view.closeBulkInspector && !view.closeBulkInspector()) return;
  view.closeBulkInspector = null;
  if (!keepSelection) selectRow(transactionId);
  try {
    const detail = await api(`/transactions/${transactionId}`);
    const panel = document.querySelector('#detail');
    panel.hidden = false;
    renderDetail(panel, detail, {
      manual: isManualAccount(detail.account_id),
      close: () => {
        panel.hidden = true;
        view.selectedId = null;
        selectRow('');
      },
      reload: async () => {
        await reloadSingle(transactionId);
        await showDetail(transactionId, { keepSelection: true });
      },
    });
    panel.querySelector('.detail > header')?.append(el('button', {
      type: 'button', title: msg("ui.explain_classification"), onclick: () => explainDialog(transactionId),
    }, msg("ui.explain")));
  } catch (error) {
    notify(error.message, 'error');
  }
}

function refreshSelection() {
  const bar = document.querySelector('#selection-bar');
  if (!bar) return;
  bar.hidden = !view.checked.size;
  bind(document.querySelector('#selection-count'), 'textContent', msg("ui.selected", { count: view.checked.size }));
  for (const row of document.querySelectorAll('#transaction-body tr')) {
    const checked = view.checked.has(row.dataset.id);
    row.classList.toggle('checked', checked);
    const box = row.querySelector('.selection-column input');
    if (box) box.checked = checked;
  }
}

function clearSelection() {
  if (view.closeBulkInspector && !view.closeBulkInspector()) return;
  view.closeBulkInspector = null;
  view.checked.clear();
  view.lastCheckedIndex = null;
  refreshSelection();
}

async function showBulkInspector() {
  if (!view.checked.size) return;
  if (view.checked.size > 500) {
    notify(msg("ui.the_bulk_inspector_supports_at_most_500_transactions"), 'warning');
    return;
  }
  const panel = document.querySelector('#detail');
  await renderBulkInspector(panel, [...view.checked], {
    registerClose: (close) => { view.closeBulkInspector = close; },
    close: () => {
      panel.hidden = true;
      view.closeBulkInspector = null;
    },
    applied: async (rows) => {
      for (const row of rows) replaceLoadedRow(row);
      view.checked.clear();
      view.lastCheckedIndex = null;
      refreshSelection();
    },
    conflict: async (transactionIds) => {
      for (const transactionId of transactionIds) await reloadSingle(transactionId);
    },
  });
}

function replaceLoadedRow(current) {
  const index = view.items.findIndex((item) => item.id === current.id);
  if (index < 0) return;
  view.items[index] = current;
  const existing = document.querySelector(`tr[data-id="${CSS.escape(current.id)}"]`);
  existing?.replaceWith(transactionRow(current, index));
}

function showColumnDialog() {
  const available = allColumns(state);
  const order = preferences.order.filter((column) => available.includes(column));
  for (const column of available) if (!order.includes(column)) order.push(column);
  const chosen = new Set(preferences.visible);
  const list = el('div', { className: 'column-list' });
  const render = () => {
    clear(list);
    order.forEach((column, index) => append(list, el('div', { className: 'column-option' }, [
      el('label', {}, [el('input', {
        type: 'checkbox', checked: chosen.has(column),
        disabled: ['date', 'booking', 'amount'].includes(column),
        onchange: (event) => event.target.checked ? chosen.add(column) : chosen.delete(column),
      }), columnLabels(state)[column]]),
      el('button', { type: 'button', className: 'icon-button', disabled: index === 0, onclick: () => {
        [order[index - 1], order[index]] = [order[index], order[index - 1]]; render();
      } }, '↑'),
      el('button', { type: 'button', className: 'icon-button', disabled: index === order.length - 1, onclick: () => {
        [order[index + 1], order[index]] = [order[index], order[index + 1]]; render();
      } }, '↓'),
    ])));
  };
  render();
  const density = simpleSelect('', [['compact', msg("ui.compact")], ['comfortable', msg("ui.comfortable")]], preferences.density);
  const content = el('div', {}, [list, labelled(msg("ui.row_height"), density)]);
  const node = dialog(msg("ui.table_columns"), content, [el('button', {
    type: 'button', className: 'primary', onclick: async () => {
      preferences = { ...preferences, visible: [...chosen], order, density: density.value };
      saveColumnPreferences(preferences);
      node.close();
      await renderTransactions(document.querySelector('#app'));
    },
  }, msg("ui.save"))]);
}

function resizeColumn(event, column) {
  event.preventDefault();
  const header = event.target.closest('th');
  const startX = event.clientX;
  const startWidth = header.getBoundingClientRect().width;
  const move = (moveEvent) => {
    const width = Math.max(70, Math.round(startWidth + moveEvent.clientX - startX));
    header.style.width = `${width}px`;
    preferences.widths[column] = width;
  };
  const stop = () => {
    window.removeEventListener('pointermove', move);
    saveColumnPreferences(preferences);
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', stop, { once: true });
}

function categoryEntries() {
  return Object.entries(state.options.category).flatMap(([profile, categories]) =>
    categories.map((category) => [`${category.code}`, joinMessages([profile, builtinLabel(category.code, category.name)], ' · ')])
  );
}

function simpleSelect(id, pairs, current) {
  const node = el('select', { id });
  append(node, el('option', { value: '' }, msg('filter.all')));
  for (const [value, label] of pairs) append(node, el('option', { value, selected: value === current }, label));
  node.value = current || '';
  return node;
}

function labelled(text, control) {
  return el('label', {}, [el('span', {}, text), control]);
}

export function reloadTransactions() {
  return load({ reset: true });
}
