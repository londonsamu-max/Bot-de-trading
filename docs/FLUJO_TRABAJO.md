# Flujo de órdenes de trabajo

Revisa los correos de clientes, convoca a los trabajadores por WhatsApp para
confirmar asistencia y, cuando hay cuadrilla, manda la orden de trabajo con el
material asignado y lo reserva del inventario.

Es un módulo independiente del bot de trading: vive en `src/workflow/` y se
ejecuta con `workflow.py`. No comparte nada con `main.py`.

## De dónde llegan las solicitudes

`jbrenovate.com` **no publica ninguna dirección de correo**. Los clientes
contactan por dos vías, y las dos terminan en el buzón como un correo
**reenviado por GoDaddy**, no escrito por el cliente:

1. el formulario *"Drop us a line!"* (Name, Email, Phone, Attach Files),
2. el sistema de *Bookings*.

Esto importa mucho: el remitente de esos correos es un `noreply@` de GoDaddy, y
el cliente real viene **dentro del cuerpo**. Por eso existe
`recepcion.reenviadores` en `config/workflow.yaml`: los remitentes que estén ahí

- **no** se descartan por el filtro de correos automáticos, y
- se les saca del cuerpo el nombre, correo y teléfono reales del cliente, que es
  a donde se manda el acuse de recibo.

> **Ajusta esa lista con un correo real.** Puse los dominios habituales de
> GoDaddy (`@godaddy.com`, `@secureserver.net`, `@email.godaddy.com`), pero el
> remitente exacto solo se ve en un correo que ya te haya llegado. Si no
> coincide, esas solicitudes se ignoran en silencio. Para comprobarlo:
> `python workflow.py once --dry-run` y mira el log.

Un correo del formulario se interpreta así:

```
Name: Sarah Miller            -> cliente
Email: sarah.miller@gmail.com -> a quién se le responde (no a GoDaddy)
Phone: (555) 987-6543         -> va en la orden de trabajo, para llamar al llegar
Message: ...                  -> descripción del trabajo
Attach Files                  -> las fotos se guardan y se mandan a la cuadrilla
```

Las etiquetas funcionan en español y en inglés (`Name`/`Nombre`,
`Address`/`Dirección`, `Materials`/`Materiales`, `Message`/`Mensaje`...), porque
el sitio está en inglés pero los mensajes al personal salen en español.

## Cómo funciona

```
correo del cliente (o formulario del sitio, reenviado)
       |
       v
  [1] se lee la bandeja (IMAP) y se interpreta el correo
       |  -> folio JB-AAAAMMDD-NNN, cliente, dirección, fecha, materiales
       |  -> se acusa recibo al cliente
       v
  [2] se revisa el inventario (data/inventario.csv)
       |  -> se marca lo que falta y se avisa al supervisor
       v
  [3] se convoca a los trabajadores (data/trabajadores.csv)
       |  -> WhatsApp: "¿Puedes asistir? Responde SI o NO"
       |  -> se eligen por habilidad, zona y carga de trabajo
       v
  [4] llegan las respuestas (webhook de WhatsApp, correo o a mano)
       |  -> SI  -> cuenta para el cupo
       |  -> NO  -> se convoca al siguiente de la lista
       |  -> sin respuesta -> recordatorio a la hora, expira a las 4 horas
       v
  [5] cuando se cubre el cupo
          -> se reserva el material en el inventario
          -> se manda la ORDEN DE TRABAJO a cada confirmado
          -> se avisa al cliente que su servicio quedó asignado
          -> a quien seguía pendiente se le avisa que ya está cubierto
```

Cada ciclo es idempotente: si el proceso se cae a medias y vuelve a arrancar,
no reenvía nada que ya haya salido.

### Las fotos del cliente

El formulario del sitio deja adjuntar archivos, y en remodelación la foto suele
explicar el trabajo mejor que el texto. Se guardan en
`data/adjuntos/<folio>/` y **viajan adjuntas en el correo de la orden de
trabajo**, porque por WhatsApp de texto no se pueden mandar; el mensaje de
WhatsApp solo dice cuántas fotos hay y que están en el correo.

### Horario de envíos

Las convocatorias llegan al celular personal de la gente, así que solo salen
dentro de `general.horario_envios` (por defecto lun-vie 08:00-19:00, tomado del
horario publicado en el sitio y ampliado un poco). Fuera de esa ventana el
trabajo **espera**, no se da por perdido.

Lo que **nunca** se retiene: la orden de trabajo a quien ya confirmó (está
esperando los datos) y las alertas al supervisor.

> **Revisa `zona_horaria`.** El sitio no publica dirección, así que dejé
> `America/New_York` como valor por defecto. Si no es tu zona, cámbialo o los
> mensajes saldrán a horas equivocadas.

## Puesta en marcha

```bash
pip install -r requirements.txt      # no agrega dependencias nuevas
python workflow.py init              # crea data/inventario.csv y data/trabajadores.csv
cp .env.example .env                 # y llena las credenciales del correo
python workflow.py check             # valida config y prueba la conexión IMAP
```

### Credenciales del correo (`.env`)

`Workorder@jbrenovate.com` se conecta por IMAP y SMTP. Según dónde esté alojado
el dominio:

| Proveedor | IMAP_HOST | SMTP_HOST |
|---|---|---|
| Google Workspace | `imap.gmail.com` | `smtp.gmail.com` |
| Microsoft 365 | `outlook.office365.com` | `smtp.office365.com` |

En los dos casos hay que usar una **contraseña de aplicación**, no la del
usuario, y tener la verificación en dos pasos activada. En Google se genera en
*Cuenta > Seguridad > Contraseñas de aplicaciones*; en Microsoft, en
*Seguridad > Opciones de seguridad adicionales*.

### Los dos archivos de datos

`data/trabajadores.csv` — se edita en Excel:

```csv
id,nombre,telefono,email,habilidades,zona,activo
T01,Juan Perez,+15551234001,juan@ejemplo.com,pintura|drywall,norte,si
```

- `habilidades` separadas por `|`. El correo del cliente pide habilidades y solo
  se convoca a quien las tenga.
- `zona` se usa para preferir a quien está más cerca.
- `activo` en `no` saca a esa persona de las convocatorias sin borrarla.

`data/inventario.csv`:

```csv
sku,nombre,unidad,stock,reservado,minimo
PIN-BLA-5,Pintura blanca 5 galones,cubeta,12,0,3
```

- `reservado` lo maneja el bot: es material comprometido que todavía está en
  bodega. `disponible = stock - reservado`.
- `minimo` es el punto de reorden; `python workflow.py inventario --bajos`
  muestra lo que ya lo alcanzó.

## WhatsApp

Se elige en `config/workflow.yaml`, en `whatsapp.proveedor`:

- **`manual`** (el de arranque): no manda nada por API. Escribe cada mensaje en
  `logs/whatsapp_pendientes.txt` con un enlace `wa.me` listo para abrir y
  enviar desde el teléfono. Sirve para operar el flujo completo sin tener aún
  una cuenta de WhatsApp Business.
- **`twilio`**: necesita `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` y
  `TWILIO_WHATSAPP_FROM`.
- **`meta`**: WhatsApp Cloud API, necesita `META_PHONE_NUMBER_ID` y
  `META_ACCESS_TOKEN`. Fuera de la ventana de 24 horas Meta solo deja mandar
  plantillas aprobadas; en ese caso hay que poner el nombre de la plantilla en
  `whatsapp.meta.plantilla`.

Si WhatsApp falla, el mismo mensaje se manda por correo automáticamente
(`whatsapp.respaldo_email`).

### Recibir las respuestas

Hay tres formas, y las tres se pueden usar a la vez:

1. **A mano** (siempre funciona, no necesita nada):
   ```bash
   python workflow.py confirmar T01 --si
   python workflow.py confirmar T03 --no --job JB-20260820-001
   ```
2. **Por correo**: si el trabajador responde el correo de respaldo con "SI" o
   "NO", se registra solo.
3. **Webhook de WhatsApp** (automático de verdad): requiere una URL pública con
   HTTPS apuntando al servidor.
   ```bash
   python workflow.py webhook --puerto 8080
   ```
   Esa URL se configura en Twilio (*Sandbox / Messaging > A message comes in*) o
   en Meta (*WhatsApp > Configuration > Webhook*, con `META_VERIFY_TOKEN`).

Se entienden respuestas naturales: `si`, `sí`, `ok`, `confirmo`, `ahí estaré`,
`no puedo`, `hoy no`, 👍, 👎. Si algo no se entiende, se pide una aclaración
**una sola vez** y el trabajo queda marcado para revisión humana.

## Uso diario

```bash
python workflow.py run                    # deja el flujo corriendo (cada 5 min)
python workflow.py once --dry-run         # un ciclo sin mandar nada, para probar
python workflow.py estado                 # trabajos abiertos
python workflow.py estado --job JB-...    # detalle: material, quién confirmó, historial
python workflow.py inventario             # existencias, reservado y disponible
python workflow.py completar JB-...       # cierra el trabajo y descuenta el material
```

Para dejarlo corriendo como servicio en Linux:

```ini
# /etc/systemd/system/workflow.service
[Service]
WorkingDirectory=/ruta/al/repo
ExecStart=/usr/bin/python3 workflow.py run
Restart=always
```

## Qué correos entiende

Lo ideal es que el cliente mande campos etiquetados, en cualquier orden y con o
sin acentos:

```
Cliente: Constructora Vega
Direccion: Av. Reforma 123, local 4
Zona: norte
Fecha: 20/08/2026
Hora: 08:00
Habilidades: pintura
Trabajadores: 2

Materiales:
- 2 cubetas Pintura blanca 5 galones
- 4 Brocha 4 pulgadas
- Cinta de enmascarar azul x 6
```

Si el correo viene sin etiquetas, igual se procesa: el cuerpo se toma como
descripción y las líneas con viñeta que parecen material (`- 3 Pintura`) se
detectan solas. Lo que no se pueda deducir queda vacío y aparece en
`python workflow.py estado --job ...` para completarlo a mano.

Se pueden agregar las etiquetas que use cada cliente en `parser.etiquetas_extra`
dentro de `config/workflow.yaml`, sin tocar código.

Para probar cómo se interpreta un correo sin tocar el buzón:

```bash
python workflow.py simular correo.txt --de cliente@ejemplo.com
```

## Ajustes que probablemente quieras cambiar

En `config/workflow.yaml`:

| Opción | Qué hace |
|---|---|
| `general.intervalo_segundos` | Cada cuánto se revisa la bandeja (300 = 5 min) |
| `general.horario_envios.zona_horaria` | Tu zona horaria real (revísala) |
| `general.horario_envios.inicio` / `.fin` / `.dias` | Ventana en que se puede molestar al personal |
| `recepcion.reenviadores` | Remitentes que reenvían el formulario web (revísalo) |
| `asistencia.recordatorio_minutos` | Cuánto se espera antes de insistir (60) |
| `asistencia.expiracion_horas` | Cuándo se da por perdida una convocatoria (4) |
| `asistencia.margen_convocatoria` | Cuánta gente extra se convoca por si alguien dice que no (1) |
| `despacho.exigir_inventario_completo` | `true` = no manda la orden si falta material |
| `recepcion.remitentes_permitidos` | Lista blanca de clientes; vacío = cualquiera |
| `alertas.email_supervisor` | A quién avisar cuando algo necesita a una persona |
| `mensajes.*` | El texto exacto de cada mensaje que se envía |

## Cuándo avisa a una persona

El bot manda una alerta al supervisor (`alertas.email_supervisor` /
`alertas.whatsapp_supervisor`) cuando:

- falta material para un trabajo,
- nadie confirmó y ya no quedan convocatorias pendientes (`sin_personal`),
- llega un WhatsApp de un número que no está en `trabajadores.csv`,
- el cliente responde sobre un folio que ya existe.

## Pruebas

```bash
python -m pytest tests/test_workflow.py -q
```

78 pruebas que cubren el parser de correos (incluido un formulario web
reenviado por GoDaddy), el inventario, la selección de trabajadores, la máquina
de estados de asistencia, los adjuntos, la ventana de envíos y un ciclo completo
de punta a punta con buzón y WhatsApp simulados. No tocan la red ni necesitan
credenciales.
