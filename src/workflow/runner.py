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
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from src.workflow.attendance import AttendanceManager
from src.workflow.catalogs import PropertyDirectory, ServiceMaterials, UnitSizes
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
from src.workflow.property_parser import PropertyEmailParser
from src.workflow.schedule import VentanaDeEnvio
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
        self.property_parser = PropertyEmailParser(config.get("parser", {}))
        self.propiedades = PropertyDirectory(rutas.get("propiedades", "data/propiedades.csv"))
        self.tamanos = UnitSizes(rutas.get("unidades", "data/unidades.csv"))
        self.consumos = ServiceMaterials(rutas.get("consumos", "data/consumos.csv"))
        self.habilidades_por_servicio = {
            str(k).lower(): list(v)
            for k, v in (config.get("servicios", {}).get("habilidades") or {}).items()
        }
        self.attendance = AttendanceManager(config.get("asistencia", {}))
        self.max_trabajos_por_dia = int(
            config.get("asistencia", {}).get("max_trabajos_por_dia", 2))
        self.messages = MessageBuilder(
            config.get("mensajes", {}),
            empresa=self.empresa,
            contacto=general.get("contacto", ""),
        )

        recepcion = config.get("recepcion", {})
        self.remitentes_permitidos = [s.lower() for s in recepcion.get("remitentes_permitidos", [])]
        self.remitentes_ignorados = [s.lower() for s in recepcion.get("remitentes_ignorados", [])]
        # Website form submissions arrive forwarded by a notification service, so
        # the envelope sender is that service and not the client.
        self.reenviadores = [s.lower() for s in recepcion.get("reenviadores", [])]
        self.acusar_cliente = bool(recepcion.get("acusar_recibo", True))
        self.avisar_cliente_asignado = bool(recepcion.get("avisar_asignacion", True))
        self.carpeta_adjuntos = recepcion.get("carpeta_adjuntos", "data/adjuntos")

        self.horario = VentanaDeEnvio(general.get("horario_envios", {}))

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
        # Management companies and form forwarders are known clients: they skip
        # the automated-sender filter and the allowlist.
        conocido = self._es_reenviador(remitente) or self.propiedades.conocido(remitente)
        reenviado = conocido
        if not conocido and self._ignorado(remitente):
            logger.info("Correo ignorado de %s (%s)", remitente, correo.asunto)
            return

        trabajador = self.roster.by_email(remitente)
        if trabajador:
            if self._registrar_respuesta(trabajador, correo.cuerpo, "email",
                                         extract_job_id(f"{correo.asunto}\n{correo.cuerpo}")):
                resumen["respuestas"] += 1
            return

        if not reenviado and not self._remitente_permitido(remitente):
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
        """Route the email to the right parser and register the resulting jobs."""
        propiedad = self.propiedades.match(correo.remitente_email, correo.cuerpo,
                                           correo.asunto)
        if propiedad:
            self._crear_trabajos_por_unidad(correo, propiedad, resumen)
            return
        self._crear_trabajo_suelto(correo, resumen)

    def _crear_trabajos_por_unidad(self, correo: CorreoEntrante,
                                   propiedad, resumen: dict) -> None:
        """A management-company email: every unit line becomes its own job."""
        unidades = self.property_parser.parse(correo.cuerpo)
        if not unidades:
            logger.warning("Correo de %s (%s) sin unidades reconocibles; "
                           "se registra como solicitud suelta",
                           propiedad.propiedad, correo.remitente_email)
            self._crear_trabajo_suelto(correo, resumen, propiedad=propiedad)
            return

        adjuntos = self._guardar_adjuntos(f"correo-{_slug(correo.message_id)}",
                                          correo.adjuntos)
        creados: list[Trabajo] = []

        for unidad in unidades:
            tamano = unidad.tamano or self.tamanos.get(propiedad.propiedad, unidad.unidad)
            trabajo = Trabajo(
                id=self.store.next_job_id(date.today().strftime("%Y%m%d")),
                message_id=correo.message_id,
                cliente_nombre=propiedad.propiedad,
                cliente_email=correo.remitente_email,
                asunto=correo.asunto,
                empresa_gestion=propiedad.empresa_gestion,
                propiedad=propiedad.propiedad,
                unidad=unidad.unidad,
                tamano=tamano,
                servicio=unidad.servicio,
                descripcion_servicio=unidad.descripcion,
                turno=unidad.turno,
                ocupada=unidad.ocupada,
                descripcion=unidad.linea,
                direccion=propiedad.direccion,
                zona=propiedad.zona,
                fecha_servicio=unidad.fecha,
                habilidades=self._habilidades(unidad.servicio),
                items=self.consumos.para(unidad.servicio, tamano),
                adjuntos=adjuntos,
            )

            duplicado = self._buscar_duplicado(trabajo)
            if duplicado:
                logger.info("Unidad repetida, ya existe %s para %s",
                            duplicado.id, trabajo.etiqueta())
                duplicado.agregar_nota(f"La empresa reenvió esta unidad ({correo.asunto})")
                continue

            if not trabajo.tamano:
                trabajo.agregar_nota("Sin tamaño conocido: revisa data/unidades.csv")
            if not trabajo.items:
                trabajo.agregar_nota(
                    f"Sin material asignado para el servicio '{unidad.servicio}': "
                    f"revisa data/consumos.csv")

            self.inventory.check(trabajo.items)
            self.store.add(trabajo)
            creados.append(trabajo)
            resumen["trabajos_nuevos"] += 1

        if not creados:
            return

        logger.info("Correo de %s: %d unidades -> %d trabajos (%s)",
                    propiedad.propiedad, len(unidades), len(creados),
                    ", ".join(t.unidad for t in creados))
        self._avisar_faltantes(creados)
        self._acusar_recibo_lote(correo, propiedad, creados)

    def _buscar_duplicado(self, trabajo: Trabajo) -> Optional[Trabajo]:
        """Same property + unit + service + date already open = a re-sent list."""
        clave = trabajo.clave_unidad()
        for existente in self.store.trabajos.values():
            if existente.estado in (EstadoTrabajo.CANCELADO.value,
                                    EstadoTrabajo.COMPLETADO.value):
                continue
            if existente.clave_unidad() == clave:
                return existente
        return None

    def _habilidades(self, servicio: str) -> list[str]:
        """Map a service to the skills listed in trabajadores.csv."""
        return list(self.habilidades_por_servicio.get((servicio or "").lower(), []))

    def _avisar_faltantes(self, trabajos: list[Trabajo]) -> None:
        """One alert for the whole email instead of one per unit."""
        con_faltante = [t for t in trabajos if t.faltantes_inventario()]
        if not con_faltante:
            return
        detalle = "\n".join(
            f"- {t.etiqueta()} ({t.id}): " +
            ", ".join(f"{i.descripcion} faltan {i.faltante:g}"
                      for i in t.faltantes_inventario())
            for t in con_faltante
        )
        self._alertar(f"Falta material para {len(con_faltante)} unidades:\n{detalle}")

    def _acusar_recibo_lote(self, correo: CorreoEntrante, propiedad,
                            trabajos: list[Trabajo]) -> None:
        if not self.acusar_cliente or not correo.remitente_email:
            return
        lineas = "\n".join(
            f"- {t.unidad} ({t.servicio}) {t.fecha_servicio or 'sin fecha'} "
            f"{t.turno} -> folio {t.id}".rstrip()
            for t in trabajos
        )
        cuerpo = (f"Hola,\n\nRecibimos su solicitud para {propiedad.propiedad} "
                  f"y registramos {len(trabajos)} unidades:\n\n{lineas}\n\n"
                  f"Ya estamos asignando al personal y les confirmamos.\n\n"
                  f"Saludos,\n{self.empresa}")
        if self._enviar_email(correo.remitente_email,
                              f"Recibido: {len(trabajos)} unidades - {propiedad.propiedad}",
                              cuerpo, responder_a=correo.message_id):
            for trabajo in trabajos:
                trabajo.acuse_cliente_at = utc_now()

    def _crear_trabajo_suelto(self, correo: CorreoEntrante, resumen: dict,
                              propiedad=None) -> None:
        """One-off request (website form, direct client): a single job."""
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
        if propiedad:
            # Known management company, but the email had no parseable unit list.
            trabajo.empresa_gestion = propiedad.empresa_gestion
            trabajo.propiedad = propiedad.propiedad
            trabajo.cliente_nombre = trabajo.cliente_nombre or propiedad.propiedad
            trabajo.direccion = trabajo.direccion or propiedad.direccion
            trabajo.zona = trabajo.zona or propiedad.zona
            trabajo.agregar_nota("No se reconocieron unidades; revísalo a mano")
            self._alertar(f"Correo de {propiedad.propiedad} sin unidades reconocibles "
                          f"({trabajo.id}). Hay que capturarlo a mano.")

        trabajo.adjuntos = self._guardar_adjuntos(trabajo.id, correo.adjuntos)
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

    def _guardar_adjuntos(self, job_id: str, adjuntos: list) -> list[str]:
        """Save the client's photos under data/adjuntos/<folio>/ and return the paths."""
        if not adjuntos:
            return []

        destino = Path(self.carpeta_adjuntos) / job_id
        destino.mkdir(parents=True, exist_ok=True)
        rutas: list[str] = []

        for indice, adjunto in enumerate(adjuntos, start=1):
            nombre = _nombre_seguro(adjunto.nombre) or f"adjunto-{indice}"
            ruta = destino / f"{indice:02d}-{nombre}"
            try:
                ruta.write_bytes(adjunto.datos)
            except OSError as e:
                logger.error("No se pudo guardar el adjunto %s: %s", nombre, e)
                continue
            rutas.append(str(ruta))

        if rutas:
            logger.info("Trabajo %s: %d adjuntos guardados en %s",
                        job_id, len(rutas), destino)
        return rutas

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
        todos = list(self.store.trabajos.values())

        # Cold-calling workers at night is not acceptable; delivering a work
        # order to somebody who already said yes is.
        puede_convocar = self.horario.abierta(ahora)
        if abiertos and not puede_convocar:
            logger.info("Convocatorias en pausa: %s. Se reanudan el %s",
                        self.horario.descripcion(ahora),
                        self.horario.proxima_apertura(ahora).strftime("%a %d/%m %H:%M"))

        for trabajo in abiertos:
            self.attendance.expirar(trabajo, ahora)

            if trabajo.cupo_cubierto():
                self._despachar(trabajo, resumen)
                continue

            if not puede_convocar:
                continue

            # Load is counted per service date: a full Tuesday says nothing
            # about whether somebody can take a unit on Thursday.
            carga = self.roster.carga_actual(todos, fecha=trabajo.fecha_servicio)
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
            trabajo, cantidad, excluir=set(trabajo.convocatorias), carga=carga,
            max_por_dia=self.max_trabajos_por_dia,
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
        asunto = f"[{trabajo.id}] Orden de trabajo confirmada"
        no_entregadas = []
        for convocatoria in confirmados:
            mensaje = self.messages.orden_trabajo(trabajo, convocatoria.nombre)
            entregado = self._notificar(convocatoria.telefono, convocatoria.email,
                                        asunto, mensaje)
            # Photos cannot travel over a plain WhatsApp text, so when the client
            # sent any, the crew also gets the order by email with them attached.
            if trabajo.adjuntos and convocatoria.email:
                entregado = self._enviar_email(convocatoria.email, asunto, mensaje,
                                               adjuntos=trabajo.adjuntos) or entregado
            if not entregado:
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
                      responder_a: str = "",
                      adjuntos: Optional[list[str]] = None) -> bool:
        if self.dry_run:
            logger.info("[DRY-RUN] Correo a %s | %s (%d adjuntos)\n%s",
                        destinatario, asunto, len(adjuntos or []), cuerpo)
            return True
        return self.email.send(destinatario, asunto, cuerpo,
                               responder_a=responder_a, adjuntos=adjuntos)

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

    def _es_reenviador(self, remitente: str) -> bool:
        """True for services that forward website form submissions to the mailbox."""
        return any(self._coincide(remitente, patron) for patron in self.reenviadores)

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


def _slug(texto: str) -> str:
    """Turn a Message-ID into something usable as a folder name."""
    limpio = re.sub(r"[^\w.-]", "-", (texto or "").strip("<>"))
    return limpio.strip("-")[:60] or "sin-id"


def _nombre_seguro(nombre: str) -> str:
    """Sanitise an attachment filename: no paths, no surprises on disk."""
    base = Path(nombre or "").name
    limpio = re.sub(r"[^\w.\- ]", "_", base).strip(". ")
    return limpio[:80]
