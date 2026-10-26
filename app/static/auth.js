/* Sign-in helpers shared by the sign-in and reset-password pages.
   Supabase Auth handles accounts, passwords, email confirmation, password reset and Google sign-in;
   once signed in, the access token is exchanged once for the app's own session cookie. */
const $ = (id) => document.getElementById(id);

const asked = new URLSearchParams(location.search).get('next') || sessionStorage.getItem('intuvora_next') || '';
const nextPage = /^\/(?![\/\\])/.test(asked) ? asked : '/dashboard';  // same-site paths only

function show(id, text, ok) {
  const el = $(id);
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'ok' : 'error');
}

/* Errors Supabase puts in the address after an expired or invalid email link. */
function linkError() {
  const params = new URLSearchParams(location.hash.slice(1) || location.search.slice(1));
  const description = params.get('error_description');
  return description ? description.replace(/\+/g, ' ') : '';
}

async function supabaseClient() {
  const cfg = await (await fetch('/api/config')).json();
  if (!cfg.supabase_url || !cfg.supabase_anon_key) throw new Error('Sign-in is not configured yet (SUPABASE_URL / SUPABASE_ANON_KEY).');
  const client = window.supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key, {
    auth: {flowType: 'implicit', detectSessionInUrl: true, persistSession: true, autoRefreshToken: false},
  });
  return {client, cfg};
}

let finishing = false;
/* Turn a Supabase session into an Intuvora session, then go to the dashboard (or the page that was asked for). */
async function finishSignIn(client, session, msgId) {
  if (finishing || !session) return;
  finishing = true;
  const res = await fetch('/api/auth/session', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({access_token: session.access_token}),
  });
  await client.auth.signOut({scope: 'local'});  // only the app's own session is kept from here on
  if (res.ok) {
    sessionStorage.removeItem('intuvora_next');
    location.href = nextPage;
    return;
  }
  finishing = false;
  const d = await res.json().catch(() => ({}));
  show(msgId, typeof d.detail === 'string' ? d.detail : 'Sign-in failed. Please try again.');
}
