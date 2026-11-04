function preferenceKey(key) {
  const workspace = new URLSearchParams(location.search).get('workspace');
  return workspace ? `${key}.workspace.${workspace}` : key;
}

export function readPreference(key, fallback = null) {
  try { return localStorage.getItem(preferenceKey(key)) ?? fallback; } catch { return fallback; }
}

export function writePreference(key, value) {
  try { localStorage.setItem(preferenceKey(key), value); } catch { /* Storage restrictions must not interrupt editing. */ }
}

export function readSession(key, fallback = '') {
  try { return sessionStorage.getItem(key) ?? fallback; } catch { return fallback; }
}

export function writeSession(key, value) {
  try { sessionStorage.setItem(key, value); } catch { /* The fragment token remains usable in memory. */ }
}
