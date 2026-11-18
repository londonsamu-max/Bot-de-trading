// Rasgos de volumen en vivo para ajustar el riesgo de una entrada (perfil de volumen diario de SPY/QQQ, de Quantium):
//   lejosNodos: el nivel de gamma está a ≥ 5 pts SPX (escalado en QQQ) de los nodos del perfil de ayer y de hoy
//   dentroValor: el precio de entrada está dentro del área de valor (70 %) de la sesión regular de ayer
// En el backtest (42 días de SPX): lejos de nodos +1.51 R/op contra +0.46 R/op sobre un nodo; dentro del valor de ayer, peor.
'use strict';
const fs = require('fs');
const path = require('path');
const sesion = require('./sesion');
const { perfil } = require('./perfil-volumen');

const API = 'https://api-spike.quantium.global';
const FUENTE = { SPX: 'SPY', QQQ: 'QQQ' };
const ESCALA = { SPX: 1, QQQ: 0.089 }; // para pasar los 5 pts de SPX a QQQ
const CACHE = path.join(__dirname, 'registros', 'cache', 'vivo');
const memo = new Map();

const nyFecha = (d) => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(d);
async function minutos(fuente, fecha, cacheable) {
  const f = path.join(CACHE, `${fuente}_${fecha}.json`);
  if (cacheable && fs.existsSync(f)) return JSON.parse(fs.readFileSync(f, 'utf8'));
  const r = await fetch(`${API}/uw/ohlc/1m?ticker=${fuente}&date=${fecha}`, { headers: { Authorization: 'Bearer ' + sesion.accessToken() } });
  if (!r.ok) throw new Error(`volumen ${fuente} ${fecha}: ${r.status}`);
  const filas = ((await r.json()).data || []).filter((x) => x.market_time === 'r')
    .map((x) => ({ t: Date.parse(x.start_time), h: +x.high, l: +x.low, c: +x.close, v: +x.volume })).sort((a, b) => a.t - b.t);
  if (cacheable && filas.length >= 300) { fs.mkdirSync(CACHE, { recursive: true }); fs.writeFileSync(f, JSON.stringify(filas)); }
  return filas;
}
// sesión regular completa del último día hábil con datos antes de hoy
async function ayer(fuente) {
  const hoy = nyFecha(new Date()); const k = `${fuente}_${hoy}`;
  if (memo.has(k)) return memo.get(k);
  let d = new Date(Date.now() - 86400000);
  for (let i = 0; i < 7; i++, d = new Date(d.getTime() - 86400000)) {
    const f = nyFecha(d); const w = new Date(f + 'T12:00:00Z').getUTCDay(); if (w === 0 || w === 6) continue;
    const m = await minutos(fuente, f, true); if (m.length >= 300) { memo.set(k, m); return m; }
  }
  throw new Error('sin volumen del día anterior');
}

// el volumen de hoy se precarga cada minuto (precargar) para que la entrada no espere la descarga
const hoyCache = new Map();
async function deHoy(fuente, maxEdadMs) {
  const c = hoyCache.get(fuente);
  if (c && Date.now() - c.at < maxEdadMs) return c.m;
  const m = await minutos(fuente, nyFecha(new Date()), false); hoyCache.set(fuente, { at: Date.now(), m }); return m;
}
async function precargar() { for (const f of Object.values(FUENTE)) { try { await ayer(f); await deHoy(f, 0); } catch (e) {} } }

async function rasgos(tk, nivel, precio) {
  const fuente = FUENTE[tk]; if (!fuente) return null;
  const [mAyer, mHoy] = await Promise.all([ayer(fuente), deHoy(fuente, 90000)]);
  if (!mHoy.length) return null;
  const k = precio / mHoy[mHoy.length - 1].c; // escala índice/fuente (SPX/SPY ≈ 10; QQQ = 1)
  const conK = (ms) => ms.map((m) => Object.assign({}, m, { k }));
  const pa = perfil(conK(mAyer), tk); const ph = perfil(conK(mHoy), tk);
  const nodos = (p) => (p ? [p.poc, p.vah, p.val, ...p.hvn] : []);
  const todos = [...nodos(pa), ...nodos(ph)]; if (!todos.length || !pa) return null;
  const dist = Math.min(...todos.map((v) => Math.abs(v - nivel))) / ESCALA[tk]; // en pts SPX
  return { distNodo: dist, lejosNodos: dist >= 5, dentroValor: precio <= pa.vah && precio >= pa.val, poc: pa.poc, vah: pa.vah, val: pa.val };
}

module.exports = { rasgos, precargar };
