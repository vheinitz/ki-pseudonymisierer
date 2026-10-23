"""Profile: manuell gewählte Bündel aus Voreinstellungen und einem Gazetteer bekannter Personen.

Ein Profil lockert die Anonymisierung gezielt: Namen aus seinem Gazetteer (z. B. verstorbene
Wissenschaftler – die DSGVO gilt nicht für Verstorbene, Erwägungsgrund 27) dürfen stehen
bleiben. Weil eine falsche Freigabe ein Datenschutzvorfall ist, eine falsche Ersetzung aber
nur ein Schönheitsfehler, gilt eine Freigabe je Gazetteer-Eintrag und nur wenn
  1. das Modell (GLiNER) den Namen mindestens einmal im Kontext als bekannte Person einordnet
     (sofern Bestätigungslabels konfiguriert sind), und
  2. keine Nennung im Dokument einen Sperrhinweis in der Nähe hat („Herr“, „aus dem“, „@“ …).
Sperrt eine Nennung, bleiben alle ersetzt – „Thomas Mann <t.mann@firma.de>“ im Kopf verrät,
dass auch das „Thomas Mann“ im Fließtext der Kollege ist.
Voreinstellungen (Schwelle, abgeschaltete Entitäten) übernimmt die Oberfläche sichtbar in
ihre Steuerfelder; serverseitig wirkt nur die Freigabelogik.
"""

import json
import re
import threading
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

DEFAULT_CONFIRM_LABELS = ["famous historical person", "author", "scientist"]
# Direkt am Namen (bis zu 3 Wörter davor/danach): Anrede, Arbeitsplatzbezug, Grußformel.
DEFAULT_BLOCKING_CUES = [
    "Herr", "Herrn", "Frau", "Hr.", "Fr.", "Kollege", "Kollegin", "Mitarbeiter", "Mitarbeiterin",
    "aus dem", "aus der", "vom", "Abteilung", "Team", "Vertrieb", "Buchhaltung", "Einkauf",
    "Geschäftsführer", "Geschäftsführerin", "Gruß", "Grüße", "Mr.", "Mrs.", "Ms.", "colleague",
    "department", "from the", "regards",
]
# Irgendwo in derselben Zeile (± cue_window Zeichen): Kontaktdaten.
DEFAULT_BLOCKING_CUES_LINE = ["@", "Tel.", "Telefon", "Durchwahl", "Fax", "phone"]

STARTER_PROFILES = [
    {
        "name": "Wissenschaftsgeschichte",
        "description": "Verstorbene Wissenschaftler und Erfinder dürfen stehen bleiben (DSGVO gilt nicht für Verstorbene, ErwG 27).",
        "known_persons": [
            "Isaac Newton | Newton", "Albert Einstein | Einstein",
            "Gottfried Wilhelm Leibniz | Leibniz", "Nikola Tesla | Tesla", "James Watt | Watt",
            "Marie Curie | Curie", "Charles Darwin | Darwin", "Galileo Galilei | Galilei",
            "Johannes Kepler | Kepler", "Max Planck | Planck", "Carl Friedrich Gauß | Gauß",
            "Leonhard Euler | Euler", "Werner Heisenberg | Heisenberg", "Niels Bohr | Bohr",
            "Alan Turing | Turing", "Ada Lovelace | Lovelace", "Robert Koch | Koch",
            "Louis Pasteur | Pasteur", "Michael Faraday | Faraday", "Thomas Edison | Edison",
        ],
    },
    {
        "name": "Literatur",
        "description": "Verstorbene Autorinnen und Autoren dürfen stehen bleiben (DSGVO gilt nicht für Verstorbene, ErwG 27).",
        "known_persons": [
            "Johann Wolfgang von Goethe | Goethe", "Friedrich Schiller | Schiller",
            "Thomas Mann", "Heinrich Mann", "Franz Kafka | Kafka", "Theodor Fontane | Fontane",
            "Hermann Hesse | Hesse", "Bertolt Brecht | Brecht", "Heinrich Heine | Heine",
            "Gotthold Ephraim Lessing | Lessing", "Rainer Maria Rilke | Rilke",
            "Georg Büchner | Büchner", "Heinrich von Kleist | Kleist", "Theodor Storm | Storm",
            "Heinrich Böll | Böll", "Günter Grass | Grass", "William Shakespeare | Shakespeare",
            "Charles Dickens | Dickens", "Leo Tolstoi | Tolstoi", "Fjodor Dostojewski | Dostojewski",
        ],
    },
]


def _new_profile(raw):
    return {
        "id": raw.get("id") or uuid.uuid4().hex[:12],
        "name": (raw.get("name") or "").strip(),
        "description": (raw.get("description") or "").strip(),
        "score_threshold": raw.get("score_threshold"),
        "entities_off": sorted({e.strip().upper() for e in raw.get("entities_off", []) if e.strip()}),
        "applies_to": sorted({e.strip().upper() for e in raw.get("applies_to", ["PERSON"]) if e.strip()})
                      or ["PERSON"],
        "known_persons": [l.strip() for l in raw.get("known_persons", []) if l.strip()],
        "confirm_labels": [l.strip() for l in raw.get("confirm_labels", DEFAULT_CONFIRM_LABELS) if l.strip()],
        "confirm_threshold": float(raw.get("confirm_threshold", 0.5)),
        "blocking_cues": [c.strip() for c in raw.get("blocking_cues", DEFAULT_BLOCKING_CUES) if c.strip()],
        "blocking_cues_line": [c.strip() for c in raw.get("blocking_cues_line", DEFAULT_BLOCKING_CUES_LINE)
                               if c.strip()],
        "cue_window": int(raw.get("cue_window", 60)),
    }


def validate(profiles):
    out, errors, names = [], [], set()
    for i, raw in enumerate(profiles, 1):
        p = _new_profile(raw)
        if not p["name"]:
            errors.append(f"Profil {i}: Name fehlt")
        elif p["name"].lower() in names or p["name"].lower() == "standard":
            errors.append(f"Profil {i}: Name „{p['name']}“ ist schon vergeben")
        names.add(p["name"].lower())
        st = p["score_threshold"]
        if st not in (None, ""):
            try:
                p["score_threshold"] = float(st)
                if not 0 <= p["score_threshold"] <= 1:
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"Profil {i}: Score-Schwelle zwischen 0 und 1 angeben")
        else:
            p["score_threshold"] = None
        if not 0 <= p["confirm_threshold"] <= 1:
            errors.append(f"Profil {i}: Mindest-Score der Bestätigung zwischen 0 und 1 angeben")
        out.append(p)
    if errors:
        raise ValueError("; ".join(errors))
    return out


class ProfileStore:
    def __init__(self, data_dir=DATA_DIR):
        self._path = Path(data_dir) / "profiles.json"
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if self._path.exists():
            self._profiles = json.loads(self._path.read_text(encoding="utf-8"))
        else:
            self._profiles = []
            self.replace_all(STARTER_PROFILES)

    def all(self):
        with self._lock:
            return [dict(p) for p in self._profiles]

    def get(self, profile_id):
        return next((p for p in self.all() if p["id"] == profile_id), None)

    def replace_all(self, profiles):
        profiles = validate(profiles)
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path)
            self._profiles = profiles
        return self.all()


# --- Freigabe ---------------------------------------------------------------------------------

def _occurrences(text, variants):
    """Alle Vorkommen der Schreibweisen im Text (ganze Wörter, auch mit Genitiv-s)."""
    alternatives = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
    return [(m.start(), m.end()) for m in
            re.finditer(rf"(?<![\w’'])(?:{alternatives})(?:’s|'s|s)?(?![\w’'])", text)]


def _gazetteer(profile):
    """{Schreibweise: Eintrag (erste Schreibweise der Zeile)}."""
    lookup = {}
    for line in profile["known_persons"]:
        variants = [" ".join(v.split()) for v in line.split("|") if v.strip()]
        for v in variants:
            lookup[v] = variants[0]
    return lookup


def _match(span_text, gazetteer):
    name = " ".join(span_text.split())
    if name in gazetteer:
        return gazetteer[name]
    # Genitiv/Possessiv: „Newtons“, „Newton’s“, „Newton's“
    for suffix in ("’s", "'s", "s"):
        if name.endswith(suffix) and name[: -len(suffix)] in gazetteer:
            return gazetteer[name[: -len(suffix)]]
    return None


def _line_window(text, start, end, width):
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end < 0 else line_end
    return max(line_start, start - width), min(line_end, end + width)


def _cue_pattern(cue):
    return re.escape(cue) if not cue[0].isalnum() else rf"(?<!\w){re.escape(cue)}(?!\w)"


def _cue(text, start, end, profile):
    # Nahbereich: je 3 Wörter davor und danach (auch über Zeilenumbrüche, für Grußformeln) –
    # ein weiteres Fenster würde Satzteile wie „Referenzen aus der Geschichte: Newton“ treffen.
    before = " ".join(text[max(0, start - 200):start].split()[-3:])
    after = " ".join(text[end:end + 200].split()[:3])
    near = before + " | " + after
    for cue in profile["blocking_cues"]:
        if re.search(_cue_pattern(cue), near, re.IGNORECASE):
            return cue
    ws, we = _line_window(text, start, end, profile["cue_window"])
    line = text[ws:start] + " " + text[end:we]
    for cue in profile["blocking_cues_line"]:
        if re.search(_cue_pattern(cue), line, re.IGNORECASE):
            return cue
    return None


def _confirm(text, start, end, profile, gliner):
    """(Label, Score) der GLiNER-Einordnung dieser Nennung im Kontext ihrer Zeile."""
    ws, we = _line_window(text, start, end, 300)
    labels = ["person"] + profile["confirm_labels"]
    best = (None, 0.0)
    for p in gliner.predict_entities(text[ws:we], labels, threshold=0.3, flat_ner=True):
        if p["start"] + ws < end and p["end"] + ws > start and p["score"] > best[1]:
            best = (p["label"], p["score"])
    return best


def apply(text, results, profile, gliner_loader):
    """Freigaben nach Profil: (verbleibende Ergebnisse, freigegeben, nicht freigegeben).

    gliner_loader: liefert das GLiNER-Modell (nur aufgerufen, wenn Bestätigung nötig ist).
    """
    gazetteer = _gazetteer(profile)
    if not gazetteer:
        return results, [], []
    candidates = {}  # Eintrag -> [Ergebnis]
    for r in results:
        if r.entity_type in profile["applies_to"]:
            entry = _match(text[r.start:r.end], gazetteer)
            if entry:
                candidates.setdefault(entry, []).append(r)

    released, blocked, release_spans = [], [], set()
    gliner = None
    for entry, mentions in candidates.items():
        reason = None
        # Sperrhinweise an *allen* Vorkommen prüfen, nicht nur an den erkannten: die NER
        # erfasst „Herrn Newton aus dem Labor“ mit Anrede, das passt zu keinem Eintrag.
        variants = [v for v, e in gazetteer.items() if e == entry]
        for start, end in _occurrences(text, variants):
            cue = _cue(text, start, end, profile)
            if cue:
                reason = f"Sperrhinweis „{cue}“ bei „{text[start:end]}“ (Position {start})"
                break
        confirmation = None
        if not reason and profile["confirm_labels"]:
            if gliner is None:
                gliner = gliner_loader()
            for r in mentions:
                label, score = _confirm(text, r.start, r.end, profile, gliner)
                if label in profile["confirm_labels"] and score >= profile["confirm_threshold"]:
                    confirmation = f"vom Modell als „{label}“ eingeordnet ({score:.2f})"
                    break
            if not confirmation:
                reason = "vom Modell nicht als bekannte Person bestätigt"
        info = {"entry": entry, "mentions": [text[r.start:r.end] for r in mentions],
                "count": len(mentions)}
        if reason:
            blocked.append(info | {"reason": reason})
        else:
            released.append(info | {"reason": confirmation or "im Gazetteer, kein Sperrhinweis"})
            release_spans.update((r.start, r.end) for r in mentions)
    # Freigabe gilt für die Textstelle, auch wenn sie zusätzlich unter anderem Typ markiert ist
    # (GLiNER: „Tesla“ als ORGANIZATION neben PERSON).
    return [r for r in results if (r.start, r.end) not in release_spans], released, blocked
