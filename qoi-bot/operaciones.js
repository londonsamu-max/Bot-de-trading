// Junta todas las operaciones: las REALES del bot en MT5 (registro del bot + registro de MT5) y las SIMULADAS del
// backtest (lo que el bot habría hecho cada día con la configuración actual). Escribe registros/operaciones.json.
//   node operaciones.js      (antes: node backtest.js ambos 1000 con QBOT_SOLO_CERRADOS=1 para las simuladas)
'use strict';
const fs = require('fs');
const path = require('path');
const { readCfg } = require('./comun');
const mt5 = require('./mt5');

const DIR = __dirname; const REG = path.join(DIR, 'registros');
const MT5LOGS = path.join(path.dirname(path.dirname(mt5.FILES)), 'logs');
const cfg = readCfg(); const cta = (cfg.cuentas || []).find((a) => a.tipo === 'mt5') || { riesgoUSD: 250, maxLotes: {} };

function utf16(f) { try { const b = fs.readFileSync(f); return b[0] === 0xff && b[1] === 0xfe ? b.toString('utf16le') : b.toString('utf8'); } catch (e) { return ''; } }

// ---------- reales ----------
function reales() {
  const out = []; const abiertas = new Map(((mt5.estado().s || {}).openPositions || []).map((p) => [Number(p.ticket), p]));
  for (const f of fs.readdirSync(REG).filter((f) => /^qbot_\d{4}-\d{2}-\d{2}\.csv$/.test(f)).sort()) {
    const dia = f.slice(5, 15); const log = utf16(path.join(MT5LOGS, dia.replace(/-/g, '') + '.log')).split('\n').filter((l) => l.includes('Trades'));
    for (const l of fs.readFileSync(path.join(REG, f), 'utf8').split('\n')) {
      const [hora, inst, evento, resp] = l.split(';'); if (!resp) continue;
      const m = resp.match(/OK (compra|venta) ([\d.]+) lotes (US500|NAS100)[^@]*@ ([\d.]+) · stop ([\d.]+) · ticket (\d+)/); if (!m) continue;
      const [, lado, lotes, cfd, entrada, stop, ticket] = m; const t = Number(ticket);
      const em = evento.match(/^(?:\[papel\] )?(BUY|SELL) \S+ \S+ · stop [\d.]+ pts(?: · objetivo ([\d.]+))? · (.+?) · nivel (\d+)/) || [];
      const motor = (inst.split('·')[1] || 'principal'); const r = { tipo: 'real', dia, hora, instrumento: inst.split('·')[0], cfd, motor, regla: em[3] || '', nivel: Number(em[4]) || null,
        lado, lotes: Number(lotes), entrada: Number(entrada), stop: Number(stop), ticket: t };
      // cierre: por el bot ("close #ticket") o por stop/objetivo en MT5 (primer trato contrario del mismo tamaño y símbolo)
      let cierre = null; const idx = log.findIndex((x) => x.includes(`based on order #${t})`)); const opuesto = lado === 'compra' ? 'sell' : 'buy';
      const ci = log.findIndex((x) => x.includes(`close #${t} `));
      if (ci >= 0) { const d = log.slice(ci).find((x) => /deal #\d+ (buy|sell) /.test(x)); if (d) cierre = d; }
      if (!cierre && idx >= 0) cierre = log.slice(idx + 1).find((x) => new RegExp(`deal #\\d+ ${opuesto} ${lotes} ${cfd} at`).test(x) && !/close #/.test(x));
      if (abiertas.has(t)) { const p = abiertas.get(t); Object.assign(r, { estado: 'abierta', salida: p.currentPrice, stopActual: p.sl, usd: p.profit }); }
      else if (cierre) { const cm = cierre.match(/(\d\d:\d\d):\d\d\.\d+\s+Trades.* at ([\d.]+)/); const sal = Number(cm[2]);
        Object.assign(r, { estado: 'cerrada', horaSalida: cm[1], salida: sal, usd: (sal - r.entrada) * (lado === 'compra' ? 1 : -1) * r.lotes }); }
      else r.estado = 'sin datos de cierre';
      r.R = r.usd != null ? r.usd / (Math.abs(r.entrada - r.stop) * r.lotes) : null;
      out.push(r);
    }
  }
  return out;
}

// ---------- simuladas ----------
function simuladas() {
  const f = path.join(REG, 'backtest_operaciones.json'); if (!fs.existsSync(f)) return [];
  const ratio = Number(((fs.readFileSync(path.join(REG, 'consola.log'), 'utf8').match(/ratio MNQ\/QQQ automático = ([\d.]+)/g) || []).pop() || '= 41').split('= ')[1]);
  return JSON.parse(fs.readFileSync(f, 'utf8')).map((o) => {
    const k = o.tk === 'SPX' ? 1 : ratio; const cfd = o.tk === 'SPX' ? 'US500' : 'NAS100';
    const favor = o.regimen ? ((o.lado === 'compra') === (o.regimen === '+')) : true; const factor = favor ? 1 : ((cfg.motor || {}).riesgoContra || 1);
    const stopC = Math.abs(o.e - o.st0) * k; const lotes = Math.min((cta.maxLotes || {})[cfd] || 50, Math.floor(cta.riesgoUSD * factor / stopC * 100) / 100);
    return { tipo: 'simulada', dia: o.d, hora: o.hora, horaSalida: o.horaSal, instrumento: o.tk, cfd, motor: o.motor, regla: o.regla, nivel: o.k, tipoNivel: o.tipo,
      lado: o.lado, lotes, entrada: o.e, stop: o.st0, salida: o.px, puntos: o.pts, R: o.R, usd: o.pts * k * lotes, salidaPor: o.why, estado: 'cerrada', regimen: favor ? 'a favor' : 'en contra' };
  });
}

const data = { generado: new Date().toISOString(), riesgoUSD: cta.riesgoUSD, real: reales(), simulado: simuladas() };
fs.writeFileSync(path.join(REG, 'operaciones.json'), JSON.stringify(data));
console.log(`reales: ${data.real.length} · simuladas: ${data.simulado.length} → registros/operaciones.json`);
for (const r of data.real) console.log(' ', r.dia, r.hora, r.cfd, r.motor, r.regla, r.lado, r.lotes, '@', r.entrada, '→', r.salida, r.estado, r.usd != null ? r.usd.toFixed(2) : '');
