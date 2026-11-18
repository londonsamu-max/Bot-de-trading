// caffeinate -i evita que el Mac se duerma mientras el bot corre (no evita el reposo al cerrar la tapa).
// Programa el bot para que arranque solo de lunes a viernes a las 8:20 hora local (Colombia),
// que cubre la apertura de las 9:30 NY tanto en invierno como en verano. El bot se cierra solo a las 16:10 NY.
// Uso: node instalar-horario.js            (instala)
//      node instalar-horario.js --quitar   (desinstala)
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execSync } = require('child_process');

const DIR = __dirname;
const NODE = process.execPath;
const quitar = process.argv.includes('--quitar');

if (process.platform === 'darwin') {
  const label = 'com.samuel.qoibot';
  const plist = path.join(os.homedir(), 'Library', 'LaunchAgents', `${label}.plist`);
  try { execSync(`launchctl unload "${plist}"`, { stdio: 'ignore' }); } catch (e) {}
  if (quitar) { if (fs.existsSync(plist)) fs.unlinkSync(plist); console.log('Horario quitado.'); process.exit(0); }
  const days = [1, 2, 3, 4, 5].map((d) => `<dict><key>Weekday</key><integer>${d}</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>20</integer></dict>`).join('');
  fs.mkdirSync(path.dirname(plist), { recursive: true });
  fs.writeFileSync(plist, `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>${label}</string>
<key>ProgramArguments</key><array><string>/usr/bin/caffeinate</string><string>-i</string><string>${NODE}</string><string>${path.join(DIR, 'runner.js')}</string></array>
<key>WorkingDirectory</key><string>${DIR}</string>
<key>StartCalendarInterval</key><array>${days}</array>
<key>StandardOutPath</key><string>${path.join(DIR, 'registros', 'consola.log')}</string>
<key>StandardErrorPath</key><string>${path.join(DIR, 'registros', 'errores.log')}</string>
</dict></plist>`);
  execSync(`launchctl load "${plist}"`);
  console.log('Listo: el bot arrancará de lunes a viernes a las 8:20 (hora del computador).');
  console.log('El Mac debe estar encendido y con la sesión de usuario abierta a esa hora (Ajustes > Batería > programar encendido ayuda).');
} else if (process.platform === 'win32') {
  const name = 'BotQOI';
  try { execSync(`schtasks /Delete /TN ${name} /F`, { stdio: 'ignore' }); } catch (e) {}
  if (quitar) { console.log('Horario quitado.'); process.exit(0); }
  execSync(`schtasks /Create /TN ${name} /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:20 /TR "\\"${NODE}\\" \\"${path.join(DIR, 'runner.js')}\\"" /F`);
  console.log('Listo: el bot arrancará de lunes a viernes a las 8:20. El computador debe estar encendido y con tu sesión abierta.');
} else {
  console.log('Agrega esta línea a tu crontab (crontab -e):');
  console.log(`20 8 * * 1-5 cd "${DIR}" && "${NODE}" runner.js >> registros/consola.log 2>&1`);
}
