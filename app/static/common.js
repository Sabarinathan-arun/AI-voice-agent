/* Shared helpers for the logged-in pages (dashboard, campaign). */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const label = (s) => esc(String(s || '').replace(/[_-]/g, ' '));
const rupees = (paise) => new Intl.NumberFormat('en-IN', {style: 'currency', currency: 'INR'}).format((paise || 0) / 100);
const minutes = (seconds) => { const s = Math.round(seconds || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
const when = (iso) => iso ? new Date(iso).toLocaleString('en-IN', {day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit'}) : '';

/* fetch wrapper: sends people to the login page when their session has ended. */
async function call(path, opts = {}) {
  const res = await fetch(path, {credentials: 'same-origin', ...opts});
  if (res.status === 401) {
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
    throw new Error('Please log in.');
  }
  return res;
}

async function errText(res) {
  try {
    const d = await res.json();
    if (typeof d.detail === 'string') return d.detail;
    if (Array.isArray(d.detail)) return d.detail.map(e => e.msg).join(' ');
    return JSON.stringify(d.detail);
  } catch (e) { return res.statusText; }
}

function msg(el, text, ok) { $(el).textContent = text; $(el).className = 'msg ' + (ok ? 'ok' : 'error'); }

/* Header: name, balance chip and log out. */
let pricePerMinute = 1000, lowBalance = 2000;
function showBalance(paise) {
  const chip = $('balance-chip');
  if (!chip) return;
  chip.textContent = rupees(paise);
  chip.classList.toggle('low', paise < lowBalance);
}
let configRequest = null;  // shared, so the page and the header fetch /api/config only once
function getConfig() {
  configRequest = configRequest || fetch('/api/config').then(r => r.json());
  return configRequest;
}
async function initHeader() {
  const meRequest = call('/api/me');  // in parallel with the config
  try {
    const cfg = await getConfig();
    pricePerMinute = cfg.price_per_minute_paise; lowBalance = cfg.low_balance_paise;
  } catch (e) {}
  const me = await (await meRequest).json();
  if ($('user-name')) $('user-name').textContent = me.name;
  showBalance(me.balance_paise);
  $('logout').onclick = async () => { await fetch('/api/auth/logout', {method: 'POST'}); location.href = '/'; };
  return me;
}
