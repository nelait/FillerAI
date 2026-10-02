/* The docs access-code page. One field, one POST, then the page asked for. */
'use strict';

(function gate() {
  const form = document.getElementById('gate');
  if (!form) return;
  const error = document.getElementById('gateError');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button');
    button.disabled = true;
    error.hidden = true;
    try {
      const response = await fetch('/api/docs/unlock', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: document.getElementById('code').value }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error || `refused (${response.status})`);
      // The cookie is set; the same address now answers with the page.
      window.location.reload();
    } catch (problem) {
      error.textContent = problem.message;
      error.hidden = false;
      button.disabled = false;
      document.getElementById('code').select();
    }
  });
})();
