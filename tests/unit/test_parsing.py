from __future__ import annotations

import io

import pytest

from app.ingestion.chunker import chunk_sections
from app.ingestion.parsers import Section, UnsupportedFormat, mime_for, parse


def _pdf_with_text(lines: list[str]) -> bytes:
    """Build a minimal valid PDF with one text page (no PDF library needed)."""
    content = "BT /F1 12 Tf 72 720 Td " + " ".join(f"({t}) Tj 0 -16 Td" for t in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n{body}\nendobj\n".encode())
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def test_markdown_tracks_headings() -> None:
    md = "# Handbook\nIntro.\n\n## Leave\nTwenty days.\n\n## Travel\nBook early."
    sections = parse(md.encode(), "h.md")
    assert [s.meta.get("heading") for s in sections] == ["Handbook", "Handbook > Leave", "Handbook > Travel"]
    assert "Twenty days." in sections[1].text


def test_txt_html() -> None:
    assert parse(b"plain text", "a.txt")[0].text == "plain text"
    html = b"<html><head><title>T</title><script>evil()</script></head><body><h1>Hi</h1><p>Body</p></body></html>"
    s = parse(html, "a.html")[0]
    assert "evil" not in s.text and "Body" in s.text and s.meta == {"heading": "T"}


def test_docx_and_xlsx() -> None:
    import docx
    import openpyxl

    d = docx.Document()
    d.add_heading("Salary bands", level=1)
    d.add_paragraph("Senior engineer: band E4.")
    buf = io.BytesIO()
    d.save(buf)
    s = parse(buf.getvalue(), "x.docx")
    assert s[0].meta["heading"] == "Salary bands" and "band E4" in s[0].text

    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Invoices"
    ws.append(["id", "amount"])
    ws.append(["INV-1", 12000])
    buf = io.BytesIO()
    wb.save(buf)
    s = parse(buf.getvalue(), "x.xlsx")
    assert s[0].meta == {"sheet": "Invoices"} and "id: INV-1; amount: 12000" in s[0].text


def test_pdf() -> None:
    s = parse(_pdf_with_text(["Quarterly report", "Revenue grew"]), "r.pdf")
    assert s[0].meta == {"page": 1} and "Revenue grew" in s[0].text


def test_unsupported() -> None:
    with pytest.raises(UnsupportedFormat):
        mime_for("malware.exe")
    with pytest.raises(UnsupportedFormat):
        parse(b"x", "a.pptx")


def test_chunker_respects_size_and_keeps_meta() -> None:
    para = "This is a sentence about policy. " * 20
    text = "\n\n".join([para] * 6)
    drafts = chunk_sections([Section(text, {"heading": "H"})], max_chars=1200, overlap_chars=150)
    assert len(drafts) > 2
    assert all(len(d.text) <= 1200 + 150 + 2 for d in drafts)
    assert [d.ordinal for d in drafts] == list(range(len(drafts)))
    assert all(d.meta == {"heading": "H"} for d in drafts)
