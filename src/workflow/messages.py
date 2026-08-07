"""
Message templating.

Templates live in config/workflow.yaml under `mensajes:` so the team can
reword what clients and workers receive without touching code. Placeholders
use {llave} syntax; an unknown placeholder is left as-is instead of raising,
so a typo in the YAML degrades to visible text rather than a crash.
"""

import logging
from typing import Optional

from src.workflow.models import Trabajo

logger = logging.getLogger(__name__)

PLANTILLAS_POR_DEFECTO = {
    "convocatoria": (
        "Hola {trabajador}, hay trabajo disponible.\n\n"
        "Folio: {job_id}\n"
        "Cliente: {cliente}\n"
        "Fecha: {fecha} {hora}\n"
        "Dirección: {direccion}\n\n"
        "Trabajo:\n{descripcion}\n\n"
        "Material que se entrega:\n{materiales}\n\n"
        "¿Puedes asistir? Responde SI o NO a este mensaje."
    ),
    "recordatorio": (
        "Recordatorio {job_id} ({fecha} {hora} - {direccion}).\n"
        "Seguimos esperando tu respuesta: SI o NO."
    ),
    "orden_trabajo": (
        "CONFIRMADO - Orden de trabajo {job_id}\n\n"
        "Cliente: {cliente}\n"
        "Teléfono del cliente: {telefono_cliente}\n"
        "Fecha: {fecha} {hora}\n"
        "Dirección: {direccion}\n"
        "Cuadrilla: {cuadrilla}\n\n"
        "Trabajo a realizar:\n{descripcion}\n\n"
        "Material asignado:\n{materiales}\n\n"
        "Fotos del cliente: {adjuntos}\n\n"
        "Recoge el material antes de salir. Cualquier cambio avisa a {contacto}."
    ),
    "acuse_cliente": (
        "Hola {cliente},\n\n"
        "Recibimos su solicitud y quedó registrada con el folio {job_id}.\n"
        "Fecha programada: {fecha} {hora}\n"
        "Dirección: {direccion}\n\n"
        "Ya estamos confirmando al personal y le avisamos en cuanto quede asignado.\n\n"
        "Saludos,\n{empresa}"
    ),
    "cliente_asignado": (
        "Hola {cliente},\n\n"
        "Su servicio {job_id} quedó asignado para {fecha} {hora}.\n"
        "Personal asignado: {cuadrilla}\n"
        "Dirección: {direccion}\n\n"
        "Saludos,\n{empresa}"
    ),
    "alerta_sin_personal": (
        "ATENCIÓN: el trabajo {job_id} ({cliente}, {fecha} {hora}) no tiene "
        "personal confirmado.\n"
        "Convocados: {convocados} | Confirmados: {confirmados} de {requeridos}\n"
        "Dirección: {direccion}\n\n"
        "Hay que asignarlo a mano."
    ),
    "alerta_inventario": (
        "Faltante de material para {job_id} ({cliente}):\n{faltantes}\n\n"
        "El trabajo sigue en pie, pero hay que comprar o sustituir ese material."
    ),
}


class _SafeDict(dict):
    """Leaves unknown placeholders untouched instead of raising KeyError."""

    def __missing__(self, key: str) -> str:
        logger.warning("Plantilla usa {%s}, que no existe en el contexto", key)
        return "{" + key + "}"


class MessageBuilder:
    """Renders the messages sent to workers and clients."""

    def __init__(self, config: Optional[dict] = None, empresa: str = "",
                 contacto: str = ""):
        config = config or {}
        self.plantillas = {**PLANTILLAS_POR_DEFECTO, **config}
        self.empresa = empresa
        self.contacto = contacto

    def render(self, nombre: str, **extra) -> str:
        plantilla = self.plantillas.get(nombre, "")
        if not plantilla:
            logger.error("No existe la plantilla '%s'", nombre)
            return ""
        return plantilla.format_map(_SafeDict(extra)).strip()

    def contexto(self, trabajo: Trabajo, trabajador: str = "") -> dict:
        """Build the placeholder values for a job."""
        confirmados = trabajo.confirmados()
        return {
            "job_id": trabajo.id,
            "cliente": trabajo.cliente_nombre or trabajo.cliente_email or "cliente",
            "cliente_email": trabajo.cliente_email,
            "telefono_cliente": trabajo.cliente_telefono or "no lo dejó",
            "adjuntos": _formato_adjuntos(trabajo.adjuntos),
            "fecha": trabajo.fecha_servicio or "por confirmar",
            "hora": trabajo.hora_servicio,
            "direccion": trabajo.direccion or "por confirmar",
            "zona": trabajo.zona,
            "descripcion": trabajo.descripcion or "(sin detalle)",
            "materiales": formato_items(trabajo.items),
            "faltantes": formato_items(trabajo.faltantes_inventario()) or "ninguno",
            "trabajador": trabajador,
            "cuadrilla": ", ".join(c.nombre for c in confirmados) or "por asignar",
            "requeridos": trabajo.trabajadores_requeridos,
            "confirmados": len(confirmados),
            "convocados": len(trabajo.convocatorias),
            "empresa": self.empresa,
            "contacto": self.contacto,
            "asunto_original": trabajo.asunto,
        }

    def convocatoria(self, trabajo: Trabajo, nombre_trabajador: str) -> str:
        return self.render("convocatoria", **self.contexto(trabajo, nombre_trabajador))

    def recordatorio(self, trabajo: Trabajo, nombre_trabajador: str) -> str:
        return self.render("recordatorio", **self.contexto(trabajo, nombre_trabajador))

    def orden_trabajo(self, trabajo: Trabajo, nombre_trabajador: str) -> str:
        return self.render("orden_trabajo", **self.contexto(trabajo, nombre_trabajador))


def formato_items(items: list) -> str:
    """Render material lines as a readable bullet list."""
    if not items:
        return "sin material asignado"
    lineas = []
    for item in items:
        linea = f"- {item}"
        if getattr(item, "faltante", 0) > 0:
            linea += f"  (FALTAN {_num(item.faltante)})"
        lineas.append(linea)
    return "\n".join(lineas)


def _formato_adjuntos(adjuntos: list) -> str:
    """Photos travel attached to the work-order email; WhatsApp only gets the count."""
    if not adjuntos:
        return "ninguna"
    if len(adjuntos) == 1:
        return "1 foto (va adjunta en el correo)"
    return f"{len(adjuntos)} fotos (van adjuntas en el correo)"


def _num(valor: float) -> str:
    return str(int(valor)) if float(valor).is_integer() else f"{valor:g}"
