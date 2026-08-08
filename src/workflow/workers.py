"""
CSV-backed worker roster and candidate selection.

data/trabajadores.csv columns:
    id,nombre,telefono,email,habilidades,zona,activo

`habilidades` is a "|"-separated list (plomeria|electricidad), `activo` accepts
si/no/true/false/1/0.
"""

import csv
import logging
import re
from pathlib import Path
from typing import Optional

from src.workflow.models import Trabajador, Trabajo
from src.workflow.parser import strip_accents

logger = logging.getLogger(__name__)

_VERDADEROS = {"si", "s", "yes", "y", "true", "1", "activo", "x"}


class WorkerRoster:
    """Loads workers and picks who to call for a job."""

    def __init__(self, path: str = "data/trabajadores.csv",
                 default_country_code: str = "+1"):
        self.path = Path(path)
        self.default_country_code = default_country_code
        self.trabajadores: dict[str, Trabajador] = {}
        self.load()

    def load(self) -> None:
        self.trabajadores = {}
        if not self.path.exists():
            logger.warning("No existe %s; no hay trabajadores cargados", self.path)
            return
        with self.path.open(encoding="utf-8-sig", newline="") as f:
            for fila in csv.DictReader(f):
                trabajador = self._row_to_trabajador(fila)
                if trabajador:
                    self.trabajadores[trabajador.id] = trabajador
        activos = sum(1 for t in self.trabajadores.values() if t.activo)
        logger.info("Trabajadores cargados: %d (%d activos) desde %s",
                    len(self.trabajadores), activos, self.path)

    def _row_to_trabajador(self, fila: dict) -> Optional[Trabajador]:
        normalizada = {strip_accents(k or "").strip(): (v or "").strip()
                       for k, v in fila.items()}
        nombre = normalizada.get("nombre")
        worker_id = normalizada.get("id") or normalizada.get("codigo")
        if not nombre and not worker_id:
            return None
        if not worker_id:
            worker_id = strip_accents(nombre).replace(" ", "-")[:16].upper()

        habilidades = [
            h.strip() for h in re.split(r"[|,;/]", normalizada.get("habilidades", ""))
            if h.strip()
        ]
        activo_raw = strip_accents(normalizada.get("activo", "si"))
        return Trabajador(
            id=worker_id,
            nombre=nombre or worker_id,
            telefono=self.normalize_phone(normalizada.get("telefono", "")),
            email=normalizada.get("email", ""),
            habilidades=habilidades,
            zona=normalizada.get("zona", ""),
            activo=activo_raw in _VERDADEROS or activo_raw == "",
        )

    def normalize_phone(self, telefono: str) -> str:
        """Return an E.164-ish number: digits only, prefixed with the country code."""
        if not telefono:
            return ""
        limpio = re.sub(r"[^\d+]", "", telefono)
        if limpio.startswith("+"):
            return "+" + re.sub(r"\D", "", limpio[1:])
        digitos = re.sub(r"\D", "", limpio)
        if not digitos:
            return ""
        if digitos.startswith("00"):
            return "+" + digitos[2:]
        return f"{self.default_country_code}{digitos}"

    def get(self, worker_id: str) -> Optional[Trabajador]:
        return self.trabajadores.get(worker_id)

    def by_phone(self, telefono: str) -> Optional[Trabajador]:
        """Look a worker up from an inbound WhatsApp number."""
        objetivo = self.normalize_phone(telefono)
        if not objetivo:
            return None
        for trabajador in self.trabajadores.values():
            if trabajador.telefono == objetivo:
                return trabajador
        # Some gateways drop or add a leading country digit; compare the last 8.
        cola = objetivo[-8:]
        for trabajador in self.trabajadores.values():
            if trabajador.telefono and trabajador.telefono[-8:] == cola:
                return trabajador
        return None

    def by_email(self, email: str) -> Optional[Trabajador]:
        objetivo = (email or "").strip().lower()
        if not objetivo:
            return None
        for trabajador in self.trabajadores.values():
            if trabajador.email.lower() == objetivo:
                return trabajador
        return None

    def seleccionar(self, trabajo: Trabajo, cantidad: int,
                    excluir: Optional[set[str]] = None,
                    carga: Optional[dict[str, int]] = None,
                    max_por_dia: int = 0) -> list[Trabajador]:
        """Pick the best `cantidad` candidates for a job.

        Ranking: skills matched first, then same zone, then lightest workload,
        then name for a stable, predictable order.

        `max_por_dia` caps how many units one person can take on the job's date.
        A single painter cannot turn over six apartments in a morning, and
        without the cap the nearest worker absorbs the whole email.
        """
        excluir = excluir or set()
        carga = carga or {}
        zona_trabajo = strip_accents(trabajo.zona)

        candidatos = [
            t for t in self.trabajadores.values()
            if t.activo and t.id not in excluir and t.tiene_habilidades(trabajo.habilidades)
        ]
        if not candidatos and trabajo.habilidades:
            # Nobody covers every skill: fall back to anyone with at least one.
            requeridas = {strip_accents(h) for h in trabajo.habilidades}
            candidatos = [
                t for t in self.trabajadores.values()
                if t.activo and t.id not in excluir
                and requeridas & {strip_accents(h) for h in t.habilidades}
            ]
            if candidatos:
                logger.warning(
                    "Ningún trabajador cubre todas las habilidades %s del trabajo %s; "
                    "se convoca con cobertura parcial", trabajo.habilidades, trabajo.id
                )

        if max_por_dia > 0:
            disponibles = [t for t in candidatos if carga.get(t.id, 0) < max_por_dia]
            if disponibles:
                candidatos = disponibles
            elif candidatos:
                # Everybody is at the cap. Leaving the unit unstaffed is worse
                # than a visible overload, so assign anyway and say so.
                logger.warning(
                    "Todos los candidatos de %s ya tienen %d trabajos el %s; "
                    "se asigna por encima del tope",
                    trabajo.id, max_por_dia, trabajo.fecha_servicio or "sin fecha")

        candidatos.sort(key=lambda t: (
            0 if (zona_trabajo and strip_accents(t.zona) == zona_trabajo) else 1,
            carga.get(t.id, 0),
            strip_accents(t.nombre),
        ))
        return candidatos[:cantidad]

    def carga_actual(self, trabajos: list[Trabajo],
                     fecha: Optional[str] = None) -> dict[str, int]:
        """Count active assignments per worker.

        With `fecha`, only jobs scheduled that day are counted: what matters is
        how full someone's Tuesday is, not how many jobs they have all month.
        """
        carga: dict[str, int] = {}
        for trabajo in trabajos:
            if fecha is not None and trabajo.fecha_servicio != fecha:
                continue
            for convocatoria in trabajo.convocatorias.values():
                if convocatoria.estado in ("pendiente", "confirmado"):
                    carga[convocatoria.trabajador_id] = \
                        carga.get(convocatoria.trabajador_id, 0) + 1
        return carga
