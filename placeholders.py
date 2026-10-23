"""Umkehrbare Platzhalter <TYP:ID> (Pseudonymisierung), Wiederherstellung und KI-Kontext-Prompt.

Die ID ist kein Hash des Werts, sondern HMAC(Sitzungsschlüssel, Typ, Wert, n-tes Vorkommen):
ohne Schlüssel und Zuordnungstabelle verrät sie nichts, jede Fundstelle bekommt eine eigene ID
(auch bei derselben Person), und ein erneuter Lauf über denselben Text in derselben Sitzung
liefert dieselben IDs – so erzeugt die Live-Vorschau nicht bei jedem Tastendruck neue.
"""

import hashlib
import hmac
import re
import secrets

from presidio_anonymizer.operators import Operator, OperatorType

import language_id
import replacement_rules

ID_LENGTH = 12  # 48 Bit: Kollisionen erst bei Millionen gespeicherter IDs denkbar


class PlaceholderAllocator:
    """Vergibt IDs in Dokumentreihenfolge.

    Presidio ruft Operatoren vom Textende zum Anfang auf. Damit die n-te Nennung eines Werts
    unabhängig von später angehängtem Text dieselbe ID behält, läuft die Anonymisierung zweimal:
    erst zeichnet der Allocator die Aufrufe auf (record), dann vergibt er die vorab in
    Dokumentreihenfolge berechneten IDs in Aufrufreihenfolge (assign).
    """

    def __init__(self, session_key: bytes):
        self.key = session_key
        self.mode = "record"
        self.calls = []      # (entity_type, value, same_id_for_same_value), Aufrufreihenfolge
        self.ids = []
        self.entries = []    # (id, entity_type, value), Dokumentreihenfolge

    def __call__(self, entity_type, value, same_id):
        if self.mode == "record":
            self.calls.append((entity_type, value, same_id))
            return value
        return f"<{entity_type}:{self.ids.pop()}>"

    def start_assign(self):
        counts = {}
        for entity_type, value, same_id in reversed(self.calls):
            if same_id:
                occurrence = "*"
            else:
                occurrence = counts[(entity_type, value)] = counts.get((entity_type, value), -1) + 1
            message = f"{entity_type}\x1f{value}\x1f{occurrence}".encode("utf-8")
            pid = hmac.new(self.key, message, hashlib.sha256).hexdigest()[:ID_LENGTH]
            self.entries.append((pid, entity_type, value))
        # pop() vom Ende: Aufrufreihenfolge ist umgekehrte Dokumentreihenfolge
        self.ids = [pid for pid, _, _ in self.entries]
        self.mode = "assign"


class PlaceholderOperator(Operator):
    """Presidio-Operator „placeholder“: ersetzt durch <TYP:ID> über einen PlaceholderAllocator."""

    def operate(self, text=None, params=None):
        return params["allocator"](params["entity_type"], text,
                                   bool(params.get("same_id_for_same_value")))

    def validate(self, params=None):
        if not isinstance(params.get("allocator"), PlaceholderAllocator):
            raise ValueError("placeholder: allocator fehlt")

    def operator_name(self):
        return "placeholder"

    def operator_type(self):
        return OperatorType.Anonymize


def session_key(hex_key=None):
    """Schlüssel aus der Oberfläche (je Browser-Sitzung) oder zufällig."""
    if hex_key and re.fullmatch(r"[0-9a-f]{32,128}", hex_key):
        return bytes.fromhex(hex_key)
    return secrets.token_bytes(32)


# --- Wiederherstellung --------------------------------------------------------------------

# Tolerant gegenüber üblichen Veränderungen durch KI-Ausgaben: HTML- oder Markdown-Escaping
# (&lt;…&gt;, \<…\>), andere Klammern, Groß-/Kleinschreibung, Leerraum, fehlender Typ.
# Ersetzt wird nur, was als ID in der Zuordnungstabelle steht.
_RESTORE_RE = re.compile(
    r"(?P<open>\\?<|&lt;|\[|\{|«)?[ \t]*"
    r"(?:(?P<type>[A-Za-z][A-Za-z_]{1,39})[ \t]*:[ \t]*)?"
    rf"(?<![0-9A-Fa-f])(?P<id>[0-9A-Fa-f]{{{ID_LENGTH}}})(?![0-9A-Fa-f])"
    r"[ \t]*(?P<close>\\?>|&gt;|\]|\}|»)?"
)
_PAIRS = {"<": ">", "\\<": "\\>", "&lt;": "&gt;", "[": "]", "{": "}", "«": "»"}


def _find_placeholders(text, store):
    """Presidio-Platzhalter: (Kandidaten, unbekannte, Typ geändert)."""
    matches = list(_RESTORE_RE.finditer(text))
    known = store.lookup([m.group("id").lower() for m in matches])
    candidates, unknown, type_changed = [], [], []
    for m in matches:
        pid = m.group("id").lower()
        bracketed = m.group("open") and _PAIRS.get(m.group("open")) == m.group("close")
        if pid not in known:
            if bracketed and m.group("type"):
                unknown.append(m.group().strip())
            continue
        entity_type, value = known[pid]
        # Klammern nur als Paar mit ersetzen – sonst bliebe z. B. von „[siehe PERSON:…]“
        # die öffnende Klammer stehen und die schließende verschwände.
        start = m.start() if bracketed else m.start("type") if m.group("type") else m.start("id")
        end = m.end() if bracketed else m.end("id")
        candidates.append({"start": start, "end": end, "value": value,
                           "entity_type": entity_type, "id": pid})
        if m.group("type") and m.group("type").upper() != entity_type:
            type_changed.append({"id": pid, "found": m.group("type"), "stored": entity_type})
    return candidates, unknown, type_changed


_RULE_TAG_RE = re.compile(r"(?:\\?<|&lt;)[ \t]*[A-Za-z][A-Za-z0-9_]{0,39}[ \t]*:[ \t]*\d{3,8}[ \t]*(?:\\?>|&gt;)")


def restore(text, store, rules=(), language="auto"):
    """Platzhalter und Ersetzungsliste in einer KI-Antwort auflösen.

    language: "auto" = Sprache blockweise erkennen und Ersatz der Ersetzungsliste in der
    Sprache seines Blocks einsetzen (hinterlegte Übersetzung); sonst ISO-Kürzel für alles.
    """
    candidates, unknown, type_changed = _find_placeholders(text, store)
    rule_matches = replacement_rules.find_restore(text, rules)

    # Tags der Form <NAME:Nummer>, die zu keiner Regel passen: verändert oder erfunden.
    rule_spans = {(s, e) for s, e, _ in rule_matches}
    unknown += [m.group() for m in _RULE_TAG_RE.finditer(text) if (m.start(), m.end()) not in rule_spans]

    languages, missing = {}, {}
    if rule_matches:
        if language == "auto":
            # Platzhalter ausblenden (gleiche Länge, Positionen bleiben gültig): „PERSON“,
            # „ADDRESS“ usw. würden kurze deutsche Zeilen sonst nach Englisch aussehen lassen.
            clean = list(text)
            for c in candidates:
                clean[c["start"]:c["end"]] = " " * (c["end"] - c["start"])
            for s, e, _ in rule_matches:
                clean[s:e] = " " * (e - s)
            blocks, _ = language_id.detect_blocks("".join(clean), None, None)
        for s, e, rule in rule_matches:
            if language == "auto":
                block = next((b for b in blocks if b["start"] <= s < b["end"]), None)
                lang = block["language"] if block else rule["language"]
            else:
                lang = language
            term, found = replacement_rules.term_for(rule, lang)
            languages[lang] = languages.get(lang, 0) + 1
            if not found:
                missing.setdefault(lang, set()).add(rule["search"])
            candidates.append({"start": s, "end": e, "value": term, "language": lang,
                               "entity_type": "ERSETZUNGSLISTE",
                               "id": replacement_rules.replacement(rule)})

    # Überlappungen (z. B. Klartext-Ersatz innerhalb eines Platzhalters): der längere gewinnt.
    chosen = []
    for c in sorted(candidates, key=lambda c: (c["start"] - c["end"], c["start"])):
        if all(c["end"] <= o["start"] or c["start"] >= o["end"] for o in chosen):
            chosen.append(c)
    chosen.sort(key=lambda c: c["start"])

    out, pos, offset, replaced = [], 0, 0, []
    for c in chosen:
        out.append(text[pos:c["start"]])
        out.append(c["value"])
        new_start = c["start"] + offset
        offset += len(c["value"]) - (c["end"] - c["start"])
        pos = c["end"]
        replaced.append({k: v for k, v in c.items() if k not in ("start", "end", "value")}
                        | {"start": new_start, "end": new_start + len(c["value"])})
    out.append(text[pos:])
    return {"text": "".join(out), "replaced": replaced, "unknown": unknown,
            "type_changed": type_changed, "languages": languages,
            "missing_translations": {lang: sorted(terms) for lang, terms in missing.items()}}


# --- Kontext-Prompt -----------------------------------------------------------------------

ENTITY_NAMES = {
    "de": {
        "PERSON": "Person", "ORGANIZATION": "Organisation", "ADDRESS": "Anschrift",
        "LOCATION": "Ort", "PHONE_NUMBER": "Telefonnummer", "EMAIL_ADDRESS": "E-Mail-Adresse",
        "DATE_TIME": "Datum/Uhrzeit", "IBAN_CODE": "IBAN", "CREDIT_CARD": "Kreditkartennummer",
        "URL": "URL", "IP_ADDRESS": "IP-Adresse", "DE_PLZ": "Postleitzahl",
        "DE_HEALTH_INSURANCE": "Krankenversichertennummer", "DE_LANR": "Arztnummer (LANR)",
        "DE_BSNR": "Betriebsstättennummer (BSNR)", "DE_TAX_ID": "Steuer-ID",
        "DE_TAX_NUMBER": "Steuernummer", "DE_VAT_ID": "USt-IdNr.", "DE_ID_CARD": "Ausweisnummer",
        "DE_PASSPORT": "Reisepassnummer", "DE_SOCIAL_SECURITY": "Sozialversicherungsnummer",
        "DE_KFZ": "Kfz-Kennzeichen", "NRP": "Nationalität/Religion/politische Gruppe",
        "ID_NUMBER": "Kennnummer", "CREDENTIAL": "Zugangsdaten", "USERNAME": "Benutzername",
        "HEALTH": "Gesundheitsangabe", "MAC_ADDRESS": "MAC-Adresse",
    },
    "en": {
        "PERSON": "person", "ORGANIZATION": "organization", "ADDRESS": "postal address",
        "LOCATION": "location", "PHONE_NUMBER": "phone number", "EMAIL_ADDRESS": "e-mail address",
        "DATE_TIME": "date/time", "IBAN_CODE": "IBAN", "CREDIT_CARD": "credit card number",
        "URL": "URL", "IP_ADDRESS": "IP address", "DE_PLZ": "postal code",
        "NRP": "nationality/religion/political group",
        "ID_NUMBER": "identification number", "CREDENTIAL": "credentials", "USERNAME": "user name",
        "HEALTH": "health information", "MAC_ADDRESS": "MAC address",
    },
}

_PROMPTS = {
    "de": """Hinweis zum folgenden Text: Er wurde pseudonymisiert. Personenbezogene und vertrauliche Angaben sind durch Platzhalter der Form <TYP:ID> ersetzt, zum Beispiel {example}. Die Platzhalter werden nach deiner Antwort automatisch durch die echten Angaben ersetzt.

Regeln für deine Antwort:
1. Wenn du dich auf eine ersetzte Angabe beziehst, übernimm ihren Platzhalter exakt Zeichen für Zeichen: spitze Klammern, Typ in Großbuchstaben, Doppelpunkt, ID. Nicht übersetzen, nicht kürzen, nicht formatieren (kein Fett, kein Code, kein Escaping).
2. {identity}
3. Erfinde keine neuen Platzhalter und versuche nicht, die echten Angaben zu erraten oder zu ergänzen.
4. Behandle Platzhalter grammatisch wie Eigennamen. Leite daraus kein Geschlecht, keine Herkunft und keine Anrede ab.
5. Diese Regeln gelten auch für erzeugte Dateien, Tabellen, Code und Zusammenfassungen.
6. Wenn du den Text übersetzt, bleiben die Platzhalter unübersetzt stehen. Sie werden danach automatisch in der Zielsprache eingesetzt.

Vorkommende Typen: {types}.{plain}""",
    "en": """Note on the following text: it has been pseudonymized. Personal and confidential data has been replaced with placeholders of the form <TYPE:ID>, for example {example}. The placeholders will be replaced with the real data automatically after your answer.

Rules for your answer:
1. When you refer to replaced data, copy its placeholder exactly, character by character: angle brackets, type in capitals, colon, ID. Do not translate, shorten or format it (no bold, no code, no escaping).
2. {identity}
3. Do not invent new placeholders and do not try to guess or complete the real data.
4. Treat placeholders grammatically like proper names. Do not infer gender, origin or form of address from them.
5. These rules also apply to generated files, tables, code and summaries.
6. If you translate the text, leave the placeholders untranslated. They will be inserted in the target language automatically afterwards.

Types present: {types}.{plain}""",
}

_IDENTITY = {
    "de": {
        True: "Jeder Platzhalter steht für eine einzelne Nennung. Verschiedene Platzhalter können "
              "dieselbe Person oder Sache bezeichnen – führe sie nicht zusammen und schließe nicht "
              "aus unterschiedlichen IDs auf unterschiedliche Personen.",
        False: "Gleiche Angaben haben denselben Platzhalter. Führe Platzhalter trotzdem nicht "
               "zusammen und verändere ihre Zuordnung nicht.",
    },
    "en": {
        True: "Each placeholder stands for a single mention. Different placeholders may refer to "
              "the same person or thing – do not merge them and do not conclude from different "
              "IDs that they are different people.",
        False: "Identical data shares the same placeholder. Still, do not merge placeholders or "
               "change what they refer to.",
    },
}


_RULE_TERM = {"de": "Begriff aus der Ersetzungsliste", "en": "term from the replacement list"}
_PLAIN = {
    "de": "\n\nAußerdem sind diese Wörter Ersatzbezeichnungen und genauso unverändert zu "
          "übernehmen (nicht übersetzen, nicht beugen, nicht umschreiben): {terms}.",
    "en": "\n\nThe following words are substitute names as well and must be kept unchanged "
          "(do not translate, inflect or paraphrase them): {terms}.",
}


def context_prompt(entries, unique_per_mention, language="de", rules=()):
    """Prompt, der einer KI erklärt, wie sie mit den Platzhaltern umgehen soll.

    entries: Presidio-Platzhalter (id, Typ, Wert); rules: angewendete Regeln der
    Ersetzungsliste (Tags erscheinen als Typen, Klartext-Ersatz als eigene Liste).
    """
    tag_rules = [r for r in rules if r["format"] == "tag"]
    plain_rules = [r for r in rules if r["format"] == "plain"]
    if not entries and not rules:
        return ""
    names = ENTITY_NAMES.get(language, ENTITY_NAMES["en"])
    types = [f"{t} ({names.get(t, ENTITY_NAMES['en'].get(t, t))})"
             for t in sorted({e[1] for e in entries})]
    types += [f"{name} ({_RULE_TERM[language]})" for name in sorted({r["name"] for r in tag_rules})]
    if entries:
        example = f"<{entries[0][1]}:{entries[0][0]}>"
    elif tag_rules:
        example = replacement_rules.replacement(tag_rules[0])
    else:
        example = "<PERSON:3f9a2c71b04e>"
    quote = "„{}“" if language == "de" else "\"{}\""
    plain = (_PLAIN[language].format(terms=", ".join(quote.format(r["text"]) for r in plain_rules))
             if plain_rules else "")
    return _PROMPTS[language].format(
        example=example,
        identity=_IDENTITY[language][unique_per_mention],
        types=", ".join(types) or "–",
        plain=plain,
    )
