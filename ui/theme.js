/* Theme toggle, shared by every page.
   The stored choice is applied by a tiny inline script in each <head> — doing it here
   would flash the wrong palette for a frame before this file loads. */
(() => {
  const btn = document.getElementById('theme');
  if (!btn) return;

  const dark = () => document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === 'dark'
    : matchMedia('(prefers-color-scheme: dark)').matches;

  // The button names what pressing it does, not what is currently showing.
  const paint = () => {
    const next = dark() ? 'Light' : 'Dark';
    btn.textContent = next;
    btn.setAttribute('aria-label', `Switch to ${next.toLowerCase()} mode`);
  };

  btn.addEventListener('click', () => {
    const next = dark() ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('fg-theme', next);
    paint();
  });

  // Keep following the OS until the user has chosen for themselves.
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (!localStorage.getItem('fg-theme')) paint();
  });

  paint();
})();
