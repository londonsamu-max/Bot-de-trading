"""
IMAP reader + SMTP sender.

Provider-agnostic: works with Google Workspace, Microsoft 365, Zoho or any
host that speaks IMAP/SMTP. Credentials come from the environment through
config/workflow.yaml.
"""

import email
import imaplib
import logging
import mimetypes
import smtplib
import ssl
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Adjunto:
    """A file attached to an incoming email (usually a photo of the job)."""

    nombre: str
    datos: bytes
    tipo: str = ""


@dataclass
class CorreoEntrante:
    """One fetched email, already decoded to text."""

    uid: str
    message_id: str
    remitente_nombre: str
    remitente_email: str
    asunto: str
    cuerpo: str
    fecha: str = ""
    in_reply_to: str = ""
    destinatarios: list[str] = field(default_factory=list)
    adjuntos: list[Adjunto] = field(default_factory=list)


class EmailClient:
    """Fetch unread mail and send messages."""

    def __init__(self, config: dict):
        self.imap_host = config.get("imap_host", "")
        self.imap_port = int(config.get("imap_port", 993))
        self.smtp_host = config.get("smtp_host", "")
        self.smtp_port = int(config.get("smtp_port", 587))
        self.usuario = config.get("usuario", "")
        self.password = config.get("password", "")
        self.buzon = config.get("buzon", "INBOX")
        self.remitente_visible = config.get("remitente_visible") or self.usuario
        self.smtp_ssl = bool(config.get("smtp_ssl", False))
        self.max_por_ciclo = int(config.get("max_correos_por_ciclo", 25))
        self.max_cuerpo = int(config.get("max_caracteres_cuerpo", 20000))
        self.max_adjunto_bytes = int(config.get("max_adjunto_mb", 10)) * 1024 * 1024
        self.max_adjuntos = int(config.get("max_adjuntos_por_correo", 10))

    # --- reading ------------------------------------------------------------

    def fetch_unread(self) -> list[CorreoEntrante]:
        """Fetch unread messages without marking them read (BODY.PEEK).

        Marking happens in `mark_seen` only after the runner processed them, so
        a crash mid-cycle re-delivers the message instead of losing it.
        """
        correos: list[CorreoEntrante] = []
        try:
            with self._imap() as imap:
                imap.select(self.buzon)
                status, data = imap.uid("search", None, "UNSEEN")
                if status != "OK":
                    logger.error("Búsqueda IMAP falló: %s", status)
                    return []

                uids = (data[0] or b"").split()[: self.max_por_ciclo]
                for uid in uids:
                    correo = self._fetch_one(imap, uid)
                    if correo:
                        correos.append(correo)
        except (imaplib.IMAP4.error, OSError, ssl.SSLError) as e:
            logger.error("Error leyendo correo por IMAP: %s", e)
            return correos

        logger.info("Correos nuevos leídos: %d", len(correos))
        return correos

    def _fetch_one(self, imap: imaplib.IMAP4_SSL, uid: bytes) -> Optional[CorreoEntrante]:
        status, data = imap.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK" or not data or not isinstance(data[0], tuple):
            logger.warning("No se pudo descargar el correo uid=%s", uid)
            return None

        mensaje = email.message_from_bytes(data[0][1])
        nombre, direccion = parseaddr(mensaje.get("From", ""))
        return CorreoEntrante(
            uid=uid.decode(),
            message_id=(mensaje.get("Message-ID") or f"uid-{uid.decode()}").strip(),
            remitente_nombre=_decode(nombre),
            remitente_email=direccion.lower(),
            asunto=_decode(mensaje.get("Subject", "")),
            cuerpo=self._extract_body(mensaje),
            adjuntos=self._extract_attachments(mensaje),
            fecha=mensaje.get("Date", ""),
            in_reply_to=(mensaje.get("In-Reply-To") or "").strip(),
            destinatarios=[a.strip().lower() for a in (mensaje.get("To") or "").split(",")
                           if a.strip()],
        )

    def _extract_body(self, mensaje: email.message.Message) -> str:
        """Prefer text/plain; fall back to text/html with tags stripped."""
        texto, html = "", ""
        for parte in mensaje.walk():
            if parte.get_content_maintype() == "multipart":
                continue
            if "attachment" in (parte.get("Content-Disposition") or ""):
                continue
            contenido = self._decode_part(parte)
            if parte.get_content_type() == "text/plain" and not texto:
                texto = contenido
            elif parte.get_content_type() == "text/html" and not html:
                html = contenido

        cuerpo = texto or _html_to_text(html)
        return cuerpo[: self.max_cuerpo]

    def _extract_attachments(self, mensaje: email.message.Message) -> list["Adjunto"]:
        """Collect attached files (clients send photos of the job to be done)."""
        adjuntos: list[Adjunto] = []
        for parte in mensaje.walk():
            if parte.get_content_maintype() == "multipart":
                continue
            disposicion = (parte.get("Content-Disposition") or "").lower()
            nombre = parte.get_filename()
            if "attachment" not in disposicion and not nombre:
                continue

            datos = parte.get_payload(decode=True)
            if not datos:
                continue
            if len(datos) > self.max_adjunto_bytes:
                logger.warning("Adjunto '%s' omitido: pesa %.1f MB",
                               nombre, len(datos) / 1024 / 1024)
                continue

            adjuntos.append(Adjunto(
                nombre=_decode(nombre or "adjunto"),
                datos=datos,
                tipo=parte.get_content_type(),
            ))
            if len(adjuntos) >= self.max_adjuntos:
                logger.warning("Se alcanzó el máximo de %d adjuntos por correo",
                               self.max_adjuntos)
                break
        return adjuntos

    @staticmethod
    def _decode_part(parte: email.message.Message) -> str:
        payload = parte.get_payload(decode=True)
        if payload is None:
            return ""
        charset = parte.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return payload.decode("utf-8", errors="replace")

    def mark_seen(self, uids: list[str]) -> None:
        """Flag messages as read once they have been processed."""
        if not uids:
            return
        try:
            with self._imap() as imap:
                imap.select(self.buzon)
                imap.uid("store", ",".join(uids), "+FLAGS", "(\\Seen)")
        except (imaplib.IMAP4.error, OSError) as e:
            logger.error("No se pudieron marcar como leídos %s: %s", uids, e)

    def _imap(self) -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(self.imap_host, self.imap_port,
                                 ssl_context=ssl.create_default_context())
        imap.login(self.usuario, self.password)
        return imap

    # --- sending ------------------------------------------------------------

    def send(self, destinatario: str, asunto: str, cuerpo: str,
             responder_a: str = "", adjuntos: Optional[list[str]] = None) -> bool:
        """Send a plain-text email, optionally attaching files by path."""
        if not destinatario:
            logger.warning("Envío omitido: destinatario vacío (asunto: %s)", asunto)
            return False

        mensaje = EmailMessage()
        mensaje["From"] = self.remitente_visible
        mensaje["To"] = destinatario
        mensaje["Subject"] = asunto
        if responder_a:
            mensaje["In-Reply-To"] = responder_a
            mensaje["References"] = responder_a
        mensaje.set_content(cuerpo)

        for ruta in adjuntos or []:
            self._adjuntar(mensaje, ruta)

        try:
            if self.smtp_ssl:
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port,
                                      context=ssl.create_default_context()) as smtp:
                    smtp.login(self.usuario, self.password)
                    smtp.send_message(mensaje)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port) as smtp:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.login(self.usuario, self.password)
                    smtp.send_message(mensaje)
        except (smtplib.SMTPException, OSError) as e:
            logger.error("No se pudo enviar correo a %s: %s", destinatario, e)
            return False

        logger.info("Correo enviado a %s: %s", destinatario, asunto)
        return True

    @staticmethod
    def _adjuntar(mensaje: EmailMessage, ruta: str) -> None:
        """Attach one file; a missing or unreadable file must not block the email."""
        archivo = Path(ruta)
        try:
            datos = archivo.read_bytes()
        except OSError as e:
            logger.warning("No se pudo adjuntar %s: %s", ruta, e)
            return

        tipo, _ = mimetypes.guess_type(archivo.name)
        principal, _, secundario = (tipo or "application/octet-stream").partition("/")
        mensaje.add_attachment(datos, maintype=principal,
                               subtype=secundario or "octet-stream",
                               filename=archivo.name)


def _decode(valor: str) -> str:
    """Decode RFC 2047 encoded headers (=?UTF-8?B?...?=)."""
    if not valor:
        return ""
    try:
        return str(make_header(decode_header(valor)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return valor


def _html_to_text(html: str) -> str:
    """Minimal HTML -> text: enough to read an email body, no dependencies."""
    if not html:
        return ""
    import re
    from html import unescape

    texto = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    texto = re.sub(r"(?i)<br\s*/?>", "\n", texto)
    texto = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", texto)
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = unescape(texto)
    texto = re.sub(r"[ \t\xa0]+", " ", texto)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", texto).strip()
