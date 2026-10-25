// 轮询（技术选型 12.5）：页面在后台时暂停、请求不重叠、出错后逐步放慢，恢复后回到原间隔。

export function poll(task, { interval = 3000, maxInterval = 15000, onError, onRecover } = {}) {
  let timer = null;
  let running = false;
  let delay = interval;
  let failing = false;
  let stopped = false;

  async function run() {
    clearTimeout(timer);
    timer = null;
    if (running || stopped || document.hidden) return;
    running = true;
    try {
      await task();
      if (failing) {
        failing = false;
        onRecover?.();
      }
      delay = interval;
    } catch (err) {
      failing = true;
      delay = Math.min(delay * 2, maxInterval);
      onError?.(err);
    } finally {
      running = false;
      if (!stopped && !document.hidden) timer = setTimeout(run, delay);
    }
  }

  function onVisibility() {
    if (document.hidden) {
      clearTimeout(timer);
      timer = null;
    } else {
      run(); // 切回来立刻刷新一次
    }
  }

  document.addEventListener("visibilitychange", onVisibility);
  run();
  return {
    now: run,
    stop() {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    },
  };
}
