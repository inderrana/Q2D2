// R2D2 — main app (HIG / M3-aligned UI)
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  user: null,
  chats: [],
  currentChatId: null,
  currentModel: null,
  models: [],
  documents: [],
  settings: {},
  providers: [],
  activeProvider: 'tavily',
  // Active streams keyed by chatId (or a temp "new:<n>" id until meta arrives).
  // Each entry: { controller, full, thinking, citations, stats, model, done, error, tempId? }
  streams: new Map(),
  lastUserMessage: null,
  chatAttachments: [],
  refChatIds: new Set(),  // cross-chat references for the *next* message
  pendingSearchOverride: null,  // set by the "edit search query" affordance; cleared on next send
  modelCaps: new Map(),         // model name -> Set<string> of capabilities
};
// Backwards-compat shim: `state.streaming` should reflect "is the currently
// viewed chat streaming?" (used by composer disable / regen guard / etc.).
Object.defineProperty(state, 'streaming', {
  get() {
    const s = state.streams.get(state.currentChatId);
    return !!(s && !s.done);
  },
});

// ---------- API helper ----------
async function api(path, opts = {}) {
  const baseHeaders = { 'X-Requested-With': 'fetch' };
  const headers = opts.body && !(opts.body instanceof FormData)
    ? { 'Content-Type': 'application/json', ...baseHeaders, ...(opts.headers || {}) }
    : { ...baseHeaders, ...(opts.headers || {}) };
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401) { location.replace('/login'); throw new Error('unauthorized'); }
  if (r.status === 429) {
    const retry = r.headers.get('Retry-After') || '?';
    throw new Error(`Too many requests \u2014 retry in ${retry}s`);
  }
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    // FastAPI 422 returns `detail` as an array of validation-error objects;
    // a plain HTTPException returns it as a string. Normalise both shapes
    // into something readable instead of letting it stringify to
    // "[object Object]".
    let msg;
    if (typeof err.detail === 'string') {
      msg = err.detail;
    } else if (Array.isArray(err.detail) && err.detail.length) {
      msg = err.detail.map(d => {
        const loc = Array.isArray(d.loc) ? d.loc.filter(p => p !== 'body').join('.') : '';
        return loc ? `${loc}: ${d.msg}` : d.msg;
      }).filter(Boolean).join('; ') || `HTTP ${r.status}`;
    } else {
      msg = `HTTP ${r.status}`;
    }
    throw new Error(msg);
  }
  if (r.status === 204) return null;
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r.text();
}
const J = (body) => ({ method: 'POST', body: JSON.stringify(body) });

// ---------- Markdown ----------
marked.setOptions({
  breaks: true, gfm: true,
  highlight: (code, lang) => {
    try {
      if (lang && hljs.getLanguage(lang)) return hljs.highlight(code, { language: lang }).value;
      return hljs.highlightAuto(code).value;
    } catch { return code; }
  },
});
// Force `rel="noopener noreferrer"` on any link that opens in a new tab,
// so untrusted markdown (model output / RAG sources) can't tab-nap the opener
// or leak the referrer. Registered once at module load.
if (typeof DOMPurify !== 'undefined' && DOMPurify.addHook) {
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A' && node.getAttribute('target') === '_blank') {
      node.setAttribute('rel', 'noopener noreferrer');
    }
  });
}
function _wrapThinking(md) {
  // Reasoning models (deepseek-r1, qwq, qwen3-thinking, …) emit chain-of-
  // thought wrapped in <think>...</think>. Convert to a collapsible <details>
  // block BEFORE markdown/sanitization so the tag survives DOMPurify.
  // Handle the streaming case: an unclosed opening tag means we're still
  // inside the thought — close it provisionally so the panel renders live.
  if (!md) return md;
  let s = md;
  const opens = (s.match(/<think>/gi) || []).length;
  const closes = (s.match(/<\/think>/gi) || []).length;
  if (opens > closes) s += '</think>'.repeat(opens - closes);
  return s.replace(/<think>([\s\S]*?)<\/think>/gi,
    (_, inner) => `\n\n<details class="thinking"><summary>Thinking</summary>\n\n${inner.trim()}\n\n</details>\n\n`);
}
function renderMarkdown(md) {
  const html = marked.parse(_wrapThinking(md) || '');
  const safe = DOMPurify.sanitize(html, {
    ADD_ATTR: ['target', 'rel', 'open'],
    ADD_TAGS: ['details', 'summary'],
  });
  // Turn `[N]` into a clickable inline citation chip; wireCitations() attaches
  // the tooltip + click handler once the HTML is in the DOM.
  return safe.replace(/\[(\d+)\]/g, (_, n) => `<a class="cite" data-cite="${n}" tabindex="0">[${n}]</a>`);
}
function addCopyButtons(root) {
  $$('pre', root).forEach(pre => {
    if (pre.querySelector('.copy-btn')) return;
    const btn = document.createElement('button');
    btn.className = 'copy-btn'; btn.textContent = 'Copy';
    btn.onclick = () => {
      navigator.clipboard.writeText(pre.innerText.replace(/^Copy\n?/, ''));
      btn.textContent = 'Copied'; setTimeout(() => btn.textContent = 'Copy', 1200);
    };
    pre.appendChild(btn);
  });
}

// ---------- Theme ----------
const THEMES = ['dark', 'light', 'slate'];
const THEME_LABELS = { dark: 'Dark', light: 'Light', slate: 'Slate' };
function normalizeTheme(t) { return THEMES.includes(t) ? t : 'dark'; }
function applyTheme(t) {
  const theme = normalizeTheme(t);
  const root = document.documentElement;
  THEMES.forEach(k => root.classList.toggle(k, k === theme));
  localStorage.setItem('r2d2-theme', theme);
  const label = $('#theme-label'); if (label) label.textContent = THEME_LABELS[theme];
  const btn = $('#theme-toggle');
  if (btn) {
    // Cycle: dark → light → slate → dark. aria advertises the *next* theme.
    const next = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
    btn.setAttribute('aria-label', `Switch to ${THEME_LABELS[next].toLowerCase()} theme`);
    btn.setAttribute('data-theme', theme);
  }
  // Re-derive accent container colors against the new surface
  if (state && state.settings && state.settings.accent) applyAccent(state.settings.accent);
  // Re-apply sidebar color so derived contrast tracks the new base theme
  if (state && state.settings) applySidebarColor(state.settings.sidebar_color);
}
applyTheme(normalizeTheme(localStorage.getItem('r2d2-theme') || 'dark'));

// ---------- Accent color ----------
// Curated palette: 10 dark-theme friendly, 5 light-theme friendly.
// Each entry has hex + explicit on-primary chosen for WCAG-safe contrast,
// so we never get dark-on-dark or white-on-yellow failures.
const ACCENT_PRESETS = [
  // Dark-theme friendly (lighter, more saturated — white text)
  { id: 'azure',    name: 'Azure',    hex: '#4da3ff', on: '#001a33', theme: 'dark' },
  { id: 'sky',      name: 'Sky',      hex: '#5bc0eb', on: '#002633', theme: 'dark' },
  { id: 'mint',     name: 'Mint',     hex: '#34d399', on: '#003322', theme: 'dark' },
  { id: 'lime',     name: 'Lime',     hex: '#a3e635', on: '#1a2600', theme: 'dark' },
  { id: 'amber',    name: 'Amber',    hex: '#fbbf24', on: '#332100', theme: 'dark' },
  { id: 'coral',    name: 'Coral',    hex: '#ff8a65', on: '#330d00', theme: 'dark' },
  { id: 'rose',     name: 'Rose',     hex: '#fb7185', on: '#33000d', theme: 'dark' },
  { id: 'pink',     name: 'Pink',     hex: '#f472b6', on: '#33001f', theme: 'dark' },
  { id: 'lavender', name: 'Lavender', hex: '#a78bfa', on: '#1f0d33', theme: 'dark' },
  { id: 'teal',     name: 'Teal',     hex: '#2dd4bf', on: '#003329', theme: 'dark' },
  // Light-theme friendly (deeper, more saturated — white text on them)
  { id: 'royal',    name: 'Royal',    hex: '#1d4ed8', on: '#ffffff', theme: 'light' },
  { id: 'forest',   name: 'Forest',   hex: '#15803d', on: '#ffffff', theme: 'light' },
  { id: 'plum',     name: 'Plum',     hex: '#7c3aed', on: '#ffffff', theme: 'light' },
  { id: 'crimson',  name: 'Crimson',  hex: '#b91c1c', on: '#ffffff', theme: 'light' },
  { id: 'bronze',   name: 'Bronze',   hex: '#b45309', on: '#ffffff', theme: 'light' },
];

function findPreset(hex) {
  if (!hex) return null;
  const v = hex.toLowerCase();
  return ACCENT_PRESETS.find(p => p.hex.toLowerCase() === v) || null;
}

function _hexToRgb(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec((hex || '').trim());
  if (!m) return null;
  const v = parseInt(m[1], 16);
  return { r: (v >> 16) & 255, g: (v >> 8) & 255, b: v & 255 };
}
function _relLum({ r, g, b }) {
  const ch = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b);
}
function _mix(a, b, t) {
  return { r: Math.round(a.r + (b.r - a.r) * t), g: Math.round(a.g + (b.g - a.g) * t), b: Math.round(a.b + (b.b - a.b) * t) };
}
function _rgbStr({ r, g, b }) { return `rgb(${r}, ${g}, ${b})`; }

function applyAccent(hex) {
  const root = document.documentElement.style;
  const props = ['--primary', '--accent', '--on-primary', '--primary-container', '--on-primary-container'];
  if (!hex) { props.forEach(p => root.removeProperty(p)); return; }
  const rgb = _hexToRgb(hex);
  if (!rgb) { props.forEach(p => root.removeProperty(p)); return; }
  const preset = findPreset(hex);
  // Prefer the preset's hand-tuned on-primary; fall back to luminance heuristic.
  const onPrimary = preset ? preset.on : (_relLum(rgb) > 0.55 ? '#000000' : '#ffffff');
  const isDarkTheme = !document.documentElement.classList.contains('light');
  const surfaceMix = isDarkTheme ? { r: 18, g: 18, b: 18 } : { r: 255, g: 255, b: 255 };
  const container = isDarkTheme ? _mix(rgb, surfaceMix, 0.7) : _mix(rgb, surfaceMix, 0.85);
  const onContainer = isDarkTheme ? _mix(rgb, { r: 255, g: 255, b: 255 }, 0.7) : _mix(rgb, { r: 0, g: 0, b: 0 }, 0.55);
  root.setProperty('--primary', hex);
  root.setProperty('--accent', hex);
  root.setProperty('--on-primary', onPrimary);
  root.setProperty('--primary-container', _rgbStr(container));
  root.setProperty('--on-primary-container', _rgbStr(onContainer));
}

// ---------- Sidebar tone ----------
// Curated sidebar background presets that work across themes. Hand-picked
// for WCAG-safe text contrast once `applySidebarColor` derives the fg.
const SIDEBAR_PRESETS = [
  { id: 'graphite', name: 'Graphite', hex: '#2a2d33' },
  { id: 'slate',    name: 'Slate',    hex: '#3a3e44' },
  { id: 'fog',      name: 'Fog',      hex: '#e7e8ec' },
  { id: 'parchment',name: 'Parchment',hex: '#f2ecdf' },
  { id: 'ink',      name: 'Ink',      hex: '#0f1b2d' },
  { id: 'forest',   name: 'Forest',   hex: '#0f2a23' },
  { id: 'wine',     name: 'Wine',     hex: '#2a1421' },
  { id: 'sand',     name: 'Sand',     hex: '#d9c7a7' },
];
function applySidebarColor(hex) {
  const root = document.documentElement.style;
  const props = ['--sidebar-bg', '--sidebar-on', '--sidebar-on-var',
                 '--sidebar-on-faint', '--sidebar-state', '--sidebar-border'];
  if (!hex) { props.forEach(p => root.removeProperty(p)); return; }
  const rgb = _hexToRgb(hex);
  if (!rgb) { props.forEach(p => root.removeProperty(p)); return; }
  const lum = _relLum(rgb);
  const light = lum > 0.45; // background is light -> use dark text
  const fg       = light ? '#1d1d1f' : '#f3f4f6';
  const fgVar    = light ? '#4a4d52' : '#c8ccd3';
  const fgFaint  = light ? '#7a7d83' : '#8e9299';
  // Border: subtle tint of the chosen color toward fg, picked separately for
  // each polarity so it stays visible without becoming heavy.
  const borderMix = light
    ? _mix(rgb, { r: 0, g: 0, b: 0 }, 0.18)
    : _mix(rgb, { r: 255, g: 255, b: 255 }, 0.10);
  root.setProperty('--sidebar-bg',       hex);
  root.setProperty('--sidebar-on',       fg);
  root.setProperty('--sidebar-on-var',   fgVar);
  root.setProperty('--sidebar-on-faint', fgFaint);
  root.setProperty('--sidebar-state',    fg); // state layers tint with fg color
  root.setProperty('--sidebar-border',   _rgbStr(borderMix));
}
function toggleTheme() {
  const cur = THEMES.find(t => document.documentElement.classList.contains(t)) || 'dark';
  const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
  applyTheme(next);
  // persist to user settings if logged in
  if (state.user) {
    api('/api/settings', { method: 'PATCH', body: JSON.stringify({ theme: next }) })
      .then(s => { state.settings = s; }).catch(() => {});
  }
}
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('theme-toggle');
  if (btn) btn.addEventListener('click', toggleTheme);
});
// also bind immediately in case DOM is already ready
if (document.readyState !== 'loading') {
  const btn = document.getElementById('theme-toggle');
  if (btn && !btn.dataset.bound) { btn.dataset.bound = '1'; btn.addEventListener('click', toggleTheme); }
}

// ---------- Toast ----------
// Default duration depends on kind: errors hang around longer so users can read.
function toast(msg, kind = '', ms = null) {
  if (ms === null) ms = (kind === 'err') ? 5000 : 2800;
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateY(8px)'; setTimeout(() => el.remove(), 200); }, ms);
}

// ---------- Focus trap for modal dialogs ----------
const _trapStack = [];
function trapFocus(container) {
  if (!container) return () => {};
  const FOCUSABLE = 'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]):not([type="hidden"]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';
  const previous = document.activeElement;
  const handler = (e) => {
    if (e.key === 'Escape') {
      const cancel = container.querySelector('[data-modal-close], .icon-btn[aria-label="Close"], #ur-close, #ur-ok');
      if (cancel) { e.preventDefault(); cancel.click(); }
      return;
    }
    if (e.key !== 'Tab') return;
    const items = Array.from(container.querySelectorAll(FOCUSABLE)).filter(el => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  container.addEventListener('keydown', handler);
  // Move focus inside on open.
  const initial = container.querySelector(FOCUSABLE);
  if (initial) initial.focus();
  const release = () => {
    container.removeEventListener('keydown', handler);
    if (previous && previous.focus) previous.focus();
    const i = _trapStack.indexOf(release); if (i >= 0) _trapStack.splice(i, 1);
  };
  _trapStack.push(release);
  return release;
}
function releaseTopTrap() {
  const release = _trapStack.pop();
  if (release) release();
}

// Auto-bind focus trap to known dialogs by watching their .hidden class.
function _bindAutoTrap(id) {
  const root = document.getElementById(id);
  if (!root) return;
  let release = null;
  new MutationObserver(() => {
    const open = !root.classList.contains('hidden');
    if (open && !release) release = trapFocus(root);
    else if (!open && release) { release(); release = null; }
  }).observe(root, { attributes: true, attributeFilter: ['class'] });
}
['modal', 'move-picker', 'upload-report'].forEach(_bindAutoTrap);

// ---------- Account menu ----------
$('#account-btn').addEventListener('click', (e) => {
  e.stopPropagation();
  const menu = $('#account-menu');
  const open = menu.classList.toggle('hidden');
  $('#account-btn').setAttribute('aria-expanded', String(!open));
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('#account-menu') && !e.target.closest('#account-btn')) {
    $('#account-menu').classList.add('hidden');
    $('#account-btn').setAttribute('aria-expanded', 'false');
  }
});
$('#account-menu').addEventListener('click', async (e) => {
  const action = e.target.closest('[data-action]')?.dataset.action;
  if (!action) return;
  $('#account-menu').classList.add('hidden');
  if (action === 'settings') openSettings('general');
  else if (action === 'usage') openSettings('usage');
  else if (action === 'logout') {
    await api('/api/auth/logout', { method: 'POST' });
    location.replace('/login');
  }
});

// ---------- Boot ----------
async function init() {
  try { state.brand = await api('/api/branding'); } catch { state.brand = null; }
  try { state.user = await api('/api/auth/me'); } catch { return; }
  applyBranding();
  renderUser();
  state.settings = await api('/api/settings');
  await Promise.all([refreshModels(), refreshProviders(), refreshChats(), refreshDocs()]);
  applyTheme(state.settings.theme || localStorage.getItem('r2d2-theme') || 'dark');
  applyAccent(state.settings.accent || null);
  applySidebarColor(state.settings.sidebar_color || (state.brand?.theme?.sidebar_color || null));
  renderEmpty();
  $('#toggle-rag').checked = !!state.settings.use_rag_default;
  $('#toggle-web').checked = !!state.settings.use_web_default;
  syncWebChipLabel();
}

// Names ------------------------------------------------------------------
function brandName()      { return state.brand?.product?.name        || 'AI'; }
function brandShort()     { return state.brand?.product?.short_name  || 'AI'; }
function assistantName()  { return state.brand?.persona?.assistant_name || brandName(); }

function applyBranding() {
  const b = state.brand;
  if (!b) return;
  _refreshAssistantIcon();
  // Theme overrides (CSS custom properties, only the ones we expose)
  const t = b.theme || {};
  const root = document.documentElement;
  const map = {
    primary: '--primary',
    on_primary: '--on-primary',
    primary_container: '--primary-container',
    on_primary_container: '--on-primary-container',
    accent: '--accent',
  };
  for (const [k, css] of Object.entries(map)) {
    if (t[k]) root.style.setProperty(css, t[k]);
  }
  // Branded default sidebar tone — only used when the user has no override.
  if (t.sidebar_color && !(state.settings && state.settings.sidebar_color)) {
    applySidebarColor(t.sidebar_color);
  }
  // Branded slate base color — inject scoped overrides for html.slate so the
  // slate theme adopts the brand's grey without touching dark/light.
  if (t.slate_base) {
    injectSlateOverrides(t.slate_base);
  }
  if (t.font_url) {
    const link = document.createElement('link');
    link.rel = 'stylesheet'; link.href = t.font_url;
    document.head.appendChild(link);
  }
  if (t.font_family) root.style.setProperty('--font-sans', t.font_family);
}

// Derive a full slate surface ramp from a single base hex and inject it as a
// scoped <style> block so it only affects html.slate.
function injectSlateOverrides(baseHex) {
  const rgb = _hexToRgb(baseHex);
  if (!rgb) return;
  const white = { r: 255, g: 255, b: 255 };
  const black = { r: 0, g: 0, b: 0 };
  const light = _relLum(rgb) > 0.45;
  // Build a 5-step ramp around the base by mixing toward white/black.
  // For dark slates, surfaces step lighter; for light slates, surfaces step darker.
  const tint = light ? black : white;
  const ramp = [0.04, 0.08, 0.14, 0.20, 0.27].map(t => _rgbStr(_mix(rgb, tint, t)));
  const dim  = _rgbStr(_mix(rgb, light ? white : black, 0.12));
  const fg       = light ? '#1d1d1f' : '#e8eaed';
  const fgVar    = light ? '#4a4d52' : '#b8bdc4';
  const fgFaint  = light ? '#7a7d83' : '#898e95';
  const outline  = _rgbStr(_mix(rgb, light ? black : white, 0.22));
  const outlineV = _rgbStr(_mix(rgb, light ? black : white, 0.10));
  const css = `html.slate {
  --surface: ${baseHex};
  --surface-dim: ${dim};
  --surface-bright: ${ramp[3]};
  --surface-1: ${ramp[0]};
  --surface-2: ${ramp[1]};
  --surface-3: ${ramp[2]};
  --surface-4: ${ramp[3]};
  --surface-5: ${ramp[4]};
  --on-surface: ${fg};
  --on-surface-var: ${fgVar};
  --on-surface-faint: ${fgFaint};
  --outline: ${outline};
  --outline-variant: ${outlineV};
}`;
  let tag = document.getElementById('brand-slate-overrides');
  if (!tag) {
    tag = document.createElement('style');
    tag.id = 'brand-slate-overrides';
    document.head.appendChild(tag);
  }
  tag.textContent = css;
}

function renderUser() {
  const u = state.user;
  const name = u.display_name || u.username;
  $('#user-name').textContent = name;
  $('#user-sub').textContent = u.is_admin ? `${u.username} · admin` : u.username;
  setUserAvatar($('#user-avatar'), u, name);
}

function setUserAvatar(el, u, name, sizeClass) {
  el.classList.add('avatar');
  if (sizeClass) el.classList.add(sizeClass);
  if (u && u.avatar) {
    el.innerHTML = `<img alt="" src="/api/settings/avatar/${u.id}?v=${encodeURIComponent(u.avatar)}">`;
  } else {
    const initial = ((name || u?.username || '?')[0] || '?').toUpperCase();
    el.textContent = initial;
  }
}

const FALLBACK_BRAND_ICON = `<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"
  fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round">
  <circle cx="32" cy="32" r="26"/>
  <path d="M32 54 L19.07 14.20 L52.92 38.80 L11.08 38.80 L44.93 14.20 Z"/>
  <circle cx="32" cy="32" r="1.8" fill="#a8843a" stroke="none"/>
</svg>`;
let ASSISTANT_ICON = FALLBACK_BRAND_ICON;
function _refreshAssistantIcon() {
  const url = state.brand?.assets?.logo_light || state.brand?.assets?.logo_dark || '';
  ASSISTANT_ICON = url
    ? `<img src="${url}" alt="" style="width:100%;height:100%;display:block;" />`
    : FALLBACK_BRAND_ICON;
}
function assistantAvatar(extraClass = '') {
  return `<div class="avatar r ${extraClass}" aria-hidden="true">${ASSISTANT_ICON}</div>`;
}

// ---------- Search providers ----------
async function refreshProviders() {
  try {
    const data = await api('/api/search/providers');
    state.providers = data.providers; state.activeProvider = data.active;
    renderProviderMenu();
    syncWebChipLabel();
  } catch { /* ignore */ }
}
function renderProviderMenu() {
  const root = $('#provider-menu');
  root.innerHTML = state.providers.map(p => `
    <button class="menu-item" data-provider="${p.id}" role="menuitemradio" aria-checked="${p.active}">
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        ${p.active
          ? `<path d="M20 6L9 17l-5-5"/>`
          : (p.configured
              ? `<circle cx="12" cy="12" r="9"/>`
              : `<circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><circle cx="12" cy="16" r="0.5" fill="currentColor"/>`)}
      </svg>
      <span class="grow">${escapeHtml(p.label)}</span>
      <span class="hint">${p.configured ? (p.monthly_quota ? `${p.monthly_quota}/mo` : 'ready') : 'setup'}</span>
    </button>`).join('');
  $$('#provider-menu [data-provider]').forEach(b => b.addEventListener('click', async (e) => {
    e.stopPropagation();
    const id = b.dataset.provider;
    const p = state.providers.find(x => x.id === id);
    if (!p) return;
    if (!p.configured) {
      $('#account-menu').classList.add('hidden');
      openSettings('web');
      return;
    }
    try {
      state.settings = await api('/api/settings', { method: 'PATCH', body: JSON.stringify({ search_provider: id }) });
      await refreshProviders();
      toast(`Search provider: ${p.label}`, 'ok');
    } catch (err) { toast(err.message, 'err'); }
  }));
}
function syncWebChipLabel() {
  const active = state.providers.find(p => p.active);
  $('#web-chip-label').textContent = active ? `Web · ${active.label}` : 'Web';
}

function syncRefChipLabel() {
  const n = state.refChatIds.size;
  $('#ref-chip-label').textContent = n ? `Chats · ${n}` : 'Chats';
  $('#ref-chats')?.classList.toggle('on', n > 0);
}

function openRefChatsPicker() {
  // Don't allow self-reference (it's already in the conversation history).
  const candidates = state.chats.filter(c => c.id !== state.currentChatId);
  if (!candidates.length) {
    toast('No other chats to reference yet', 'info');
    return;
  }
  // Tear down any existing instance.
  document.getElementById('ref-picker')?.remove();
  const overlay = document.createElement('div');
  overlay.id = 'ref-picker';
  overlay.className = 'ref-picker-overlay';
  const rows = candidates.map(c => {
    const checked = state.refChatIds.has(c.id) ? 'checked' : '';
    return `<label class="ref-chat-row">
      <input type="checkbox" data-cid="${escapeAttr(c.id)}" ${checked}>
      <span class="title">${escapeHtml(c.title || 'Untitled')}</span>
    </label>`;
  }).join('');
  overlay.innerHTML = `
    <div class="ref-picker-panel" role="dialog" aria-modal="true" aria-label="Reference other chats">
      <div class="ref-picker-head">
        <strong>Reference other chats</strong>
        <button type="button" class="icon-btn" id="ref-picker-close" aria-label="Close">\u2715</button>
      </div>
      <div class="ref-chat-list">${rows}</div>
      <p class="desc" style="margin:var(--sp-2) 0 0;">Recent messages from selected chats will be added as context to your next prompt.</p>
      <div class="ref-picker-actions">
        <button type="button" class="btn btn-text" id="ref-picker-clear">Clear all</button>
        <button type="button" class="btn btn-filled" id="ref-picker-done">Done</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.querySelector('#ref-picker-close').addEventListener('click', close);
  overlay.querySelector('#ref-picker-clear').addEventListener('click', () => {
    state.refChatIds.clear(); syncRefChipLabel(); close();
  });
  overlay.querySelector('#ref-picker-done').addEventListener('click', () => {
    state.refChatIds = new Set(
      $$('#ref-picker input[type="checkbox"][data-cid]:checked').map(c => c.dataset.cid)
    );
    syncRefChipLabel(); close();
  });
}

document.addEventListener('DOMContentLoaded', () => {
  $('#ref-chats')?.addEventListener('click', openRefChatsPicker);
  syncRefChipLabel();
});

// ---------- Chats sidebar ----------
async function refreshChats() {
  state.chats = await api('/api/chats');
  renderChatList();
}
function renderChatList() {
  const q = $('#chat-search').value.trim().toLowerCase();
  const list = state.chats.filter(c => !q || (c.title || '').toLowerCase().includes(q));
  const el = $('#chat-list');
  el.innerHTML = '';
  if (!list.length) {
    el.innerHTML = `<div style="padding:var(--sp-4); font:var(--t-caption); color:var(--on-surface-faint); text-align:center;">No chats yet</div>`;
    return;
  }
  for (const c of list) {
    const item = document.createElement('div');
    item.className = 'nav-row' + (c.id === state.currentChatId ? ' active' : '');
    const streaming = state.streams.get(c.id) && !state.streams.get(c.id).done;
    item.innerHTML = `
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8z"/></svg>
      <span class="title">${escapeHtml(c.title || 'New chat')}</span>
      ${streaming ? '<span class="stream-dot" title="Streaming…"></span>' : ''}
      <span class="row-actions">
        <button class="icon-btn sm" data-act="rename" title="Rename"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4z"/></svg></button>
        <button class="icon-btn sm" data-act="delete" title="Delete"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg></button>
      </span>`;
    item.addEventListener('click', (e) => {
      const act = e.target.closest('[data-act]')?.dataset.act;
      if (act === 'rename') { e.stopPropagation(); renameChat(c); }
      else if (act === 'delete') { e.stopPropagation(); deleteChat(c); }
      else openChat(c.id);
    });
    el.appendChild(item);
  }
}
$('#chat-search').addEventListener('input', renderChatList);

async function openChat(id) {
  // Cancel any pending poller from the chat we're leaving.
  const prev = state.currentChatId;
  if (prev && prev !== id) {
    const h = _pendingPollers.get(prev);
    if (h) { h.cancelled = true; _pendingPollers.delete(prev); }
  }
  state.currentChatId = id;
  renderChatList();
  // A different chat is now active; clear any pending cross-chat references
  // (they would otherwise include or exclude the wrong chat).
  if (state.refChatIds.size) { state.refChatIds.clear(); syncRefChipLabel?.(); }
  // If this chat has a live stream, re-attach instead of fetching history.
  const stream = state.streams.get(id);
  if (stream && !stream.done) {
    // Reflect the streaming chat's model in the composer, otherwise the
    // dropdown still shows the model from whichever chat we came from.
    if (stream.model) {
      state.currentModel = stream.model;
      const sel = $('#model-select');
      if (sel && [...sel.options].some(o => o.value === stream.model)) {
        sel.value = stream.model;
      }
    }
    const root = $('#messages');
    root.innerHTML = `<div class="messages-inner"></div>`;
    const inner = $('.messages-inner', root);
    // Re-render the user's prompt + a fresh assistant row, then rebind the
    // stream's DOM refs so subsequent tokens land in the new nodes.
    inner.appendChild(renderMessage('user', state.lastUserMessage || ''));
    const aRow = renderMessage('assistant', '');
    const body = aRow.querySelector('.prose');
    body.classList.add('typing');
    inner.appendChild(aRow);
    stream.aRow = aRow; stream.body = body;
    _renderStreamInto(stream, body);
    refreshChatAttachments();
    _refreshComposerUI();
    scrollToBottom();
    return;
  }
  renderChatSkeleton();
  try {
    const { chat, messages } = await api(`/api/chats/${id}`);
    const sel = $('#model-select');
    const available = sel ? [...sel.options].map(o => o.value) : [];
    let target = chat.model || state.currentModel;
    if (target && available.length && !available.includes(target)) {
      // The chat's stored model has been removed since last use.
      const fallback = available.includes(state.settings?.default_model)
        ? state.settings.default_model : available[0];
      toast(`Model "${target}" is no longer installed; using ${fallback}`, 'info');
      target = fallback;
    }
    if (target) {
      state.currentModel = target;
      if (sel) sel.value = target;
    }
    renderMessages(messages);
    refreshChatAttachments();
    // If the last persisted message is the user's, a reply is still in
    // flight server-side (e.g. the page was refreshed mid-stream). Show a
    // typing indicator and poll until the assistant message lands.
    const last = messages[messages.length - 1];
    if (last && last.role === 'user') _attachPendingPoller(id, messages.length);
  } catch (err) {
    toast(err.message, 'err');
    renderEmpty();
  }
  _refreshComposerUI();
}

// Per-chat poller registry so navigating away cancels the previous one.
const _pendingPollers = new Map();
function _attachPendingPoller(chatId, baseCount) {
  // Cancel any prior poller for this chat.
  _pendingPollers.get(chatId)?.cancelled && _pendingPollers.delete(chatId);
  const handle = { cancelled: false };
  _pendingPollers.set(chatId, handle);

  // Show a "generating" placeholder row with the typing cursor.
  const inner = ensureInner();
  const aRow = renderMessage('assistant', '');
  const body = aRow.querySelector('.prose');
  body.classList.add('typing');
  body.innerHTML = '<span class="muted">Generating response\u2026</span>';
  aRow.dataset.pending = '1';
  inner.appendChild(aRow);
  scrollToBottom();

  const tick = async () => {
    if (handle.cancelled || state.currentChatId !== chatId) {
      _pendingPollers.delete(chatId);
      return;
    }
    try {
      const { messages } = await api(`/api/chats/${chatId}`);
      if (handle.cancelled || state.currentChatId !== chatId) return;
      if (messages.length > baseCount) {
        // Reply landed \u2014 re-render fully and stop polling.
        renderMessages(messages);
        _pendingPollers.delete(chatId);
        return;
      }
    } catch { /* keep polling \u2014 transient errors are fine */ }
    setTimeout(tick, 1500);
  };
  setTimeout(tick, 1200);
}

function renderChatSkeleton() {
  const root = $('#messages');
  if (!root) return;
  root.innerHTML = `
    <div class="msg-skel" aria-hidden="true">
      <div class="skel-line w70"></div>
      <div class="skel-line w90"></div>
      <div class="skel-line w55"></div>
    </div>
    <div class="msg-skel assistant" aria-hidden="true">
      <div class="skel-line w95"></div>
      <div class="skel-line w80"></div>
      <div class="skel-line w60"></div>
    </div>`;
}
async function renameChat(c) {
  const title = prompt('Rename chat', c.title);
  if (!title) return;
  await api(`/api/chats/${c.id}`, { method: 'PATCH', body: JSON.stringify({ title }) });
  refreshChats();
}
async function deleteChat(c) {
  if (!confirm(`Delete "${c.title}"?`)) return;
  await api(`/api/chats/${c.id}`, { method: 'DELETE' });
  if (state.currentChatId === c.id) {
    state.currentChatId = null;
    state.chatAttachments = [];
    renderChatAttachments();
    renderEmpty();
  }
  refreshChats();
}
$('#new-chat').addEventListener('click', () => {
  state.currentChatId = null;
  state.chatAttachments = [];
  renderChatAttachments();
  renderChatList(); renderEmpty(); $('#input').focus();
  _refreshComposerUI();
});

// ---------- Documents (folder tree) ----------
async function refreshDocs() {
  const data = await api('/api/documents');
  // Backwards compat if server returns a bare array
  if (Array.isArray(data)) { state.documents = data; state.folders = []; }
  else { state.documents = data.documents || []; state.folders = data.folders || []; }
  renderDocs();
  // Kick off polling for any docs that are still indexing in the background.
  for (const d of state.documents) {
    if (d.status === 'indexing') pollIndexing(d.id);
  }
}

const _pollers = new Set();
async function pollIndexing(id) {
  if (_pollers.has(id)) return;
  _pollers.add(id);
  let delay = 800;
  for (let i = 0; i < 30; i++) {  // ~ up to 2 minutes total
    await new Promise(r => setTimeout(r, delay));
    delay = Math.min(delay * 1.4, 5000);
    try {
      const d = await api(`/api/documents/${id}`);
      if (d.status === 'ready' || d.status === 'error') {
        _pollers.delete(id);
        if (d.status === 'error') toast(`Indexing failed: ${d.error || 'unknown'}`, 'err');
        await refreshDocs();
        return;
      }
    } catch {
      _pollers.delete(id);
      return;
    }
  }
  _pollers.delete(id);
}
function fmtBytes(b) {
  if (!b) return '';
  const u = ['B', 'KB', 'MB', 'GB']; let i = 0, n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
}

// Persist expanded folders across renders within session
state.expandedFolders = new Set(JSON.parse(localStorage.getItem('r2d2-expanded-folders') || '[]'));
function persistExpanded() {
  localStorage.setItem('r2d2-expanded-folders', JSON.stringify([...state.expandedFolders]));
}

function buildTree(folders, docs) {
  // Tree node: { name, path, children: Map<name,node>, docs: [] }
  const root = { name: '', path: '', children: new Map(), docs: [] };
  const ensure = (path) => {
    if (!path) return root;
    const parts = path.split('/');
    let node = root;
    let acc = '';
    for (const p of parts) {
      acc = acc ? `${acc}/${p}` : p;
      if (!node.children.has(p)) {
        node.children.set(p, { name: p, path: acc, children: new Map(), docs: [] });
      }
      node = node.children.get(p);
    }
    return node;
  };
  for (const f of folders) ensure(f);
  for (const d of docs) ensure(d.folder || '').docs.push(d);
  return root;
}

function svgChevron() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>`;
}
function svgFolder(open = false) {
  return open
    ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v1"/><path d="M3 9h18l-2 9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>`
    : `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`;
}
function svgFile() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg>`;
}
function svgTrash() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>`;
}
function svgPencil() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4z"/></svg>`;
}
function svgMove() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v3"/><path d="M14 17h7"/><path d="M18 14l3 3-3 3"/></svg>`;
}

function renderDocs() {
  const el = $('#doc-list');
  el.innerHTML = '';
  if (!state.documents.length && !state.folders.length) {
    el.innerHTML = `<div class="empty">No documents yet</div>`;
    return;
  }
  const tree = buildTree(state.folders, state.documents);
  renderTreeChildren(tree, el, 0);
  // Allow dropping onto root area to move to root
  el.ondragover = (e) => { e.preventDefault(); };
  el.ondrop = async (e) => {
    e.preventDefault();
    const id = e.dataTransfer.getData('application/r2d2-doc');
    if (id) await moveDoc(id, '');
  };
}

function renderTreeChildren(node, container, depth) {
  // Sort: folders first (alpha), then docs (newest first — already from server)
  const folders = [...node.children.values()].sort((a, b) => a.name.localeCompare(b.name));
  for (const child of folders) renderFolder(child, container, depth);
  for (const d of node.docs) renderDocLeaf(d, container, depth);
}

function renderFolder(node, container, depth) {
  const expanded = state.expandedFolders.has(node.path);
  const row = document.createElement('div');
  row.className = `tree-row folder${expanded ? ' expanded' : ''}`;
  row.style.paddingLeft = `${8 + depth * 14}px`;
  row.dataset.folder = node.path;
  row.innerHTML = `
    <span class="twisty">${svgChevron()}</span>
    <span class="icon">${svgFolder(expanded)}</span>
    <span class="name">${escapeHtml(node.name)}</span>
    <span class="meta">${countDocs(node)}</span>
    <span class="actions">
      <button class="icon-btn sm" data-act="upload" title="Upload here">${svgFolder(false).replace('<svg', '<svg style="opacity:.7"')}</button>
      <button class="icon-btn sm" data-act="move" title="Move folder">${svgMove()}</button>
      <button class="icon-btn sm" data-act="rename" title="Rename">${svgPencil()}</button>
      <button class="icon-btn sm" data-act="delete" title="Delete folder">${svgTrash()}</button>
    </span>`;
  // Click to toggle
  row.addEventListener('click', (e) => {
    if (e.target.closest('[data-act]')) return;
    if (state.expandedFolders.has(node.path)) state.expandedFolders.delete(node.path);
    else state.expandedFolders.add(node.path);
    persistExpanded();
    renderDocs();
  });
  // Actions
  row.querySelector('[data-act="rename"]').addEventListener('click', async (e) => {
    e.stopPropagation();
    const newName = prompt('Rename folder', node.name);
    if (!newName || newName === node.name) return;
    const parent = node.path.includes('/') ? node.path.slice(0, node.path.lastIndexOf('/')) : '';
    const dst = parent ? `${parent}/${newName}` : newName;
    try {
      await api('/api/documents/folders', { method: 'PATCH', body: JSON.stringify({ from: node.path, to: dst }) });
      // Update expanded set for renamed paths
      const newSet = new Set();
      for (const p of state.expandedFolders) {
        if (p === node.path) newSet.add(dst);
        else if (p.startsWith(node.path + '/')) newSet.add(dst + p.slice(node.path.length));
        else newSet.add(p);
      }
      state.expandedFolders = newSet; persistExpanded();
      await refreshDocs(); toast('Folder renamed', 'ok');
    } catch (err) { toast(err.message, 'err'); }
  });
  row.querySelector('[data-act="delete"]').addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!confirm(`Delete folder "${node.path}" and all documents inside?`)) return;
    try {
      await api(`/api/documents/folders?path=${encodeURIComponent(node.path)}`, { method: 'DELETE' });
      await refreshDocs(); toast('Folder deleted', 'ok');
    } catch (err) { toast(err.message, 'err'); }
  });
  row.querySelector('[data-act="upload"]').addEventListener('click', (e) => {
    e.stopPropagation();
    state.uploadTargetFolder = node.path;
    $('#file-input').click();
  });
  row.querySelector('[data-act="move"]').addEventListener('click', (e) => {
    e.stopPropagation();
    openMovePicker({ kind: 'folder', srcPath: node.path, name: node.name });
  });
  // Drag-and-drop target for moving docs
  row.addEventListener('dragover', (e) => { e.preventDefault(); row.classList.add('drop-target'); });
  row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
  row.addEventListener('drop', async (e) => {
    e.preventDefault(); e.stopPropagation();
    row.classList.remove('drop-target');
    const id = e.dataTransfer.getData('application/r2d2-doc');
    if (id) await moveDoc(id, node.path);
  });
  container.appendChild(row);
  if (expanded) renderTreeChildren(node, container, depth + 1);
}

function countDocs(node) {
  let n = node.docs.length;
  for (const c of node.children.values()) n += countDocs(c);
  return n;
}

function renderDocLeaf(d, container, depth) {
  const row = document.createElement('div');
  row.className = 'tree-row';
  row.style.paddingLeft = `${8 + depth * 14}px`;
  row.dataset.docId = d.id;
  row.draggable = true;
  const statusBadge =
    d.status === 'indexing' ? '<span class="meta indexing" title="Indexing…">⏳</span>'
    : d.status === 'error'   ? `<span class="meta err" data-act="reindex" role="button" tabindex="0" title="${escapeHtml((d.error || 'error') + ' — click to retry indexing')}">⚠</span>`
    : `<span class="meta">${d.chunks}</span>`;
  row.title = `${d.name}\n${d.status === 'indexing' ? 'indexing…' : d.chunks + ' chunks'} • ${fmtBytes(d.bytes)}${d.folder ? '\n' + d.folder : ''}`;
  row.innerHTML = `
    <span class="twisty placeholder">${svgChevron()}</span>
    <span class="icon">${svgFile()}</span>
    <span class="name">${escapeHtml(d.name)}</span>
    ${statusBadge}
    <span class="actions">
      <button class="icon-btn sm" data-act="move" title="Move">${svgMove()}</button>
      <button class="icon-btn sm" data-act="rename" title="Rename">${svgPencil()}</button>
      <button class="icon-btn sm" data-act="delete" title="Delete">${svgTrash()}</button>
    </span>`;
  row.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('application/r2d2-doc', d.id);
    e.dataTransfer.effectAllowed = 'move';
  });
  row.querySelector('[data-act="move"]').addEventListener('click', (e) => {
    e.stopPropagation();
    openMovePicker({ kind: 'doc', id: d.id, name: d.name, currentFolder: d.folder || '' });
  });
  row.querySelector('[data-act="rename"]').addEventListener('click', async (e) => {
    e.stopPropagation();
    const name = prompt('Rename document', d.name);
    if (!name || name === d.name) return;
    try {
      await api(`/api/documents/${d.id}`, { method: 'PATCH', body: JSON.stringify({ name }) });
      await refreshDocs(); toast('Renamed', 'ok');
    } catch (err) { toast(err.message, 'err'); }
  });
  row.querySelector('[data-act="delete"]').addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!confirm(`Delete "${d.name}"?`)) return;
    try {
      await api(`/api/documents/${d.id}`, { method: 'DELETE' });
      await refreshDocs(); toast('Document removed', 'ok');
    } catch (err) { toast(err.message, 'err'); }
  });
  const reindexBadge = row.querySelector('[data-act="reindex"]');
  if (reindexBadge) {
    const doReindex = async (e) => {
      e.stopPropagation();
      if (!confirm(`Retry indexing "${d.name}"?\n\nFor scanned PDFs this will run OCR (~1–3s per page).`)) return;
      try {
        await api(`/api/documents/${d.id}/reindex`, { method: 'POST' });
        toast('Reindexing started', 'ok');
        await refreshDocs();
      } catch (err) {
        // 410 = the original file is gone (deleted under older code).
        // Offer to re-upload in place: pick a file, upload it with the
        // same name+folder, then remove the orphaned record.
        if (/no longer on disk|410/i.test(err.message)) {
          const ok = confirm(
            `The original file for "${d.name}" is no longer on disk.\n\n` +
            `Pick the file again and we'll re-upload it into the same folder.`
          );
          if (!ok) return;
          await _replaceFailedDoc(d);
        } else {
          toast(err.message, 'err');
        }
      }
    };
    reindexBadge.addEventListener('click', doReindex);
    reindexBadge.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); doReindex(e); }
    });
  }
  container.appendChild(row);
}

// Re-upload helper used when the on-disk file for a failed document has
// been lost (older code unlinked failed uploads). Picks a single file,
// uploads it into the same folder, then deletes the old failed record so
// the tree doesn't show duplicates.
async function _replaceFailedDoc(d) {
  const picker = document.createElement('input');
  picker.type = 'file';
  picker.style.display = 'none';
  document.body.appendChild(picker);
  const file = await new Promise((resolve) => {
    picker.addEventListener('change', () => resolve(picker.files?.[0] || null), { once: true });
    picker.addEventListener('cancel', () => resolve(null), { once: true });
    picker.click();
  });
  picker.remove();
  if (!file) return;
  try {
    const fd = new FormData();
    fd.append('file', file, file.name);
    if (d.folder) fd.append('folder', d.folder);
    fd.append('ocr', 'true');
    await uploadOne(fd);
    // Remove the old failed record after the new one is queued.
    try { await api(`/api/documents/${d.id}`, { method: 'DELETE' }); } catch {}
    toast('Re-uploaded — indexing in background', 'ok');
    await refreshDocs();
  } catch (err) {
    toast(err.message, 'err');
  }
}

async function moveDoc(id, folder) {
  try {
    await api(`/api/documents/${id}`, { method: 'PATCH', body: JSON.stringify({ folder }) });
    if (folder) state.expandedFolders.add(folder);
    persistExpanded();
    await refreshDocs();
    toast(folder ? `Moved to ${folder}` : 'Moved to root', 'ok');
  } catch (err) { toast(err.message, 'err'); }
}

async function moveFolder(srcPath, parent) {
  const name = srcPath.includes('/') ? srcPath.slice(srcPath.lastIndexOf('/') + 1) : srcPath;
  const dst = parent ? `${parent}/${name}` : name;
  if (dst === srcPath) return;
  try {
    await api('/api/documents/folders', { method: 'PATCH', body: JSON.stringify({ from: srcPath, to: dst }) });
    // Migrate expanded set entries that were under srcPath
    const next = new Set();
    for (const p of state.expandedFolders) {
      if (p === srcPath) next.add(dst);
      else if (p.startsWith(srcPath + '/')) next.add(dst + p.slice(srcPath.length));
      else next.add(p);
    }
    if (parent) next.add(parent);
    next.add(dst);
    state.expandedFolders = next; persistExpanded();
    await refreshDocs();
    toast(`Moved to ${parent || 'root'}`, 'ok');
  } catch (err) { toast(err.message, 'err'); }
}

// ---- Move picker dialog ---------------------------------------------------
function openMovePicker(opts) {
  // opts: { kind: 'doc'|'folder', id?, srcPath?, name, currentFolder? }
  const dlg = $('#move-picker');
  const body = $('#mp-body');
  const confirmBtn = $('#mp-confirm');
  $('#mp-title').textContent = opts.kind === 'folder' ? `Move folder "${opts.name}"` : `Move "${opts.name}"`;

  // Destinations: all folders + root. For folder moves, exclude self and descendants.
  let folders = (state.folders || []).slice().sort();
  if (opts.kind === 'folder') {
    folders = folders.filter(f => f !== opts.srcPath && !f.startsWith(opts.srcPath + '/'));
  }
  const current = opts.kind === 'folder'
    ? (opts.srcPath.includes('/') ? opts.srcPath.slice(0, opts.srcPath.lastIndexOf('/')) : '')
    : (opts.currentFolder || '');

  let selected = current; // start highlighting current location
  const renderList = () => {
    body.innerHTML = `
      <div class="mp-current">Currently in: <strong>${current ? escapeHtml(current) : 'Root'}</strong></div>
      <div class="mp-list">
        <button type="button" class="mp-item ${selected === '' ? 'selected' : ''}" data-path="">
          ${svgFolder(false)}<span class="name">Root</span>
        </button>
        ${folders.map(f => {
          const depth = f.split('/').length - 1;
          const leaf = f.includes('/') ? f.slice(f.lastIndexOf('/') + 1) : f;
          return `
            <button type="button" class="mp-item ${selected === f ? 'selected' : ''}" data-path="${escapeAttr(f)}" style="padding-left:${12 + depth * 16}px">
              ${svgFolder(false)}<span class="name">${escapeHtml(leaf)}</span><span class="path">${escapeHtml(f)}</span>
            </button>`;
        }).join('')}
      </div>
      <div class="mp-new">
        <input class="field" id="mp-new-input" placeholder="Or type a new folder path (e.g. Projects/2026)">
        <button class="btn" id="mp-new-btn" type="button">Create &amp; move</button>
      </div>`;
    body.querySelectorAll('.mp-item').forEach(b => b.addEventListener('click', () => {
      selected = b.dataset.path;
      body.querySelectorAll('.mp-item').forEach(x => x.classList.toggle('selected', x === b));
      updateConfirmState();
    }));
    $('#mp-new-btn').addEventListener('click', async () => {
      const p = $('#mp-new-input').value.trim().replace(/^\/+|\/+$/g, '');
      if (!p) return;
      try {
        await api('/api/documents/folders', { method: 'POST', body: JSON.stringify({ path: p }) });
        await refreshDocs();
        // Fold new folder into list and select it
        if (!folders.includes(p)) folders.push(p);
        folders.sort();
        selected = p;
        renderList();
        updateConfirmState();
      } catch (err) { toast(err.message, 'err'); }
    });
    updateConfirmState();
  };
  const updateConfirmState = () => {
    const sameAsCurrent = selected === current;
    // For folder moves, also disallow choosing self/descendant (already excluded above)
    confirmBtn.disabled = sameAsCurrent;
  };
  confirmBtn.onclick = async () => {
    dlg.classList.add('hidden');
    if (opts.kind === 'doc') await moveDoc(opts.id, selected);
    else await moveFolder(opts.srcPath, selected);
  };
  renderList();
  dlg.classList.remove('hidden');
}
$('#mp-close')?.addEventListener('click', () => $('#move-picker').classList.add('hidden'));
$('#mp-cancel')?.addEventListener('click', () => $('#move-picker').classList.add('hidden'));
$('#move-picker')?.addEventListener('click', (e) => {
  if (e.target.id === 'move-picker') $('#move-picker').classList.add('hidden');
});

// Upload buttons
$('#attach').addEventListener('click', () => {
  state.uploadTargetFolder = '';
  state.uploadAsChatAttachment = true;
  $('#file-input').click();
});
$('#add-file').addEventListener('click', () => {
  state.uploadTargetFolder = '';
  state.uploadAsChatAttachment = false;
  $('#file-input').click();
});
$('#add-folder')?.addEventListener('click', async () => {
  const name = prompt('New folder name (use "/" for nested):');
  if (!name) return;
  try {
    await api('/api/documents/folders', J({ path: name }));
    const path = name.replace(/^\/+|\/+$/g, '');
    if (path) state.expandedFolders.add(path);
    persistExpanded();
    await refreshDocs(); toast('Folder created', 'ok');
  } catch (err) { toast(err.message, 'err'); }
});
$('#add-folder-upload')?.addEventListener('click', () => $('#folder-input').click());

$('#file-input').addEventListener('change', async (e) => {
  const files = [...e.target.files]; e.target.value = '';
  if (!files.length) return;
  const asChatAttachment = !!state.uploadAsChatAttachment;
  state.uploadAsChatAttachment = false;
  if (asChatAttachment) {
    await uploadChatAttachments(files);
    return;
  }
  const folder = state.uploadTargetFolder || '';
  state.uploadTargetFolder = '';
  const queue = files.map(f => ({ file: f, folder }));
  const { count, failures, cancelled } = await uploadBatch(queue, {
    title: files.length === 1 ? `Uploading ${files[0].name}` : `Uploading ${files.length} files`,
  });
  persistExpanded();
  refreshDocs();
  finishUploadReport({ total: files.length, count, failures, cancelled });
});

async function ensureCurrentChat() {
  if (state.currentChatId) return state.currentChatId;
  const c = await api('/api/chats', J({ title: 'New chat' }));
  state.currentChatId = c.id;
  await refreshChats();
  renderChatList();
  return c.id;
}

async function uploadChatAttachments(files) {
  const cid = await ensureCurrentChat();
  const queue = files.map(f => ({ file: f, folder: '' }));
  const { count, failures, cancelled } = await uploadBatch(queue, {
    title: files.length === 1 ? `Attaching ${files[0].name}` : `Attaching ${files.length} files`,
    chat_id: cid,
  });
  await refreshChatAttachments();
  finishUploadReport({ total: files.length, count, failures, cancelled });
}

async function refreshChatAttachments() {
  if (!state.currentChatId) {
    state.chatAttachments = [];
    renderChatAttachments();
    return;
  }
  try {
    const r = await api(`/api/chats/${state.currentChatId}/documents`);
    state.chatAttachments = r.documents || [];
  } catch { state.chatAttachments = []; }
  renderChatAttachments();
}

function renderChatAttachments() {
  const box = $('#chat-attachments');
  if (!box) return;
  const list = state.chatAttachments;
  if (!list.length) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  box.classList.remove('hidden');
  box.innerHTML = list.map(d => `
    <span class="att-chip" data-att="${d.id}" title="${escapeAttr(d.name)}">
      <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
      <span class="att-name">${escapeHtml(d.name)}</span>
      <button class="att-x" aria-label="Remove" data-remove="${d.id}">×</button>
    </span>`).join('');
  box.querySelectorAll('[data-remove]').forEach(b => b.addEventListener('click', async (e) => {
    e.stopPropagation();
    const id = b.dataset.remove;
    try {
      await api(`/api/documents/${id}`, { method: 'DELETE' });
      await refreshChatAttachments();
    } catch (err) { toast(err.message, 'err'); }
  }));
}

// Mirrors backend rag.extract_text supported types.
const SUPPORTED_EXTS = new Set([
  // Office / documents
  '.pdf', '.docx', '.xlsx', '.xls', '.pptx',
  '.odt', '.ods', '.odp', '.rtf', '.epub',
  // Email
  '.eml', '.msg',
  // Notebooks
  '.ipynb',
  // Web / markup / plain text
  '.html', '.htm', '.xhtml', '.xml',
  '.txt', '.md', '.markdown', '.csv', '.tsv', '.log', '.json',
  '.yaml', '.yml', '.toml', '.ini', '.cfg', '.env', '.sql',
  // Code
  '.py', '.js', '.ts', '.tsx', '.jsx', '.css', '.scss',
  '.rs', '.go', '.java', '.c', '.cc', '.cpp', '.h', '.hpp',
  '.rb', '.php', '.sh', '.bash', '.zsh',
  '.swift', '.kt', '.m', '.mm', '.dart', '.lua', '.r', '.jl',
  '.tex', '.srt', '.vtt',
  // Images (server OCRs them with tesseract)
  '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp',
]);
function fileExt(name) {
  const i = name.lastIndexOf('.');
  return i === -1 ? '' : name.slice(i).toLowerCase();
}
function isHidden(relPath) {
  return relPath.split('/').some(p => p.startsWith('.'));
}

// ---------- Upload progress UI ----------
const upUI = {
  current: null, // active XHR
  cancelled: false,
  show({ title }) {
    this.cancelled = false;
    $('#up-title').textContent = title;
    $('#up-file').textContent = '—';
    $('#up-file-fill').style.width = '0%';
    $('#up-overall-fill').style.width = '0%';
    $('#up-count').textContent = '0 / 0';
    $('#up-rate').textContent = '';
    $('#upload-progress').classList.remove('hidden');
  },
  setFile(name) { $('#up-file').textContent = name; },
  setCount(i, total) { $('#up-count').textContent = `${i} / ${total}`; },
  setOverall(pct) { $('#up-overall-fill').style.width = `${Math.min(100, pct).toFixed(1)}%`; },
  setFile_pct(pct) { $('#up-file-fill').style.width = `${Math.min(100, pct).toFixed(1)}%`; },
  setRate(text) { $('#up-rate').textContent = text || ''; },
  hide() { $('#upload-progress').classList.add('hidden'); this.current = null; },
};
$('#up-cancel')?.addEventListener('click', () => {
  upUI.cancelled = true;
  if (upUI.current) try { upUI.current.abort(); } catch {}
});

function uploadOne(formData, { onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    upUI.current = xhr;
    xhr.open('POST', '/api/documents/upload');
    xhr.withCredentials = true;
    // CSRF guard on the backend requires this header on all mutating
    // requests; the `api()` helper sets it for fetch() calls but XHR
    // uploads have to do it themselves.
    xhr.setRequestHeader('X-Requested-With', 'fetch');
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total);
    };
    xhr.onload = () => {
      upUI.current = null;
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)); }
        catch { resolve({}); }
      } else if (xhr.status === 401) {
        location.replace('/login');
        reject(new Error('unauthorized'));
      } else {
        let detail = `HTTP ${xhr.status}`;
        try { const j = JSON.parse(xhr.responseText); if (j.detail) detail = j.detail; } catch {}
        reject(new Error(detail));
      }
    };
    xhr.onerror = () => { upUI.current = null; reject(new Error('network error')); };
    xhr.onabort = () => { upUI.current = null; reject(new Error('cancelled')); };
    xhr.send(formData);
  });
}

function fmtRate(bytesPerSec) {
  if (!isFinite(bytesPerSec) || bytesPerSec <= 0) return '';
  return `${fmtBytes(bytesPerSec)}/s`;
}

// ---- Upload failure categorization & report dialog ------------------------
function categorizeFailure(msg) {
  const m = (msg || '').toLowerCase();
  // Fixable categories — surface a concrete next step.
  if (m.includes('password-protected') || m.includes('encrypted'))
    return { fixable: true, label: 'Encrypted PDF',
      hint: 'Open in a PDF reader, remove the password ("Print to PDF" works), then re-upload.' };
  if (m.includes('scan') || m.includes('image-only') || m.includes('ocr'))
    return { fixable: true, ocr: true, label: 'Scanned / image-only',
      hint: 'This PDF is a scan with no text layer. Click "OCR & retry" to run server-side OCR (Tesseract) and re-import.' };
  if (m.includes('network') || m.includes('timeout') || m.includes('temporarily') || m.includes('502') || m.includes('503') || m.includes('504'))
    return { fixable: true, retry: true, label: 'Network error',
      hint: 'A transient network problem. Retry should work.' };
  if (m.includes('http 5'))
    return { fixable: true, retry: true, label: 'Server error',
      hint: 'Server hiccup. Retry usually succeeds; if it persists, check the server logs.' };
  if (m.includes('too large') || m.includes('413'))
    return { fixable: true, label: 'File too large',
      hint: 'Split the file or compress it before re-uploading.' };
  if (m.includes('empty'))
    return { fixable: false, label: 'Empty file',
      hint: 'The file has 0 bytes — there\'s nothing to index.' };
  if (m.includes('corrupted') || m.includes('not a valid'))
    return { fixable: false, label: 'Corrupted file',
      hint: 'The file is unreadable. Re-export or re-download the source and try again.' };
  if (m.includes('unsupported file type'))
    return { fixable: true, label: 'Unsupported type',
      hint: 'Convert to PDF, DOCX, TXT, Markdown, CSV, or a code/JSON/YAML/HTML file.' };
  if (m.includes('no readable') || m.includes('no extractable') || m.includes('extract text'))
    return { fixable: true, ocr: true, label: 'No readable text',
      hint: 'Likely scanned or image-based. Click "OCR & retry" to run server-side OCR.' };
  return { fixable: false, label: 'Failed', hint: msg || 'Unknown error.' };
}

function showUploadReport({ total, count, failures, cancelled, skipped }) {
  const dlg = $('#upload-report');
  const body = $('#ur-body');
  const skippedList = skipped || [];
  const summaryParts = [`<strong>Uploaded ${count}/${total}</strong>`];
  if (skippedList.length) summaryParts.push(`${skippedList.length} skipped`);
  if (failures.length) summaryParts.push(`${failures.length} failed`);
  if (cancelled) summaryParts.push('cancelled');
  const items = failures.map(f => {
    const cat = categorizeFailure(f.msg);
    f._cat = cat;
    return `
      <div class="fail-item ${cat.fixable ? 'fixable' : 'unfixable'}">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          ${cat.fixable
            ? '<path d="M3 12a9 9 0 1 0 3-6.7"/><polyline points="3 4 3 10 9 10"/>'
            : '<circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>'}
        </svg>
        <div class="body">
          <div class="name">${escapeHtml(f.rel)}</div>
          <div class="reason">${escapeHtml(f.msg || 'Unknown error')}</div>
          <div class="hint">${escapeHtml(cat.hint)}</div>
        </div>
        <span class="tag">${escapeHtml(cat.label)}</span>
      </div>`;
  }).join('');
  body.innerHTML = `
    <p class="fail-summary">${summaryParts.join(' &middot; ')}</p>
    ${failures.length ? `<div class="fail-list">${items}</div>` : ''}
    ${skippedList.length && !failures.length ? `<p class="fail-summary">${skippedList.length} files were skipped (hidden, system, or unsupported types).</p>` : ''}
  `;
  const retryable = failures.filter(f => f.file && (f._cat.retry || f._cat.fixable));
  const ocrable = failures.filter(f => f.file && f._cat.ocr && /\.pdf$/i.test(f.rel));
  const retryBtn = $('#ur-retry');
  const ocrBtn = $('#ur-ocr');
  retryBtn.hidden = retryable.length === 0;
  retryBtn.textContent = retryable.length ? `Retry ${retryable.length} fixable` : 'Retry fixable';
  retryBtn.onclick = async () => {
    dlg.classList.add('hidden');
    const queue = retryable.map(f => ({ file: f.file, folder: f.folder || '' }));
    const r = await uploadBatch(queue, { title: `Retrying ${queue.length} files` });
    await refreshDocs();
    finishUploadReport({ total: queue.length, count: r.count, failures: r.failures, cancelled: r.cancelled });
  };
  ocrBtn.hidden = ocrable.length === 0;
  ocrBtn.textContent = ocrable.length ? `OCR & retry ${ocrable.length}` : 'OCR & retry';
  ocrBtn.onclick = async () => {
    if (!confirm(`Run server-side OCR on ${ocrable.length} PDF${ocrable.length === 1 ? '' : 's'}? This can take ~1–3 seconds per page.`)) return;
    dlg.classList.add('hidden');
    const queue = ocrable.map(f => ({ file: f.file, folder: f.folder || '' }));
    const r = await uploadBatch(queue, { title: `OCR + uploading ${queue.length} PDF${queue.length === 1 ? '' : 's'}`, ocr: true });
    await refreshDocs();
    finishUploadReport({ total: queue.length, count: r.count, failures: r.failures, cancelled: r.cancelled });
  };
  dlg.classList.remove('hidden');
}

function finishUploadReport({ total, count, failures, cancelled, skipped }) {
  if (failures.length) console.warn('[upload] failures:', failures);
  if (failures.length || (skipped && skipped.length)) {
    showUploadReport({ total, count, failures, cancelled, skipped });
  } else {
    const parts = [`Uploaded ${count}/${total}`];
    if (cancelled) parts.push('cancelled');
    toast(parts.join(' \u2022 '), cancelled ? 'err' : 'ok', 3000);
  }
}

$('#ur-close')?.addEventListener('click', () => $('#upload-report').classList.add('hidden'));
$('#ur-ok')?.addEventListener('click', () => $('#upload-report').classList.add('hidden'));
$('#upload-report')?.addEventListener('click', (e) => {
  if (e.target.id === 'upload-report') $('#upload-report').classList.add('hidden');
});

async function uploadBatch(files, { folderFromRel = false, defaultFolder = '', title, ocr = false, chat_id = null } = {}) {
  // files: [{ file: File, folder: string }]
  const totalBytes = files.reduce((n, x) => n + x.file.size, 0);
  let bytesUploadedBefore = 0;
  const failures = [];
  let count = 0;
  upUI.show({ title: title || `Uploading ${files.length} files` });
  upUI.setCount(0, files.length);
  const t0 = performance.now();
  for (let i = 0; i < files.length; i++) {
    if (upUI.cancelled) break;
    const { file: f, folder } = files[i];
    upUI.setFile((ocr ? '🔍 ' : '') + (f.webkitRelativePath || f.name));
    upUI.setFile_pct(0);
    const fd = new FormData();
    fd.append('file', f);
    if (folder) fd.append('folder', folder);
    if (ocr) fd.append('ocr', 'true');
    if (chat_id) fd.append('chat_id', chat_id);
    let lastLoaded = 0;
    try {
      await uploadOne(fd, {
        onProgress: (loaded, total) => {
          lastLoaded = loaded;
          upUI.setFile_pct((loaded / total) * 100);
          if (totalBytes > 0) upUI.setOverall(((bytesUploadedBefore + loaded) / totalBytes) * 100);
          const elapsed = (performance.now() - t0) / 1000;
          if (elapsed > 0.4) upUI.setRate(fmtRate((bytesUploadedBefore + loaded) / elapsed));
        },
      });
      count++;
      if (folder) state.expandedFolders.add(folder);
    } catch (err) {
      if (err.message === 'cancelled') break;
      // Keep the original File so the user can retry without re-picking.
      failures.push({ rel: f.webkitRelativePath || f.name, msg: err.message, file: f, folder });
    }
    bytesUploadedBefore += f.size;
    upUI.setCount(Math.min(i + 1, files.length), files.length);
    upUI.setOverall(totalBytes > 0 ? (bytesUploadedBefore / totalBytes) * 100 : 0);
  }
  upUI.hide();
  return { count, failures, cancelled: upUI.cancelled };
}

$('#folder-input')?.addEventListener('change', async (e) => {
  const all = [...e.target.files]; e.target.value = '';
  if (!all.length) return;
  const skipped = [];
  const queue = [];
  for (const f of all) {
    const rel = f.webkitRelativePath || f.name;
    if (isHidden(rel)) { skipped.push(rel); continue; }
    if (!SUPPORTED_EXTS.has(fileExt(f.name))) { skipped.push(rel); continue; }
    const folder = rel.includes('/') ? rel.slice(0, rel.lastIndexOf('/')) : '';
    queue.push({ file: f, folder });
  }
  if (!queue.length) {
    toast(`No supported files found (${skipped.length} skipped). Supported: PDF, DOCX, TXT, MD, code, JSON, YAML, HTML, CSV.`, 'err', 6000);
    return;
  }
  const { count, failures, cancelled } = await uploadBatch(queue, { title: `Uploading folder (${queue.length} files)` });
  persistExpanded();
  await refreshDocs();
  finishUploadReport({ total: queue.length, count, failures, cancelled, skipped });
});

$('#add-url').addEventListener('click', async () => {
  const url = prompt('Ingest URL:');
  if (!url) return;
  setStatus(`Fetching ${url}…`, true);
  try {
    const r = await api('/api/documents/url', J({ url }));
    toast(r.status === 'indexing' ? 'Indexing in background…' : `Indexed (${r.chunks} chunks)`, 'ok');
    refreshDocs();
  } catch (err) { toast(err.message, 'err'); }
  setStatus('');
});

// ---------- Models ----------

// ---- Pretty-name helpers --------------------------------------------------
// Turn raw Ollama tags like `qwen2.5-coder:7b-instruct-q4_K_M` into
// human-friendly { name, variant, params, quant, embed } parts that the UI
// can render however it wants.
const _VARIANT_WORDS = new Set([
  // Behaviour / role
  'instruct', 'chat', 'reasoning', 'reason', 'thinking', 'think',
  'coder', 'code', 'codestral', 'vision', 'multimodal', 'mm',
  'tool', 'tools', 'tooluse', 'function', 'functions',
  'guard', 'rerank', 'reranker', 'translate', 'translation',
  'math', 'medical', 'med', 'edu', 'finance', 'legal',
  'audio', 'speech', 'voice', 'video',
  // Tuning lineage
  'uncensored', 'abliterated', 'dpo', 'sft', 'rlhf', 'orpo',
  'dense', 'moe', 'sparse', 'distill', 'distilled', 'preview', 'experimental',
  // NOTE: size adjectives (mini, small, large, etc.) are intentionally NOT
  // listed here — they read more naturally as part of the family name
  // ("Phi 4 Mini" rather than "Phi 4 (mini)").
]);
const _VARIANT_PRETTY = {
  // Render shorter forms; falls back to capitalised word otherwise.
  reasoning: 'reasoning', reason: 'reasoning', thinking: 'thinking', think: 'thinking',
  instruct: 'instruct', chat: 'chat',
  coder: 'code', code: 'code', codestral: 'code',
  vision: 'vision', multimodal: 'multimodal', mm: 'multimodal',
  tool: 'tools', tools: 'tools', tooluse: 'tools', function: 'tools', functions: 'tools',
  guard: 'guard', rerank: 'rerank', reranker: 'rerank',
  translate: 'translation', translation: 'translation',
  math: 'math', medical: 'medical', med: 'medical', edu: 'edu',
  finance: 'finance', legal: 'legal',
  audio: 'audio', speech: 'speech', voice: 'voice', video: 'video',
  uncensored: 'uncensored', abliterated: 'abliterated',
  dpo: 'DPO', sft: 'SFT', rlhf: 'RLHF', orpo: 'ORPO',
  moe: 'MoE', sparse: 'sparse', dense: 'dense',
  distill: 'distilled', distilled: 'distilled',
  preview: 'preview', experimental: 'experimental',
};
// Canonical brand/family capitalisation. Match against the full base path
// first (so "command-r" wins over "command"), then the first chunk.
const _FAMILY_FIXUPS = {
  // Core families
  'llama': 'Llama', 'llama2': 'Llama 2', 'llama3': 'Llama 3', 'llama4': 'Llama 4',
  'llama3.1': 'Llama 3.1', 'llama3.2': 'Llama 3.2', 'llama3.3': 'Llama 3.3',
  'codellama': 'Code Llama', 'tinyllama': 'TinyLlama',
  'mistral': 'Mistral', 'mixtral': 'Mixtral', 'codestral': 'Codestral',
  'mistral-nemo': 'Mistral Nemo', 'mistral-large': 'Mistral Large',
  'mistral-small': 'Mistral Small', 'mathstral': 'Mathstral',
  'ministral': 'Ministral', 'devstral': 'Devstral',
  'phi': 'Phi', 'phi3': 'Phi 3', 'phi4': 'Phi 4', 'phi3.5': 'Phi 3.5',
  'phi3-mini': 'Phi 3 Mini', 'phi3-medium': 'Phi 3 Medium',
  'phi4-mini': 'Phi 4 Mini', 'phi4-mini-reasoning': 'Phi 4 Mini',
  'gemma': 'Gemma', 'gemma2': 'Gemma 2', 'gemma3': 'Gemma 3', 'codegemma': 'CodeGemma',
  'gemma3n': 'Gemma 3n',
  'qwen': 'Qwen', 'qwen2': 'Qwen 2', 'qwen2.5': 'Qwen 2.5', 'qwen3': 'Qwen 3',
  'qwen2.5-coder': 'Qwen 2.5 Coder', 'qwen2-math': 'Qwen 2 Math',
  'qwen2.5-math': 'Qwen 2.5 Math',
  'qwq': 'QwQ', 'qvq': 'QvQ',
  'deepseek': 'DeepSeek', 'deepseek-coder': 'DeepSeek Coder',
  'deepseek-coder-v2': 'DeepSeek Coder V2', 'deepseek-llm': 'DeepSeek LLM',
  'deepseek-r1': 'DeepSeek R1', 'deepseek-v2': 'DeepSeek V2',
  'deepseek-v3': 'DeepSeek V3', 'deepseek-math': 'DeepSeek Math',
  // IBM / Cohere / others
  'granite': 'Granite', 'granite-code': 'Granite Code',
  'granite3-dense': 'Granite 3 Dense', 'granite3-moe': 'Granite 3 MoE',
  'granite3.1-dense': 'Granite 3.1', 'granite3.2': 'Granite 3.2',
  'granite3.3': 'Granite 3.3', 'granite-embedding': 'Granite Embed',
  'command-r': 'Command R', 'command-r-plus': 'Command R+',
  'commandr': 'Command R', 'commandr-plus': 'Command R+',
  'aya': 'Aya', 'aya-expanse': 'Aya Expanse',
  // Chinese / EU labs
  'yi': 'Yi', 'yi-coder': 'Yi Coder', 'internlm': 'InternLM',
  'internlm2': 'InternLM 2', 'glm4': 'GLM 4', 'glm-4': 'GLM 4',
  'chatglm': 'ChatGLM', 'falcon': 'Falcon', 'falcon3': 'Falcon 3',
  'falcon-mamba': 'Falcon Mamba', 'mamba': 'Mamba',
  'jamba': 'Jamba', 'reka': 'Reka', 'arcee': 'Arcee', 'kimi': 'Kimi',
  'baichuan': 'Baichuan', 'minicpm': 'MiniCPM', 'orion': 'Orion',
  // Community / finetunes
  'nous-hermes': 'Nous Hermes', 'nous-hermes2': 'Nous Hermes 2',
  'hermes': 'Hermes', 'hermes3': 'Hermes 3',
  'wizardlm': 'WizardLM', 'wizardlm2': 'WizardLM 2',
  'wizard-math': 'WizardMath', 'wizard-vicuna': 'Wizard Vicuna',
  'wizardcoder': 'WizardCoder',
  'openchat': 'OpenChat', 'openhermes': 'OpenHermes',
  'orca': 'Orca', 'orca-mini': 'Orca Mini', 'orca2': 'Orca 2',
  'dolphin': 'Dolphin', 'dolphin-mixtral': 'Dolphin Mixtral',
  'dolphin-phi': 'Dolphin Phi', 'dolphin-llama3': 'Dolphin Llama 3',
  'dolphin3': 'Dolphin 3', 'dolphin-mistral': 'Dolphin Mistral',
  'samantha-mistral': 'Samantha Mistral', 'samantha': 'Samantha',
  'vicuna': 'Vicuna', 'alpaca': 'Alpaca',
  'solar': 'Solar', 'solar-pro': 'Solar Pro',
  'sailor2': 'Sailor 2', 'smollm': 'SmolLM', 'smollm2': 'SmolLM 2',
  'tinydolphin': 'TinyDolphin', 'tinyllava': 'TinyLLaVA',
  // Vision / multimodal
  'llava': 'LLaVA', 'llava-llama3': 'LLaVA Llama 3', 'llava-phi3': 'LLaVA Phi 3',
  'bakllava': 'BakLLaVA', 'moondream': 'Moondream', 'minicpm-v': 'MiniCPM-V',
  'pixtral': 'Pixtral', 'cogvlm': 'CogVLM',
  // Code
  'starcoder': 'StarCoder', 'starcoder2': 'StarCoder 2', 'stable-code': 'Stable Code',
  'deepcoder': 'DeepCoder', 'opencoder': 'OpenCoder', 'sqlcoder': 'SQLCoder',
  'magicoder': 'Magicoder',
  // Embeddings / reranking
  'nomic': 'Nomic', 'nomic-embed-text': 'Nomic Embed',
  'mxbai-embed-large': 'MixedBread Embed', 'mxbai': 'MixedBread',
  'snowflake-arctic-embed': 'Snowflake Arctic Embed',
  'snowflake-arctic-embed2': 'Snowflake Arctic Embed 2',
  'all-minilm': 'all-MiniLM', 'paraphrase-multilingual': 'Paraphrase Multilingual',
  'bge-large': 'BGE Large', 'bge-m3': 'BGE-M3',
  'jina': 'Jina', 'jina-embeddings-v2': 'Jina v2',
  // Safety / classifiers
  'llama-guard3': 'Llama Guard 3', 'shieldgemma': 'ShieldGemma',
  'prompt-guard': 'Prompt Guard',
  // Misc
  'nemotron': 'Nemotron', 'nemotron-mini': 'Nemotron Mini',
  'tulu3': 'Tulu 3', 'olmo': 'OLMo', 'olmo2': 'OLMo 2',
  'tablellama': 'TableLlama', 'sea-lion': 'SEA-LION',
};

function _normFamilyKey(s) { return (s || '').toLowerCase().replace(/_/g, '-'); }

function _titleFamily(word) {
  if (!word) return '';
  const lower = _normFamilyKey(word);
  if (_FAMILY_FIXUPS[lower]) return _FAMILY_FIXUPS[lower];
  // Split into letter+digit chunks: "qwen2.5" -> ["qwen", "2.5"], "phi4" -> ["phi", "4"].
  const m = lower.match(/^([a-z]+)([\d.]+.*)?$/);
  if (m) {
    const head = (_FAMILY_FIXUPS[m[1]] || (m[1][0].toUpperCase() + m[1].slice(1)));
    return m[2] ? `${head} ${m[2]}` : head;
  }
  // Hyphenated unknown brand: title-case each chunk.
  return lower.split('-').map(c => c ? c[0].toUpperCase() + c.slice(1) : c).join(' ');
}
function formatModelName(raw, opts = {}) {
  if (!raw) return { name: '', variant: '', params: '', quant: '', embed: false, raw: '' };
  // Strip optional registry prefix (e.g. "hf.co/user/model" or "user/model")
  // so unknown namespaces don't crowd the label. The raw tag is preserved in
  // the tooltip for power users.
  let work = raw;
  if (work.includes('/')) work = work.split('/').slice(-1)[0];
  const [base, tag = ''] = work.split(':');

  // Try the full base as a fixup key first (handles "command-r", "deepseek-coder", …).
  const fullKey = _normFamilyKey(base);
  let headName = null;
  let headEnd = 0;
  if (_FAMILY_FIXUPS[fullKey]) {
    headName = _FAMILY_FIXUPS[fullKey];
    headEnd = base.split('-').length;
  } else {
    // Try progressively shorter prefixes: "dolphin-mixtral-8x7b" -> "dolphin-mixtral" -> "dolphin".
    const parts = base.split('-');
    for (let n = parts.length; n >= 1; n--) {
      const k = _normFamilyKey(parts.slice(0, n).join('-'));
      if (_FAMILY_FIXUPS[k]) { headName = _FAMILY_FIXUPS[k]; headEnd = n; break; }
    }
  }

  const baseParts = base.split('-');
  let variantParts = [];
  let extraNameParts = [];   // unknown post-head tokens that aren't sizes/variants
  if (headName !== null) {
    for (const p of baseParts.slice(headEnd)) {
      const lp = p.toLowerCase();
      if (_VARIANT_WORDS.has(lp)) variantParts.push(lp);
      else if (/^\d/.test(lp)) {/* embedded size, ignore here */}
      else extraNameParts.push(p);
    }
  } else {
    // Fallback: original heuristic — walk from the left taking chunks until a
    // variant or numeric size appears.
    let i = 0;
    const headParts = [];
    while (i < baseParts.length) {
      const p = baseParts[i].toLowerCase();
      if (_VARIANT_WORDS.has(p)) break;
      if (/^\d/.test(p)) break;
      headParts.push(baseParts[i]); i++;
    }
    if (!headParts.length) headParts.push(baseParts[0]);
    variantParts = baseParts.slice(i).filter(p => _VARIANT_WORDS.has(p.toLowerCase()));
    headName = headParts.map(_titleFamily).join(' ');
  }

  // Tag is e.g. "7b-instruct-q4_K_M", "latest", "3.8b-q8_0", "8x7b", "v0.3-q4_K_M".
  let params = '', quant = '';
  const variantFromTag = [];
  if (tag && tag !== 'latest') {
    for (const piece of tag.split('-')) {
      const lp = piece.toLowerCase();
      if (/^\d+x\d+(\.\d+)?[mb]$/.test(lp)) {
        // MoE shape: "8x7b" -> "8x7B"
        params = lp.replace(/[mb]$/, m => m.toUpperCase());
      } else if (/^\d+(\.\d+)?[mb]$/.test(lp)) {
        params = lp.toUpperCase();        // "7b" -> "7B", "1.7b" -> "1.7B"
      } else if (/^q\d/.test(lp)) {
        const m = lp.match(/^q(\d+)/);
        quant = m ? `Q${m[1]}` : lp.toUpperCase();
      } else if (/^iq\d/.test(lp)) {
        // Importance-matrix quants: iq4_xs etc.
        const m = lp.match(/^iq(\d+)/);
        quant = m ? `IQ${m[1]}` : lp.toUpperCase();
      } else if (/^(fp|f|bf)\d+$/.test(lp)) {
        quant = lp.toUpperCase();          // FP16, BF16
      } else if (_VARIANT_WORDS.has(lp)) {
        variantFromTag.push(lp);
      }
      // Drop: date stamps (4-8 digits), version stamps (v0.3), build hashes.
    }
  }

  const name = (headName + (extraNameParts.length ? ' ' + extraNameParts.join(' ') : '')).trim();
  const allVariants = [...variantParts, ...variantFromTag]
    .map(v => v.toLowerCase())
    .filter((v, idx, arr) => arr.indexOf(v) === idx)
    .map(v => _VARIANT_PRETTY[v] || v);
  // Dedupe again after prettifying ("reasoning" + "reason" both map to "reasoning").
  const variant = [...new Set(allVariants)].join(', ');
  const embed = /embed|rerank/i.test(base);
  return { name, variant, params, quant, embed, raw };
}
function modelLabel(raw) {
  // Single-line label for compact contexts (dropdowns, citations).
  const f = formatModelName(raw);
  const bits = [];
  if (f.params) bits.push(f.params);
  if (f.quant) bits.push(f.quant);
  if (f.embed && !bits.length) bits.push('embed');
  const suffix = bits.length ? ` \u00b7 ${bits.join(' \u00b7 ')}` : '';
  const variant = f.variant ? ` (${f.variant})` : '';
  return `${f.name}${variant}${suffix}`;
}

// Extract vendor/organization from model name
function getModelVendor(modelName) {
  const name = (modelName || '').toLowerCase();
  // Model names often have format: vendor/model:tag or vendor-model or just model
  // Extract the vendor part
  if (name.includes('/')) return name.split('/')[0];  // ollama registry: e.g. "meta/llama2"
  if (name.includes('llama')) return 'Meta';
  if (name.includes('mistral')) return 'Mistral';
  if (name.includes('gemma')) return 'Google';
  if (name.includes('deepseek')) return 'DeepSeek';
  if (name.includes('qwen')) return 'Alibaba';
  if (name.includes('phi')) return 'Microsoft';
  if (name.includes('neural-chat') || name.includes('intel')) return 'Intel';
  if (name.includes('orca')) return 'Microsoft';
  if (name.includes('starling')) return 'Berkeley';
  if (name.includes('dolphin') || name.includes('hermes')) return 'Nous Research';
  if (name.includes('neural')) return 'Intel';
  if (name.includes('vicuna') || name.includes('lmsys')) return 'LMSYS';
  if (name.includes('openchat')) return 'OpenChat';
  if (name.includes('zephyr')) return 'Hugging Face';
  if (name.includes('code')) return 'Code Models';
  return 'Other';
}

async function refreshModels() {
  const { models, default: def } = await api('/api/models');
  state.models = models;
  const chatModels = models.filter(m => m.kind !== 'embed').sort((a, b) => a.name.localeCompare(b.name));
  
  // Group models by vendor
  const grouped = {};
  chatModels.forEach(m => {
    const vendor = getModelVendor(m.name);
    if (!grouped[vendor]) grouped[vendor] = [];
    grouped[vendor].push(m);
  });
  
  // Sort vendors alphabetically, but put "Other" at the end
  const vendors = Object.keys(grouped).sort((a, b) => {
    if (a === 'Other') return 1;
    if (b === 'Other') return -1;
    return a.localeCompare(b);
  });
  
  const sel = $('#model-select');
  sel.innerHTML = vendors.map(vendor => {
    const vendorModels = grouped[vendor];
    return `<optgroup label="${escapeHtml(vendor)}">
      ${vendorModels.map(m =>
        `<option value="${escapeAttr(m.name)}" title="${escapeAttr(m.name)}">${escapeHtml(modelLabel(m.name))}</option>`
      ).join('')}
    </optgroup>`;
  }).join('');
  
  // Preserve the per-chat selection across model-list refreshes (e.g. after a
  // pull or delete). Only fall back to the global default when the current
  // selection isn't available anymore or hasn't been set yet.
  const saved = state.settings.default_model;
  const cur = state.currentModel;
  const hasCur = cur && chatModels.some(m => m.name === cur);
  if (!hasCur) {
    state.currentModel = (saved && chatModels.find(m => m.name === saved)) ? saved
      : (chatModels.find(m => m.name === def) ? def : (chatModels[0]?.name || def));
  }
  sel.value = state.currentModel;
  _refreshThinkChip();
}

$('#model-select').addEventListener('change', e => {
  state.currentModel = e.target.value;
  _refreshThinkChip();
});

$('#pull-model').addEventListener('click', () => {
  if (!state.user.is_admin) { toast('Admin only', 'err'); return; }
  openPullDialog();
});

// In-flight pull state. Keyed by model name so the user can browse other
// dialogs without losing the progress UI; only one pull at a time though.
const _activePull = { controller: null, name: null };

function openPullDialog(prefill = '') {
  document.getElementById('pull-dialog')?.remove();
  const ov = document.createElement('div');
  ov.id = 'pull-dialog';
  ov.className = 'ref-picker-overlay';
  ov.innerHTML = `
    <div class="ref-picker-panel" role="dialog" aria-modal="true" aria-label="Pull model" style="width:min(560px,100%);">
      <div class="ref-picker-head">
        <strong>Pull an Ollama model</strong>
        <button type="button" class="icon-btn" id="pd-close" aria-label="Close">✕</button>
      </div>
      <div style="display:flex; gap:var(--sp-2);">
        <input class="field" id="pd-name" placeholder="llama3.2, qwen2.5:7b, nomic-embed-text" style="flex:1;" value="${escapeAttr(prefill)}">
        <button class="btn" id="pd-details" type="button">Details</button>
      </div>
      <div id="pd-info" class="pd-info"></div>
      <div id="pd-progress" class="pd-progress hidden">
        <div class="pd-bar"><div class="pd-bar-fill" id="pd-bar-fill"></div></div>
        <div class="pd-status" id="pd-status"></div>
      </div>
      <div class="ref-picker-actions">
        <button type="button" class="btn btn-text" id="pd-cancel" disabled>Cancel</button>
        <button type="button" class="btn btn-filled" id="pd-pull">Pull</button>
      </div>
    </div>`;
  document.body.appendChild(ov);
  const close = () => {
    if (_activePull.controller) {
      if (!confirm('A pull is in progress. Cancel it and close?')) return;
      _activePull.controller.abort();
    }
    ov.remove();
  };
  ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
  $('#pd-close', ov).addEventListener('click', close);
  $('#pd-details', ov).addEventListener('click', () => loadModelDetails($('#pd-name', ov).value.trim()));
  $('#pd-pull', ov).addEventListener('click', () => startPull($('#pd-name', ov).value.trim()));
  $('#pd-cancel', ov).addEventListener('click', () => _activePull.controller?.abort());
  $('#pd-name', ov).addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); loadModelDetails(e.target.value.trim()); }
  });
  setTimeout(() => $('#pd-name', ov).focus(), 0);
}

async function loadModelDetails(name) {
  if (!name) return;
  const info = $('#pd-info');
  info.innerHTML = `<div class="muted">Loading details for <code>${escapeHtml(name)}</code>…</div>`;
  try {
    const d = await api(`/api/models/info?name=${encodeURIComponent(name)}`);
    info.innerHTML = renderModelDetails(d);
  } catch (err) {
    info.innerHTML = `<div class="muted err">${escapeHtml(err.message)}</div>`;
  }
}

function renderModelDetails(d) {
  const rows = [];
  const local = d.local || null;
  const remote = d.remote || null;
  if (local) {
    rows.push(['Status', '<span class="chip on" style="display:inline-block;padding:2px 8px;border-radius:8px;background:var(--primary-container);color:var(--on-primary-container);font:var(--t-micro);">Installed</span>']);
    const det = local.details || {};
    if (det.family) rows.push(['Family', escapeHtml(det.family)]);
    if (det.parameter_size) rows.push(['Parameters', escapeHtml(det.parameter_size)]);
    if (det.quantization_level) rows.push(['Quantization', escapeHtml(det.quantization_level)]);
    if (det.format) rows.push(['Format', escapeHtml(det.format)]);
    if (local.size) rows.push(['On-disk size', fmtBytes(local.size)]);
    if (local.modified_at) rows.push(['Modified', escapeHtml(local.modified_at)]);
    // Model info: context length, embedding length, etc.
    const mi = local.model_info || {};
    for (const [k, v] of Object.entries(mi)) {
      if (typeof v === 'number' && /context_length|embedding_length|attention\.head_count/i.test(k)) {
        rows.push([escapeHtml(k), String(v)]);
      }
    }
    if (local.license) {
      const lic = String(local.license).split('\n').slice(0, 1)[0] || 'see model card';
      rows.push(['License', escapeHtml(lic.slice(0, 120))]);
    }
  }
  if (remote && remote.total_size) {
    rows.push(['Download size', fmtBytes(remote.total_size)]);
  }
  if (remote && remote.error && !local) {
    rows.push(['Registry', `<span class="err">${escapeHtml(remote.error)}</span>`]);
  }
  if (!rows.length) return `<div class="muted">No info available for <code>${escapeHtml(d.name)}</code>.</div>`;
  return `<table class="pd-table">${
    rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join('')
  }</table>`;
}

async function startPull(name) {
  if (!name) { toast('Enter a model name', 'err'); return; }
  if (_activePull.controller) { toast('A pull is already running', 'err'); return; }
  const prog = $('#pd-progress'); const fill = $('#pd-bar-fill');
  const stat = $('#pd-status'); const cancelBtn = $('#pd-cancel');
  const pullBtn = $('#pd-pull');
  prog.classList.remove('hidden');
  fill.style.width = '0%';
  stat.textContent = 'Connecting…';
  cancelBtn.disabled = false;
  pullBtn.disabled = true;
  const controller = new AbortController();
  _activePull.controller = controller; _activePull.name = name;
  setStatus(`Pulling ${name}…`, true);
  try {
    const r = await fetch('/api/models/pull', {
      method: 'POST', signal: controller.signal,
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
      body: JSON.stringify({ name }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n'); buf = lines.pop() || '';
      for (const l of lines) {
        if (!l.trim()) continue;
        let o; try { o = JSON.parse(l); } catch { continue; }
        const status = o.status || '';
        if (typeof o.total === 'number' && typeof o.completed === 'number' && o.total > 0) {
          const pct = Math.max(0, Math.min(100, (o.completed / o.total) * 100));
          fill.style.width = pct.toFixed(1) + '%';
          stat.textContent = `${status} — ${fmtBytes(o.completed)} / ${fmtBytes(o.total)} (${pct.toFixed(0)}%)`;
        } else {
          stat.textContent = status;
        }
        setStatus(`Pulling ${name}: ${status}`, true);
      }
    }
    fill.style.width = '100%';
    stat.textContent = 'Done.';
    toast(`Pulled ${name}`, 'ok');
    await refreshModels();
    // Pull succeeded — swap the action buttons into a single "Done" that
    // just closes the dialog. Covers both button slots (Cancel + Pull).
    pullBtn.classList.add('hidden');
    cancelBtn.textContent = 'Done';
    cancelBtn.classList.remove('btn-text');
    cancelBtn.classList.add('btn-filled');
    cancelBtn.disabled = false;
    cancelBtn.onclick = () => document.getElementById('pull-dialog')?.remove();
    // If the settings dialog is open on the models tab, refresh it.
    if (!$('#modal').classList.contains('hidden')) {
      try { renderSettingsBody('models'); } catch {}
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      stat.textContent = 'Cancelled.';
      toast('Pull cancelled', 'info');
    } else {
      stat.textContent = `Error: ${err.message}`;
      toast(err.message, 'err');
    }
    // Failure path: re-enable retry, keep Cancel as-is.
    cancelBtn.disabled = true;
    pullBtn.disabled = false;
  } finally {
    _activePull.controller = null; _activePull.name = null;
    setStatus('');
  }
}

// Legacy helper, still used by the settings tab's quick-pull input.
async function pullModel(name) { openPullDialog(name); }

function openCreateUserDialog() {
  document.getElementById('create-user-modal')?.remove();
  const ov = document.createElement('div');
  ov.id = 'create-user-modal';
  ov.className = 'ref-picker-overlay';
  ov.innerHTML = `
    <div class="ref-picker-panel" role="dialog" aria-modal="true" aria-label="Create user" style="width:min(480px,100%);">
      <div class="ref-picker-head">
        <strong>Create user account</strong>
        <button type="button" class="icon-btn" id="cu-close" aria-label="Close">✕</button>
      </div>
      <form id="cu-form" style="display:flex; flex-direction:column; gap:var(--sp-3);">
        <label style="display:flex; flex-direction:column; gap:var(--sp-1);">
          <span style="font:var(--t-caption-b);">Username</span>
          <input class="field" id="cu-username" type="text" required placeholder="e.g. john.doe" />
        </label>
        <label style="display:flex; flex-direction:column; gap:var(--sp-1);">
          <span style="font:var(--t-caption-b);">Email</span>
          <input class="field" id="cu-email" type="email" required placeholder="user@example.com" />
        </label>
        <label style="display:flex; flex-direction:column; gap:var(--sp-1);">
          <span style="font:var(--t-caption-b);">Display name (optional)</span>
          <input class="field" id="cu-display-name" type="text" placeholder="e.g. John Doe" />
        </label>
        <label style="display:flex; flex-direction:column; gap:var(--sp-1);">
          <span style="font:var(--t-caption-b);">Password</span>
          <input class="field" id="cu-password" type="password" required minlength="6" placeholder="Min 6 characters" />
        </label>
        <div id="cu-error" class="auth-error hidden" style="margin:0;"></div>
      </form>
      <div class="ref-picker-actions">
        <button type="button" class="btn btn-text" id="cu-cancel">Cancel</button>
        <button type="button" class="btn btn-filled" id="cu-create">Create</button>
      </div>
    </div>`;
  document.body.appendChild(ov);
  const close = () => ov.remove();
  const errorDiv = $('#cu-error', ov);
  
  ov.addEventListener('click', (e) => { if (e.target === ov) close(); });
  $('#cu-close', ov).addEventListener('click', close);
  $('#cu-cancel', ov).addEventListener('click', close);
  
  $('#cu-create', ov).addEventListener('click', async () => {
    const username = $('#cu-username', ov).value.trim();
    const email = $('#cu-email', ov).value.trim();
    const displayName = $('#cu-display-name', ov).value.trim();
    const password = $('#cu-password', ov).value;
    
    if (!username || !email || !password) {
      errorDiv.textContent = 'All fields required (except display name)';
      errorDiv.classList.remove('hidden');
      return;
    }
    
    const btn = $('#cu-create', ov);
    btn.disabled = true;
    const orig = btn.textContent;
    btn.innerHTML = '<span class="spinner"></span>';
    
    try {
      await api('/api/admin/users', {
        method: 'POST',
        body: JSON.stringify({
          username,
          email,
          display_name: displayName || null,
          password,
        }),
      });
      toast(`Created user ${username}`, 'ok');
      close();
      renderSettingsBody('users');
    } catch (err) {
      errorDiv.textContent = err.message;
      errorDiv.classList.remove('hidden');
      btn.disabled = false;
      btn.textContent = orig;
    }
  });
  
  setTimeout(() => $('#cu-username', ov).focus(), 0);
}

// ---------- Welcome ----------
const SUGGESTIONS = [
  // { title: 'Summarize a document', desc: 'Distill your uploads', text: 'Summarize my most recent uploaded document in 5 bullet points.' },
  // { title: 'Draft a plan',         desc: 'Strategic outline',    text: 'Draft a 30-day plan to learn and ship a small Rust project.' },
  // { title: 'Search the web',       desc: 'Live information',     text: "What's the most important AI news this week? Use the web." },
  // { title: 'Write code',           desc: 'Practical snippets',   text: 'Write a Python script to deduplicate rows in a CSV by an email column.' },
];
function renderEmpty() {
  const name = state.user?.display_name || state.user?.username || '';
  const rawTitle = state.brand?.ui?.empty_state_title || 'How can I help';
  // If the brand title ends with terminal punctuation (e.g. "Greetings!"),
  // splice the name in before that punctuation as a vocative: "Greetings, Name!".
  // Otherwise ("How can I help"), append the name with a comma and end with "?".
  const trimmed = rawTitle.trim();
  const m = trimmed.match(/^(.*?)([!.?]+)$/);
  const headline = m
    ? (name ? `${escapeHtml(m[1])}, ${escapeHtml(name)}${escapeHtml(m[2])}` : escapeHtml(trimmed))
    : (name ? `${escapeHtml(trimmed)}, ${escapeHtml(name)}?` : `${escapeHtml(trimmed)}?`);
  const capsRaw = state.brand?.ui?.empty_state_capabilities || '';
  const caps = capsRaw.split('|').map(s => s.trim()).filter(Boolean);
  const eyebrowHtml = caps.length
    ? `<div class="welcome-eyebrow">${caps.map(c => `<span>${escapeHtml(c)}</span>`).join('<i></i>')}</div>`
    : '';
  $('#messages').innerHTML = `
    <div class="welcome"><div class="welcome-inner">
      <div class="welcome-mark">${ASSISTANT_ICON}</div>
      ${eyebrowHtml}
      <h1>${headline}</h1>
      <div class="suggestions">
        ${SUGGESTIONS.map(s => `
          <button class="suggestion" data-text="${escapeAttr(s.text)}">
            <div class="title">${escapeHtml(s.title)}</div>
            <div class="desc">${escapeHtml(s.desc)}</div>
          </button>`).join('')}
      </div>
    </div></div>`;
  $$('.suggestion').forEach(b => b.addEventListener('click', () => {
    $('#input').value = b.dataset.text;
    $('#input').dispatchEvent(new Event('input'));
    send();
  }));
}

function renderMessages(messages) {
  const root = $('#messages');
  if (!messages.length) { renderEmpty(); return; }
  root.innerHTML = `<div class="messages-inner"></div>`;
  const inner = $('.messages-inner', root);
  for (const m of messages) {
    inner.appendChild(renderMessage(m.role, m.content, m.meta?.citations || [],
      { tokens_in: m.tokens_in, tokens_out: m.tokens_out, duration_ms: m.duration_ms, model: m.model },
      m.meta?.thinking || ''));
  }
  scrollToBottom();
}
function ensureInner() {
  let inner = $('.messages-inner', $('#messages'));
  if (!inner) {
    $('#messages').innerHTML = `<div class="messages-inner"></div>`;
    inner = $('.messages-inner', $('#messages'));
  }
  return inner;
}
function userAvatar(extraClass = '') {
  const u = state.user;
  if (!u) return `<div class="avatar u ${extraClass}">?</div>`;
  if (u.avatar) {
    return `<div class="avatar u ${extraClass}"><img alt="" src="/api/settings/avatar/${u.id}?v=${encodeURIComponent(u.avatar)}"></div>`;
  }
  const name = u.display_name || u.username || '?';
  const initial = (name[0] || '?').toUpperCase();
  return `<div class="avatar u ${extraClass}">${escapeHtml(initial)}</div>`;
}

function renderMessage(role, content, citations = [], stats = {}, thinking = '') {
  const row = document.createElement('div');
  row.className = `msg ${role}`;
  if (role === 'assistant') {
    const merged = thinking ? `<think>${thinking}</think>\n\n${content}` : content;
    row.innerHTML = `
      ${assistantAvatar()}
      <div class="body">
        <div class="prose"></div>
        <div class="actions"></div>
      </div>`;
    const body = row.querySelector('.prose');
    body.innerHTML = renderMarkdown(merged);
    addCopyButtons(body);
    wireCitations(body, citations);
    if (citations.length) row.querySelector('.body').insertBefore(buildCitationsEl(citations), row.querySelector('.actions'));
    row.querySelector('.actions').appendChild(buildAssistantActions(content, stats));
  } else {
    row.innerHTML = `<div class="bubble"></div>${userAvatar()}`;
    row.querySelector('.bubble').textContent = content;
  }
  return row;
}
function buildCitationsEl(cites) {
  const el = document.createElement('div');
  el.className = 'citations';
  el.innerHTML = cites.map(c => {
    if (c.type === 'web')
      return `<div class="citation"><span class="n">${c.n}</span><a href="${escapeAttr(c.url)}" target="_blank" rel="noopener">🌐 ${escapeHtml(c.title || c.url)}</a></div>`;
    if (c.type === 'doc')
      return `<div class="citation"><span class="n">${c.n}</span><span>📄 ${escapeHtml(c.name || 'document')}</span></div>`;
    if (c.type === 'chat')
      return `<div class="citation"><span class="n">${c.n}</span><span>💬 ${escapeHtml(c.title || 'chat')}</span></div>`;
    return `<div class="citation warning"><span>⚠</span><span>${escapeHtml(c.title || '')}${c.note ? ' — ' + escapeHtml(c.note) : ''}</span></div>`;
  }).join('');
  return el;
}

// Attach tooltip + click behaviour to every inline `[n]` citation marker that
// `renderMarkdown` produced. Safe to call multiple times — it short-circuits
// on already-wired anchors.
function wireCitations(root, cites) {
  if (!root || !cites || !cites.length) return;
  const byN = new Map(cites.map(c => [String(c.n), c]));
  root.querySelectorAll('a.cite[data-cite]').forEach(a => {
    if (a.dataset.wired === '1') return;
    const c = byN.get(a.dataset.cite);
    if (!c) return;
    a.dataset.wired = '1';
    const label =
      c.type === 'web'  ? `${c.title || c.url}\n${c.url || ''}` :
      c.type === 'doc'  ? `📄 ${c.name || 'document'}` :
      c.type === 'chat' ? `💬 ${c.title || 'chat'}` :
                          (c.title || '');
    a.title = label.trim();
    if (c.type === 'web' && c.url) {
      a.setAttribute('href', c.url);
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
    } else {
      a.setAttribute('role', 'button');
      a.addEventListener('click', (e) => {
        e.preventDefault();
        // Find the matching entry in the citations footer and flash it.
        const block = a.closest('.body')?.querySelector('.citations');
        const target = [...(block?.querySelectorAll('.citation .n') || [])]
          .find(n => n.textContent.trim() === String(c.n))?.parentElement;
        if (target) {
          target.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          target.classList.add('flash');
          setTimeout(() => target.classList.remove('flash'), 1200);
        }
        if (c.type === 'chat' && c.chat_id) openChat(c.chat_id);
      });
    }
  });
}
function buildAssistantActions(content, stats) {
  const wrap = document.createElement('div');
  wrap.style.display = 'contents';
  const prettyModel = stats.model ? ` \u00b7 ${escapeHtml(modelLabel(stats.model))}` : '';
  const modelTip = stats.model ? ` title="${escapeAttr(stats.model)}"` : '';
  const statText = stats.tokens_out
    ? `${stats.tokens_in || 0}\u2192${stats.tokens_out} tok \u00b7 ${((stats.duration_ms || 0) / 1000).toFixed(1)}s${prettyModel}`
    : '';
  wrap.innerHTML = `
    <button class="icon-btn" data-act="copy" title="Copy"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
    <button class="icon-btn" data-act="regen" title="Regenerate"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg></button>
    ${statText ? `<span class="stats"${modelTip}>${statText}</span>` : ''}`;
  wrap.querySelector('[data-act="copy"]').addEventListener('click', () => {
    navigator.clipboard.writeText(content); toast('Copied', 'ok');
  });
  wrap.querySelector('[data-act="regen"]').addEventListener('click', () => {
    const active = state.streams.get(state.currentChatId);
    if (!state.lastUserMessage || (active && !active.done)) return;
    $('#input').value = state.lastUserMessage;
    $('#input').dispatchEvent(new Event('input'));
    send();
  });
  return wrap;
}

// ---------- Composer ----------
const input = $('#input');
input.addEventListener('input', () => {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 240) + 'px';
  _refreshComposerUI();
  $('#counter').textContent = input.value.length ? `${input.value.length}` : '';
});
input.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
$('#send').addEventListener('click', send);
$('#stop').addEventListener('click', () => {
  const s = state.streams.get(state.currentChatId);
  s?.controller?.abort();
});

// Render the in-progress stream content into a freshly opened DOM (used when
// switching back to a chat that's still streaming).
function _renderStreamInto(stream, body) {
  body.innerHTML = renderMarkdown(stream.thinking
    ? `<think>${stream.thinking}</think>\n\n${stream.full}`
    : stream.full);
  addCopyButtons(body);
  wireCitations(body, stream.citations);
  // Honour the stream's current think-panel state and remember user toggles
  // (the DOM is re-rendered on every token, so we have to re-bind each time).
  body.querySelectorAll('details.thinking').forEach(d => {
    d.open = stream.thinkingOpen !== false;
    d.addEventListener('toggle', () => { stream.thinkingOpen = d.open; });
  });
}

let _newChatCounter = 0;

// ---- Live status line shown above the assistant message while context
// (web search / RAG / classifier) is being built. The line is replaced on
// every status event and disappears as soon as token generation starts.
function _clearStatus(stream) {
  stream.statusEl?.remove();
  stream.statusEl = null;
}
// Render a "(query: …)" suffix for tool-call status labels when the call
// args include a query string. Returns "" when the args don't contain
// something sensible to surface.
function _toolQ(args) {
  if (!args) return '';
  let a = args;
  if (typeof a === 'string') {
    try { a = JSON.parse(a); } catch { return ''; }
  }
  const q = a && (a.query || a.q || a.text);
  return q ? ` \u201C${String(q).slice(0, 80)}\u201D` : '';
}
// Pinned warning banner shown beneath the streaming answer (e.g. when the
// grounding verifier flagged an unsupported claim). Idempotent: repeated
// calls update in place rather than stacking.
function _renderWarning(stream, payload) {
  const body = stream.aRow?.querySelector('.body');
  if (!body) return;
  let el = stream.warnEl;
  if (!el) {
    el = document.createElement('div');
    el.className = 'answer-warning';
    body.appendChild(el);
    stream.warnEl = el;
  }
  el.innerHTML = '';
  const icon = document.createElement('span');
  icon.className = 'warn-icon';
  icon.textContent = '\u26A0';
  const txt = document.createElement('span');
  txt.className = 'warn-text';
  const title = document.createElement('strong');
  title.textContent = (payload.title || 'Warning') + ': ';
  txt.appendChild(title);
  txt.appendChild(document.createTextNode(payload.note || ''));
  el.appendChild(icon);
  el.appendChild(txt);
}
function _renderStatus(stream, payload, originalMsg) {
  const body = stream.aRow?.querySelector('.body');
  if (!body) return;
  // Lazy-create the status row above the .prose content.
  if (!stream.statusEl) {
    const el = document.createElement('div');
    el.className = 'live-status';
    body.insertBefore(el, body.firstChild);
    stream.statusEl = el;
  }
  // Phase = 'generating' means context build finished and we're now
  // streaming tokens — drop the status line entirely.
  if (payload.phase === 'generating') { _clearStatus(stream); return; }
  const phase = payload.phase || '';
  const labels = {
    classifying: 'Deciding whether to search the web…',
    rewriting: payload.for === 'documents' ? 'Rewriting query for documents…' : 'Rewriting query for the web…',
    rag: `Searching documents…`,
    searching: `Searching the web…`,
    fetched: `Read ${payload.count} sources.`,
    reranking: `Ranking ${payload.count || ''} sources by relevance…`.replace(/\s+/g, ' '),
    disagreement: 'Sources disagree on a key fact.',
    skipped: 'Skipped web search (answerable from context).',
    thinking_tools: `Deciding what tools to use${payload.round > 1 ? ` (round ${payload.round})` : ''}…`,
    tool_call: payload.name === 'web_search'
      ? `Calling web_search${_toolQ(payload.args)}…`
      : payload.name === 'search_documents'
        ? `Calling search_documents${_toolQ(payload.args)}…`
        : `Calling ${payload.name || 'tool'}…`,
    tool_done: payload.name === 'web_search'
      ? `Got ${payload.count || 0} web results.`
      : payload.name === 'search_documents'
        ? `Got ${payload.count || 0} document passages.`
        : `${payload.name || 'Tool'} complete.`,
    tool_error: `Tool ${payload.name || ''} failed${payload.error ? ': ' + payload.error : ''}.`,
  };
  let label = labels[phase] || phase;
  stream.statusEl.innerHTML = '';
  const dot = document.createElement('span'); dot.className = 'dot'; dot.textContent = '●';
  const txt = document.createElement('span'); txt.className = 'txt'; txt.textContent = label;
  stream.statusEl.appendChild(dot);
  stream.statusEl.appendChild(txt);
  // For the search phase, expose the exact query + an "edit" affordance so
  // the user can correct a bad rewrite without waiting for the answer.
  if (phase === 'searching' && payload.query) {
    const q = document.createElement('span');
    q.className = 'query';
    q.textContent = ' \u201C' + payload.query + '\u201D';
    stream.statusEl.appendChild(q);
    const edit = document.createElement('button');
    edit.className = 'btn-link';
    edit.type = 'button';
    edit.textContent = 'edit';
    edit.title = 'Cancel and re-run with a different query';
    edit.addEventListener('click', () => _editSearchQuery(stream, payload.query, originalMsg));
    stream.statusEl.appendChild(edit);
  }
}
// User clicked "edit" on the live "Searching for X" indicator. Pop a tiny
// input, abort the in-flight stream, and resend with `search_query_override`
// so the backend skips the rewriter and uses the user's wording verbatim.
function _editSearchQuery(stream, currentQuery, originalMsg) {
  const el = stream.statusEl;
  if (!el) return;
  el.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'edit-search';
  const inp = document.createElement('input');
  inp.type = 'text';
  inp.className = 'field';
  inp.value = currentQuery;
  inp.setAttribute('aria-label', 'Edit search query');
  const go = document.createElement('button');
  go.className = 'btn btn-filled';
  go.type = 'button';
  go.textContent = 'Re-run';
  const cancel = document.createElement('button');
  cancel.className = 'btn';
  cancel.type = 'button';
  cancel.textContent = 'Cancel';
  wrap.appendChild(inp); wrap.appendChild(go); wrap.appendChild(cancel);
  el.appendChild(wrap);
  setTimeout(() => { inp.focus(); inp.select(); }, 0);
  const submit = () => {
    const newQ = inp.value.trim();
    if (!newQ) return;
    // Abort current stream — finally{} block will save partial output as-is.
    stream.controller?.abort();
    // Remove the assistant row so the resend creates a fresh one.
    stream.aRow?.remove();
    _clearStatus(stream);
    // Resend with override. `send()` reads the input box, so stuff the
    // original user message back in and tag the override on the request.
    state.pendingSearchOverride = newQ;
    input.value = originalMsg;
    input.dispatchEvent(new Event('input'));
    send();
  };
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); submit(); }
    if (e.key === 'Escape') { _clearStatus(stream); }
  });
  go.addEventListener('click', submit);
  cancel.addEventListener('click', () => _clearStatus(stream));
}

async function send() {
  const msg = input.value.trim();
  if (!msg) return;
  // Block only if the *current* chat is still streaming. Other chats are
  // free to keep running in parallel.
  if (state.streams.get(state.currentChatId) && !state.streams.get(state.currentChatId).done) return;

  input.value = ''; input.style.height = 'auto'; $('#counter').textContent = '';
  state.lastUserMessage = msg;

  if ($('#messages .welcome')) $('#messages').innerHTML = `<div class="messages-inner"></div>`;
  const inner = ensureInner();
  inner.appendChild(renderMessage('user', msg));
  const aRow = renderMessage('assistant', '');
  const body = aRow.querySelector('.prose');
  body.classList.add('typing');
  inner.appendChild(aRow);
  scrollToBottom();

  // Bookkeeping: until the server replies with meta{chat_id}, we key the
  // stream by a synthetic id so the user can switch away and back.
  const startedChatId = state.currentChatId;
  const streamKey = startedChatId || `new:${++_newChatCounter}`;
  const controller = new AbortController();
  const stream = {
    controller, full: '', thinking: '',
    citations: [], stats: {}, model: state.currentModel,
    done: false, error: null, tempId: startedChatId ? null : streamKey,
    // Thinking panel is open while the model is still thinking; auto-collapses
    // as soon as the first answer token arrives. User toggles override this.
    thinkingOpen: true,
    // Live DOM refs — replaced when user re-opens this chat later.
    aRow, body,
  };
  state.streams.set(streamKey, stream);
  _refreshComposerUI();
  renderChatList();

  try {
    // Chat-attached files are an implicit "use these specifically" signal:
    // force RAG on and scope the search to just those doc_ids so the model
    // can't ignore them in favour of the broader KB or its own knowledge.
    const attIds = (state.chatAttachments || []).map(d => d.id).filter(Boolean);
    const useRag = $('#toggle-rag').checked || attIds.length > 0;
    const resp = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' },
      signal: controller.signal,
      body: JSON.stringify({
        chat_id: startedChatId, model: state.currentModel, message: msg,
        use_web: $('#toggle-web').checked, use_rag: useRag,
        doc_ids: attIds.length ? attIds : null,
        ref_chat_ids: [...state.refChatIds].filter(id => id !== startedChatId),
        // Set when user edited the rewritten query via the "edit" affordance
        // on the live status line (Batch 2 — #4 editable rewritten query).
        search_query_override: state.pendingSearchOverride || null,
        // Per-chat thinking toggle. Only meaningful for models that
        // advertise the `thinking` capability — backend ignores it
        // otherwise. `null` means "default on if supported".
        think: $('#think-chip').classList.contains('hidden')
          ? null
          : $('#toggle-think').checked,
      }),
    });
    // One-shot: don't leak the override into the next send.
    state.pendingSearchOverride = null;
    if (resp.status === 401) { location.replace('/login'); return; }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = '';
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      const events = buf.split('\n\n'); buf = events.pop() || '';
      for (const evt of events) {
        let ev = 'message', data = '';
        for (const line of evt.split('\n')) {
          if (line.startsWith('event:')) ev = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        if (!data) continue;
        let o; try { o = JSON.parse(data); } catch { continue; }
        if (ev === 'meta') {
          stream.citations = o.citations || [];
          // Re-key from temp id to real chat id once the server tells us.
          if (stream.tempId && o.chat_id) {
            state.streams.delete(stream.tempId);
            state.streams.set(o.chat_id, stream);
            stream.tempId = null;
            if (state.currentChatId === null) state.currentChatId = o.chat_id;
          }
          if (!state.chats.find(c => c.id === o.chat_id)) {
            const provisionalTitle = msg.slice(0, 60) || 'New chat';
            state.chats = [{ id: o.chat_id, title: provisionalTitle, model: o.model, updated_at: Date.now() }, ...state.chats];
            renderChatList();
          }
        }
        else if (ev === 'status') {
          // Live progress shown above the answer while context is being
          // built. Cleared automatically on the "generating" phase or as
          // soon as real tokens start arriving.
          _renderStatus(stream, o, msg);
        }
        else if (ev === 'token') {
          // First answer token — auto-collapse the thinking panel so the
          // reply isn't pushed off-screen by a wall of chain-of-thought.
          if (stream.thinking && stream.thinkingOpen !== false && !stream.full) {
            stream.thinkingOpen = false;
          }
          // Real content has started — kill any lingering status line.
          if (!stream.full) _clearStatus(stream);
          stream.full += o.t;
          _renderStreamInto(stream, stream.body);
          if (_viewingStream(stream)) scrollToBottom();
        }
        else if (ev === 'thinking') {
          stream.thinking += o.t;
          _renderStreamInto(stream, stream.body);
          if (_viewingStream(stream)) scrollToBottom();
        }
        else if (ev === 'stats') { stream.stats = { ...stream.stats, ...o, model: stream.model }; }
        else if (ev === 'warning') {
          // Post-generation warnings (e.g. grounding check flagged a
          // possibly-unsupported claim). Pinned under the answer; also
          // re-rendered from the citations panel on refresh.
          _renderWarning(stream, o);
        }
        else if (ev === 'error') {
          stream.full += `\n\n**Error:** ${o.error}`;
          _renderStreamInto(stream, stream.body);
        }
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      stream.error = err;
      const errBox = document.createElement('div');
      errBox.className = 'stream-error';
      errBox.innerHTML = `<span>Connection lost: ${err.message}</span>`;
      const retry = document.createElement('button');
      retry.className = 'btn btn-filled';
      retry.textContent = 'Retry';
      retry.addEventListener('click', () => {
        errBox.remove();
        input.value = state.lastUserMessage || '';
        send();
      });
      errBox.appendChild(retry);
      _renderStreamInto(stream, stream.body);
      stream.aRow.querySelector('.body').appendChild(errBox);
    }
  } finally {
    stream.done = true;
    stream.body.classList.remove('typing');
    if (stream.citations.length) {
      const cBody = stream.aRow.querySelector('.body');
      cBody.insertBefore(buildCitationsEl(stream.citations), cBody.querySelector('.actions'));
    }
    const actions = stream.aRow.querySelector('.actions');
    actions.innerHTML = '';
    actions.appendChild(buildAssistantActions(stream.full, stream.stats));
    _refreshComposerUI();

    const finalId = stream.tempId ? null : [...state.streams.entries()]
      .find(([, s]) => s === stream)?.[0];
    // Trigger auto-title after the FIRST assistant turn in a chat. Detect
    // by message count (== 2 user+assistant pair just added) rather than
    // comparing titles, since chats pre-created by file attachment carry
    // the literal "New chat" title even after the first real message.
    const msgCount = (stream.aRow?.parentElement?.querySelectorAll('.msg').length) || 0;
    const wasFirst = finalId && msgCount <= 2;
    await refreshChats();
    if (wasFirst && finalId) {
      try { await api(`/api/chats/${finalId}/title`, { method: 'POST' }); await refreshChats(); } catch {}
    }
    if ($('#toggle-web').checked) refreshProviders();
    // Keep the finished stream in the map briefly so re-opening the chat
    // before refresh still re-attaches; clean up once history fetch will cover it.
    setTimeout(() => {
      if (finalId && state.streams.get(finalId) === stream) state.streams.delete(finalId);
    }, 2000);
  }
}

function _viewingStream(stream) {
  return stream.body && document.body.contains(stream.body);
}

function _refreshComposerUI() {
  const active = state.streams.get(state.currentChatId);
  const streaming = !!(active && !active.done);
  $('#send').disabled = !input.value.trim() || streaming;
  $('#send').classList.toggle('hidden', streaming);
  $('#stop').classList.toggle('hidden', !streaming);
}

// Lazily fetch a model's capabilities and show/hide the Thinking chip
// accordingly. Caps are cached for the lifetime of the page session.
async function _refreshThinkChip() {
  const chip = $('#think-chip');
  if (!chip) return;
  const m = state.currentModel;
  if (!m) { chip.classList.add('hidden'); return; }
  let caps = state.modelCaps.get(m);
  if (!caps) {
    try {
      const r = await api(`/api/models/capabilities?name=${encodeURIComponent(m)}`);
      caps = new Set(r.capabilities || []);
    } catch { caps = new Set(); }
    state.modelCaps.set(m, caps);
  }
  const supports = caps.has('thinking');
  chip.classList.toggle('hidden', !supports);
  if (supports) {
    // Default ON when the model supports it (matches prior behaviour);
    // restore the user's last choice for this model from localStorage.
    const stored = localStorage.getItem(`r2d2-think-${m}`);
    $('#toggle-think').checked = stored === null ? true : stored === '1';
  }
}
$('#toggle-think')?.addEventListener('change', e => {
  const m = state.currentModel;
  if (m) localStorage.setItem(`r2d2-think-${m}`, e.target.checked ? '1' : '0');
});

// ---------- Sidebar collapse / resize / status ----------
// Two distinct modes:
//   • Desktop (>=900px): .collapsed shrinks width to 0 (CSS rule). Because the
//     resizer sets inline width/flex-basis, we stash them when collapsing and
//     restore on expand so the class actually wins.
//   • Mobile  (<900px):  .open slides the fixed drawer in/out via translateX.
// The two states are mutually exclusive — toggling one always clears the other.
const SIDEBAR_MOBILE_MQ = '(max-width: 900px)';
function _isSidebarMobile() { return window.matchMedia(SIDEBAR_MOBILE_MQ).matches; }
function _stashSidebarWidth(sb) {
  if (sb.style.width)      sb.dataset.savedWidth     = sb.style.width;
  if (sb.style.flexBasis)  sb.dataset.savedFlexBasis = sb.style.flexBasis;
  sb.style.width = '';
  sb.style.flexBasis = '';
}
function _restoreSidebarWidth(sb) {
  if (sb.dataset.savedWidth)     { sb.style.width     = sb.dataset.savedWidth;     delete sb.dataset.savedWidth; }
  if (sb.dataset.savedFlexBasis) { sb.style.flexBasis = sb.dataset.savedFlexBasis; delete sb.dataset.savedFlexBasis; }
}
function setSidebarCollapsed(collapsed) {
  const sb = $('#sidebar');
  if (!sb) return;
  // Desktop collapse uses width; mobile uses drawer slide via .open.
  if (_isSidebarMobile()) {
    sb.classList.remove('collapsed');
    sb.classList.toggle('open', !collapsed);
  } else {
    sb.classList.remove('open');
    if (collapsed) { _stashSidebarWidth(sb); sb.classList.add('collapsed'); }
    else           { sb.classList.remove('collapsed'); _restoreSidebarWidth(sb); }
    try { localStorage.setItem('r2d2-sidebar-collapsed', collapsed ? '1' : '0'); } catch {}
  }
}
$('#sidebar-collapse')?.addEventListener('click', () => setSidebarCollapsed(true));
$('#sidebar-toggle')?.addEventListener('click', () => {
  const sb = $('#sidebar');
  if (!sb) return;
  if (_isSidebarMobile()) {
    // Toggle drawer; always clear any leftover .collapsed from a desktop session.
    sb.classList.remove('collapsed');
    sb.classList.toggle('open');
  } else {
    setSidebarCollapsed(!sb.classList.contains('collapsed'));
  }
});
// Restore prior desktop state on load; never auto-collapse on mobile.
try {
  if (!_isSidebarMobile() && localStorage.getItem('r2d2-sidebar-collapsed') === '1') {
    queueMicrotask(() => setSidebarCollapsed(true));
  }
} catch {}
// If the viewport crosses the breakpoint, reset class soup so the new mode renders correctly.
window.matchMedia(SIDEBAR_MOBILE_MQ).addEventListener('change', () => {
  const sb = $('#sidebar');
  if (!sb) return;
  sb.classList.remove('open');
  if (_isSidebarMobile()) {
    // Drop desktop-only collapsed/inline state while in mobile mode.
    sb.classList.remove('collapsed');
    _restoreSidebarWidth(sb);
  } else if (localStorage.getItem('r2d2-sidebar-collapsed') === '1') {
    setSidebarCollapsed(true);
  }
});

// Sidebar drag-to-resize
(function initSidebarResize() {
  const sb = $('#sidebar'); const handle = $('#sidebar-resizer');
  if (!sb || !handle) return;
  const MIN = 220, MAX = 520;
  const saved = parseInt(localStorage.getItem('r2d2-sidebar-w') || '', 10);
  if (saved && saved >= MIN && saved <= MAX) {
    sb.style.flexBasis = saved + 'px';
    sb.style.width = saved + 'px';
  }
  let dragging = false;
  const onMove = (e) => {
    if (!dragging) return;
    const x = e.touches ? e.touches[0].clientX : e.clientX;
    const w = Math.max(MIN, Math.min(MAX, x));
    sb.style.flexBasis = w + 'px';
    sb.style.width = w + 'px';
  };
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    document.body.style.userSelect = '';
    document.body.style.cursor = '';
    const w = parseInt(sb.style.width || '', 10);
    if (w) localStorage.setItem('r2d2-sidebar-w', String(w));
    window.removeEventListener('mousemove', onMove);
    window.removeEventListener('touchmove', onMove);
    window.removeEventListener('mouseup', stop);
    window.removeEventListener('touchend', stop);
    // Re-enable transition after dragging
    sb.style.transition = '';
  };
  const start = (e) => {
    if (sb.classList.contains('collapsed')) return;
    dragging = true;
    handle.classList.add('dragging');
    document.body.style.userSelect = 'none';
    document.body.style.cursor = 'col-resize';
    sb.style.transition = 'none';
    window.addEventListener('mousemove', onMove);
    window.addEventListener('touchmove', onMove, { passive: true });
    window.addEventListener('mouseup', stop);
    window.addEventListener('touchend', stop);
    e.preventDefault();
  };
  handle.addEventListener('mousedown', start);
  handle.addEventListener('touchstart', start, { passive: false });
  // Double-click to reset
  handle.addEventListener('dblclick', () => {
    sb.style.flexBasis = ''; sb.style.width = '';
    localStorage.removeItem('r2d2-sidebar-w');
  });
  // Keyboard accessibility
  handle.addEventListener('keydown', (e) => {
    const cur = parseInt(getComputedStyle(sb).width, 10) || 280;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      const w = Math.max(MIN, Math.min(MAX, cur + (e.key === 'ArrowLeft' ? -16 : 16)));
      sb.style.flexBasis = w + 'px'; sb.style.width = w + 'px';
      localStorage.setItem('r2d2-sidebar-w', String(w));
    }
  });
})();
function setStatus(text, spin) {
  const el = $('#status-pill');
  if (!text) { el.classList.add('hidden'); el.innerHTML = ''; return; }
  el.innerHTML = (spin ? `<span style="width:10px;height:10px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;display:inline-block;animation:spin .8s linear infinite"></span>` : '') + `<span>${escapeHtml(text)}</span>`;
  el.classList.remove('hidden');
}

// ============================================================
// Settings dialog
// ============================================================
const TABS = [
  { id: 'general',  label: 'General',     icon: 'M12 20h9 M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4z' },
  { id: 'profile',  label: 'Profile',     icon: 'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2 M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0z' },
  { id: 'models',   label: 'Models',      icon: 'M20 17.58A5 5 0 0 0 18 8h-1.26A8 8 0 1 0 4 16.25' },
  { id: 'embeddings', label: 'Embeddings', icon: 'M4 7h16 M4 12h16 M4 17h10 M18 14l3 3-3 3', adminOnly: true },
  { id: 'web',      label: 'Web Search',  icon: 'M2 12h20 M12 2a15 15 0 0 1 0 20 M12 2a15 15 0 0 0 0 20 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z' },
  { id: 'usage',    label: 'Usage',       icon: 'M3 3v18h18 M7 14l4-4 4 4 5-5' },
  { id: 'users',    label: 'Users',       icon: 'M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2 M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M23 21v-2a4 4 0 0 0-3-3.87 M16 3.13a4 4 0 0 1 0 7.75', adminOnly: true },
  { id: 'about',    label: 'About',       icon: 'M12 16v-4 M12 8h.01 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0z' },
];

function openSettings(tabId = 'general') {
  $('#modal').classList.remove('hidden');
  renderSettingsTabs(tabId);
}
$('#modal-close').addEventListener('click', () => $('#modal').classList.add('hidden'));
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') $('#modal').classList.add('hidden'); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('#modal').classList.add('hidden'); });

function renderSettingsTabs(active) {
  const isAdmin = !!state.user?.is_admin;
  const visible = TABS.filter(t => !t.adminOnly || isAdmin);
  if (!visible.find(t => t.id === active)) active = 'general';
  $('#modal-tabs').innerHTML = visible.map(t => `
    <button class="tab-btn ${t.id === active ? 'active' : ''}" data-tab="${t.id}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="${t.icon}"/></svg>
      ${t.label}
    </button>`).join('');
  $$('#modal-tabs .tab-btn').forEach(b => b.addEventListener('click', () => renderSettingsTabs(b.dataset.tab)));
  renderSettingsBody(active);
}

async function renderSettingsBody(tab) {
  const body = $('#modal-body');
  body.innerHTML = `<div style="color:var(--on-surface-faint)">Loading…</div>`;
  if      (tab === 'general')  body.innerHTML = await tabGeneral();
  else if (tab === 'profile')  body.innerHTML = await tabProfile();
  else if (tab === 'models')   body.innerHTML = await tabModels();
  else if (tab === 'embeddings') body.innerHTML = await tabEmbeddings();
  else if (tab === 'web')      body.innerHTML = await tabWeb();
  else if (tab === 'usage')    body.innerHTML = await tabUsage();
  else if (tab === 'users')    body.innerHTML = await tabUsers();
  else if (tab === 'about')    body.innerHTML = tabAbout();
  bindSettings(tab);
}

async function tabGeneral() {
  const s = state.settings;
  
  // Group models by vendor for default model selector
  const chatModels = state.models.filter(m => m.kind !== 'embed');
  const grouped = {};
  chatModels.forEach(m => {
    const vendor = getModelVendor(m.name);
    if (!grouped[vendor]) grouped[vendor] = [];
    grouped[vendor].push(m);
  });
  const vendors = Object.keys(grouped).sort((a, b) => {
    if (a === 'Other') return 1;
    if (b === 'Other') return -1;
    return a.localeCompare(b);
  });
  const modelOptionsHtml = vendors.map(vendor => {
    const vendorModels = grouped[vendor];
    return `<optgroup label="${escapeHtml(vendor)}">
      ${vendorModels.map(m => `<option value="${escapeAttr(m.name)}" title="${escapeAttr(m.name)}" ${s.default_model === m.name ? 'selected' : ''}>${escapeHtml(modelLabel(m.name))}</option>`).join('')}
    </optgroup>`;
  }).join('');
  
  return `
    <h3 class="section-title">General</h3>

    <div class="form-row">
      <div><div class="label">Default model</div><div class="desc">Used for new chats</div></div>
      <select class="field" data-set="default_model">
        <option value="">— Server default —</option>
        ${modelOptionsHtml}
      </select>
    </div>

    <div class="form-row">
      <div><div class="label">Temperature</div><div class="desc">Higher values are more creative</div></div>
      <div style="display:flex; align-items:center; gap:var(--sp-4);">
        <input type="range" min="0" max="1.5" step="0.1" data-set="temperature" value="${s.temperature}" style="flex:1;">
        <span id="temp-v" style="font:var(--t-callout); width:32px; text-align:right;">${(+s.temperature).toFixed(1)}</span>
      </div>
    </div>

    <div class="form-row">
      <div><div class="label">Use documents (RAG) by default</div></div>
      <label class="switch"><input type="checkbox" data-set="use_rag_default" ${s.use_rag_default ? 'checked' : ''}><span class="track"></span><span class="thumb"></span></label>
    </div>

    <div class="form-row">
      <div><div class="label">Use web search by default</div></div>
      <label class="switch"><input type="checkbox" data-set="use_web_default" ${s.use_web_default ? 'checked' : ''}><span class="track"></span><span class="thumb"></span></label>
    </div>

    <div class="form-row">
      <div><div class="label">Theme</div></div>
      <select class="field" data-set="theme">
        <option value="dark"  ${s.theme === 'dark'  ? 'selected' : ''}>Dark</option>
        <option value="light" ${s.theme === 'light' ? 'selected' : ''}>Light</option>
        <option value="slate" ${s.theme === 'slate' ? 'selected' : ''}>Slate</option>
      </select>
    </div>

    <div class="form-row stacked">
      <div><div class="label">Persona override</div><div class="desc">Appended to ${escapeHtml(assistantName())}'s system prompt</div></div>
      <textarea class="field" rows="4" data-set="persona_extra" placeholder="e.g. Always reply in concise bullet points.">${escapeHtml(s.persona_extra || '')}</textarea>
    </div>

    <h3 class="section-title subtle">Retrieval</h3>
    <div class="form-row">
      <div><div class="label">RAG top-K</div><div class="desc">Chunks to retrieve per query</div></div>
      <input class="field" type="number" min="1" max="60" data-set="rag_top_k" value="${s.rag_top_k}">
    </div>
    <div class="form-row">
      <div><div class="label">Web search top-K</div></div>
      <input class="field" type="number" min="1" max="10" data-set="web_top_k" value="${s.web_top_k}">
    </div>
    <div class="form-row">
      <div><div class="label">Web chars per result</div><div class="desc">Extracted page text length sent to the model (per source)</div></div>
      <input class="field" type="number" min="500" max="20000" step="100" data-set="web_chars_per_result" value="${s.web_chars_per_result}">
    </div>

    <h3 class="section-title subtle">Memory</h3>
    <div class="form-row">
      <div><div class="label">Conversation history</div><div class="desc">Max prior messages sent back to the model</div></div>
      <input class="field" type="number" min="4" max="400" step="2" data-set="max_history_messages" value="${s.max_history_messages}">
    </div>
    <div class="form-row">
      <div><div class="label">Cross-chat reference depth</div><div class="desc">Recent messages pulled per referenced chat</div></div>
      <input class="field" type="number" min="2" max="60" step="2" data-set="ref_chat_turns" value="${s.ref_chat_turns}">
    </div>`;
}

async function tabProfile() {
  const u = state.user;
  const accent = state.settings.accent || '';
  const accentValue = accent || '#3478f6';
  const sidebarColor = state.settings.sidebar_color || '';
  const hasAvatar = !!u.avatar;
  return `
    <h3 class="section-title">Profile</h3>
    <div class="form-row">
      <div class="label">Avatar</div>
      <div class="avatar-edit">
        <div id="prof-avatar" class="avatar xl"></div>
        <div class="avatar-edit-actions">
          <input type="file" id="avatar-input" accept="image/*" hidden>
          <button class="btn" id="avatar-pick">${hasAvatar ? 'Replace' : 'Upload image'}</button>
          <button class="btn btn-text" id="avatar-remove" ${hasAvatar ? '' : 'hidden'}>Remove</button>
        </div>
      </div>
    </div>
    <div class="form-row"><div class="label">Username</div><input class="field" id="prof-username" value="${escapeAttr(u.username)}" autocomplete="username"></div>
    <div class="form-row"><div class="label">Display name</div><input class="field" id="prof-name" value="${escapeAttr(u.display_name || '')}"></div>
    <div class="form-row"><div class="label">Email</div><input class="field" id="prof-email" type="email" value="${escapeAttr(u.email || '')}"></div>
    <div class="form-row"><div></div><button class="btn btn-filled" id="prof-save">Save profile</button></div>

    <div class="form-row">
      <div class="label">Theme accent</div>
      <div class="accent-edit">
        <div class="swatches" id="accent-swatches">
          <button type="button" class="swatch reset ${accent ? '' : 'selected'}" data-accent="" title="Default (system blue)" aria-label="Default"></button>
          ${ACCENT_PRESETS.map(p => `
            <button type="button" class="swatch ${accent && accent.toLowerCase() === p.hex.toLowerCase() ? 'selected' : ''}"
              data-accent="${p.hex}" data-theme="${p.theme}" title="${p.name} (best on ${p.theme})"
              style="--sw:${p.hex}" aria-label="${p.name}"></button>`).join('')}
        </div>
      </div>
    </div>

    <div class="form-row">
      <div>
        <div class="label">Sidebar color</div>
        <div class="desc">Text contrast is computed automatically. Custom hex accepted.</div>
      </div>
      <div class="accent-edit">
        <div class="swatches" id="sidebar-swatches">
          <button type="button" class="swatch reset ${sidebarColor ? '' : 'selected'}" data-sidebar="" title="Theme default" aria-label="Theme default"></button>
          ${SIDEBAR_PRESETS.map(p => `
            <button type="button" class="swatch ${sidebarColor && sidebarColor.toLowerCase() === p.hex.toLowerCase() ? 'selected' : ''}"
              data-sidebar="${p.hex}" title="${p.name}"
              style="--sw:${p.hex}" aria-label="${p.name}"></button>`).join('')}
          <input type="color" id="sidebar-color-input" class="swatch-picker" value="${sidebarColor || '#3a3e44'}" aria-label="Custom sidebar color" title="Pick custom color">
        </div>
      </div>
    </div>

    <h3 class="section-title subtle">Change password</h3>
    <div class="form-row"><div class="label">Current password</div><input class="field" id="pw-cur" type="password" autocomplete="current-password"></div>
    <div class="form-row"><div class="label">New password</div><input class="field" id="pw-new" type="password" autocomplete="new-password"></div>
    <div class="form-row"><div></div><button class="btn btn-filled" id="pw-save">Update password</button></div>`;
}

async function tabModels() {
  // Hide embedding-only models from this tab — they're administered from
  // Settings → Embeddings (per-doc lifecycle, not user-pickable). The
  // backend tags them with kind='embed' on /api/models; we reuse that set
  // to filter the loaded-in-memory panel, whose endpoint doesn't carry
  // the `kind` field.
  const _stripTag = (s) => (s || '').split(':')[0];
  const _embedNames = new Set(
    (state.models || []).filter(m => m.kind === 'embed').map(m => _stripTag(m.name))
  );
  const _isEmbed = (name) => _embedNames.has(_stripTag(name));
  // Loaded-models panel — best effort: if Ollama is unreachable, skip silently.
  let loaded = [];
  try {
    const r = await api('/api/models/loaded');
    loaded = (r.models || []).filter(m => !_isEmbed(m.name || m.model || ''));
  } catch { /* ignore */ }
  const loadedTotal = loaded.reduce((n, m) => n + (m.size || 0), 0);
  const loadedVram = loaded.reduce((n, m) => n + (m.size_vram || 0), 0);
  const loadedPanel = `
    <h3 class="section-title">Loaded in memory</h3>
    <p style="font:var(--t-caption); color:var(--on-surface-var); margin:-8px 0 var(--sp-3);">
      Models currently held in RAM/VRAM. Ollama auto-unloads after a few minutes of inactivity, but you can free memory now to make room for a larger model.
    </p>
    <div style="display:flex; flex-direction:column; gap:var(--sp-2); margin-bottom:var(--sp-4);">
      ${loaded.length === 0 ? `
        <div class="stat-card" style="padding:var(--sp-3) var(--sp-4); color:var(--on-surface-faint);">
          No models loaded — they'll appear here when used.
        </div>
      ` : loaded.map(m => {
        const name = m.name || m.model || '';
        const vram = m.size_vram || 0;
        const total = m.size || 0;
        const cpu = Math.max(0, total - vram);
        const expires = m.expires_at ? new Date(m.expires_at) : null;
        const expiresIn = expires ? Math.max(0, Math.round((expires - Date.now()) / 60000)) : null;
        return `
        <div class="stat-card" style="display:flex; align-items:center; gap:var(--sp-3); padding:var(--sp-3) var(--sp-4);">
          <div style="flex:1; min-width:0;">
            <div style="font:var(--t-callout);">${escapeHtml(modelLabel(name))}</div>
            <div style="font:var(--t-micro); color:var(--on-surface-faint); margin-top:2px;">
              <code style="opacity:.7;">${escapeHtml(name)}</code>
              · ${fmtBytes(total)} total
              ${vram ? ` · ${fmtBytes(vram)} VRAM` : ''}
              ${cpu ? ` · ${fmtBytes(cpu)} RAM` : ''}
              ${expiresIn !== null ? ` · auto-unload in ~${expiresIn}m` : ''}
            </div>
          </div>
          <button class="btn btn-text" data-unload="${escapeAttr(name)}" title="Free memory now">Unload</button>
        </div>`;
      }).join('')}
    </div>
    ${loaded.length > 1 ? `
      <div style="display:flex; gap:var(--sp-2); align-items:center; margin: -8px 0 var(--sp-4);">
        <button class="btn btn-text danger" id="unload-all" title="Free all model memory">Unload all (${fmtBytes(loadedTotal)})</button>
        <span style="font:var(--t-caption); color:var(--on-surface-faint);">${loadedVram ? `${fmtBytes(loadedVram)} on GPU` : 'CPU only'}</span>
      </div>
    ` : ''}
    ${state.user.is_admin ? `
      <div style="display:flex; gap:var(--sp-2); align-items:center; margin: -8px 0 var(--sp-4); flex-wrap:wrap;">
        <button class="btn btn-text danger" id="restart-ollama" title="Forcibly restart the Ollama service to release stuck memory">Restart Ollama service</button>
        <span style="font:var(--t-caption); color:var(--on-surface-faint);">Last resort if Unload doesn't free enough memory.</span>
      </div>
    ` : ''}
  `;
  
  // Group installed models by vendor
  const installedModels = state.models.filter(m => m.kind !== 'embed');
  const installedGrouped = {};
  installedModels.forEach(m => {
    const vendor = getModelVendor(m.name);
    if (!installedGrouped[vendor]) installedGrouped[vendor] = [];
    installedGrouped[vendor].push(m);
  });
  const installedVendors = Object.keys(installedGrouped).sort((a, b) => {
    if (a === 'Other') return 1;
    if (b === 'Other') return -1;
    return a.localeCompare(b);
  });
  
  return `
    ${loadedPanel}
    <h3 class="section-title">Installed models</h3>
    <div style="display:flex; flex-direction:column; gap:var(--sp-2);">
      ${installedVendors.map(vendor => `
        <div style="margin-top: var(--sp-2);">
          <div style="font:var(--t-caption-b); color:var(--on-surface-var); padding:var(--sp-2) var(--sp-4); opacity:0.7;">${escapeHtml(vendor)}</div>
          <div style="display:flex; flex-direction:column; gap:var(--sp-2);">
            ${installedGrouped[vendor].map(m => `
              <div class="stat-card" style="display:flex; align-items:center; gap:var(--sp-3); padding:var(--sp-3) var(--sp-4);">
                <div style="flex:1; min-width:0;">
                  <div style="font:var(--t-callout);">${escapeHtml(modelLabel(m.name))}</div>
                  <div style="font:var(--t-micro); color:var(--on-surface-faint); margin-top:2px;"><code style="opacity:.7;">${escapeHtml(m.name)}</code> · ${fmtBytes(m.size)}</div>
                </div>
                <button class="btn btn-text" data-info="${escapeAttr(m.name)}" title="Show details">Details</button>
                ${state.user.is_admin ? `<button class="btn btn-text danger" data-del="${escapeAttr(m.name)}" title="Delete model">Delete</button>` : ''}
              </div>`).join('')}
          </div>
        </div>
      `).join('')}
    </div>
    ${state.user.is_admin ? `
      <h3 class="section-title subtle">Pull a new model</h3>
      <div style="display:flex; gap:var(--sp-2);">
        <button class="btn btn-filled" id="pull-open">Pull model…</button>
        <span style="font:var(--t-caption); color:var(--on-surface-faint); align-self:center;">Browse the <a href="https://ollama.com/library" target="_blank" rel="noopener">Ollama library</a> for names.</span>
      </div>
    ` : `<p style="font:var(--t-caption); color:var(--on-surface-faint); margin-top:var(--sp-4);">Only admins can pull or delete models.</p>`}`;
}

// ---------------------------------------------------------------------------
// Embeddings tab (admin only)
// ---------------------------------------------------------------------------
// The active embedding model is a deployment-wide setting. Changing it
// requires re-embedding every document, so we surface a clear confirmation
// and live progress instead of treating it like a preference.

async function tabEmbeddings() {
  let data;
  try { data = await api('/api/admin/embeddings'); }
  catch (err) { return `<p style="color:var(--err);">${escapeHtml(err.message)}</p>`; }
  state.embed = data;
  const active = data.active || {};
  const curated = data.curated || [];
  const reembed = data.reembed || { state: 'idle' };

  const row = (m) => {
    const isActive = m.id === active.model;
    return `
      <label class="provider-row ${isActive ? 'active' : ''}" data-pick-embed="${escapeAttr(m.id)}">
        <input type="radio" name="embed-pick" ${isActive ? 'checked' : ''}>
        <div class="pinfo">
          <div class="ptitle">
            <span class="name">${escapeHtml(m.label)}</span>
            ${isActive ? `<span class="badge primary">Active</span>` : ''}
            ${m.recommended ? `<span class="badge success">Recommended</span>` : ''}
            <span class="badge">${m.dim}-dim</span>
            <span class="badge">${m.ctx.toLocaleString()} ctx</span>
            <span class="badge">${m.size_mb} MB</span>
            <span class="badge">${escapeHtml(m.license)}</span>
          </div>
          <div class="pdesc"><code style="opacity:.7;">${escapeHtml(m.id)}</code> — ${escapeHtml(m.summary)}</div>
        </div>
      </label>`;
  };

  return `
    <h3 class="section-title">Active model</h3>
    <p style="font:var(--t-caption); color:var(--on-surface-var); margin: -8px 0 var(--sp-4);">
      Used by every user for document retrieval. Vectors from different models cannot be mixed,
      so switching triggers a re-embed of every existing document.
    </p>
    <div class="stat-card" style="padding:var(--sp-3) var(--sp-4); margin-bottom:var(--sp-4);">
      <div style="font:var(--t-callout);"><code>${escapeHtml(active.model || '—')}</code></div>
      <div style="font:var(--t-micro); color:var(--on-surface-faint); margin-top:2px;">
        ${active.dim ? `${active.dim}-dim vectors` : 'dimension not yet recorded — set on first embed'}
      </div>
    </div>

    <h3 class="section-title">Choose model</h3>
    <div style="display:flex; flex-direction:column; gap:var(--sp-2);">
      ${curated.map(row).join('')}
    </div>

    <h3 class="section-title subtle">Custom</h3>
    <p style="font:var(--t-caption); color:var(--on-surface-var); margin: -8px 0 var(--sp-3);">
      Use any embedding model available to Ollama. We probe it once to confirm output dimension before switching.
    </p>
    <div style="display:flex; gap:var(--sp-2); align-items:center; flex-wrap:wrap;">
      <input id="embed-custom" type="text" placeholder="e.g. mxbai-embed-large:latest" style="flex:1; min-width:240px;">
      <button class="btn btn-text" id="embed-probe">Probe</button>
    </div>
    <div id="embed-probe-out" style="font:var(--t-micro); color:var(--on-surface-faint); margin-top:var(--sp-2);"></div>

    <div style="display:flex; gap:var(--sp-3); align-items:center; margin-top:var(--sp-5); padding-top:var(--sp-4); border-top:1px solid var(--outline-variant);">
      <button class="btn btn-filled" id="embed-apply" disabled>Apply &amp; re-embed</button>
      <span id="embed-apply-hint" style="font:var(--t-caption); color:var(--on-surface-faint);">Select a different model to enable.</span>
    </div>

    <div id="embed-reembed-panel" style="margin-top:var(--sp-4);">${renderReembedPanel(reembed)}</div>
  `;
}

function renderReembedPanel(s) {
  if (!s || s.state === 'idle') return '';
  const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
  const fails = (s.failed_docs || []).length;
  const label = s.state === 'running' ? 'Re-embedding documents…'
              : s.state === 'done'    ? 'Re-embed complete'
              : 'Re-embed failed';
  const tone  = s.state === 'error' ? 'err' : (s.state === 'done' ? 'ok' : 'primary');
  return `
    <div class="stat-card" style="padding:var(--sp-3) var(--sp-4);">
      <div style="display:flex; align-items:center; gap:var(--sp-3); margin-bottom:var(--sp-2);">
        <span class="badge ${tone}">${label}</span>
        <span style="font:var(--t-micro); color:var(--on-surface-faint);">
          ${s.from_model ? `${s.from_model} → ${s.to_model} (${s.to_dim}-dim)` : ''}
        </span>
      </div>
      <div style="height:6px; border-radius:3px; background:var(--outline-variant); overflow:hidden;">
        <div style="height:100%; width:${pct}%; background:linear-gradient(90deg, #4f46e5, #ec4899); transition:width .3s;"></div>
      </div>
      <div style="font:var(--t-micro); color:var(--on-surface-faint); margin-top:var(--sp-2);">
        ${s.done} / ${s.total} documents · ${s.chunks_done.toLocaleString()} chunks re-embedded
        ${fails ? ` · <span style="color:var(--err);">${fails} failed</span>` : ''}
      </div>
      ${fails ? `<details style="margin-top:var(--sp-2);"><summary style="cursor:pointer; font:var(--t-micro);">Failed documents</summary>
        <ul style="margin:var(--sp-2) 0 0; padding-left:var(--sp-4); font:var(--t-micro); color:var(--on-surface-var);">
          ${s.failed_docs.map(f => `<li><code>${escapeHtml(f.name || f.id)}</code> — ${escapeHtml(f.error)}</li>`).join('')}
        </ul></details>` : ''}
    </div>`;
}

// Wires up the Embeddings tab. Picking a different model (curated or
// custom) enables the Apply button; clicking Apply probes the model first
// (to confirm dim), then POSTs /api/admin/embeddings/apply which starts
// the re-embed job. We then poll /status until it leaves the running state.
function bindEmbeddings() {
  const apply  = $('#embed-apply');
  const hint   = $('#embed-apply-hint');
  const custom = $('#embed-custom');
  const probeOut = $('#embed-probe-out');
  const panel  = $('#embed-reembed-panel');

  let chosen = { model: state.embed.active.model, dim: state.embed.active.dim, source: 'active' };

  const refreshApplyState = () => {
    const sameAsActive = chosen.model === state.embed.active.model && chosen.dim === state.embed.active.dim;
    const running = (state.embed.reembed?.state === 'running');
    apply.disabled = sameAsActive || !chosen.model || !chosen.dim || running;
    hint.textContent = running ? 'A re-embed job is in progress…'
                      : sameAsActive ? 'Select a different model to enable.'
                      : `Will switch to ${chosen.model} (${chosen.dim}-dim) and re-embed all documents.`;
  };

  $$('#modal-body [data-pick-embed]').forEach(row => row.addEventListener('click', (e) => {
    if (e.target.closest('input')) {} // allow native radio toggle
    const id = row.dataset.pickEmbed;
    const m = (state.embed.curated || []).find(x => x.id === id);
    if (!m) return;
    chosen = { model: m.id, dim: m.dim, source: 'curated' };
    $$('#modal-body [data-pick-embed]').forEach(r => r.classList.toggle('active', r === row));
    row.querySelector('input').checked = true;
    probeOut.textContent = '';
    refreshApplyState();
  }));

  $('#embed-probe').addEventListener('click', async () => {
    const model = (custom.value || '').trim();
    if (!model) { probeOut.textContent = 'Enter a model id first.'; return; }
    probeOut.textContent = 'Probing…';
    try {
      const r = await api('/api/admin/embeddings/probe', J({ model }));
      probeOut.innerHTML = `<span style="color:var(--ok);">OK</span> — ${escapeHtml(model)} returns <strong>${r.dim}</strong>-dim vectors.`;
      chosen = { model, dim: r.dim, source: 'custom' };
      $$('#modal-body [data-pick-embed]').forEach(r => r.classList.remove('active'));
      $$('#modal-body [data-pick-embed] input').forEach(i => i.checked = false);
      refreshApplyState();
    } catch (err) {
      probeOut.innerHTML = `<span style="color:var(--err);">${escapeHtml(err.message)}</span>`;
    }
  });

  apply.addEventListener('click', async () => {
    if (!confirm(`Switch the embedding model to "${chosen.model}" (${chosen.dim}-dim)?\n\nEvery existing document will be re-embedded in the background. New uploads start using the new model immediately. Retrieval quality on the old corpus may dip while the job runs.`)) return;
    apply.disabled = true; apply.textContent = 'Starting…';
    try {
      const r = await api('/api/admin/embeddings/apply', J({ model: chosen.model, dim: chosen.dim }));
      state.embed.active = { model: chosen.model, dim: chosen.dim };
      state.embed.reembed = r.reembed || { state: 'running' };
      panel.innerHTML = renderReembedPanel(state.embed.reembed);
      toast('Re-embed started', 'ok');
      pollReembed();
    } catch (err) {
      toast(err.message, 'err');
    } finally {
      apply.textContent = 'Apply & re-embed';
      refreshApplyState();
    }
  });

  // If a job is already running when the user opens this tab, start polling
  // so they see live progress without having to click anything.
  if (state.embed.reembed?.state === 'running') pollReembed();

  refreshApplyState();
}

let _reembedPoll = null;
async function pollReembed() {
  if (_reembedPoll) return;
  const tick = async () => {
    try {
      const s = await api('/api/admin/embeddings/status');
      const panel = $('#embed-reembed-panel');
      if (panel) panel.innerHTML = renderReembedPanel(s);
      if (state.embed) state.embed.reembed = s;
      if (s.state !== 'running') {
        clearInterval(_reembedPoll); _reembedPoll = null;
        if (s.state === 'done') toast('Re-embed complete', 'ok');
      }
    } catch (e) {
      clearInterval(_reembedPoll); _reembedPoll = null;
    }
  };
  _reembedPoll = setInterval(tick, 2000);
  tick();
}

async function tabWeb() {
  try { const d = await api('/api/search/providers'); state.providers = d.providers; state.activeProvider = d.active; renderProviderMenu(); } catch {}
  const providers = state.providers;
  const u = await api('/api/usage');
  const w = u.web_search;
  const activeId = state.activeProvider;
  return `
    <h3 class="section-title">Search provider</h3>
    <p style="font:var(--t-caption); color:var(--on-surface-var); margin: -8px 0 var(--sp-4);">Choose the engine ${escapeHtml(assistantName())} uses when "Web" is enabled. Switch any time from the account menu.</p>

    <div>
      ${providers.map(p => `
        <label class="provider-row ${p.id === activeId ? 'active' : ''}" data-pick="${p.id}">
          <input type="radio" name="prov" ${p.id === activeId ? 'checked' : ''} ${!p.configured ? 'disabled' : ''}>
          <div class="pinfo">
            <div class="ptitle">
              <span class="name">${escapeHtml(p.label)}</span>
              ${p.id === activeId ? `<span class="badge primary">Active</span>` : ''}
              ${p.configured ? `<span class="badge success">Ready</span>` : `<span class="badge warning">Needs setup</span>`}
              ${p.monthly_quota ? `<span class="badge">${p.monthly_quota.toLocaleString()}/mo free</span>` : ''}
            </div>
            <div class="pdesc">${escapeHtml(p.description || '')}</div>
          </div>
          ${p.help_url ? `<a class="btn btn-text" href="${escapeAttr(p.help_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">Get key</a>` : ''}
        </label>
      `).join('')}
    </div>

    <h3 class="section-title subtle">Provider credentials</h3>
    <p style="font:var(--t-caption); color:var(--on-surface-var); margin: -8px 0 var(--sp-4);">Keys are stored per-user and override environment variables.</p>

    <div class="form-row">
      <div><div class="label">Tavily API key</div><div class="desc">Free 1000/mo at tavily.com</div></div>
      <input class="field" type="password" placeholder="${state.settings.tavily_api_key ? '••••••••' : 'tvly-…'}" data-set="tavily_api_key" value="${escapeAttr(state.settings.tavily_api_key || '')}">
    </div>
    <div class="form-row">
      <div><div class="label">Brave Search API key</div><div class="desc">Free 2000/mo at brave.com/search/api</div></div>
      <input class="field" type="password" placeholder="${state.settings.brave_api_key ? '••••••••' : 'BSA…'}" data-set="brave_api_key" value="${escapeAttr(state.settings.brave_api_key || '')}">
    </div>
    <div class="form-row">
      <div><div class="label">SearXNG URL</div><div class="desc">Self-hosted instance with JSON enabled</div></div>
      <input class="field" type="url" placeholder="https://searx.example.com" data-set="searxng_url" value="${escapeAttr(state.settings.searxng_url || '')}">
    </div>

    <h3 class="section-title subtle">Quota — active provider</h3>
    <div class="stats-grid">
      <div class="stat-card">
        <div class="label">This month</div>
        <div class="value">${w.active_month ?? 0}</div>
        <div class="sub">${w.monthly_quota ? `of ${w.monthly_quota.toLocaleString()} free` : 'no documented limit'}</div>
        ${w.monthly_quota ? `<div class="progress"><div class="bar" style="width:${Math.min(100, ((w.active_month || 0) / w.monthly_quota) * 100)}%"></div></div>` : ''}
      </div>
      <div class="stat-card">
        <div class="label">Remaining (est.)</div>
        <div class="value">${w.remaining_estimate ?? '∞'}</div>
        <div class="sub">resets monthly</div>
      </div>
      <div class="stat-card">
        <div class="label">Lifetime searches</div>
        <div class="value">${w.total}</div>
      </div>
    </div>`;
}

async function tabUsage() {
  const u = await api('/api/usage');
  const t = u.totals, td = u.today, d = u.documents, w = u.web_search;
  return `
    <h3 class="section-title">Your usage</h3>
    <div class="stats-grid">
      <div class="stat-card"><div class="label">Chats</div><div class="value">${t.chats}</div></div>
      <div class="stat-card"><div class="label">Messages</div><div class="value">${t.messages}</div><div class="sub">${td.messages} today</div></div>
      <div class="stat-card"><div class="label">Tokens in</div><div class="value">${t.tokens_in.toLocaleString()}</div><div class="sub">${td.tokens_in.toLocaleString()} today</div></div>
      <div class="stat-card"><div class="label">Tokens out</div><div class="value">${t.tokens_out.toLocaleString()}</div><div class="sub">${td.tokens_out.toLocaleString()} today</div></div>
      <div class="stat-card"><div class="label">Documents</div><div class="value">${d.count}</div><div class="sub">${d.chunks} chunks · ${fmtBytes(d.bytes)}</div></div>
      <div class="stat-card"><div class="label">Web searches</div><div class="value">${w.total}</div><div class="sub">${w.month} last 30d</div></div>
      <div class="stat-card"><div class="label">Compute time</div><div class="value">${t.total_seconds.toFixed(1)}s</div></div>
      <div class="stat-card"><div class="label">Avg tokens / msg</div><div class="value">${t.messages ? Math.round(t.tokens_out / t.messages) : 0}</div></div>
    </div>

    <h3 class="section-title subtle">By model</h3>
    ${u.by_model.length ? `
      <table style="width:100%; border-collapse:collapse; font:var(--t-caption);">
        <thead><tr style="text-align:left; color:var(--on-surface-faint);"><th style="padding:8px 0;">Model</th><th>Messages</th><th>Tokens in</th><th>Tokens out</th></tr></thead>
        <tbody>${u.by_model.map(r => `
          <tr style="border-top:1px solid var(--outline-variant);"><td style="padding:10px 0;">${escapeHtml(r.model)}</td><td>${r.msgs}</td><td>${r.tok_in.toLocaleString()}</td><td>${r.tok_out.toLocaleString()}</td></tr>`).join('')}
        </tbody>
      </table>` : `<p style="font:var(--t-caption); color:var(--on-surface-faint);">No data yet.</p>`}

    <h3 class="section-title subtle">By search provider</h3>
    ${w.by_provider && w.by_provider.length ? `
      <table style="width:100%; border-collapse:collapse; font:var(--t-caption);">
        <thead><tr style="text-align:left; color:var(--on-surface-faint);"><th style="padding:8px 0;">Provider</th><th>Searches</th></tr></thead>
        <tbody>${w.by_provider.map(r => `
          <tr style="border-top:1px solid var(--outline-variant);"><td style="padding:10px 0;">${escapeHtml(r.p)}</td><td>${r.c}</td></tr>`).join('')}
        </tbody>
      </table>` : `<p style="font:var(--t-caption); color:var(--on-surface-faint);">No searches yet.</p>`}`;
}

function fmtRelTime(ms) {
  if (!ms) return '—';
  const diff = Date.now() - ms;
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  return new Date(ms).toLocaleDateString();
}

async function tabUsers() {
  let data;
  try { data = await api('/api/admin/users'); }
  catch (err) { return `<p style="color:var(--err)">${escapeHtml(err.message)}</p>`; }
  const me = state.user;
  const rows = data.users.map(u => {
    const isMe = u.id === me.id;
    const initial = (u.display_name || u.username || '?')[0].toUpperCase();
    const avatarHtml = u.avatar
      ? `<img class="avatar sm" src="/api/settings/avatar/${u.id}?v=${encodeURIComponent(u.avatar)}" alt="">`
      : `<div class="avatar sm" data-letter="${escapeAttr(initial)}">${escapeHtml(initial)}</div>`;
    return `
      <tr data-uid="${u.id}">
        <td>${avatarHtml}</td>
        <td>
          <div style="font:var(--t-body)">${escapeHtml(u.display_name || u.username)}</div>
          <div style="font:var(--t-caption); color:var(--on-surface-faint)">@${escapeHtml(u.username)}${isMe ? ' · you' : ''}</div>
        </td>
        <td style="font:var(--t-caption); color:var(--on-surface-var)">${escapeHtml(u.email || '—')}</td>
        <td style="text-align:right; font-variant-numeric:tabular-nums">${u.chats}</td>
        <td style="text-align:right; font-variant-numeric:tabular-nums">${u.messages}</td>
        <td style="text-align:right; font-variant-numeric:tabular-nums">${u.documents}</td>
        <td style="text-align:right; font-variant-numeric:tabular-nums">${(u.tokens_in + u.tokens_out).toLocaleString()}</td>
        <td style="font:var(--t-caption); color:var(--on-surface-faint)">${fmtRelTime(u.last_active)}</td>
        <td style="text-align:center">
          <label class="switch" title="Admin">
            <input type="checkbox" data-admin-toggle="${u.id}" ${u.is_admin ? 'checked' : ''} ${isMe ? 'disabled' : ''}>
            <span class="track"></span>
            <span class="thumb"></span>
          </label>
        </td>
        <td style="text-align:right">
          <button class="btn btn-text danger" data-del-user="${u.id}" data-username="${escapeAttr(u.username)}" ${isMe ? 'disabled' : ''}>Delete</button>
        </td>
      </tr>`;
  }).join('');

  return `
    <h3 class="section-title">Users</h3>
    <p style="font:var(--t-caption); color:var(--on-surface-faint); margin:0 0 var(--sp-3);">All registered users and their activity. Toggle admin role or delete a user (which removes all their chats, messages, documents, and uploads).</p>
    <div style="margin-bottom: var(--sp-4);">
      <button class="btn btn-filled" id="create-user-btn">Create user account</button>
    </div>
    <div style="overflow-x:auto">
    <table class="table users-table">
      <thead><tr>
        <th></th><th>User</th><th>Email</th>
        <th style="text-align:right">Chats</th>
        <th style="text-align:right">Msgs</th>
        <th style="text-align:right">Docs</th>
        <th style="text-align:right">Tokens</th>
        <th>Last active</th>
        <th style="text-align:center">Admin</th>
        <th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    </div>`;
}

function tabAbout() {
  return `
    <div style="display:flex; align-items:flex-start; gap:var(--sp-4);">
      ${assistantAvatar('lg')}
      <div>
        <h3 class="section-title" style="margin-bottom:var(--sp-2);">${escapeHtml(brandName())}${state.brand?.product?.tagline ? ' — ' + escapeHtml(state.brand.product.tagline) : ''}</h3>
        <p style="font:var(--t-body); color:var(--on-surface-var); margin:0;">Powered by Ollama, ChromaDB, and your choice of search engine. Everything runs on your machine.</p>
        <ul style="margin-top:var(--sp-4); padding-left:var(--sp-5); color:var(--on-surface-var); font:var(--t-caption);">
          <li>Strategic, loyal persona — no beeps</li>
          <li>Streaming chat with markdown + syntax highlighting</li>
          <li>Document RAG (PDF / DOCX / TXT / MD / CSV / code)</li>
          <li>Multi-provider web search: Tavily, Brave, SearXNG, DuckDuckGo</li>
          <li>Multi-user with isolated chats &amp; documents</li>
        </ul>
      </div>
    </div>`;
}

function bindSettings(tab) {
  if (tab === 'general') {
    const temp = $('#modal-body input[data-set="temperature"]');
    if (temp) temp.addEventListener('input', e => $('#temp-v').textContent = parseFloat(e.target.value).toFixed(1));
    $$('#modal-body [data-set]').forEach(el => el.addEventListener('change', () => savePartial(el)));
  } else if (tab === 'profile') {
    setUserAvatar($('#prof-avatar'), state.user, state.user.display_name || state.user.username, 'xl');
    $('#avatar-pick').addEventListener('click', () => $('#avatar-input').click());
    $('#avatar-input').addEventListener('change', async (e) => {
      const f = e.target.files?.[0]; if (!f) return;
      const fd = new FormData(); fd.append('file', f);
      try {
        const res = await fetch('/api/settings/avatar', { method: 'POST', body: fd, headers: { 'X-Requested-With': 'fetch' }, credentials: 'include' });
        if (!res.ok) { const t = await res.text(); throw new Error(t || 'upload failed'); }
        state.user = await res.json();
        setUserAvatar($('#prof-avatar'), state.user, state.user.display_name || state.user.username, 'xl');
        renderUser();
        $('#avatar-remove').hidden = false;
        $('#avatar-pick').textContent = 'Replace';
        toast('Avatar updated', 'ok');
      } catch (err) { toast(err.message || 'Upload failed', 'err'); }
      finally { e.target.value = ''; }
    });
    $('#avatar-remove').addEventListener('click', async () => {
      try {
        state.user = await api('/api/settings/avatar', { method: 'DELETE' });
        setUserAvatar($('#prof-avatar'), state.user, state.user.display_name || state.user.username, 'xl');
        renderUser();
        $('#avatar-remove').hidden = true;
        $('#avatar-pick').textContent = 'Upload image';
        toast('Avatar removed', 'ok');
      } catch (err) { toast(err.message, 'err'); }
    });
    const saveAccent = async (hex) => {
      const value = hex || null;
      applyAccent(value);
      $$('#accent-swatches .swatch').forEach(b => b.classList.toggle('selected',
        (b.dataset.accent || '').toLowerCase() === (value || '').toLowerCase()));
      try {
        state.settings = await api('/api/settings', { method: 'PATCH', body: JSON.stringify({ accent: value }) });
      } catch (err) { toast(err.message, 'err'); }
    };
    $$('#accent-swatches .swatch').forEach(b => b.addEventListener('click', () => saveAccent(b.dataset.accent || null)));
    const saveSidebar = async (hex) => {
      const value = hex || null;
      applySidebarColor(value);
      $$('#sidebar-swatches .swatch').forEach(b => b.classList.toggle('selected',
        (b.dataset.sidebar || '').toLowerCase() === (value || '').toLowerCase()));
      try {
        state.settings = await api('/api/settings', { method: 'PATCH', body: JSON.stringify({ sidebar_color: value }) });
      } catch (err) { toast(err.message, 'err'); }
    };
    $$('#sidebar-swatches .swatch').forEach(b => b.addEventListener('click', () => saveSidebar(b.dataset.sidebar || null)));
    const cpick = $('#sidebar-color-input');
    if (cpick) {
      // Live preview as the user drags; persist on commit (change).
      cpick.addEventListener('input',  (e) => applySidebarColor(e.target.value));
      cpick.addEventListener('change', (e) => saveSidebar(e.target.value));
    }
    $('#prof-save').addEventListener('click', async () => {
      try {
        state.user = await api('/api/settings/profile', { method: 'PATCH', body: JSON.stringify({
          username: $('#prof-username').value,
          display_name: $('#prof-name').value, email: $('#prof-email').value }) });
        renderUser(); toast('Profile saved', 'ok');
      } catch (err) { toast(err.message, 'err'); }
    });
    $('#pw-save').addEventListener('click', async () => {
      try {
        await api('/api/settings/password', J({ current_password: $('#pw-cur').value, new_password: $('#pw-new').value }));
        $('#pw-cur').value = ''; $('#pw-new').value = ''; toast('Password updated', 'ok');
      } catch (err) { toast(err.message, 'err'); }
    });
  } else if (tab === 'models') {
    $$('#modal-body [data-info]').forEach(b => b.addEventListener('click', () => openPullDialog(b.dataset.info)));
    $$('#modal-body [data-del]').forEach(b => b.addEventListener('click', async () => {
      const name = b.dataset.del;
      if (!confirm(`Delete model "${name}"?\n\nThis frees disk space and cannot be undone. You can re-pull it later.`)) return;
      try { await api(`/api/models/${encodeURIComponent(name)}`, { method: 'DELETE' });
        await refreshModels(); renderSettingsBody('models'); toast(`Deleted ${name}`, 'ok');
      } catch (err) { toast(err.message, 'err'); }
    }));
    $$('#modal-body [data-unload]').forEach(b => b.addEventListener('click', async () => {
      const name = b.dataset.unload;
      b.disabled = true; b.textContent = 'Unloading…';
      try {
        await api('/api/models/unload', J({ name }));
        renderSettingsBody('models'); toast(`Unloaded ${modelLabel(name)}`, 'ok');
      } catch (err) { toast(err.message, 'err'); b.disabled = false; b.textContent = 'Unload'; }
    }));
    $('#unload-all')?.addEventListener('click', async (e) => {
      if (!confirm('Unload all models from memory?\n\nThis frees RAM/VRAM. Models will reload on next use.')) return;
      e.target.disabled = true; e.target.textContent = 'Unloading…';
      try {
        const r = await api('/api/models/unload', J({ all: true }));
        renderSettingsBody('models'); toast(`Unloaded ${(r.unloaded || []).length} model(s)`, 'ok');
      } catch (err) { toast(err.message, 'err'); }
    });
    $('#restart-ollama')?.addEventListener('click', async (e) => {
      if (!confirm('Restart the Ollama service?\n\nThis forcibly kills the Ollama container to release stuck memory. All in-flight requests will fail. The service comes back automatically (a few seconds).')) return;
      e.target.disabled = true; e.target.textContent = 'Restarting…';
      try {
        await api('/api/models/restart-service', { method: 'POST' });
        // Give Ollama a moment to come back before we refresh the loaded list.
        await new Promise(r => setTimeout(r, 2000));
        renderSettingsBody('models'); toast('Ollama service restarted', 'ok');
      } catch (err) { toast(err.message, 'err'); e.target.disabled = false; e.target.textContent = 'Restart Ollama service'; }
    });
    $('#pull-open')?.addEventListener('click', () => openPullDialog());
  } else if (tab === 'embeddings') {
    bindEmbeddings();
  } else if (tab === 'web') {
    $$('#modal-body [data-pick]').forEach(row => row.addEventListener('click', async (e) => {
      if (e.target.closest('a') || e.target.closest('input')) return;
      const id = row.dataset.pick;
      const p = state.providers.find(x => x.id === id);
      if (!p) return;
      if (!p.configured) { toast(`${p.label} needs configuration`, 'err'); return; }
      try {
        state.settings = await api('/api/settings', { method: 'PATCH', body: JSON.stringify({ search_provider: id }) });
        await refreshProviders(); renderSettingsBody('web'); toast(`Switched to ${p.label}`, 'ok');
      } catch (err) { toast(err.message, 'err'); }
    }));
    $$('#modal-body [data-set]').forEach(el => el.addEventListener('change', async () => {
      await savePartial(el);
      await refreshProviders();
      renderSettingsBody('web');
    }));
  } else if (tab === 'users') {
    $('#create-user-btn').addEventListener('click', openCreateUserDialog);
    $$('#modal-body [data-admin-toggle]').forEach(cb => cb.addEventListener('change', async () => {
      const uid = cb.dataset.adminToggle;
      const wasChecked = cb.checked;
      try {
        await api(`/api/admin/users/${uid}`, { method: 'PATCH', body: JSON.stringify({ is_admin: wasChecked }) });
        toast(wasChecked ? 'Promoted to admin' : 'Removed admin', 'ok');
      } catch (err) {
        cb.checked = !wasChecked;
        toast(err.message, 'err');
      }
    }));
    $$('#modal-body [data-del-user]').forEach(btn => btn.addEventListener('click', async () => {
      const uid = btn.dataset.delUser;
      const username = btn.dataset.username;
      const confirmed = prompt(`Type "${username}" to permanently delete this user and ALL their chats, messages, documents, and uploads. This cannot be undone.`);
      if (confirmed !== username) { if (confirmed !== null) toast('Username did not match', 'err'); return; }
      try {
        await api(`/api/admin/users/${uid}`, { method: 'DELETE' });
        toast(`Deleted ${username}`, 'ok');
        renderSettingsBody('users');
      } catch (err) { toast(err.message, 'err'); }
    }));
  }
}

async function savePartial(el) {
  const key = el.dataset.set;
  let v = el.type === 'checkbox' ? el.checked
       : el.type === 'number'    ? Number(el.value)
       : el.type === 'range'     ? Number(el.value)
       : el.value;
  if (v === '') v = null;
  try {
    state.settings = await api('/api/settings', { method: 'PATCH', body: JSON.stringify({ [key]: v }) });
    if (key === 'theme') applyTheme(v);
    if (key === 'use_rag_default') $('#toggle-rag').checked = !!v;
    if (key === 'use_web_default') $('#toggle-web').checked = !!v;
  } catch (err) { toast(err.message, 'err'); }
}

// ---------- Utils ----------
function scrollToBottom() { const el = $('#messages'); el.scrollTop = el.scrollHeight; }
function escapeHtml(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function escapeAttr(s) { return escapeHtml(s); }

// Spinner keyframes used by status pill
const sheet = document.createElement('style');
sheet.textContent = `@keyframes spin { to { transform: rotate(360deg); } }`;
document.head.appendChild(sheet);

init();
