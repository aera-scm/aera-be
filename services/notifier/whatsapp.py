"""IR-07: send only pre-approved supplier question templates through WhatsApp."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from services.dialogue.policy import Question
from services.rules.br_04 import e164

_NAME = re.compile(r"[a-z0-9_]{1,512}")
_VERSION = re.compile(r"v[0-9]+\.[0-9]+")
_LANGUAGE = re.compile(r"[a-z]{2}(?:_[A-Z]{2})?")


class WhatsAppDeliveryError(Exception):
    """Configuration or delivery failed; caller must not retry an ambiguous send."""


@dataclass
class WhatsAppTemplateSender:
    config: Callable[[], dict[str, Any]]
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=10.0))

    def send_question(self, question: Question) -> str:
        settings = self.config()
        token = str(settings.get("accessToken") or "")
        phone_id = str(settings.get("phoneNumberId") or "")
        version = str(settings.get("graphVersion") or "v21.0")
        templates = settings.get("templates") or {}
        entry = (templates.get(question.template.value) or {}).get(question.language.value)
        destination = e164(str((settings.get("standins") or {}).get(question.recipient) or ""))
        if not isinstance(entry, dict) or not destination or not token or token == "none-until-M4":
            raise WhatsAppDeliveryError("approved WhatsApp template or stand-in missing")
        name, language_code = str(entry.get("name") or ""), str(entry.get("languageCode") or "")
        body = str(entry.get("body") or "")
        if (
            not phone_id.isdigit()
            or not _VERSION.fullmatch(version)
            or not _NAME.fullmatch(name)
            or not _LANGUAGE.fullmatch(language_code)
            or language_code.split("_")[0] != question.language.value.lower()
            or body.count("{po}") != 1
            or body.count("{token}") != 1
            or re.search(r"[{}]", body.replace("{po}", "").replace("{token}", ""))
            or body.format(po=question.po_number, token=question.reference_token)
            != question.rendered_text
        ):
            raise WhatsAppDeliveryError("approved WhatsApp template does not match V-14 text")
        payload = {
            "messaging_product": "whatsapp",
            "to": destination.removeprefix("+"),
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": language_code},
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": question.po_number},
                            {"type": "text", "text": question.reference_token},
                        ],
                    }
                ],
            },
        }
        try:
            response = self.http.post(
                f"https://graph.facebook.com/{version}/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            response.raise_for_status()
            message_id = str(response.json()["messages"][0]["id"])
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
            raise WhatsAppDeliveryError("WhatsApp send outcome unavailable") from error
        if not message_id:
            raise WhatsAppDeliveryError("WhatsApp send outcome unavailable")
        return message_id
