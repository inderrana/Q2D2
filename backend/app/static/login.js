const tabs = document.querySelectorAll('.seg');
const form = document.getElementById('form');
const errorBox = document.getElementById('error');
const btnLabel = document.getElementById('btn-label');
const submitBtn = document.getElementById('submit');
let mode = 'login';

function setMode(m) {
  mode = m;
  tabs.forEach(t => {
    const active = t.dataset.tab === m;
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  document.querySelectorAll('.field-register').forEach(el => el.classList.toggle('hidden', m !== 'register'));
  btnLabel.textContent = m === 'login' ? 'Sign in' : 'Create account';
  form.password.autocomplete = m === 'login' ? 'current-password' : 'new-password';
  errorBox.classList.add('hidden');
}

tabs.forEach(t => t.addEventListener('click', () => setMode(t.dataset.tab)));

async function init() {
  try {
    const me = await fetch('/api/auth/me');
    if (me.ok) { location.replace('/'); return; }
    const s = await fetch('/api/auth/status').then(r => r.json());
    if (s.user_count === 0) {
      setMode('register');
      document.getElementById('subtitle').textContent = 'Create the first account to begin.';
    } else {
      setMode('login');
    }
  } catch {
    setMode('login');
  }
}
init();

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  errorBox.classList.add('hidden');
  submitBtn.disabled = true;
  btnLabel.innerHTML = '<span class="spinner"></span>';
  const formData = Object.fromEntries(new FormData(form));
  
  const data = {};
  
  if (mode === 'login') {
    // Login: send username_or_email and password
    data.username_or_email = formData.username;
    data.password = formData.password;
  } else {
    // Register: send username, password, email, and optional display_name
    data.username = formData.username;
    data.password = formData.password;
    data.email = formData.email || '';
    if (formData.display_name) {
      data.display_name = formData.display_name;
    }
  }
  
  try {
    const r = await fetch(`/api/auth/${mode === 'login' ? 'login' : 'register'}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'fetch'},
      body: JSON.stringify(data),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({detail: 'request failed'}));
      let msg;
      if (typeof err.detail === 'string') {
        msg = err.detail;
      } else if (Array.isArray(err.detail) && err.detail.length) {
        msg = err.detail.map(d => d.msg).filter(Boolean).join('; ') || `HTTP ${r.status}`;
      } else {
        msg = `HTTP ${r.status}`;
      }
      throw new Error(msg);
    }
    location.replace('/');
  } catch (err) {
    errorBox.textContent = err.message;
    errorBox.classList.remove('hidden');
    submitBtn.disabled = false;
    btnLabel.textContent = mode === 'login' ? 'Sign in' : 'Create account';
  }
});
