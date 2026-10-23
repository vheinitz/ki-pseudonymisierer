"""Deny-List: Begriffe, die immer als bestimmte Entität erkannt werden (Score 1,0).

Für Namen, die die Modelle nicht zuverlässig finden (seltene oder fremdsprachige Namen,
Tabellen ohne Satzkontext). Eine Zeile je Eintrag, optional mit Typ: „ORGANIZATION: Acme“;
ohne Typ gilt PERSON. Leerraum im Eintrag passt auf beliebigen Leerraum (auch Tabulatoren
und Zeilenumbrüche), Groß-/Kleinschreibung egal, nur ganze Wörter.
"""

import json
import re
import threading
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
_TYPED = re.compile(r"([A-Z][A-Z0-9_]{1,39})\s*:\s*(.+)")


def parse(lines):
    """[(Typ, Begriff)] aus Textzeilen."""
    out = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _TYPED.fullmatch(line)
        out.append((m.group(1), m.group(2).strip()) if m else ("PERSON", line))
    return out


def find(text, lines):
    """[(Typ, start, end, Eintrag)] – längere Einträge zuerst, ohne Überlappung."""
    found, taken = [], []
    for entity_type, term in sorted(parse(lines), key=lambda x: -len(x[1])):
        pattern = r"\s+".join(re.escape(p) for p in term.split())
        for m in re.finditer(rf"(?<![\w’'-]){pattern}(?![\w’'-])", text, re.IGNORECASE):
            if any(m.start() < e and m.end() > s for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            found.append((entity_type, m.start(), m.end(), term))
    return found


class DenyListStore:
    def __init__(self, data_dir=DATA_DIR):
        self._path = Path(data_dir) / "deny_list.json"
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            return json.loads(self._path.read_text(encoding="utf-8")) if self._path.exists() else []

    def put(self, lines):
        lines = [l.rstrip() for l in lines if l.strip()]
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(lines, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path)
        return lines


# --- Tabellenspalten mit Namensüberschrift ----------------------------------------------------

# Spaltenüberschriften, deren Zellen Personennamen sind (Vergleich ohne Groß/klein, Punkt, Doppelpunkt).
NAME_HEADERS = {
    "name", "namen", "vorname", "vornamen", "nachname", "familienname", "zuname", "rufname",
    "geburtsname", "name, vorname", "vorname name", "vorname, name", "schüler", "schülerin",
    "teilnehmer", "teilnehmerin", "mitarbeiter", "mitarbeiterin", "patient", "patientin",
    "ansprechpartner", "ansprechpartnerin", "kunde", "kundin",
    "first name", "last name", "surname", "given name", "full name", "student", "employee",
}
# Spaltenüberschriften, deren Zellen Orte sind.
LOCATION_HEADERS = {
    "ort", "stadt", "wohnort", "geburtsort", "gemeinde", "city", "town", "place of birth",
    "birthplace", "city of birth",
}
# Tabulator, mehrere Leerzeichen (aus PDF/Text), Semikolon (CSV) oder senkrechter Strich
# (Markdown). Komma nicht: „Name, Vorname“ ist selbst eine übliche Überschrift.
_CELL_SPLIT = re.compile(r"\t| {2,}|\s*;\s*|\s*\|\s*")


def _cells(line, offset):
    pos, out = 0, []
    for m in list(_CELL_SPLIT.finditer(line)) + [None]:
        end = m.start() if m else len(line)
        out.append((offset + pos, offset + end))
        pos = m.end() if m else len(line)
    return out


def find_table_cells(text):
    """Zellen unter Namens- und Ortsüberschriften, bis zur nächsten Leerzeile.

    Gibt ([(start, end, "PERSON"|"LOCATION")], [(start, end)] der Überschriftszellen) zurück;
    die Überschriften selbst sind keine Fundstellen („Stadt“ ist kein Ort).
    """
    found, headers, columns, offset = [], [], None, 0
    for line in text.split("\n"):
        cells = _cells(line, offset)
        labels = [text[s:e].strip().strip(".:").lower() for s, e in cells]
        kinds = {i: ("PERSON" if l in NAME_HEADERS else "LOCATION")
                 for i, l in enumerate(labels) if l in NAME_HEADERS or l in LOCATION_HEADERS}
        if len(cells) >= 2 and kinds:
            columns = kinds
            headers += [cells[i] for i in kinds]
        elif not line.strip():
            columns = None
        elif columns:
            for i, kind in columns.items():
                if i < len(cells):
                    s, e = cells[i]
                    value = text[s:e].strip()
                    # nur Zellen mit Buchstaben, ohne Ziffern (keine Summenzeilen, Beträge)
                    if value and any(c.isalpha() for c in value) and not any(c.isdigit() for c in value):
                        s += len(text[s:e]) - len(text[s:e].lstrip())
                        found.append((s, s + len(value), kind))
        offset += len(line) + 1
    return found, headers


def find_table_names(text):
    """[(start, end)] der Zellen unter Namensüberschriften."""
    return [(s, e) for s, e, kind in find_table_cells(text)[0] if kind == "PERSON"]
