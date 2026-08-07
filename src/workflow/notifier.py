"""
WhatsApp notification providers.

Three backends, chosen with `whatsapp.proveedor` in config/workflow.yaml:

    twilio  - Twilio WhatsApp API (needs account_sid / auth_token / numero_origen)
    meta    - WhatsApp Cloud API (needs phone_number_id / access_token)
    manual  - no API: writes each message to logs/whatsapp_pendientes.txt with a
              wa.me link, ready to copy-paste. Useful to run the whole workflow
              before any WhatsApp Business account exists.

Meta's Cloud API only allows free-form text inside a 24h customer service
window; outside it a pre-approved template is required. `plantilla` in the
config switches to template mode.
"""

import json
import logging
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 20


@dataclass
class ResultadoEnvio:
    ok: bool
    canal: str
    detalle: str = ""


class WhatsAppNotifier(ABC):
    """Send one WhatsApp message to one phone number."""

    canal = "whatsapp"

    @abstractmethod
    def send(self, telefono: str, mensaje: str) -> ResultadoEnvio:
        ...


class TwilioWhatsApp(WhatsAppNotifier):
    """Twilio's WhatsApp channel (https://api.twilio.com)."""

    canal = "whatsapp:twilio"

    def __init__(self, config: dict):
        self.account_sid = config.get("account_sid", "")
        self.auth_token = config.get("auth_token", "")
        self.numero_origen = _wa(config.get("numero_origen", ""))

    def send(self, telefono: str, mensaje: str) -> ResultadoEnvio:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        try:
            respuesta = requests.post(
                url,
                auth=(self.account_sid, self.auth_token),
                data={"From": self.numero_origen, "To": _wa(telefono), "Body": mensaje},
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            logger.error("Twilio: fallo de red enviando a %s: %s", telefono, e)
            return ResultadoEnvio(False, self.canal, str(e))

        if respuesta.status_code >= 400:
            logger.error("Twilio rechazó el envío a %s (%s): %s",
                         telefono, respuesta.status_code, respuesta.text[:300])
            return ResultadoEnvio(False, self.canal, respuesta.text[:300])

        sid = _json_field(respuesta, "sid")
        logger.info("WhatsApp enviado a %s (Twilio sid=%s)", telefono, sid)
        return ResultadoEnvio(True, self.canal, sid)


class MetaWhatsApp(WhatsAppNotifier):
    """WhatsApp Cloud API (graph.facebook.com)."""

    canal = "whatsapp:meta"

    def __init__(self, config: dict):
        self.phone_number_id = config.get("phone_number_id", "")
        self.access_token = config.get("access_token", "")
        self.version = config.get("api_version", "v21.0")
        self.plantilla = config.get("plantilla", "")
        self.idioma = config.get("idioma_plantilla", "es")

    def send(self, telefono: str, mensaje: str) -> ResultadoEnvio:
        url = f"https://graph.facebook.com/{self.version}/{self.phone_number_id}/messages"
        destino = telefono.lstrip("+")

        if self.plantilla:
            payload = {
                "messaging_product": "whatsapp",
                "to": destino,
                "type": "template",
                "template": {
                    "name": self.plantilla,
                    "language": {"code": self.idioma},
                    "components": [{
                        "type": "body",
                        "parameters": [{"type": "text", "text": mensaje}],
                    }],
                },
            }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": destino,
                "type": "text",
                "text": {"body": mensaje},
            }

        try:
            respuesta = requests.post(
                url,
                headers={"Authorization": f"Bearer {self.access_token}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            logger.error("Meta: fallo de red enviando a %s: %s", telefono, e)
            return ResultadoEnvio(False, self.canal, str(e))

        if respuesta.status_code >= 400:
            logger.error("Meta rechazó el envío a %s (%s): %s",
                         telefono, respuesta.status_code, respuesta.text[:300])
            return ResultadoEnvio(False, self.canal, respuesta.text[:300])

        logger.info("WhatsApp enviado a %s (Meta Cloud API)", telefono)
        return ResultadoEnvio(True, self.canal, _json_field(respuesta, "messages"))


class ManualWhatsApp(WhatsAppNotifier):
    """No API configured: queue messages to a file with ready-to-open wa.me links."""

    canal = "whatsapp:manual"

    def __init__(self, config: dict):
        self.path = Path(config.get("archivo_pendientes", "logs/whatsapp_pendientes.txt"))

    def send(self, telefono: str, mensaje: str) -> ResultadoEnvio:
        enlace = (f"https://wa.me/{telefono.lstrip('+')}"
                  f"?text={urllib.parse.quote(mensaje)}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 70}\nPARA: {telefono}\n{'-' * 70}\n"
                    f"{mensaje}\n{'-' * 70}\nABRIR: {enlace}\n")
        logger.info("WhatsApp en cola manual para %s -> %s", telefono, self.path)
        return ResultadoEnvio(True, self.canal, str(self.path))


def build_whatsapp_notifier(config: dict) -> WhatsAppNotifier:
    """Instantiate the provider named in `whatsapp.proveedor`."""
    proveedor = (config.get("proveedor") or "manual").lower()
    if proveedor == "twilio":
        return TwilioWhatsApp(config.get("twilio", {}))
    if proveedor == "meta":
        return MetaWhatsApp(config.get("meta", {}))
    if proveedor != "manual":
        logger.warning("Proveedor de WhatsApp '%s' desconocido; se usa modo manual",
                       proveedor)
    return ManualWhatsApp(config.get("manual", {}))


class Notifier:
    """WhatsApp as the primary channel, email as automatic backup."""

    def __init__(self, whatsapp: WhatsAppNotifier, email_client=None,
                 email_respaldo: bool = True):
        self.whatsapp = whatsapp
        self.email_client = email_client
        self.email_respaldo = email_respaldo

    def notificar(self, telefono: str, email: str, asunto: str,
                  mensaje: str) -> ResultadoEnvio:
        """Send by WhatsApp; on failure (or no phone) fall back to email."""
        if telefono:
            resultado = self.whatsapp.send(telefono, mensaje)
            if resultado.ok:
                return resultado
            logger.warning("WhatsApp falló para %s, se intenta correo", telefono)
        else:
            resultado = ResultadoEnvio(False, "whatsapp", "sin teléfono")

        if self.email_respaldo and self.email_client and email:
            if self.email_client.send(email, asunto, mensaje):
                return ResultadoEnvio(True, "email", "respaldo")

        return ResultadoEnvio(False, resultado.canal,
                              resultado.detalle or "sin canal disponible")


def _wa(numero: str) -> str:
    """Twilio addresses WhatsApp numbers as 'whatsapp:+123...'."""
    numero = (numero or "").strip()
    if not numero:
        return ""
    return numero if numero.startswith("whatsapp:") else f"whatsapp:{numero}"


def _json_field(respuesta: requests.Response, campo: str) -> str:
    try:
        valor = respuesta.json().get(campo, "")
    except (json.JSONDecodeError, ValueError):
        return ""
    return valor if isinstance(valor, str) else json.dumps(valor)[:200]
