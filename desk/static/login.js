(() => {
  const mode = document.body.dataset.mode;
  const form = document.querySelector('#login-form');
  const password = document.querySelector('#password');
  const confirmation = document.querySelector('#confirmation');
  const button = document.querySelector('#submit');
  const error = document.querySelector('#login-error');
  const deskName = document.querySelector('meta[name="desk-name"]')?.content || 'Support Desk';
  if (mode === 'setup') {
    document.title = `Choose a password · ${deskName}`;
    document.querySelector('#login-title').textContent = 'Make yourself at home.';
    document.querySelector('#login-description').textContent = 'Choose a password for your private desk. Use at least 7 characters.';
    form.action = '/auth/setup';
    password.autocomplete = 'new-password';
    password.minLength = confirmation.minLength = 7;
    document.querySelector('#confirmation-field').hidden = false;
    confirmation.required = true;
    button.textContent = 'Set password & enter';
  } else if (mode === 'locked') {
    document.querySelector('#login-title').textContent = 'One quick setup.';
    document.querySelector('#login-description').textContent = `Open the ${deskName} on your Mac to choose its password first.`;
    form.hidden = true;
  }
  document.querySelector('#show-password').addEventListener('click', event => {
    const show = password.type === 'password';
    password.type = confirmation.type = show ? 'text' : 'password';
    event.currentTarget.textContent = show ? 'Hide' : 'Show';
    event.currentTarget.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    event.currentTarget.setAttribute('aria-pressed', String(show));
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled) return;
    error.hidden = true;
    if (mode === 'setup' && password.value !== confirmation.value) {
      error.textContent = 'The passwords do not match.';
      error.hidden = false;
      confirmation.focus();
      return;
    }
    const label = button.textContent;
    button.disabled = true;
    button.textContent = mode === 'setup' ? 'Setting your password…' : 'Signing in…';
    try {
      const response = await fetch(form.action, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: password.value, confirmation: confirmation.value, remember: document.querySelector('#remember').checked }),
      });
      const body = await response.json();
      if (response.ok) { location.replace('/'); return; }
      error.textContent = body.message || 'Sign-in did not work. Try again.';
      error.hidden = false;
      if (response.status === 401) { password.value = ''; password.focus(); }
      if (response.status === 409) { location.replace('/login'); return; }
    } catch {
      error.textContent = 'The desk could not be reached. Check your connection and try again.';
      error.hidden = false;
    }
    button.disabled = false;
    button.textContent = label;
  });
})();
