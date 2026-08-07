"""
Workflow state persistence (JSON file).

Holds every job the bot has seen, which email message-ids were already
processed, and how far the WhatsApp inbox has been consumed. Writes are
atomic (temp file + os.replace) so a crash mid-write cannot corrupt state.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from src.workflow.models import Trabajo, utc_now

logger = logging.getLogger(__name__)


class WorkflowStore:
    """Load/save the workflow state document."""

    def __init__(self, path: str = "data/workflow_state.json"):
        self.path = Path(path)
        self.trabajos: dict[str, Trabajo] = {}
        self.mensajes_procesados: set[str] = set()
        self.whatsapp_offset: int = 0   # bytes already consumed from the inbox file
        self.actualizado_at: str = ""
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            logger.info("Estado nuevo, no existe %s", self.path)
            return
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("No se pudo leer %s (%s). Se empieza con estado vacío.", self.path, e)
            return

        self.trabajos = {
            job_id: Trabajo.from_dict(job)
            for job_id, job in data.get("trabajos", {}).items()
        }
        self.mensajes_procesados = set(data.get("mensajes_procesados", []))
        self.whatsapp_offset = int(data.get("whatsapp_offset", 0))
        self.actualizado_at = data.get("actualizado_at", "")
        logger.info("Estado cargado: %d trabajos", len(self.trabajos))

    def save(self) -> None:
        """Atomically write state to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "actualizado_at": utc_now(),
            "trabajos": {job_id: job.to_dict() for job_id, job in self.trabajos.items()},
            # Keep the tail only: enough to stay idempotent without growing forever.
            "mensajes_procesados": sorted(self.mensajes_procesados)[-2000:],
            "whatsapp_offset": self.whatsapp_offset,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # --- jobs ---------------------------------------------------------------

    def add(self, trabajo: Trabajo) -> None:
        self.trabajos[trabajo.id] = trabajo

    def get(self, job_id: str) -> Optional[Trabajo]:
        return self.trabajos.get(job_id)

    def by_estado(self, *estados: str) -> list[Trabajo]:
        return [t for t in self.trabajos.values() if t.estado in estados]

    def next_job_id(self, fecha: str) -> str:
        """Build the next sequential job id for a date: JB-YYYYMMDD-001."""
        prefijo = f"JB-{fecha}-"
        usados = [
            int(job_id[len(prefijo):])
            for job_id in self.trabajos
            if job_id.startswith(prefijo) and job_id[len(prefijo):].isdigit()
        ]
        return f"{prefijo}{max(usados, default=0) + 1:03d}"

    def find_pending_for_phone(self, telefono: str) -> Optional[tuple[Trabajo, str]]:
        """Most recent job with a pending attendance request for that phone number.

        Used when a worker answers "SI" on WhatsApp without quoting the job code.
        """
        candidatos: list[tuple[str, Trabajo, str]] = []
        for trabajo in self.trabajos.values():
            for worker_id, convocatoria in trabajo.convocatorias.items():
                if convocatoria.telefono == telefono and convocatoria.estado == "pendiente":
                    candidatos.append((convocatoria.enviado_at or "", trabajo, worker_id))
        if not candidatos:
            return None
        _, trabajo, worker_id = max(candidatos, key=lambda c: c[0])
        return trabajo, worker_id

    # --- processed email ids ------------------------------------------------

    def ya_procesado(self, message_id: str) -> bool:
        return bool(message_id) and message_id in self.mensajes_procesados

    def marcar_procesado(self, message_id: str) -> None:
        if message_id:
            self.mensajes_procesados.add(message_id)
