"""Ersetzungsliste: feste Suchbegriffe -> Ersatz, mit Übersetzungen für die Rückübersetzung.

Anders als Presidios Erkennung ist das eine deterministische Zeichenkettenersetzung, auch in
Teilstrings („HUGOleaner“ -> „<BOBO:4711>leaner“). Der Ersatz ist entweder ein Tag
<NAME:Nummer> oder Klartext, wenn der Nutzer ihn für eindeutig genug hält. Bei der
Wiederherstellung wird der Ersatz in der Sprache des umgebenden Textes eingesetzt – hat die
KI übersetzt, kommt die hinterlegte Übersetzung des Suchbegriffs hinein.

Regeln werden nie automatisch gelöscht: ohne Regel lassen sich ältere KI-Antworten nicht mehr
zurückführen. Deaktivierte Regeln ersetzen nicht mehr, werden aber weiter zurückgeführt.
"""

import csv
import io
import json
import re
import secrets
import threading
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,39}")
_LANG_RE = re.compile(r"[a-z]{2,3}")


def replacement(rule):
    if rule["format"] == "tag":
        return f"<{rule['name']}:{rule['number']}>"
    return rule["text"]


class RuleStore:
    def __init__(self, data_dir=DATA_DIR):
        self._path = Path(data_dir) / "replacements.json"
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._rules = json.loads(self._path.read_text(encoding="utf-8")) if self._path.exists() else []

    def all(self):
        with self._lock:
            return [dict(r) for r in self._rules]

    def replace_all(self, rules):
        """Komplette Liste speichern (die Oberfläche schickt immer alle Regeln)."""
        rules = validate(rules)
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rules, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path)
            self._rules = rules
        return self.all()


def validate(rules):
    """Normalisieren, fehlende IDs/Nummern vergeben, Mehrdeutigkeiten zurückweisen."""
    used_numbers = {r.get("number") for r in rules if r.get("format") == "tag" and r.get("number")}
    out, errors, seen = [], [], {}
    for i, raw in enumerate(rules, 1):
        r = {
            "id": raw.get("id") or uuid.uuid4().hex[:12],
            "enabled": bool(raw.get("enabled", True)),
            "search": (raw.get("search") or "").strip(),
            "language": (raw.get("language") or "de").strip().lower(),
            "case_sensitive": bool(raw.get("case_sensitive", True)),
            "whole_word": bool(raw.get("whole_word", False)),
            "format": "plain" if raw.get("format") == "plain" else "tag",
            "name": (raw.get("name") or "").strip().upper(),
            "number": str(raw.get("number") or "").strip(),
            "text": (raw.get("text") or "").strip(),
            "translations": {
                lang.strip().lower(): term.strip()
                for lang, term in (raw.get("translations") or {}).items()
                if lang.strip() and term and term.strip()
            },
        }
        label = f"Regel {i} ({r['search'] or 'ohne Suchbegriff'})"
        if not r["search"]:
            errors.append(f"{label}: Suchbegriff fehlt")
        if not _LANG_RE.fullmatch(r["language"]):
            errors.append(f"{label}: Sprache als ISO-Kürzel angeben (de, en, …)")
        for lang in r["translations"]:
            if not _LANG_RE.fullmatch(lang):
                errors.append(f"{label}: Übersetzungssprache „{lang}“ ist kein ISO-Kürzel")
        if r["format"] == "tag":
            if not _NAME_RE.fullmatch(r["name"]):
                errors.append(f"{label}: Name nur aus A–Z, 0–9, _ (beginnt mit Buchstaben)")
            if not r["number"]:
                r["number"] = _new_number(used_numbers)
                used_numbers.add(r["number"])
            elif not r["number"].isdigit():
                errors.append(f"{label}: Nummer nur aus Ziffern")
        elif not r["text"]:
            errors.append(f"{label}: Ersatztext fehlt")
        rep = replacement(r)
        # Gleicher Ersatz für zwei Suchbegriffe wäre bei der Wiederherstellung nicht auflösbar.
        if rep and rep in seen:
            errors.append(f"{label}: Ersatz „{rep}“ wird schon von Regel {seen[rep]} verwendet")
        seen[rep] = i
        out.append(r)
    if errors:
        raise ValueError("; ".join(errors))
    return out


def _new_number(used):
    for digits in (4, 5, 6, 8):
        for _ in range(50):
            number = str(secrets.randbelow(9 * 10 ** (digits - 1)) + 10 ** (digits - 1))
            if number not in used:
                return number
    raise ValueError("Keine freie Nummer gefunden")


def _non_overlapping(matches):
    """Längster Treffer gewinnt, bei gleicher Länge der frühere."""
    chosen = []
    for m in sorted(matches, key=lambda m: (m[0] - m[1], m[0])):
        if all(m[1] <= c[0] or m[0] >= c[1] for c in chosen):
            chosen.append(m)
    return sorted(chosen)


def find_forward(text, rules):
    """Treffer der aktiven Regeln im Originaltext: [(start, end, rule)]."""
    matches = []
    for r in rules:
        if not r["enabled"]:
            continue
        pattern = re.escape(r["search"])
        if r["whole_word"]:
            pattern = rf"(?<!\w){pattern}(?!\w)"
        flags = 0 if r["case_sensitive"] else re.IGNORECASE
        matches += [(m.start(), m.end(), r) for m in re.finditer(pattern, text, flags)]
    return _non_overlapping(matches)


def find_restore(text, rules):
    """Ersatzstellen in einer KI-Antwort: [(start, end, rule)] – auch deaktivierte Regeln.

    Tags tolerant wie bei den Presidio-Platzhaltern (Escaping, Leerraum, Groß/klein);
    Klartext-Ersatz wörtlich, Groß/klein wie beim Suchbegriff eingestellt, auch als Teilstring
    („BOBOs“ -> „HUGOs“).
    """
    matches = []
    for r in rules:
        if r["format"] == "tag":
            pattern = (rf"(?:\\?<|&lt;)[ \t]*{re.escape(r['name'])}[ \t]*:[ \t]*"
                       rf"{re.escape(r['number'])}[ \t]*(?:\\?>|&gt;)")
            flags = re.IGNORECASE
        else:
            pattern = re.escape(r["text"])
            if r["whole_word"]:
                pattern = rf"(?<!\w){pattern}(?!\w)"
            flags = 0 if r["case_sensitive"] else re.IGNORECASE
        matches += [(m.start(), m.end(), r) for m in re.finditer(pattern, text, flags)]
    return _non_overlapping(matches)


def term_for(rule, language):
    """(Begriff, übersetzt?) – Suchbegriff in seiner Sprache, sonst die Übersetzung."""
    if language == rule["language"] or language is None:
        return rule["search"], True
    if language in rule["translations"]:
        return rule["translations"][language], True
    return rule["search"], False


# --- CSV (Semikolon, UTF-8 mit BOM – öffnet in deutschem Excel direkt richtig) ----------------

_BASE_COLUMNS = ["aktiv", "suchbegriff", "sprache", "gross_klein_beachten", "ganzes_wort",
                 "format", "name", "nummer", "ersatztext"]


def to_csv(rules):
    langs = sorted({lang for r in rules for lang in r["translations"]})
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(_BASE_COLUMNS + [f"uebersetzung_{lang}" for lang in langs])
    for r in rules:
        writer.writerow(
            ["ja" if r["enabled"] else "nein", r["search"], r["language"],
             "ja" if r["case_sensitive"] else "nein", "ja" if r["whole_word"] else "nein",
             r["format"], r["name"], r["number"], r["text"]]
            + [r["translations"].get(lang, "") for lang in langs])
    return "﻿" + buf.getvalue()


def from_csv(data):
    """Regeln aus CSV (Trennzeichen ; oder , automatisch)."""
    text = data.decode("utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,\t")
    yes = lambda v: (v or "").strip().lower() in ("ja", "yes", "true", "1", "x")  # noqa: E731
    rules = []
    for row in csv.DictReader(io.StringIO(text), dialect=dialect):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        rules.append({
            "enabled": yes(row.get("aktiv", "ja")),
            "search": row.get("suchbegriff", ""),
            "language": row.get("sprache", "de"),
            "case_sensitive": yes(row.get("gross_klein_beachten", "ja")),
            "whole_word": yes(row.get("ganzes_wort", "nein")),
            "format": row.get("format", "tag") or "tag",
            "name": row.get("name", ""),
            "number": row.get("nummer", ""),
            "text": row.get("ersatztext", ""),
            "translations": {k.removeprefix("uebersetzung_"): v for k, v in row.items()
                             if k.startswith("uebersetzung_") and v},
        })
    return rules


def merge(existing, imported):
    """Import: gleicher Suchbegriff + Sprache aktualisiert die Regel, sonst neue Regel."""
    by_key = {(r["search"], r["language"]): r for r in existing}
    result = list(existing)
    for r in imported:
        key = (r["search"], (r.get("language") or "de").lower())
        if key in by_key:
            target = by_key[key]
            # Leere Zellen überschreiben nichts – eine neu vergebene Nummer würde den Ersatz
            # ändern und ältere KI-Antworten unauflösbar machen.
            for k, v in r.items():
                if k == "translations":
                    target["translations"] = {**target.get("translations", {}), **v}
                elif k != "id" and v not in ("", None):
                    target[k] = v
        else:
            result.append(r)
    return result
