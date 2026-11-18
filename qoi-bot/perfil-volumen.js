// Perfil de volumen por día a partir del volumen por minuto de SPY (para SPX) o QQQ, en la escala del índice.
//   node perfil-volumen.js descargar   -> baja el volumen por minuto de cada día de registros/cache a registros/cache/vol_<TK>_<día>.json
//   require('./perfil-volumen').perfil(minutos, desde, hasta) -> { poc, vah, val, hvn: [...], lvn: [...] }
// SPX no tiene volumen propio: se usa SPY y se convierte cada minuto con la relación SPX/SPY de ese minuto.
'use strict';
const fs = require('fs');
const path = require('path');

const DIR = __dirname; const CACHE = path.join(DIR, 'registros', 'cache');
const FUENTE = { SPX: 'SPY', QQQ: 'QQQ' };
const PASO = { SPX: 1, QQQ: 0.1 }; // tamaño de cada escalón del perfil, en puntos del índice

async function descargar() {
  const sesion = require('./sesion'); const { readCfg } = require('./comun');
  if (readCfg().sesionDesdeChrome) sesion.usarChrome(true);
  await sesion.asegurar();
  const dias = fs.readdirSync(CACHE).filter((f) => /^(SPX|QQQ)_\d{4}-\d{2}-\d{2}\.json$/.test(f));
  let n = 0;
  for (const f of dias) {
    const tk = f.slice(0, 3); const d = f.slice(4, 14); const out = path.join(CACHE, `vol2_${tk}_${d}.json`);
    if (fs.existsSync(out)) continue;
    const r = await fetch(`https://api-spike.quantium.global/uw/ohlc/1m?ticker=${FUENTE[tk]}&date=${d}`, { headers: { Authorization: 'Bearer ' + sesion.accessToken() } });
    if (!r.ok) { console.log(d, tk, 'sin volumen', r.status); continue; }
    const j = await r.json();
    // se guardan todas las sesiones: s = 'pr' (premarket), 'r' (regular, RTH) o 'po' (after-hours)
    const filas = (j.data || []).map((x) => ({ t: Date.parse(x.start_time), o: +x.open, h: +x.high, l: +x.low, c: +x.close, v: +x.volume, s: x.market_time }));
    fs.writeFileSync(out, JSON.stringify(filas)); n++;
  }
  console.log(`volumen descargado de ${n} días nuevos`);
}

// minutos: [{t, h, l, c, v, k}] con k = índice / fuente (para pasar a la escala del índice). Reparte el volumen del minuto en su rango.
function perfil(minutos, tk, desde = -Infinity, hasta = Infinity) {
  const paso = PASO[tk] || 1; const hist = new Map(); let total = 0;
  for (const m of minutos) {
    if (m.t < desde || m.t >= hasta || !(m.v > 0) || !(m.k > 0)) continue;
    const lo = Math.floor((m.l * m.k) / paso); const hi = Math.floor((m.h * m.k) / paso); const nb = hi - lo + 1;
    for (let b = lo; b <= hi; b++) hist.set(b, (hist.get(b) || 0) + m.v / nb);
    total += m.v;
  }
  if (!hist.size) return null;
  const bins = [...hist.entries()].sort((a, b) => a[0] - b[0]);
  let poc = bins[0]; for (const b of bins) if (b[1] > poc[1]) poc = b;
  // área de valor: 70 % del volumen alrededor del POC
  let i = bins.indexOf(poc), j = i, acc = poc[1];
  while (acc < 0.7 * total && (i > 0 || j < bins.length - 1)) {
    const up = j < bins.length - 1 ? bins[j + 1][1] : -1; const dn = i > 0 ? bins[i - 1][1] : -1;
    if (up >= dn) { j++; acc += up; } else { i--; acc += dn; }
  }
  // nodos: máximos y mínimos locales (suavizado de 5 escalones), los de alto volumen sobre la media y los de bajo bajo la mitad
  const vs = bins.map((b) => b[1]); const sm = vs.map((_, k) => { let s = 0, c = 0; for (let q = k - 2; q <= k + 2; q++) if (q >= 0 && q < vs.length) { s += vs[q]; c++; } return s / c; });
  const media = total / bins.length; const hvn = [], lvn = [];
  for (let k = 2; k < sm.length - 2; k++) {
    if (sm[k] >= sm[k - 1] && sm[k] >= sm[k + 1] && sm[k] >= sm[k - 2] && sm[k] >= sm[k + 2] && sm[k] > media) hvn.push((bins[k][0] + 0.5) * paso);
    if (sm[k] <= sm[k - 1] && sm[k] <= sm[k + 1] && sm[k] <= sm[k - 2] && sm[k] <= sm[k + 2] && sm[k] < media * 0.5) lvn.push((bins[k][0] + 0.5) * paso);
  }
  return { poc: (poc[0] + 0.5) * paso, vah: (bins[j][0] + 1) * paso, val: bins[i][0] * paso, hvn, lvn };
}

// Une el volumen de la fuente (SPY/QQQ) con el precio del índice de ese minuto (snapshots de Quantium) para tener la escala k
function unir(vol, snaps) {
  const px = new Map(snaps.map((s) => [Math.floor(s.t.getTime() / 60000), s.spot]));
  let kAnt = null;
  return vol.map((m) => { const s = px.get(Math.floor(m.t / 60000)); if (s && m.c > 0) kAnt = s / m.c; return Object.assign({}, m, { k: kAnt }); }).filter((m) => m.k);
}

module.exports = { perfil, unir, descargar, FUENTE };
if (require.main === module && process.argv[2] === 'descargar') descargar().catch((e) => { console.error('FALLÓ', e.message); process.exit(1); });
