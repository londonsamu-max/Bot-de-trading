"""
Optional inbound WhatsApp webhook (standard library only).

Receives worker answers from Twilio (form-encoded POST) or Meta Cloud API
(JSON POST) and appends them to the WhatsApp inbox; the runner picks them up
on its next cycle. Needs a public HTTPS URL in front of it (a tunnel or a
reverse proxy) — without one, workers can still be confirmed by hand with
`python workflow.py confirmar`.

    python workflow.py webhook --puerto 8080
"""

import json
import logging
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from src.workflow.inbox import WhatsAppInbox

logger = logging.getLogger(__name__)

MAX_BODY = 1_000_000  # 1 MB: webhook payloads are tiny; anything bigger is junk


class _WebhookHandler(BaseHTTPRequestHandler):
    """Handles Twilio and Meta inbound message callbacks."""

    inbox: WhatsAppInbox = None       # injected by run_webhook
    verify_token: str = ""

    def do_GET(self) -> None:  # noqa: N802 (name required by BaseHTTPRequestHandler)
        """Meta's subscription handshake: echo hub.challenge if the token matches."""
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        modo = params.get("hub.mode", [""])[0]
        token = params.get("hub.verify_token", [""])[0]
        challenge = params.get("hub.challenge", [""])[0]

        if modo == "subscribe" and token and token == self.verify_token:
            self._responder(200, challenge)
        elif self.path.startswith("/salud"):
            self._responder(200, "ok")
        else:
            self._responder(403, "token inválido")

    def do_POST(self) -> None:  # noqa: N802
        longitud = int(self.headers.get("Content-Length") or 0)
        if longitud > MAX_BODY:
            self._responder(413, "payload demasiado grande")
            return

        cuerpo = self.rfile.read(longitud).decode("utf-8", errors="replace")
        tipo = (self.headers.get("Content-Type") or "").lower()

        try:
            if "json" in tipo:
                mensajes = _parse_meta(cuerpo)
            else:
                mensajes = _parse_twilio(cuerpo)
        except (ValueError, KeyError, TypeError) as e:
            logger.error("Webhook: payload no reconocido (%s): %s", e, cuerpo[:200])
            self._responder(400, "payload no reconocido")
            return

        for telefono, texto, origen in mensajes:
            if telefono and texto:
                self.inbox.append(telefono, texto, origen=origen)

        # Both providers retry on non-2xx, so always ack once stored.
        self._responder(200, "ok")

    def _responder(self, codigo: int, cuerpo: str) -> None:
        datos = cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def log_message(self, formato: str, *args) -> None:
        """Route http.server's stderr logging into the app logger."""
        logger.debug("webhook %s", formato % args)


def _parse_twilio(cuerpo: str) -> list[tuple[str, str, str]]:
    """Twilio posts application/x-www-form-urlencoded with From/Body."""
    campos = urllib.parse.parse_qs(cuerpo)
    remitente = campos.get("From", [""])[0].replace("whatsapp:", "").strip()
    texto = campos.get("Body", [""])[0].strip()
    return [(remitente, texto, "twilio")] if remitente else []


def _parse_meta(cuerpo: str) -> list[tuple[str, str, str]]:
    """Meta Cloud API posts entry[].changes[].value.messages[]."""
    datos = json.loads(cuerpo)
    mensajes: list[tuple[str, str, str]] = []
    for entrada in datos.get("entry", []):
        for cambio in entrada.get("changes", []):
            valor = cambio.get("value", {})
            for mensaje in valor.get("messages", []):
                telefono = mensaje.get("from", "")
                if telefono and not telefono.startswith("+"):
                    telefono = "+" + telefono
                texto = ""
                if mensaje.get("type") == "text":
                    texto = mensaje.get("text", {}).get("body", "")
                elif mensaje.get("type") == "button":
                    texto = mensaje.get("button", {}).get("text", "")
                elif mensaje.get("type") == "interactive":
                    interactivo = mensaje.get("interactive", {})
                    texto = (interactivo.get("button_reply", {}).get("title")
                             or interactivo.get("list_reply", {}).get("title", ""))
                if texto:
                    mensajes.append((telefono, texto, "meta"))
    return mensajes


def run_webhook(puerto: int = 8080, host: str = "0.0.0.0",
                inbox_path: str = "data/whatsapp_inbox.jsonl",
                verify_token: str = "", server_class=HTTPServer) -> None:
    """Serve the webhook until interrupted."""
    _WebhookHandler.inbox = WhatsAppInbox(inbox_path)
    _WebhookHandler.verify_token = verify_token

    servidor = server_class((host, puerto), _WebhookHandler)
    logger.info("Webhook de WhatsApp escuchando en http://%s:%d", host, puerto)
    logger.info("Configura esa URL (pública, con HTTPS) en Twilio o Meta")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        logger.info("Webhook detenido por el usuario")
    finally:
        servidor.server_close()


def parse_payload(cuerpo: str, content_type: str = "") -> list[tuple[str, str, str]]:
    """Public entry point for payload parsing, shared by the handler and tests."""
    if "json" in (content_type or "").lower():
        return _parse_meta(cuerpo)
    return _parse_twilio(cuerpo)
