"""
Domain models for the work dispatch workflow.

Domain vocabulary is Spanish because it matches the CSV files, the WhatsApp
messages and the emails the team actually reads.

    Trabajo      -> a job requested by a client through email
    ItemPedido   -> a material/inventory line inside that job
    Convocatoria -> the attendance request sent to one worker for one job
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def utc_now() -> str:
    """Current UTC timestamp as ISO-8601 string (the format used in state.json)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp written by `utc_now`. Returns None if unset/invalid."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class EstadoTrabajo(str, Enum):
    """Lifecycle of a job."""

    NUEVO = "nuevo"                  # parsed from email, nothing sent yet
    CONVOCANDO = "convocando"        # attendance requests sent, waiting for answers
    ASIGNADO = "asignado"            # enough workers confirmed, work order sent
    COMPLETADO = "completado"        # job done, material consumed
    SIN_PERSONAL = "sin_personal"    # nobody confirmed before the deadline
    CANCELADO = "cancelado"          # manually cancelled


class EstadoConvocatoria(str, Enum):
    """Answer of one worker to one attendance request."""

    PENDIENTE = "pendiente"
    CONFIRMADO = "confirmado"
    RECHAZADO = "rechazado"
    EXPIRADO = "expirado"


@dataclass
class ItemPedido:
    """One material line requested by the client."""

    descripcion: str
    cantidad: float = 1.0
    unidad: str = "u"
    sku: Optional[str] = None          # filled by the inventory matcher
    disponible: Optional[float] = None  # stock free at check time
    faltante: float = 0.0               # what could not be covered, 0 if complete
    reservado: float = 0.0              # units actually held for this job

    def to_dict(self) -> dict:
        return {
            "descripcion": self.descripcion,
            "cantidad": self.cantidad,
            "unidad": self.unidad,
            "sku": self.sku,
            "disponible": self.disponible,
            "faltante": self.faltante,
            "reservado": self.reservado,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ItemPedido":
        return cls(
            descripcion=data["descripcion"],
            cantidad=float(data.get("cantidad", 1.0)),
            unidad=data.get("unidad", "u"),
            sku=data.get("sku"),
            disponible=data.get("disponible"),
            faltante=float(data.get("faltante", 0.0)),
            reservado=float(data.get("reservado", 0.0)),
        )

    def __str__(self) -> str:
        cantidad = int(self.cantidad) if float(self.cantidad).is_integer() else self.cantidad
        return f"{cantidad} {self.unidad} - {self.descripcion}"


@dataclass
class Trabajador:
    """One worker, loaded from data/trabajadores.csv."""

    id: str
    nombre: str
    telefono: str = ""
    email: str = ""
    habilidades: list[str] = field(default_factory=list)
    zona: str = ""
    activo: bool = True

    def tiene_habilidades(self, requeridas: list[str]) -> bool:
        """True if the worker covers every requested skill (empty request = anyone)."""
        if not requeridas:
            return True
        propias = {h.lower() for h in self.habilidades}
        return all(r.lower() in propias for r in requeridas)


@dataclass
class Convocatoria:
    """Attendance request sent to one worker for one job."""

    trabajador_id: str
    nombre: str
    telefono: str = ""
    email: str = ""
    estado: str = EstadoConvocatoria.PENDIENTE.value
    canal: str = ""              # whatsapp / email / manual
    enviado_at: Optional[str] = None
    recordado_at: Optional[str] = None
    respondido_at: Optional[str] = None
    respuesta_cruda: str = ""    # raw text of the answer, for auditing

    def to_dict(self) -> dict:
        return {
            "trabajador_id": self.trabajador_id,
            "nombre": self.nombre,
            "telefono": self.telefono,
            "email": self.email,
            "estado": self.estado,
            "canal": self.canal,
            "enviado_at": self.enviado_at,
            "recordado_at": self.recordado_at,
            "respondido_at": self.respondido_at,
            "respuesta_cruda": self.respuesta_cruda,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Convocatoria":
        return cls(
            trabajador_id=data["trabajador_id"],
            nombre=data.get("nombre", ""),
            telefono=data.get("telefono", ""),
            email=data.get("email", ""),
            estado=data.get("estado", EstadoConvocatoria.PENDIENTE.value),
            canal=data.get("canal", ""),
            enviado_at=data.get("enviado_at"),
            recordado_at=data.get("recordado_at"),
            respondido_at=data.get("respondido_at"),
            respuesta_cruda=data.get("respuesta_cruda", ""),
        )


@dataclass
class Trabajo:
    """A job extracted from a client email."""

    id: str
    cliente_nombre: str = ""
    cliente_email: str = ""
    cliente_telefono: str = ""
    asunto: str = ""
    descripcion: str = ""
    direccion: str = ""
    zona: str = ""
    fecha_servicio: str = ""     # ISO date if parsed, raw text otherwise
    hora_servicio: str = ""
    habilidades: list[str] = field(default_factory=list)
    trabajadores_requeridos: int = 1
    items: list[ItemPedido] = field(default_factory=list)
    estado: str = EstadoTrabajo.NUEVO.value
    creado_at: str = field(default_factory=utc_now)
    message_id: str = ""
    adjuntos: list[str] = field(default_factory=list)  # rutas de las fotos del cliente
    convocatorias: dict[str, Convocatoria] = field(default_factory=dict)
    inventario_reservado: bool = False
    orden_enviada_at: Optional[str] = None
    acuse_cliente_at: Optional[str] = None
    notas: list[str] = field(default_factory=list)

    # --- attendance helpers -------------------------------------------------

    def confirmados(self) -> list[Convocatoria]:
        return [c for c in self.convocatorias.values()
                if c.estado == EstadoConvocatoria.CONFIRMADO.value]

    def pendientes(self) -> list[Convocatoria]:
        return [c for c in self.convocatorias.values()
                if c.estado == EstadoConvocatoria.PENDIENTE.value]

    def cupo_cubierto(self) -> bool:
        return len(self.confirmados()) >= self.trabajadores_requeridos

    def faltantes_inventario(self) -> list[ItemPedido]:
        return [i for i in self.items if i.faltante > 0]

    def agregar_nota(self, texto: str) -> None:
        self.notas.append(f"{utc_now()} {texto}")

    # --- serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "cliente_nombre": self.cliente_nombre,
            "cliente_email": self.cliente_email,
            "cliente_telefono": self.cliente_telefono,
            "asunto": self.asunto,
            "descripcion": self.descripcion,
            "direccion": self.direccion,
            "zona": self.zona,
            "fecha_servicio": self.fecha_servicio,
            "hora_servicio": self.hora_servicio,
            "habilidades": self.habilidades,
            "trabajadores_requeridos": self.trabajadores_requeridos,
            "items": [i.to_dict() for i in self.items],
            "estado": self.estado,
            "creado_at": self.creado_at,
            "message_id": self.message_id,
            "adjuntos": self.adjuntos,
            "convocatorias": {k: v.to_dict() for k, v in self.convocatorias.items()},
            "inventario_reservado": self.inventario_reservado,
            "orden_enviada_at": self.orden_enviada_at,
            "acuse_cliente_at": self.acuse_cliente_at,
            "notas": self.notas,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Trabajo":
        return cls(
            id=data["id"],
            cliente_nombre=data.get("cliente_nombre", ""),
            cliente_email=data.get("cliente_email", ""),
            cliente_telefono=data.get("cliente_telefono", ""),
            asunto=data.get("asunto", ""),
            descripcion=data.get("descripcion", ""),
            direccion=data.get("direccion", ""),
            zona=data.get("zona", ""),
            fecha_servicio=data.get("fecha_servicio", ""),
            hora_servicio=data.get("hora_servicio", ""),
            habilidades=list(data.get("habilidades", [])),
            trabajadores_requeridos=int(data.get("trabajadores_requeridos", 1)),
            items=[ItemPedido.from_dict(i) for i in data.get("items", [])],
            estado=data.get("estado", EstadoTrabajo.NUEVO.value),
            creado_at=data.get("creado_at", utc_now()),
            message_id=data.get("message_id", ""),
            adjuntos=list(data.get("adjuntos", [])),
            convocatorias={k: Convocatoria.from_dict(v)
                           for k, v in data.get("convocatorias", {}).items()},
            inventario_reservado=bool(data.get("inventario_reservado", False)),
            orden_enviada_at=data.get("orden_enviada_at"),
            acuse_cliente_at=data.get("acuse_cliente_at"),
            notas=list(data.get("notas", [])),
        )
