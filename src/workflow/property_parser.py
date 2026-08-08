"""
Property-management email parser.

The real clients are apartment management companies, and one email carries a
LIST of units, each of which is its own job:

    B109 vacant, full tub. 8/8/26
    M102 Vacant, full tub. 8/8/26
    J209 vacant, full tub. 8/10/26 AM please

Each line becomes one Trabajo, with the fields of the tracking spreadsheet:
DATE, Service, Mgmt CO., Property Name, Person, Unit, Size, Service Description.

Signature blocks, greetings and addresses must NOT become jobs, so a line only
counts when it starts with a unit token AND names a service or an occupancy
state ("vacant" / "occ").
"""

import logging
import re
from datetime import date, datetime
from typing import Optional

from src.workflow.parser import strip_accents

logger = logging.getLogger(__name__)

# Service categories and the words that identify them. Short codes like "cc"
# are matched as whole words so "occ" (occupied) never reads as carpet.
SERVICIOS: dict[str, list[str]] = {
    "paint": ["paint", "painting", "repaint", "pintura", "pintar"],
    "jani": ["jani", "janitorial", "clean", "cleaning", "detail", "limpieza"],
    "carpet": ["carpet", "cc", "shampoo", "steam", "alfombra"],
    "tub": ["tub", "bathtub", "reglaze", "reglazing", "resurface", "refinish",
            "tina", "banera"],
}

_FECHA = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_TAMANO = re.compile(r"\b(\d)\s*\+\s*(\d)\b")
_TURNO = re.compile(r"\b(am|pm)\b", re.IGNORECASE)
_VACANTE = re.compile(r"\b(vacant|vacante|vacia|empty)\w*\b", re.IGNORECASE)
_OCUPADA = re.compile(r"\b(occ|occupied|ocupada|ocupado)\b", re.IGNORECASE)
_RUIDO = re.compile(r"\b(please|porfavor|por favor|thanks|gracias|asap)\b", re.IGNORECASE)


class UnidadDeTrabajo:
    """One parsed line: a unit that needs a service on a date."""

    def __init__(self, unidad: str, servicio: str, descripcion: str,
                 fecha: str = "", turno: str = "", tamano: str = "",
                 ocupada: bool = False, linea: str = ""):
        self.unidad = unidad
        self.servicio = servicio
        self.descripcion = descripcion
        self.fecha = fecha
        self.turno = turno
        self.tamano = tamano
        self.ocupada = ocupada
        self.linea = linea

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"UnidadDeTrabajo({self.unidad!r}, {self.servicio!r}, "
                f"{self.descripcion!r}, {self.fecha!r})")


class PropertyEmailParser:
    """Turns a management-company email into one work item per unit line."""

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        # Costa Mesa, CA: dates arrive as month/day/year.
        self.formato_us = str(config.get("formato_fecha", "US")).upper() == "US"
        self.max_lineas = int(config.get("max_unidades_por_correo", 60))

        self.servicios = {k: list(v) for k, v in SERVICIOS.items()}
        for nombre, palabras in (config.get("servicios_extra") or {}).items():
            self.servicios.setdefault(nombre, [])
            self.servicios[nombre] += list(palabras)

    def parse(self, cuerpo: str, hoy: Optional[date] = None) -> list[UnidadDeTrabajo]:
        """Extract every unit line from the email body."""
        unidades: list[UnidadDeTrabajo] = []

        for linea in (cuerpo or "").splitlines():
            if len(unidades) >= self.max_lineas:
                logger.warning("Se alcanzó el máximo de %d unidades por correo",
                               self.max_lineas)
                break
            unidades.extend(self._parse_linea(linea, hoy))

        return unidades

    # --- internals ----------------------------------------------------------

    def _parse_linea(self, linea: str, hoy: Optional[date]) -> list[UnidadDeTrabajo]:
        texto = linea.strip().lstrip("-*•·>+").strip()
        # Drop an enumeration prefix ("1) B109 ...") without eating the unit.
        texto = re.sub(r"^\d{1,2}[.)]\s+(?=\S)", "", texto)
        if not texto or len(texto) > 300:
            return []

        unidad = self._extraer_unidad(texto)
        if not unidad:
            return []

        servicios = self._detectar_servicios(texto)
        vacante = _VACANTE.search(texto)
        ocupada = _OCUPADA.search(texto)
        if not servicios and not (vacante or ocupada):
            return []   # a signature or address line, not work

        fecha = self._extraer_fecha(texto, hoy)
        turno = self._extraer_turno(texto)
        tamano = self._extraer_tamano(texto)
        descripcion = self._descripcion(texto, unidad)

        # A line naming two services ("jani and carpet") is two rows in the
        # spreadsheet, so it becomes two jobs here.
        return [
            UnidadDeTrabajo(
                unidad=unidad, servicio=servicio, descripcion=descripcion,
                fecha=fecha, turno=turno, tamano=tamano,
                ocupada=bool(ocupada) and not vacante, linea=texto,
            )
            for servicio in (servicios or ["otro"])
        ]

    @staticmethod
    def _extraer_unidad(texto: str) -> str:
        """The unit code is the first token, and it always carries a digit."""
        primero = texto.split()[0] if texto.split() else ""
        limpio = primero.strip(".,;:()[]")
        if not limpio or len(limpio) > 15:
            return ""
        if not any(c.isdigit() for c in limpio):
            return ""
        # A bare date or a size is not a unit number.
        if _FECHA.fullmatch(limpio) or _TAMANO.fullmatch(limpio):
            return ""
        if limpio.count("/") >= 2:
            return ""
        return limpio

    def _detectar_servicios(self, texto: str) -> list[str]:
        """Every service named in the line, in the order they appear."""
        normalizado = strip_accents(texto)
        encontrados: list[tuple[int, str]] = []

        for servicio, palabras in self.servicios.items():
            posicion = None
            for palabra in palabras:
                match = re.search(rf"\b{re.escape(strip_accents(palabra))}\w*\b",
                                  normalizado)
                if match and (posicion is None or match.start() < posicion):
                    posicion = match.start()
            if posicion is not None:
                encontrados.append((posicion, servicio))

        encontrados.sort()
        return [servicio for _, servicio in encontrados]

    def _extraer_fecha(self, texto: str, hoy: Optional[date]) -> str:
        match = _FECHA.search(texto)
        if not match:
            return ""

        primero, segundo, anio = match.groups()
        mes, dia = (primero, segundo) if self.formato_us else (segundo, primero)

        hoy = hoy or date.today()
        if anio:
            anio_int = int(anio)
            if anio_int < 100:
                anio_int += 2000
        else:
            anio_int = hoy.year

        try:
            resultado = date(anio_int, int(mes), int(dia))
        except ValueError:
            logger.warning("Fecha inválida en la línea: %s", match.group(0))
            return match.group(0)

        # "12/28" written in early January means last month, not next December.
        if not anio and (resultado - hoy).days < -180:
            try:
                resultado = resultado.replace(year=anio_int + 1)
            except ValueError:
                pass
        return resultado.isoformat()

    @staticmethod
    def _extraer_turno(texto: str) -> str:
        match = _TURNO.search(texto)
        return match.group(1).upper() if match else ""

    @staticmethod
    def _extraer_tamano(texto: str) -> str:
        match = _TAMANO.search(texto)
        return f"{match.group(1)}+{match.group(2)}" if match else ""

    @staticmethod
    def _descripcion(texto: str, unidad: str) -> str:
        """What they actually asked for, minus the unit, date and filler words.

        Kept close to the original wording because it goes straight into the
        Service Description column ("full tub", "cc pm", "flat paint only").
        """
        resto = texto[len(texto.split()[0]):] if texto.split() else texto
        resto = _FECHA.sub(" ", resto)
        resto = _RUIDO.sub(" ", resto)
        resto = _VACANTE.sub(" ", resto)
        resto = _OCUPADA.sub(" ", resto)
        resto = re.sub(r"[,;.]+", " ", resto)
        resto = re.sub(r"\s+", " ", resto).strip(" -:")
        return resto or unidad


def parse_us_date(texto: str, hoy: Optional[date] = None) -> str:
    """Parse a US-style m/d/yy date. Returns the raw text if unparseable."""
    parser = PropertyEmailParser()
    resultado = parser._extraer_fecha(texto, hoy)
    return resultado or texto


def fecha_iso_a_corta(iso: str) -> str:
    """2026-08-08 -> 8/8/26, the format used in the tracking spreadsheet."""
    try:
        parsed = datetime.strptime(iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return iso or ""
    return f"{parsed.month}/{parsed.day}/{parsed.strftime('%y')}"
