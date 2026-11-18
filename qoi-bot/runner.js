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
const { DIR, readCfg, nyNow, hhmm, sys, writeLog, postSignal, getNQ } = require('./comun');

const AUTH_KEY = 'sb-euwdfoswzwsqrknnvjvj-auth-token';
const API = 'https://api-spike.quantium.global';

async function probar() {
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
  await sesion.asegurar();
  const c = readCfg();
  const pageCfg = { modo: c.modo, webhook: (c.cuentas || []).some((a) => a.activa && a.webhook) ? '(en el programa)' : '', riesgoUSD: c.riesgoUSD, maxPerdidaR: c.maxPerdidaR, maxOps: c.maxOps,
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
    qbotLog: async (row) => { writeLog(row); console.log(`[${row.hora} NY] ${row.tk} ${row.msg} ${row.extra || ''}`); },
    qbotNQ: async () => (c.ratioMNQ ? null : await getNQ()),
  };
  ctx.window = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(DIR, 'bot.js'), 'utf8'), ctx, { filename: 'bot.js' });

  sys(`bot iniciado SIN NAVEGADOR en modo ${c.modo.toUpperCase()} · cuentas activas: ${(c.cuentas || []).filter((a) => a.activa && (a.webhook || a.tipo === 'mt5')).map((a) => a.nombre).join(', ') || 'ninguna (no se envía nada)'}`);

  const end = hhmm(c.cerrarHoraNY || '16:10');
  const salir = (msg, code = 0) => { sys(msg); clearInterval(timer); process.exit(code); };
  let fallos = 0;
  const timer = setInterval(async () => {
    const n = nyNow();
    if (n.getHours() * 60 + n.getMinutes() >= end) return salir('fin de la sesión: cerrando');
    try {
      if (await sesion.asegurar()) sys(`token renovado · caduca en ${sesion.minutosRestantes()} min`);
      fallos = 0;
    } catch (e) {
      fallos += 1; sys(`ERROR de sesión (${fallos}): ${e.message}`);
      if (fallos >= 3) return salir('sin sesión válida: el bot se detiene (corre "node runner.js --probar" tras copiar un token nuevo)', 1);
    }
    if (ctx.QBOT && !ctx.QBOT.running && !ctx.QBOT.state.apagado) { sys('el bot no estaba corriendo: arrancando'); ctx.QBOT.start(); }
  }, 60000);
  process.on('SIGINT', () => salir('detenido con Ctrl+C'));
}

(process.argv.includes('--probar') ? probar() : correr()).catch((e) => { try { sys('no arrancó: ' + e.message); } catch (x) { console.error(e); } process.exit(1); });
