// ==UserScript==
// @name         Bot Q OI (Quantium → webhook)
// @namespace    samuel.qoi
// @version      0.3
// @description  Corre la máquina Q OI en vivo sobre SPX y QQQ y manda las órdenes de MES y MNQ a un webhook (TradersPost). Empieza en modo papel.
// @match        https://quantium.global/app/*
// @grant        GM_xmlhttpRequest
// @connect      traderspost.io
// @connect      webhooks.traderspost.io
// @connect      *
// ==/UserScript==
// Motor de la máquina Q OI (misma lógica que qd/maquina.py), minuto a minuto, sin mirar el futuro.
// Entrada: un snapshot por minuto {t: Date, spot, flip, cw, pw, strikes:[{k, gf, oc, op, sc, sp, dx, ch}]}
// Salida: eventos {type:'entry'|'exit'|'armed'|'cancel', ...}. Todas las distancias en puntos del índice (SPX o QQQ).
(function (root) {
  'use strict';
  const nyMin = (t) => { const d = new Date(t.toLocaleString('en-US', { timeZone: 'America/New_York' })); return d.getHours() * 60 + d.getMinutes(); };
  const median = (a) => { const b = a.filter(x => isFinite(x)).sort((x, y) => x - y); if (!b.length) return NaN; const m = b.length >> 1; return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2; };
  const pct = (a, q) => { const b = a.filter(x => isFinite(x)).sort((x, y) => x - y); if (!b.length) return NaN; const i = (b.length - 1) * q, lo = Math.floor(i), hi = Math.ceil(i); return b[lo] + (b[hi] - b[lo]) * (i - lo); };
  const sign = (x) => (x > 0 ? 1 : x < 0 ? -1 : 0);

  function Engine(opts) {
    const sc = opts.sc || 1;                 // escala: 1 punto SPX = sc puntos del ticker
    const P = (x) => x * sc;                 // convierte distancias SPX al ticker
    const C = Object.assign({
      mg: 2.34e9, open: true, opd: 1.5, cht: 0.1,
      corr: { min: 3, ruptura: [30, 15], rebote: [20, 10], noTighten: true, maxMin: 180, A: [25, 15] },
      D: { brk: 5, sl: 12 }, E: { brk: 10, sl: 12 },
      pendingBlocks: false,                  // si una orden pendiente bloquea otras entradas
      noRepeat: true,                        // no volver a armar D o E en un nivel donde ya perdieron hoy
      skipRebMinus: false,                   // no operar el rebote sin vuelta (DEX y charm negativos); se activa en QQQ/SPY
    }, opts.cfg || {});
    const st = { hist: [], pos: null, pend: null, cool: -1e9, last: {}, n: -1, events: [], failed: {} };

    function majors(s, win, top, mg) {
      const near = s.strikes.filter(x => Math.abs(x.k - s.spot) <= P(win) && isFinite(x.gf));
      const thr = pct(s.strikes.map(x => Math.max(x.oc, x.op)), 0.7);
      return near.sort((a, b) => Math.abs(b.gf) - Math.abs(a.gf)).slice(0, top).filter(x => Math.max(x.oc, x.op) >= thr && (mg == null || Math.abs(x.gf) >= mg)).map(x => x.k);
    }
    function sigVerde(s, e, d) {
      const ks = majors(s, 60, 8, null).filter(k => (k - e) * d >= P(8) && (s.strikes.find(x => x.k === k) || {}).gf > 0);
      if (!ks.length) return null; const k = ks.reduce((a, b) => ((a - e) * d <= (b - e) * d ? a : b)); return k - d * P(1);
    }
    function openLeg(s, n, e, d, stop, mode, regla, k, tipo, info, leg) {
      const mm = nyMin(s.t); const sk = s.strikes.find(x => x.k === k) || {}; const dfx = (e - s.flip) / sc * d;
      const score = (mm < 645 ? 1 : 0) + (dfx > 20 ? 1 : 0) + (sk.ch < 0 ? 1 : 0); const run = score >= C.corr.min;
      let tr = mode === 'rebote' ? [20, 10] : mode === 'ruptura' ? [30, 15] : null;
      if (run && tr) tr = mode === 'ruptura' ? C.corr.ruptura : C.corr.rebote;
      const lg = { n0: n, t0: s.t, e, d, st: stop, st0: stop, mode, regla, k, tipo, info, leg, run, best: 0,
        dist: tr ? P(tr[1]) : null, act: tr ? P(tr[0]) : null, tg: tr ? ((run && C.corr.noTighten) ? null : sigVerde(s, e, d)) : null,
        tp: mode === 'A' ? P(20) : null, maxn: run ? C.corr.maxMin : 120, sar: false };
      if (run && mode === 'A' && C.corr.A) Object.assign(lg, { tp: null, dist: P(C.corr.A[1]), act: P(C.corr.A[0]), tg: null });
      return lg;
    }
    function emit(ev) { st.events.push(ev); return ev; }

    function manage(s, n, p, mm) {
      const pos = st.pos; const d = pos.d; pos.best = Math.max(pos.best, (p - pos.e) * d); let why = null, px = p;
      if ((p - pos.st) * d <= 0) { why = 'stop'; px = pos.st; }
      else if (pos.tp && (p - pos.e) * d >= pos.tp) { why = 'objetivo'; px = pos.e + d * pos.tp; }
      else if (n - pos.n0 >= pos.maxn) why = 'tiempo';
      else if (mm >= 958) why = 'cierre';
      if (!why && pos.dist) {
        if (pos.tg != null && (p - pos.tg) * d >= 0 && pos.dist !== P(3)) { pos.dist = P(3); pos.act = 0; emit({ type: 'tighten', t: s.t, pos }); }
        if (pos.best >= pos.act) { const ns = pos.e + d * (pos.best - pos.dist); if ((ns - pos.st) * d > 0) { pos.st = ns; emit({ type: 'trail', t: s.t, st: ns, pos }); } }
      }
      if (!why) return;
      const pts = (px - pos.e) * d;
      emit({ type: 'exit', t: s.t, why, px, pts, pos });
      if (pts <= 0 && (pos.regla === 'D' || pos.regla === 'E')) st.failed[pos.regla + pos.k] = 1;
      let nxt = null;
      if (why === 'stop' && pos.sar && pts <= 0 && mm < 958) {
        const b = -d; nxt = openLeg(s, n, pos.st, b, pos.k - b * P(8), 'ruptura', 'C', pos.k, pos.tipo, pos.info, 'ruptura');
        emit({ type: 'entry', t: s.t, pos: nxt });
      }
      st.pos = nxt; if (!nxt) st.cool = n;
    }

    function checkPending(s, n, p, mm) {
      const q = st.pend; if (!q) return;
      if (n - q.n0 > q.wait || mm >= 958 || (q.cancel != null && (p - q.cancel) * q.d <= 0)) { emit({ type: 'cancel', t: s.t, pend: q }); st.pend = null; return; }
      if ((p - q.trig) * q.d >= 0) {
        st.pend = null; if (st.pos) { emit({ type: 'cancel', t: s.t, pend: q, why: 'posición abierta' }); return; }
        st.pos = openLeg(s, n, q.trig, q.d, q.stop, 'ruptura', q.regla, q.k, q.tipo, q.info, q.leg);
        emit({ type: 'entry', t: s.t, pos: st.pos });
      }
    }

    function onMinute(s) {
      st.events = [];
      const p = s.spot; if (!isFinite(p)) return st.events;
      const mm = nyMin(s.t); if (mm >= 960) return st.events;
      st.n += 1; const n = st.n; st.hist.push(p); if (st.hist.length > 200) st.hist.shift();
      const H = st.hist; const prevAt = (len) => H.slice(Math.max(0, H.length - 1 - len), H.length - 1);
      if (st.pos && st.pos.n0 === n) return st.events;
      if (st.pos) { manage(s, n, p, mm); return st.events; }
      if (st.pend) { checkPending(s, n, p, mm); if (st.pos || (st.pend && C.pendingBlocks)) return st.events; }
      if (n === 0 || n - st.cool < 15 || mm >= 930 || (mm >= 780 && mm < 870) || mm < 570) return st.events;
      const L = new Map();
      if (isFinite(s.cw)) L.set(s.cw, 'call wall');
      if (isFinite(s.pw) && !L.has(s.pw)) L.set(s.pw, 'put wall');
      for (const k of majors(s, 40, 6, C.mg).slice(0, 2)) if (!L.has(k)) L.set(k, 'gex');
      const reg = p > s.flip ? 1 : -1;
      for (const [k, tipo] of L) {
        const sk = s.strikes.find(x => x.k === k); if (!sk || Math.abs(p - k) > P(1.5) || (k in st.last && n - st.last[k] < 15)) continue;
        let side;
        if (mm < 585 && H.length < 16) {
          if (!C.open || (tipo !== 'call wall' && tipo !== 'put wall') || Math.abs(p - k) > P(C.opd) || (k in st.last)) continue;
          side = tipo === 'call wall' ? -1 : 1;
        } else {
          const prev = prevAt(15); if (!prev.length || Math.max(...prev.map(v => Math.abs(v - k))) < P(8)) continue;
          side = sign(prev.reduce((a, v) => a + v, 0) / prev.length - k); if (!side) continue;
        }
        st.last[k] = n;
        const g = sk.gf, oc = sk.oc, op = sk.op, scs = sk.sc, sps = sk.sp;
        const near = s.strikes.filter(x => Math.abs(x.k - p) <= P(40));
        const drel = Math.abs(sk.dx) / median(near.map(x => Math.abs(x.dx)));
        const chm = median(near.map(x => Math.abs(x.ch)));
        const dc = (drel >= 1.55 && sk.ch > C.cht * chm) ? 'DEX alto charm+' : (sk.dx < 0 && sk.ch < 0) ? 'DEX- charm-' : 'otro';
        const info = `${oc >= op ? 'calls' : 'puts'} ${(oc >= op ? scs : sps) > 0 ? 'vendidos' : 'comprados'} · GEX ${(g / 1e6).toFixed(0)}M · ${dc} · régimen ${reg > 0 ? '+' : '−'}`;
        // Regla A
        if (g < 0 && side > 0 && scs < 0 && oc > op) { st.pos = openLeg(s, n, p, 1, k - P(20), 'A', 'A', k, tipo, info, 'compra A'); emit({ type: 'entry', t: s.t, pos: st.pos }); break; }
        // Regla D
        if (g < 0 && side < 0 && scs < 0 && oc > op) { if (C.noRepeat && st.failed['D' + k]) continue; st.pend = { n0: n, t0: s.t, d: 1, trig: k + P(C.D.brk), stop: k + P(C.D.brk) - P(C.D.sl), cancel: k - P(15), wait: 90, regla: 'D', k, tipo, info, leg: 'compra ruptura D' }; emit({ type: 'armed', t: s.t, pend: st.pend }); break; }
        // Regla E
        if (g < 0 && side > 0 && sps < 0 && op > oc) { if (C.noRepeat && st.failed['E' + k]) continue; st.pend = { n0: n, t0: s.t, d: -1, trig: k - P(C.E.brk), stop: k - P(C.E.brk) + P(C.E.sl), cancel: k + P(15), wait: 90, regla: 'E', k, tipo, info, leg: 'venta ruptura E' }; emit({ type: 'armed', t: s.t, pend: st.pend }); break; }
        // Regla C
        if (g > 0) {
          const cls = oc >= op ? (scs > 0 ? 'calls vendidos' : 'calls comprados') : (sps > 0 ? 'puts vendidos' : 'puts comprados');
          if (!((cls === 'calls vendidos' && side < 0) || (cls === 'puts vendidos' && side > 0))) continue;
          if (C.skipRebMinus && dc === 'DEX- charm-') continue;
          const d = side;
          const past = H.slice(Math.max(0, H.length - 61)).map(v => sign(v - k)).filter(x => x !== 0);
          let cruces = 0; for (let i = 1; i < past.length; i++) if (past[i] !== past[i - 1]) cruces++;
          const SL = (reg > 0 && cruces >= 2) ? 12 : 8;
          if (dc === 'DEX alto charm+') { st.pend = { n0: n, t0: s.t, d: -d, trig: k - d * P(SL), stop: k + d * P(8), cancel: null, wait: 120, regla: 'C', k, tipo, info, leg: 'ruptura (solo)' }; emit({ type: 'armed', t: s.t, pend: st.pend }); break; }
          st.pos = openLeg(s, n, p, d, k - d * P(SL), 'rebote', 'C', k, tipo, info + ` · stop ${SL}`, 'rebote'); st.pos.sar = dc !== 'DEX- charm-';
          emit({ type: 'entry', t: s.t, pos: st.pos }); break;
        }
      }
      return st.events;
    }
    function flatten(s, why) { if (st.pos) { const pos = st.pos; st.pos = null; st.cool = st.n; return { type: 'exit', t: s ? s.t : new Date(), why, px: s ? s.spot : null, pts: s ? (s.spot - pos.e) * pos.d : null, pos }; } return null; }
    return { onMinute, state: st, cfg: C, flatten };
  }
  root.QOIEngine = Engine;
  if (typeof module !== 'undefined') module.exports = Engine;
})(typeof window !== 'undefined' ? window : globalThis);
(function () {
  'use strict';
  if (window.QBOT && window.QBOT.running) { console.log('[QBOT] ya está corriendo'); return; }
  const API = 'https://api-spike.quantium.global';
  const LS = 'qbot_cfg_v1';
  // ---------- configuración (se guarda en este navegador) ----------
  const DEF = {
    modo: 'papel',            // 'papel' = solo registra · 'demo' = manda las órdenes al webhook
    webhook: '',              // URL del webhook de la estrategia en TradersPost
    riesgoUSD: 250,           // riesgo por operación hasta el stop inicial
    maxPerdidaR: 3,           // pérdida del día (en veces el riesgo) que apaga las entradas
    maxOps: 8,                // operaciones por día y por instrumento
    SPX: { activo: true, ticker: 'MESZ2026', pv: 5, maxQty: 10 },
    QQQ: { activo: true, ticker: 'MNQZ2026', pv: 2, maxQty: 10, ratio: null }, // ratio = precio MNQ / precio QQQ (se pide al iniciar)
  };
  const loadCfg = () => { try { const c = JSON.parse(localStorage.getItem(LS)); if (c) return Object.assign({}, DEF, c, { SPX: Object.assign({}, DEF.SPX, c.SPX), QQQ: Object.assign({}, DEF.QQQ, c.QQQ) }); } catch (e) {} return JSON.parse(JSON.stringify(DEF)); };
  const saveCfg = () => { try { localStorage.setItem(LS, JSON.stringify(cfg)); } catch (e) {} };
  let cfg = loadCfg();
  const TK = { SPX: { sc: 1, mg: 2.34e9, skip: false }, QQQ: { sc: 0.089, mg: 1.73e8, skip: true } };

  // ---------- utilidades ----------
  const token = () => { try { return JSON.parse(localStorage.getItem('sb-euwdfoswzwsqrknnvjvj-auth-token')).access_token; } catch (e) { return null; } };
  const nyDate = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date());
  const nyMin = (t) => { const d = new Date((t || new Date()).toLocaleString('en-US', { timeZone: 'America/New_York' })); return d.getHours() * 60 + d.getMinutes(); };
  const nyHM = (t) => new Date(t.toLocaleString('en-US', { timeZone: 'America/New_York' })).toTimeString().slice(0, 5);
  const weekday = () => { const d = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' })).getDay(); return d > 0 && d < 6; };
  async function get(path, params) {
    const r = await fetch(`${API}${path}?${new URLSearchParams(params)}`, { headers: { Authorization: 'Bearer ' + token() } });
    if (!r.ok) throw new Error(path + ' ' + r.status);
    return r.json();
  }
  const S = { running: false, timer: null, eng: {}, vcache: {}, vtime: {}, last: {}, log: [], day: null, R: { SPX: 0, QQQ: 0 }, ops: { SPX: 0, QQQ: 0 }, apagado: false, err: null, live: { SPX: null, QQQ: null }, lat: {} };
  function log(msg, tk, extra) { const e = { t: new Date(), tk: tk || '', msg, extra: extra || '' }; S.log.push(e); console.log('[QBOT]', nyHM(e.t), tk || '', msg, extra || ''); render(); }

  // ---------- datos ----------
  function histSnap(x) {
    if (!x.sc_strike || !x.sp_strike || !x.gex_strike_flujo) return null;
    return { t: new Date(x.ts), spot: x.spot, flip: x.gamma_flip ?? NaN, cw: x.call_wall ?? NaN, pw: x.put_wall ?? NaN,
      strikes: x.strikes.map((k, i) => ({ k: +k, oc: x.oi_call[i] || 0, op: x.oi_put[i] || 0, sc: Math.sign(x.sc_strike[i] || 0), sp: Math.sign(x.sp_strike[i] || 0), gf: x.gex_strike_flujo[i] ?? NaN,
        dx: x.dex_strike_flujo ? x.dex_strike_flujo[i] : NaN, ch: x.charm_strike_flujo ? x.charm_strike_flujo[i] : NaN })) };
  }
  async function refreshGreeks(tk) {
    S.vtime[tk] = Date.now();
    try { const h = await get('/historial', { ticker: tk, exps: '0dte', day: nyDate() }); const x = h[h.length - 1];
      if (x && x.dex_strike_flujo) S.vcache[tk] = new Map(x.strikes.map((k, i) => [+k, [x.dex_strike_flujo[i], x.charm_strike_flujo ? x.charm_strike_flujo[i] : NaN]])); } catch (e) { console.warn('[QBOT] griegas', tk, e.message); }
  }
  async function liveSnap(tk) {
    const t0 = performance.now();
    const [tr, mx, sg] = await Promise.all([get('/trace', { ticker: tk, exps: '0dte' }), get('/theta/matrix', { ticker: tk, nexp: 1 }), get('/theta/flow/signos', { ticker: tk })]);
    S.lat[tk] = Math.round(performance.now() - t0);
    const exp = tr.exp_used || (mx.expirations || [])[0];
    const SG = sg.exps && sg.exps[exp] ? new Map(sg.exps[exp].s.map(r => [+r[0], r])) : new Map();
    const cells = new Map(); for (const c of (mx.cells || [])) { const ce = c.exp || c.expiration || c.vto; if (ce && exp && ce !== exp) continue; const k = +(c.strike ?? c.k ?? c.s); if (!isNaN(k)) cells.set(k, c); }
    if (!S.vtime[tk] || Date.now() - S.vtime[tk] > 2 * 60000) refreshGreeks(tk);
    const strikes = tr.strikes.map((k, i) => {
      const c = cells.get(+k); const s = SG.get(+k) || [k, 0, 0]; const sc = Math.sign(s[1]), sp = Math.sign(s[2]); let gf;
      if (c && c.gex_call != null && c.gex_put != null) gf = sc * Math.abs(c.gex_call) + sp * Math.abs(c.gex_put);
      else { const g = tr.gex_strike[i] || 0, oc = tr.oi_call[i] || 0, op = tr.oi_put[i] || 0; gf = (sc > 0 && sp < 0) ? g : (sc < 0 && sp > 0) ? -g : Math.abs(g) * ((op >= oc ? sp : sc) || 1); }
      const g3 = S.vcache[tk] ? S.vcache[tk].get(+k) : null;
      return { k: +k, oc: tr.oi_call[i] || 0, op: tr.oi_put[i] || 0, sc, sp, gf, dx: g3 ? g3[0] : NaN, ch: g3 ? g3[1] : NaN };
    });
    const ts = typeof tr.ts === 'number' ? new Date(tr.ts < 1e12 ? tr.ts * 1000 : tr.ts) : new Date(tr.ts);
    return { t: ts, spot: tr.spot, flip: tr.gamma_flip ?? NaN, cw: tr.call_wall ?? NaN, pw: tr.put_wall ?? NaN, strikes };
  }

  // ---------- órdenes (formato de señal de TradersPost) ----------
  function post(body) {
    return new Promise((res) => {
      if (cfg.modo !== 'demo' || !cfg.webhook) { res({ ok: true, papel: true }); return; }
      const data = JSON.stringify(body);
      if (typeof GM_xmlhttpRequest === 'function') {
        GM_xmlhttpRequest({ method: 'POST', url: cfg.webhook, data, headers: { 'Content-Type': 'application/json' }, timeout: 8000,
          onload: (r) => res({ ok: r.status >= 200 && r.status < 300, status: r.status, text: (r.responseText || '').slice(0, 200) }),
          onerror: () => res({ ok: false, status: 'error de red' }), ontimeout: () => res({ ok: false, status: 'sin respuesta' }) });
      } else fetch(cfg.webhook, { method: 'POST', mode: 'no-cors', headers: { 'Content-Type': 'text/plain' }, body: data }).then(() => res({ ok: true, status: 'enviado sin confirmación' })).catch(e => res({ ok: false, status: e.message }));
    });
  }
  const wait = (ms) => new Promise(r => setTimeout(r, ms));
  function futPts(tk, idxPts) { return tk === 'SPX' ? idxPts : (cfg.QQQ.ratio ? idxPts * cfg.QQQ.ratio : null); }
  const tick4 = (x) => Math.round(x * 4) / 4;
  async function sendEntry(tk, pos) {
    const c = cfg[tk]; const stopIdx = Math.abs(pos.e - pos.st0); const stopF = futPts(tk, stopIdx);
    if (stopF == null) { log('sin ratio MNQ/QQQ: no se envía', tk); return false; }
    const qty = Math.min(c.maxQty, Math.floor(cfg.riesgoUSD / (stopF * c.pv)));
    if (qty < 1) { log(`stop de ${stopF.toFixed(2)} pts es muy amplio para $${cfg.riesgoUSD}: no se envía`, tk); return false; }
    const body = { ticker: c.ticker, action: pos.d > 0 ? 'buy' : 'sell', quantity: qty, stopLoss: { type: 'stop', amount: tick4(stopF) } };
    if (pos.tp) body.takeProfit = { amount: tick4(futPts(tk, pos.tp)) };
    const r = await post(body); pos.qty = qty; pos.sent = true;
    log(`${r.papel ? '[papel] ' : ''}${body.action.toUpperCase()} ${qty} ${c.ticker} · stop ${body.stopLoss.amount} pts${body.takeProfit ? ' · objetivo ' + body.takeProfit.amount : ''} · ${pos.regla} ${pos.leg}${pos.run ? ' · corredor' : ''} · nivel ${pos.k}`, tk, r.papel ? '' : (r.ok ? 'OK ' + r.status : 'FALLÓ ' + r.status));
    return true;
  }
  async function sendExit(tk, why) {
    const c = cfg[tk]; const r1 = await post({ ticker: c.ticker, action: 'exit' }); await wait(1200); const r2 = await post({ ticker: c.ticker, action: 'cancel' });
    log(`${r1.papel ? '[papel] ' : ''}CIERRE ${c.ticker} (${why})`, tk, r1.papel ? '' : `exit ${r1.ok ? 'OK' : 'FALLÓ ' + r1.status} · cancel ${r2.ok ? 'OK' : 'FALLÓ ' + r2.status}`);
  }

  // ---------- ciclo ----------
  async function handle(tk, evs) {
    for (const ev of evs) {
      if (ev.type === 'armed') log(`armada ${ev.pend.regla} ${ev.pend.leg} en ${ev.pend.k} · dispara en ${ev.pend.trig.toFixed(2)}`, tk);
      else if (ev.type === 'cancel') log(`cancelada ${ev.pend.regla} en ${ev.pend.k}${ev.why ? ' (' + ev.why + ')' : ''}`, tk);
      else if (ev.type === 'tighten') log(`trailing apretado a 3 pts (siguiente strike verde)`, tk);
      else if (ev.type === 'exit') {
        const R = ev.pts / Math.abs(ev.pos.e - ev.pos.st0); S.R[tk] += R;
        if (ev.pos.sent) await sendExit(tk, ev.why);
        log(`salida ${ev.why} · ${ev.pts >= 0 ? '+' : ''}${ev.pts.toFixed(2)} pts del índice (${R >= 0 ? '+' : ''}${R.toFixed(2)} R) · día ${S.R[tk].toFixed(2)} R`, tk);
      } else if (ev.type === 'entry') {
        const bloqueo = S.apagado ? 'bot apagado' : !cfg[tk].activo ? 'instrumento desactivado' : S.R.SPX + S.R.QQQ <= -cfg.maxPerdidaR ? 'límite de pérdida del día' : S.ops[tk] >= cfg.maxOps ? 'máximo de operaciones del día' : null;
        if (bloqueo) { log(`entrada ${ev.pos.regla} ignorada: ${bloqueo}`, tk); ev.pos.sent = false; continue; }
        S.ops[tk]++; if (ev.pos.leg === 'ruptura' && ev.pos.regla === 'C') await wait(2500); // deja terminar el cierre del rebote
        await sendEntry(tk, ev.pos);
      }
    }
  }
  async function step() {
    const day = nyDate(); if (S.day !== day) { S.day = day; S.R = { SPX: 0, QQQ: 0 }; S.ops = { SPX: 0, QQQ: 0 }; await warmup(); }
    const m = nyMin(); if (!weekday() || m < 570 || m > 961) { render(); return; }
    await Promise.all(['SPX', 'QQQ'].map(async (tk) => {
      if (!cfg[tk].activo) return;
      try {
        const snap = await liveSnap(tk); S.err = null;
        const age = (Date.now() - snap.t.getTime()) / 1000; S.live[tk] = { spot: snap.spot, flip: snap.flip, age };
        if (age > 180) { log(`datos atrasados ${Math.round(age)} s: no se evalúa`, tk); return; }
        if (S.last[tk] && nyHM(S.last[tk]) === nyHM(snap.t)) return; // mismo minuto
        S.last[tk] = snap.t;
        await handle(tk, S.eng[tk].onMinute(snap));
      } catch (e) { S.err = e.message; log('error de datos: ' + e.message, tk); }
    }));
    render();
  }
  async function warmup() {
    for (const tk of ['SPX', 'QQQ']) {
      S.eng[tk] = window.QOIEngine({ sc: TK[tk].sc, cfg: { mg: TK[tk].mg, skipRebMinus: TK[tk].skip } });
      try {
        const h = (await get('/historial', { ticker: tk, exps: '0dte', day: nyDate() })).map(histSnap).filter(Boolean);
        const byMin = new Map(); for (const s of h) byMin.set(nyHM(s.t), s);
        for (const s of byMin.values()) S.eng[tk].onMinute(s);
        const st = S.eng[tk].state; if (st.pos || st.pend) { log(`al arrancar había una operación del modelo abierta o armada; se descarta (no está en tu cuenta)`, tk); st.pos = null; st.pend = null; }
        S.last[tk] = h.length ? h[h.length - 1].t : null;
        log(`historial cargado: ${byMin.size} minutos`, tk);
      } catch (e) { log('no se pudo cargar el historial: ' + e.message, tk); }
    }
  }
  async function start() {
    if (S.running) return; S.apagado = false;
    if (cfg.QQQ.activo && !cfg.QQQ.ratio) askRatio();
    S.running = true; S.day = null; log(`iniciado en modo ${cfg.modo.toUpperCase()}`); await step(); S.timer = setInterval(() => { if (!S.busy) { S.busy = true; step().finally(() => { S.busy = false; }); } }, 5000);
  }
  function pause() { clearInterval(S.timer); S.running = false; log('pausado (no cierra posiciones)'); }
  async function kill() {
    S.apagado = true; clearInterval(S.timer); S.running = false;
    for (const tk of ['SPX', 'QQQ']) { const e = S.eng[tk]; if (e) { e.state.pos = null; e.state.pend = null; } await sendExit(tk, 'apagado manual'); }
    log('APAGADO: se mandó cierre y cancelación en los dos instrumentos');
  }
  function askRatio() {
    const q = S.live.QQQ && S.live.QQQ.spot; const v = prompt(`Precio actual de MNQ${q ? ` (QQQ está en ${q.toFixed(2)})` : ''}:`);
    const mnq = parseFloat(v); if (!mnq) return;
    const qq = q || parseFloat(prompt('Precio actual de QQQ:')); if (qq) { cfg.QQQ.ratio = mnq / qq; saveCfg(); log(`ratio MNQ/QQQ = ${cfg.QQQ.ratio.toFixed(3)}`); }
  }
  function settings() {
    const w = prompt('URL del webhook de TradersPost (vacío = no enviar):', cfg.webhook); if (w !== null) cfg.webhook = w.trim();
    const r = parseFloat(prompt('Riesgo por operación en USD:', cfg.riesgoUSD)); if (r > 0) cfg.riesgoUSD = r;
    const a = prompt('Contrato MES:', cfg.SPX.ticker); if (a) cfg.SPX.ticker = a.trim();
    const b = prompt('Contrato MNQ:', cfg.QQQ.ticker); if (b) cfg.QQQ.ticker = b.trim();
    const l = parseFloat(prompt('Pérdida máxima del día (veces el riesgo):', cfg.maxPerdidaR)); if (l > 0) cfg.maxPerdidaR = l;
    saveCfg(); log('configuración guardada'); render();
  }
  function setModo() {
    if (cfg.modo === 'papel') {
      if (!cfg.webhook) { alert('Primero pon la URL del webhook en Ajustes.'); return; }
      if (!confirm('Modo DEMO: el bot mandará órdenes reales a la cuenta conectada a ese webhook. ¿Es una cuenta demo?')) return;
      cfg.modo = 'demo';
    } else cfg.modo = 'papel';
    saveCfg(); log('modo ' + cfg.modo.toUpperCase());
  }
  function exportLog() {
    const rows = [['hora_NY', 'instrumento', 'evento', 'respuesta']].concat(S.log.map(e => [nyHM(e.t), e.tk, e.msg.replace(/;/g, ','), e.extra]));
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([rows.map(r => r.join(';')).join('\n')], { type: 'text/csv' })); a.download = `qbot_${nyDate()}.csv`; a.click();
  }

  // ---------- panel ----------
  let box;
  function render() {
    if (!box) return;
    const posTxt = (tk) => { const e = S.eng[tk]; if (!e) return '—'; const p = e.state.pos, q = e.state.pend;
      if (p) return `${p.d > 0 ? 'COMPRA' : 'VENTA'} ${p.regla} ${p.leg} · nivel ${p.k} · entrada ${p.e.toFixed(2)} · stop ${p.st.toFixed(2)}${p.qty ? ' · ' + p.qty + ' ctr' : ''}`;
      if (q) return `armada ${q.regla} en ${q.k} → ${q.trig.toFixed(2)}`; return 'sin posición'; };
    const R = S.R.SPX + S.R.QQQ;
    box.querySelector('.st').innerHTML = `<b style="color:${cfg.modo === 'demo' ? '#f59e0b' : '#60a5fa'}">${cfg.modo.toUpperCase()}</b> · ${S.apagado ? '<b style="color:#ef4444">APAGADO</b>' : S.running ? '<span style="color:#22c55e">corriendo</span>' : 'detenido'} · día ${R >= 0 ? '+' : ''}${R.toFixed(2)} R · datos ${['SPX', 'QQQ'].map(t => S.live[t] ? `${t} ${Math.round(S.live[t].age)}s/${S.lat[t] || '?'}ms` : '').join(' ')}${S.err ? `<br><span style="color:#ef4444">${S.err}</span>` : ''}
      <br>MES: ${posTxt('SPX')}<br>MNQ: ${posTxt('QQQ')}${cfg.QQQ.ratio ? '' : ' <span style="color:#f59e0b">(falta ratio)</span>'}`;
    box.querySelector('.lg').innerHTML = S.log.slice(-12).reverse().map(e => `<div><span style="color:#94a3b8">${nyHM(e.t)} ${e.tk}</span> ${e.msg}${e.extra ? ` <span style="color:${/FALL/.test(e.extra) ? '#ef4444' : '#22c55e'}">${e.extra}</span>` : ''}</div>`).join('');
  }
  function ui() {
    box = document.createElement('div');
    box.style.cssText = 'position:fixed;left:12px;bottom:12px;z-index:99999;width:380px;max-height:60vh;overflow:auto;background:#0b1220;color:#e2e8f0;border:1px solid #334155;border-radius:10px;padding:10px;font:12px/1.4 system-ui,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.5)';
    box.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><b>Bot Q OI</b><span style="color:#94a3b8">v0.3</span></div>
      <div class="st"></div>
      <div style="display:flex;flex-wrap:wrap;gap:4px;margin:8px 0">${['Iniciar', 'Pausar', 'Modo', 'Ajustes', 'Ratio MNQ', 'Registro', 'APAGAR TODO'].map(b => `<button data-b="${b}" style="font:600 11px system-ui;padding:4px 8px;border-radius:6px;border:1px solid #334155;background:${b === 'APAGAR TODO' ? '#7f1d1d' : '#1e293b'};color:#e2e8f0;cursor:pointer">${b}</button>`).join('')}</div>
      <div class="lg" style="border-top:1px solid #1e293b;padding-top:6px"></div>`;
    box.addEventListener('click', (e) => { const b = e.target.closest('button'); if (!b) return; const k = b.dataset.b;
      if (k === 'Iniciar') start(); else if (k === 'Pausar') pause(); else if (k === 'Modo') setModo(); else if (k === 'Ajustes') settings(); else if (k === 'Ratio MNQ') askRatio(); else if (k === 'Registro') exportLog();
      else if (k === 'APAGAR TODO' && confirm('¿Cerrar posiciones, cancelar órdenes y apagar el bot?')) kill(); render(); });
    document.body.appendChild(box); render();
  }
  window.QBOT = { start, pause, kill, cfg: () => cfg, state: S, get running() { return S.running; }, _test: { handle, histSnap, post, setCfg: (c) => { cfg = Object.assign(cfg, c); }, newEngines: () => { for (const tk of ['SPX', 'QQQ']) S.eng[tk] = window.QOIEngine({ sc: TK[tk].sc, cfg: { mg: TK[tk].mg, skipRebMinus: TK[tk].skip } }); } } };
  if (!window.__QBOT_NO_UI) ui();
})();
