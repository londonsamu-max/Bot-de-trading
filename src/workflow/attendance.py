"""
Attendance tracking.

Owns the state machine of a Convocatoria (attendance request):

    pendiente --SI--> confirmado
              --NO--> rechazado
              --timeout--> expirado

and answers the two questions the runner asks each cycle: who needs a
reminder, and which requests have run out of time.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.workflow.models import (
    Convocatoria,
    EstadoConvocatoria,
    Trabajador,
    Trabajo,
    parse_ts,
    utc_now,
)

logger = logging.getLogger(__name__)


class AttendanceManager:
    """Creates attendance requests and records the answers."""

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.recordatorio_minutos = int(config.get("recordatorio_minutos", 60))
        self.expiracion_horas = float(config.get("expiracion_horas", 4))
        self.max_convocados_extra = int(config.get("max_convocados_extra", 2))
        # Invite a couple more workers than strictly needed so one "NO" doesn't
        # stall the job waiting for a second round.
        self.margen_convocatoria = int(config.get("margen_convocatoria", 1))

    def cuantos_convocar(self, trabajo: Trabajo) -> int:
        """How many workers still need to be invited for this job."""
        faltan = trabajo.trabajadores_requeridos - len(trabajo.confirmados())
        if faltan <= 0:
            return 0
        pendientes = len(trabajo.pendientes())
        return max(0, faltan + self.margen_convocatoria - pendientes)

    def abrir(self, trabajo: Trabajo, trabajadores: list[Trabajador]) -> list[Convocatoria]:
        """Register attendance requests for workers not already invited."""
        nuevas = []
        for trabajador in trabajadores:
            if trabajador.id in trabajo.convocatorias:
                continue
            convocatoria = Convocatoria(
                trabajador_id=trabajador.id,
                nombre=trabajador.nombre,
                telefono=trabajador.telefono,
                email=trabajador.email,
                estado=EstadoConvocatoria.PENDIENTE.value,
            )
            trabajo.convocatorias[trabajador.id] = convocatoria
            nuevas.append(convocatoria)
        return nuevas

    def marcar_enviada(self, convocatoria: Convocatoria, canal: str) -> None:
        convocatoria.enviado_at = utc_now()
        convocatoria.canal = canal

    def registrar_respuesta(self, trabajo: Trabajo, trabajador_id: str,
                            afirmativo: bool, canal: str = "",
                            crudo: str = "") -> bool:
        """Record a worker's answer. Returns False if there is nothing to answer.

        Answers arriving after the request expired are still accepted: a worker
        replying late is better than an unstaffed job.
        """
        convocatoria = trabajo.convocatorias.get(trabajador_id)
        if not convocatoria:
            logger.warning("%s no fue convocado al trabajo %s", trabajador_id, trabajo.id)
            return False

        if convocatoria.estado in (EstadoConvocatoria.CONFIRMADO.value,
                                   EstadoConvocatoria.RECHAZADO.value):
            logger.info("%s ya había respondido al trabajo %s (%s)",
                        trabajador_id, trabajo.id, convocatoria.estado)
            return False

        convocatoria.estado = (EstadoConvocatoria.CONFIRMADO.value if afirmativo
                               else EstadoConvocatoria.RECHAZADO.value)
        convocatoria.respondido_at = utc_now()
        convocatoria.respuesta_cruda = (crudo or "")[:300]
        if canal:
            convocatoria.canal = canal

        trabajo.agregar_nota(
            f"{convocatoria.nombre} respondió {'SI' if afirmativo else 'NO'} por {canal or 'n/d'}"
        )
        logger.info("Trabajo %s: %s -> %s", trabajo.id, convocatoria.nombre,
                    convocatoria.estado)
        return True

    def pendientes_por_recordar(self, trabajo: Trabajo,
                                ahora: Optional[datetime] = None) -> list[Convocatoria]:
        """Requests sent long enough ago to deserve one (and only one) reminder."""
        ahora = ahora or datetime.now(timezone.utc)
        limite = timedelta(minutes=self.recordatorio_minutos)
        listas = []
        for convocatoria in trabajo.pendientes():
            if convocatoria.recordado_at:
                continue
            enviado = parse_ts(convocatoria.enviado_at)
            if enviado and ahora - enviado >= limite:
                listas.append(convocatoria)
        return listas

    def expirar(self, trabajo: Trabajo,
                ahora: Optional[datetime] = None) -> list[Convocatoria]:
        """Mark unanswered requests as expired once the deadline passed."""
        ahora = ahora or datetime.now(timezone.utc)
        limite = timedelta(hours=self.expiracion_horas)
        expiradas = []
        for convocatoria in trabajo.pendientes():
            enviado = parse_ts(convocatoria.enviado_at)
            if enviado and ahora - enviado >= limite:
                convocatoria.estado = EstadoConvocatoria.EXPIRADO.value
                expiradas.append(convocatoria)
                trabajo.agregar_nota(f"{convocatoria.nombre} no respondió a tiempo")
        return expiradas

    def sin_salida(self, trabajo: Trabajo) -> bool:
        """True when the job can no longer be staffed: no pending answers left."""
        return not trabajo.cupo_cubierto() and not trabajo.pendientes()
