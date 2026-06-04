async function resetOdysseusLocalCache() {
  const status = document.getElementById('status');
  const say = (text) => { if (status) status.textContent = text; };
  const swPath = '/static/sw.js';
  const isOdysseusWorker = (reg) => {
    const urls = [reg.active, reg.waiting, reg.installing]
      .map((worker) => worker && worker.scriptURL)
      .filter(Boolean);
    return urls.some((url) => {
      try { return new URL(url).pathname === swPath; } catch (_) { return false; }
    });
  };

  try {
    say('Unregistering Odysseus service worker…');
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(
        regs
          .filter(isOdysseusWorker)
          .map((reg) => reg.unregister().catch(() => false))
      );
    }

    say('Deleting Odysseus Cache Storage entries…');
    if ('caches' in window) {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((key) => key.startsWith('odysseus-'))
          .map((key) => caches.delete(key).catch(() => false))
      );
    }

    say('Done. Reloading Odysseus with fresh files…');
    const target = new URL('/', window.location.origin);
    target.searchParams.set('cache-reset', Date.now().toString());
    window.location.replace(target.toString());
  } catch (err) {
    console.error('[sw-reset] failed', err);
    say('Reset hit an error. See console, then manually reload Odysseus.');
  }
}

resetOdysseusLocalCache();
