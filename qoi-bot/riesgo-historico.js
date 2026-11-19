// Factor de riesgo que el bot habría usado en cada operación simulada, con las mismas reglas que en vivo (bot.js sendEntry):
//   en contra del régimen ×riesgoContra · nivel lejos de nodos de volumen ×lejosNodos · dentro del valor de ayer ×dentroValor ·
//   ruptura en número redondo ×redondos · tope riesgoMax. El perfil sale del volumen por minuto guardado en registros/cache (vol2_).
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { perfil } = require('./perfil-volumen');

const DIR = __dirname; const CACHE = path.join(DIR, 'registros', 'cache');
const ESCALA = { SPX: 1, QQQ: 0.089 };
const hmNY = (ms) => new Date(new Date(ms).toLocaleString('en-US', { timeZone: 'America/New_York' })).toTimeString().slice(0, 5);

let bot = null;
function histSnap() {
  if (!bot) {
    const ctx = { console: { log() {}, warn() {}, error() {} }, fetch, URLSearchParams, URL, Blob, performance, Intl, Date, Math, JSON, Promise, Map, Set, setTimeout, clearTimeout, setInterval, clearInterval, localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, prompt: () => null, __QBOT_NO_UI: true };
    ctx.window = ctx; vm.createContext(ctx); vm.runInContext(fs.readFileSync(path.join(DIR, 'bot.js'), 'utf8'), ctx); bot = ctx;
  }
  return bot.QBOT._test.histSnap;
}
// minutos de la sesión regular de un día, con la escala k = índice/fuente (mediana del día)
const memo = new Map();
function minutosDia(tk, d) {
  const key = tk + d; if (memo.has(key)) return memo.get(key);
  let out = null;
  const vf = path.join(CACHE, `vol2_${tk}_${d}.json`); const sf = path.join(CACHE, `${tk}_${d}.json`);
  if (fs.existsSync(vf) && fs.existsSync(sf)) {
    const snaps = JSON.parse(fs.readFileSync(sf, 'utf8')).map(histSnap()).filter(Boolean);
    const px = new Map(snaps.map((s) => [Math.floor(s.t.getTime() / 60000), s.spot]));
    const vol = JSON.parse(fs.readFileSync(vf, 'utf8')).filter((m) => m.s === 'r').sort((a, b) => a.t - b.t);
    const ks = vol.filter((m) => px.has(Math.floor(m.t / 60000))).map((m) => px.get(Math.floor(m.t / 60000)) / m.c).sort((a, b) => a - b);
    if (ks.length) { const k = ks[ks.length >> 1]; out = vol.map((m) => Object.assign({}, m, { k, hm: hmNY(m.t) })); }
  }
  memo.set(key, out); return out;
}
const snapsMemo = new Map();
function memoSnaps(tk, d) { const k = tk + d; if (!snapsMemo.has(k)) snapsMemo.set(k, JSON.parse(fs.readFileSync(path.join(CACHE, `${tk}_${d}.json`), 'utf8')).map(histSnap()).filter(Boolean)); return snapsMemo.get(k); }
function diasDe(tk) { return fs.readdirSync(CACHE).filter((f) => new RegExp(`^${tk}_\\d{4}-\\d{2}-\\d{2}\\.json$`).test(f)).map((f) => f.slice(tk.length + 1, -5)).sort(); }

// o: operación del backtest ({tk, d, hora, lado, e, k, regla, regimen, motor}) · M: config.motor
function factor(o, M) {
  const motivos = []; let f = 1;
  const favor = o.regimen ? ((o.lado === 'compra') === (o.regimen === '+')) : true;
  if (!favor && M.riesgoContra) { f *= M.riesgoContra; motivos.push('contra régimen'); }
  if (M.volumen) {
    const dias = diasDe(o.tk); const i = dias.indexOf(o.d); const hoy = minutosDia(o.tk, o.d); const ayer = i > 0 ? minutosDia(o.tk, dias[i - 1]) : null;
    if (hoy && ayer) {
      const pa = perfil(ayer, o.tk); const ph = perfil(hoy.filter((m) => m.hm < o.hora), o.tk);
      const nodos = (p) => (p ? [p.poc, p.vah, p.val, ...p.hvn] : []); const todos = [...nodos(pa), ...nodos(ph)];
      if (pa && todos.length) {
        const dist = Math.min(...todos.map((v) => Math.abs(v - o.k))) / ESCALA[o.tk];
        if (dist >= 5 && M.lejosNodos && M.lejosNodos !== 1) { f *= M.lejosNodos; motivos.push('lejos de nodos de volumen'); }
        if (o.e <= pa.vah && o.e >= pa.val && M.dentroValor) { f *= M.dentroValor; motivos.push('dentro del valor de ayer'); }
      }
    } else motivos.push('sin datos de volumen');
  }
  if (M.contraDia) { // movimiento del día al entrar: precio de entrada vs primer minuto de la sesión (datos de Quantium)
    const sf = path.join(CACHE, `${o.tk}_${o.d}.json`);
    if (fs.existsSync(sf)) { const S = memoSnaps(o.tk, o.d); const abre = S.length ? S[0].spot : null; const ent = S.filter((x) => hmNY(x.t.getTime()) < o.hora).pop();
      const mov = abre && ent ? (ent.spot - abre) * (o.lado === 'compra' ? 1 : -1) / ESCALA[o.tk] : 0;
      if (mov < -(M.contraDiaPts || 10)) { f *= M.contraDia; motivos.push('contra el día'); } }
  }
  const ruptura = /ruptura/.test(o.regla); const redondo = o.tk === 'SPX' ? o.k % 50 === 0 : o.k % 5 === 0;
  if (M.redondos && ruptura && redondo) { f *= M.redondos; motivos.push('ruptura en número redondo'); }
  if (/X/.test(o.regla) && M.XRiesgo) { f *= M.XRiesgo; motivos.push('regla X'); }
  if (M.riesgoMax && f > M.riesgoMax) { f = M.riesgoMax; motivos.push('tope'); }
  return { factor: Math.round(f * 1000) / 1000, motivos };
}

module.exports = { factor };
