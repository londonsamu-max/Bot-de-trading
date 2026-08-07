"""
Workflow orchestrator.

One cycle does, in order:

    1. Read the mailbox: client emails become jobs, worker emails become answers.
    2. Drain the WhatsApp inbox (webhook / manual confirmations).
    3. For every open job: invite workers, remind, expire, escalate.
    4. When enough workers confirmed: reserve inventory and send the work order.
    5. Persist state.

Every step is idempotent: re-running a cycle after a crash re-sends nothing
that was already sent, because state is saved with what went out.
"""

import logging
import time
from datetime import date, datetime, timezone
from typing import Optional

from src.workflow.attendance import AttendanceManager
from src.workflow.config import validate_config
from src.workflow.email_client import CorreoEntrante, EmailClient
from src.workflow.inbox import WhatsAppInbox
from src.workflow.inventory import Inventory
from src.workflow.messages import MessageBuilder, formato_items
from src.workflow.models import (
    EstadoConvocatoria,
    EstadoTrabajo,
    Trabajo,
    Trabajador,
    utc_now,
)
from src.workflow.notifier import Notifier, ResultadoEnvio, build_whatsapp_notifier
from src.workflow.parser import EmailJobParser, extract_job_id, parse_answer, strip_accents
from src.workflow.store import WorkflowStore
from src.workflow.workers import WorkerRoster

logger = logging.getLogger(__name__)


class WorkflowRunner:
    """Ties mailbox, inventory, roster, attendance and dispatch together."""

    def __init__(self, config: dict, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run

        general = config.get("general", {})
        rutas = config.get("rutas", {})
        self.empresa = general.get("empresa", "")
        self.intervalo = int(general.get("intervalo_segundos", 300))

        self.store = WorkflowStore(rutas.get("estado", "data/workflow_state.json"))
        self.inventory = Inventory(rutas.get("inventario", "data/inventario.csv"))
        self.roster = WorkerRoster(
            rutas.get("trabajadores", "data/trabajadores.csv"),
            default_country_code=general.get("codigo_pais", "+1"),
        )
        self.inbox = WhatsAppInbox(rutas.get("whatsapp_inbox", "data/whatsapp_inbox.jsonl"))

        self.email = EmailClient(config.get("email", {}))
        self.notifier = Notifier(
            build_whatsapp_notifier(config.get("whatsapp", {})),
            email_client=self.email,
            email_respaldo=bool(config.get("whatsapp", {}).get("respaldo_email", True)),
        )
        self.parser = EmailJobParser(config.get("parser", {}))
        self.attendance = AttendanceManager(config.get("asistencia", {}))
        self.messages = MessageBuilder(
            config.get("mensajes", {}),
            empresa=self.empresa,
            contacto=general.get("contacto", ""),
        )

        recepcion = config.get("recepcion", {})
        self.remitentes_permitidos = [s.lower() for s in recepcion.get("remitentes_permitidos", [])]
        self.remitentes_ignorados = [s.lower() for s in recepcion.get("remitentes_ignorados", [])]
        self.acusar_cliente = bool(recepcion.get("acusar_recibo", True))
        self.avisar_cliente_asignado = bool(recepcion.get("avisar_asignacion", True))

        despacho = config.get("despacho", {})
        self.reservar_inventario = bool(despacho.get("reservar_inventario", True))
        self.exigir_inventario = bool(despacho.get("exigir_inventario_completo", False))

        alertas = config.get("alertas", {})
        self.email_supervisor = alertas.get("email_supervisor", "")
        self.telefono_supervisor = alertas.get("whatsapp_supervisor", "")

    # --- public API ---------------------------------------------------------

    def run_once(self) -> dict:
        """Run a single cycle. Returns a summary dict for logging/CLI output."""
        resumen = {
            "correos_leidos": 0,
            "trabajos_nuevos": 0,
            "respuestas": 0,
            "convocatorias_enviadas": 0,
            "recordatorios": 0,
            "trabajos_asignados": 0,
            "trabajos_sin_personal": 0,
        }

        self._leer_correos(resumen)
        self._procesar_whatsapp(resumen)
        self._atender_trabajos(resumen)
        self.store.save()

        logger.info("Ciclo terminado: %s", resumen)
        return resumen

    def run_forever(self) -> None:
        """Loop `run_once` every `general.intervalo_segundos` until interrupted."""
        logger.info("Flujo iniciado (intervalo: %ds, dry_run=%s)", self.intervalo, self.dry_run)
        try:
            while True:
                try:
                    self.run_once()
                except Exception:  # keep the daemon alive across transient failures
                    logger.exception("Error en el ciclo; se reintenta en %ds", self.intervalo)
                time.sleep(self.intervalo)
        except KeyboardInterrupt:
            logger.info("Flujo detenido por el usuario")
            self.store.save()

    # --- step 1: mailbox ----------------------------------------------------

    def _leer_correos(self, resumen: dict) -> None:
        problemas = validate_config(self.config)
        if problemas:
            logger.error("Configuración de correo incompleta, no se lee la bandeja: %s",
                         "; ".join(problemas))
            return

        correos = self.email.fetch_unread()
        resumen["correos_leidos"] = len(correos)
        procesados: list[str] = []

        for correo in correos:
            try:
                self._procesar_correo(correo, resumen)
            except Exception:
                logger.exception("No se pudo procesar el correo %s; se deja sin leer",
                                 correo.message_id)
                continue
            self.store.marcar_procesado(correo.message_id)
            procesados.append(correo.uid)

        self.email.mark_seen(procesados)

    def _procesar_correo(self, correo: CorreoEntrante, resumen: dict) -> None:
        if self.store.ya_procesado(correo.message_id):
            logger.debug("Correo %s ya procesado", correo.message_id)
            return

        remitente = correo.remitente_email
        if self._ignorado(remitente):
            logger.info("Correo ignorado de %s (%s)", remitente, correo.asunto)
            return

        trabajador = self.roster.by_email(remitente)
        if trabajador:
            if self._registrar_respuesta(trabajador, correo.cuerpo, "email",
                                         extract_job_id(f"{correo.asunto}\n{correo.cuerpo}")):
                resumen["respuestas"] += 1
            return

        if not self._remitente_permitido(remitente):
            logger.info("Remitente no autorizado, se omite: %s", remitente)
            return

        job_id = extract_job_id(correo.asunto)
        if job_id and (trabajo := self.store.get(job_id)):
            trabajo.agregar_nota(f"El cliente respondió sobre {job_id}: "
                                 f"{correo.cuerpo.strip()[:200]}")
            logger.info("Seguimiento del cliente sobre %s; requiere revisión manual", job_id)
            self._alertar(f"El cliente respondió sobre {job_id} ({trabajo.cliente_nombre}). "
                          f"Revisa la bandeja.")
            return

        self._crear_trabajo(correo, resumen)

    def _crear_trabajo(self, correo: CorreoEntrante, resumen: dict) -> None:
        campos = self.parser.parse(
            correo.asunto, correo.cuerpo,
            remitente_nombre=correo.remitente_nombre,
            remitente_email=correo.remitente_email,
        )
        trabajo = Trabajo(
            id=self.store.next_job_id(date.today().strftime("%Y%m%d")),
            message_id=correo.message_id,
            **campos,
        )
        self.inventory.check(trabajo.items)
        self.store.add(trabajo)
        resumen["trabajos_nuevos"] += 1
        logger.info("Trabajo %s creado desde el correo de %s (%d materiales)",
                    trabajo.id, trabajo.cliente_email, len(trabajo.items))

        if trabajo.faltantes_inventario():
            faltantes = formato_items(trabajo.faltantes_inventario())
            trabajo.agregar_nota(f"Faltante de inventario:\n{faltantes}")
            self._alertar(self.messages.render(
                "alerta_inventario",
                **self.messages.contexto(trabajo),
            ))

        if self.acusar_cliente and trabajo.cliente_email:
            enviado = self._enviar_email(
                trabajo.cliente_email,
                f"[{trabajo.id}] Recibimos su solicitud",
                self.messages.render("acuse_cliente", **self.messages.contexto(trabajo)),
                responder_a=correo.message_id,
            )
            if enviado:
                trabajo.acuse_cliente_at = utc_now()

    # --- step 2: WhatsApp answers -------------------------------------------

    def _procesar_whatsapp(self, resumen: dict) -> None:
        registros, offset = self.inbox.consume(self.store.whatsapp_offset)
        self.store.whatsapp_offset = offset

        for registro in registros:
            telefono = registro.get("telefono", "")
            texto = registro.get("texto", "")
            trabajador = self.roster.by_phone(telefono)
            if not trabajador:
                logger.warning("Respuesta de un número desconocido (%s): %s",
                               telefono, texto[:80])
                self._alertar(f"Mensaje de WhatsApp de un número no registrado "
                              f"({telefono}): {texto[:150]}")
                continue

            job_id = registro.get("job_id") or extract_job_id(texto)
            if self._registrar_respuesta(trabajador, texto,
                                         registro.get("origen", "whatsapp"), job_id):
                resumen["respuestas"] += 1

    def _registrar_respuesta(self, trabajador: Trabajador, texto: str, canal: str,
                             job_id: Optional[str] = None) -> bool:
        """Route an answer to the right job and record it."""
        trabajo = self.store.get(job_id) if job_id else None
        if not trabajo:
            encontrado = self.store.find_pending_for_phone(trabajador.telefono)
            if encontrado:
                trabajo, _ = encontrado

        if not trabajo:
            logger.info("Respuesta de %s sin trabajo pendiente asociado: %s",
                        trabajador.nombre, texto[:80])
            return False

        respuesta = parse_answer(texto)
        if respuesta is None:
            logger.info("Respuesta ambigua de %s en %s: %s",
                        trabajador.nombre, trabajo.id, texto[:80])
            trabajo.agregar_nota(f"Respuesta ambigua de {trabajador.nombre}: {texto[:150]}")
            self._pedir_aclaracion(trabajo, trabajador)
            return False

        return self.attendance.registrar_respuesta(
            trabajo, trabajador.id, respuesta, canal=canal, crudo=texto
        )

    def _pedir_aclaracion(self, trabajo: Trabajo, trabajador: Trabajador) -> None:
        """Ask once for a clear SI/NO; never twice, so no message ping-pong."""
        convocatoria = trabajo.convocatorias.get(trabajador.id)
        if not convocatoria or convocatoria.recordado_at:
            return
        self._notificar(
            trabajador.telefono, trabajador.email,
            f"[{trabajo.id}] Confirmación de asistencia",
            f"No entendí tu respuesta para el trabajo {trabajo.id}. "
            f"Por favor responde solamente SI o NO.",
        )
        convocatoria.recordado_at = utc_now()

    # --- step 3+4: jobs -----------------------------------------------------

    def _atender_trabajos(self, resumen: dict) -> None:
        abiertos = self.store.by_estado(EstadoTrabajo.NUEVO.value,
                                        EstadoTrabajo.CONVOCANDO.value)
        ahora = datetime.now(timezone.utc)
        carga = self.roster.carga_actual(list(self.store.trabajos.values()))

        for trabajo in abiertos:
            self.attendance.expirar(trabajo, ahora)

            if trabajo.cupo_cubierto():
                self._despachar(trabajo, resumen)
                continue

            enviadas = self._convocar(trabajo, carga)
            resumen["convocatorias_enviadas"] += enviadas

            for convocatoria in self.attendance.pendientes_por_recordar(trabajo, ahora):
                mensaje = self.messages.recordatorio(trabajo, convocatoria.nombre)
                if self._notificar(convocatoria.telefono, convocatoria.email,
                                   f"[{trabajo.id}] Recordatorio de asistencia", mensaje):
                    convocatoria.recordado_at = utc_now()
                    resumen["recordatorios"] += 1

            if self.attendance.sin_salida(trabajo) and enviadas == 0:
                trabajo.estado = EstadoTrabajo.SIN_PERSONAL.value
                trabajo.agregar_nota("Sin personal disponible ni convocatorias pendientes")
                resumen["trabajos_sin_personal"] += 1
                self._alertar(self.messages.render("alerta_sin_personal",
                                                   **self.messages.contexto(trabajo)))
                logger.warning("Trabajo %s se quedó sin personal", trabajo.id)

    def _convocar(self, trabajo: Trabajo, carga: dict[str, int]) -> int:
        """Invite as many workers as the job still needs. Returns messages sent."""
        cantidad = self.attendance.cuantos_convocar(trabajo)
        if cantidad <= 0:
            return 0

        candidatos = self.roster.seleccionar(
            trabajo, cantidad, excluir=set(trabajo.convocatorias), carga=carga
        )
        if not candidatos:
            logger.warning("No hay trabajadores disponibles para %s (habilidades: %s)",
                           trabajo.id, trabajo.habilidades or "cualquiera")
            return 0

        nuevas = self.attendance.abrir(trabajo, candidatos)
        enviadas = 0
        for convocatoria in nuevas:
            mensaje = self.messages.convocatoria(trabajo, convocatoria.nombre)
            resultado = self._notificar(
                convocatoria.telefono, convocatoria.email,
                f"[{trabajo.id}] ¿Puedes tomar este trabajo?", mensaje, devolver=True,
            )
            if resultado and resultado.ok:
                self.attendance.marcar_enviada(convocatoria, resultado.canal)
                carga[convocatoria.trabajador_id] = carga.get(convocatoria.trabajador_id, 0) + 1
                enviadas += 1
            else:
                # Could not reach them: drop the request so another worker is
                # invited on the next cycle instead of waiting on a dead line.
                trabajo.convocatorias.pop(convocatoria.trabajador_id, None)
                logger.error("No se pudo convocar a %s para %s",
                             convocatoria.nombre, trabajo.id)

        if enviadas:
            trabajo.estado = EstadoTrabajo.CONVOCANDO.value
            trabajo.agregar_nota(f"Convocados {enviadas} trabajadores")
        return enviadas

    def _despachar(self, trabajo: Trabajo, resumen: dict) -> None:
        """Enough workers confirmed: reserve material and send the work order."""
        if trabajo.orden_enviada_at:
            trabajo.estado = EstadoTrabajo.ASIGNADO.value
            return

        if self.reservar_inventario and not trabajo.inventario_reservado and trabajo.items:
            ok, problemas = self.inventory.reservar(trabajo.items,
                                                    estricto=self.exigir_inventario)
            if ok:
                trabajo.inventario_reservado = True
                trabajo.agregar_nota("Inventario reservado")
            else:
                detalle = "; ".join(problemas)
                trabajo.agregar_nota(f"Material incompleto: {detalle}")
                self._alertar(f"Trabajo {trabajo.id}: falta material.\n{detalle}")
                if self.exigir_inventario:
                    logger.error("Trabajo %s en espera por falta de material: %s",
                                 trabajo.id, detalle)
                    return
                # Partial reservation: whatever existed is now held for this job,
                # and the work order goes out flagging what is still missing.
                trabajo.inventario_reservado = any(i.reservado for i in trabajo.items)
                logger.warning("Trabajo %s se despacha con material incompleto: %s",
                               trabajo.id, detalle)

        confirmados = trabajo.confirmados()
        no_entregadas = []
        for convocatoria in confirmados:
            mensaje = self.messages.orden_trabajo(trabajo, convocatoria.nombre)
            if not self._notificar(convocatoria.telefono, convocatoria.email,
                                   f"[{trabajo.id}] Orden de trabajo confirmada", mensaje):
                no_entregadas.append(convocatoria.nombre)

        if no_entregadas:
            # The crew is set but somebody never got the details: that needs a
            # phone call, so it cannot stay buried in the log.
            detalle = ", ".join(no_entregadas)
            trabajo.agregar_nota(f"No se entregó la orden a: {detalle}")
            self._alertar(f"Trabajo {trabajo.id}: no se pudo entregar la orden de "
                          f"trabajo a {detalle}. Hay que avisarles por teléfono.")

        # Free anyone still pending: the crew is complete.
        for convocatoria in trabajo.pendientes():
            convocatoria.estado = EstadoConvocatoria.EXPIRADO.value
            self._notificar(convocatoria.telefono, convocatoria.email,
                            f"[{trabajo.id}] Cupo cubierto",
                            f"Gracias {convocatoria.nombre}, el trabajo {trabajo.id} "
                            f"ya quedó cubierto. No necesitas asistir.")

        trabajo.estado = EstadoTrabajo.ASIGNADO.value
        trabajo.orden_enviada_at = utc_now()
        trabajo.agregar_nota(
            f"Orden enviada a {', '.join(c.nombre for c in confirmados)}"
        )
        resumen["trabajos_asignados"] += 1
        logger.info("Trabajo %s asignado a %d trabajadores", trabajo.id, len(confirmados))

        if self.avisar_cliente_asignado and trabajo.cliente_email:
            self._enviar_email(
                trabajo.cliente_email,
                f"[{trabajo.id}] Servicio asignado",
                self.messages.render("cliente_asignado", **self.messages.contexto(trabajo)),
                responder_a=trabajo.message_id,
            )

    # --- helpers ------------------------------------------------------------

    def _notificar(self, telefono: str, email: str, asunto: str, mensaje: str,
                   devolver: bool = False):
        """Send through the notifier, honouring dry-run mode."""
        if self.dry_run:
            logger.info("[DRY-RUN] Para %s / %s | %s\n%s",
                        telefono or "-", email or "-", asunto, mensaje)
            resultado = ResultadoEnvio(True, "dry-run")
        else:
            resultado = self.notifier.notificar(telefono, email, asunto, mensaje)
        return resultado if devolver else resultado.ok

    def _enviar_email(self, destinatario: str, asunto: str, cuerpo: str,
                      responder_a: str = "") -> bool:
        if self.dry_run:
            logger.info("[DRY-RUN] Correo a %s | %s\n%s", destinatario, asunto, cuerpo)
            return True
        return self.email.send(destinatario, asunto, cuerpo, responder_a=responder_a)

    def _alertar(self, mensaje: str) -> None:
        """Notify whoever supervises the workflow (optional)."""
        if not (self.email_supervisor or self.telefono_supervisor):
            logger.warning("ALERTA (sin supervisor configurado): %s", mensaje)
            return
        self._notificar(self.telefono_supervisor, self.email_supervisor,
                        f"[{self.empresa or 'Flujo'}] Alerta", mensaje)

    def _ignorado(self, remitente: str) -> bool:
        if not remitente:
            return True
        if remitente == (self.email.usuario or "").lower():
            return True   # our own outgoing copy
        automaticos = ("noreply", "no-reply", "mailer-daemon", "postmaster")
        if any(a in remitente for a in automaticos):
            return True
        return any(self._coincide(remitente, patron) for patron in self.remitentes_ignorados)

    def _remitente_permitido(self, remitente: str) -> bool:
        if not self.remitentes_permitidos:
            return True   # empty allowlist = accept anyone
        return any(self._coincide(remitente, patron) for patron in self.remitentes_permitidos)

    @staticmethod
    def _coincide(remitente: str, patron: str) -> bool:
        """Match a full address or a bare domain ('@cliente.com' or 'cliente.com')."""
        patron = strip_accents(patron).strip()
        remitente = strip_accents(remitente).strip()
        if not patron:
            return False
        if patron.startswith("@"):
            return remitente.endswith(patron)
        if "@" in patron:
            return remitente == patron
        return remitente.endswith("@" + patron)
