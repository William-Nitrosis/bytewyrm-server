const navToggle = document.querySelector('.nav-toggle');
const nav = document.querySelector('.site-nav');

if (navToggle && nav) {
  navToggle.addEventListener('click', () => {
    const isOpen = nav.classList.toggle('open');
    navToggle.setAttribute('aria-expanded', String(isOpen));
  });

  nav.querySelectorAll('a').forEach((link) => {
    link.addEventListener('click', () => {
      nav.classList.remove('open');
      navToggle.setAttribute('aria-expanded', 'false');
    });
  });
}

document.querySelectorAll('[data-copy]').forEach((button) => {
  button.addEventListener('click', async () => {
    const text = button.dataset.copy;
    const original = button.textContent;

    try {
      await navigator.clipboard.writeText(text);
      button.textContent = 'Copied';
      button.classList.add('copied');
      setTimeout(() => {
        button.textContent = original;
        button.classList.remove('copied');
      }, 1400);
    } catch {
      button.textContent = 'Select & copy';
      setTimeout(() => {
        button.textContent = original;
      }, 1400);
    }
  });
});
