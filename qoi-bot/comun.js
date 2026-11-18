// Funciones compartidas por launcher.js (con Chrome) y runner.js (sin navegador):
// configuración, registro CSV, envío de señales a TradersPost y precio de NQ.
'use strict';
const fs = require('fs');
const path = require('path');

const DIR = __dirname;
const CFG_PATH = path.join(DIR, 'config.json');
const LOGS = path.join(DIR, 'registros');
fs.mkdirSync(LOGS, { recursive: true });

const readCfg = () => JSON.parse(fs.readFileSync(CFG_PATH, 'utf8'));
const nyNow = () => new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
const nyDate = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date());
const hhmm = (s) => { const [h, m] = s.split(':').map(Number); return h * 60 + m; };
const logFile = () => path.join(LOGS, `qbot_${nyDate()}.csv`);
function writeLog(row) {
  const f = logFile(); const head = !fs.existsSync(f);
  const clean = (v) => String(v ?? '').replace(/[\r\n;]/g, ' ');
  fs.appendFileSync(f, (head ? 'hora_NY;instrumento;evento;respuesta\n' : '') + [row.hora, row.tk, row.msg, row.extra].map(clean).join(';') + '\n');
}
function sys(msg) { const t = nyNow().toTimeString().slice(0, 5); console.log(`[${t} NY] ${msg}`); writeLog({ hora: t, tk: 'SISTEMA', msg, extra: '' }); }

// Señal hacia TradersPost. Aquí, y solo aquí, se decide si se envía: "modo": "demo" en config.json.
// Cada cuenta (bróker de futuros o empresa de fondeo) es una estrategia de TradersPost con su propio webhook.
// El tamaño se recalcula por cuenta con su propio riesgo: contratos = riesgoUSD / (stop en puntos × valor del punto).
const PV = { MES: 5, MNQ: 2, ES: 50, NQ: 20 };
const root = (t) => (t.match(/^(MES|MNQ|ES|NQ)/) || [])[1];
async function postSignal(body) {
  const c = readCfg(); // se relee cada vez: cambiar config.json a "papel" detiene los envíos al instante
  const cuentas = (c.cuentas || []).filter((a) => a.activa && (a.webhook || a.tipo === 'mt5'));
  if (process.env.QBOT_TEST || c.modo !== 'demo' || !cuentas.length) return { ok: true, papel: true }; // las pruebas nunca operan
  const out = [];
  for (const a of cuentas) {
    if (a.tipo === 'mt5') { // MetaTrader 5 por el puente de archivos del EA (ver mt5.js)
      if (body.action === 'cancel') continue; // en MT5 las órdenes son a mercado: no hay pendientes que cancelar
      try { out.push(await require('./mt5').senal(body, a)); } catch (e) { out.push(`${a.nombre}: FALLÓ ${e.message}`); }
      continue;
    }
    if (body.action === 'mover_stop') continue; // TradersPost no tiene esta acción: el stop se maneja con la salida
    if (body.grupo) continue; // el motor F aparte solo opera en MT5 (en TradersPost compartiría la misma estrategia)
    const b = JSON.parse(JSON.stringify(body)); const r = root(b.ticker);
    if ((b.action === 'buy' || b.action === 'sell') && b.stopLoss && r) {
      const q = Math.floor(a.riesgoUSD * (Number(b.riesgoFactor) || 1) / (b.stopLoss.amount * PV[r])); const max = (a.maxQty || {})[r] ?? 10;
      b.quantity = Math.min(q, max);
      if (b.quantity < 1) { out.push(`${a.nombre}: stop muy amplio para $${a.riesgoUSD}, no se envía`); continue; }
    }
    try {
      const res = await fetch(a.webhook, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
      out.push(`${a.nombre}: ${res.ok ? 'OK' : 'FALLÓ'} ${res.status}${b.quantity ? ' · ' + b.quantity + ' ctr' : ''}`);
      if (!res.ok) out[out.length - 1] += ' ' + (await res.text()).slice(0, 120);
    } catch (e) { out.push(`${a.nombre}: FALLÓ ${e.message}`); }
  }
  return { ok: !out.some((x) => /FALLÓ/.test(x)), status: out.join(' | ') };
}
// Precio de NQ para calcular el ratio MNQ/QQQ (Yahoo Finance; si falla, se usa ratioMNQ de config.json)
async function getNQ() {
  const c = readCfg();
  try {
    const r = await fetch('https://query1.finance.yahoo.com/v8/finance/chart/NQ=F?interval=1m&range=1d', { headers: { 'User-Agent': 'Mozilla/5.0' } });
    const j = await r.json(); const p = j.chart.result[0].meta.regularMarketPrice;
    if (p > 0) return p;
  } catch (e) { sys('precio de NQ no disponible: ' + e.message); }
  return null;
}

// Notificación de macOS (aparece en la esquina de la pantalla y en el centro de notificaciones)
function avisar(titulo, texto) {
  if (process.platform !== 'darwin') return;
  const q = (x) => JSON.stringify(String(x || '').replace(/[\r\n]/g, ' ').slice(0, 200));
  try { require('child_process').execFile('osascript', ['-e', `display notification ${q(texto)} with title "Bot Q OI" subtitle ${q(titulo)}`]); } catch (e) {}
}

module.exports = { avisar, DIR, readCfg, nyNow, nyDate, hhmm, writeLog, sys, postSignal, getNQ };
