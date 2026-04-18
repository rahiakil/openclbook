from __future__ import annotations

import html
import io
import re
import zipfile
from pathlib import Path


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _odt_bytes_to_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("content.xml").decode("utf-8", errors="replace")
    return _html_to_text(xml)


def read_document_from_bytes(filename: str, data: bytes) -> str:
    """Read plain text from uploaded bytes (suffix selects parser)."""
    suf = Path(filename).suffix.lower()
    if suf in {".md", ".txt", ".markdown"}:
        return data.decode("utf-8", errors="replace")
    if suf == ".docx":
        from docx import Document

        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
        return "\n\n".join(parts)
    if suf in {".html", ".htm"}:
        return _html_to_text(data.decode("utf-8", errors="replace"))
    if suf == ".odt":
        return _odt_bytes_to_text(data)
    if suf == ".rtf":
        try:
            from striprtf.striprtf import rtf_to_text
        except ImportError as e:  # pragma: no cover
            raise ValueError(
                "RTF requires the striprtf package (pip install striprtf)."
            ) from e
        return rtf_to_text(data.decode("latin-1", errors="replace")).strip()
    raise ValueError(
        f"Unsupported format {suf!r}. Supported: .md, .txt, .docx, .html, .odt, .rtf."
    )


def read_document(path: Path) -> str:
    suf = path.suffix.lower()
    if suf in {".md", ".txt", ".markdown", ".docx", ".html", ".htm", ".odt", ".rtf"}:
        return read_document_from_bytes(path.name, path.read_bytes())
    raise ValueError(
        f"Unsupported format {suf!r}: use .md, .txt, .docx, .html, .odt, .rtf "
        f"(other formats: convert with pandoc/LibreOffice first)."
    )


def split_by_h2(text: str) -> list[tuple[str, str]]:
    """Split markdown-ish text on ## headings; returns (title, body) pairs."""
    pattern = re.compile(r"^##\s+(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return [("document", text.strip())]
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        out.append((title, body))
    return out


def write_sections(
    workspace_sections: Path,
    pairs: list[tuple[str, str]],
    *,
    prefix: str = "sec",
) -> list[Path]:
    workspace_sections.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for i, (title, body) in enumerate(pairs, start=1):
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in title.lower())[:60]
        name = f"{prefix}-{i:03d}-{safe}.md" if safe else f"{prefix}-{i:03d}.md"
        path = workspace_sections / name
        path.write_text(f"## {title}\n\n{body}\n", encoding="utf-8")
        written.append(path)
    return written
