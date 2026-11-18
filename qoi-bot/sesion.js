// Sesión de Quantium para el modo sin navegador: lee el token guardado y lo renueva solo antes de que caduque.
// Archivos (fuera del repo, permisos 600):
//   ~/.qoibot-token.json      -> la sesión de Supabase copiada de localStorage (access_token + refresh_token)
//   ~/.qoibot-supabase-key    -> la clave pública (apikey) de Supabase que usa la app de Quantium
// Cada renovación entrega un refresh_token nuevo y el anterior deja de servir: por eso esta sesión
// debe ser solo del bot (cópiala de una ventana de incógnito y cierra esa ventana).
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');

const SUPABASE = 'https://euwdfoswzwsqrknnvjvj.supabase.co';
const TOKEN_PATH = process.env.QBOT_TOKEN || path.join(os.homedir(), '.qoibot-token.json');
const KEY_PATH = process.env.QBOT_SUPABASE_KEY || path.join(os.homedir(), '.qoibot-supabase-key');
const MARGEN_S = 5 * 60; // renovar cuando falten menos de 5 minutos

let ses = null;

function leer() {
  let d = JSON.parse(fs.readFileSync(TOKEN_PATH, 'utf8'));
  if (typeof d === 'string') d = JSON.parse(d);
  if (!d || !d.access_token || !d.refresh_token) throw new Error(`${TOKEN_PATH} no tiene access_token y refresh_token`);
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
  if (!ses) ses = leer();
  if (expira(ses) - Date.now() / 1000 > MARGEN_S) return false;
  await renovar();
  return true;
}

const accessToken = () => (ses ? ses.access_token : null);
// Lo que bot.js lee de localStorage con la clave sb-...-auth-token
const comoLocalStorage = () => (ses ? JSON.stringify(ses) : null);

module.exports = { renovarYa: async () => { if (!ses) ses = leer(); await renovar(); }, asegurar, accessToken, comoLocalStorage, minutosRestantes, TOKEN_PATH };
