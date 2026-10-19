// ==UserScript==
// @name         WB iPhone 16 — мониторинг цен
// @namespace    https://github.com/bhyserpro/WB-Parser
// @version      1.0.0
// @description  Раз в час собирает выдачу Wildberries в этой вкладке и отправляет в локальный receiver.py
// @match        https://www.wildberries.ru/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// @noframes
// ==/UserScript==

/*
 * Работает только во вкладке, открытой по адресу с #wb-monitor:
 *   https://www.wildberries.ru/catalog/0/search.aspx?search=iPhone%2016#wb-monitor
 * Остальные вкладки WB скрипт не трогает. Выключить: открыть адрес с #wb-monitor-off.
 */
(function () {
  'use strict';

  const pageWindow = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
  const CONFIG = Object.assign({
    query: 'iPhone 16',
    intervalMin: 60,          // период сбора
    maxPages: 100,            // максимум страниц выдачи
    delayMinMs: 1000,         // пауза между страницами 1–3 с
    delayMaxMs: 3000,
    retryMin: 5,              // повтор после ошибки
    challengeAlertMin: 10,    // сообщить, если проверка WB висит дольше
    receiver: 'http://127.0.0.1:8765',
  }, pageWindow.WB_MONITOR_CONFIG || {});

  const FLAG = 'wbMonitorTab';
  const DEFAULT_API = 'https://www.wildberries.ru/__internal/search/exactmatch/ru/common/v18/search' +
    '?ab_testing=false&appType=1&curr=rub&dest=-1257786&lang=ru&locale=ru&resultset=catalog' +
    '&sort=popular&spp=30&suppressSpellcheck=false';
  const KEEP_FIELDS = ['id', 'name', 'brand', 'supplier', 'supplierId', 'reviewRating', 'rating',
    'feedbacks', 'nmFeedbacks', 'colors', 'salePriceU', 'priceU'];

  if (location.hash === '#wb-monitor-off') { sessionStorage.removeItem(FLAG); return; }
  if (location.hash === '#wb-monitor') sessionStorage.setItem(FLAG, '1');
  if (sessionStorage.getItem(FLAG) !== '1') return;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const rand = (a, b) => a + Math.random() * (b - a);
  const fmtTime = (ts) => new Date(ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });

  class TokenError extends Error {}

  // ---------------------------------------------------------------- статус

  let badge;
  function status(text, kind = 'info') {
    if (!badge) {
      badge = document.createElement('div');
      badge.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:2147483647;max-width:320px;' +
        'padding:8px 12px;border-radius:10px;font:12px/1.4 system-ui,sans-serif;color:#fff;' +
        'box-shadow:0 2px 10px rgba(0,0,0,.25);white-space:pre-line;pointer-events:none';
      document.body.appendChild(badge);
    }
    badge.style.background = { info: '#6b3fa0', ok: '#2e7d32', error: '#c62828' }[kind];
    badge.textContent = 'WB-мониторинг\n' + text;
  }

  // ------------------------------------------------------------- приёмник

  function getSecret() {
    let secret = GM_getValue('secret', '');
    if (!secret) {
      secret = (prompt('WB-мониторинг: вставьте RECEIVER_SECRET (его печатает receiver.py при запуске)') || '').trim();
      if (secret) GM_setValue('secret', secret);
    }
    return secret;
  }

  function send(path, body) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST',
        url: CONFIG.receiver + path,
        headers: { 'Content-Type': 'application/json', 'X-Auth-Token': getSecret() },
        data: JSON.stringify(body),
        timeout: 120000,
        onload: (r) => {
          let data = {};
          try { data = JSON.parse(r.responseText); } catch (e) { /* не JSON */ }
          if (r.status === 401) GM_setValue('secret', '');  // спросим секрет заново
          if (r.status >= 200 && r.status < 300) resolve(data);
          else reject(new Error(`receiver: HTTP ${r.status} ${data.error || ''}`.trim()));
        },
        onerror: () => reject(new Error('receiver.py не отвечает — он запущен?')),
        ontimeout: () => reject(new Error('receiver.py не ответил за 2 минуты')),
      });
    });
  }

  // ------------------------------------------------------------------ сбор

  /** URL поиска, которым пользовался сам сайт (с вашим регионом), или стандартный. */
  async function findApiTemplate() {
    for (let i = 0; i < 30; i++) {
      const entry = performance.getEntriesByType('resource')
        .map((e) => e.name)
        .find((n) => n.includes('/__internal/search/') && n.includes('/search?'));
      if (entry) return entry;
      await sleep(1000);
    }
    return DEFAULT_API;
  }

  function pageUrl(template, page) {
    const url = new URL(template, location.origin);
    url.searchParams.set('query', CONFIG.query);
    url.searchParams.set('page', String(page));
    return url.toString();
  }

  /** Запрос от имени страницы — с её cookie, как это делает сам сайт. */
  async function fetchJson(url, retries = 4) {
    const pageFetch = pageWindow.fetch.bind(pageWindow);
    for (let attempt = 0; ; attempt++) {
      let resp = null;
      let reason;
      try {
        resp = await pageFetch(url);
      } catch (e) {
        reason = `сеть: ${e.message}`;
      }
      if (resp) {
        if ([401, 403, 498].includes(resp.status)) throw new TokenError(`WB отклонил запрос: HTTP ${resp.status}`);
        if (resp.ok) return JSON.parse(await resp.text());
        reason = `HTTP ${resp.status}`;
        if (![429, 500, 502, 503, 504].includes(resp.status)) throw new Error(reason);
      }
      if (attempt >= retries) throw new Error(`${reason}, попытки кончились`);
      await sleep(2 ** (attempt + 1) * 1000 + rand(0, 1000));
    }
  }

  function trimProduct(p) {
    const out = {};
    for (const k of KEEP_FIELDS) if (p[k] !== undefined) out[k] = p[k];
    out.sizes = (p.sizes || []).map((s) => ({ name: s.name, origName: s.origName, price: s.price }));
    return out;
  }

  async function collect() {
    const template = await findApiTemplate();
    const pages = [];
    for (let page = 1; page <= CONFIG.maxPages; page++) {
      const data = await fetchJson(pageUrl(template, page));
      const products = data.products || (data.data && data.data.products) || [];
      status(`страница ${page}: товаров ${products.length}`);
      console.info(`[WB-мониторинг] страница ${page}: товаров ${products.length}`);
      if (!products.length) break;
      pages.push({ products: products.map(trimProduct) });
      if (page < CONFIG.maxPages) await sleep(rand(CONFIG.delayMinMs, CONFIG.delayMaxMs));
    }
    return pages;
  }

  // ----------------------------------------------------------- расписание

  function scheduleReload(ms, note) {
    const at = Date.now() + ms;
    GM_setValue('nextRun', at);
    console.info(`[WB-мониторинг] следующий запуск в ${fmtTime(at)}`);
    setTimeout(() => location.reload(), ms);
    return `${note}\nследующий запуск в ${fmtTime(at)}`;
  }

  async function run() {
    const intervalMs = CONFIG.intervalMin * 60000;
    const started = Date.now();
    GM_setValue('lastRun', started);
    status('сбор выдачи…');
    try {
      const pages = await collect();
      const res = await send('/ingest', { query: CONFIG.query, pages, page_url: location.href });
      GM_setValue('failures', 0);
      const note = `${fmtTime(started)}: страниц ${pages.length}, iPhone ${res.items}, снижений ${res.drops}`;
      status(scheduleReload(intervalMs + rand(-60000, 60000), note), 'ok');
    } catch (e) {
      const failures = GM_getValue('failures', 0) + 1;
      GM_setValue('failures', failures);
      console.error('[WB-мониторинг]', e);
      // протух токен — перезагрузка вкладки даст сайту пройти проверку заново
      const retryMs = (e instanceof TokenError ? 1 : CONFIG.retryMin) * 60000 * Math.min(failures, 6);
      if (failures >= 3 || !(e instanceof TokenError)) {
        send('/error', { message: `${e.message} (ошибок подряд: ${failures})` }).catch(() => {});
      }
      GM_setValue('lastRun', Date.now() - intervalMs + retryMs);
      status(scheduleReload(retryMs, `ошибка: ${e.message}`), 'error');
    }
  }

  function isChallengePage() {
    return !!document.querySelector('script[src*="/__wbaas/challenges/"]');
  }

  function start() {
    if (isChallengePage()) {
      // сайт сам перезагрузится после проверки; если висит долго — вероятно, капча
      setTimeout(() => {
        if (isChallengePage()) {
          send('/error', { message: 'WB показывает проверку/капчу во вкладке мониторинга — откройте её и пройдите проверку' })
            .catch(() => {});
        }
      }, CONFIG.challengeAlertMin * 60000);
      return;
    }
    const intervalMs = CONFIG.intervalMin * 60000;
    const due = GM_getValue('lastRun', 0) + intervalMs;
    const wait = due - Date.now();
    if (wait <= 60000) run();
    else status(scheduleReload(wait, `последний запуск в ${fmtTime(GM_getValue('lastRun', 0))}`));
  }

  start();
})();
