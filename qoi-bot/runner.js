// Bot Q OI sin navegador: corre bot.js tal cual dentro de Node, pidiendo los datos a la API de Quantium
// con el token guardado (sesion.js lo renueva solo). Mismo config.json, mismos registros y mismo envío
// a TradersPost que launcher.js. Se cierra solo a la hora de cierre (cerrarHoraNY).
// Uso:  node runner.js            (sesión de trading normal)
//       node runner.js --probar   (comprueba token y API, no opera)
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const sesion = require('./sesion');
const mt5 = require('./mt5');
const { DIR, readCfg, nyNow, hhmm, sys, writeLog, postSignal, getNQ, avisar } = require('./comun');

const AUTH_KEY = 'sb-euwdfoswzwsqrknnvjvj-auth-token';
const API = 'https://api-spike.quantium.global';

async function probar() {
  if (readCfg().sesionDesdeChrome) sesion.usarChrome(true);
  const renovado = await sesion.asegurar();
  console.log(`token ${renovado ? 'renovado' : 'vigente'} · caduca en ${sesion.minutosRestantes()} min`);
  for (const [p, q] of [['/spot', { ticker: 'SPX' }], ['/trace', { ticker: 'QQQ', exps: '0dte' }]]) {
    const r = await fetch(`${API}${p}?${new URLSearchParams(q)}`, { headers: { Authorization: 'Bearer ' + sesion.accessToken() } });
    const j = await r.json().catch(() => null);
    console.log(p, r.status, j && j.spot != null ? `spot ${j.spot}` : '');
    if (!r.ok) process.exitCode = 1;
  }
}

async function correr() {
  const c = readCfg();
  if (c.sesionDesdeChrome) {
    // La sesión la renueva solo tu Chrome y llega por el relevo local; el bot nunca la renueva (no chocan)
    sesion.usarChrome(true);
    require('./relevo').iniciar((msg, importante) => { sys(msg); if (importante) avisar('Quantium', msg); });
    try { await sesion.asegurar(); sys(`sesión de Chrome vigente · caduca en ${sesion.minutosRestantes()} min`); }
    catch (e) { sys('esperando la sesión de Chrome: ' + e.message); avisar('Quantium: falta la sesión', 'Abre Quantium en tu Chrome'); }
  } else await sesion.asegurar();
  // Un error inesperado se registra y el bot sigue (no se cae a mitad de sesión)
  process.on('uncaughtException', (e) => sys('error inesperado (el bot sigue): ' + e.message));
  process.on('unhandledRejection', (e) => sys('error inesperado (el bot sigue): ' + (e && e.message)));
  // Si hay una cuenta MT5 activa y el EA no responde, abre MetaTrader 5 (restaura sus gráficos y el EA)
  if (c.modo === 'demo' && (c.cuentas || []).some((a) => a.activa && a.tipo === 'mt5')) {
    const e = mt5.estado();
    if (e.ok) sys(`MT5 listo: cuenta ${e.s.account} (${e.s.mode})`);
    else {
      sys('MT5 no responde (' + e.error + '): abriendo MetaTrader 5');
      try { require('child_process').execSync('open -a "MetaTrader 5"'); } catch (x) { sys('no se pudo abrir MT5: ' + x.message); }
      for (let i = 0; i < 24 && !mt5.estado().ok; i++) await new Promise((r) => setTimeout(r, 5000));
      const e2 = mt5.estado(); if (e2.ok) sys(`MT5 listo: cuenta ${e2.s.account} (${e2.s.mode})`); else { sys('ATENCIÓN: MT5 sigue sin responder; revisa que el EA esté en un gráfico y Algo Trading activo'); avisar('MT5 no responde', 'Revisa el EA y Algo Trading'); }
    }
  }
  const pageCfg = { modo: c.modo, pollMs: c.pollMs, webhook: (c.cuentas || []).some((a) => a.activa && a.webhook) ? '(en el programa)' : '', riesgoUSD: c.riesgoUSD, maxPerdidaR: c.maxPerdidaR, maxOps: c.maxOps,
    SPX: Object.assign({ pv: 5 }, c.SPX), QQQ: Object.assign({ pv: 2, ratio: c.ratioMNQ || null }, c.QQQ) };

  // Lo mínimo del navegador que usa bot.js
  const store = new Map();
  const localStorage = {
    getItem: (k) => (k === AUTH_KEY ? sesion.comoLocalStorage() : store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { if (k !== AUTH_KEY) store.set(k, String(v)); },
    removeItem: (k) => store.delete(k),
  };
  const ctx = {
    // bot.js ya manda cada línea por qbotLog: no repetirla en la consola
    console: Object.assign(Object.create(console), { log: (...a) => { if (a[0] !== '[QBOT]') console.log(...a); } }),
    fetch, URLSearchParams, URL, Blob, performance, Intl, Date, Math, JSON, Promise, Map, Set,
    setTimeout, clearTimeout, setInterval, clearInterval, localStorage,
    prompt: () => null,
    __QBOT_NO_UI: true,
    __QBOT_RUNNER: { cfg: pageCfg },
    qbotPost: async (body) => { try { return await postSignal(body); } catch (e) { return { ok: false, status: e.message }; } },
    qbotLog: async (row) => { writeLog(row); console.log(`[${row.hora} NY] ${row.tk} ${row.msg} ${row.extra || ''}`); if (/OK|FALLÓ/.test(row.extra || '') || /APAGADO|límite de pérdida/.test(row.msg)) avisar(`${row.tk} ${row.msg}`.slice(0, 120), row.extra); },
    qbotNQ: async () => (c.ratioMNQ ? null : await getNQ()),
  };
  ctx.window = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(DIR, 'bot.js'), 'utf8'), ctx, { filename: 'bot.js' });

  sys(`bot iniciado SIN NAVEGADOR en modo ${c.modo.toUpperCase()} · cuentas activas: ${(c.cuentas || []).filter((a) => a.activa && (a.webhook || a.tipo === 'mt5')).map((a) => a.nombre).join(', ') || 'ninguna (no se envía nada)'}`);

  const end = hhmm(c.cerrarHoraNY || '16:10');
  const salir = (msg, code = 0) => { sys(msg); clearInterval(timer); process.exit(code); };
  let fallos = 0; let mt5Caido = false; let mt5Reabierto = 0;
  const timer = setInterval(async () => {
    const n = nyNow();
    if (n.getHours() * 60 + n.getMinutes() >= end) return salir('fin de la sesión: cerrando');
    try {
      if (await sesion.asegurar()) sys(`token renovado · caduca en ${sesion.minutosRestantes()} min`);
      if (fallos && c.sesionDesdeChrome) sys('sesión de Chrome de nuevo vigente');
      fallos = 0;
    } catch (e) {
      fallos += 1;
      if (c.sesionDesdeChrome) { // en modo Chrome no se apaga: espera a que Chrome mande un token nuevo
        if (fallos === 1) { sys('sin sesión de Chrome: ' + e.message); avisar('Quantium: sin sesión', 'Abre Quantium en tu Chrome para que el bot siga'); }
      } else {
        sys(`ERROR de sesión (${fallos}): ${e.message}`); if (fallos === 1) avisar('Quantium: sesión con problemas', e.message);
        if (fallos >= 3) return salir('sin sesión válida: el bot se detiene (corre "node runner.js --probar" tras copiar un token nuevo)', 1);
      }
    }
    // Vigilancia de MT5: si el EA deja de responder, se reabre MetaTrader 5 (como mucho cada 3 minutos) y se avisa
    if (c.modo === 'demo' && (readCfg().cuentas || []).some((a) => a.activa && a.tipo === 'mt5')) {
      const e = mt5.estado();
      if (e.ok) { if (mt5Caido) { mt5Caido = false; sys('MT5 volvió a responder'); avisar('MT5 volvió', 'El EA responde de nuevo'); } }
      else if (Date.now() - mt5Reabierto > 180000) {
        mt5Caido = true; mt5Reabierto = Date.now(); sys('MT5 no responde (' + e.error + '): reabriendo MetaTrader 5'); avisar('MT5 no responde', 'Reabriendo MetaTrader 5');
        try { require('child_process').execSync('open -a "MetaTrader 5"'); } catch (x) { sys('no se pudo abrir MT5: ' + x.message); }
      }
    }
    if (ctx.QBOT && !ctx.QBOT.running && !ctx.QBOT.state.apagado) { sys('el bot no estaba corriendo: arrancando'); ctx.QBOT.start(); }
  }, 60000);
  process.on('SIGINT', () => salir('detenido con Ctrl+C'));
}

(process.argv.includes('--probar') ? probar() : correr()).catch((e) => { try { sys('no arrancó: ' + e.message); } catch (x) { console.error(e); } process.exit(1); });
