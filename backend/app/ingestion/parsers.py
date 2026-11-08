"""Document parsers. Supported: PDF, DOCX, XLSX, HTML, Markdown, plain text.

Only permissive libraries are used (pypdf BSD, python-docx MIT, openpyxl MIT, bs4 MIT).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

SUPPORTED = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
}


class UnsupportedFormat(ValueError):
    pass


@dataclass
class Section:
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


def mime_for(filename: str) -> str:
    ext = PurePath(filename).suffix.lower()
    if ext not in SUPPORTED:
        raise UnsupportedFormat(f"Unsupported file type: {ext or filename!r}")
    return SUPPORTED[ext]


def parse(data: bytes, filename: str) -> list[Section]:
    ext = PurePath(filename).suffix.lower()
    if ext == ".pdf":
        return _pdf(data)
    if ext == ".docx":
        return _docx(data)
    if ext == ".xlsx":
        return _xlsx(data)
    if ext in {".html", ".htm"}:
        return _html(data)
    if ext in {".md", ".markdown"}:
        return _markdown(_decode(data))
    if ext == ".txt":
        return [Section(_decode(data))]
    raise UnsupportedFormat(f"Unsupported file type: {ext}")


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def _markdown(text: str) -> list[Section]:
    sections: list[Section] = []
    heading: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append(Section(body, {"heading": " > ".join(heading)} if heading else {}))
        buf.clear()

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            heading[:] = [*heading[: level - 1], m.group(2).strip()]
            buf.append(line)
        else:
            buf.append(line)
    flush()
    return sections


def _pdf(data: bytes) -> list[Section]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    out = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            out.append(Section(text, {"page": i}))
    return out


def _docx(data: bytes) -> list[Section]:
    import docx

    document = docx.Document(io.BytesIO(data))
    sections: list[Section] = []
    heading = ""
    buf: list[str] = []
    for para in document.paragraphs:
        style = (para.style.name if para.style is not None else "") or ""
        text = para.text.strip()
        if not text:
            continue
        if style.lower().startswith("heading") or style.lower() == "title":
            if buf:
                sections.append(Section("\n".join(buf), {"heading": heading} if heading else {}))
                buf = []
            heading = text
        buf.append(text)
    for table in document.tables:
        rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        buf.append("\n".join(rows))
    if buf:
        sections.append(Section("\n".join(buf), {"heading": heading} if heading else {}))
    return sections


def _xlsx(data: bytes) -> list[Section]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sections = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        header = [str(h) if h is not None else f"col{i + 1}" for i, h in enumerate(rows[0])]
        lines = []
        for row in rows[1:]:
            if all(v is None for v in row):
                continue
            lines.append("; ".join(f"{h}: {v}" for h, v in zip(header, row, strict=False) if v is not None))
        if lines:
            sections.append(Section(f"Sheet {ws.title}\n" + "\n".join(lines), {"sheet": ws.title}))
    return sections


def _html(data: bytes) -> list[Section]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_decode(data), "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(strip=True) if soup.title else ""
    body = soup.body or soup
    lines = [s.strip() for s in body.get_text("\n").splitlines()]
    text = "\n".join(line for line in lines if line)
    return [Section(text, {"heading": title} if title else {})] if text else []
