export async function resetOdysseusLocalCache(options = {}) {
  const status = options.statusElement || document.getElementById(options.statusElementId || 'status');
  const say = (text) => {
    if (typeof options.onStatus === 'function') options.onStatus(text);
    if (status) status.textContent = text;
  };
  const swPath = options.swPath || '/static/sw.js';
  const cachePrefix = options.cachePrefix || 'odysseus-';
  const shouldRedirect = options.redirect !== false;
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
    let workersRemoved = 0;
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      const odysseusRegs = regs.filter(isOdysseusWorker);
      await Promise.all(
        odysseusRegs.map((reg) => reg.unregister().then((ok) => {
          if (ok) workersRemoved += 1;
          return ok;
        }).catch(() => false))
      );
    }

    say('Deleting Odysseus Cache Storage entries…');
    let cachesRemoved = 0;
    if ('caches' in window) {
      const keys = await caches.keys();
      const odysseusKeys = keys.filter((key) => key.startsWith(cachePrefix));
      await Promise.all(
        odysseusKeys.map((key) => caches.delete(key).then((ok) => {
          if (ok) cachesRemoved += 1;
          return ok;
        }).catch(() => false))
      );
    }

    const doneText = `Reset complete: ${workersRemoved} service worker registration(s), ${cachesRemoved} Odysseus cache(s). Reloading fresh…`;
    say(doneText);
    if (shouldRedirect) {
      const target = new URL(options.targetPath || '/', window.location.origin);
      target.searchParams.set('cache-reset', Date.now().toString());
      window.location.replace(target.toString());
    }
    return { ok: true, workersRemoved, cachesRemoved };
  } catch (err) {
    console.error('[sw-reset] failed', err);
    say('Reset hit an error. See console, then manually reload Odysseus.');
    return { ok: false, error: err };
  }
}

if (window.location.pathname.endsWith('/sw-reset.html')) {
  resetOdysseusLocalCache();
}
