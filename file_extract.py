"""Text aus hochgeladenen Dateien: .txt/.md, .docx, .pdf; .doc/.odt/.rtf über LibreOffice."""

import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".log", ".json", ".xml", ".html", ".htm"}
OFFICE_SUFFIXES = {".doc", ".odt", ".rtf"}
SUPPORTED = sorted(TEXT_SUFFIXES | OFFICE_SUFFIXES | {".docx", ".pdf"})


def extract_text(filename, data):
    """(text, warnings) aus Dateiname und Inhalt."""
    suffix = Path(filename).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return _decode(data)
    if suffix == ".docx":
        return _docx(data), []
    if suffix == ".pdf":
        return _pdf(data)
    if suffix in OFFICE_SUFFIXES:
        return _libreoffice(data, suffix)
    raise ValueError(f"Dateityp {suffix or '(ohne Endung)'} wird nicht unterstützt "
                     f"(unterstützt: {', '.join(SUPPORTED)})")


def _decode(data):
    try:
        return data.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), [
            "Datei ist nicht UTF-8-kodiert, als Windows-1252 gelesen – Umlaute prüfen."]


def _docx(data):
    import docx

    document = docx.Document(io.BytesIO(data))
    parts = []
    # Kopf- und Fußzeilen zuerst: Briefköpfe enthalten oft Anschrift und Kontaktdaten.
    seen = set()
    for section in document.sections:
        for part in (section.header, section.first_page_header, section.footer,
                     section.first_page_footer):
            text = "\n".join(p.text for p in part.paragraphs).strip()
            if text and text not in seen:
                seen.add(text)
                parts.append(text)
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            parts.append("".join(t.text or "" for t in child.iter() if t.tag.endswith("}t")))
        elif tag == "tbl":
            table = docx.table.Table(child, document)
            for row in table.rows:
                cells = []
                for cell in row.cells:  # verbundene Zellen nur einmal
                    if not cells or cell.text != cells[-1]:
                        cells.append(cell.text)
                parts.append("\t".join(cells))
    return "\n".join(parts)


def _pdf(data):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(p.strip("\n") for p in pages)
    # Silbentrennung am Zeilenende mit typografischem (U+2010) oder weichem Trennstrich (U+00AD)
    # zusammenfügen; normale Bindestriche bleiben („Müller-\nBau“ kann ein echter sein).
    text = re.sub(r"(\w)[‐­]\n(\w)", r"\1\2", text)
    # Zeilenumbruch nach Bindestrich innerhalb von E-Mail-Adressen und URLs: der Bindestrich
    # gehört dort zur Adresse („rechnung@muster-\nsoftware.de“); getrennt würde der Erkenner
    # nur den ersten Teil finden und der Rest bliebe im Klartext.
    text = re.sub(r"(\S*(?:@|://|www\.)\S*-)\n(?=\w)", r"\1", text)
    warnings = []
    if len(text.strip()) < 20 * max(len(pages), 1):  # < 20 Zeichen pro Seite
        warnings.append("Kaum Text gefunden – vermutlich ein gescanntes PDF ohne Textebene. "
                        "Texterkennung (OCR) ist nicht enthalten.")
    return text, warnings


def _libreoffice(data, suffix):
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise ValueError(f"Für {suffix}-Dateien wird LibreOffice benötigt (soffice nicht gefunden).")
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / f"upload{suffix}"
        source.write_bytes(data)
        # Eigenes Profil: Läuft LibreOffice schon (z. B. am Desktop), würde die Konvertierung
        # sonst stillschweigend an die laufende Instanz gehen und nichts erzeugen.
        profile = Path(tmp) / "profile"
        # Nach .docx statt direkt nach Text: der Textexport lässt Kopf- und Fußzeilen weg,
        # also gerade den Briefkopf mit Anschrift.
        subprocess.run(
            [soffice, f"-env:UserInstallation=file://{profile}", "--headless",
             "--convert-to", "docx:MS Word 2007 XML", "--outdir", tmp, str(source)],
            check=True, capture_output=True, timeout=120,
        )
        target = source.with_suffix(".docx")
        if not target.exists():
            raise ValueError("LibreOffice konnte die Datei nicht umwandeln.")
        return _docx(target.read_bytes()), []
