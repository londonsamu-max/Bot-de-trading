"""
Client email -> Trabajo parser.

Clients rarely write structured emails, so the parser works in two layers:

1. Labelled fields ("Cliente:", "Dirección:", "Materiales:") in any order,
   accent- and case-insensitive, with configurable aliases.
2. Free text fallback: the whole body becomes the description and any line
   that looks like "3 x Pintura" is picked up as a material.

Also parses worker answers ("SI" / "NO") coming back by email or WhatsApp.
"""

import logging
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Optional

from src.workflow.models import ItemPedido

logger = logging.getLogger(__name__)

# Field name -> accepted labels in the email body.
FIELD_ALIASES: dict[str, list[str]] = {
    "cliente": ["cliente", "client", "customer", "nombre", "nombre del cliente"],
    "direccion": ["direccion", "address", "domicilio", "ubicacion", "lugar", "sitio"],
    "fecha": ["fecha", "date", "fecha de servicio", "fecha del servicio", "dia", "día"],
    "hora": ["hora", "time", "horario", "hora de llegada"],
    "zona": ["zona", "zone", "area", "ciudad", "city", "sector"],
    "habilidades": ["habilidades", "skills", "especialidad", "oficio",
                    "tipo de trabajo", "servicio", "trade"],
    "trabajadores": ["trabajadores", "personal", "workers", "cuadrilla", "crew",
                     "cantidad de personal", "no. de trabajadores"],
    "items": ["materiales", "material", "insumos", "inventario", "items",
              "articulos", "productos", "supplies", "materials"],
    "descripcion": ["descripcion", "detalle", "detalles", "trabajo", "description",
                    "notas", "observaciones", "scope"],
    "telefono": ["telefono", "tel", "phone", "celular", "movil", "contacto"],
}

# Units recognised in a material line. Anything else after the quantity is
# treated as part of the description ("3 Pintura" -> 3 u of "Pintura").
UNIDADES = {
    "u", "un", "und", "uds", "pza", "pzas", "pieza", "piezas", "kg", "g", "lb",
    "l", "lt", "ml", "gal", "m", "m2", "m3", "cm", "mm", "ft", "pie", "pies",
    "caja", "cajas", "rollo", "rollos", "bolsa", "bolsas", "saco", "sacos",
    "par", "pares", "juego", "set", "cubeta", "cubetas", "galon", "galones",
}

JOB_ID_PATTERN = re.compile(r"\b(JB-\d{8}-\d{3})\b", re.IGNORECASE)

# "y" is deliberately absent: in Spanish it means "and", not "yes", and a false
# confirmation staffs a job with somebody who never agreed to go.
_AFIRMATIVAS = {
    "si", "s", "sii", "siii", "yes", "ok", "oka", "okay", "vale", "dale",
    "va", "claro", "listo", "confirmo", "confirmado", "confirmada", "acepto",
    "aceptado", "voy", "asisto", "cuenten", "presente", "1", "👍", "✅",
}
_NEGATIVAS = {
    "no", "n", "nop", "nel", "nope", "niego", "rechazo", "imposible",
    "negativo", "cancelo", "2", "👎", "❌",
}
# Multi-word answers checked before the single-token lookup, negatives first.
_FRASES_NEGATIVAS = [
    "no puedo", "no voy", "no asisto", "no me queda", "no alcanzo", "no podre",
    "no podré", "esta vez no", "hoy no", "cant make", "can't make",
]
_FRASES_AFIRMATIVAS = [
    "ahi estare", "ahí estaré", "ahi voy", "cuenta conmigo", "cuenten conmigo",
    "si puedo", "si voy", "si asisto", "confirmo asistencia", "ahi nos vemos",
]


def strip_accents(text: str) -> str:
    """Lowercase and remove accents, so 'Dirección' matches 'direccion'."""
    normalized = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn")


def _build_label_lookup(aliases: dict[str, list[str]]) -> dict[str, str]:
    """Flatten {campo: [alias...]} into {alias_normalizado: campo}."""
    lookup: dict[str, str] = {}
    for campo, etiquetas in aliases.items():
        for etiqueta in etiquetas:
            lookup[strip_accents(etiqueta).strip()] = campo
    return lookup


def parse_quantity_line(linea: str) -> Optional[ItemPedido]:
    """Parse one material line into an ItemPedido, or None if it isn't one.

    Recognised shapes:
        3 x Pintura blanca      2 cubetas Sellador       - 4 Tornillos M8
        Pintura blanca x 3      Sellador: 2 galones      SKU-123 x 4
    """
    texto = linea.strip().lstrip("-*•·+>").strip()
    if not texto:
        return None

    # Optional SKU prefix: "SKU-123 x 4 Pintura" or "A-1099: 2"
    sku = None
    sku_match = re.match(r"^([A-Z]{1,6}[-_]?\d{2,8})\s*[:xX\-]\s*(.+)$", texto)
    if sku_match:
        sku, texto = sku_match.group(1), sku_match.group(2).strip()
        # "SKU-123 x 4": the code is the description and the rest is the quantity.
        solo_cantidad = re.fullmatch(
            r"(?P<cant>\d+(?:[.,]\d+)?)\s*(?P<unidad>[a-zA-Z]{1,8})?\.?", texto
        )
        if solo_cantidad:
            unidad = (solo_cantidad.group("unidad") or "u").lower()
            return ItemPedido(
                descripcion=sku,
                cantidad=_to_float(solo_cantidad.group("cant")),
                unidad=strip_accents(unidad) if strip_accents(unidad) in UNIDADES else "u",
                sku=sku,
            )

    # Shape A: quantity first  ->  "3 x Pintura", "2 cubetas Sellador", "4 Tornillos"
    match = re.match(
        r"^(?P<cant>\d+(?:[.,]\d+)?)\s*(?:x|X|\*)?\s*(?P<resto>.+)$", texto
    )
    if match:
        resto = match.group("resto").strip()
        unidad, descripcion = _split_unit(resto)
        if descripcion:
            return ItemPedido(
                descripcion=descripcion,
                cantidad=_to_float(match.group("cant")),
                unidad=unidad,
                sku=sku,
            )

    # Shape B: quantity last  ->  "Pintura x 3", "Sellador: 2 galones"
    match = re.match(
        r"^(?P<desc>.+?)\s*(?:x|X|:|=|-)\s*(?P<cant>\d+(?:[.,]\d+)?)\s*(?P<unidad>[a-zA-Z]{1,8})?\.?$",
        texto,
    )
    if match:
        unidad = (match.group("unidad") or "u").lower()
        if unidad != "u" and strip_accents(unidad) not in UNIDADES:
            return None  # trailing word isn't a unit -> probably not a material line
        descripcion = match.group("desc").strip(" .:-")
        if descripcion:
            return ItemPedido(
                descripcion=descripcion,
                cantidad=_to_float(match.group("cant")),
                unidad=strip_accents(unidad),
                sku=sku,
            )

    # No quantity anywhere: a bare material name still counts as 1 unit,
    # but only if it is short enough to be an item and not a sentence.
    if sku or (len(texto.split()) <= 6 and not texto.endswith((".", "!", "?"))):
        return ItemPedido(descripcion=texto, cantidad=1.0, unidad="u", sku=sku)

    return None


def _split_unit(resto: str) -> tuple[str, str]:
    """Split "cubetas Sellador" into ("cubetas", "Sellador"). Defaults to unit "u"."""
    partes = resto.split(None, 1)
    if len(partes) == 2 and strip_accents(partes[0].rstrip(".")) in UNIDADES:
        return strip_accents(partes[0].rstrip(".")), partes[1].strip()
    return "u", resto.strip()


def _to_float(value: str) -> float:
    return float(value.replace(",", "."))


def parse_date(texto: str, hoy: Optional[date] = None) -> str:
    """Parse a service date into ISO format. Returns the raw text if unparseable."""
    texto = texto.strip()
    if not texto:
        return ""
    hoy = hoy or date.today()
    normalizado = strip_accents(texto)

    relativos = {"hoy": 0, "manana": 1, "pasado manana": 2}
    for palabra, offset in relativos.items():
        if normalizado.startswith(palabra):
            return (hoy + timedelta(days=offset)).isoformat()

    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y",
                    "%m/%d/%Y", "%d.%m.%Y", "%d/%m"):
        try:
            parsed = datetime.strptime(texto, formato).date()
        except ValueError:
            continue
        if formato == "%d/%m":  # no year given -> assume the upcoming one
            parsed = parsed.replace(year=hoy.year)
            if parsed < hoy:
                parsed = parsed.replace(year=hoy.year + 1)
        return parsed.isoformat()

    return texto


def parse_answer(texto: str) -> Optional[bool]:
    """Interpret a worker's reply. True = confirms, False = declines, None = unclear."""
    if not texto:
        return None

    # Only the first line matters: email replies quote the original message below.
    primera_linea = ""
    for linea in texto.splitlines():
        limpia = linea.strip()
        if limpia and not limpia.startswith((">", "|", "El ", "On ")):
            primera_linea = limpia
            break
    if not primera_linea:
        return None

    normalizado = strip_accents(primera_linea)
    normalizado = re.sub(r"[^\w\s👍✅👎❌]", " ", normalizado, flags=re.UNICODE)
    normalizado = re.sub(r"\s+", " ", normalizado).strip()
    if not normalizado:
        # Emoji-only answers survive here (regex above keeps the four we know).
        normalizado = primera_linea.strip()

    # Negatives first: "no puedo" contains no affirmative token, but "si no puedo" does.
    for frase in _FRASES_NEGATIVAS:
        if strip_accents(frase) in normalizado:
            return False
    for frase in _FRASES_AFIRMATIVAS:
        if strip_accents(frase) in normalizado:
            return True

    # Only the opening words decide: further in, "si" is usually a conditional
    # ("voy si consigo la escalera") rather than an answer.
    tokens = normalizado.split()
    for token in tokens[:2]:
        if token in _NEGATIVAS:
            return False
        if token in _AFIRMATIVAS:
            return True
    return None


def extract_job_id(texto: str) -> Optional[str]:
    """Find a job code (JB-YYYYMMDD-NNN) inside a subject line or message body."""
    match = JOB_ID_PATTERN.search(texto or "")
    return match.group(1).upper() if match else None


class EmailJobParser:
    """Turns the body of a client email into the fields of a Trabajo."""

    def __init__(self, config: Optional[dict] = None):
        config = config or {}
        aliases = dict(FIELD_ALIASES)
        for campo, extra in (config.get("etiquetas_extra") or {}).items():
            aliases.setdefault(campo, [])
            aliases[campo] = aliases[campo] + list(extra)
        self._lookup = _build_label_lookup(aliases)
        self.trabajadores_por_defecto = int(config.get("trabajadores_por_defecto", 1))
        self.max_items = int(config.get("max_items", 50))

    def parse(self, asunto: str, cuerpo: str, remitente_nombre: str = "",
              remitente_email: str = "") -> dict:
        """Extract job fields from an email. Always returns a usable dict."""
        campos, bloques = self._extract_fields(cuerpo)

        items = self._extract_items(bloques.get("items", []))
        if not items:
            items = self._scan_free_text_items(cuerpo)

        descripcion = campos.get("descripcion") or self._fallback_description(cuerpo, asunto)

        habilidades = [
            h.strip() for h in re.split(r"[,/;]", campos.get("habilidades", "")) if h.strip()
        ]

        trabajadores = self.trabajadores_por_defecto
        if campos.get("trabajadores"):
            numeros = re.findall(r"\d+", campos["trabajadores"])
            if numeros:
                trabajadores = max(1, int(numeros[0]))

        return {
            "cliente_nombre": campos.get("cliente") or remitente_nombre or remitente_email,
            "cliente_email": remitente_email,
            "asunto": asunto,
            "descripcion": descripcion,
            "direccion": campos.get("direccion", ""),
            "zona": campos.get("zona", ""),
            "fecha_servicio": parse_date(campos.get("fecha", "")),
            "hora_servicio": campos.get("hora", ""),
            "habilidades": habilidades,
            "trabajadores_requeridos": trabajadores,
            "items": items[: self.max_items],
        }

    # --- internals ----------------------------------------------------------

    def _extract_fields(self, cuerpo: str) -> tuple[dict[str, str], dict[str, list[str]]]:
        """Split the body into single-line fields and multi-line blocks (materials)."""
        campos: dict[str, str] = {}
        bloques: dict[str, list[str]] = {}
        bloque_activo: Optional[str] = None

        for linea in (cuerpo or "").splitlines():
            if not linea.strip():
                bloque_activo = None
                continue

            campo, valor = self._match_label(linea)
            if campo:
                if campo in ("items", "descripcion") and not valor:
                    # "Materiales:" alone -> the following lines belong to the block.
                    bloque_activo = campo
                    bloques.setdefault(campo, [])
                    continue
                bloque_activo = "items" if campo == "items" else None
                if campo == "items":
                    bloques.setdefault("items", []).append(valor)
                elif campo not in campos:
                    campos[campo] = valor
                continue

            if bloque_activo:
                bloques.setdefault(bloque_activo, []).append(linea.strip())

        for campo, lineas in bloques.items():
            if campo != "items":
                campos.setdefault(campo, "\n".join(lineas).strip())

        return campos, bloques

    def _match_label(self, linea: str) -> tuple[Optional[str], str]:
        """If the line starts with a known label, return (campo, valor)."""
        if ":" not in linea:
            return None, ""
        etiqueta, _, valor = linea.partition(":")
        clave = strip_accents(etiqueta).strip(" -*•·\t")
        if len(clave) > 40:
            return None, ""
        campo = self._lookup.get(clave)
        return (campo, valor.strip()) if campo else (None, "")

    def _extract_items(self, lineas: list[str]) -> list[ItemPedido]:
        items: list[ItemPedido] = []
        for linea in lineas:
            # A single-line "Materiales: 3 pintura, 2 brochas" holds several items.
            piezas = re.split(r"[,;]|\s+y\s+", linea) if len(linea) < 200 else [linea]
            for pieza in piezas:
                item = parse_quantity_line(pieza)
                if item:
                    items.append(item)
        return items

    def _scan_free_text_items(self, cuerpo: str) -> list[ItemPedido]:
        """No 'Materiales:' block: pick up bullet lines that look like materials."""
        items: list[ItemPedido] = []
        for linea in (cuerpo or "").splitlines():
            limpia = linea.strip()
            if not limpia.startswith(("-", "*", "•", "·")):
                continue
            if self._match_label(limpia)[0]:
                continue
            item = parse_quantity_line(limpia)
            if item:
                items.append(item)
        return items

    def _fallback_description(self, cuerpo: str, asunto: str) -> str:
        """Body lines that aren't labels or bullets; falls back to the subject."""
        utiles = []
        for linea in (cuerpo or "").splitlines():
            limpia = linea.strip()
            if not limpia or limpia.startswith((">", "-", "*", "•", "·")):
                continue
            if self._match_label(limpia)[0]:
                continue
            if strip_accents(limpia).startswith(("saludos", "gracias", "atte", "enviado desde")):
                break
            utiles.append(limpia)
        return "\n".join(utiles).strip() or asunto
