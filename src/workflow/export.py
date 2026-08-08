"""
Export to the tracking spreadsheet.

Produces exactly the columns already in use, in the same order, so the file
opens in Excel and can be pasted straight into the existing sheet:

    DATE | Service | Mgmt CO. | Property Name | Person | Unit | Size |
    Service Description

"Person" is the crew that confirmed attendance. A job with two confirmed
workers becomes two rows, which is how the sheet already records shared jobs.
"""

import csv
import logging
import os
from pathlib import Path
from typing import Iterable, Optional

from src.workflow.models import EstadoTrabajo, Trabajo
from src.workflow.property_parser import fecha_iso_a_corta

logger = logging.getLogger(__name__)

COLUMNAS = ["DATE", "Service", "Mgmt CO.", "Property Name", "Person", "Unit",
            "Size", "Service Description"]

# Extra columns the sheet does not have but that make the export auditable.
COLUMNAS_EXTRA = ["Folio", "Estado", "Turno", "Ocupada", "Material"]


def filas(trabajos: Iterable[Trabajo], incluir_extra: bool = False) -> list[dict]:
    """Build one row per confirmed worker (or one row if nobody confirmed yet)."""
    resultado: list[dict] = []

    for trabajo in trabajos:
        personas = [c.nombre for c in trabajo.confirmados()] or [""]
        for persona in personas:
            fila = {
                "DATE": fecha_iso_a_corta(trabajo.fecha_servicio),
                "Service": trabajo.servicio,
                "Mgmt CO.": trabajo.empresa_gestion,
                "Property Name": trabajo.propiedad,
                "Person": persona,
                "Unit": trabajo.unidad,
                "Size": trabajo.tamano,
                "Service Description": trabajo.descripcion_servicio,
            }
            if incluir_extra:
                fila.update({
                    "Folio": trabajo.id,
                    "Estado": trabajo.estado,
                    "Turno": trabajo.turno,
                    "Ocupada": "si" if trabajo.ocupada else "no",
                    "Material": "; ".join(str(i) for i in trabajo.items),
                })
            resultado.append(fila)

    return resultado


def seleccionar(trabajos: Iterable[Trabajo], desde: str = "", hasta: str = "",
                solo_asignados: bool = False) -> list[Trabajo]:
    """Filter by service date (ISO yyyy-mm-dd) and, optionally, by state."""
    elegidos = []
    for trabajo in trabajos:
        if solo_asignados and trabajo.estado not in (EstadoTrabajo.ASIGNADO.value,
                                                     EstadoTrabajo.COMPLETADO.value):
            continue
        fecha = trabajo.fecha_servicio or ""
        if desde and (not fecha or fecha < desde):
            continue
        if hasta and (not fecha or fecha > hasta):
            continue
        elegidos.append(trabajo)

    # Same order as the sheet: by date, then property, then unit.
    elegidos.sort(key=lambda t: (t.fecha_servicio or "9999",
                                 t.propiedad.lower(), t.unidad.lower()))
    return elegidos


def escribir_csv(trabajos: Iterable[Trabajo], destino: str,
                 incluir_extra: bool = False) -> int:
    """Write the export. Returns the number of rows written."""
    columnas = COLUMNAS + (COLUMNAS_EXTRA if incluir_extra else [])
    datos = filas(trabajos, incluir_extra=incluir_extra)

    ruta = Path(destino)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")

    # utf-8-sig so Excel shows accents correctly on a double click.
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columnas)
        writer.writeheader()
        writer.writerows(datos)
    os.replace(tmp, ruta)

    logger.info("Exportadas %d filas a %s", len(datos), ruta)
    return len(datos)


def tabla_texto(trabajos: Iterable[Trabajo], limite: Optional[int] = None) -> str:
    """Same columns rendered for the terminal."""
    datos = filas(trabajos)
    if limite:
        datos = datos[:limite]
    if not datos:
        return "Sin trabajos que exportar."

    anchos = {c: max(len(c), *(len(str(d[c])) for d in datos)) for c in COLUMNAS}
    lineas = [" | ".join(c.ljust(anchos[c]) for c in COLUMNAS)]
    lineas.append("-+-".join("-" * anchos[c] for c in COLUMNAS))
    for fila in datos:
        lineas.append(" | ".join(str(fila[c]).ljust(anchos[c]) for c in COLUMNAS))
    return "\n".join(lineas)
