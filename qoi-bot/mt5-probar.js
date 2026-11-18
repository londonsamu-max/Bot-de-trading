// Prueba del puente con MT5.
//   node mt5-probar.js           -> solo muestra cuenta, si es demo, precios y posiciones (no opera)
//   node mt5-probar.js --orden   -> en cuenta DEMO: compra 0.1 lote de US500 y lo cierra a los 3 s
'use strict';
const mt5 = require('./mt5');
(async () => {
  const e = mt5.estado();
  if (!e.ok) { console.log('MT5:', e.error); process.exit(1); }
  const s = e.s; const demo = mt5.cuentaSegura(s, {});
  console.log(`cuenta ${s.account} · ${s.server} · modo ${s.mode} · ${demo ? 'DEMO: se puede operar' : 'NO ES DEMO: el bot no operará'}`);
  console.log('saldo', s.balance, s.currency, '· posiciones del EA:', (s.openPositions || []).length);
  console.log('precios', JSON.stringify(s.prices));
  if (!process.argv.includes('--orden')) return;
  if (!demo) { console.log('no se manda la orden de prueba: la cuenta no es demo'); process.exit(1); }
  const cuenta = { nombre: 'prueba', tipo: 'mt5', riesgoUSD: 1, usdPorPuntoLote: { US500: 1 }, maxLotes: { US500: 0.1 } };
  console.log(await mt5.senal({ ticker: 'MESZ2026', action: 'buy', stopLoss: { type: 'stop', amount: 10 } }, Object.assign({}, cuenta, { riesgoUSD: 1000 })));
  await new Promise((r) => setTimeout(r, 3000));
  console.log(await mt5.senal({ ticker: 'MESZ2026', action: 'exit' }, cuenta));
})().catch((e) => { console.error(e.message); process.exit(1); });
