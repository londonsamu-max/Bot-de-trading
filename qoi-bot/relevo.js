// Relevo local del token de Quantium: tu Chrome (script de Tampermonkey qbot-token.user.js) manda aquí su sesión
// cada 30 s y el bot la usa sin renovarla nunca. Escucha solo en este Mac (127.0.0.1) y exige una clave privada.
//   GET  http://127.0.0.1:8765/qbot-token.user.js  -> instala el script en Tampermonkey (con la clave ya puesta)
//   POST http://127.0.0.1:8765/token               -> recibe la sesión (cabecera x-qbot-clave)
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const crypto = require('crypto');
const sesion = require('./sesion');

const PUERTO = Number(process.env.QBOT_RELEVO_PUERTO || 8765);
const CLAVE_PATH = path.join(os.homedir(), '.qoibot-relay-key');

function clave() {
  try { return fs.readFileSync(CLAVE_PATH, 'utf8').trim(); } catch (e) {}
  const k = crypto.randomBytes(24).toString('hex');
  fs.writeFileSync(CLAVE_PATH, k, { mode: 0o600 });
  return k;
}

function script(k) {
  return `// ==UserScript==
// @name         Bot Q OI — pasa la sesión de Quantium al bot
// @namespace    samuel.qoi
// @version      1.0
// @description  Manda la sesión de esta pestaña de Quantium al bot de tu Mac (127.0.0.1) cada 30 s. El bot no la renueva: solo Chrome.
// @match        https://quantium.global/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==
(function () {
  'use strict';
  const KEY = 'sb-euwdfoswzwsqrknnvjvj-auth-token';
  let ultimo = null;
  function mandar(forzar) {
    const v = localStorage.getItem(KEY);
    if (!v || (!forzar && v === ultimo)) return;
    GM_xmlhttpRequest({ method: 'POST', url: 'http://127.0.0.1:${PUERTO}/token', headers: { 'Content-Type': 'application/json', 'x-qbot-clave': '${k}' }, data: v, timeout: 5000,
      onload: (r) => { if (r.status === 200) ultimo = v; },
      onerror: () => {}, ontimeout: () => {} });
  }
  mandar(true);
  setInterval(() => mandar(false), 30000);   // cuando Chrome renueva el token, el bot lo recibe en menos de 30 s
  setInterval(() => mandar(true), 300000);   // y cada 5 min de todas formas (por si el bot se reinició)
  window.addEventListener('storage', (e) => { if (e.key === KEY) mandar(false); });
})();
`;
}

function iniciar(log) {
  const k = clave();
  let recibido = 0;
  const srv = http.createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/qbot-token.user.js') {
      res.writeHead(200, { 'Content-Type': 'text/javascript; charset=utf-8' }); return res.end(script(k));
    }
    if (req.method === 'POST' && req.url === '/token') {
      const dada = String(req.headers['x-qbot-clave'] || '');
      if (dada.length !== k.length || !crypto.timingSafeEqual(Buffer.from(dada), Buffer.from(k))) { res.writeHead(403); return res.end('clave'); }
      let body = ''; req.on('data', (c) => { body += c; if (body.length > 20000) req.destroy(); });
      req.on('end', () => {
        try {
          const nuevo = sesion.recibir(body);
          if (nuevo) { recibido += 1; log(`sesión recibida de Chrome · caduca en ${sesion.minutosRestantes()} min`, recibido === 1); }
          res.writeHead(200); res.end('ok');
        } catch (e) { log('token de Chrome rechazado: ' + e.message, true); res.writeHead(400); res.end(e.message); }
      });
      return;
    }
    res.writeHead(404); res.end();
  });
  srv.on('error', (e) => log('el relevo no pudo abrir el puerto ' + PUERTO + ': ' + e.message, true));
  srv.listen(PUERTO, '127.0.0.1');
  return srv;
}

module.exports = { iniciar, PUERTO };
