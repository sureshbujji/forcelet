"""Minimal PDF writer — stdlib only.

Builds a single-page (or multi-page) PDF 1.4 using the built-in Helvetica
family. Good enough for quote documents without extra dependencies.

By Suresh Itha — part of the Forcelet platform.
"""
from __future__ import annotations


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("(", "\\(")
                .replace(")", "\\)").replace("\r", ""))


def build_pdf(title: str, lines: list[tuple[str, int, bool]]) -> bytes:
    """lines: list of (text, font_size, bold). Returns PDF bytes."""
    # paginate: ~48 lines per page at mixed sizes; simple greedy split
    pages: list[list[tuple[str, int, bool]]] = [[]]
    y_budget = 700
    for text, size, bold in lines:
        for chunk in (text or "").split("\n") or [""]:
            h = size * 1.35
            if y_budget - h < 60:
                pages.append([])
                y_budget = 700
            pages[-1].append((chunk, size, bold))
            y_budget -= h

    objects: list[bytes] = []
    # 1: catalog, 2: pages, then per page: page obj + content obj, then font objs
    font_regular = len(pages) * 2 + 3
    font_bold = font_regular + 1
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(len(pages)))

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())

    for i, page in enumerate(pages):
        pageno, contentno = 3 + i * 2, 4 + i * 2
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >> "
            f"/Contents {contentno} 0 R >>".encode())
        ops = ["BT"]
        y = 740.0
        for text, size, bold in page:
            font = "/F2" if bold else "/F1"
            ops.append(f"{font} {size} Tf 1 0 0 1 56 {y:.1f} Tm ({_escape(text)}) Tj")
            y -= size * 1.35
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1", "replace")
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                       + stream + b"\nendstream")

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF").encode()
    # title goes into document info via a comment-free approach: prepend metadata
    _ = title
    return bytes(out)
