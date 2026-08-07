"""
WhatsApp inbound queue (append-only JSONL).

The webhook process and the CLI only append; the runner consumes from a byte
offset stored in the workflow state. That keeps two processes off the same
JSON document, so no locking is needed and nothing is lost if the runner is
stopped while answers keep arriving.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from src.workflow.models import utc_now

logger = logging.getLogger(__name__)


class WhatsAppInbox:
    """Append-only file of inbound WhatsApp answers."""

    def __init__(self, path: str = "data/whatsapp_inbox.jsonl"):
        self.path = Path(path)

    def append(self, telefono: str, texto: str, origen: str = "manual",
               job_id: Optional[str] = None) -> None:
        """Queue one inbound answer."""
        registro = {
            "recibido_at": utc_now(),
            "telefono": telefono,
            "texto": texto,
            "origen": origen,
            "job_id": job_id,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
        logger.info("Respuesta encolada de %s (%s)", telefono, origen)

    def consume(self, offset: int = 0) -> tuple[list[dict], int]:
        """Read everything appended after `offset`. Returns (registros, nuevo_offset)."""
        if not self.path.exists():
            return [], 0

        tamano = self.path.stat().st_size
        if offset > tamano:
            # File was rotated or truncated: start over rather than skip everything.
            logger.warning("El archivo %s se truncó; se relee desde el inicio", self.path)
            offset = 0

        registros: list[dict] = []
        with self.path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            for linea in f:
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    registros.append(json.loads(linea))
                except json.JSONDecodeError:
                    logger.warning("Línea inválida en %s: %s", self.path, linea[:120])
            nuevo_offset = f.tell()

        return registros, nuevo_offset
