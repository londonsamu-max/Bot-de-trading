// Backtest de la máquina Q OI con los días que Quantium tiene guardados.
// Usa el MISMO bot.js (motor + conversión de datos) que el bot en vivo, minuto a minuto, sin mirar el futuro,
// con los mismos límites de config.json (maxOps por día e instrumento, maxPerdidaR del día).
// Uso: node backtest.js [SPX|QQQ|ambos] [máximo de días]      ej.: node backtest.js ambos 60
// Los datos se guardan en registros/cache/ para no volver a pedirlos.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const sesion = require('./sesion');
const { DIR, readCfg } = require('./comun');

const API = 'https://api-spike.quantium.global';
const CACHE = path.join(DIR, 'registros', 'cache');
fs.mkdirSync(CACHE, { recursive: true });
const TK = { SPX: { sc: 1, mg: 2.34e9, skip: false }, QQQ: { sc: 0.089, mg: 1.73e8, skip: true } }; // igual que bot.js

async function get(p, q) {
  await sesion.asegurar();
  const r = await fetch(`${API}${p}?${new URLSearchParams(q)}`, { headers: { Authorization: 'Bearer ' + sesion.accessToken() } });
  if (!r.ok) throw new Error(`${p} ${r.status}`);
  return r.json();
}
async function dia(tk, d) {
  const f = path.join(CACHE, `${tk}_${d}.json`);
  if (fs.existsSync(f)) return JSON.parse(fs.readFileSync(f, 'utf8'));
  const h = await get('/historial', { ticker: tk, exps: '0dte', day: d });
  if (d !== new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date())) fs.writeFileSync(f, JSON.stringify(h)); // hoy puede estar incompleto
  return h;
}

function cargarBot() {
  const ctx = { console: { log() {}, warn() {}, error() {} }, fetch, URLSearchParams, URL, Blob, performance, Intl, Date, Math, JSON, Promise, Map, Set,
    setTimeout, clearTimeout, setInterval, clearInterval, localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, prompt: () => null, __QBOT_NO_UI: true };
  ctx.window = ctx; ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(DIR, 'bot.js'), 'utf8'), ctx, { filename: 'bot.js' });
  return ctx;
}
const nyHM = (t) => new Date(t.toLocaleString('en-US', { timeZone: 'America/New_York' })).toTimeString().slice(0, 5);

(async () => {
  const cfg = readCfg();
  const quien = (process.argv[2] || 'ambos').toUpperCase();
  const maxDias = Number(process.argv[3] || 60);
  const tks = quien === 'AMBOS' ? ['SPX', 'QQQ'] : [quien];
  const bot = cargarBot();
  const ops = [];
  for (const tk of tks) {
    const dias = (await get('/historial/dias', { ticker: tk, exps: '0dte' })).slice(0, maxDias).reverse();
    process.stderr.write(`${tk}: ${dias.length} días\n`);
    for (const d of dias) {
      let raw; try { raw = await dia(tk, d); } catch (e) { process.stderr.write(`  ${d} sin datos (${e.message})\n`); continue; }
      const snaps = raw.map(bot.QBOT._test.histSnap).filter(Boolean);
      const porMin = new Map(); for (const s of snaps) porMin.set(nyHM(s.t), s);
      const eng = bot.QOIEngine({ sc: TK[tk].sc, cfg: Object.assign({ mg: TK[tk].mg, skipRebMinus: TK[tk].skip }, process.env.QBOT_PROX ? { prox: Number(process.env.QBOT_PROX) } : {}, process.env.QBOT_F ? { F: true, FSL: Number(process.env.QBOT_F) } : {}, process.env.QBOT_MG ? { mg: TK[tk].mg * Number(process.env.QBOT_MG) } : {}) });
      let n = 0, Rdia = 0, abierta = null;
      for (const s of porMin.values()) {
        for (const ev of eng.onMinute(s)) {
          if (ev.type === 'entry') {
            const bloqueada = n >= cfg.maxOps || Rdia <= -cfg.maxPerdidaR;
            abierta = bloqueada ? null : ev.pos; if (!bloqueada) n++;
          } else if (ev.type === 'exit' && abierta === ev.pos) {
            const riesgo = Math.abs(ev.pos.e - ev.pos.st0); const R = ev.pts / riesgo; Rdia += R; abierta = null;
            ops.push({ tk, d, hora: nyHM(ev.pos.t || ev.t), regla: `${ev.pos.regla} ${ev.pos.leg || ''}`.trim(), lado: ev.pos.d > 0 ? 'compra' : 'venta', R, pts: ev.pts, why: ev.why });
          }
        }
      }
    }
  }
  // ---------- métricas ----------
  const res = (lista, nombre) => {
    if (!lista.length) return console.log(`\n${nombre}: sin operaciones`);
    const g = lista.filter((o) => o.R > 0), p = lista.filter((o) => o.R <= 0);
    const tot = lista.reduce((a, o) => a + o.R, 0);
    const sg = g.reduce((a, o) => a + o.R, 0), sp = -p.reduce((a, o) => a + o.R, 0);
    let pico = 0, acc = 0, dd = 0; for (const o of lista) { acc += o.R; pico = Math.max(pico, acc); dd = Math.min(dd, acc - pico); }
    const dias = [...new Set(lista.map((o) => o.d))]; const porDia = dias.map((d) => lista.filter((o) => o.d === d).reduce((a, o) => a + o.R, 0));
    const usd = (r) => `${r >= 0 ? '+' : '-'}$${Math.abs(r * cfg.riesgoUSD).toFixed(0)}`;
    console.log(`\n${nombre}`);
    console.log(`  operaciones ${lista.length} en ${dias.length} días con operaciones · aciertos ${(100 * g.length / lista.length).toFixed(0)}%`);
    console.log(`  total ${tot >= 0 ? '+' : ''}${tot.toFixed(2)} R (${usd(tot)} con $${cfg.riesgoUSD} por R) · promedio ${(tot / lista.length).toFixed(2)} R por operación`);
    console.log(`  ganancia media +${g.length ? (sg / g.length).toFixed(2) : 0} R · pérdida media -${p.length ? (sp / p.length).toFixed(2) : 0} R · profit factor ${sp ? (sg / sp).toFixed(2) : '∞'}`);
    console.log(`  peor racha (drawdown) ${dd.toFixed(2)} R (${usd(dd)}) · días ganadores ${porDia.filter((x) => x > 0).length}/${dias.length} · mejor día ${Math.max(...porDia).toFixed(2)} R · peor día ${Math.min(...porDia).toFixed(2)} R`);
  };
  res(ops, 'TOTAL');
  for (const tk of tks) res(ops.filter((o) => o.tk === tk), tk === 'SPX' ? 'SPX (MES / US500)' : 'QQQ (MNQ / NAS100)');
  const reglas = [...new Set(ops.map((o) => o.regla))];
  console.log('\nPor regla:');
  for (const r of reglas) { const l = ops.filter((o) => o.regla === r); const t = l.reduce((a, o) => a + o.R, 0); console.log(`  ${r.padEnd(14)} ${String(l.length).padStart(3)} ops · ${(100 * l.filter((o) => o.R > 0).length / l.length).toFixed(0)}% aciertos · ${t >= 0 ? '+' : ''}${t.toFixed(2)} R`); }
  const f = path.join(DIR, 'registros', 'backtest_operaciones.csv');
  fs.writeFileSync(f, 'instrumento;dia;hora_NY;regla;lado;R;puntos;salida\n' + ops.map((o) => [o.tk, o.d, o.hora, o.regla, o.lado, o.R.toFixed(3), o.pts.toFixed(2), o.why].join(';')).join('\n'));
  console.log(`\nDetalle de cada operación: ${f}`);
})().catch((e) => { console.error('FALLÓ', e.message); process.exit(1); });
