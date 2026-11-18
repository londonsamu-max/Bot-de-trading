// Registro de niveles (lo que anota el observador del bot) para estudiar qué hace el precio en cada tipo de barra.
//   node niveles.js historial   -> pasa los días guardados en registros/cache por el mismo observador y escribe
//                                  registros/niveles/historial.jsonl (se rehace completo cada vez)
//   node niveles.js resumen     -> junta el historial y lo que el bot registra cada día y muestra las tablas
//   node niveles.js resumen QQQ -> solo un instrumento
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const DIR = __dirname;
const NIV = path.join(DIR, 'registros', 'niveles');
const CACHE = path.join(DIR, 'registros', 'cache');
const TK = { SPX: { sc: 1, mg: 2.34e9 }, QQQ: { sc: 0.089, mg: 1.73e8 } };
fs.mkdirSync(NIV, { recursive: true });

function cargarBot() {
  const ctx = { console: { log() {}, warn() {}, error() {} }, fetch, URLSearchParams, URL, Blob, performance, Intl, Date, Math, JSON, Promise, Map, Set,
    setTimeout, clearTimeout, setInterval, clearInterval, localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, prompt: () => null, __QBOT_NO_UI: true };
  ctx.window = ctx; vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(DIR, 'bot.js'), 'utf8'), ctx, { filename: 'bot.js' });
  return ctx;
}
const hm = (t) => new Date(t.toLocaleString('en-US', { timeZone: 'America/New_York' })).toTimeString().slice(0, 5);

function historial() {
  const bot = cargarBot(); const out = [];
  const archivos = fs.existsSync(CACHE) ? fs.readdirSync(CACHE).filter((f) => /^(SPX|QQQ)_\d{4}-\d{2}-\d{2}\.json$/.test(f)).sort() : [];
  for (const f of archivos) {
    const tk = f.slice(0, 3); const dia = f.slice(4, 14);
    const S = [...new Map(JSON.parse(fs.readFileSync(path.join(CACHE, f), 'utf8')).map(bot.QBOT._test.histSnap).filter(Boolean).map((s) => [hm(s.t), s])).values()];
    const obs = bot.QOIObs({ sc: TK[tk].sc, mg: TK[tk].mg });
    for (const s of S) for (const r of obs.onMinute(s)) out.push(Object.assign({ dia, instrumento: tk, fuente: 'historial' }, r));
  }
  fs.writeFileSync(path.join(NIV, 'historial.jsonl'), out.map((r) => JSON.stringify(r)).join('\n') + '\n');
  const dias = new Set(out.map((r) => r.instrumento + r.dia));
  console.log(`historial: ${out.length} registros de ${dias.size} días (${archivos.length} archivos) → registros/niveles/historial.jsonl`);
}

function leerTodo() {
  const vistos = new Set(); const r = [];
  // primero lo registrado en vivo; el historial solo aporta días que el bot no registró
  const vivos = fs.readdirSync(NIV).filter((f) => /^niveles_.*\.jsonl$/.test(f)).sort();
  for (const f of vivos) for (const l of fs.readFileSync(path.join(NIV, f), 'utf8').split('\n')) if (l.trim()) { const x = JSON.parse(l); x.fuente = 'en vivo'; r.push(x); vistos.add(x.instrumento + x.dia); }
  const h = path.join(NIV, 'historial.jsonl');
  if (fs.existsSync(h)) for (const l of fs.readFileSync(h, 'utf8').split('\n')) if (l.trim()) { const x = JSON.parse(l); if (!vistos.has(x.instrumento + x.dia)) r.push(x); }
  return r;
}

function resumen(solo) {
  let r = leerTodo(); if (solo) r = r.filter((x) => x.instrumento === solo);
  const lleg = r.filter((x) => x.tipo === 'llegada'); const camb = r.filter((x) => x.tipo === 'cambio de flujo');
  const dias = new Set(r.map((x) => x.instrumento + ' ' + x.dia));
  console.log(`${lleg.length} llegadas a niveles y ${camb.length} cambios de flujo en barras parejas · ${dias.size} días (${r.filter((x) => x.fuente === 'en vivo').length} registros en vivo)`);
  const fila = (nombre, l) => {
    if (!l.length) return; const n = l.length; const rb = l.filter((x) => x.resultado === 'rebote').length; const ru = l.filter((x) => x.resultado === 'ruptura').length;
    console.log(`  ${nombre.padEnd(58)} ${String(n).padStart(4)} · rebota ${String(Math.round(100 * rb / n)).padStart(3)}% · rompe ${String(Math.round(100 * ru / n)).padStart(3)}% · sin dirección ${String(Math.round(100 * (n - rb - ru) / n)).padStart(3)}%${n < 10 ? '  (pocos casos)' : ''}`);
  };
  for (const tk of ['SPX', 'QQQ']) {
    const t = lleg.filter((x) => x.instrumento === tk); if (!t.length) continue;
    console.log(`\n===== ${tk}: llegadas a niveles (${t.length})`);
    for (const [sg, f] of [['gamma NEGATIVA', (x) => x.gamma < 0], ['gamma POSITIVA', (x) => x.gamma > 0]]) {
      console.log(sg);
      for (const m of ['dominan calls', 'parejos', 'dominan puts']) for (const ll of ['desde abajo', 'desde arriba']) fila(`${m}, llegando ${ll}`, t.filter((x) => f(x) && x.mezcla === m && x.llega === ll));
      fila('>> parejos y los dos altos', t.filter((x) => f(x) && x.mezcla === 'parejos' && x.ambosAltos));
      for (const fl of [...new Set(t.filter(f).map((x) => x.flujo))].sort()) fila(`flujo: ${fl}`, t.filter((x) => f(x) && x.flujo === fl));
    }
    console.log('por tipo de nivel');
    for (const tn of ['call wall', 'put wall', 'barra grande', 'barra mediana']) fila(tn, t.filter((x) => x.tipoNivel === tn));
  }
  if (camb.length) {
    console.log('\n===== Cambios de flujo en barras parejas y altas (movimiento de los 30 min siguientes, en pts escala SPX)');
    const grupos = {};
    for (const x of camb) { const k = `${x.instrumento} · ahora ${x.ahora}`; (grupos[k] = grupos[k] || []).push(x); }
    for (const [k, l] of Object.entries(grupos).sort()) {
      const fin = l.filter((x) => x.final30 != null);
      const sigue = fin.filter((x) => x.ultimos5 && Math.sign(x.final30) === Math.sign(x.ultimos5) && Math.abs(x.final30) >= 5).length;
      const vuelve = fin.filter((x) => x.ultimos5 && Math.sign(x.final30) === -Math.sign(x.ultimos5) && Math.abs(x.final30) >= 5).length;
      const mov = fin.length ? fin.reduce((a, x) => a + Math.max(x.subeMax, x.bajaMax), 0) / fin.length : 0;
      console.log(`  ${k.padEnd(58)} ${String(l.length).padStart(3)} · siguió la dirección previa ${sigue} · se devolvió ${vuelve} · movimiento máx medio ${mov.toFixed(1)} pts${l.length < 10 ? '  (pocos casos)' : ''}`);
    }
  }
}

const cmd = process.argv[2] || 'resumen';
if (cmd === 'historial') historial();
else if (cmd === 'resumen') resumen((process.argv[3] || '').toUpperCase() || null);
else console.log('uso: node niveles.js historial | resumen [SPX|QQQ]');
