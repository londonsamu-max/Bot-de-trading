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

const { readCfg, nyNow, hhmm, sys, writeLog, postSignal, getNQ } = require('./comun');

(async () => {
  const login = process.argv.includes('--login');
  const c = readCfg();
  const ctx = await chromium.launchPersistentContext(PROFILE, { headless: !login && c.verVentana === false, viewport: null, args: ['--start-maximized'] });
  const botSrc = fs.readFileSync(path.join(DIR, 'bot.js'), 'utf8');
  const pageCfg = { modo: c.modo, pollMs: c.pollMs, motor: c.motor, webhook: (c.cuentas || []).some((a) => a.activa && a.webhook) ? '(en el programa)' : '', riesgoUSD: c.riesgoUSD, maxPerdidaR: c.maxPerdidaR, maxOps: c.maxOps,
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
  sys(`bot iniciado en modo ${c.modo.toUpperCase()} · cuentas activas: ${(c.cuentas || []).filter((a) => a.activa && (a.webhook || a.tipo === 'mt5')).map((a) => a.nombre).join(', ') || 'ninguna (no se envía nada)'}`);

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
