// Bot Q OI: abre Quantium en un Chrome propio, carga el bot, manda las señales al webhook y guarda el registro.
// Uso:  node launcher.js            (sesión de trading normal; se cierra solo a las 16:10 NY)
//       node launcher.js --login    (solo abre Quantium para iniciar sesión la primera vez)
// El token de Quantium nunca sale del navegador: este programa no lo lee; solo recibe las señales ya calculadas.
'use strict';
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const DIR = __dirname;
const CFG_PATH = path.join(DIR, 'config.json');
const PROFILE = path.join(DIR, 'perfil-chrome');
const LOGS = path.join(DIR, 'registros');
const URL_APP = 'https://quantium.global/app/#/';
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
  const cuentas = (c.cuentas || []).filter((a) => a.activa && a.webhook);
  if (c.modo !== 'demo' || !cuentas.length) return { ok: true, papel: true };
  const out = [];
  for (const a of cuentas) {
    const b = JSON.parse(JSON.stringify(body)); const r = root(b.ticker);
    if ((b.action === 'buy' || b.action === 'sell') && b.stopLoss && r) {
      const q = Math.floor(a.riesgoUSD / (b.stopLoss.amount * PV[r])); const max = (a.maxQty || {})[r] ?? 10;
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

(async () => {
  const login = process.argv.includes('--login');
  const c = readCfg();
  const ctx = await chromium.launchPersistentContext(PROFILE, { headless: !login && c.verVentana === false, viewport: null, args: ['--start-maximized'] });
  const botSrc = fs.readFileSync(path.join(DIR, 'bot.js'), 'utf8');
  const pageCfg = { modo: c.modo, webhook: (c.cuentas || []).some((a) => a.activa && a.webhook) ? '(en el programa)' : '', riesgoUSD: c.riesgoUSD, maxPerdidaR: c.maxPerdidaR, maxOps: c.maxOps,
    SPX: Object.assign({ pv: 5 }, c.SPX), QQQ: Object.assign({ pv: 2, ratio: c.ratioMNQ || null }, c.QQQ) };

  await ctx.exposeBinding('qbotPost', async (_src, body) => {
    try { const r = await postSignal(body); return r; } catch (e) { return { ok: false, status: e.message }; }
  });
  await ctx.exposeBinding('qbotLog', async (_src, row) => { writeLog(row); console.log(`[${row.hora} NY] ${row.tk} ${row.msg} ${row.extra || ''}`); });
  await ctx.exposeBinding('qbotNQ', async () => (c.ratioMNQ ? null : await getNQ()));
  if (!login) {
    await ctx.addInitScript(({ src, cfg }) => {
      if (location.host !== 'quantium.global' || !location.pathname.startsWith('/app')) return;
      window.__QBOT_RUNNER = { cfg };
      const go = () => { try { (0, eval)(src); } catch (e) { console.error('[QBOT] no cargó', e); } };
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', go); else go();
    }, { src: botSrc, cfg: pageCfg });
  }

  if (process.env.QBOT_TEST) { // solo para pruebas: página y API simuladas
    await ctx.route('https://quantium.global/**', (r) => r.fulfill({ contentType: 'text/html', body: '<!doctype html><html><body>stub</body></html>' }));
    await ctx.route('https://api-spike.quantium.global/**', (r) => r.fulfill({ status: 403, contentType: 'application/json', body: '{"detail":"stub"}' }));
  }
  const page = ctx.pages()[0] || await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error' && /QBOT/.test(m.text())) sys('error en la página: ' + m.text()); });
  await page.goto(URL_APP, { waitUntil: 'domcontentloaded' });

  if (login) {
    sys('Inicia sesión en Quantium en la ventana que se abrió. Cuando veas la app, cierra la ventana.');
    await new Promise((r) => ctx.on('close', r));
    return;
  }
  if (process.env.QBOT_TEST) {
    await page.waitForTimeout(12000);
    const r = await page.evaluate(async () => ({ cargado: !!window.QBOT, corriendo: window.QBOT && window.QBOT.running, envio: await window.qbotPost({ ticker: 'MESZ2026', action: 'sell', quantity: 3, stopLoss: { type: 'stop', amount: 16 } }), salida: await window.qbotPost({ ticker: 'MESZ2026', action: 'exit' }) }));
    console.log('PRUEBA', JSON.stringify(r)); await ctx.close(); process.exit(0);
  }
  sys(`bot iniciado en modo ${c.modo.toUpperCase()} · cuentas activas: ${(c.cuentas || []).filter((a) => a.activa && a.webhook).map((a) => a.nombre).join(', ') || 'ninguna (no se envía nada)'}`);

  // Vigilancia: si la página no tiene sesión, avisa; si el bot se cae, recarga; a la hora de cierre, termina.
  const end = hhmm(c.cerrarHoraNY || '16:10');
  const timer = setInterval(async () => {
    const n = nyNow(); const m = n.getHours() * 60 + n.getMinutes();
    if (m >= end) { sys('fin de la sesión: cerrando'); clearInterval(timer); await ctx.close(); process.exit(0); }
    try {
      const st = await page.evaluate(() => ({ sesion: !!localStorage.getItem('sb-euwdfoswzwsqrknnvjvj-auth-token'), bot: !!(window.QBOT && window.QBOT.running), url: location.href }));
      if (!st.sesion) sys('Quantium no tiene sesión abierta: corre "node launcher.js --login" e inicia sesión.');
      else if (!st.bot) { sys('el bot no está corriendo: recargando la página'); await page.goto(URL_APP, { waitUntil: 'domcontentloaded' }); }
    } catch (e) { sys('página no responde: recargando'); try { await page.goto(URL_APP, { waitUntil: 'domcontentloaded' }); } catch (x) {} }
  }, 90000);
  process.on('SIGINT', async () => { sys('detenido con Ctrl+C'); await ctx.close(); process.exit(0); });
})().catch((e) => { console.error(e); process.exit(1); });
