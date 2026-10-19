// ==UserScript==
// @name         WB iPhone 16 — мониторинг цен
// @namespace    https://github.com/bhyserpro/WB-Parser
// @version      2.1.0
// @description  Раз в час собирает выдачу Wildberries в этой вкладке и отправляет в локальный receiver.py
// @match        https://www.wildberries.ru/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-start
// @inject-into  page
// @noframes
// ==/UserScript==

/*
 * Работает только во вкладке, открытой по адресу с #wb-monitor:
 *   https://www.wildberries.ru/catalog/0/search.aspx?search=iPhone%2016#wb-monitor
 * Остальные вкладки WB скрипт не трогает. Выключить: открыть адрес с #wb-monitor-off.
 *
 * Скрипт не делает собственных запросов к API WB. Выдачу запрашивает сам сайт —
 * при открытии страницы поиска, прокрутке вниз и переходе на &page=N, — а скрипт
 * только читает эти ответы и пересылает их в receiver.py.
 */
(function () {
  'use strict';

  const pageWindow = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
  const CONFIG = Object.assign({
    query: 'iPhone 16',
    // Свой адрес выдачи с фильтрами (категория «Смартфоны», бренд Apple и т.п.):
    // настройте фильтры на сайте и скопируйте адрес страницы сюда. null — поиск по query.
    searchUrl: null,
    intervalMin: 60,          // период сбора
    maxPages: 50,             // максимум страниц выдачи (по ~100 товаров)
    delayMinMs: 1000,         // пауза между прокрутками/переходами 1–3 с
    delayMaxMs: 3000,
    waitResponseMs: 30000,    // сколько ждать ответа сайта после загрузки страницы
    scrollStallMs: 15000,     // сколько крутить без новых ответов, прежде чем перейти на &page=N
    retryMin: 5,              // повтор после ошибки
    reloadDelayMs: [5000, 10000],  // пауза перед перезагрузкой после отказа WB
    runTimeoutMin: 30,        // предел длительности одного сбора
    challengeAlertMin: 10,    // сообщить, если проверка WB висит дольше
    receiver: 'http://127.0.0.1:8765',
  }, pageWindow.WB_MONITOR_CONFIG || {});

  const FLAG = 'wbMonitorTab';
  const KEEP_FIELDS = ['id', 'name', 'brand', 'supplier', 'supplierId', 'reviewRating', 'rating',
    'feedbacks', 'nmFeedbacks', 'colors', 'salePriceU', 'priceU'];

  if (location.hash === '#wb-monitor-off') { sessionStorage.removeItem(FLAG); return; }
  if (location.hash === '#wb-monitor') sessionStorage.setItem(FLAG, '1');
  if (sessionStorage.getItem(FLAG) !== '1') return;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const rand = (a, b) => a + Math.random() * (b - a);
  const fmtTime = (ts) => new Date(ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  const log = (...a) => console.info('[WB-мониторинг]', ...a);

  // ---------------------------------------------------------------- состояние

  /** Текущий сбор переживает перезагрузки вкладки: хранится в GM-хранилище. */
  const loadRun = () => GM_getValue('run', null);
  const saveRun = (run) => GM_setValue('run', run);

  // -------------------------------------------- перехват ответов самого сайта

  const captured = [];         // ответы поиска, пойманные за эту загрузку страницы
  let onCapture = () => {};

  function trimProduct(p) {
    const out = {};
    for (const k of KEEP_FIELDS) if (p[k] !== undefined) out[k] = p[k];
    out.sizes = (p.sizes || []).map((s) => ({ name: s.name, origName: s.origName, price: s.price }));
    return out;
  }

  function isSearchUrl(url) {
    return url.includes('/__internal/search/') && url.includes('/search?');
  }

  function handleResponse(rawUrl, status, text) {
    try {
      const url = new URL(String(rawUrl), location.href);
      const entry = { page: Number(url.searchParams.get('page') || '1'), status: Number(status) };
      if (entry.status === 200) {
        const data = JSON.parse(String(text));
        const products = data.products || (data.data && data.data.products) || [];
        entry.products = products.map(trimProduct);
      }
      captured.push(entry);
      log(`ответ сайта: страница ${entry.page}, HTTP ${entry.status}, товаров ${entry.products ? entry.products.length : '—'}`);
      onCapture(entry);
    } catch (e) {
      log('не удалось разобрать ответ поиска', e);
    }
  }

  /** Обёртки fetch и XHR страницы; в режиме content-скрипта Firefox — через exportFunction. */
  function installHooks() {
    const w = pageWindow;
    const exp = (fn) => (typeof exportFunction === 'function' ? exportFunction(fn, w) : fn);

    const origFetch = w.fetch;
    w.fetch = exp(function (input) {
      const promise = origFetch.apply(this, arguments);
      try {
        const url = String(typeof input === 'string' ? input : (input && input.url) || '');
        if (isSearchUrl(url)) {
          promise.then((r) => r.clone().text().then((t) => handleResponse(url, r.status, t))).catch(() => {});
        }
      } catch (e) { /* не мешаем сайту */ }
      return promise;
    });

    const proto = w.XMLHttpRequest.prototype;
    const origOpen = proto.open;
    const origSend = proto.send;
    proto.open = exp(function (method, url) {
      try { this.__wbMonitorUrl = String(url); } catch (e) { /* ignore */ }
      return origOpen.apply(this, arguments);
    });
    proto.send = exp(function () {
      const xhr = this;
      try {
        if (xhr.__wbMonitorUrl && isSearchUrl(xhr.__wbMonitorUrl)) {
          xhr.addEventListener('load', exp(() => {
            let text = '';
            try {
              text = xhr.responseType === '' || xhr.responseType === 'text'
                ? xhr.responseText : JSON.stringify(xhr.response);
            } catch (e) { /* ignore */ }
            handleResponse(xhr.__wbMonitorUrl, xhr.status, text);
          }));
        }
      } catch (e) { /* ignore */ }
      return origSend.apply(this, arguments);
    });
  }

  installHooks();

  // ---------------------------------------------------------------- статус

  let badge;
  function status(text, kind = 'info') {
    if (!document.body) return;
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

  /** Адрес выдачи: свой (с фильтрами из CONFIG.searchUrl) или поиск по query. */
  function searchUrl(page) {
    const url = CONFIG.searchUrl
      ? new URL(CONFIG.searchUrl, location.origin)
      : new URL('/catalog/0/search.aspx', location.origin);
    if (!CONFIG.searchUrl) url.searchParams.set('search', CONFIG.query);
    url.hash = '';
    if (page > 1) url.searchParams.set('page', String(page));
    else url.searchParams.delete('page');
    return url.toString();
  }

  /** Адрес без page и якоря, с упорядоченными параметрами — для сравнения. */
  function normalizeUrl(href) {
    const url = new URL(href, location.origin);
    url.hash = '';
    url.searchParams.delete('page');
    url.searchParams.sort();
    return url.pathname + '?' + url.searchParams.toString().toLowerCase();
  }

  function currentPage() {
    return Number(new URLSearchParams(location.search).get('page') || '1');
  }

  function go(url) {
    log('переход:', url);
    location.href = url;
  }

  /** Карточки товаров на странице — запасной источник, если ответы API не перехватились. */
  function parseCards() {
    const digits = (el) => Number(((el && el.textContent) || '').replace(/\D/g, '')) || null;
    let nodes = [...document.querySelectorAll('[data-nm-id]')];
    if (!nodes.length) {
      nodes = [...document.querySelectorAll('a[href*="/detail.aspx"]')]
        .map((a) => a.closest('article, li, [class*="card"]') || a);
    }
    const out = [];
    const seen = new Set();
    for (const card of nodes) {
      const link = card.matches('a[href*="/detail.aspx"]') ? card : card.querySelector('a[href*="/detail.aspx"]');
      const fromLink = ((link && link.getAttribute('href')) || '').match(/catalog\/(\d+)\//);
      const id = Number(card.getAttribute('data-nm-id')) || Number(fromLink && fromLink[1]);
      if (!id || seen.has(id)) continue;
      const text = (sel) => ((card.querySelector(sel) || {}).textContent || '').trim();
      const name = (text('.product-card__name') ||
        (card.querySelector('[aria-label]') || { getAttribute: () => '' }).getAttribute('aria-label') ||
        (card.querySelector('img[alt]') || {}).alt || '').replace(/^[\s/]+/, '').trim();
      let price = digits(card.querySelector('ins, .price__lower-price, [class*="lower-price"]'));
      let basic = digits(card.querySelector('del, [class*="old-price"]'));
      if (!price) {
        const all = [...card.textContent.matchAll(/(\d[\d\s   ]*)\s*₽/g)]
          .map((m) => Number(m[1].replace(/\D/g, ''))).filter(Boolean);
        if (all.length) { price = Math.min(...all); basic = basic || Math.max(...all); }
      }
      if (!name || !price) continue;
      seen.add(id);
      const rating = parseFloat(text('.address-rate-mini, [class*="rating"]').replace(',', '.'));
      out.push({
        id,
        name,
        brand: text('.product-card__brand'),
        reviewRating: Number.isFinite(rating) && rating <= 5 ? rating : undefined,
        feedbacks: digits(card.querySelector('.product-card__count, [class*="count"]')) || undefined,
        sizes: [{ price: { product: price * 100, basic: basic && basic > price ? basic * 100 : undefined } }],
        source: 'dom',
      });
    }
    return out;
  }

  /** Переносит пойманные ответы API и карточки со страницы в сбор. */
  function absorb(run) {
    for (const entry of captured) {
      if (!entry.products) continue;
      if (!entry.products.length) { run.ended = true; continue; }
      if (entry.page <= CONFIG.maxPages && !run.pages[entry.page]) run.pages[entry.page] = entry.products;
    }
    for (const p of parseCards()) if (!run.dom[p.id]) run.dom[p.id] = p;
    saveRun(run);
    return { api: Object.keys(run.pages).length, dom: Object.keys(run.dom).length };
  }

  const limitReached = (c) => c.api >= CONFIG.maxPages || c.dom >= CONFIG.maxPages * 100;

  async function finish(run, note) {
    const pages = Object.keys(run.pages).map(Number).sort((a, b) => a - b)
      .map((n) => ({ page: n, products: run.pages[n] }));
    // карточки, которых нет в ответах API (их receiver склеит по артикулу)
    const apiIds = new Set(pages.flatMap((p) => p.products.map((x) => x.id)));
    const domOnly = Object.values(run.dom).filter((p) => !apiIds.has(p.id));
    if (domOnly.length) pages.push({ page: 'dom', products: domOnly });
    if (!pages.length) return fail(run, 'не удалось получить ни одного товара');
    status(`отправка в receiver.py…`);
    const res = await send('/ingest', { query: CONFIG.query, pages, page_url: location.href });
    saveRun(null);
    GM_setValue('failures', 0);
    GM_setValue('lastRun', run.startedAt);
    const source = Object.keys(run.pages).length
      ? `ответов API ${Object.keys(run.pages).length}` + (domOnly.length ? `, с карточек ${domOnly.length}` : '')
      : `с карточек ${domOnly.length} (без продавца)`;
    const text = `${fmtTime(run.startedAt)}: ${source}\niPhone ${res.items}, снижений ${res.drops}` +
      (note ? `\n${note}` : '');
    status(scheduleNext(run.startedAt + CONFIG.intervalMin * 60000 + rand(-60000, 60000) - Date.now(), text), 'ok');
  }

  async function fail(run, message) {
    saveRun(null);
    const failures = GM_getValue('failures', 0) + 1;
    GM_setValue('failures', failures);
    log('ошибка:', message);
    if (failures >= 2) send('/error', { message: `${message} (ошибок подряд: ${failures})` }).catch(() => {});
    const retryMs = CONFIG.retryMin * 60000 * Math.min(failures, 6);
    GM_setValue('lastRun', Date.now() - CONFIG.intervalMin * 60000 + retryMs);
    status(scheduleNext(retryMs, `ошибка: ${message}`), 'error');
  }

  /** Обработка одной загрузки страницы во время сбора. */
  async function step(run) {
    const expected = run.expect;
    status(`сбор: жду выдачу (страница ${expected})…`);
    const t0 = Date.now();
    while (Date.now() - t0 < CONFIG.waitResponseMs && !captured.some((e) => e.products) && !parseCards().length) {
      await sleep(500);
    }
    const searchCodes = [...new Set(captured.filter((e) => !e.products).map((e) => e.status))];
    if (!captured.some((e) => e.products) && !parseCards().length) {
      run.navFailures = (run.navFailures || 0) + 1;
      saveRun(run);
      const why = searchCodes.length
        ? `WB отклоняет запросы самого сайта: HTTP ${searchCodes.join(', ')}`
        : 'на странице нет ни товаров, ни ответов поиска';
      if (run.navFailures > 3) return fail(run, why);
      await sleep(rand(CONFIG.reloadDelayMs[0], CONFIG.reloadDelayMs[1]));
      return go(searchUrl(expected));   // перезагрузка — сайт пройдёт проверку заново
    }
    run.navFailures = 0;
    const firstIds = parseCards().slice(0, 5).map((p) => p.id);
    if (expected > 1) {
      const apiWrongPage = captured.some((e) => e.products) && !captured.some((e) => e.page === expected);
      const domRepeat = !captured.some((e) => e.products) && firstIds.length && firstIds.every((id) => run.dom[id]);
      if (apiWrongPage || domRepeat) {
        // сайт открыл не ту страницу — значит, &page=N он не поддерживает
        absorb(run);
        return finish(run, `сайт не открывает &page=${expected}, собрано что удалось`);
      }
    }

    // прокрутка: сайт сам подгружает следующие товары
    let counts = absorb(run);
    let lastNew = Date.now();
    while (!run.ended && !limitReached(counts) && Date.now() - lastNew < CONFIG.scrollStallMs) {
      pageWindow.scrollTo(0, document.documentElement.scrollHeight);
      status(`сбор: ответов API ${counts.api}, карточек ${counts.dom}, прокрутка…`);
      await sleep(rand(CONFIG.delayMinMs, CONFIG.delayMaxMs));
      const now = absorb(run);
      if (now.api > counts.api || now.dom > counts.dom) lastNew = Date.now();
      counts = now;
    }
    if (!captured.length && !run.loggedNoApi) {
      const seenInNetwork = performance.getEntriesByType('resource').filter((e) => isSearchUrl(e.name)).length;
      log(`перехват не поймал ответов API (в Network запросов поиска: ${seenInNetwork}) — беру данные с карточек`);
      run.loggedNoApi = true;
    }
    if (run.ended || limitReached(counts)) return finish(run);

    // прокрутка больше ничего не даёт — открываем следующую страницу по адресу
    const apiPages = Object.keys(run.pages).map(Number);
    run.expect = apiPages.length ? Math.max(...apiPages) + 1 : currentPage() + 1;
    saveRun(run);
    await sleep(rand(CONFIG.delayMinMs, CONFIG.delayMaxMs));
    return go(searchUrl(run.expect));
  }

  // ----------------------------------------------------------- расписание

  let nextTimer;
  function scheduleNext(ms, note) {
    const at = Date.now() + Math.max(ms, 1000);
    clearTimeout(nextTimer);
    log(`следующий запуск в ${fmtTime(at)}`);
    nextTimer = setTimeout(() => main(), at - Date.now());
    return `${note}\nследующий запуск в ${fmtTime(at)}`;
  }

  function isChallengePage() {
    return !!document.querySelector('script[src*="/__wbaas/challenges/"]');
  }

  function main() {
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
    const run = loadRun();
    if (run) {
      if (Date.now() - run.startedAt > CONFIG.runTimeoutMin * 60000) {
        return fail(run, `сбор не уложился в ${CONFIG.runTimeoutMin} мин`);
      }
      return step(run).catch((e) => fail(run, e.message));
    }
    const intervalMs = CONFIG.intervalMin * 60000;
    const last = GM_getValue('lastRun', 0);
    const wait = last + intervalMs - Date.now();
    if (wait > 60000) {
      status(scheduleNext(wait, last ? `последний запуск в ${fmtTime(last)}` : 'ожидание'));
      return;
    }
    const fresh = { startedAt: Date.now(), expect: 1, pages: {}, dom: {}, navFailures: 0 };
    saveRun(fresh);
    if (onFirstSearchPage()) return step(fresh).catch((e) => fail(fresh, e.message));
    status('сбор: открываю выдачу…');
    go(searchUrl(1));   // свежая загрузка, чтобы перехват поймал первую страницу
  }

  /** Открыта ли уже первая страница нужной выдачи (тогда начинаем без перезагрузки). */
  function onFirstSearchPage() {
    return currentPage() === 1 && normalizeUrl(location.href) === normalizeUrl(searchUrl(1));
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', main);
  else main();
})();
