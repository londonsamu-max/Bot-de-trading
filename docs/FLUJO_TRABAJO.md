# Flujo de órdenes de trabajo

Revisa los correos de clientes, convoca a los trabajadores por WhatsApp para
confirmar asistencia y, cuando hay cuadrilla, manda la orden de trabajo con el
material asignado y lo reserva del inventario.

Es un módulo independiente del bot de trading: vive en `src/workflow/` y se
ejecuta con `workflow.py`. No comparte nada con `main.py`.

## De dónde llegan las solicitudes

**El canal principal son las empresas de gestión de apartamentos.** Escriben
directo a `Workorder@jbrenovate.com` con una lista de unidades, y cada renglón
es un trabajo distinto:

```
B109 vacant,  full tub. 8/8/26
M102 Vacant, full tub. 8/8/26
M104 Vacant, full tub. 8/8/26
J209 vacant, full tub. 8/10/26 AM please

Best Regards,
Guillermo Gonzalez — Service Manager
Park Mesa Villas
550 Paularino Avenue, Costa Mesa, CA 92626
```

Ese correo son **4 trabajos**, no uno. De cada renglón se saca:

| Del renglón | Sale |
|---|---|
| `B109` | Unit |
| `vacant` / `occ` | si la unidad está ocupada (hay que avisar al residente) |
| `full tub` | Service Description, y de ahí el Service (`tub`) |
| `8/8/26` | DATE — se lee en formato de EE.UU., `8/10/26` es 10 de agosto |
| `AM` | turno |

Y del remitente (`pmvmaint@rentparkmesa.com`), mirando `data/propiedades.csv`,
salen **Mgmt CO.** (Shea Properties) y **Property Name** (Park Mesa Villas).

La firma no genera trabajos: un renglón solo cuenta si empieza con un número de
unidad **y** además nombra un servicio o dice `vacant`/`occ`. Por eso
`550 Paularino Avenue` y `P. 714-751-6995` se ignoran.

### Servicios que reconoce

`paint`, `jani`, `carpet` y `tub`, con sus variantes (`cc`, `full paint`,
`flat paint`, `shampoo`, `reglaze`...). Se agregan más en
`parser.servicios_extra` sin tocar código.

Ojo con un caso: `occ` es *occupied*, `cc` es limpieza de alfombra. El parser
los distingue, así que `90-es occ cc wear shoe covers pm` se lee como alfombra
en unidad ocupada, turno PM.

Si un renglón nombra dos servicios (`full paint and jani`), salen **dos**
trabajos, igual que en tu hoja son dos renglones.

### Canal secundario: el formulario del sitio

`jbrenovate.com` no publica correo; lleva un formulario *"Drop us a line!"* cuyo
aviso llega **reenviado por GoDaddy**. Esos remitentes van en
`recepcion.reenviadores` para que no se filtren como `noreply` y para sacar del
cuerpo el cliente real. Se procesan como una solicitud suelta (un trabajo).

> **Ajusta `recepcion.reenviadores` con un correo real.** El remitente exacto
> solo se ve en uno que ya te haya llegado.

## Cómo funciona

```
correo de la empresa de gestión (varias unidades)
       |
       v
  [1] se lee la bandeja (IMAP) -> UN TRABAJO POR UNIDAD
       |  -> folio JB-AAAAMMDD-NNN por unidad
       |  -> empresa, propiedad, unidad, tamaño, servicio, fecha, turno
       |  -> un solo acuse de recibo con la lista completa de folios
       v
  [2] se calcula el material (data/consumos.csv: servicio + tamaño)
       |  -> se revisa contra el inventario y se avisa lo que falta
       v
  [3] se convoca a los trabajadores (data/trabajadores.csv)
       |  -> WhatsApp: "¿Puedes asistir? Responde SI o NO"
       |  -> se eligen por habilidad, zona y carga DE ESE DÍA (tope diario)
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
       v
  [6] python workflow.py exportar
          -> CSV con las columnas de tu control, listo para Excel
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
python workflow.py init              # crea los 5 CSV de datos con ejemplos
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

### Los cinco archivos de datos

Todos se editan en Excel. `python workflow.py init` los crea con ejemplos.

**`data/propiedades.csv`** — quién es cada cliente. Es el que hace que un correo
se reconozca como lista de unidades:

```csv
propiedad,empresa_gestion,dominio,contacto,direccion,zona,alias
Park Mesa Villas,Shea Properties,rentparkmesa.com,pmvmaint@rentparkmesa.com,550 Paularino Ave Costa Mesa CA,costa mesa,PMV|Park Mesa
```

- `dominio` o `contacto` identifican de quién viene el correo. **Este es el
  archivo clave**: sin él, un correo de una empresa nueva se procesa como
  solicitud suelta y no se separa por unidades.
- `alias` (separados por `|`) sirve cuando escriben desde otro buzón y solo se
  puede reconocer la propiedad por el nombre en la firma.

**`data/trabajadores.csv`** — tu personal:

```csv
id,nombre,telefono,email,habilidades,zona,activo
T06,Luis,+15551234006,luis@ejemplo.com,tinas|pintura,costa mesa,si
```

- `habilidades` separadas por `|`, y se conectan con el servicio en
  `servicios.habilidades` del config (`tub` -> `tinas`, `paint` -> `pintura`...).
- `zona` prefiere a quien está más cerca de la propiedad.
- `activo` en `no` lo saca de las convocatorias sin borrarlo.

**`data/unidades.csv`** — el tamaño de cada apartamento, que el correo no dice:

```csv
propiedad,unidad,tamano
Park Mesa Villas,B109,1+1
```

Sin esto el tamaño sale vacío y el material no se puede calcular por tamaño.

**`data/consumos.csv`** — cuánto material lleva cada servicio. Los correos
**nunca** listan materiales, así que esta tabla es la que hace que la cuadrilla
sepa qué recoger:

```csv
servicio,tamano,sku,cantidad
paint,*,BRO-4,2        <- '*' = para cualquier tamaño
paint,1+1,PIN-BLA-5,2
paint,3+2,PIN-BLA-5,4  <- la regla del tamaño manda sobre la de '*'
```

**`data/inventario.csv`** — tus existencias:

```csv
sku,nombre,unidad,stock,reservado,minimo
PIN-BLA-5,Pintura blanca 5 galones,cubeta,12,0,3
```

- `reservado` lo maneja el bot: material comprometido que sigue en bodega.
  `disponible = stock - reservado`.
- `minimo` es el punto de reorden; `python workflow.py inventario --bajos`
  muestra lo que ya lo alcanzó.

## El control de trabajos (Excel)

```bash
python workflow.py exportar --ver              # en pantalla
python workflow.py exportar                    # a data/control_trabajos.csv
python workflow.py exportar --desde 2026-08-01 --hasta 2026-08-31
python workflow.py exportar --solo-asignados   # solo con personal confirmado
python workflow.py exportar --detalle          # + folio, estado, turno, material
```

Salen exactamente tus columnas y en el mismo orden, así que se pega directo en
la hoja:

```
DATE    | Service | Mgmt CO.        | Property Name    | Person | Unit | Size | Service Description
8/8/26  | tub     | Shea Properties | Park Mesa Villas | Luis   | B109 | 1+1  | full tub
8/10/26 | tub     | Shea Properties | Park Mesa Villas | Luis   | J209 | 2+2  | full tub AM
```

**Person** es quien confirmó asistencia. Si a una unidad van dos personas, salen
dos renglones, como ya lo llevas tú.

## Tope de trabajo por día

`asistencia.max_trabajos_por_dia` (2 por defecto) limita cuántas unidades toma
una persona **el mismo día**. Sin ese tope, cuando una empresa manda 6 unidades
juntas se las lleva todas quien esté más cerca de la propiedad.

La carga se cuenta **por fecha de servicio**: tener el martes lleno no impide
tomar trabajo el jueves. Si todos llegan al tope, se asigna igual (mejor eso que
dejar la unidad sin nadie) y queda avisado en el log.

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
python workflow.py exportar --ver         # control de trabajos con tus columnas
python workflow.py simular correo.txt --de pmvmaint@rentparkmesa.com
```

`simular` con un remitente de `propiedades.csv` muestra la tabla de unidades
detectadas con su material, sin tocar el buzón ni mandar nada. Es la forma de
comprobar que un correo nuevo se lee bien antes de dejarlo en automático.

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
| `asistencia.max_trabajos_por_dia` | Unidades máximas por persona y día (2) |
| `parser.formato_fecha` | `US` (8/10/26 = 10 de agosto) o `EU` |
| `parser.servicios_extra` | Más palabras para reconocer cada servicio |
| `servicios.habilidades` | Qué habilidad exige cada servicio |
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

109 pruebas. Entre ellas, el correo real de Park Mesa Villas de punta a punta:
que salgan 4 trabajos y no 1, que la firma no genere trabajos falsos, que
`8/10/26` sea el 10 de agosto, que `occ` no se confunda con `cc`, que un reenvío
de la misma lista no duplique nada, y que el export reproduzca las columnas de
la hoja. No tocan la red ni necesitan credenciales.
