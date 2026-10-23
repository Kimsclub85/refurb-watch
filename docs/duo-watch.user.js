// ==UserScript==
// @name         듀오 자급제 감시 (쿠팡·네이버)
// @namespace    https://kimsclub85.github.io/refurb-watch/
// @version      1.0.2
// @description  쿠팡 듀오 8옵션 재입고, 네이버 애플 브랜드스토어 3차 예약 상태를 실제 브라우저 탭에서 감시하고 ntfy로 알린다.
// @match        https://pages.coupang.com/m/176175*
// @match        https://brand.naver.com/applestore/products/13771279736*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        unsafeWindow
// @connect      ntfy.sh
// @run-at       document-idle
// @updateURL    https://kimsclub85.github.io/refurb-watch/duo-watch.user.js
// @downloadURL  https://kimsclub85.github.io/refurb-watch/duo-watch.user.js
// ==/UserScript==

/*
 * 탭 두 개를 띄워 두면 된다.
 *   쿠팡  https://pages.coupang.com/m/176175
 *   네이버 https://brand.naver.com/applestore/products/13771279736
 * 쿠팡은 페이지가 쓰는 POST /api/products 를 탭 안에서 부르고, 네이버는 페이지를
 * 다시 열어 __PRELOADED_STATE__ 를 읽는다. 간격은 성공이 이어지면 바닥값까지 좁히고,
 * 실패하면 두 배씩 늘린다. ntfy 토픽은 처음 한 번 물어보고 이 브라우저에만 저장한다.
 */
(function () {
  'use strict';

  const REDIRECT = 'https://kimsclub85.github.io/refurb-watch/duo.html';
  // @grant 를 쓰면 스크립트는 격리된 window 에서 돈다. 페이지 변수는 unsafeWindow 로 읽는다.
  const PAGE = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
  const HEARTBEAT_MS = 6 * 3600 * 1000;

  const COUPANG = {
    pid: 9729252969,
    // vendorItemId → 옵션 이름. 🎯는 우선 타깃.
    items: {
      96039350773: '🎯 스타 화이트 256GB',
      96039350775: '나이트 스카이 256GB',
      96039350776: '스타 화이트 512GB',
      96039350779: '나이트 스카이 512GB',
      96039350774: '스타 화이트 1TB',
      96039350780: '나이트 스카이 1TB',
      96039350777: '스타 화이트 2TB',
      96039350778: '나이트 스카이 2TB',
    },
    // 2026-10-24 실측: 1초 간격 40회, 2초 간격 60회 모두 200.
    pace: { start: 3000, floor: 1500, max: 300000 },
    reloadEveryMs: 4 * 60 * 1000, // 숨은 탭의 강한 타이머 스로틀링(5분)을 피하려고 문서를 새로 연다.
  };

  const NAVER = {
    id: '13771279736',
    name: '네이버 3차 예약 256 스타화이트',
    // 2026-10-24 실측: 약 2.2초 간격 46회 연속 통과.
    pace: { start: 5000, floor: 3000, max: 300000 },
    nearOpenMs: 3 * 60 * 1000, // 오픈 시각 ±3분은 바닥 간격으로.
    remindBeforeMs: 5 * 60 * 1000,
  };

  // ---------- 공통 ----------

  const now = () => Date.now();
  const kst = (t) => new Date(t).toLocaleString('ko-KR', { timeZone: 'Asia/Seoul', hour12: false });
  const jitter = (ms) => Math.round(ms * (0.85 + Math.random() * 0.3));
  const load = (k, d) => { const v = GM_getValue(k); return v === undefined ? d : v; };
  const save = (k, v) => GM_setValue(k, v);

  // 토픽은 감시 페이지 주소 끝에 #duo-topic=<토픽> 을 붙여 한 번 열면 저장된다.
  // prompt() 는 무인 실행 중인 탭을 멈추게 하므로 메뉴에서 직접 고를 때만 쓴다.
  const topic = () => load('topic', '');

  function topicFromHash() {
    const m = location.hash.match(/duo-topic=([A-Za-z0-9_-]{1,64})/);
    if (!m) return false;
    save('topic', m[1]);
    history.replaceState(null, '', location.pathname + location.search);
    return true;
  }

  function ntfy(title, message, { click, priority = 4, tags = [] } = {}) {
    const t = topic();
    if (!t) { console.warn('[duo] ntfy 토픽 없음', title, message); return; }
    GM_xmlhttpRequest({
      method: 'POST',
      url: 'https://ntfy.sh/',
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ topic: t, title, message, priority, tags, ...(click ? { click } : {}) }),
      onerror: (e) => console.warn('[duo] ntfy 실패', e),
    });
  }

  GM_registerMenuCommand('ntfy 토픽 바꾸기', () => {
    const t = (prompt('ntfy 토픽 이름', topic()) || '').trim();
    if (/^[A-Za-z0-9_-]{1,64}$/.test(t)) save('topic', t);
  });
  GM_registerMenuCommand('테스트 알림 보내기', () =>
    ntfy('듀오 감시 테스트', location.hostname + '에서 보냄', { click: REDIRECT + '?s=c&v=96039350773', priority: 3 }));

  // 성공이 20번 이어질 때마다 10%씩 좁히고, 실패하면 두 배로 늘린다.
  function makePace(key, cfg) {
    const st = load(key, { iv: cfg.start, ok: 0, fail: 0, alerted: false });
    return {
      get iv() { return st.iv; },
      get fail() { return st.fail; },
      ok() {
        st.fail = 0; st.ok += 1;
        if (st.ok % 20 === 0) st.iv = Math.max(cfg.floor, Math.round(st.iv * 0.9));
        if (st.alerted) { st.alerted = false; ntfy('듀오 감시 복구', key + ' 다시 정상', { priority: 2 }); }
        save(key, st);
      },
      bad(reason) {
        st.ok = 0; st.fail += 1;
        st.iv = Math.min(cfg.max, st.iv * 2);
        if (st.fail >= 3 && !st.alerted) {
          st.alerted = true;
          ntfy('듀오 감시 막힘', `${key}: ${reason} (연속 ${st.fail}회, 간격 ${Math.round(st.iv / 1000)}초로 늘림)`, { priority: 3, tags: ['warning'] });
        }
        save(key, st);
      },
    };
  }

  function badge(text) {
    let el = document.getElementById('duo-watch-badge');
    if (!el) {
      el = document.createElement('div');
      el.id = 'duo-watch-badge';
      el.style.cssText = 'position:fixed;z-index:2147483647;right:8px;bottom:8px;padding:6px 10px;border-radius:8px;'
        + 'background:#111;color:#fff;font:12px/1.4 system-ui,sans-serif;opacity:.85;pointer-events:none;white-space:pre';
      document.body.appendChild(el);
    }
    el.textContent = (topic() ? '' : '⚠ ntfy 토픽 없음 (#duo-topic=… 로 설정)\n') + text;
  }

  function heartbeat(key, line) {
    const last = load(key + ':hb', 0);
    if (now() - last > HEARTBEAT_MS) {
      save(key + ':hb', now());
      if (last) ntfy('듀오 감시 살아있음', line, { priority: 1 });
    }
  }

  // ---------- 쿠팡 ----------

  async function coupangCheck() {
    const vids = Object.keys(COUPANG.items).map(Number);
    const r = await fetch('/api/products', {
      method: 'POST',
      credentials: 'include',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ itemIds: vids, isMobile: true, isPIBased: false }),
    });
    if (r.status !== 200) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const snap = {};
    for (const v of vids) {
      const it = d[v];
      if (!it || !it.soldoutArea) throw new Error('응답에 ' + v + ' 없음');
      snap[v] = {
        soldout: !!it.soldoutArea.soldout,
        date: ((it.promisedDeliveryDateArea || {}).contents || []).map((c) => c.text).join('').trim(),
        price: (it.priceArea || {}).salesPrice || '',
      };
    }
    return snap;
  }

  function coupangDiff(prev, cur) {
    const open = [];
    for (const [v, c] of Object.entries(cur)) {
      const p = prev[v];
      const name = COUPANG.items[v];
      const click = `${REDIRECT}?s=c&v=${v}&p=${COUPANG.pid}`;
      if (!p) continue; // 첫 관측은 조용히 시드
      if (p.soldout && !c.soldout) {
        open.push(name);
        ntfy('🚨 쿠팡 듀오 재고: ' + name, `${c.price}원 · ${c.date || '도착일 미표시'}\n탭하면 쿠팡 앱으로`, { click, priority: 5, tags: ['rotating_light'] });
      } else if (!p.soldout && c.soldout) {
        ntfy('쿠팡 듀오 다시 품절: ' + name, kst(now()), { priority: 2 });
      } else if (p.date !== c.date && c.date) {
        ntfy('쿠팡 듀오 도착일 변경: ' + name, `${p.date || '-'} → ${c.date}`, { click, priority: 3 });
      }
    }
    return open;
  }

  function runCoupang() {
    const pace = makePace('coupang', COUPANG.pace);
    const startedAt = now();
    let checks = 0;

    async function tick() {
      try {
        const cur = await coupangCheck();
        const prev = load('coupang:snap', {});
        const open = coupangDiff(prev, cur);
        save('coupang:snap', cur);
        pace.ok();
        checks += 1;
        const inStock = Object.entries(cur).filter(([, c]) => !c.soldout).map(([v]) => COUPANG.items[v]);
        badge(`듀오 감시 · 쿠팡\n${kst(now())}\n재고: ${inStock.length ? inStock.join(', ') : '전부 품절'}\n간격 ${(pace.iv / 1000).toFixed(1)}초 · ${checks}회`);
        heartbeat('coupang', `쿠팡 정상, 재고 ${inStock.length}/8`);
        if (open.length) document.title = '🚨 ' + open.join(', ');
      } catch (e) {
        pace.bad(String(e.message || e));
        badge(`듀오 감시 · 쿠팡\n오류: ${e.message || e}\n${Math.round(pace.iv / 1000)}초 뒤 재시도`);
      }
      if (now() - startedAt > COUPANG.reloadEveryMs) {
        location.reload();
        return;
      }
      setTimeout(tick, jitter(pace.iv));
    }
    tick();
  }

  // ---------- 네이버 ----------

  async function naverRead() {
    for (let i = 0; i < 25; i++) {
      if (/에러|시스템오류/.test(document.title)) throw new Error('에러 페이지(429 추정)');
      const x = PAGE.__PRELOADED_STATE__ && PAGE.__PRELOADED_STATE__.simpleProductForDetailPage;
      if (x && x.productStatusType) {
        return {
          pst: x.productStatusType,
          stock: x.stockQuantity,
          start: x.saleStartDate || '',
          end: x.saleEndDate || '',
          ordered: x.preOrder ? x.preOrder.orderQty : null,
          name: x.name,
        };
      }
      await new Promise((r) => setTimeout(r, 200));
    }
    throw new Error('상태 없음');
  }

  function naverDiff(prev, cur) {
    const click = `${REDIRECT}?s=n&id=${NAVER.id}`;
    const startAt = Date.parse(cur.start);
    if (startAt && cur.pst === 'WAIT' && startAt - now() < NAVER.remindBeforeMs && startAt > now()) {
      const key = 'naver:remind:' + cur.start;
      if (!load(key, false)) {
        save(key, true);
        ntfy('⏰ 네이버 듀오 곧 오픈', `${kst(startAt)} 오픈 · 재고 ${cur.stock} · 1인 1개\n지금 앱 열어둬`, { click, priority: 5, tags: ['alarm_clock'] });
      }
    }
    if (!prev.pst) return; // 첫 관측은 조용히 시드
    if (cur.pst === 'SALE' && cur.stock > 0 && (prev.pst !== 'SALE' || prev.stock <= 0)) {
      ntfy('🚨 네이버 듀오 구매 가능', `${cur.name}\n재고 ${cur.stock} · 탭하면 네이버플러스 스토어 앱으로`, { click, priority: 5, tags: ['rotating_light'] });
    } else if (cur.pst === 'SALE' && cur.stock > prev.stock && prev.pst === 'SALE') {
      ntfy('🚨 네이버 듀오 재고 늘어남', `${prev.stock} → ${cur.stock}`, { click, priority: 5, tags: ['rotating_light'] });
    } else if (prev.pst !== cur.pst) {
      ntfy('네이버 듀오 상태 변경', `${prev.pst} → ${cur.pst} · 재고 ${cur.stock}`, { click, priority: 3 });
    }
    if (prev.start !== cur.start || prev.end !== cur.end) {
      ntfy('네이버 듀오 판매기간 변경', `${kst(Date.parse(cur.start))} ~ ${kst(Date.parse(cur.end))}`, { click, priority: 4 });
    }
  }

  async function runNaver() {
    const pace = makePace('naver', NAVER.pace);
    let cur = null;
    try {
      cur = await naverRead();
      naverDiff(load('naver:snap', {}), cur);
      save('naver:snap', cur);
      save('naver:checks', load('naver:checks', 0) + 1);
      pace.ok();
      heartbeat('naver', `네이버 정상, ${cur.pst} · 재고 ${cur.stock}`);
    } catch (e) {
      pace.bad(String(e.message || e));
    }
    let wait = pace.iv;
    const startAt = cur && Date.parse(cur.start);
    if (startAt && pace.fail === 0 && Math.abs(startAt - now()) < NAVER.nearOpenMs) wait = NAVER.pace.floor;
    wait = jitter(wait);
    badge(cur
      ? `듀오 감시 · 네이버\n${kst(now())}\n${cur.pst} · 재고 ${cur.stock}\n다음 ${(wait / 1000).toFixed(1)}초 · ${load('naver:checks', 0)}회`
      : `듀오 감시 · 네이버\n읽기 실패 · ${Math.round(wait / 1000)}초 뒤 재시도`);
    setTimeout(() => {
      const u = new URL(location.href);
      u.searchParams.set('_', String(now()));
      location.replace(u.toString());
    }, wait);
  }

  // ---------- 시작 ----------

  // 토픽이 새로 들어오거나 바뀐 뒤 사이트별 첫 실행은 폰에 확인 푸시.
  const site = location.hostname === 'pages.coupang.com' ? 'coupang' : 'naver';
  if (topicFromHash()) save(site + ':started', '');
  if (topic() && load(site + ':started', '') !== topic()) {
    save(site + ':started', topic());
    ntfy('듀오 감시 시작', `${site === 'coupang' ? '쿠팡' : '네이버'} 탭에서 감시 시작 · ${kst(now())}`, { priority: 3 });
  }

  if (site === 'coupang') runCoupang();
  else runNaver();
})();
