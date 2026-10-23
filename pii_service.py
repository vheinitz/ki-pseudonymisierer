"""Presidio-Anbindung: Engines laden, PII erkennen, anonymisieren, entschlüsseln."""

import os
from pathlib import Path as _Path

# Hugging Face fragt sonst bei jedem Laden online nach neuen Modellversionen – hängt huggingface.co,
# hängt der Start (und jedes Laden ruft nach außen). Liegen die Modelle im Cache, offline arbeiten.
# Muss vor dem ersten Import von huggingface_hub/transformers gesetzt sein; HF_HUB_OFFLINE=0
# in der Umgebung erzwingt den Online-Modus (z. B. um ein neues Modell herunterzuladen).
_HF_CACHE = _Path(os.environ.get("HF_HOME", _Path.home() / ".cache" / "huggingface")) / "hub"
if (_HF_CACHE / "models--urchade--gliner_multi_pii-v1").exists():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import copy
import hashlib
import json
import re
import threading
from pathlib import Path

import spacy.util
from presidio_analyzer import predefined_recognizers
from presidio_analyzer import (AnalysisExplanation, AnalyzerEngine, Pattern, PatternRecognizer,
                               RecognizerRegistry, RecognizerResult)
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine, DeanonymizeEngine
from presidio_anonymizer.entities import OperatorConfig, OperatorResult

import language_id
import deny_list
import placeholders
import profiles
import recognizers
import replacement_rules

BASE_DIR = Path(__file__).resolve().parent
ENGINES_FILE = BASE_DIR / "engines.json"

# Parameter je Presidio-Operator, wie sie die Oberfläche anbietet.
OPERATORS = {
    "placeholder": {"same_id_for_same_value": "bool"},
    "replace": {"new_value": "str"},
    "redact": {},
    "mask": {"masking_char": "str", "chars_to_mask": "int", "from_end": "bool"},
    "hash": {"hash_type": "str", "salt": "str"},
    "encrypt": {"key": "str"},
    "keep": {},
}

anonymizer = AnonymizerEngine()
anonymizer.add_anonymizer(placeholders.PlaceholderOperator)
deanonymizer = DeanonymizeEngine()


def _resolve_model(model_name):
    """Pfadangaben relativ zum Projekt auflösen; None, wenn das Modell fehlt."""
    if "/" in model_name:
        path = (BASE_DIR / model_name).resolve()
        return str(path) if (path / "meta.json").exists() else None
    return model_name if spacy.util.is_package(model_name) else None


class EngineRegistry:
    """Liest engines.json und lädt AnalyzerEngines erst bei Bedarf (spaCy-Modelle sind groß)."""

    def __init__(self, config_file=ENGINES_FILE):
        self._defs = {}
        self._analyzers = {}
        self._lock = threading.Lock()
        for definition in json.loads(Path(config_file).read_text(encoding="utf-8")):
            nlp_conf = copy.deepcopy(definition["nlp_configuration"])
            available, missing = [], []
            for model in nlp_conf["models"]:
                resolved = _resolve_model(model["model_name"])
                if resolved:
                    model["model_name"] = resolved
                    available.append(model)
                else:
                    missing.append(f'{model["lang_code"]}: {model["model_name"]}')
            nlp_conf["models"] = available
            self._defs[definition["id"]] = {
                "id": definition["id"],
                "label": definition["label"],
                "languages": [m["lang_code"] for m in available],
                "missing_models": missing,
                "nlp_configuration": nlp_conf,
                "additional_recognizers": definition.get("additional_recognizers", {}),
                "remove_recognizers": definition.get("remove_recognizers", []),
            }

    def describe(self):
        return [
            {k: d[k] for k in ("id", "label", "languages", "missing_models")}
            for d in self._defs.values()
        ]

    def get(self, engine_id):
        definition = self._defs.get(engine_id)
        if definition is None:
            raise ValueError(f"Unbekannte Engine: {engine_id}")
        if not definition["languages"]:
            raise ValueError(
                f"Engine '{engine_id}' hat kein installiertes Modell "
                f"(fehlt: {', '.join(definition['missing_models'])})"
            )
        with self._lock:
            if engine_id not in self._analyzers:
                langs = definition["languages"]
                nlp_engine = NlpEngineProvider(
                    nlp_configuration=definition["nlp_configuration"]
                ).create_engine()
                registry = RecognizerRegistry(supported_languages=langs)
                registry.load_predefined_recognizers(languages=langs, nlp_engine=nlp_engine)
                # libphonenumber prüft standardmäßig nur die Form (leniency=1) und hält z. B.
                # „08/2026 7“ (aus „Übertrag 08/2026 7,20 €“) für eine Nummer; VALID prüft
                # zusätzlich, ob der Nummernbereich tatsächlich vergeben ist.
                registry.remove_recognizer("PhoneRecognizer")
                for lang in langs:
                    registry.add_recognizer(
                        predefined_recognizers.PhoneRecognizer(supported_language=lang, leniency=2))
                registry.remove_recognizer("EmailRecognizer")
                for lang in langs:
                    registry.add_recognizer(recognizers.LenientEmailRecognizer(supported_language=lang))
                # Presidio liefert z. B. die deutschen Erkenner (LANR, KV-Nummer, …)
                # deaktiviert aus; engines.json schaltet sie je Sprache zu.
                # "*": für alle Sprachen der Engine. Kennnummern stehen oft in einem Textblock
                # anderer Sprache (italienische Steuernummer in einer deutschen Akte); Presidio
                # registriert seine Länder-Erkenner aber nur für die Landessprache.
                for lang, entries in definition["additional_recognizers"].items():
                    for target in (langs if lang == "*" else [lang] if lang in langs else []):
                        for entry in entries:
                            # Eintrag: Klassenname oder {"class": ..., "params": {...}}
                            if isinstance(entry, str):
                                entry = {"class": entry}
                            name = entry["class"]
                            cls = getattr(recognizers, name, None) or getattr(predefined_recognizers, name)
                            recognizer = cls(supported_language=target, **entry.get("params", {}))
                            if any(r.name == recognizer.name and r.supported_language == target
                                   for r in registry.recognizers):
                                continue  # schon von Presidio für diese Sprache geladen
                            registry.add_recognizer(recognizer)
                # z. B. SpacyRecognizer, wenn ein anderes Modell (GLiNER) die NER übernimmt;
                # spaCy bleibt dann nur für Tokenisierung, Wortarten und Kontextwörter.
                for name in definition["remove_recognizers"]:
                    registry.remove_recognizer(name)
                self._analyzers[engine_id] = AnalyzerEngine(
                    nlp_engine=nlp_engine, registry=registry, supported_languages=langs
                )
            return self._analyzers[engine_id]

    def languages(self, engine_id):
        return self._defs[engine_id]["languages"]

    def entities(self, engine_id, language):
        analyzer = self.get(engine_id)
        langs = self.languages(engine_id) if language == "auto" else [language]
        return sorted({e for lang in langs for e in analyzer.get_supported_entities(language=lang)})


def _ad_hoc_recognizers(custom, language):
    """Eigene Regex- und Deny-List-Erkenner aus der Oberfläche, nur für diese Anfrage."""
    found = []
    for i, item in enumerate(custom or []):
        entity = (item.get("entity") or "").strip().upper()
        if not entity:
            continue
        if item.get("kind") == "regex" and item.get("pattern"):
            pattern = Pattern(f"{entity}_{i}", item["pattern"], float(item.get("score", 0.8)))
            found.append(
                PatternRecognizer(supported_entity=entity, patterns=[pattern],
                                  supported_language=language, name=f"adhoc_regex_{i}")
            )
        elif item.get("kind") == "deny_list":
            words = [w.strip() for w in item.get("words", []) if w.strip()]
            if words:
                found.append(
                    PatternRecognizer(supported_entity=entity, deny_list=words,
                                      supported_language=language, name=f"adhoc_deny_{i}")
                )
    return found


def _operator_config(spec, allocator):
    name = spec.get("operator", "replace")
    if name not in OPERATORS:
        raise ValueError(f"Unbekannter Operator: {name}")
    params = {}
    for key, kind in OPERATORS[name].items():
        value = spec.get("params", {}).get(key)
        if value in (None, ""):
            continue
        if kind == "int":
            value = int(value)
        elif kind == "bool":
            value = bool(value)
        params[key] = value
    if name == "placeholder":
        params["allocator"] = allocator
    return OperatorConfig(name, params)


def _analyze(analyzer, text, language, req):
    custom = _ad_hoc_recognizers(req.get("custom_recognizers"), language)
    entities = req.get("entities") or None
    if entities:
        # Nur Entitäten anfragen, die es in dieser Sprache gibt – sonst bricht Presidio
        # bei Blöcken ab, für die keine der gewählten Entitäten einen Erkenner hat.
        available = set(analyzer.get_supported_entities(language=language))
        available.update(r.supported_entities[0] for r in custom)
        entities = [e for e in entities if e in available]
        if not entities:
            return []
    allow_list = [w.strip() for w in req.get("allow_list", []) if w.strip()]
    return analyzer.analyze(
        text=text,
        language=language,
        entities=entities,
        score_threshold=float(req.get("score_threshold", 0.35)),
        allow_list=allow_list or None,
        allow_list_match=req.get("allow_list_match", "exact"),
        context=[w.strip() for w in req.get("context", []) if w.strip()] or None,
        ad_hoc_recognizers=custom or None,
        return_decision_process=True,
    )


_DETERMINERS = {
    "der", "die", "das", "dem", "den", "des", "ein", "eine", "einen", "einem", "einer", "eines",
    "kein", "keine", "mein", "meine", "dein", "deine", "sein", "seine", "ihr", "ihre", "unser",
    "unsere", "euer", "eure", "jeder", "jede", "dieser", "diese", "jener",
    "the", "a", "an", "my", "your", "his", "her", "our", "their", "this", "that", "every",
}
_TITLES = {"Herr", "Herrn", "Frau", "Hr.", "Fr.", "Dr.", "Prof.", "Mr.", "Mrs.", "Ms.", "Sir", "Dame"}
_NER_RECOGNIZERS = {"GLiNERRecognizer", "SpacyRecognizer"}


def _keep_only_names(analyzer, text, results, segments):
    """NER-Treffer verwerfen, die keine Eigennamen sind.

    GLiNER beantwortet „ist das eine Person?“ – auch für „der Geschäftsführer“, „er“, „die
    Firma“ (nominale/pronominale Erwähnungen). Die Konfidenz trennt das nicht. Die Wortart
    tut es: Namen taggt spaCy als PROPN, Rollen als NOUN, Pronomen als PRON.
      PERSON: bleibt, wenn ein Token PROPN ist oder Anrede/Titel vor einem großgeschriebenen
              Wort steht („Herrn Dr. Schmidt“).
      ORGANIZATION/LOCATION: nur einzelne Wörter ohne PROPN fallen weg („Firma“, „Behörden“);
              Mehrwortnamen wie „Deutsche Bank“ bestehen aus Gattungswörtern und bleiben.
    Musterbasierte Erkenner sind nicht betroffen.
    """
    docs = {}

    def tokens(start, end):
        seg = next((sg for sg in segments if sg[0] <= start < sg[1]), (0, len(text), None))
        if seg not in docs:
            docs[seg] = (seg[0], analyzer.nlp_engine.process_text(text[seg[0]:seg[1]], seg[2]).tokens)
        offset, doc = docs[seg]
        return [t for t in doc if t.idx + offset < end and t.idx + offset + len(t) > start]

    kept, dropped = [], []
    for r in results:
        recognizer = r.analysis_explanation.recognizer if r.analysis_explanation else None
        if recognizer not in _NER_RECOGNIZERS or r.entity_type not in ("PERSON", "ORGANIZATION", "LOCATION"):
            kept.append(r)
            continue
        toks = tokens(r.start, r.end)
        has_propn = any(t.pos_ == "PROPN" for t in toks)
        if r.entity_type == "PERSON":
            titled = any(t.text in _TITLES and i + 1 < len(toks) and toks[i + 1].text[:1].isupper()
                         for i, t in enumerate(toks))
            # Mehrere großgeschriebene Wörter ohne Artikel/Pronomen gelten als Name: seltene
            # Namen („Mihalache Ianis“) taggt das kleine spaCy-Modell als NOUN. Rollen sind fast
            # immer ein Wort, ggf. mit Artikel („der Geschäftsführer“) – die fallen weiter weg.
            capitalized = [t for t in toks if t.text[:1].isupper() and t.pos_ not in ("DET", "PRON")]
            # Das kleine Modell taggt auch Vornamen als NOUN („Kinder (Lena, 9 …)“). Ein einzelnes
            # großgeschriebenes Wort bleibt Name, wenn GLiNER sicher ist und kein Artikel oder
            # Possessivpronomen davorsteht – Rollen stehen fast immer mit („der Geschäftsführer“).
            previous = re.search(r"(\w+)\W*$", text[max(0, r.start - 20):r.start])
            confident_single = (len(capitalized) == 1 and r.score >= 0.7 and
                                not (previous and previous.group(1).lower() in _DETERMINERS))
            is_name = has_propn or titled or len(capitalized) >= 2 or confident_single
        else:
            words = [t for t in toks if not t.is_punct]
            is_name = has_propn or len(words) > 1
        (kept if is_name else dropped).append(r)
    return kept, dropped
PROPAGATION_SCORE = 0.6

# Vor einer Folgenennung schließen diese Wörter einen Personennamen aus: „im Sommer“,
# „nach Berlin“, „aus Paris“ – derselbe Name kann als Wort oder Ort vorkommen.
_NOT_BEFORE_NAME = {
    "im", "am", "in", "an", "auf", "nach", "aus", "von", "vom", "zum", "zur", "bei", "beim", "bis",
    "der", "die", "das", "dem", "den", "des", "ein", "eine", "einen", "einem", "einer", "diesen",
    "the", "a", "an", "in", "to", "from", "at", "of", "this", "next", "last",
}

# Musterbasierte Treffer, die nur ein Teil einer längeren Kennung sind: „2026-10-23“ in der
# Artikelnummer „2026-10-23-001“, „1.2.3.4“ in „Version 1.2.3.4“.
_EMBEDDED_TYPES = {"DATE_TIME", "IP_ADDRESS"}


def _drop_embedded(text, results):
    kept = []
    for r in results:
        recognizer = r.analysis_explanation.recognizer if r.analysis_explanation else None
        if r.entity_type in _EMBEDDED_TYPES and recognizer not in _NER_RECOGNIZERS:
            after, before = text[r.end:r.end + 2], text[max(0, r.start - 2):r.start]
            if re.match(r"[\w]|[-./]\d", after) or re.search(r"\w$|\d[-./]$", before):
                continue
            if r.entity_type == "IP_ADDRESS" and re.search(r"(?:Version|Ver\.|v|Firmware|Software|Release|Build)\s*$",
                                                        text[max(0, r.start - 12):r.start], re.I):
                continue
        kept.append(r)
    return kept


def _propagate_person_names(text, results, threshold):
    """Weitere Nennungen erkannter Personen: voller Name ohne Anrede und Nachname.

    Die NER erkennt Folgenennungen oft nicht („Isaac Newton … Herrn Newton“: GLiNER findet
    das zweite nicht) – dann wäre der volle Name ersetzt, der Nachname drei Sätze später aber
    im Klartext. Vornamen allein werden nicht übertragen (zu häufig, zu wenig identifizierend).
    """
    if PROPAGATION_SCORE < threshold:
        return []
    names = {}
    for r in results:
        if r.entity_type != "PERSON":
            continue
        tokens = text[r.start:r.end].split()
        while tokens and tokens[0] in _TITLES:
            tokens.pop(0)
        if not tokens:
            continue
        full = " ".join(tokens)
        names.setdefault(full, full)
        surname = tokens[-1]
        if len(tokens) >= 2 and len(surname) >= 3 and surname[0].isupper():
            names.setdefault(surname, full)
    covered = [(r.start, r.end) for r in results]
    # Namen, die anderswo im Text als Ort oder Organisation erkannt sind („Herrn Berlin“ und
    # „nach Berlin“), werden nicht übertragen – welche Nennung was ist, entscheidet die NER.
    places = {text[r.start:r.end] for r in results if r.entity_type in ("LOCATION", "ORGANIZATION", "ADDRESS")}
    added = []
    for name, source in names.items():
        single = " " not in name
        if single and name in places:
            continue
        pattern = r"\s+".join(re.escape(p) for p in name.split())
        for m in re.finditer(rf"(?<![\w’'-]){pattern}(?:’s|'s|s)?(?![\w’'-])", text):
            if any(s <= m.start() < e or s < m.end() <= e for s, e in covered):
                continue
            previous = re.search(r"(\w+)\W*$", text[max(0, m.start() - 20):m.start()])
            if single and previous and previous.group(1).lower() in _NOT_BEFORE_NAME:
                continue
            why = f"weitere Nennung von „{source}“"
            added.append(RecognizerResult(
                "PERSON", m.start(), m.end(), PROPAGATION_SCORE,
                analysis_explanation=AnalysisExplanation(
                    recognizer="NamePropagation", original_score=PROPAGATION_SCORE,
                    pattern_name=why, textual_explanation=why)))
            covered.append((m.start(), m.end()))
    return added


def anonymize(engines, req, store=None, rules=(), profile=None):
    """req: Dict aus der Oberfläche, siehe static/app.js (buildRequest).

    language == "auto": Sprache blockweise bestimmen und jeden Bereich mit dem
    Modell seiner Sprache analysieren (siehe language_id.detect_blocks).
    Operator "placeholder": umkehrbare Platzhalter <TYP:ID>; mit req["persist"] wird die
    Zuordnung in store gespeichert (erst beim Übernehmen, nicht in der Live-Vorschau).
    rules: Ersetzungsliste (replacement_rules), angewendet wenn req["apply_rules"].
    profile: gewähltes Profil (profiles); gibt Gazetteer-Namen unter Bedingungen frei.
    """
    engine_id = req["engine"]
    language = req["language"]
    text = req.get("text", "")
    analyzer = engines.get(engine_id)

    blocks = []
    if language == "auto":
        supported = engines.languages(engine_id)
        fallback = req.get("fallback_language") or supported[0]
        blocks, segments = language_id.detect_blocks(
            text, supported, fallback, granularity=req.get("granularity", "line")
        )
        results = []
        for start, end, lang in segments:
            for r in _analyze(analyzer, text[start:end], lang, req):
                r.start += start
                r.end += start
                results.append(r)
    else:
        results = _analyze(analyzer, text, language, req)

    # spaCy zieht Entitäten gelegentlich über Zeilenumbrüche („Petra Wagner\nTel“);
    # dort abschneiden. Musterbasierte Erkenner (z. B. umbrochene Firmennamen) bleiben ganz.
    for r in results:
        if r.analysis_explanation and r.analysis_explanation.recognizer != "SpacyRecognizer":
            continue
        newline = text.find("\n", r.start, r.end)
        if newline > r.start:
            r.end = len(text[r.start:newline].rstrip()) + r.start

    # Deny-List: diese Begriffe immer erkennen, unabhängig von Modell und Filtern.
    for entity_type, s_, e_, entry in deny_list.find(text, req.get("deny_list") or []):
        why = f"Deny-List: „{entry}“"
        results.append(RecognizerResult(entity_type, s_, e_, 1.0, analysis_explanation=AnalysisExplanation(
            recognizer="DenyList", original_score=1.0, pattern_name=why, textual_explanation=why)))

    if req.get("table_names", True):
        cells, header_cells = deny_list.find_table_cells(text)
        # Überschriftszellen („Stadt“, „Name“) sind keine Fundstellen, auch wenn die NER sie markiert
        results = [r for r in results if (r.start, r.end) not in header_cells]
        for s_, e_, kind in cells:
            why = f"Tabellenspalte mit {'Namens' if kind == 'PERSON' else 'Orts'}überschrift"
            results.append(RecognizerResult(kind, s_, e_, 0.9, analysis_explanation=AnalysisExplanation(
                recognizer="TableColumn", original_score=0.9, pattern_name=why, textual_explanation=why)))

    results = _drop_embedded(text, results)

    name_filtered = []
    if req.get("names_only", True):
        segs = segments if language == "auto" else [(0, len(text), language)]
        results, name_filtered = _keep_only_names(analyzer, text, results, segs)

    if req.get("propagate_names", True):
        results += _propagate_person_names(text, results, float(req.get("score_threshold", 0.35)))

    # Ersetzungsliste hat Vorrang: Presidio-Fundstellen, die einen Treffer berühren, entfallen
    # – sonst würde aus „HUGOleaner“ eine ORGANIZATION statt „<BOBO:4711>leaner“.
    rule_matches = replacement_rules.find_forward(text, rules) if req.get("apply_rules", True) else []
    if rule_matches:
        results = [r for r in results
                   if all(r.end <= s or r.start >= e for s, e, _ in rule_matches)]
    rule_results = [RecognizerResult(f"RULE_{rule['id']}", s, e, 1.0) for s, e, rule in rule_matches]

    released, release_blocked = [], []
    if profile:
        results, released, release_blocked = profiles.apply(text, results, profile,
                                                            recognizers.gliner_model)

    key = placeholders.session_key(req.get("session_key"))
    allocator = placeholders.PlaceholderAllocator(key)
    operators = {"DEFAULT": _operator_config(req.get("default_operator", {}), allocator)}
    for entity, spec in (req.get("entity_operators") or {}).items():
        operators[entity] = _operator_config(spec, allocator)
    used_rules = list({rule["id"]: rule for _, _, rule in rule_matches}.values())
    for rule in used_rules:
        operators[f"RULE_{rule['id']}"] = OperatorConfig(
            "replace", {"new_value": replacement_rules.replacement(rule)})

    # Presidio verändert die Ergebnisse beim Zusammenführen; jeder Lauf bekommt eine Kopie.
    if any(op.operator_name == "placeholder" for op in operators.values()):
        anonymizer.anonymize(text=text, analyzer_results=copy.deepcopy(results + rule_results),
                             operators=operators)
        allocator.start_assign()
    anonymized = anonymizer.anonymize(text=text, analyzer_results=copy.deepcopy(results + rule_results),
                                      operators=operators)

    persisted = False
    if req.get("persist") and allocator.entries:
        if store is None:
            raise ValueError("Keine Zuordnungstabelle konfiguriert")
        store.save(allocator.entries, document=hashlib.sha256(key).hexdigest()[:12])
        persisted = True
    unique = any(not same for _, _, same in allocator.calls)

    findings = []
    for r in sorted(results, key=lambda r: r.start):
        explanation = r.analysis_explanation
        findings.append({
            "entity_type": r.entity_type,
            "start": r.start,
            "end": r.end,
            "text": text[r.start:r.end],
            "score": round(r.score, 3),
            "recognizer": explanation.recognizer if explanation else None,
            "pattern": explanation.pattern_name if explanation else None,
            "context_boost": bool(explanation and explanation.score_context_improvement),
        })
    for s, e, rule in rule_matches:
        findings.append({"entity_type": "ERSETZUNGSLISTE", "start": s, "end": e, "text": text[s:e],
                         "score": 1.0, "recognizer": "Ersetzungsliste",
                         "pattern": replacement_rules.replacement(rule), "context_boost": False})
    findings.sort(key=lambda f: f["start"])

    return {
        "text": anonymized.text,
        "findings": findings,
        "language_blocks": blocks,
        "placeholders": len(allocator.entries),
        "persisted": persisted,
        "rules_applied": len(rule_matches),
        "released": released,
        "not_names": [{"entity_type": r.entity_type, "text": text[r.start:r.end],
                       "score": round(r.score, 2)} for r in name_filtered],
        "release_blocked": release_blocked,
        "prompt": {lang: placeholders.context_prompt(allocator.entries, unique, lang, used_rules)
                   for lang in ("de", "en")},
        "items": [
            {"start": i.start, "end": i.end,
             "entity_type": "ERSETZUNGSLISTE" if i.entity_type.startswith("RULE_") else i.entity_type,
             "operator": i.operator, "text": i.text}
            for i in sorted(anonymized.items, key=lambda i: i.start)
        ],
    }


def decrypt(req):
    """Mit 'encrypt' verschlüsselte Stellen wieder in Klartext umwandeln.

    req["keys"]: {"DEFAULT": key, ENTITY: key, ...} – je Entität kann ein anderer
    Schlüssel verwendet worden sein.
    """
    items = [
        OperatorResult(start=i["start"], end=i["end"], entity_type=i["entity_type"])
        for i in req["items"] if i.get("operator") == "encrypt"
    ]
    operators = {
        entity: OperatorConfig("decrypt", {"key": key})
        for entity, key in req["keys"].items() if key
    }
    result = deanonymizer.deanonymize(text=req["text"], entities=items, operators=operators)
    return {"text": result.text}
