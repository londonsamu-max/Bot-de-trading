// ==UserScript==
// @name         Detector de giros Q OI (Quantium)
// @namespace    samuel.qoi
// @version      1.8
// @description  Vigila en vivo el Q OI por strike en SPX, SPY y QQQ y avisa giros y rupturas.
// @match        https://quantium.global/app/*
// @grant        none
// ==/UserScript==
(function () {
  'use strict';
  if (window.QG && window.QG.running) { console.log('[QG] ya está corriendo'); return; }
  const API = 'https://api-spike.quantium.global';
  const CFG = {
    tickers: { SPX: { sc: 1, minGex: 2.34e9 }, SPY: { sc: 0.1, minGex: 0 }, QQQ: { sc: 0.089, minGex: 0 } },
    everyMs: 60000,          // consulta cada minuto
    nearPts: 40,             // strikes a revisar alrededor del precio (puntos SPX equivalentes)
    giroRange: 12,           // el strike del giro debe estar a <=12 pts del precio
    holdMin: 5,              // el nuevo signo debe sostenerse 5 min
    disputeFlips: 3,         // 3+ cambios en 60 min = en disputa
    flipZone: 15,            // a <15 pts del flip = zona dudosa
    sound: true,
  };
  const state = { hist: {}, pending: {}, alerts: [], last: {}, watch: {}, running: true, timer: null, vcache: {}, vtime: {}, near: {}, wallsAll: {}, wtime: {}, reglaD: {} };
  // Estadística de 318 toques a niveles relevantes de SPX en 41 días: [casos, rebota%, rompe%, pegado%, pase p80 en los rebotes]
  const PERF = {"débil|alta|lenta": [4, 75, 0, 25, 0.8], "débil|alta|rápida": [4, 50, 25, 25, null], "débil|normal/baja|lenta": [43, 42, 26, 33, 8.4], "débil|normal/baja|rápida": [50, 56, 14, 30, 11.8], "fuerte|alta|lenta": [53, 42, 9, 49, 6.9], "fuerte|alta|rápida": [45, 49, 9, 42, 5.2], "fuerte|normal/baja|lenta": [57, 61, 12, 26, 8.4], "fuerte|normal/baja|rápida": [62, 48, 19, 32, 11.8], "rojo|débil|alta|lenta": [1, 100, 0, 0, null], "rojo|débil|normal/baja|lenta": [22, 41, 27, 32, 6.9], "rojo|débil|normal/baja|rápida": [20, 65, 15, 20, 15.4], "rojo|fuerte|alta|lenta": [27, 37, 11, 52, 4.2], "rojo|fuerte|alta|rápida": [13, 62, 0, 38, 3.8], "rojo|fuerte|normal/baja|lenta": [29, 59, 10, 31, 8.8], "rojo|fuerte|normal/baja|rápida": [35, 51, 17, 31, 8.1], "verde|débil|alta|lenta": [3, 67, 0, 33, null], "verde|débil|alta|rápida": [4, 50, 25, 25, null], "verde|débil|normal/baja|lenta": [21, 43, 24, 33, 7.9], "verde|débil|normal/baja|rápida": [30, 50, 13, 37, 11.1], "verde|fuerte|alta|lenta": [26, 46, 8, 46, 8.8], "verde|fuerte|alta|rápida": [32, 44, 12, 44, 7.6], "verde|fuerte|normal/baja|lenta": [28, 64, 14, 21, 6.9], "verde|fuerte|normal/baja|rápida": [27, 44, 22, 33, 16.4]};
  const median = (a) => { const b = a.filter(x => isFinite(x)).sort((x, y) => x - y); return b.length ? b[Math.floor(b.length / 2)] : 0; };
  function profile(tk, snap, s, mom) {
    const { sc } = CFG.tickers[tk];
    const near = snap.strikes.filter(x => Math.abs(x.k - snap.spot) <= CFG.nearPts * sc && x.gf);
    // fuerte = uno de los 2 strikes de mayor |GEX| cerca del precio (misma definición del estudio de 41 días)
    const above = near.filter(x => Math.abs(x.gf) > Math.abs(s.gf)).length;
    const vmed = median(near.map(x => Math.abs(x.va || 0)));
    const vrel = s.va == null || !vmed ? null : Math.abs(s.va) / vmed;
    const fuerza = above <= 1 ? 'fuerte' : 'débil';
    const vanna = vrel == null ? null : vrel >= 0.254 ? 'alta' : 'normal/baja';
    const llegada = mom == null ? null : mom >= 8.5 ? 'rápida' : 'lenta';
    const color = s.gf > 0 ? 'verde' : 'rojo';
    let st = null, key = null;
    if (vanna && llegada) {
      const k4 = [color, fuerza, vanna, llegada].join('|'), k3 = [fuerza, vanna, llegada].join('|');
      if (PERF[k4] && PERF[k4][0] >= 12) { st = PERF[k4]; key = k4; } else if (PERF[k3]) { st = PERF[k3]; key = k3; }
    }
    let lectura = '', stop = null;
    if (!st || st[0] < 10) lectura = 'pocos casos en el histórico: sin base';
    else {
      stop = st[4] != null ? Math.ceil(st[4]) + 1 : null;
      if (fuerza === 'fuerte' && vanna === 'alta') lectura = 'IMÁN: suele quedarse pegado y rompe poco; rebote con stop corto y ganancia rápida';
      else if (fuerza === 'fuerte') lectura = 'suele rebotar, pero se pasa antes de devolverse';
      else if (llegada === 'lenta') lectura = 'nivel débil: riesgo de ruptura alto; espera a ver si pasa el umbral';
      else lectura = 'nivel débil y llegada rápida: se pasa bastante antes de decidir; stop amplio o espera la ruptura';
    }
    // Combinaciones extra (318 toques SPX, se repiten en las dos mitades de los 41 días)
    const extra = [], short = [];
    const dmed = median(near.map(x => Math.abs(x.dx || 0)));
    const dexAlto = s.dx != null && dmed ? Math.abs(s.dx) / dmed >= 1.55 : null;
    const cmed = median(near.map(x => Math.abs(x.ch || 0))); const chPos = s.ch != null && s.ch > 0.1 * (cmed || 0); // charm casi cero no cuenta como positivo
    if (s.dx != null && s.ch != null) {
      if (s.dx < 0 && s.ch < 0) short.push('★rebote'), extra.push(dexAlto ? 'DEX y charm negativos + DEX alto: perfil de REBOTE (rebota 77%, rompe 10%)' : 'DEX y charm negativos: el nivel suele aguantar (rebota 67%, rompe 7%)');
      else if (dexAlto && chPos) short.push('★ruptura'), extra.push(snap.spot < snap.flip ? 'DEX alto + charm positivo bajo el flip: perfil de RUPTURA (rompe 32%)' : 'DEX alto + charm positivo: rupturas casi al doble (rompe 27%)');
    }
    const et = new Date(snap.t.toLocaleString('en-US', { timeZone: 'America/New_York' })); const mm = et.getHours() * 60 + et.getMinutes();
    if (mm >= 870) short.push('★cierre'), extra.push(fuerza === 'fuerte' ? 'después de 14:30 NY + GEX fuerte: imán de cierre, casi no rompe (pegado 60%)' : 'después de 14:30 NY: casi no hay rupturas (rompe 2%), evita operar rupturas');
    const zona = near.reduce((a, x) => a + (x.gf || 0), 0);
    if (Math.abs(snap.spot - snap.flip) < 15 * sc && zona > 0) short.push('★no fiable'), extra.push('cerca del flip con GEX de zona positivo: nivel poco fiable (rebota 25%), mejor no operar');
    if (s.dx == null) extra.push('sin DEX/charm todavía');
    return { color, fuerza, vanna, llegada, mom, st, key, lectura, stop: stop != null ? stop * sc : null, extra, short,
      tag: `${fuerza}${vanna === 'alta' ? ' · vanna alta' : ''}${short.length ? ' ' + short.join(' ') : ''}`,
      text: `${color} ${fuerza} · vanna ${vanna || '¿?'} · llegada ${llegada || '¿?'}${mom != null ? ' (' + (mom * sc).toFixed(2) + ' pts en 10 min)' : ''}` +
        (st ? ` → rebota ${st[1]}% · rompe ${st[2]}% · pegado ${st[3]}% (${st[0]} casos)` : '') +
        ` · ${lectura}` + (stop != null ? ` · stop para rebote ${(stop * sc).toFixed(2)} pts` : '') +
        (extra.length ? ` · ★ ${extra.join(' · ★ ')}` : '') };
  }
  function momentum(tk, t, p) { const rs = (state.recentSpots || {})[tk] || []; const sc = CFG.tickers[tk].sc; let ref = null; for (const x of rs) if (t - x.t >= 10 * 60000) ref = x; return ref ? Math.abs(p - ref.p) / sc : null; }

  const token = () => { try { return JSON.parse(localStorage.getItem('sb-euwdfoswzwsqrknnvjvj-auth-token')).access_token; } catch (e) { return null; } };
  async function get(path, params) {
    const q = new URLSearchParams(params).toString();
    const r = await fetch(`${API}${path}?${q}`, { headers: { Authorization: 'Bearer ' + token() } });
    if (!r.ok) { const e = new Error(path + ' ' + r.status); if (r.status === 403) state.err = 'Quantium respondió 403: la cuenta abierta no tiene Premium o la sesión cambió.'; throw e; }
    return r.json();
  }
  const etNow = () => new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
  const hhmm = (d) => d.toTimeString().slice(0, 5);
  const marketOpen = () => { const d = etNow(); const m = d.getHours() * 60 + d.getMinutes(); return d.getDay() > 0 && d.getDay() < 6 && m >= 570 && m <= 960; };

  // ---------- construir una "foto" con el mismo formato en vivo y en repetición ----------
  // Vanna, DEX y charm de flujo por strike: se toman del último minuto del historial cada 2 min
  async function refreshVanna(tk) {
    state.vtime[tk] = Date.now();
    try { const h = await get('/historial', { ticker: tk, exps: '0dte', day: etNow().toISOString().slice(0, 10) }); const x = h[h.length - 1];
      if (x && x.vanna_strike_flujo) state.vcache[tk] = new Map(x.strikes.map((k, i) => [+k, [x.vanna_strike_flujo[i], x.dex_strike_flujo ? x.dex_strike_flujo[i] : null, x.charm_strike_flujo ? x.charm_strike_flujo[i] : null]])); } catch (e) { console.warn('[QG] vanna', tk, e.message); }
  }
  async function liveSnapshot(tk) {
    const tr = await get('/trace', { ticker: tk, exps: '0dte' });
    const mx = await get('/theta/matrix', { ticker: tk, nexp: 1 });
    const sg = await get('/theta/flow/signos', { ticker: tk });
    const exp = tr.exp_used || (mx.expirations || [])[0];
    const S = sg.exps && sg.exps[exp] ? new Map(sg.exps[exp].s.map(r => [+r[0], r])) : new Map();
    const cells = new Map();
    for (const c of (mx.cells || [])) { const ce = c.exp || c.expiration || c.vto; if (ce && exp && ce !== exp) continue; const k = +(c.strike ?? c.k ?? c.s); if (!isNaN(k)) cells.set(k, c); }
    let exact = 0;
    const strikes = tr.strikes.map((k, i) => {
      const c = cells.get(+k); const s = S.get(+k) || [k, 0, 0];
      const sc = Math.sign(s[1]), sp = Math.sign(s[2]); let gf;
      if (c && c.gex_call != null && c.gex_put != null) { gf = sc * Math.abs(c.gex_call) + sp * Math.abs(c.gex_put); exact++; }
      else { const g = tr.gex_strike[i] || 0; const oc = tr.oi_call[i] || 0, op = tr.oi_put[i] || 0; gf = (sc > 0 && sp < 0) ? g : (sc < 0 && sp > 0) ? -g : Math.abs(g) * ((op >= oc ? sp : sc) || 1); }
      const g3 = state.vcache[tk] ? state.vcache[tk].get(+k) : null;
      return { k: +k, oc: tr.oi_call[i] || 0, op: tr.oi_put[i] || 0, sc, sp, gf, va: g3 ? g3[0] : null, dx: g3 ? g3[1] : null, ch: g3 ? g3[2] : null };
    });
    if (!S.size) console.warn('[QG]', tk, 'sin signos para', exp);
    if (!exact) console.warn('[QG]', tk, 'matrix sin gex_call/gex_put: GEX de flujo aproximado');
    if (!state.vtime[tk] || Date.now() - state.vtime[tk] > 2 * 60000) refreshVanna(tk);
    if (!state.wtime[tk] || Date.now() - state.wtime[tk] > 5 * 60000) refreshWallsAll(tk);
    const ts = typeof tr.ts === 'number' ? new Date(tr.ts < 1e12 ? tr.ts * 1000 : tr.ts) : new Date(tr.ts);
    return { t: ts, spot: tr.spot, flip: tr.gamma_flip, cw: tr.call_wall, pw: tr.put_wall, strikes, exp, staleSec: sg.exps && sg.exps[exp] ? sg.exps[exp].edad_seg : null };
  }
  function histSnapshot(x) {
    if (!x.sc_strike || !x.sp_strike || !x.gex_strike_flujo) return null;
    return { t: new Date(x.ts), spot: x.spot, flip: x.gamma_flip, cw: x.call_wall, pw: x.put_wall,
      strikes: x.strikes.map((k, i) => ({ k, oc: x.oi_call[i] || 0, op: x.oi_put[i] || 0, sc: Math.sign(x.sc_strike[i] || 0), sp: Math.sign(x.sp_strike[i] || 0), gf: x.gex_strike_flujo[i] || 0, va: x.vanna_strike_flujo ? x.vanna_strike_flujo[i] : null, dx: x.dex_strike_flujo ? x.dex_strike_flujo[i] : null, ch: x.charm_strike_flujo ? x.charm_strike_flujo[i] : null })) };
  }

  // ---------- lógica (la misma que el backtest) ----------
  const dom = (s) => (s.op >= s.oc ? s.sp : s.sc);
  const domLabel = (s) => (s.op >= s.oc ? `${s.op.toLocaleString()} puts ${s.sp > 0 ? 'vendidos' : s.sp < 0 ? 'comprados' : '—'}` : `${s.oc.toLocaleString()} calls ${s.sc > 0 ? 'vendidos' : s.sc < 0 ? 'comprados' : '—'}`);

  // ---------- contexto del mapa y recorrido probable (41 días SPX, a favor del régimen) ----------
  // franja: A 9:35-10:45 · B 10:45-13:00 · C 13:00-14:30 · D 14:30-15:30 ; [cerca del flip (<=40), lejos (>40)] -> [P50, P80] en pts SPX
  const EXT = { A: [[19.5, 35.8], [22.9, 42.4]], B: [[13.0, 26.5], [24.1, 45.1]], C: [[8.6, 17.1], [15.7, 28.0]], D: [[10.0, 13.6], [7.2, 21.0]] };
  // modo corredor (en observación): antes de 10:45 NY, a más de 20 pts SPX del flip a favor y charm negativo en el nivel
  function corredor(tk, snap, k, dir) {
    const sc = CFG.tickers[tk].sc; const et = new Date(snap.t.toLocaleString('en-US', { timeZone: 'America/New_York' })); const m = et.getHours() * 60 + et.getMinutes();
    const sk = snap.strikes.find(x => x.k === Number(k)); const ch = sk ? sk.ch : null;
    return m < 645 && (snap.spot - snap.flip) * dir > 20 * sc && ch != null && ch < 0;
  }
  const CORR = ' · 🏃 MODO CORREDOR: no apretar en el siguiente verde, sin objetivo fijo (trailing 15 al llegar a +25), hasta 3 h';
  function mapCtx(tk, snap, dir) {
    const sc = CFG.tickers[tk].sc, p = snap.spot; let ah = 0, bh = 0;
    for (const x of snap.strikes) { const r = (x.k - p) * dir / sc; const a = Math.abs(x.gf || 0); if (r > 3 && r <= 40) ah += a; else if (r < -3 && r >= -40) bh += a; }
    const gimb = (ah - bh) / Math.max(1, ah + bh); const flipOr = (p - snap.flip) * dir / sc;
    const et = new Date(snap.t.toLocaleString('en-US', { timeZone: 'America/New_York' })); const mm = et.getHours() * 60 + et.getMinutes();
    const fr = mm < 645 ? 'A' : mm < 780 ? 'B' : mm < 870 ? 'C' : 'D'; const lejos = flipOr > 40;
    const [p50, p80] = EXT[fr][lejos ? 1 : 0];
    const filtro = !(flipOr <= 40 && gimb >= 0);   // cerca del flip con más gamma adelante = no
    const wa = state.wallsAll[tk]; let tgt = null;
    if (wa) { const w = dir < 0 ? wa.pw : wa.cw; if (w != null && (w - p) * dir >= 15 * sc) tgt = w; }
    return { gimb, flipOr, lejos, p50: p50 * sc, p80: p80 * sc, corto: p80 <= 30, filtro, tgt, sc,
      text: `mapa: ${gimb < 0 ? 'más gamma detrás que adelante' : 'más gamma adelante'} · flip a ${(flipOr * sc).toFixed(sc < 1 ? 1 : 0)} pts${lejos ? ' (lejos)' : ''} · recorrido probable ~${(p50 * sc).toFixed(sc < 1 ? 2 : 0)} pts (1 de cada 5 veces > ${(p80 * sc).toFixed(sc < 1 ? 2 : 0)})` +
        (p80 <= 30 ? ' · recorrido corto: asegura ganancia pronto' : '') + ` · objetivo: ${tgt != null ? 'muro de todas las expiraciones en ' + tgt : 'salida estructural (anclas)'}` };
  }
  async function refreshWallsAll(tk) {
    state.wtime[tk] = Date.now();
    try { const tr = await get('/trace', { ticker: tk, exps: 'all' }); if (tr && tr.call_wall != null) state.wallsAll[tk] = { cw: tr.call_wall, pw: tr.put_wall }; } catch (e) {}
  }
  function majors(tk, snap) {
    const { sc, minGex } = CFG.tickers[tk];
    const near = snap.strikes.filter(s => Math.abs(s.k - snap.spot) <= CFG.nearPts * sc && s.gf);
    const ois = snap.strikes.map(s => Math.max(s.oc, s.op)).sort((a, b) => a - b);
    const thr = ois[Math.floor(ois.length * 0.7)] || 0;
    let M = near.sort((a, b) => Math.abs(b.gf) - Math.abs(a.gf)).slice(0, 6).filter(s => Math.max(s.oc, s.op) >= thr && Math.abs(s.gf) >= minGex);
    const pw = snap.strikes.find(s => s.k === snap.pw);
    if (pw && Math.abs(pw.k - snap.spot) <= CFG.nearPts * sc && !M.includes(pw)) M.push(pw);
    return M;
  }
  function record(tk, snap) {
    const H = (state.hist[tk] = state.hist[tk] || {});
    for (const s of snap.strikes) {
      const d = dom(s); if (!d) continue;
      const arr = (H[s.k] = H[s.k] || []);
      if (!arr.length || arr[arr.length - 1].d !== d) arr.push({ t: snap.t, d });
      while (arr.length && snap.t - arr[0].t > 3 * 3600e3) arr.shift();
    }
  }
  function stability(tk, k, t) { const a = (state.hist[tk] || {})[k]; if (!a || !a.length) return 0; return Math.round((t - a[a.length - 1].t) / 60000); }
  function flips60(tk, k, t) { const a = (state.hist[tk] || {})[k] || []; return a.filter(x => t - x.t <= 3600e3).length - 1; }
  function prevDom(tk, k, t, minAgo) { const a = (state.hist[tk] || {})[k] || []; let v = null; for (const x of a) { if (t - x.t >= minAgo * 60000) v = x.d; } return v; }

  function evaluate(tk, snap, emit) {
    const { sc } = CFG.tickers[tk];
    record(tk, snap);
    const M = majors(tk, snap); const t = snap.t; const p = snap.spot;
    const reg = p < snap.flip ? -1 : 1; const nearFlip = Math.abs(p - snap.flip) < CFG.flipZone * sc;
    state.last[tk] = { snap, M, reg, nearFlip, mom: momentum(tk, t, p) };
    // 1) giros: se avisa en el minuto en que se detecta (igual que el backtest) y a los 5 min se confirma si se sostuvo
    state.fired = state.fired || {};
    for (const s of M) {
      const d = dom(s); const before = prevDom(tk, s.k, t, 6); const st = stability(tk, s.k, t);
      const a = (state.hist[tk] || {})[s.k] || []; const chg = a.length ? a[a.length - 1].t.getTime() : 0;
      const key = tk + s.k + ':' + chg; if (state.fired[key] || st > CFG.holdMin) continue;
      const rel = (s.k - p) / sc; const fl = flips60(tk, s.k, t);
      if (d < 0 && s.gf < 0 && before > 0 && Math.abs(rel) <= CFG.giroRange) {
        state.fired[key] = 1;
        const dir = rel >= -1.5 ? -1 : 1; const mc = mapCtx(tk, snap, dir); const ok = dir === reg && fl < CFG.disputeFlips && !nearFlip && mc.filtro;
        const stop = dir < 0 ? Math.max(s.k, p) + 7 * sc : s.k - 7 * sc;
        emit({ tk, t, level: ok ? 'SEÑAL' : 'info', title: `${tk} ${s.k}: GIRO a combustible → ${dir < 0 ? 'VENTA' : 'COMPRA'}${ok ? '' : ' (descartada)'}`,
          detail: `${domLabel(s)} · GEX ${(s.gf / 1e6).toFixed(0)}M · precio ${p.toFixed(2)} (${(s.k - p).toFixed(2)} al strike) · stop ${stop.toFixed(2)}` +
            (ok ? '' : ` · motivo: ${dir !== reg ? 'contra el régimen' : fl >= CFG.disputeFlips ? 'en disputa (' + fl + ' cambios/h)' : nearFlip ? 'muy cerca del flip' : 'cerca del flip con más gamma adelante'}`) + ` · ${mc.text}`, ex: mc });
        if (ok) state.pending[key] = { tk, k: s.k, t0: t, want: -1 };
      } else if (d > 0 && s.gf > 0 && before < 0 && Math.abs(rel) <= CFG.giroRange) {
        state.fired[key] = 1;
        emit({ tk, t, level: 'info', title: `${tk} ${s.k}: aparece DEFENSA`, detail: `${domLabel(s)} · GEX +${(s.gf / 1e6).toFixed(0)}M · precio ${p.toFixed(2)}` });
      }
    }
    for (const [key, pd] of Object.entries(state.pending)) {
      if (pd.tk !== tk) continue;
      const s = snap.strikes.find(x => x.k === pd.k); if (!s) { delete state.pending[key]; continue; }
      if (dom(s) !== pd.want) { delete state.pending[key]; emit({ tk, t, level: 'AVISO', title: `${tk} ${pd.k}: el giro se DEVOLVIÓ`, detail: `${domLabel(s)} · el strike volvió a defensa; no es salida automática, manda tu stop estructural` }); continue; }
      if ((t - pd.t0) / 60000 >= CFG.holdMin) { delete state.pending[key]; emit({ tk, t, level: 'info', title: `${tk} ${pd.k}: giro sostenido 5 min`, detail: `${domLabel(s)} · GEX ${(s.gf / 1e6).toFixed(0)}M` }); }
    }
    // 2) toques y rupturas por clasificación (umbral 5/10/15)
    const W = (state.watch[tk] = state.watch[tk] || {});
    const mom = momentum(tk, t, p);
    const recent = state.recentSpots = state.recentSpots || {}; const rs = (recent[tk] = recent[tk] || []);
    rs.push({ t, p });
    // aviso de acercamiento: a menos de 4 pts (SPX eq.) de un nivel relevante, viniendo de lejos
    const NR = (state.near[tk] = state.near[tk] || {});
    for (const s of M) {
      const dist = Math.abs(p - s.k) / sc;
      if (dist <= 4 && dist > 1.5 && (!NR[s.k] || t - NR[s.k] > 30 * 60000) && rs.some(x => Math.abs(x.p - s.k) >= 8 * sc)) {
        NR[s.k] = t; const pf = profile(tk, snap, s, mom);
        emit({ tk, t, level: 'NIVEL', title: `${tk} se acerca a ${s.k}${s.k === snap.pw ? ' (PW)' : ''} · ${pf.tag}`, detail: `${domLabel(s)} · GEX ${(s.gf / 1e6).toFixed(0)}M · ${pf.text}` });
      }
    } while (rs.length && t - rs[0].t > 15 * 60000) rs.shift();
    for (const s of M) {
      if (Math.abs(p - s.k) <= 1.5 * sc && !W[s.k]) {
        const etA = new Date(snap.t.toLocaleString('en-US', { timeZone: 'America/New_York' })); const mA = etA.getHours() * 60 + etA.getMinutes();
        const apertura = mA < 585 && (s.k === snap.cw || s.k === snap.pw); // abre pegado al call wall / put wall: cuenta el toque aunque no venga de lejos
        const far = rs.some(x => Math.abs(x.p - s.k) >= 8 * sc); if (!far && !apertura) continue;
        const avg = rs.reduce((a, x) => a + x.p, 0) / rs.length; const side = far ? Math.sign(avg - s.k) : (s.k === snap.cw ? -1 : 1); if (!side) continue;
        const isPut = s.op >= s.oc; const lab = isPut ? (s.sp < 0 ? 'pc' : 'pv') : (s.sc < 0 ? 'cc' : 'cv');
        const est = stability(tk, s.k, t) >= 30;
        const th = (lab === 'cc' && side < 0 && est) ? 5 : (lab === 'pc' && side > 0 && !est) ? 10 : 15;
        W[s.k] = { side, t0: t, th: th * sc, lab: domLabel(s), maxpen: 0 };
        const pf = profile(tk, snap, s, mom); NR[s.k] = t;
        emit({ tk, t, level: 'NIVEL', title: `${tk} toca ${s.k} desde ${side > 0 ? 'arriba' : 'abajo'} · ${pf.tag}`, detail: `${domLabel(s)}${est ? ' (estable)' : ' (reciente)'} · GEX ${(s.gf / 1e6).toFixed(0)}M · ruptura si se pasa ${(th * sc).toFixed(2)} · ${pf.text}${corredor(tk, snap, s.k, side) ? CORR + ' (si opera a favor del toque)' : ''}` });
        // Regla D (en observación): sube a un nivel rojo con calls comprados -> no vender el rebote; orden de compra 5 pts encima
        const etD = new Date(snap.t.toLocaleString('en-US', { timeZone: 'America/New_York' })); const mD = etD.getHours() * 60 + etD.getMinutes();
        if (lab === 'cc' && side < 0 && s.gf < 0 && mD < 930 && !(mD >= 780 && mD < 870)) {
          const DD = (state.reglaD[tk] = state.reglaD[tk] || {}); const ent = s.k + 5 * sc;
          DD[s.k] = { t0: t, ent, stop: ent - 12 * sc, cancel: s.k - 15 * sc };
          emit({ tk, t, level: 'NIVEL', title: `${tk} ${s.k}: REGLA D armada (observación) · no vender el rebote`, detail: `GEX rojo con calls comprados y precio desde abajo · compra stop en ${ent.toFixed(2)} · stop ${(ent - 12 * sc).toFixed(2)} · trailing 15 al llegar a +${(30 * sc).toFixed(2)} · se cancela bajo ${(s.k - 15 * sc).toFixed(2)} o a los 90 min · backtest: 7 casos, +108 pts, 86%` });
        }
        // Regla E (en observación): baja a un nivel rojo con puts comprados -> no comprar el rebote; orden de venta 10 pts debajo
        if (lab === 'pc' && side > 0 && s.gf < 0 && mD < 930 && !(mD >= 780 && mD < 870)) {
          const EE = (state.reglaD[tk] = state.reglaD[tk] || {}); const ent = s.k - 10 * sc;
          EE['E' + s.k] = { E: true, k: s.k, t0: t, ent, stop: ent + 12 * sc, cancel: s.k + 15 * sc };
          emit({ tk, t, level: 'NIVEL', title: `${tk} ${s.k}: REGLA E armada (observación) · no comprar el rebote`, detail: `GEX rojo con puts comprados y precio desde arriba · venta stop en ${ent.toFixed(2)} · stop ${(ent + 12 * sc).toFixed(2)} · trailing 15 al llegar a +${(30 * sc).toFixed(2)} · se cancela sobre ${(s.k + 15 * sc).toFixed(2)} o a los 90 min · backtest: 11 casos, +158 pts, 82%` });
        }
      }
    }
    for (const [key, o] of Object.entries(state.reglaD[tk] || {})) {
      const k = o.E ? o.k : key; const R = o.E ? 'E' : 'D';
      const etO = new Date(snap.t.toLocaleString('en-US', { timeZone: 'America/New_York' })); const mO = etO.getHours() * 60 + etO.getMinutes();
      const cancelled = o.E ? p >= o.cancel : p <= o.cancel;
      if ((t - o.t0) / 60000 > 90 || cancelled || mO >= 958) { delete state.reglaD[tk][key]; emit({ tk, t, level: 'info', title: `${tk} ${k}: Regla ${R} cancelada`, detail: cancelled ? `el precio se fue 15 pts al otro lado del nivel` : 'pasó el tiempo sin romper' }); continue; }
      if (o.E ? p <= o.ent : p >= o.ent) { delete state.reglaD[tk][key]; emit({ tk, t, level: 'SEÑAL', title: `${tk} ${k}: REGLA ${R} → ${o.E ? 'VENTA' : 'COMPRA'} de ruptura (observación)`, detail: `entrada ${o.ent.toFixed(2)} · stop ${o.stop.toFixed(2)} · trailing 15 al llegar a +30 · máx. 2 h · regla nueva, validar en vivo${corredor(tk, snap, k, o.E ? -1 : 1) ? CORR : ''}` }); }
    }
    for (const [k, w] of Object.entries(W)) {
      const pen = (Number(k) - p) * w.side; w.maxpen = Math.max(w.maxpen, pen);
      if ((t - w.t0) / 60000 > 90 || pen <= -10 * sc || pen >= 30 * sc) { delete W[k]; continue; }
      if (pen >= w.th) {
        const dir = -w.side; const mc = mapCtx(tk, snap, dir); delete W[k];
        const sk = snap.strikes.find(x => x.k === Number(k)); const px = sk ? profile(tk, snap, sk, mom).extra.filter(e => !/sin DEX/.test(e)) : [];
        const etR = new Date(snap.t.toLocaleString('en-US', { timeZone: 'America/New_York' })); const tarde = etR.getHours() * 60 + etR.getMinutes() >= 870;
        const perfRebote = sk && sk.dx != null && sk.ch != null && sk.dx < 0 && sk.ch < 0;
        const ok = dir === reg && !nearFlip && mc.filtro && !tarde && !perfRebote;
        emit({ tk, t, level: ok ? 'SEÑAL' : 'info', title: `${tk} ${k}: RUPTURA confirmada → ${dir < 0 ? 'VENTA' : 'COMPRA'}${ok ? '' : ' (descartada)'}`,
          detail: `${w.lab} · precio ${p.toFixed(2)} · stop si vuelve a ${(Number(k) + w.side * 3 * sc).toFixed(2)}${ok ? '' : ' · motivo: ' + (dir !== reg ? 'contra el régimen' : nearFlip ? 'muy cerca del flip' : !mc.filtro ? 'cerca del flip con más gamma adelante' : tarde ? 'ruptura después de 14:30 NY' : 'el strike tiene DEX y charm negativos (perfil de rebote)')}${px.length ? ' · ★ ' + px.join(' · ★ ') : ''}${ok && corredor(tk, snap, k, dir) ? CORR : ''} · ${mc.text}`, ex: mc });
      }
    }
  }

  // ---------- avisos ----------
  function beep(f = 880, ms = 220) { try { const a = new (window.AudioContext || window.webkitAudioContext)(); const o = a.createOscillator(); const g = a.createGain(); o.frequency.value = f; o.connect(g); g.connect(a.destination); g.gain.value = 0.15; o.start(); setTimeout(() => { o.stop(); a.close(); }, ms); } catch (e) {} }
  function emitLive(a) {
    state.alerts.unshift(a); state.alerts = state.alerts.slice(0, 60);
    console.log(`[QG] ${hhmm(a.t)} ${a.title} — ${a.detail}`);
    if (a.level === 'NIVEL' && CFG.sound) beep(660, 150);
    if (a.level === 'NIVEL') { try { if (Notification.permission === 'granted') new Notification(a.title, { body: a.detail }); } catch (e) {} }
    if (a.level === 'SEÑAL' || a.level === 'AVISO') {
      if (CFG.sound) { beep(988); setTimeout(() => beep(1318), 260); }
      try { if (Notification.permission === 'granted') new Notification(a.title, { body: a.detail }); } catch (e) {}
    }
    render();
  }

  // <render> (función pura: la usan el script en vivo y la página de demostración)
  const QG_CSS = `
#qg-root{--bg:#0b1220;--card:#101a2c;--line:#24324a;--fg:#e5e7eb;--mut:#94a3b8;--faint:#64748b;--g:#22c55e;--r:#ef4444;--y:#fde047;--o:#fb923c;--b:#93c5fd;--flip:#7aa2f7;
  background:var(--bg);color:var(--fg);font:12px/1.35 Inter,system-ui,-apple-system,sans-serif;border:1px solid #334155;border-radius:12px;overflow:hidden;box-shadow:0 10px 34px rgba(0,0,0,.55)}
#qg-root *{box-sizing:border-box}
#qg-root .hd{cursor:move;padding:8px 10px;background:#111827;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center;gap:8px}
#qg-root .hd b{font-size:13px}
#qg-root .hd .live{font-size:10px;color:var(--mut);display:flex;align-items:center;gap:5px}
#qg-root .dot{width:7px;height:7px;border-radius:50%;background:var(--faint)}#qg-root .dot.on{background:var(--g);box-shadow:0 0 0 3px rgba(34,197,94,.2)}
#qg-root button{background:#1f2937;color:var(--fg);border:1px solid #475569;border-radius:6px;padding:2px 7px;cursor:pointer;font:inherit}
#qg-root .bd{padding:8px 10px;display:grid;gap:10px}
#qg-root .err{background:#7f1d1d;padding:6px 10px}
#qg-root .ban{border-radius:10px;padding:8px 10px;display:grid;grid-template-columns:auto 1fr auto;gap:2px 10px;align-items:center}
#qg-root .ban.v{background:linear-gradient(90deg,rgba(239,68,68,.28),rgba(239,68,68,.06));border:1px solid rgba(239,68,68,.55)}
#qg-root .ban.c{background:linear-gradient(90deg,rgba(34,197,94,.26),rgba(34,197,94,.06));border:1px solid rgba(34,197,94,.55)}
#qg-root .ban .ic{font-size:22px;grid-row:span 2}
#qg-root .ban .t1{font-weight:700;font-size:14px}
#qg-root .ban .t2{color:var(--mut);font-size:11px}
#qg-root .ban .ext{grid-column:1/-1;display:grid;grid-template-columns:auto 1fr auto;gap:2px 8px;align-items:center;margin-top:6px;padding-top:6px;border-top:1px solid rgba(255,255,255,.12)}
#qg-root .ban .exl{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
#qg-root .ban .exb{height:6px;background:rgba(255,255,255,.12);border-radius:3px;overflow:hidden}
#qg-root .ban .exb b{display:block;height:100%;background:var(--y)}
#qg-root .ban .exv{font:600 11px ui-monospace,Menlo,monospace}
#qg-root .ban .exn{grid-column:1/-1;font-size:11px;color:#e2e8f0}
#qg-root .ban .age{font-size:11px;color:var(--mut);grid-row:span 2;text-align:right}
#qg-root .tk{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px}
#qg-root .tkh{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px}
#qg-root .tkh .n{font-weight:700;font-size:13px}
#qg-root .tkh .px{font:600 15px ui-monospace,Menlo,monospace}
#qg-root .pill{font-size:10px;font-weight:700;letter-spacing:.04em;padding:2px 7px;border-radius:999px}
#qg-root .pill.v{background:rgba(239,68,68,.18);color:#fca5a5}#qg-root .pill.c{background:rgba(34,197,94,.18);color:#86efac}#qg-root .pill.w{background:rgba(253,224,71,.16);color:var(--y)}
#qg-root .meta{color:var(--mut);font-size:11px;display:flex;flex-wrap:wrap;gap:4px 10px;margin:4px 0 6px}
#qg-root .meter{display:inline-flex;align-items:center;gap:5px}
#qg-root .meter i{position:relative;display:inline-block;width:56px;height:6px;border-radius:3px;background:#1e293b;overflow:hidden}
#qg-root .meter i b{position:absolute;left:0;top:0;bottom:0;background:var(--b)}#qg-root .meter i b.fast{background:var(--o)}
#qg-root .meter i u{position:absolute;top:-1px;bottom:-1px;width:1px;background:#e2e8f0}
#qg-root .lad{display:grid;gap:1px}
#qg-root .row{display:grid;grid-template-columns:58px 50px 1fr 74px 44px;gap:6px;align-items:center;padding:2px 4px;border-radius:5px}
#qg-root .row.maj{background:rgba(148,163,184,.07)}
#qg-root .row.min{opacity:.5}
#qg-root .k{font:500 11px ui-monospace,Menlo,monospace;white-space:nowrap}
#qg-root .k s{text-decoration:none;font:700 8.5px Inter,system-ui,sans-serif;padding:0 3px;border-radius:3px;margin-left:3px;vertical-align:1px}
#qg-root .k s.pw{background:rgba(239,68,68,.25);color:#fca5a5}#qg-root .k s.cw{background:rgba(34,197,94,.22);color:#86efac}
#qg-root .bar{position:relative;height:12px}
#qg-root .bar:before{content:"";position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:#334155}
#qg-root .bar b{position:absolute;top:1px;bottom:1px;border-radius:2px}
#qg-root .gv{font:10px ui-monospace,Menlo,monospace;text-align:right;white-space:nowrap}
#qg-root .oi{font-size:10.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#qg-root .st{display:grid;gap:2px;justify-items:end;font-size:10px;color:var(--mut)}
#qg-root .st i{display:block;width:40px;height:4px;border-radius:2px;background:#1e293b;overflow:hidden}
#qg-root .st i b{display:block;height:100%;background:var(--b)}
#qg-root .st.dis{color:var(--o)}#qg-root .st.dis i b{background:var(--o)}
#qg-root .sub{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:4px;align-items:center;padding:1px 0 3px 0}
#qg-root .chip{font-size:9.5px;padding:1px 6px;border-radius:999px;background:#1e293b;color:#cbd5e1;white-space:nowrap}
#qg-root .chip.f{background:rgba(147,197,253,.16);color:var(--b)}#qg-root .chip.va{background:rgba(196,181,253,.16);color:#c4b5fd}
#qg-root .chip.sr{background:rgba(34,197,94,.18);color:#86efac}#qg-root .chip.sb{background:rgba(239,68,68,.2);color:#fca5a5}
#qg-root .chip.sc{background:rgba(253,224,71,.14);color:var(--y)}#qg-root .chip.sn{background:rgba(251,146,60,.18);color:var(--o)}
#qg-root .prob{display:flex;height:8px;width:120px;border-radius:4px;overflow:hidden;background:#1e293b}
#qg-root .prob b{display:block;height:100%}
#qg-root .pt{font-size:10px;color:var(--mut)}
#qg-root .price{display:grid;grid-template-columns:auto 1fr;gap:6px;align-items:center;padding:1px 4px}
#qg-root .price span{white-space:nowrap;font:700 11px ui-monospace,Menlo,monospace;color:var(--y)}
#qg-root .price hr{border:0;border-top:1.5px solid var(--y);margin:0;opacity:.8}
#qg-root .als{display:flex;flex-direction:column;gap:5px}
#qg-root .al{flex:none}
#qg-root .al{border-left:3px solid var(--faint);background:#0f172a;border-radius:6px;padding:5px 8px;cursor:pointer}
#qg-root .al.SEÑAL{border-color:var(--y)}#qg-root .al.AVISO{border-color:var(--o)}#qg-root .al.NIVEL{border-color:var(--b)}
#qg-root .al .t{display:flex;gap:6px;align-items:baseline}
#qg-root .al time{font:600 11px ui-monospace,Menlo,monospace;color:var(--mut)}
#qg-root .al.SEÑAL .tt{color:var(--y);font-weight:600}#qg-root .al.AVISO .tt{color:var(--o)}#qg-root .al.NIVEL .tt{color:var(--b)}#qg-root .al.info .tt{color:#cbd5e1}
#qg-root .al .d{color:var(--mut);font-size:11px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-top:2px}
#qg-root .al.open .d{-webkit-line-clamp:unset}
#qg-root .ft{color:var(--faint);font-size:10.5px;padding:0 10px 8px}
#qg-root .sec{font-weight:700;font-size:11px;color:var(--mut);letter-spacing:.06em;text-transform:uppercase}
`;
  function renderPanel(V, alerts, o) {
    const fx = (v, d) => (v == null || isNaN(v) ? '—' : (+v).toFixed(d));
    const kfmt = (n) => n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n);
    let h = `<style>${QG_CSS}</style>`;
    h += (o.err ? `<div class="err">${o.err}</div>` : '');
    h += `<div class="hd" data-h="1"><b data-h="1">Detector de giros Q OI</b><span class="live"><span class="dot ${o.open ? 'on' : ''}"></span>${o.open ? 'En vivo' : (o.replay ? 'Repetición' : 'Mercado cerrado')} · ${o.clock}</span><span>
      <button id="qg-snd" type="button" title="Sonido">${o.sound ? '🔔' : '🔕'}</button> <button id="qg-min" type="button" title="Minimizar">${o.min ? '+' : '–'}</button></span></div>`;
    if (o.min) return h;
    h += '<div class="bd">';
    // banner de la última señal (15 min)
    const sig = alerts.find(a => a.lv === 'SEÑAL' && o.now - a.m <= 15);
    if (sig) {
      const sell = /VENTA/.test(sig.ti); const stop = (sig.de.match(/stop[^·]*?([\d.]+)/) || [])[1];
      h += `<div class="ban ${sell ? 'v' : 'c'}"><span class="ic">${sell ? '▼' : '▲'}</span><span class="t1">${sell ? 'VENTA' : 'COMPRA'} · ${sig.ti.split(':')[0]}</span><span class="age">hace ${Math.max(0, o.now - sig.m)} min<br>${sig.hm}</span>
        <span class="t2">${/GIRO/.test(sig.ti) ? 'Giro a combustible' : 'Ruptura confirmada'}${stop ? ' · stop ' + stop : ''}</span>
        ${sig.ex ? `<span class="ext"><span class="exl">Recorrido probable</span><span class="exb"><b style="width:${Math.min(100, sig.ex.p50 / Math.max(sig.ex.p80, 1) * 100)}%"></b></span><span class="exv">~${sig.ex.p50} · hasta ${sig.ex.p80} (1 de 5)</span>
          <span class="exn">${sig.ex.corto ? 'Recorrido corto: asegura pronto' : 'Hay espacio: deja correr con anclas'}${sig.ex.tgt != null ? ' · objetivo muro todas exp. ' + sig.ex.tgt : ''} · ${sig.ex.gimb < 0 ? 'gamma atrás' : 'gamma adelante'} · flip ${sig.ex.flip} pts</span></span>` : ''}</div>`;
    }
    for (const v of V) {
      if (!v.p) { h += `<div class="tk"><span class="n">${v.tk}</span> <span class="pt">esperando datos…</span></div>`; continue; }
      const dFlip = (v.p - v.f) / v.sc;
      h += `<div class="tk"><div class="tkh"><span class="n">${v.tk}</span><span class="px">${fx(v.p, 2)}</span>
        <span class="pill ${v.reg < 0 ? 'v' : 'c'}">${v.reg < 0 ? 'SOLO VENTAS' : 'SOLO COMPRAS'}</span>${v.nf ? '<span class="pill w">CERCA DEL FLIP · NO OPERAR</span>' : ''}</div>
        <div class="meta"><span>flip ${fx(v.f, 2)} (${dFlip > 0 ? '+' : ''}${fx(dFlip * v.sc, 2)})</span><span>PW ${v.pw}</span><span>CW ${v.cw}</span>
        ${v.mo != null ? `<span class="meter">llegada <i><b class="${v.rap ? 'fast' : ''}" style="width:${Math.min(100, v.mo / v.sc / 20 * 100)}%"></b><u style="left:${8.5 / 20 * 100}%"></u></i>${v.rap ? 'rápida' : 'lenta'} ${fx(v.mo, 2)}</span>` : ''}</div><div class="lad">`;
      const gmax = Math.max(1, ...v.rows.map(r => Math.abs(r.g)));
      let priced = false;
      const priceRow = `<div class="price"><span>► ${fx(v.p, 2)}</span><hr></div>`;
      for (const r of v.rows) {
        if (!priced && r.k < v.p) { h += priceRow; priced = true; }
        const w = Math.abs(r.g) / gmax * 50; const pos = r.g >= 0;
        const lab = r.d > 0 ? 'vend' : r.d < 0 ? 'comp' : '—';
        h += `<div class="row ${r.maj ? 'maj' : 'min'}"><span class="k">${r.k}${r.k === v.pw ? '<s class="pw">PW</s>' : ''}${r.k === v.cw ? '<s class="cw">CW</s>' : ''}</span>
          <span class="gv" style="color:${pos ? '#86efac' : '#fca5a5'}">${r.g > 0 ? '+' : ''}${r.g.toLocaleString('en-US')}M</span>
          <span class="bar"><b style="${pos ? 'left:50%' : 'right:50%'};width:${w}%;background:${pos ? 'var(--g)' : 'var(--r)'}"></b></span>
          <span class="oi" style="color:${r.d > 0 ? '#86efac' : r.d < 0 ? '#fca5a5' : 'var(--mut)'}">${kfmt(r.oi)} ${r.lab} ${lab}</span>
          <span class="st ${r.fl >= 3 ? 'dis' : ''}">${r.fl >= 3 ? '⚠ ' : ''}${r.st}m<i><b style="width:${Math.min(100, r.st / 60 * 100)}%"></b></i></span>`;
        if (r.pf) {
          const p = r.pf; const chips = [`<span class="chip f">${p.fuerza}</span>`];
          if (p.vanna === 'alta') chips.push('<span class="chip va">vanna alta</span>');
          for (const x of p.x) chips.push(`<span class="chip ${x === '★rebote' ? 'sr' : x === '★ruptura' ? 'sb' : x === '★cierre' ? 'sc' : 'sn'}">${x}</span>`);
          if (p.n) chips.push(`<span class="prob" title="rebota ${p.reb}% · rompe ${p.rup}% · pegado ${p.peg}% (${p.n} casos)"><b style="width:${p.reb}%;background:var(--g)"></b><b style="width:${p.rup}%;background:var(--r)"></b><b style="width:${p.peg}%;background:#64748b"></b></span><span class="pt">${p.reb}/${p.rup}/${p.peg}${p.n < 10 ? ' (pocos casos)' : ''}${p.stop ? ' · stop ' + fx(p.stop, 2) : ''}</span>`);
          h += `<div class="sub">${chips.join('')}</div>`;
        }
        h += '</div>';
      }
      if (!priced) h += priceRow;
      h += '</div></div>';
    }
    h += `<div class="sec">Avisos</div><div class="als">`;
    const ic = (a) => a.lv === 'SEÑAL' ? (/VENTA/.test(a.ti) ? '▼' : '▲') : a.lv === 'AVISO' ? '↺' : a.lv === 'NIVEL' ? '◆' : '·';
    h += alerts.slice(0, o.maxAlerts || 30).map(a => `<div class="al ${a.lv}"><div class="t"><time>${a.hm}</time><span class="tt">${ic(a)} ${a.ti}</span></div><div class="d">${a.de}</div></div>`).join('') || '<div class="pt">Sin avisos todavía.</div>';
    h += `</div></div><div class="ft">Barras: GEX de flujo (verde defensa, rojo combustible). Barra azul: minutos sin cambio de signo. Prob.: rebota/rompe/pegado en 41 días de SPX. Análisis, no recomendación.</div>`;
    return h;
  }
  // </render>

  // ---------- panel ----------
  const minOf = (d) => d.getHours() * 60 + d.getMinutes();
  function buildVM(tk) {
    const L = state.last[tk]; const sc = CFG.tickers[tk].sc;
    if (!L) return { tk, sc };
    const s = L.snap;
    const rows = s.strikes.filter(x => Math.abs(x.k - s.spot) <= 30 * sc && x.gf).sort((a, b) => b.k - a.k).map(x => {
      const maj = L.M.includes(x); let pf = null;
      if (maj) { const p = profile(tk, s, x, L.mom); pf = { fuerza: p.fuerza, vanna: p.vanna, x: p.short, n: p.st ? p.st[0] : 0, reb: p.st ? p.st[1] : 0, rup: p.st ? p.st[2] : 0, peg: p.st ? p.st[3] : 0, stop: p.stop }; }
      return { k: x.k, g: Math.round(x.gf / 1e6), d: dom(x), lab: x.op >= x.oc ? 'P' : 'C', oi: Math.max(x.oc, x.op), st: stability(tk, x.k, s.t), fl: flips60(tk, x.k, s.t), maj, pf };
    });
    return { tk, sc, p: s.spot, f: +s.flip, pw: s.pw, cw: s.cw, reg: L.reg, nf: L.nearFlip ? 1 : 0, mo: L.mom == null ? null : L.mom * sc, rap: L.mom != null && L.mom >= 8.5, rows };
  }
  const alertVM = (a) => ({ m: minOf(a.t), hm: hhmm(a.t), lv: a.level, ti: a.title, de: a.detail, tk: a.tk, ex: a.ex ? { p50: +a.ex.p50.toFixed(2), p80: +a.ex.p80.toFixed(2), corto: a.ex.corto, tgt: a.ex.tgt, gimb: +a.ex.gimb.toFixed(2), flip: +(a.ex.flipOr * a.ex.sc).toFixed(a.ex.sc < 1 ? 1 : 0) } : null });
  let box;
  function ui() {
    box = document.createElement('div'); box.id = 'qg-root';
    box.style.cssText = 'position:fixed;right:12px;bottom:12px;width:470px;max-width:calc(100vw - 24px);max-height:85vh;overflow:auto;z-index:99999';
    document.body.appendChild(box);
    let drag = null; box.addEventListener('mousedown', e => { if (e.target.dataset && e.target.dataset.h) drag = { x: e.clientX - box.offsetLeft, y: e.clientY - box.offsetTop }; });
    window.addEventListener('mousemove', e => { if (drag) { box.style.left = (e.clientX - drag.x) + 'px'; box.style.top = (e.clientY - drag.y) + 'px'; box.style.right = 'auto'; box.style.bottom = 'auto'; } });
    window.addEventListener('mouseup', () => drag = null);
    box.addEventListener('click', e => { const al = e.target.closest && e.target.closest('.al'); if (al) al.classList.toggle('open'); });
    render();
  }
  function render() {
    if (!box) return;
    const now = new Date();
    box.innerHTML = renderPanel(Object.keys(CFG.tickers).map(buildVM), state.alerts.map(alertVM),
      { now: minOf(now), clock: hhmm(now), open: marketOpen(), sound: CFG.sound, min: CFG.min, err: state.err });
    box.querySelector('#qg-snd').onclick = () => { CFG.sound = !CFG.sound; render(); };
    box.querySelector('#qg-min').onclick = () => { CFG.min = !CFG.min; render(); };
  }

  // ---------- ciclo en vivo ----------
  async function seed() {
    const day = etNow().toISOString().slice(0, 10);
    for (const tk of Object.keys(CFG.tickers)) {
      try { const h = await get('/historial', { ticker: tk, exps: '0dte', day }); for (const x of h.slice(-120)) { const s = histSnapshot(x); if (s) record(tk, s); } } catch (e) {}
    }
  }
  async function tick() {
    if (!marketOpen()) { render(); return; }
    for (const tk of Object.keys(CFG.tickers)) {
      try { const snap = await liveSnapshot(tk); state.err = null; evaluate(tk, snap, emitLive); } catch (e) { console.warn('[QG]', tk, e.message); }
    }
    render();
  }
  async function start() {
    ui();
    try { if (Notification.permission === 'default') Notification.requestPermission(); } catch (e) {}
    await seed(); await tick(); state.timer = setInterval(tick, CFG.everyMs);
  }

  // ---------- repetición de un día (para probar): QG.replay('2026-11-16') ----------
  async function replay(day) {
    const out = []; const save = { hist: state.hist, pending: state.pending, watch: state.watch, recent: state.recentSpots, fired: state.fired, last: state.last };
    state.hist = {}; state.pending = {}; state.watch = {}; state.recentSpots = {}; state.fired = {}; state.last = {}; const saveNear = state.near; state.near = {}; const saveD = state.reglaD; state.reglaD = {};
    const H = {}; for (const tk of Object.keys(CFG.tickers)) H[tk] = (await get('/historial', { ticker: tk, exps: '0dte', day })).map(histSnapshot).filter(Boolean);
    const times = [...new Set(Object.values(H).flat().map(s => s.t.getTime()))].sort();
    for (const tk of Object.keys(CFG.tickers)) for (const s of H[tk]) { const m = new Date(s.t.toLocaleString('en-US', { timeZone: 'America/New_York' })); const mm = m.getHours() * 60 + m.getMinutes(); if (mm < 570 || mm > 960) continue; evaluate(tk, s, a => out.push(a)); }
    Object.assign(state, { hist: save.hist, pending: save.pending, watch: save.watch, fired: save.fired, last: save.last }); state.near = saveNear; state.reglaD = saveD; state.recentSpots = save.recent;
    const fmt = (d) => new Date(d.toLocaleString('en-US', { timeZone: 'America/New_York' })).toTimeString().slice(0, 5);
    return out.sort((a, b) => a.t - b.t).map(a => `${fmt(a.t)} [${a.level}] ${a.title} — ${a.detail}`);
  }

  window.QG = { buildVM, renderPanel, alertVM, start, replay, state, CFG, running: true, stop: () => { clearInterval(state.timer); state.running = false; window.QG.running = false; box && box.remove(); } };
  if (!window.__QG_NO_AUTOSTART) start();
})();
