"""
Reference tables that turn a unit line into a complete spreadsheet row.

Three CSVs, all optional and all editable in Excel:

    data/propiedades.csv  quién escribe -> Mgmt CO. + Property Name + dirección
    data/unidades.csv     propiedad + unidad -> Size (1+1, 3+2...)
    data/consumos.csv     servicio + Size -> material que se lleva la cuadrilla

The unit emails carry none of this: "B109 vacant, full tub. 8/8/26" says
nothing about who manages the property, how big B109 is, or how much paint it
takes. These tables are where that knowledge lives.
"""

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.workflow.models import ItemPedido, strip_key
from src.workflow.parser import strip_accents

logger = logging.getLogger(__name__)


@dataclass
class Propiedad:
    """An apartment complex and the company that manages it."""

    propiedad: str
    empresa_gestion: str = ""
    dominio: str = ""
    contacto: str = ""
    direccion: str = ""
    zona: str = ""
    alias: list[str] = field(default_factory=list)

    def nombres(self) -> list[str]:
        return [self.propiedad, *self.alias]


class PropertyDirectory:
    """Maps an incoming sender to the property and management company."""

    def __init__(self, path: str = "data/propiedades.csv"):
        self.path = Path(path)
        self.propiedades: list[Propiedad] = []
        self.load()

    def load(self) -> None:
        self.propiedades = []
        if not self.path.exists():
            logger.info("No existe %s; no se reconocerán empresas de gestión", self.path)
            return

        with self.path.open(encoding="utf-8-sig", newline="") as f:
            for fila in csv.DictReader(f):
                propiedad = self._row(fila)
                if propiedad:
                    self.propiedades.append(propiedad)
        logger.info("Propiedades cargadas: %d desde %s", len(self.propiedades), self.path)

    @staticmethod
    def _row(fila: dict) -> Optional[Propiedad]:
        datos = {strip_accents(k or "").strip(): (v or "").strip()
                 for k, v in fila.items()}
        nombre = datos.get("propiedad") or datos.get("property")
        if not nombre:
            return None
        alias = [a.strip() for a in (datos.get("alias", "")).split("|") if a.strip()]
        return Propiedad(
            propiedad=nombre,
            empresa_gestion=datos.get("empresa_gestion") or datos.get("mgmt") or "",
            dominio=(datos.get("dominio") or "").lower().lstrip("@"),
            contacto=(datos.get("contacto") or "").lower(),
            direccion=datos.get("direccion", ""),
            zona=datos.get("zona", ""),
            alias=alias,
        )

    def match(self, remitente: str, cuerpo: str = "",
              asunto: str = "") -> Optional[Propiedad]:
        """Identify the property from the sender, then from the text."""
        remitente = (remitente or "").lower().strip()

        for propiedad in self.propiedades:
            if propiedad.contacto and propiedad.contacto == remitente:
                return propiedad

        dominio = remitente.partition("@")[2]
        if dominio:
            for propiedad in self.propiedades:
                if propiedad.dominio and dominio.endswith(propiedad.dominio):
                    return propiedad

        # Same management company writing from an unlisted mailbox: the property
        # name is usually in the subject or the signature.
        texto = strip_accents(f"{asunto}\n{cuerpo}")
        mejor: Optional[Propiedad] = None
        for propiedad in self.propiedades:
            for nombre in propiedad.nombres():
                normalizado = strip_accents(nombre).strip()
                if len(normalizado) >= 4 and normalizado in texto:
                    if mejor is None or len(normalizado) > len(strip_accents(mejor.propiedad)):
                        mejor = propiedad
        return mejor

    def conocido(self, remitente: str) -> bool:
        """True if this sender is a management company we already work with."""
        remitente = (remitente or "").lower().strip()
        dominio = remitente.partition("@")[2]
        return any(
            (p.contacto and p.contacto == remitente)
            or (p.dominio and dominio and dominio.endswith(p.dominio))
            for p in self.propiedades
        )


class UnitSizes:
    """Unit -> size (1+1, 3+2), so the crew knows how much material to take."""

    def __init__(self, path: str = "data/unidades.csv"):
        self.path = Path(path)
        self.tamanos: dict[tuple[str, str], str] = {}
        self.load()

    def load(self) -> None:
        self.tamanos = {}
        if not self.path.exists():
            logger.info("No existe %s; los tamaños quedarán vacíos", self.path)
            return

        with self.path.open(encoding="utf-8-sig", newline="") as f:
            for fila in csv.DictReader(f):
                datos = {strip_accents(k or "").strip(): (v or "").strip()
                         for k, v in fila.items()}
                propiedad = datos.get("propiedad", "")
                unidad = datos.get("unidad", "")
                tamano = datos.get("tamano") or datos.get("size", "")
                if unidad and tamano:
                    self.tamanos[(strip_key(propiedad), strip_key(unidad))] = tamano
        logger.info("Tamaños de unidad cargados: %d desde %s",
                    len(self.tamanos), self.path)

    def get(self, propiedad: str, unidad: str) -> str:
        clave = (strip_key(propiedad), strip_key(unidad))
        if clave in self.tamanos:
            return self.tamanos[clave]
        # A unit number listed without a property still helps.
        return self.tamanos.get(("", strip_key(unidad)), "")


class ServiceMaterials:
    """Service + size -> the material the crew has to pick up.

    A row with size "*" applies to every size, so the common items (brushes,
    plastic, shoe covers) are declared once.
    """

    def __init__(self, path: str = "data/consumos.csv"):
        self.path = Path(path)
        self.reglas: dict[str, list[tuple[str, str, float]]] = {}
        self.load()

    def load(self) -> None:
        self.reglas = {}
        if not self.path.exists():
            logger.info("No existe %s; no se asignará material automáticamente",
                        self.path)
            return

        with self.path.open(encoding="utf-8-sig", newline="") as f:
            for fila in csv.DictReader(f):
                datos = {strip_accents(k or "").strip(): (v or "").strip()
                         for k, v in fila.items()}
                servicio = strip_key(datos.get("servicio", ""))
                sku = datos.get("sku", "")
                if not servicio or not sku:
                    continue
                try:
                    cantidad = float((datos.get("cantidad") or "1").replace(",", "."))
                except ValueError:
                    logger.warning("Cantidad inválida en consumos.csv: %s", fila)
                    continue
                tamano = strip_key(datos.get("tamano") or datos.get("size") or "*") or "*"
                self.reglas.setdefault(servicio, []).append((tamano, sku, cantidad))

        total = sum(len(v) for v in self.reglas.values())
        logger.info("Reglas de consumo cargadas: %d desde %s", total, self.path)

    def para(self, servicio: str, tamano: str) -> list[ItemPedido]:
        """Material for one job. Size-specific rows win over the '*' fallback."""
        reglas = self.reglas.get(strip_key(servicio), [])
        if not reglas:
            return []

        objetivo = strip_key(tamano)
        por_sku: dict[str, tuple[bool, float]] = {}
        for regla_tamano, sku, cantidad in reglas:
            especifica = regla_tamano != "*"
            if especifica and regla_tamano != objetivo:
                continue
            anterior = por_sku.get(sku)
            # Keep the size-specific quantity when both kinds of row exist.
            if anterior is None or (especifica and not anterior[0]):
                por_sku[sku] = (especifica, cantidad)

        return [ItemPedido(descripcion=sku, cantidad=cantidad, sku=sku)
                for sku, (_, cantidad) in por_sku.items()]
