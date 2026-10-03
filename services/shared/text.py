"""Text extraction for scanning (FR-ING-05): what a reader sees, and what is hidden.

Prompt attacks hide instructions where a planner would not look: in HTML elements styled
invisible, in comments, in attributes, and in PDF text layers (white or zero-size text,
invisible render mode, metadata). The gatekeeper scans both halves; the console shows the
visible half and flags the hidden half.
"""

from __future__ import annotations

import io
import re

from bs4 import BeautifulSoup, Comment, Tag

_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|font-size\s*:\s*0(?:\.0+)?(?:px|pt|em|rem|%)?\s*(?:;|$|!)"
    r"|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$|!)"
    r"|(?<![-\w])color\s*:\s*(?:#fff(?:fff)?\b|white\b|rgba?\(\s*255\s*,\s*255\s*,\s*255)"
    r"|(?:max-)?height\s*:\s*0(?:px)?\s*(?:;|$|!)"
    r"|text-indent\s*:\s*-\d{3,}"
    r"|(?:left|top)\s*:\s*-\d{3,}",
    re.IGNORECASE,
)
_ATTRIBUTES = ("alt", "title", "aria-label", "data-instructions")


def _hidden(tag: Tag) -> bool:
    if tag.name in ("script", "style", "template", "noscript", "head"):
        return True
    if tag.has_attr("hidden") or tag.get("aria-hidden") == "true":
        return True
    style = tag.get("style")
    return isinstance(style, str) and bool(_HIDDEN_STYLE.search(style))


def _squash(parts: list[str]) -> str:
    return re.sub(
        r"[ \t]*\n\s*", "\n", " ".join(p.strip() for p in parts if p and p.strip())
    ).strip()


def html_texts(html: str) -> tuple[str, str]:
    """(visible text, hidden text) of an HTML document."""
    soup = BeautifulSoup(html, "html.parser")
    hidden: list[str] = []
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        hidden.append(str(comment))
        comment.extract()
    for tag in soup.find_all(True):
        if not isinstance(tag, Tag):
            continue
        for attribute in _ATTRIBUTES:
            value = tag.get(attribute)
            if isinstance(value, str):
                hidden.append(value)
    for tag in list(soup.find_all(True)):
        if isinstance(tag, Tag) and not tag.decomposed and _hidden(tag):
            hidden.append(tag.get_text(" "))
            tag.decompose()
    visible = soup.get_text("\n")
    return _squash(visible.splitlines()), _squash(hidden)


def visible_html_text(html: str) -> str:
    return html_texts(html)[0]


def pdf_text(data: bytes) -> str:
    """Every text layer of a PDF, visible or not, plus its metadata and annotations."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        parts = [page.extract_text() or "" for page in reader.pages]
        for page in reader.pages:
            for annotation in page.get("/Annots") or []:
                contents = annotation.get_object().get("/Contents")
                if contents:
                    parts.append(str(contents))
        metadata: dict[str, object] = dict(reader.metadata or {})
        parts.extend(
            str(v) for k, v in metadata.items() if k in ("/Title", "/Subject", "/Keywords")
        )
    except (PdfReadError, ValueError, KeyError, TypeError):
        return ""
    return _squash(parts)
