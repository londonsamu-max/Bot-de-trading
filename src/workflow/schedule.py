"""
Sending window.

Attendance requests reach people's personal phones, so they only go out during
working hours. Defaults follow the hours published on jbrenovate.com
(Mon-Fri), widened a little at both ends because crews start before the office
does.

Work orders and supervisor alerts are NOT held back: somebody who just
confirmed is waiting for the details, and an alert is urgent by definition.
"""

import logging
from datetime import datetime, time, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None


class VentanaDeEnvio:
    """Decides whether it is an acceptable hour to message a worker."""

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        self.activa = bool(config.get("activo", True))
        self.inicio = _parse_hora(config.get("inicio", "08:00"), time(8, 0))
        self.fin = _parse_hora(config.get("fin", "19:00"), time(19, 0))
        self.dias = set(config.get("dias", [1, 2, 3, 4, 5]))  # 1=lunes ... 7=domingo
        self.zona = _zona_horaria(config.get("zona_horaria", "America/New_York"))

    def abierta(self, ahora: Optional[datetime] = None) -> bool:
        """True if messages may go out right now."""
        if not self.activa:
            return True
        local = self._local(ahora)
        if local.isoweekday() not in self.dias:
            return False
        return self.inicio <= local.time() <= self.fin

    def descripcion(self, ahora: Optional[datetime] = None) -> str:
        """Human-readable reason, for the log line when sending is held back."""
        local = self._local(ahora)
        return (f"{local.strftime('%a %H:%M')} fuera de "
                f"{self.inicio.strftime('%H:%M')}-{self.fin.strftime('%H:%M')} "
                f"(días {sorted(self.dias)})")

    def proxima_apertura(self, ahora: Optional[datetime] = None) -> datetime:
        """When the window opens next; useful to report how long the wait is."""
        local = self._local(ahora)
        if self.abierta(ahora):
            return local

        candidato = local.replace(hour=self.inicio.hour, minute=self.inicio.minute,
                                  second=0, microsecond=0)
        if candidato <= local:
            candidato += timedelta(days=1)

        # A misconfigured (or empty) day set must not loop forever.
        for _ in range(7):
            if candidato.isoweekday() in self.dias:
                return candidato
            candidato += timedelta(days=1)
        return candidato

    def _local(self, ahora: Optional[datetime]) -> datetime:
        if ahora is None:
            return datetime.now(self.zona) if self.zona else datetime.now()
        if self.zona and ahora.tzinfo:
            return ahora.astimezone(self.zona)
        return ahora


def _parse_hora(valor, por_defecto: time) -> time:
    """Parse "HH:MM" into a time; fall back to the default on garbage."""
    if isinstance(valor, time):
        return valor
    try:
        horas, _, minutos = str(valor).partition(":")
        return time(int(horas), int(minutos or 0))
    except (ValueError, TypeError):
        logger.warning("Hora inválida '%s' en horario_envios; se usa %s", valor, por_defecto)
        return por_defecto


def _zona_horaria(nombre: str):
    """Load a timezone, degrading to naive local time if tzdata is unavailable."""
    if not nombre or ZoneInfo is None:
        return None
    try:
        return ZoneInfo(nombre)
    except Exception:  # ZoneInfoNotFoundError and friends
        logger.warning("Zona horaria '%s' no disponible; se usa la hora local del servidor",
                       nombre)
        return None
