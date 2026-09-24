"""Recorded German PDF and Indonesian photo inputs for offline evaluation."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from generate_signals import KRIEGER, Item, _email, _whatsapp
from synthetic_pdf import Line
from synthetic_pdf import build as build_pdf
from synthetic_photo import render


def signal(language: Literal["de", "id"], quantity: int, t0: datetime) -> Item:
    po = "4500001234"
    phrase = f"{quantity} {'Stueck' if language == 'de' else 'unit'}"
    if language == "de":
        pdf = build_pdf(
            [
                Line("LIEFERSCHEIN", size=16, bold=True),
                Line(f"Bestellung {po}"),
                Line(f"Menge {phrase}"),
            ],
            title=f"Lieferschein {po}",
        )
        return Item(
            "eval-multilingual",
            "EMAIL",
            "ACCEPTED",
            {
                "eval-multilingual.eml": _email(
                    t0,
                    32,
                    KRIEGER,
                    f"Bestellung {po}",
                    f"Guten Tag, Bestellung {po}: Menge {phrase} ist versandbereit.",
                    pdf=("eval-german.pdf", pdf),
                )
            },
            po=po,
            material="MAT-48219",
        )
    media = "990000000000001"
    return Item(
        "eval-multilingual",
        "WHATSAPP",
        "ACCEPTED",
        {
            "eval-multilingual.json": _whatsapp(
                t0,
                8,
                KRIEGER[2],
                KRIEGER[0],
                f"Pesanan {po}, jumlah {phrase} siap dikirim",
                media,
            ),
            f"media/{media}.png": render(
                [f"PESANAN {po}", f"JUMLAH {phrase}"],
                seed=quantity,
            ),
        },
        po=po,
        material="MAT-48219",
    )


class Language:
    def detect_dominant_language(self, Text: str) -> dict[str, Any]:
        code = "de" if "Menge" in Text else "id" if "jumlah" in Text else "en"
        return {"Languages": [{"LanguageCode": code, "Score": 0.99}]}


class Ocr:
    def __init__(self, quantity: int, language: Literal["de", "id"]) -> None:
        self.quantity = quantity
        self.language = language

    def analyze_document(self, **request: Any) -> dict[str, Any]:
        unit = "Stueck" if self.language == "de" else "unit"
        word = "Menge" if self.language == "de" else "JUMLAH"
        return {
            "Blocks": [
                {"Id": "line", "BlockType": "LINE", "Text": f"{word} {self.quantity} {unit}"},
                {
                    "Id": "word-qty",
                    "BlockType": "WORD",
                    "Text": str(self.quantity),
                    "Confidence": 71.0,
                },
                {"Id": "word-unit", "BlockType": "WORD", "Text": unit, "Confidence": 83.0},
            ]
        }


def locate(text: str, language: str) -> dict[str, str]:
    match = re.search(r"\b\d+ (?:Stueck|unit)\b", text)
    return {"QUANTITY": match.group()} if match else {}
