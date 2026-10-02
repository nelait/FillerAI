/* The product page. Static apart from this: a browser that is already signed
 * in is offered the app rather than the login, and the version is shown. */
'use strict';

(async function product() {
  const nav = document.getElementById('nav');
  const onScroll = () => nav.classList.toggle('is-scrolled', window.scrollY > 8);
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  try {
    const meta = await (await fetch('/api/meta')).json();
    if (meta.version) document.getElementById('version').textContent = `v${meta.version}`;
    // Without accounts there is nobody to sign in as: the app is the door.
    if (meta.signed_in || !meta.accounts) {
      document.querySelectorAll('[data-enter]').forEach((link) => {
        link.href = '/app';
        if (link.textContent.trim().startsWith('Sign in')) {
          link.innerHTML = link.innerHTML.replace(/Sign in( to AIrForms)?/, 'Open AIrForms');
        }
      });
    }
  } catch (error) {
    // The page works without it; the links still go to the login.
  }
})();
