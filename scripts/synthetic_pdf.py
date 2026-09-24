"""A minimal PDF writer for synthetic supplier documents (WP-3 data generator, tests).

Writes one-page PDFs with Helvetica text lines. A line can be hidden the ways attackers hide
text: white on white, invisible render mode, or near-zero size. No third-party library.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Hide = Literal["white", "invisible", "tiny"] | None


@dataclass(frozen=True)
class Line:
    text: str
    size: float = 11
    bold: bool = False
    hide: Hide = None


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content(lines: list[Line]) -> bytes:
    out = ["BT"]
    y = 800.0
    for line in lines:
        size = 0.5 if line.hide == "tiny" else line.size
        colour = "1 1 1 rg" if line.hide == "white" else "0 0 0 rg"
        mode = "3 Tr" if line.hide == "invisible" else "0 Tr"
        font = "/F2" if line.bold else "/F1"
        out.append(
            f"{colour} {mode} {font} {size:g} Tf 1 0 0 1 50 {y:g} Tm ({_escape(line.text)}) Tj"
        )
        y -= max(line.size, 4) * 1.5
    out.append("ET")
    return "\n".join(out).encode("latin-1", "replace")


def build(lines: list[Line], *, title: str = "") -> bytes:
    stream = _content(lines)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Title (" + _escape(title).encode("latin-1", "replace") + b") >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(output)
    output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        output += f"{offset:010d} 00000 n \n".encode()
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(output)
