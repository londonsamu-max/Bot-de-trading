"""
CSV-backed inventory.

data/inventario.csv columns:
    sku,nombre,unidad,stock,reservado,minimo

`stock`    physical units on the shelf
`reservado` units already committed to dispatched jobs (still on the shelf)
`disponible` = stock - reservado  (what a new job can take)

Writes are atomic so an interrupted run cannot leave a half-written CSV.
"""

import csv
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.workflow.models import ItemPedido
from src.workflow.parser import strip_accents

logger = logging.getLogger(__name__)

CAMPOS = ["sku", "nombre", "unidad", "stock", "reservado", "minimo"]


@dataclass
class ArticuloInventario:
    sku: str
    nombre: str
    unidad: str = "u"
    stock: float = 0.0
    reservado: float = 0.0
    minimo: float = 0.0

    @property
    def disponible(self) -> float:
        return max(0.0, self.stock - self.reservado)

    @property
    def bajo_minimo(self) -> bool:
        return self.minimo > 0 and (self.stock - self.reservado) <= self.minimo


class Inventory:
    """Load, match and reserve inventory lines."""

    def __init__(self, path: str = "data/inventario.csv"):
        self.path = Path(path)
        self.articulos: dict[str, ArticuloInventario] = {}
        self.load()

    def load(self) -> None:
        self.articulos = {}
        if not self.path.exists():
            logger.warning("No existe %s; el inventario queda vacío", self.path)
            return
        with self.path.open(encoding="utf-8-sig", newline="") as f:
            for fila in csv.DictReader(f):
                articulo = self._row_to_articulo(fila)
                if articulo:
                    self.articulos[articulo.sku] = articulo
        logger.info("Inventario cargado: %d artículos desde %s",
                    len(self.articulos), self.path)

    def _row_to_articulo(self, fila: dict) -> Optional[ArticuloInventario]:
        normalizada = {strip_accents(k or "").strip(): (v or "").strip()
                       for k, v in fila.items()}
        sku = normalizada.get("sku") or normalizada.get("codigo")
        nombre = normalizada.get("nombre") or normalizada.get("descripcion")
        if not sku and not nombre:
            return None
        if not sku:
            sku = strip_accents(nombre).replace(" ", "-")[:24].upper()
        try:
            return ArticuloInventario(
                sku=sku,
                nombre=nombre or sku,
                unidad=normalizada.get("unidad") or "u",
                stock=_to_float(normalizada.get("stock")),
                reservado=_to_float(normalizada.get("reservado")),
                minimo=_to_float(normalizada.get("minimo")),
            )
        except ValueError:
            logger.warning("Fila de inventario inválida, se omite: %s", fila)
            return None

    # --- matching -----------------------------------------------------------

    def match(self, descripcion: str) -> Optional[ArticuloInventario]:
        """Find the inventory article that best matches a free-text description."""
        objetivo = strip_accents(descripcion).strip()
        if not objetivo:
            return None

        for articulo in self.articulos.values():
            if strip_accents(articulo.sku) == objetivo:
                return articulo

        for articulo in self.articulos.values():
            if strip_accents(articulo.nombre) == objetivo:
                return articulo

        # Word-overlap score: every word of the shorter name must appear in the
        # other, so "pintura blanca" matches "Pintura blanca 5 gal" but not "Brocha".
        mejor: Optional[ArticuloInventario] = None
        mejor_score = 0.0
        palabras_objetivo = set(objetivo.split())
        for articulo in self.articulos.values():
            palabras_articulo = set(strip_accents(articulo.nombre).split())
            comunes = palabras_objetivo & palabras_articulo
            if not comunes:
                continue
            score = len(comunes) / max(1, min(len(palabras_objetivo), len(palabras_articulo)))
            if score > mejor_score:
                mejor, mejor_score = articulo, score
        return mejor if mejor_score >= 0.6 else None

    def check(self, items: list[ItemPedido]) -> list[ItemPedido]:
        """Annotate each item with sku/disponible/faltante. Mutates and returns the list."""
        for item in items:
            articulo = self.match(item.sku or item.descripcion)
            if not articulo:
                item.sku = item.sku or None
                item.disponible = 0.0
                item.faltante = item.cantidad
                continue
            item.sku = articulo.sku
            item.disponible = articulo.disponible
            item.faltante = max(0.0, item.cantidad - articulo.disponible)
            if item.unidad == "u" and articulo.unidad:
                item.unidad = articulo.unidad
        return items

    # --- mutations ----------------------------------------------------------

    def reservar(self, items: list[ItemPedido],
                 estricto: bool = False) -> tuple[bool, list[str]]:
        """Hold material for a job. Records per item how much was actually held.

        By default a shortage is partial: whatever exists is reserved and the
        rest is reported, so a crew can leave with the paint even if the tape
        ran out. With `estricto=True` any shortage writes nothing at all.

        Returns (todo_cubierto, problemas).
        """
        problemas: list[str] = []
        plan: list[tuple[ItemPedido, ArticuloInventario, float]] = []
        # Track reservations made inside this call so two lines pointing at the
        # same article don't both see the original availability.
        comprometido: dict[str, float] = {}

        for item in items:
            articulo = self.articulos.get(item.sku or "") or self.match(item.descripcion)
            if not articulo:
                problemas.append(f"{item.descripcion}: no está en el inventario")
                continue

            libre = max(0.0, articulo.disponible - comprometido.get(articulo.sku, 0.0))
            if libre < item.cantidad:
                problemas.append(
                    f"{articulo.nombre}: se piden {_fmt(item.cantidad)} y hay {_fmt(libre)}"
                )
                if estricto or libre <= 0:
                    continue
                cantidad = libre
            else:
                cantidad = item.cantidad

            comprometido[articulo.sku] = comprometido.get(articulo.sku, 0.0) + cantidad
            plan.append((item, articulo, cantidad))

        if problemas and estricto:
            return False, problemas

        for item, articulo, cantidad in plan:
            articulo.reservado += cantidad
            item.sku = articulo.sku
            item.reservado = cantidad
            item.faltante = max(0.0, item.cantidad - cantidad)
            item.disponible = articulo.disponible
        self.save()
        return not problemas, problemas

    def liberar(self, items: list[ItemPedido]) -> None:
        """Undo a reservation (job cancelled)."""
        for item in items:
            articulo = self.articulos.get(item.sku or "")
            if articulo and item.reservado:
                articulo.reservado = max(0.0, articulo.reservado - item.reservado)
            item.reservado = 0.0
        self.save()

    def consumir(self, items: list[ItemPedido]) -> None:
        """Job finished: the reserved material physically left the warehouse."""
        for item in items:
            articulo = self.articulos.get(item.sku or "")
            if not articulo or not item.reservado:
                continue
            articulo.reservado = max(0.0, articulo.reservado - item.reservado)
            articulo.stock = max(0.0, articulo.stock - item.reservado)
            item.reservado = 0.0
        self.save()

    def bajo_minimo(self) -> list[ArticuloInventario]:
        """Articles at or below their reorder point."""
        return [a for a in self.articulos.values() if a.bajo_minimo]

    def save(self) -> None:
        """Atomically rewrite the CSV."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CAMPOS)
            writer.writeheader()
            for articulo in self.articulos.values():
                writer.writerow({
                    "sku": articulo.sku,
                    "nombre": articulo.nombre,
                    "unidad": articulo.unidad,
                    "stock": _fmt(articulo.stock),
                    "reservado": _fmt(articulo.reservado),
                    "minimo": _fmt(articulo.minimo),
                })
        os.replace(tmp, self.path)


def _to_float(value: Optional[str]) -> float:
    if not value:
        return 0.0
    return float(str(value).replace(",", "."))


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"
