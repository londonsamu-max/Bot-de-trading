// Conector a MetaTrader 5 por el puente de archivos del EA SpotGammaBridge (carpeta MQL5/Files).
// Convierte las señales del bot (formato TradersPost: MES/MNQ en contratos) en órdenes de CFD:
//   MES -> US500, MNQ -> NAS100. Tamaño en lotes = riesgoUSD / (stop en puntos × USD por punto de 1 lote).
// Seguridad:
//   - Solo opera si el EA reporta una cuenta DEMO (o el número de cuenta que pongas en "cuentaPermitida").
//   - Solo cierra las posiciones que abrió el propio qoi-bot (guarda sus tickets en registros/mt5_tickets.json),
//     nunca las del agente de SpotGamma u otras manuales, aunque usen el mismo EA.
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const FILES = process.env.QBOT_MT5_FILES || path.join(os.homedir(),
  'Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/Program Files/MetaTrader 5/MQL5/Files');
const ORDER = path.join(FILES, 'sg_order.json');
const RESULT = path.join(FILES, 'sg_result.json');
const STATUS = path.join(FILES, 'sg_status.json');
const TICKETS = path.join(__dirname, 'registros', 'mt5_tickets.json');

const MAPA = { MES: 'US500', ES: 'US500', MNQ: 'NAS100', NQ: 'NAS100' };
const raiz = (t) => (String(t).match(/^(MES|MNQ|ES|NQ)/) || [])[1];

const leerJson = (f) => { try { return JSON.parse(fs.readFileSync(f, 'utf8')); } catch (e) { return null; } };
const borrar = (f) => { try { fs.unlinkSync(f); } catch (e) {} };
const esperar = (ms) => new Promise((r) => setTimeout(r, ms));

function estado() {
  const s = leerJson(STATUS);
  if (!s) return { ok: false, error: 'el EA no ha escrito su estado: abre MT5 y pon SpotGammaBridge en un gráfico' };
  // El EA escribe la hora del servidor del bróker (NY+7), no la del Mac: se mide la edad por la fecha del archivo.
  let edad = Infinity; try { edad = (Date.now() - fs.statSync(STATUS).mtimeMs) / 1000; } catch (e) {}
  if (!(edad < 30)) return { ok: false, error: `el EA no responde hace ${Math.round(edad)} s (¿MT5 abierto y con AutoTrading?)`, s };
  return { ok: true, s };
}
function cuentaSegura(s, cfg) {
  if (cfg.cuentaPermitida) return Number(s.account) === Number(cfg.cuentaPermitida);
  return String(s.mode).toLowerCase() === 'demo' && !/live|real/i.test(String(s.server));
}

let cola = Promise.resolve(); // una orden a la vez: el puente tiene un solo archivo de órdenes
function enviar(orden) {
  const p = cola.then(async () => {
    for (let i = 0; i < 50 && fs.existsSync(ORDER); i++) await esperar(100); // otra orden pendiente (p. ej. del agente)
    const requestId = crypto.randomBytes(4).toString('hex');
    borrar(RESULT);
    fs.writeFileSync(ORDER + '.tmp', JSON.stringify(Object.assign({}, orden, { requestId })));
    fs.renameSync(ORDER + '.tmp', ORDER);
    const t0 = Date.now();
    while (Date.now() - t0 < 10000) {
      await esperar(50);
      const r = leerJson(RESULT);
      if (r && r.requestId === requestId) { borrar(RESULT); return Object.assign(r, { ms: Date.now() - t0 }); }
    }
    borrar(ORDER);
    return { success: false, error: 'el EA no respondió en 10 s' };
  });
  cola = p.catch(() => {});
  return p;
}

const leerTickets = () => leerJson(TICKETS) || {};
function guardarTickets(t) { fs.mkdirSync(path.dirname(TICKETS), { recursive: true }); fs.writeFileSync(TICKETS, JSON.stringify(t, null, 1)); }

// Procesa una señal para una cuenta { nombre, tipo:'mt5', riesgoUSD, maxLotes, usdPorPuntoLote, cuentaPermitida }
async function senal(body, cuenta) {
  const r = raiz(body.ticker); const cfd = MAPA[r];
  if (!cfd) return `${cuenta.nombre}: ticker ${body.ticker} sin equivalente en MT5`;
  const e = estado(); if (!e.ok) return `${cuenta.nombre}: FALLÓ ${e.error}`;
  if (!cuentaSegura(e.s, cuenta)) return `${cuenta.nombre}: FALLÓ la cuenta de MT5 (${e.s.server}) no es demo; no se opera`;

  if (body.action === 'buy' || body.action === 'sell') {
    const px = (e.s.prices || {})[cfd === 'US500' ? 'SPX500' : cfd];
    if (!px || !body.stopLoss) return `${cuenta.nombre}: FALLÓ sin precio de ${cfd} o sin stop`;
    const largo = body.action === 'buy'; const entrada = largo ? px.ask : px.bid; const stopPts = body.stopLoss.amount;
    const usdPt = (cuenta.usdPorPuntoLote || {})[cfd] ?? 1;
    const maxL = (cuenta.maxLotes || {})[cfd] ?? 1;
    const lotes = Math.min(maxL, Math.floor((cuenta.riesgoUSD / (stopPts * usdPt)) * 100) / 100);
    if (lotes < 0.1) return `${cuenta.nombre}: stop de ${stopPts} pts muy amplio para $${cuenta.riesgoUSD}, no se envía`;
    const sl = largo ? entrada - stopPts : entrada + stopPts;
    const tp = body.takeProfit ? (largo ? entrada + body.takeProfit.amount : entrada - body.takeProfit.amount) : 0;
    const res = await enviar({ action: 'place_order', cfd, direction: largo ? 'LONG' : 'SHORT', volume: lotes, sl, tp1: tp, tp2: 0, tp3: 0 });
    if (!res.success) return `${cuenta.nombre}: FALLÓ ${res.error || res.retcode}`;
    const t = leerTickets(); (t[cfd] = t[cfd] || []).push(res.ticket); guardarTickets(t);
    const real = res.volume != null ? Number(res.volume) : lotes; // el EA puede recortar al máximo de lote que tenga configurado
    return `${cuenta.nombre}: OK ${largo ? 'compra' : 'venta'} ${real} lotes ${cfd}${real < lotes ? ` (pedidos ${lotes}: el EA los recortó)` : ''} @ ${res.price} · stop ${sl.toFixed(1)} · ticket ${res.ticket} · ${res.ms} ms`;
  }
  if (body.action === 'exit') {
    // Se intenta cerrar cada ticket del bot sin mirar el estado (el EA lo actualiza cada pocos segundos y
    // una posición recién abierta podría no aparecer aún). "Posicion no encontrada" = ya la cerró el stop u objetivo.
    const t = leerTickets(); const mios = t[cfd] || [];
    const out = []; const quedan = [];
    for (const k of mios) {
      const res = await enviar({ action: 'close_position', ticket: k, volume: 0 });
      if (res.success) out.push(`${k} cerrada (${res.ms} ms)`);
      else if (/no encontrada/i.test(res.error || '')) out.push(`${k} ya estaba cerrada`);
      else { out.push(`${k} FALLÓ ${res.error || res.retcode}`); quedan.push(k); }
    }
    t[cfd] = quedan; guardarTickets(t);
    return `${cuenta.nombre}: ${out.length ? out.join(', ') : `sin posiciones del bot en ${cfd}`}`;
  }
  return `${cuenta.nombre}: ${body.action} no aplica en MT5 (órdenes a mercado)`;
}

module.exports = { senal, estado, cuentaSegura, FILES };
