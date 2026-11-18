// Abre TopstepX en un Chrome propio del bot (perfil separado, en este Mac) y lo deja abierto.
// El puerto de depuración escucha solo en este equipo (127.0.0.1) para que el conector se enganche a la página.
// Uso: node topstepx/abrir.js
'use strict';
const path = require('path');
const { chromium } = require('playwright');
const PERFIL = path.join(__dirname, '..', 'perfil-topstepx');
const PUERTO = Number(process.env.QBOT_TSX_PUERTO || 9333);
(async () => {
  const ctx = await chromium.launchPersistentContext(PERFIL, {
    headless: false, viewport: null,
    args: ['--start-maximized', `--remote-debugging-port=${PUERTO}`, '--remote-debugging-address=127.0.0.1'],
  });
  const page = ctx.pages()[0] || await ctx.newPage();
  await page.goto('https://topstepx.com/', { waitUntil: 'domcontentloaded' });
  console.log(`TopstepX abierto (depuración local en 127.0.0.1:${PUERTO}). Inicia sesión; no cierres la ventana.`);
  await new Promise((r) => ctx.on('close', r));
})().catch((e) => { console.error(e.message); process.exit(1); });
