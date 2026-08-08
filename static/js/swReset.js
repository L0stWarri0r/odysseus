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
    let workerFailures = 0;
    if ('serviceWorker' in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      const odysseusRegs = regs.filter(isOdysseusWorker);
      const workerResults = await Promise.allSettled(
        odysseusRegs.map((reg) => reg.unregister())
      );
      for (const result of workerResults) {
        if (result.status === 'fulfilled' && result.value) workersRemoved += 1;
        else workerFailures += 1;
      }
    }

    say('Deleting Odysseus Cache Storage entries…');
    let cachesRemoved = 0;
    let cacheFailures = 0;
    if ('caches' in window) {
      const keys = await caches.keys();
      const odysseusKeys = keys.filter((key) => key.startsWith(cachePrefix));
      const cacheResults = await Promise.allSettled(
        odysseusKeys.map((key) => caches.delete(key))
      );
      for (const result of cacheResults) {
        if (result.status === 'fulfilled' && result.value) cachesRemoved += 1;
        else cacheFailures += 1;
      }
    }

    const failed = workerFailures + cacheFailures;
    if (failed > 0) {
      const failText = `Reset partial: removed ${workersRemoved} worker(s) and ${cachesRemoved} cache(s); ${failed} step(s) failed. Reload manually if the app still looks stale.`;
      say(failText);
      return {
        ok: false,
        partial: true,
        workersRemoved,
        cachesRemoved,
        workerFailures,
        cacheFailures,
      };
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
