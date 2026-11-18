// Sesión de Quantium para el modo sin navegador: lee el token guardado y lo renueva solo antes de que caduque.
// Archivos (fuera del repo, permisos 600):
//   ~/.qoibot-token.json      -> la sesión de Supabase copiada de localStorage (access_token + refresh_token)
//   ~/.qoibot-supabase-key    -> la clave pública (apikey) de Supabase que usa la app de Quantium
// Cada renovación entrega un refresh_token nuevo y el anterior deja de servir. Si la misma sesión se usa
// en tu Chrome, activa "sesionDesdeChrome" en config.json (modo Chrome, abajo) para que solo Chrome renueve.
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');

const SUPABASE = 'https://euwdfoswzwsqrknnvjvj.supabase.co';
const TOKEN_PATH = process.env.QBOT_TOKEN || path.join(os.homedir(), '.qoibot-token.json');
const KEY_PATH = process.env.QBOT_SUPABASE_KEY || path.join(os.homedir(), '.qoibot-supabase-key');
const MARGEN_S = 5 * 60; // renovar cuando falten menos de 5 minutos

let ses = null;
// Modo Chrome: la sesión la renueva SOLO tu Chrome (pestaña de Quantium abierta) y la manda al bot cada 30 s
// por el relevo local (runner.js + qbot-token.user.js). El bot nunca renueva: así comparten la misma sesión sin chocar.
let modoChrome = false;
const usarChrome = (v) => { modoChrome = !!v; };

function leer() {
  let d = JSON.parse(fs.readFileSync(TOKEN_PATH, 'utf8'));
  if (typeof d === 'string') d = JSON.parse(d);
  if (!d || !d.access_token || (!modoChrome && !d.refresh_token)) throw new Error(`${TOKEN_PATH} no tiene access_token y refresh_token`);
  return d;
}
function guardar(d) {
  const tmp = TOKEN_PATH + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(d), { mode: 0o600 });
  fs.renameSync(tmp, TOKEN_PATH);
}
function expira(d) {
  if (d.expires_at) return d.expires_at;
  try { const p = d.access_token.split('.')[1]; return JSON.parse(Buffer.from(p, 'base64url').toString()).exp; } catch (e) { return 0; }
}
const minutosRestantes = () => (ses ? Math.round((expira(ses) - Date.now() / 1000) / 60) : null);

async function renovar() {
  const apikey = fs.readFileSync(KEY_PATH, 'utf8').trim();
  const r = await fetch(`${SUPABASE}/auth/v1/token?grant_type=refresh_token`, {
    method: 'POST',
    headers: { apikey, 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: ses.refresh_token }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.access_token) throw new Error(`no se pudo renovar la sesión (${r.status} ${j.error_code || j.msg || j.error || ''}). Copia un token nuevo.`);
  ses = Object.assign({}, ses, j); // conserva campos como user si la respuesta no los trae
  guardar(ses);
}

// Asegura un token vigente; devuelve true si tuvo que renovarlo.
async function asegurar() {
  if (modoChrome) {
    if (!ses) { try { ses = leer(); } catch (e) {} }
    if (!ses || expira(ses) - Date.now() / 1000 < 30) throw new Error('no hay token vigente de Chrome: abre Quantium en tu Chrome (con la sesión iniciada) y deja la pestaña abierta');
    return false;
  }
  if (!ses) ses = leer();
  if (expira(ses) - Date.now() / 1000 > MARGEN_S) return false;
  // Otro proceso (otra copia del bot, una prueba) puede haber renovado y guardado ya una sesión más nueva:
  // usarla en vez de gastar un refresh_token que ya no sirve.
  try { const f = leer(); if (expira(f) > expira(ses)) { ses = f; if (expira(ses) - Date.now() / 1000 > MARGEN_S) return true; } } catch (e) {}
  await renovar();
  return true;
}

// Token que llega de Chrome por el relevo local. Se acepta solo si es un token de Quantium válido y más nuevo.
function recibir(d) {
  if (typeof d === 'string') d = JSON.parse(d);
  if (!d || typeof d.access_token !== 'string' || d.access_token.split('.').length !== 3) throw new Error('token con formato inválido');
  let c; try { c = JSON.parse(Buffer.from(d.access_token.split('.')[1], 'base64url').toString()); } catch (e) { throw new Error('token ilegible'); }
  if (!/euwdfoswzwsqrknnvjvj\.supabase\.co/.test(String(c.iss || ''))) throw new Error('el token no es de Quantium');
  if (expira(d) - Date.now() / 1000 < 30) throw new Error('el token ya caducó');
  const nuevo = !ses || expira(d) > expira(ses) || d.access_token !== ses.access_token && expira(d) >= expira(ses);
  if (nuevo) { ses = d; guardar(d); }
  return nuevo;
}

const accessToken = () => (ses ? ses.access_token : null);
// Lo que bot.js lee de localStorage con la clave sb-...-auth-token
const comoLocalStorage = () => (ses ? JSON.stringify(ses) : null);

module.exports = { usarChrome, recibir, renovarYa: async () => { if (!ses) ses = leer(); await renovar(); }, asegurar, accessToken, comoLocalStorage, minutosRestantes, TOKEN_PATH };
