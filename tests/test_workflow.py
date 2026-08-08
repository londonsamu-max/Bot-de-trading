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
                       "margen_convocatoria": 0, "max_trabajos_por_dia": 2},
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


# =============================================================================
# Correos de empresas de gestión: un renglón = una unidad = un trabajo
# =============================================================================

from src.workflow import export
from src.workflow.catalogs import PropertyDirectory, ServiceMaterials, UnitSizes
from src.workflow.property_parser import PropertyEmailParser, fecha_iso_a_corta

# Correo real de Park Mesa Villas (Shea Properties), con su firma completa.
CORREO_GESTION = """\
B109 vacant,  full tub. 8/8/26
M102 Vacant, full tub. 8/8/26
M104 Vacant, full tub. 8/8/26
J209 vacant, full tub. 8/10/26 AM please


Best Regards,

Guillermo Gonzalez
Service Manager
Park Mesa Villas Your Lifestyle Your Location
550 Paularino Avenue, Costa Mesa, CA 92626
P. 714-751-6995 |   F. 714-751-5118
Virtual Tour: https://www.parkmesavillas.com/3dtours.aspx
www.parkmesavillas.com

pmvmaint@rentparkmesa.com |
"""

PROPIEDADES_CSV = """propiedad,empresa_gestion,dominio,contacto,direccion,zona,alias
Park Mesa Villas,Shea Properties,rentparkmesa.com,pmvmaint@rentparkmesa.com,550 Paularino Ave Costa Mesa CA,costa mesa,PMV
Reata,Shea Properties,,,,irvine,
"""

UNIDADES_CSV = """propiedad,unidad,tamano
Park Mesa Villas,B109,1+1
Park Mesa Villas,M102,2+1
Park Mesa Villas,J209,2+2
"""

CONSUMOS_CSV = """servicio,tamano,sku,cantidad
tub,*,KIT-TINA,1
tub,*,CIN-AZU,1
paint,*,BRO-4,2
paint,1+1,PIN-BLA-5,2
paint,3+2,PIN-BLA-5,4
"""

INVENTARIO_GESTION_CSV = """sku,nombre,unidad,stock,reservado,minimo
KIT-TINA,Kit de reacabado de tina,kit,6,0,2
CIN-AZU,Cinta de enmascarar azul,rollo,40,0,10
PIN-BLA-5,Pintura blanca 5 galones,cubeta,10,0,3
BRO-4,Brocha 4 pulgadas,pza,20,0,6
"""

TRABAJADORES_GESTION_CSV = """id,nombre,telefono,email,habilidades,zona,activo
T01,Santos,+15551234001,santos@ejemplo.com,pintura|tinas,costa mesa,si
T02,Marcos,+15551234002,marcos@ejemplo.com,tinas,irvine,si
T03,Adrian,+15551234003,adrian@ejemplo.com,pintura,costa mesa,si
"""


@pytest.fixture
def gestion(tmp_path):
    """Catálogos completos de una empresa de gestión."""
    archivos = {
        "propiedades": (tmp_path / "propiedades.csv", PROPIEDADES_CSV),
        "unidades": (tmp_path / "unidades.csv", UNIDADES_CSV),
        "consumos": (tmp_path / "consumos.csv", CONSUMOS_CSV),
        "inventario": (tmp_path / "inventario.csv", INVENTARIO_GESTION_CSV),
        "trabajadores": (tmp_path / "trabajadores.csv", TRABAJADORES_GESTION_CSV),
    }
    for ruta, contenido in archivos.values():
        ruta.write_text(contenido, encoding="utf-8")
    return {clave: ruta for clave, (ruta, _) in archivos.items()}


def build_config_gestion(tmp_path, gestion) -> dict:
    config = build_config(tmp_path, {"inventario": gestion["inventario"],
                                     "trabajadores": gestion["trabajadores"]})
    config["rutas"].update({
        "propiedades": str(gestion["propiedades"]),
        "unidades": str(gestion["unidades"]),
        "consumos": str(gestion["consumos"]),
    })
    config["servicios"] = {"habilidades": {"tub": ["tinas"], "paint": ["pintura"],
                                           "jani": ["limpieza"], "carpet": ["alfombra"]}}
    return config


@pytest.fixture
def runner_gestion(tmp_path, gestion):
    runner = WorkflowRunner(build_config_gestion(tmp_path, gestion))
    runner.email = FakeEmail()
    runner.whatsapp = FakeWhatsApp()
    runner.notifier = Notifier(runner.whatsapp, email_client=runner.email)
    return runner


def correo_gestion(cuerpo=CORREO_GESTION, uid="1"):
    return correo(cuerpo=cuerpo, asunto="Turnovers",
                  remitente="pmvmaint@rentparkmesa.com", uid=uid)


# --- parser de unidades -----------------------------------------------------

def test_parser_extrae_una_unidad_por_renglon():
    unidades = PropertyEmailParser().parse(CORREO_GESTION)

    assert [u.unidad for u in unidades] == ["B109", "M102", "M104", "J209"]
    assert all(u.servicio == "tub" for u in unidades)
    assert all(not u.ocupada for u in unidades)
    assert [u.descripcion for u in unidades[:3]] == ["full tub"] * 3
    # El turno se queda en la descripcion, igual que en la hoja ("jani am", "cc pm"),
    # y ademas se expone aparte en el campo turno.
    assert unidades[3].descripcion == "full tub AM"
    assert unidades[3].turno == "AM"


def test_parser_lee_fechas_en_formato_estadounidense():
    unidades = PropertyEmailParser().parse(CORREO_GESTION)
    # 8/10/26 es el 10 de agosto, no el 8 de octubre.
    assert [u.fecha for u in unidades] == ["2026-08-08", "2026-08-08",
                                           "2026-08-08", "2026-08-10"]
    assert unidades[3].turno == "AM"
    assert unidades[0].turno == ""


def test_parser_formato_europeo_configurable():
    unidades = PropertyEmailParser({"formato_fecha": "EU"}).parse("210 paint 3/4/26")
    assert unidades[0].fecha == "2026-04-03"


def test_parser_ignora_la_firma_y_la_direccion():
    """La firma trae numeros y direcciones; nada de eso puede volverse trabajo."""
    unidades = PropertyEmailParser().parse(CORREO_GESTION)
    unidades_texto = [u.unidad for u in unidades]

    assert "550" not in unidades_texto        # 550 Paularino Avenue
    assert "714-751-6995" not in unidades_texto
    assert len(unidades) == 4


@pytest.mark.parametrize("linea,servicio,descripcion", [
    ("210 full paint 7/6/26", "paint", "full paint"),
    ("339 cc pm 7/6/26", "carpet", "cc pm"),
    ("374 jani am", "jani", "jani am"),
    ("1-rr full paint + ceilings 7/11/26", "paint", "full paint + ceilings"),
    ("Q-106 jani am 7/9/26", "jani", "jani am"),
    ("90-seascape full paint", "paint", "full paint"),
])
def test_parser_reconoce_los_servicios_de_la_hoja(linea, servicio, descripcion):
    unidades = PropertyEmailParser().parse(linea)
    assert len(unidades) == 1
    assert unidades[0].servicio == servicio
    assert unidades[0].descripcion == descripcion


def test_parser_occ_no_se_confunde_con_carpet():
    """'occ' es ocupada; 'cc' es limpieza de alfombra. No pueden mezclarse."""
    unidades = PropertyEmailParser().parse("90-es occ cc wear shoe covers pm 7/6/26")

    assert len(unidades) == 1
    assert unidades[0].servicio == "carpet"
    assert unidades[0].ocupada is True
    assert "wear shoe covers" in unidades[0].descripcion


def test_parser_un_renglon_con_dos_servicios_son_dos_trabajos():
    unidades = PropertyEmailParser().parse("210 vacant, full paint and jani 8/8/26")
    assert sorted(u.servicio for u in unidades) == ["jani", "paint"]
    assert all(u.unidad == "210" for u in unidades)


def test_parser_toma_el_tamano_del_renglon_si_viene():
    unidades = PropertyEmailParser().parse("1-rr 3+2 full paint 7/11/26")
    assert unidades[0].tamano == "3+2"


def test_parser_fecha_invalida_no_revienta():
    unidades = PropertyEmailParser().parse("B109 vacant, full tub. 13/45/26")
    assert len(unidades) == 1
    assert unidades[0].fecha == "13/45/26"   # se conserva el texto original


def test_fecha_iso_a_corta():
    assert fecha_iso_a_corta("2026-08-08") == "8/8/26"
    assert fecha_iso_a_corta("") == ""
    assert fecha_iso_a_corta("mañana") == "mañana"


# --- catálogos --------------------------------------------------------------

def test_directorio_identifica_la_propiedad_por_remitente(gestion):
    directorio = PropertyDirectory(str(gestion["propiedades"]))

    por_contacto = directorio.match("pmvmaint@rentparkmesa.com")
    assert por_contacto.propiedad == "Park Mesa Villas"
    assert por_contacto.empresa_gestion == "Shea Properties"

    por_dominio = directorio.match("otra.persona@rentparkmesa.com")
    assert por_dominio.propiedad == "Park Mesa Villas"

    assert directorio.conocido("pmvmaint@rentparkmesa.com")
    assert not directorio.conocido("cliente@gmail.com")


def test_directorio_identifica_la_propiedad_por_el_texto(gestion):
    """Misma empresa escribiendo desde otro buzón: el nombre va en la firma."""
    directorio = PropertyDirectory(str(gestion["propiedades"]))
    encontrada = directorio.match("nuevo@gmail.com", cuerpo="...\nReata\nIrvine CA")
    assert encontrada.propiedad == "Reata"


def test_tamanos_de_unidad(gestion):
    tamanos = UnitSizes(str(gestion["unidades"]))
    assert tamanos.get("Park Mesa Villas", "B109") == "1+1"
    assert tamanos.get("Park Mesa Villas", "b109") == "1+1"   # sin importar mayúsculas
    assert tamanos.get("Park Mesa Villas", "M104") == ""      # no está en la tabla


def test_material_por_servicio_y_tamano(gestion):
    materiales = ServiceMaterials(str(gestion["consumos"]))

    tub = {i.sku: i.cantidad for i in materiales.para("tub", "1+1")}
    assert tub == {"KIT-TINA": 1, "CIN-AZU": 1}

    # La regla del tamaño manda sobre la regla "*".
    paint_1 = {i.sku: i.cantidad for i in materiales.para("paint", "1+1")}
    assert paint_1 == {"BRO-4": 2, "PIN-BLA-5": 2}
    paint_3 = {i.sku: i.cantidad for i in materiales.para("paint", "3+2")}
    assert paint_3["PIN-BLA-5"] == 4
    # Un tamaño sin regla propia se queda solo con lo común.
    assert {i.sku for i in materiales.para("paint", "2+1")} == {"BRO-4"}
    assert materiales.para("desconocido", "1+1") == []


# --- ciclo completo con correo de gestión -----------------------------------

def test_un_correo_de_gestion_genera_un_trabajo_por_unidad(runner_gestion):
    runner_gestion.email.entrantes = [correo_gestion()]
    resumen = runner_gestion.run_once()

    assert resumen["trabajos_nuevos"] == 4
    trabajos = sorted(runner_gestion.store.trabajos.values(), key=lambda t: t.unidad)

    assert [t.unidad for t in trabajos] == ["B109", "J209", "M102", "M104"]
    for trabajo in trabajos:
        assert trabajo.empresa_gestion == "Shea Properties"
        assert trabajo.propiedad == "Park Mesa Villas"
        assert trabajo.servicio == "tub"
        assert trabajo.descripcion_servicio.startswith("full tub")
        assert trabajo.zona == "costa mesa"
        assert trabajo.habilidades == ["tinas"]

    por_unidad = {t.unidad: t for t in trabajos}
    assert por_unidad["B109"].tamano == "1+1"        # de data/unidades.csv
    assert por_unidad["M104"].tamano == ""           # no está en la tabla
    assert por_unidad["J209"].fecha_servicio == "2026-08-10"
    assert por_unidad["J209"].turno == "AM"

    # Material asignado solo desde consumos.csv, porque el correo no lo dice.
    assert {i.sku for i in por_unidad["B109"].items} == {"KIT-TINA", "CIN-AZU"}


def test_acuse_al_gestor_lista_las_cuatro_unidades(runner_gestion):
    runner_gestion.email.entrantes = [correo_gestion()]
    runner_gestion.run_once()

    acuses = [e for e in runner_gestion.email.enviados if "Recibido" in e["asunto"]]
    assert len(acuses) == 1                       # uno solo, no cuatro
    assert acuses[0]["para"] == "pmvmaint@rentparkmesa.com"
    for unidad in ("B109", "M102", "M104", "J209"):
        assert unidad in acuses[0]["cuerpo"]


def test_reenvio_de_la_misma_lista_no_duplica_trabajos(runner_gestion):
    runner_gestion.email.entrantes = [correo_gestion()]
    runner_gestion.run_once()

    # La empresa reenvía la lista desde otro correo (otro Message-ID).
    runner_gestion.email.entrantes = [correo_gestion(uid="2")]
    resumen = runner_gestion.run_once()

    assert resumen["trabajos_nuevos"] == 0
    assert len(runner_gestion.store.trabajos) == 4
    trabajo = next(iter(runner_gestion.store.trabajos.values()))
    assert any("reenvió esta unidad" in n for n in trabajo.notas)


def test_convocatoria_dice_propiedad_unidad_y_servicio(runner_gestion):
    runner_gestion.email.entrantes = [correo_gestion()]
    runner_gestion.run_once()

    mensajes = [m for _, m in runner_gestion.whatsapp.enviados]
    assert mensajes, "se debio convocar a alguien"
    primero = mensajes[0]
    assert "Park Mesa Villas" in primero
    assert "Unidad:" in primero
    assert "tub" in primero
    assert "SI o NO" in primero


def test_solo_se_convoca_a_quien_tiene_la_habilidad(runner_gestion):
    """El servicio 'tub' exige la habilidad 'tinas': Adrian solo pinta."""
    runner_gestion.email.entrantes = [correo_gestion()]
    runner_gestion.run_once()

    convocados = set()
    for trabajo in runner_gestion.store.trabajos.values():
        convocados.update(trabajo.convocatorias)
    assert convocados == {"T01", "T02"}
    assert "T03" not in convocados


def test_correo_de_gestion_sin_unidades_alerta_en_vez_de_perderse(runner_gestion):
    cuerpo = "Hi, please call me about the schedule for next week.\n\nGuillermo"
    runner_gestion.email.entrantes = [correo_gestion(cuerpo=cuerpo)]
    resumen = runner_gestion.run_once()

    assert resumen["trabajos_nuevos"] == 1
    trabajo = next(iter(runner_gestion.store.trabajos.values()))
    assert trabajo.propiedad == "Park Mesa Villas"
    assert any("No se reconocieron unidades" in n for n in trabajo.notas)


# --- exportación ------------------------------------------------------------

def test_exportacion_reproduce_las_columnas_de_la_hoja(runner_gestion, tmp_path):
    runner_gestion.email.entrantes = [correo_gestion()]
    runner_gestion.run_once()
    runner_gestion.inbox.append("+15551234001", "SI")
    runner_gestion.run_once()

    trabajos = export.seleccionar(runner_gestion.store.trabajos.values())
    assert export.COLUMNAS == ["DATE", "Service", "Mgmt CO.", "Property Name",
                               "Person", "Unit", "Size", "Service Description"]

    filas = export.filas(trabajos)
    asignada = [f for f in filas if f["Person"]][0]
    assert asignada["DATE"] in ("8/8/26", "8/10/26")
    assert asignada["Service"] == "tub"
    assert asignada["Mgmt CO."] == "Shea Properties"
    assert asignada["Property Name"] == "Park Mesa Villas"
    assert asignada["Person"] == "Santos"
    assert asignada["Service Description"].startswith("full tub")

    destino = tmp_path / "control.csv"
    escritas = export.escribir_csv(trabajos, str(destino))
    assert escritas == len(filas)
    contenido = destino.read_text(encoding="utf-8-sig")
    assert contenido.splitlines()[0] == ",".join(export.COLUMNAS)


def test_exportacion_ordena_por_fecha_y_filtra_por_rango(runner_gestion):
    runner_gestion.email.entrantes = [correo_gestion()]
    runner_gestion.run_once()

    todos = export.seleccionar(runner_gestion.store.trabajos.values())
    assert [t.fecha_servicio for t in todos] == sorted(t.fecha_servicio for t in todos)

    solo_10 = export.seleccionar(runner_gestion.store.trabajos.values(),
                                 desde="2026-08-09")
    assert [t.unidad for t in solo_10] == ["J209"]


def test_exportacion_una_fila_por_trabajador_confirmado():
    trabajo = Trabajo(id="JB-1", propiedad="Reata", unidad="210", servicio="paint",
                      fecha_servicio="2026-07-06", descripcion_servicio="full paint")
    trabajo.convocatorias["T01"] = Convocatoria(
        trabajador_id="T01", nombre="Adrian",
        estado=EstadoConvocatoria.CONFIRMADO.value)
    trabajo.convocatorias["T02"] = Convocatoria(
        trabajador_id="T02", nombre="Marcos",
        estado=EstadoConvocatoria.CONFIRMADO.value)

    filas = export.filas([trabajo])
    assert [f["Person"] for f in filas] == ["Adrian", "Marcos"]
    assert all(f["Unit"] == "210" for f in filas)


def test_tope_diario_reparte_las_unidades_del_mismo_dia(runner_gestion):
    """3 unidades el 8/8: con tope de 2 no puede cargarselas una sola persona."""
    runner_gestion.email.entrantes = [correo_gestion()]
    runner_gestion.run_once()

    del_8 = [t for t in runner_gestion.store.trabajos.values()
             if t.fecha_servicio == "2026-08-08"]
    assert len(del_8) == 3

    por_persona = {}
    for trabajo in del_8:
        for worker_id in trabajo.convocatorias:
            por_persona[worker_id] = por_persona.get(worker_id, 0) + 1

    assert max(por_persona.values()) <= 2
    assert len(por_persona) == 2       # se repartio entre dos


def test_la_carga_se_cuenta_por_fecha_no_en_total(gestion):
    """Estar lleno el martes no impide tomar trabajo el jueves."""
    roster = WorkerRoster(str(gestion["trabajadores"]))
    ocupado = Trabajo(id="JB-1", fecha_servicio="2026-08-08")
    ocupado.convocatorias["T01"] = Convocatoria(trabajador_id="T01", nombre="Santos",
                                                estado=EstadoConvocatoria.CONFIRMADO.value)

    assert roster.carga_actual([ocupado], fecha="2026-08-08") == {"T01": 1}
    assert roster.carga_actual([ocupado], fecha="2026-08-10") == {}
    assert roster.carga_actual([ocupado]) == {"T01": 1}


def test_tope_diario_no_deja_una_unidad_sin_personal(gestion, tmp_path):
    """Si todos llegaron al tope, se asigna de todos modos en vez de perder la unidad."""
    config = build_config_gestion(tmp_path, gestion)
    config["asistencia"]["max_trabajos_por_dia"] = 1
    runner = WorkflowRunner(config)
    runner.email = FakeEmail(entrantes=[correo_gestion()])
    runner.notifier = Notifier(FakeWhatsApp(), email_client=runner.email)
    runner.run_once()

    del_8 = [t for t in runner.store.trabajos.values()
             if t.fecha_servicio == "2026-08-08"]
    # 3 unidades ese dia y solo 2 personas con la habilidad 'tinas'.
    assert all(t.convocatorias for t in del_8)
    assert all(t.estado != EstadoTrabajo.SIN_PERSONAL.value for t in del_8)
