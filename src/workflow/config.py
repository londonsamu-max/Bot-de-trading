"""
Workflow configuration loader.

Reads config/workflow.yaml and resolves ${VAR} placeholders against the
environment (same convention used by the trading bot's settings.yaml).
"""

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config/workflow.yaml"
_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


def resolve_env_vars(value: Any) -> Any:
    """Recursively replace ${VAR} placeholders with environment variables.

    A placeholder with no matching variable resolves to an empty string, so a
    missing credential surfaces as a clear validation error instead of a
    literal "${SMTP_PASSWORD}" being sent to the mail server.
    """
    if isinstance(value, str):
        def _sub(match: re.Match) -> str:
            name = match.group(1)
            resolved = os.getenv(name)
            if resolved is None:
                # Not necessarily a problem: optional integrations (Twilio, Meta,
                # supervisor alerts) leave their variables unset. `validate_config`
                # is what reports the ones that are actually required.
                logger.debug("Variable de entorno %s no definida", name)
                return ""
            return resolved

        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env_vars(v) for v in value]
    return value


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    """Load workflow.yaml with environment variables already resolved."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"No se encontró {config_path}. Copia config/workflow.yaml del repositorio."
        )
    with config_path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return resolve_env_vars(raw)


def validate_config(config: dict, *, require_email: bool = True) -> list[str]:
    """Return a list of human-readable problems. Empty list means the config is usable."""
    problems: list[str] = []

    if require_email:
        email = config.get("email", {})
        for campo in ("imap_host", "smtp_host", "usuario", "password"):
            if not email.get(campo):
                problems.append(f"email.{campo} está vacío (revisa tu archivo .env)")

    whatsapp = config.get("whatsapp", {})
    proveedor = whatsapp.get("proveedor", "manual")
    if proveedor == "twilio":
        for campo in ("account_sid", "auth_token", "numero_origen"):
            if not whatsapp.get("twilio", {}).get(campo):
                problems.append(f"whatsapp.twilio.{campo} está vacío")
    elif proveedor == "meta":
        for campo in ("phone_number_id", "access_token"):
            if not whatsapp.get("meta", {}).get(campo):
                problems.append(f"whatsapp.meta.{campo} está vacío")
    elif proveedor != "manual":
        problems.append(
            f"whatsapp.proveedor '{proveedor}' no es válido (usa: twilio, meta o manual)"
        )

    return problems
