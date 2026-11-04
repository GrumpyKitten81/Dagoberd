import { readPreference, writePreference } from './browser-storage.js';
import { builtinLabel, msg, date } from './i18n.js';
const STORAGE_KEY = 'account-eval.transaction-columns-v1';
const DEFAULT_COLUMNS = ['date', 'account', 'booking', 'category', 'amount', 'status'];

export function loadColumnPreferences() {
  try {
    const stored = JSON.parse(readPreference(STORAGE_KEY) || '{}');
    return {
      visible: Array.isArray(stored.visible) ? stored.visible : DEFAULT_COLUMNS,
      order: Array.isArray(stored.order) ? stored.order : DEFAULT_COLUMNS,
      widths: stored.widths && typeof stored.widths === 'object' ? stored.widths : {},
      density: stored.density === 'comfortable' ? 'comfortable' : 'compact',
    };
  } catch {
    return { visible: DEFAULT_COLUMNS, order: DEFAULT_COLUMNS, widths: {}, density: 'compact' };
  }
}

export function saveColumnPreferences(preferences) {
  writePreference(STORAGE_KEY, JSON.stringify(preferences));
}

export function dimensionColumns(guiState) {
  const byCode = new Map(guiState.metadata.dimensions.map((item) => [item.code, item]));
  const configured = guiState.options.dimensions || [];
  if (configured.length) return configured.map((item) => byCode.get(item.code)).filter(Boolean);
  return guiState.metadata.dimensions.filter(
    (item) => (guiState.options.dimension_value[item.code] || []).length,
  );
}

export function allColumns(guiState) {
  return [
    'date', 'account', 'booking', 'category', 'amount', 'status', 'tags', 'merchant', 'note',
    'booking_status', ...dimensionColumns(guiState).map((item) => `dimension:${item.code}`),
  ];
}

export function columnLabels(guiState) {
  return {
    date: msg("ui.date"), account: msg("ui.account"), booking: msg("ui.transaction"), category: msg("ui.category"), amount: msg("ui.amount"),
    status: msg("ui.hints"), tags: msg("ui.tags"), merchant: msg("ui.merchant"), note: msg("ui.note"), booking_status: msg("ui.status"),
    ...Object.fromEntries(
      dimensionColumns(guiState).map((item) => [`dimension:${item.code}`, builtinLabel(item.code, item.name)]),
    ),
  };
}

export function visibleColumns(guiState, preferences) {
  const available = new Set(allColumns(guiState));
  const ordered = preferences.order.filter((column) => available.has(column));
  for (const column of allColumns(guiState)) if (!ordered.includes(column)) ordered.push(column);
  return ordered
    .filter((column) => preferences.visible.includes(column))
    .filter((column) => column !== 'account' || guiState.accountIds.length !== 1);
}
