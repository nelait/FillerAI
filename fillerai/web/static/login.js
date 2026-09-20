/* The sign-in page.
 *
 * Its own file rather than a corner of app.js, because it is the one page a
 * signed-out browser is allowed to load: everything app.js does needs a
 * session, and shipping all of it to somebody who has not signed in yet
 * would be both slower and stranger.
 *
 * Two forms, one at a time. The second only appears when the password was
 * handed over by an administrator, which is the one case where signing in
 * successfully is not the end of the conversation.
 */
'use strict';

const $ = (id) => document.getElementById(id);
let csrf = '';

async function post(path, body, token) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['X-FillerAI-Token'] = token;
  const response = await fetch(path, {
    method: 'POST', headers, body: JSON.stringify(body || {}),
  });
  let payload = {};
  try { payload = await response.json(); } catch (error) { /* shown below */ }
  if (!response.ok) {
    throw new Error(payload.error || `that did not work (${response.status})`);
  }
  return payload;
}

function show(node, message) {
  node.textContent = message;
  node.hidden = false;
}

function askForNewPassword() {
  $('signIn').hidden = true;
  $('changePassword').hidden = false;
  $('newPassword').focus();
}

$('signIn').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('signInGo');
  const error = $('signInError');
  error.hidden = true;
  button.disabled = true;
  button.textContent = 'Signing in...';
  try {
    const result = await post('/api/auth/login', {
      username: $('username').value.trim(),
      password: $('password').value,
      agent: navigator.userAgent.slice(0, 120),
    });
    csrf = result.csrf;
    if (result.must_change) {
      askForNewPassword();
    } else {
      window.location.href = '/';
      return;
    }
  } catch (failure) {
    show(error, failure.message);
    $('password').value = '';
    $('password').focus();
  } finally {
    button.disabled = false;
    button.textContent = 'Sign in';
  }
});

$('changePassword').addEventListener('submit', async (event) => {
  event.preventDefault();
  const error = $('changeError');
  const button = $('changeGo');
  error.hidden = true;
  if ($('newPassword').value !== $('newPasswordAgain').value) {
    show(error, 'those two do not match');
    return;
  }
  button.disabled = true;
  button.textContent = 'Saving...';
  try {
    await post('/api/auth/password', {
      current: $('password').value,
      new: $('newPassword').value,
    }, csrf);
    window.location.href = '/';
  } catch (failure) {
    show(error, failure.message);
  } finally {
    button.disabled = false;
    button.textContent = 'Save it and carry on';
  }
});

(async function start() {
  try {
    const meta = await (await fetch('/api/meta')).json();
    $('version').textContent = `v${meta.version}`;
    // Somebody already signed in who landed here has a password to change;
    // the server sends everyone else back to the app.
    if (meta.signed_in && meta.user && meta.user.must_change) {
      $('foot').textContent = `Signed in as ${meta.user.username}.`;
    }
  } catch (error) {
    show($('signInError'), 'the FillerAI server is not reachable');
  }
})();
