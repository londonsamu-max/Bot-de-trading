"""
Tests for the work dispatch workflow.

Covers the email parser, inventory reservations, worker selection, the
attendance state machine, and one end-to-end cycle with a fake mailbox and a
fake WhatsApp provider (no network, no credentials).
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.workflow.attendance import AttendanceManager
from src.workflow.email_client import Adjunto, CorreoEntrante, _html_to_text
from src.workflow.inbox import WhatsAppInbox
from src.workflow.inventory import Inventory
from src.workflow.messages import MessageBuilder
from src.workflow.models import (
    Convocatoria,
    EstadoConvocatoria,
    EstadoTrabajo,
    ItemPedido,
    Trabajo,
    utc_now,
)
from src.workflow.notifier import Notifier, ResultadoEnvio, WhatsAppNotifier
from src.workflow.parser import (
    EmailJobParser,
    extract_job_id,
    parse_answer,
    parse_date,
    parse_quantity_line,
)
from src.workflow.runner import WorkflowRunner, _nombre_seguro
from src.workflow.schedule import VentanaDeEnvio
from src.workflow.store import WorkflowStore
from src.workflow.webhook import parse_payload
from src.workflow.workers import WorkerRoster

INVENTARIO_CSV = """sku,nombre,unidad,stock,reservado,minimo
PIN-BLA-5,Pintura blanca 5 galones,cubeta,10,0,3
BRO-4,Brocha 4 pulgadas,pza,20,0,6
CIN-AZU,Cinta de enmascarar azul,rollo,4,0,10
"""

TRABAJADORES_CSV = """id,nombre,telefono,email,habilidades,zona,activo
T01,Juan Perez,555-123-4001,juan@ejemplo.com,pintura|drywall,norte,si
T02,Maria Lopez,+15551234002,maria@ejemplo.com,pintura,norte,si
T03,Carlos Ruiz,5551234003,carlos@ejemplo.com,drywall,sur,si
T04,Rosa Diaz,5551234004,rosa@ejemplo.com,pintura,norte,no
"""

CORREO_CLIENTE = """\
Buenos dias,

Necesito que me pinten la oficina del segundo piso.

Cliente: Constructora Vega
Direccion: Av. Reforma 123, local 4
Zona: norte
Fecha: 15/09/2026
Hora: 08:00
Habilidades: pintura
Trabajadores: 2

Materiales:
- 2 cubetas Pintura blanca 5 galones
- 4 Brocha 4 pulgadas
- Cinta de enmascarar azul x 6

Gracias,
Ing. Vega
"""


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def datos(tmp_path):
    """Inventory + roster CSVs in a temp dir."""
    inventario = tmp_path / "inventario.csv"
    trabajadores = tmp_path / "trabajadores.csv"
    inventario.write_text(INVENTARIO_CSV, encoding="utf-8")
    trabajadores.write_text(TRABAJADORES_CSV, encoding="utf-8")
    return {"inventario": inventario, "trabajadores": trabajadores}


class FakeWhatsApp(WhatsAppNotifier):
    """Records messages instead of calling an API."""

    canal = "whatsapp:fake"

    def __init__(self, fallar: bool = False):
        self.enviados: list[tuple[str, str]] = []
        self.fallar = fallar

    def send(self, telefono: str, mensaje: str) -> ResultadoEnvio:
        self.enviados.append((telefono, mensaje))
        if self.fallar:
            return ResultadoEnvio(False, self.canal, "simulado")
        return ResultadoEnvio(True, self.canal)


class FakeEmail:
    """Stands in for EmailClient: serves canned messages, records what is sent."""

    def __init__(self, entrantes=None, usuario="workorder@jbrenovate.com"):
        self.entrantes = list(entrantes or [])
        self.enviados: list[dict] = []
        self.marcados: list[str] = []
        self.usuario = usuario

    def fetch_unread(self):
        correos, self.entrantes = self.entrantes, []
        return correos

    def mark_seen(self, uids):
        self.marcados.extend(uids)

    def send(self, destinatario, asunto, cuerpo, responder_a="", adjuntos=None):
        self.enviados.append({"para": destinatario, "asunto": asunto, "cuerpo": cuerpo,
                              "adjuntos": list(adjuntos or [])})
        return True


def build_config(tmp_path, datos) -> dict:
    return {
        "general": {"empresa": "JB Renovate", "contacto": "wo@jb.com",
                    "intervalo_segundos": 1, "codigo_pais": "+1",
                    "horario_envios": {"activo": False}},
        "rutas": {
            "inventario": str(datos["inventario"]),
            "trabajadores": str(datos["trabajadores"]),
            "estado": str(tmp_path / "state.json"),
            "whatsapp_inbox": str(tmp_path / "inbox.jsonl"),
        },
        "email": {"imap_host": "imap.test", "smtp_host": "smtp.test",
                  "usuario": "workorder@jbrenovate.com", "password": "x"},
        "whatsapp": {"proveedor": "manual",
                     "manual": {"archivo_pendientes": str(tmp_path / "pendientes.txt")}},
        "recepcion": {"acusar_recibo": True, "avisar_asignacion": True},
        "asistencia": {"recordatorio_minutos": 60, "expiracion_horas": 4,
                       "margen_convocatoria": 0},
        "despacho": {"reservar_inventario": True},
        "alertas": {},
        "mensajes": {},
    }


@pytest.fixture
def runner(tmp_path, datos):
    """Runner wired to fake email + fake WhatsApp."""
    runner = WorkflowRunner(build_config(tmp_path, datos))
    runner.email = FakeEmail()
    runner.whatsapp = FakeWhatsApp()
    runner.notifier = Notifier(runner.whatsapp, email_client=runner.email)
    return runner


def correo(cuerpo=CORREO_CLIENTE, asunto="Solicitud de pintura",
           remitente="ing.vega@constructoravega.com", uid="1", adjuntos=None):
    return CorreoEntrante(
        uid=uid, message_id=f"<{uid}@test>", remitente_nombre="Ing. Vega",
        remitente_email=remitente, asunto=asunto, cuerpo=cuerpo,
        adjuntos=list(adjuntos or []),
    )


# --- parser -----------------------------------------------------------------

@pytest.mark.parametrize("linea,descripcion,cantidad,unidad", [
    ("3 x Pintura blanca", "Pintura blanca", 3, "u"),
    ("2 cubetas Pintura blanca", "Pintura blanca", 2, "cubetas"),
    ("- 4 Tornillos M8", "Tornillos M8", 4, "u"),
    ("Cinta azul x 6", "Cinta azul", 6, "u"),
    ("Sellador: 2 galones", "Sellador", 2, "galones"),
    ("1,5 kg Cemento", "Cemento", 1.5, "kg"),
    ("Rodillo 9 pulgadas", "Rodillo 9 pulgadas", 1, "u"),
])
def test_parse_quantity_line(linea, descripcion, cantidad, unidad):
    item = parse_quantity_line(linea)
    assert item is not None
    assert item.descripcion == descripcion
    assert item.cantidad == cantidad
    assert item.unidad == unidad


def test_parse_quantity_line_ignora_prosa():
    assert parse_quantity_line("Por favor confirmen si pueden ir el martes.") is None
    assert parse_quantity_line("") is None


def test_parse_quantity_line_con_sku():
    item = parse_quantity_line("PIN-123 x 4")
    assert item.sku == "PIN-123"
    assert item.cantidad == 4


def test_parser_extrae_todos_los_campos():
    campos = EmailJobParser().parse("Solicitud", CORREO_CLIENTE,
                                    remitente_email="ing.vega@constructoravega.com")
    assert campos["cliente_nombre"] == "Constructora Vega"
    assert campos["direccion"] == "Av. Reforma 123, local 4"
    assert campos["zona"] == "norte"
    assert campos["fecha_servicio"] == "2026-09-15"
    assert campos["hora_servicio"] == "08:00"
    assert campos["habilidades"] == ["pintura"]
    assert campos["trabajadores_requeridos"] == 2
    assert len(campos["items"]) == 3
    assert "pinten la oficina" in campos["descripcion"]
    # The sign-off must not leak into the description.
    assert "Gracias" not in campos["descripcion"]


def test_parser_texto_libre_sin_etiquetas():
    cuerpo = ("Hola, necesito reparar el drywall del pasillo.\n"
              "- 3 Panel de drywall\n- 2 Compuesto para juntas\n")
    campos = EmailJobParser().parse("Reparacion", cuerpo, remitente_email="x@y.com")
    assert campos["trabajadores_requeridos"] == 1
    assert len(campos["items"]) == 2
    assert "drywall del pasillo" in campos["descripcion"]


def test_parser_etiquetas_extra_configurables():
    parser = EmailJobParser({"etiquetas_extra": {"direccion": ["job site"]}})
    campos = parser.parse("x", "Job site: 44 Elm Street\n", remitente_email="a@b.com")
    assert campos["direccion"] == "44 Elm Street"


def test_parser_materiales_en_una_linea():
    campos = EmailJobParser().parse(
        "x", "Materiales: 3 brochas, 2 rodillos y 1 cubeta pintura\n",
        remitente_email="a@b.com")
    assert len(campos["items"]) == 3


@pytest.mark.parametrize("texto,esperado", [
    ("SI", True), ("si", True), ("Sí, ahí estaré", True), ("confirmo", True),
    ("ok", True), ("👍", True), ("Si puedo ir", True),
    ("NO", False), ("no puedo esta vez", False), ("Hoy no", False),
    ("¿a que hora?", None), ("", None),
])
def test_parse_answer(texto, esperado):
    assert parse_answer(texto) is esperado


def test_parse_answer_ignora_texto_citado():
    respuesta = "NO puedo\n\n> El mar 15 sep, JB Renovate escribio:\n> SI o NO"
    assert parse_answer(respuesta) is False


def test_parse_date_relativa_y_formatos():
    hoy = datetime(2026, 9, 15).date()
    assert parse_date("mañana", hoy) == "2026-09-16"
    assert parse_date("15/09/2026") == "2026-09-15"
    assert parse_date("2026-09-15") == "2026-09-15"
    assert parse_date("el proximo martes") == "el proximo martes"  # se conserva el texto


def test_extract_job_id():
    assert extract_job_id("Re: [JB-20260915-003] confirmo") == "JB-20260915-003"
    assert extract_job_id("sin folio") is None


def test_html_to_text():
    assert "Hola" in _html_to_text("<p>Hola</p><script>x=1</script>")
    assert "x=1" not in _html_to_text("<p>Hola</p><script>x=1</script>")


# --- inventory --------------------------------------------------------------

def test_inventory_match_por_nombre_y_sku(datos):
    inventario = Inventory(str(datos["inventario"]))
    assert inventario.match("PIN-BLA-5").sku == "PIN-BLA-5"
    assert inventario.match("pintura blanca 5 galones").sku == "PIN-BLA-5"
    assert inventario.match("Pintura blanca").sku == "PIN-BLA-5"
    assert inventario.match("cemento") is None


def test_inventory_check_marca_faltantes(datos):
    inventario = Inventory(str(datos["inventario"]))
    items = [ItemPedido("Pintura blanca", 2), ItemPedido("Cinta de enmascarar azul", 6),
             ItemPedido("Cemento gris", 1)]
    inventario.check(items)
    assert items[0].faltante == 0
    assert items[1].faltante == 2   # hay 4, piden 6
    assert items[2].faltante == 1   # no existe en el inventario


def test_inventory_reserva_estricta_es_todo_o_nada(datos):
    inventario = Inventory(str(datos["inventario"]))
    items = [ItemPedido("Pintura blanca", 2, sku="PIN-BLA-5"),
             ItemPedido("Cinta de enmascarar azul", 99, sku="CIN-AZU")]
    ok, problemas = inventario.reservar(items, estricto=True)

    assert not ok and problemas
    # Nada se escribio: la pintura sigue sin reservar.
    assert Inventory(str(datos["inventario"])).articulos["PIN-BLA-5"].reservado == 0


def test_inventory_reserva_parcial_toma_lo_que_hay(datos):
    inventario = Inventory(str(datos["inventario"]))
    items = [ItemPedido("Pintura blanca", 2, sku="PIN-BLA-5"),
             ItemPedido("Cinta de enmascarar azul", 6, sku="CIN-AZU")]
    ok, problemas = inventario.reservar(items)

    assert not ok and len(problemas) == 1
    assert items[0].reservado == 2 and items[0].faltante == 0
    assert items[1].reservado == 4 and items[1].faltante == 2   # solo habia 4
    assert inventario.articulos["CIN-AZU"].disponible == 0


def test_inventory_reserva_no_duplica_disponibilidad(datos):
    """Dos lineas del mismo articulo no pueden reservar el mismo stock dos veces."""
    inventario = Inventory(str(datos["inventario"]))
    items = [ItemPedido("Cinta de enmascarar azul", 3, sku="CIN-AZU"),
             ItemPedido("Cinta de enmascarar azul", 3, sku="CIN-AZU")]
    inventario.reservar(items)

    assert inventario.articulos["CIN-AZU"].reservado == 4   # el stock total
    assert items[0].reservado + items[1].reservado == 4


def test_inventory_reserva_y_consume(datos):
    inventario = Inventory(str(datos["inventario"]))
    items = [ItemPedido("Pintura blanca", 2, sku="PIN-BLA-5")]

    assert inventario.reservar(items) == (True, [])
    assert inventario.articulos["PIN-BLA-5"].disponible == 8
    assert Inventory(str(datos["inventario"])).articulos["PIN-BLA-5"].reservado == 2

    inventario.consumir(items)
    assert inventario.articulos["PIN-BLA-5"].stock == 8
    assert inventario.articulos["PIN-BLA-5"].reservado == 0


def test_inventory_consume_solo_lo_reservado(datos):
    """Si solo se reservaron 4 de 6, el stock baja 4, no 6."""
    inventario = Inventory(str(datos["inventario"]))
    items = [ItemPedido("Cinta de enmascarar azul", 6, sku="CIN-AZU")]
    inventario.reservar(items)
    inventario.consumir(items)

    assert inventario.articulos["CIN-AZU"].stock == 0
    assert inventario.articulos["CIN-AZU"].reservado == 0


def test_inventory_bajo_minimo(datos):
    inventario = Inventory(str(datos["inventario"]))
    bajos = {a.sku for a in inventario.bajo_minimo()}
    assert bajos == {"CIN-AZU"}   # 4 disponibles con minimo 10


# --- workers ----------------------------------------------------------------

def test_roster_normaliza_telefonos(datos):
    roster = WorkerRoster(str(datos["trabajadores"]), default_country_code="+1")
    assert roster.get("T01").telefono == "+15551234001"
    assert roster.get("T02").telefono == "+15551234002"
    assert roster.by_phone("555 123 4003").id == "T03"
    assert roster.by_phone("+1 555-123-4001").id == "T01"


def test_roster_omite_inactivos_y_ordena_por_zona(datos):
    roster = WorkerRoster(str(datos["trabajadores"]))
    trabajo = Trabajo(id="JB-1", habilidades=["pintura"], zona="norte")
    elegidos = roster.seleccionar(trabajo, cantidad=5)

    assert [t.id for t in elegidos] == ["T01", "T02"]   # T03 no pinta, T04 inactiva


def test_roster_reparte_por_carga(datos):
    roster = WorkerRoster(str(datos["trabajadores"]))
    trabajo = Trabajo(id="JB-1", habilidades=["pintura"], zona="norte")
    elegidos = roster.seleccionar(trabajo, cantidad=1, carga={"T01": 3})
    assert elegidos[0].id == "T02"


def test_roster_cobertura_parcial_cuando_nadie_cubre_todo(datos):
    roster = WorkerRoster(str(datos["trabajadores"]))
    trabajo = Trabajo(id="JB-1", habilidades=["pintura", "plomeria"])
    elegidos = roster.seleccionar(trabajo, cantidad=3)
    assert {t.id for t in elegidos} == {"T01", "T02"}


# --- attendance -------------------------------------------------------------

def _trabajo_con_convocatoria(minutos_atras=0):
    trabajo = Trabajo(id="JB-20260915-001", trabajadores_requeridos=1)
    enviado = datetime.now(timezone.utc) - timedelta(minutes=minutos_atras)
    trabajo.convocatorias["T01"] = Convocatoria(
        trabajador_id="T01", nombre="Juan", telefono="+1555", email="j@x.com",
        enviado_at=enviado.replace(microsecond=0).isoformat(),
    )
    return trabajo


def test_attendance_confirma_y_cubre_cupo():
    manager = AttendanceManager()
    trabajo = _trabajo_con_convocatoria()

    assert manager.registrar_respuesta(trabajo, "T01", True, canal="whatsapp")
    assert trabajo.convocatorias["T01"].estado == EstadoConvocatoria.CONFIRMADO.value
    assert trabajo.cupo_cubierto()


def test_attendance_no_acepta_dos_respuestas():
    manager = AttendanceManager()
    trabajo = _trabajo_con_convocatoria()

    assert manager.registrar_respuesta(trabajo, "T01", True)
    assert not manager.registrar_respuesta(trabajo, "T01", False)
    assert trabajo.convocatorias["T01"].estado == EstadoConvocatoria.CONFIRMADO.value


def test_attendance_recordatorio_solo_una_vez():
    manager = AttendanceManager({"recordatorio_minutos": 60})
    trabajo = _trabajo_con_convocatoria(minutos_atras=90)

    pendientes = manager.pendientes_por_recordar(trabajo)
    assert [c.trabajador_id for c in pendientes] == ["T01"]

    pendientes[0].recordado_at = utc_now()
    assert manager.pendientes_por_recordar(trabajo) == []


def test_attendance_expira_y_detecta_callejon_sin_salida():
    manager = AttendanceManager({"expiracion_horas": 4})
    trabajo = _trabajo_con_convocatoria(minutos_atras=5 * 60)

    expiradas = manager.expirar(trabajo)
    assert len(expiradas) == 1
    assert manager.sin_salida(trabajo)


def test_attendance_cuantos_convocar_respeta_margen():
    manager = AttendanceManager({"margen_convocatoria": 1})
    trabajo = Trabajo(id="JB-1", trabajadores_requeridos=2)
    assert manager.cuantos_convocar(trabajo) == 3

    trabajo.convocatorias["T01"] = Convocatoria(
        trabajador_id="T01", nombre="Juan",
        estado=EstadoConvocatoria.CONFIRMADO.value)
    assert manager.cuantos_convocar(trabajo) == 2


# --- store ------------------------------------------------------------------

def test_store_persiste_y_recarga(tmp_path):
    ruta = tmp_path / "state.json"
    store = WorkflowStore(str(ruta))
    trabajo = Trabajo(id="JB-20260915-001", cliente_nombre="Vega",
                      items=[ItemPedido("Pintura blanca", 2, sku="PIN-BLA-5")])
    trabajo.convocatorias["T01"] = Convocatoria(trabajador_id="T01", nombre="Juan")
    store.add(trabajo)
    store.marcar_procesado("<abc@test>")
    store.save()

    recargado = WorkflowStore(str(ruta))
    assert recargado.get("JB-20260915-001").cliente_nombre == "Vega"
    assert recargado.get("JB-20260915-001").items[0].sku == "PIN-BLA-5"
    assert recargado.get("JB-20260915-001").convocatorias["T01"].nombre == "Juan"
    assert recargado.ya_procesado("<abc@test>")


def test_store_folios_consecutivos(tmp_path):
    store = WorkflowStore(str(tmp_path / "state.json"))
    primero = store.next_job_id("20260915")
    assert primero == "JB-20260915-001"

    store.add(Trabajo(id=primero))
    assert store.next_job_id("20260915") == "JB-20260915-002"
    assert store.next_job_id("20260916") == "JB-20260916-001"


# --- inbox / webhook --------------------------------------------------------

def test_inbox_consume_desde_offset(tmp_path):
    inbox = WhatsAppInbox(str(tmp_path / "inbox.jsonl"))
    inbox.append("+1555", "SI")
    registros, offset = inbox.consume(0)
    assert len(registros) == 1 and registros[0]["texto"] == "SI"

    inbox.append("+1556", "NO")
    nuevos, _ = inbox.consume(offset)
    assert [r["texto"] for r in nuevos] == ["NO"]


def test_inbox_relee_si_el_archivo_se_trunca(tmp_path):
    inbox = WhatsAppInbox(str(tmp_path / "inbox.jsonl"))
    inbox.append("+1555", "SI")
    registros, _ = inbox.consume(9999)
    assert len(registros) == 1


def test_webhook_parsea_twilio_y_meta():
    twilio = parse_payload("From=whatsapp%3A%2B15551234001&Body=SI", "")
    assert twilio == [("+15551234001", "SI", "twilio")]

    payload = json.dumps({"entry": [{"changes": [{"value": {"messages": [
        {"from": "15551234001", "type": "text", "text": {"body": "NO"}}
    ]}}]}]})
    assert parse_payload(payload, "application/json") == [("+15551234001", "NO", "meta")]


# --- messages ---------------------------------------------------------------

def test_message_builder_rellena_contexto():
    builder = MessageBuilder(empresa="JB Renovate", contacto="wo@jb.com")
    trabajo = Trabajo(id="JB-1", cliente_nombre="Vega", direccion="Reforma 123",
                      fecha_servicio="2026-09-15", hora_servicio="08:00",
                      items=[ItemPedido("Pintura blanca", 2, unidad="cubeta")])
    mensaje = builder.convocatoria(trabajo, "Juan")

    assert "Juan" in mensaje and "JB-1" in mensaje
    assert "2 cubeta - Pintura blanca" in mensaje
    assert "SI o NO" in mensaje


def test_message_builder_no_revienta_con_llave_desconocida():
    builder = MessageBuilder({"convocatoria": "Hola {inexistente}"})
    assert builder.render("convocatoria", trabajador="Juan") == "Hola {inexistente}"


# --- end to end -------------------------------------------------------------

def test_ciclo_completo_correo_a_orden_de_trabajo(runner, datos):
    """Client email -> job -> attendance requests -> confirmations -> work order."""
    runner.email.entrantes = [correo()]

    resumen = runner.run_once()
    assert resumen["trabajos_nuevos"] == 1
    assert resumen["convocatorias_enviadas"] == 2   # el correo pide 2 trabajadores

    trabajo = next(iter(runner.store.trabajos.values()))
    assert trabajo.estado == EstadoTrabajo.CONVOCANDO.value
    assert set(trabajo.convocatorias) == {"T01", "T02"}
    assert runner.email.marcados == ["1"]

    # El cliente recibio acuse de recibo.
    assert any("Recibimos su solicitud" in e["asunto"] for e in runner.email.enviados)
    # Cinta: piden 6 y solo hay 4 -> se detecta el faltante.
    assert [i.faltante for i in trabajo.items if i.sku == "CIN-AZU"] == [2]

    # Los dos trabajadores confirman por WhatsApp.
    runner.inbox.append("+15551234001", f"SI {trabajo.id}")
    runner.inbox.append("+15551234002", "si, ahi estare")
    resumen = runner.run_once()

    assert resumen["respuestas"] == 2
    assert resumen["trabajos_asignados"] == 1
    trabajo = runner.store.get(trabajo.id)
    assert trabajo.estado == EstadoTrabajo.ASIGNADO.value
    assert trabajo.inventario_reservado

    # Cada trabajador recibio la orden de trabajo con el material.
    ordenes = [m for _, m in runner.whatsapp.enviados if "Orden de trabajo" in m]
    assert len(ordenes) == 2
    assert "Pintura blanca" in ordenes[0]
    assert "Av. Reforma 123" in ordenes[0]

    # Y el material quedo reservado en el CSV.
    assert Inventory(str(datos["inventario"])).articulos["PIN-BLA-5"].reservado == 2


def test_ciclo_no_reprocesa_el_mismo_correo(runner):
    runner.email.entrantes = [correo()]
    runner.run_once()
    runner.email.entrantes = [correo()]   # el mismo Message-ID otra vez
    resumen = runner.run_once()

    assert resumen["trabajos_nuevos"] == 0
    assert len(runner.store.trabajos) == 1


def test_rechazo_convoca_a_otro_trabajador(runner):
    cuerpo = CORREO_CLIENTE.replace("Trabajadores: 2", "Trabajadores: 1")
    runner.email.entrantes = [correo(cuerpo=cuerpo)]
    runner.run_once()

    trabajo = next(iter(runner.store.trabajos.values()))
    assert set(trabajo.convocatorias) == {"T01"}

    runner.inbox.append("+15551234001", "no puedo")
    runner.run_once()

    trabajo = runner.store.get(trabajo.id)
    assert trabajo.convocatorias["T01"].estado == EstadoConvocatoria.RECHAZADO.value
    assert "T02" in trabajo.convocatorias   # se convoco al siguiente
    assert trabajo.estado == EstadoTrabajo.CONVOCANDO.value


def test_respuesta_de_trabajador_por_correo(runner):
    cuerpo = CORREO_CLIENTE.replace("Trabajadores: 2", "Trabajadores: 1")
    runner.email.entrantes = [correo(cuerpo=cuerpo)]
    runner.run_once()
    trabajo = next(iter(runner.store.trabajos.values()))

    runner.email.entrantes = [correo(
        cuerpo="SI, confirmo", asunto=f"Re: [{trabajo.id}] Puedes tomar este trabajo?",
        remitente="juan@ejemplo.com", uid="2",
    )]
    resumen = runner.run_once()

    assert resumen["respuestas"] == 1
    assert runner.store.get(trabajo.id).estado == EstadoTrabajo.ASIGNADO.value


def test_respuesta_ambigua_pide_aclaracion_una_sola_vez(runner):
    runner.email.entrantes = [correo()]
    runner.run_once()
    trabajo = next(iter(runner.store.trabajos.values()))

    runner.inbox.append("+15551234001", "a que hora exactamente?")
    runner.run_once()
    aclaraciones = [m for _, m in runner.whatsapp.enviados if "responde solamente SI o NO" in m]
    assert len(aclaraciones) == 1

    runner.inbox.append("+15551234001", "y donde es?")
    runner.run_once()
    aclaraciones = [m for _, m in runner.whatsapp.enviados if "responde solamente SI o NO" in m]
    assert len(aclaraciones) == 1
    assert runner.store.get(trabajo.id).convocatorias["T01"].estado == \
        EstadoConvocatoria.PENDIENTE.value


def test_sin_personal_disponible_marca_el_trabajo(runner):
    cuerpo = CORREO_CLIENTE.replace("Habilidades: pintura", "Habilidades: soldadura")
    runner.email.entrantes = [correo(cuerpo=cuerpo)]
    resumen = runner.run_once()

    trabajo = next(iter(runner.store.trabajos.values()))
    assert resumen["convocatorias_enviadas"] == 0
    assert trabajo.estado == EstadoTrabajo.SIN_PERSONAL.value
    assert resumen["trabajos_sin_personal"] == 1


def test_convocatoria_fallida_libera_al_trabajador(tmp_path, datos):
    """If WhatsApp and email both fail, the worker is freed for the next round."""
    runner = WorkflowRunner(build_config(tmp_path, datos))
    runner.email = FakeEmail(entrantes=[correo()])
    runner.email.send = lambda *a, **k: False
    runner.notifier = Notifier(FakeWhatsApp(fallar=True), email_client=runner.email)

    runner.run_once()
    trabajo = next(iter(runner.store.trabajos.values()))
    assert trabajo.convocatorias == {}
    assert trabajo.estado == EstadoTrabajo.SIN_PERSONAL.value


def test_correo_de_remitente_no_autorizado_se_ignora(tmp_path, datos):
    config = build_config(tmp_path, datos)
    config["recepcion"]["remitentes_permitidos"] = ["constructoravega.com"]
    runner = WorkflowRunner(config)
    runner.email = FakeEmail(entrantes=[correo(remitente="spam@otrodominio.com")])
    runner.notifier = Notifier(FakeWhatsApp(), email_client=runner.email)

    resumen = runner.run_once()
    assert resumen["trabajos_nuevos"] == 0


def test_correo_automatico_se_ignora(runner):
    runner.email.entrantes = [correo(remitente="noreply@facturacion.com")]
    assert runner.run_once()["trabajos_nuevos"] == 0


def test_orden_no_entregada_genera_alerta(tmp_path, datos, caplog):
    """Si la orden de trabajo no llega, tiene que quedar registrado, no solo en el log."""
    config = build_config(tmp_path, datos)
    config["recepcion"]["remitentes_permitidos"] = []
    runner = WorkflowRunner(config)
    runner.email = FakeEmail(entrantes=[correo(
        cuerpo=CORREO_CLIENTE.replace("Trabajadores: 2", "Trabajadores: 1"))])
    runner.notifier = Notifier(FakeWhatsApp(), email_client=runner.email)
    runner.run_once()

    trabajo = next(iter(runner.store.trabajos.values()))
    # A partir de aqui todos los envios fallan.
    runner.notifier = Notifier(FakeWhatsApp(fallar=True), email_client=runner.email)
    runner.email.send = lambda *a, **k: False
    runner.inbox.append("+15551234001", f"SI {trabajo.id}")
    runner.run_once()

    trabajo = runner.store.get(trabajo.id)
    assert trabajo.estado == EstadoTrabajo.ASIGNADO.value
    assert any("No se entregó la orden a: Juan Perez" in n for n in trabajo.notas)


def test_completar_descuenta_una_sola_vez(tmp_path, datos):
    inventario = Inventory(str(datos["inventario"]))
    items = [ItemPedido("Pintura blanca", 2, sku="PIN-BLA-5")]
    inventario.reservar(items)
    inventario.consumir(items)
    inventario.consumir(items)   # segunda llamada: ya no hay nada reservado

    assert inventario.articulos["PIN-BLA-5"].stock == 8


# --- formulario web reenviado -----------------------------------------------

# Asi llega una solicitud del formulario "Drop us a line!" de jbrenovate.com:
# reenviada por GoDaddy, con el cliente real dentro del cuerpo.
FORMULARIO_WEB = """\
You have a new message from your website contact form.

Name: Sarah Miller
Email: sarah.miller@gmail.com
Phone: (555) 987-6543
Message: Hi, I need the hallway drywall patched and the whole second floor
repainted. Looking to get this done the week of 9/15.

Address: 44 Elm Street, Apt 3
Date: 15/09/2026

Materials:
- 3 Panel de drywall 1/2
- 2 cubetas Pintura blanca 5 galones

Thanks,
Sarah
"""


def test_formulario_web_saca_al_cliente_del_cuerpo():
    """El remitente es GoDaddy; el cliente de verdad va dentro del mensaje."""
    campos = EmailJobParser().parse("New form submission", FORMULARIO_WEB,
                                    remitente_email="noreply@godaddy.com")

    assert campos["cliente_nombre"] == "Sarah Miller"
    assert campos["cliente_email"] == "sarah.miller@gmail.com"
    assert campos["cliente_telefono"] == "(555) 987-6543"
    assert campos["direccion"] == "44 Elm Street, Apt 3"
    assert campos["fecha_servicio"] == "2026-09-15"
    assert "drywall patched" in campos["descripcion"]
    assert "Thanks" not in campos["descripcion"]   # la despedida en ingles se corta
    assert len(campos["items"]) == 2


def test_reenviador_no_se_filtra_como_noreply(tmp_path, datos):
    """Sin esto, cada solicitud del sitio web se perderia por venir de un noreply."""
    config = build_config(tmp_path, datos)
    config["recepcion"]["reenviadores"] = ["@godaddy.com"]
    runner = WorkflowRunner(config)
    runner.email = FakeEmail(entrantes=[correo(cuerpo=FORMULARIO_WEB,
                                               remitente="noreply@godaddy.com")])
    runner.notifier = Notifier(FakeWhatsApp(), email_client=runner.email)

    assert runner.run_once()["trabajos_nuevos"] == 1
    trabajo = next(iter(runner.store.trabajos.values()))
    assert trabajo.cliente_email == "sarah.miller@gmail.com"
    # El acuse de recibo va al cliente, no a GoDaddy.
    acuses = [e for e in runner.email.enviados if "Recibimos su solicitud" in e["asunto"]]
    assert acuses and acuses[0]["para"] == "sarah.miller@gmail.com"


def test_sin_reenviadores_configurados_el_noreply_se_ignora(runner):
    """El filtro de automaticos sigue vivo para quien no esta declarado."""
    runner.email.entrantes = [correo(cuerpo=FORMULARIO_WEB,
                                     remitente="noreply@godaddy.com")]
    assert runner.run_once()["trabajos_nuevos"] == 0


# --- adjuntos ---------------------------------------------------------------

def test_fotos_del_cliente_se_guardan_y_viajan_con_la_orden(tmp_path, datos):
    config = build_config(tmp_path, datos)
    config["recepcion"]["carpeta_adjuntos"] = str(tmp_path / "adjuntos")
    runner = WorkflowRunner(config)
    runner.email = FakeEmail(entrantes=[correo(
        cuerpo=CORREO_CLIENTE.replace("Trabajadores: 2", "Trabajadores: 1"),
        adjuntos=[Adjunto("pasillo.jpg", b"\xff\xd8foto", "image/jpeg"),
                  Adjunto("../../escape.png", b"png", "image/png")],
    )])
    runner.notifier = Notifier(FakeWhatsApp(), email_client=runner.email)
    runner.run_once()

    trabajo = next(iter(runner.store.trabajos.values()))
    assert len(trabajo.adjuntos) == 2
    assert Path(trabajo.adjuntos[0]).read_bytes() == b"\xff\xd8foto"
    # El nombre malicioso queda contenido dentro de la carpeta del folio.
    assert Path(trabajo.adjuntos[1]).parent.name == trabajo.id
    assert ".." not in Path(trabajo.adjuntos[1]).name

    runner.inbox.append("+15551234001", "SI")
    runner.run_once()

    ordenes = [e for e in runner.email.enviados if "Orden de trabajo" in e["asunto"]]
    assert len(ordenes) == 1
    assert ordenes[0]["adjuntos"] == trabajo.adjuntos
    assert "2 fotos" in ordenes[0]["cuerpo"]


@pytest.mark.parametrize("entrada,esperado", [
    ("../../etc/passwd", "passwd"),   # Path.name descarta la ruta entera
    ("foto vestibulo.jpg", "foto vestibulo.jpg"),
    ("/absoluto/x.png", "x.png"),
    ("", ""),
])
def test_nombre_seguro(entrada, esperado):
    assert _nombre_seguro(entrada) == esperado


# --- ventana de envio -------------------------------------------------------

def _cuando(dia_mes, hora):
    """Septiembre 2026: el 14 es lunes y el 19 es sabado."""
    return datetime(2026, 9, dia_mes, hora, 0, tzinfo=timezone.utc)


def test_ventana_respeta_horario_y_dias():
    ventana = VentanaDeEnvio({"inicio": "08:00", "fin": "19:00", "dias": [1, 2, 3, 4, 5],
                              "zona_horaria": "UTC"})
    assert ventana.abierta(_cuando(14, 10))     # lunes 10:00
    assert not ventana.abierta(_cuando(14, 3))  # lunes 03:00
    assert not ventana.abierta(_cuando(14, 22))
    assert not ventana.abierta(_cuando(19, 10))  # sabado


def test_ventana_desactivada_siempre_abierta():
    assert VentanaDeEnvio({"activo": False}).abierta(_cuando(19, 3))


def test_ventana_proxima_apertura_salta_el_fin_de_semana():
    ventana = VentanaDeEnvio({"inicio": "08:00", "fin": "19:00", "dias": [1, 2, 3, 4, 5],
                              "zona_horaria": "UTC"})
    proxima = ventana.proxima_apertura(_cuando(19, 10))   # sabado
    assert proxima.isoweekday() == 1 and proxima.hour == 8


def test_ventana_hora_invalida_no_revienta():
    ventana = VentanaDeEnvio({"inicio": "ocho de la manana", "zona_horaria": "UTC"})
    assert ventana.inicio.hour == 8


def test_de_noche_no_se_convoca_pero_tampoco_se_da_por_perdido(tmp_path, datos):
    """A las 3am no se molesta a nadie, y el trabajo espera a la manana."""
    config = build_config(tmp_path, datos)
    config["general"]["horario_envios"] = {"activo": True, "inicio": "08:00",
                                           "fin": "19:00", "dias": [1, 2, 3, 4, 5],
                                           "zona_horaria": "UTC"}
    runner = WorkflowRunner(config)
    runner.email = FakeEmail(entrantes=[correo()])
    whatsapp = FakeWhatsApp()
    runner.notifier = Notifier(whatsapp, email_client=runner.email)
    runner.horario.abierta = lambda ahora=None: False

    resumen = runner.run_once()
    trabajo = next(iter(runner.store.trabajos.values()))

    assert resumen["convocatorias_enviadas"] == 0
    assert whatsapp.enviados == []
    assert trabajo.convocatorias == {}
    assert trabajo.estado == EstadoTrabajo.NUEVO.value        # no "sin_personal"
    assert resumen["trabajos_sin_personal"] == 0

    # Al abrir la ventana, el mismo trabajo se convoca normalmente.
    runner.horario.abierta = lambda ahora=None: True
    assert runner.run_once()["convocatorias_enviadas"] == 2


def test_orden_de_trabajo_sale_aunque_este_fuera_de_horario(tmp_path, datos):
    """Quien ya confirmo espera los datos; eso no se retiene."""
    config = build_config(tmp_path, datos)
    runner = WorkflowRunner(config)
    runner.email = FakeEmail(entrantes=[correo(
        cuerpo=CORREO_CLIENTE.replace("Trabajadores: 2", "Trabajadores: 1"))])
    whatsapp = FakeWhatsApp()
    runner.notifier = Notifier(whatsapp, email_client=runner.email)
    runner.run_once()

    trabajo = next(iter(runner.store.trabajos.values()))
    runner.horario.abierta = lambda ahora=None: False   # cae la noche
    runner.inbox.append("+15551234001", "SI")
    runner.run_once()

    assert runner.store.get(trabajo.id).estado == EstadoTrabajo.ASIGNADO.value
    assert any("Orden de trabajo" in m for _, m in whatsapp.enviados)
