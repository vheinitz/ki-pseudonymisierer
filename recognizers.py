"""Eigene Presidio-Erkenner, die engines.json per Klassenname zuschalten kann."""

import re

from presidio_analyzer import AnalysisExplanation, EntityRecognizer, RecognizerResult

# Rechtsformzusätze (DE/AT/CH, EN, FR/BE/LU, NL, IT, ES, Nordics). Längere Varianten zuerst,
# damit „GmbH & Co. KG“ nicht schon bei „GmbH“ endet.
LEGAL_FORMS = [
    r"GmbH\s*&\s*Co\.\s*KGaA", r"GmbH\s*&\s*Co\.\s*KG", r"AG\s*&\s*Co\.\s*KG", r"SE\s*&\s*Co\.\s*KG",
    r"UG\s*\(haftungsbeschränkt\)", r"PartG\s*mbB", r"gGmbH", r"GmbH", r"mbH", r"gAG", r"KGaA",
    r"AG", r"KG", r"OHG", r"oHG", r"GbR", r"UG", r"PartG", r"eG", r"SE",
    r"e\.\s?K\.", r"e\.\s?Kfm\.", r"e\.\s?Kfr\.", r"e\.\s?V\.",
    r"Inc\.?", r"Incorporated", r"Ltd\.?", r"Limited", r"LLC", r"L\.L\.C\.", r"LLP", r"PLC", r"plc",
    r"Corp\.?", r"Corporation", r"Co\.,?\s*Ltd\.?", r"Pty\.?\s*Ltd\.?",
    r"S\.A\.S\.?", r"SAS", r"SARL", r"S\.à\s?r\.l\.", r"Sàrl", r"S\.A\.", r"SA",
    r"B\.V\.", r"BV", r"N\.V\.", r"NV",
    r"S\.p\.A\.", r"SpA", r"S\.r\.l\.", r"Srl", r"S\.L\.", r"AB", r"ASA", r"A/S", r"Oy",
]

# Bestandteil eines Namens: groß geschrieben oder Ziffer am Anfang (O’Reilly, 3M, Müller-Bau, A&B),
# aber mit mindestens einem Buchstaben – „2022 O’Reilly Media“ soll bei O’Reilly beginnen.
_TOKEN = r"(?=[\w’'&.\-]*[^\W\d_])[A-ZÄÖÜ0-9][\w’'&.\-]*"
# Kleingeschriebene Verbindungswörter innerhalb eines Namens (Bank of America, Müller und Söhne).
_JOIN = r"(?:of|and|for|the|und|für|von|van|der|de|du|la|&)"
# Zwischen Namensteilen Leerzeichen oder ein einzelner Zeilenumbruch (umbrochener Fließtext).
_SEP = r"(?:[ \t]+|[ \t]*\n[ \t]*)"
# Genügsam (lazy): der Name endet am ersten Rechtsformzusatz – sonst verschluckt er
# „GmbH & Co.“ oder verbindet „Acme Ltd. und Bank of America Corp.“ zu einem Namen.
_NAME = rf"{_TOKEN}(?:{_SEP}(?:{_JOIN}{_SEP})?{_TOKEN}){{0,5}}?"
_PATTERN = re.compile(
    rf"(?P<name>{_NAME})(?:,?\s+)(?P<form>{'|'.join(LEGAL_FORMS)})(?![\w])"
)

# Merkmale, die ein Wort eindeutig als Namen ausweisen: Apostroph, Ziffer, Großbuchstabe im
# Wortinneren oder durchgehend groß (mind. 2 Zeichen).
_NAME_LIKE = re.compile(r"[’'0-9]|.[A-ZÄÖÜ]|^[A-ZÄÖÜ]{2,}$")

# Satzanfänge, die nicht zum Namen gehören („Die Müller GmbH“ -> „Müller GmbH“).
_LEADING_STOPWORDS = {
    "Der", "Die", "Das", "Den", "Dem", "Des", "Ein", "Eine", "Einer", "Eines", "Einem", "Einen",
    "The", "A", "An", "By", "From", "At", "To", "Bei", "Von", "Mit", "Für", "Durch", "An",
    "Le", "La", "Les", "De", "Het", "Il",
}


class LegalFormRecognizer(EntityRecognizer):
    """ORGANIZATION über Rechtsformzusätze, plus Folgeerwähnungen desselben Namens ohne Zusatz.

    „O’Reilly Media, Inc.“ wird als Ganzes erkannt; später im Text stehendes „O’Reilly Media“
    ebenfalls (etwas niedrigerer Score), damit derselbe Name nicht teils stehen bleibt.
    Die Kurzform aus dem ersten Namensteil („O’Reilly books“) nur, wenn sie eindeutig nach
    Namen aussieht oder spaCy sie als Eigennamen (PROPN) taggt – „Deutsche“ aus
    „Deutsche Bank AG“ bleibt so unberührt.
    """

    ENTITY = "ORGANIZATION"
    SCORE = 0.85
    MENTION_SCORE = 0.7
    SHORT_FORM_SCORE = 0.6

    def __init__(self, supported_language="en"):
        super().__init__(supported_entities=[self.ENTITY], supported_language=supported_language,
                         name="LegalFormRecognizer")

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        if entities and self.ENTITY not in entities:
            return []
        results, names = [], set()
        for match in _PATTERN.finditer(text):
            start = match.start("name")
            tokens = match.group("name").split()
            # Führende Artikel/Präpositionen abwerfen, aber nie den letzten Namensbestandteil.
            while len(tokens) > 1 and tokens[0] in _LEADING_STOPWORDS:
                start = text.index(tokens[1], start + len(tokens[0]))
                tokens = tokens[1:]
            if tokens[0] in _LEADING_STOPWORDS:
                continue
            results.append(self._result(start, match.end(), self.SCORE,
                                        f"Rechtsformzusatz '{match.group('form')}'"))
            names.add(text[start:match.start("form")].rstrip(" ,\t\n"))

        for name in names:
            # Leerraum im Namen flexibel: Folgeerwähnungen können umbrochen sein.
            pattern = r"\s+".join(re.escape(part) for part in name.split())
            for m in re.finditer(rf"(?<![\w’']){pattern}(?![\w’'])", text):
                if not any(r.start <= m.start() < r.end for r in results):
                    results.append(self._result(m.start(), m.end(), self.MENTION_SCORE,
                                                f"Folgeerwähnung von '{name}'"))

        doc = getattr(nlp_artifacts, "tokens", None)
        for name in names:
            parts = name.split()
            if len(parts) < 2:
                continue
            # Eindeutige Namensform (O’Reilly, McKinsey, 3M, SAP) gilt ohne weitere Prüfung –
            # spaCy taggt z. B. „O’Reilly“ wegen des typografischen Apostrophs als ADJ/ADV.
            name_like = bool(_NAME_LIKE.search(parts[0]))
            for m in re.finditer(rf"(?<![\w’']){re.escape(parts[0])}(?![\w’'])", text):
                if any(r.start <= m.start() < r.end for r in results):
                    continue
                span = doc.char_span(m.start(), m.end()) if doc is not None else None
                if name_like or (span is not None and all(t.pos_ == "PROPN" for t in span)):
                    results.append(self._result(m.start(), m.end(), self.SHORT_FORM_SCORE,
                                                f"Kurzform von '{name}'"))
        return results

    def _result(self, start, end, score, why):
        explanation = AnalysisExplanation(recognizer=self.name, original_score=score,
                                          textual_explanation=why, pattern_name=why)
        return RecognizerResult(self.ENTITY, start, end, score, analysis_explanation=explanation,
                                recognition_metadata={RecognizerResult.RECOGNIZER_NAME_KEY: self.name,
                                                      RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: self.id})


# --- Straßenadressen ---------------------------------------------------------------------

# Straßenbezeichnung als eigenes oder angekoppeltes Wort („Charlotte-Posenenske-Straße“,
# „Charlotte Posenenske Str“). Eindeutige Bezeichnungen gelten auch ohne Hausnummer;
# mehrdeutige (auch Nachnamen: „Paul Weg“, „Anna Ring“) nur durchgekoppelt oder mit Nummer.
_STREET_UNAMBIGUOUS = r"Stra(?:ß|ss)e|Str\.?|Allee|Platz|Pl\.|Gasse|Chaussee|Promenade"
_STREET_AMBIGUOUS = (r"Weg|Ring|Damm|Ufer|Pfad|Steig|Stieg|Markt|Kai|Graben|Wall|Twiete|Bogen|"
                     r"Anger|Zeile|Brücke")
_STREET_WORDS = rf"{_STREET_UNAMBIGUOUS}|{_STREET_AMBIGUOUS}"
# Straßenbezeichnung als Wortende („Hauptstraße“, „Lindenweg“) – mehrdeutig („Rückweg“),
# deshalb nur zusammen mit Hausnummer.
_STREET_SUFFIX = (r"stra(?:ß|ss)e|str\.|weg|allee|platz|gasse|ring|damm|ufer|chaussee|pfad|"
                  r"steig|stieg|markt|graben|wall|twiete")
# Hausnummer: 5, 12a, 12 a, 7–9, 3/1 – ein Buchstabe nur, wenn danach kein Wort weitergeht
# („Am Markt 3 oder“ -> „3“).
_HOUSE_LETTER = r"(?:\s?[a-zA-Z](?!\w))?"
_HOUSE_NO = rf"\d{{1,4}}{_HOUSE_LETTER}(?:\s?[-–/]\s?\d{{1,4}}{_HOUSE_LETTER})?(?!\d)"
_CAP = r"[A-ZÄÖÜ][\wäöüß.]*"
# Verbindungswörter nur durchgekoppelt („Walther-von-der-Vogelweide-Straße“) – mit Leerzeichen
# würden sie Satzteile ankoppeln („Lieferung an Konrad-Adenauer-Allee“).
_HYPHEN_JOIN = r"(?:-(?:von|van|der|de|zu|zum|zur|am|an|im|vom))*"

_LOCATIVE = "Lageangabe mit Hausnummer"  # Präposition gehört dazu: „An der Alster 12“

_ADDRESS_PATTERNS = [
    # Charlotte-Posenenske-Straße 5 / Konrad-Adenauer-Allee / Walther-von-der-Vogelweide-Weg
    (re.compile(rf"(?<![\w-])(?:{_CAP}{_HYPHEN_JOIN}-){{1,4}}(?:{_STREET_WORDS})(?![\w])"
                rf"(?:\s*{_HOUSE_NO})?"), 0.9, "Straßenname, durchgekoppelt"),
    # Charlotte Posenenske Str / Konrad Adenauer Allee 12
    (re.compile(rf"(?<![\w-])(?:{_CAP} ){{1,4}}(?:{_STREET_UNAMBIGUOUS})(?![\w])"
                rf"(?:\s*{_HOUSE_NO})?"), 0.9, "Straßenname mit Straßenbezeichnung"),
    # Am Weg 3 / Paul Ring 12 – mehrdeutige Bezeichnung, daher nur mit Hausnummer
    (re.compile(rf"(?<![\w-])(?:{_CAP} ){{1,4}}(?:{_STREET_AMBIGUOUS})\s*{_HOUSE_NO}"),
     0.9, "Straßenname mit Hausnummer"),
# Hauptstraße 5 / Lindenweg 12-14
    (re.compile(rf"(?<![\w-])[A-ZÄÖÜ][\wäöüß-]*(?:{_STREET_SUFFIX})\s*{_HOUSE_NO}"),
     0.9, "Straße mit Hausnummer"),
    # Am Markt 5 / An der Alster 12 / Auf dem Hohen Berg 3
    (re.compile(rf"(?<![\w])(?:Am|An|Auf|Im|In|Zum|Zur|Unter|Hinter|Vor|Bei)\s+(?:der|dem|den|die|das)?\s*"
                rf"{_CAP}(?:\s+{_CAP}){{0,2}}\s+{_HOUSE_NO}"), 0.9, _LOCATIVE),
    # 1005 Gravenstein Highway North / 221B Baker Street
    (re.compile(r"(?<![\w])\d{1,5}[A-Za-z]?\s+(?:[A-Z][\w.'’-]*\s+){1,4}"
                r"(?:Street|St\.|Avenue|Ave\.|Road|Rd\.|Boulevard|Blvd\.|Lane|Ln\.|Drive|Dr\.|"
                r"Highway|Hwy\.?|Way|Court|Ct\.|Place|Square|Terrace|Parkway|Pkwy\.?)"
                r"(?:\s+(?:North|South|East|West|N|S|E|W|NE|NW|SE|SW)\b\.?)?"), 0.9, "US-Anschrift"),
]


# PLZ + Ort („65197 Wiesbaden“, „D-60311 Frankfurt am Main“, „CH-8001 Zürich“) als eine
# Fundstelle: PLZ ohne Ort zu ersetzen (oder umgekehrt) lässt den Ort weiter erschließen.
# Ohne Länderpräfix nur fünfstellig; vierstellig (AT/CH) nur mit Präfix – sonst Jahreszahlen.
# Ortsnamenteile ohne Punkt – sonst läuft der Ort über das Satzende hinaus
# („65197 Wiesbaden. Erika Mustermann“).
_CITY_TOKEN = r"[A-ZÄÖÜ][\wäöüß]*"
_PLACE_JOIN = r"(?:am|an\s+der|an\s+dem|im|in|ob\s+der|bei|vor\s+der|i\.\s?|a\.\s?d\.\s?)"
_PLZ_CITY = re.compile(
    rf"(?<![\w-])(?:(?:D|DE)-\d{{5}}|(?:A|AT|CH|L|LU)-\d{{4}}|\d{{5}})(?!\d)[ \t]+"
    rf"(?P<city>{_CITY_TOKEN}(?:(?:[ \t]+{_PLACE_JOIN}[ \t]*|[ \t]+|-){_CITY_TOKEN}){{0,2}}"
    rf"(?:[ \t]+\([A-ZÄÖÜ][\wäöüß.]*\))?)"
)
_LOCATION_LABELS = {"LOCATION", "LOC", "GPE"}


class AddressRecognizer(EntityRecognizer):
    """Straßenadressen (ADDRESS) – Presidio hat dafür keinen Erkenner.

    Wichtig vor allem für Straßen mit Personennamen: spaCy meldet in „Charlotte-Posenenske-
    Straße“ eine PERSON. Der Score liegt über dem von spaCy (0,85); die Adresse umschließt die
    PERSON, und Presidio verwirft beim Anonymisieren eingeschlossene Fundstellen.
    """

    ENTITY = "ADDRESS"
    WITH_NUMBER_BONUS = 0.05
    PLZ_CITY_SCORE = 0.95       # mit Länderkennung oder Ortsteil von spaCy als Ort erkannt
    PLZ_CITY_WEAK_SCORE = 0.3   # sonst unter der Standardschwelle („50000 Mitarbeiter“)

    def __init__(self, supported_language="de"):
        super().__init__(supported_entities=[self.ENTITY], supported_language=supported_language,
                         name="AddressRecognizer")

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        if entities and self.ENTITY not in entities:
            return []
        results = []
        for pattern, score, why in _ADDRESS_PATTERNS:
            for m in pattern.finditer(text):
                start = m.start()
                # Führende Satzwörter abwerfen („Die Charlotte-Posenenske-Straße“).
                first = m.group().split(maxsplit=1)[0]
                if first in _LEADING_STOPWORDS and len(m.group().split()) > 1 and why != _LOCATIVE:
                    start = m.start() + m.group().index(first) + len(first) + 1
                end = m.end()
                while end > start and text[end - 1] in " \t\n,":
                    end -= 1
                has_number = bool(re.search(r"\d", text[start:end]))
                results.append(self._result(
                    start, end, min(score + self.WITH_NUMBER_BONUS * has_number, 1.0), why))

        locations = [(e.start_char, e.end_char) for e in (getattr(nlp_artifacts, "entities", None) or [])
                     if e.label_ in _LOCATION_LABELS]
        for m in _PLZ_CITY.finditer(text):
            end = m.end()
            while text[end - 1] in ".,":
                end -= 1
            city_start = m.start("city")
            if "-" in text[m.start():city_start]:
                # Länderkennung (D-, CH-, …) ist für sich eindeutig.
                results.append(self._result(m.start(), end, self.PLZ_CITY_SCORE,
                                            "PLZ mit Länderkennung + Ort"))
            elif any(s <= city_start < e for s, e in locations):
                # Bestätigter Ort: dort enden, wo die NER den Ort enden lässt – das Muster
                # allein würde folgende großgeschriebene Wörter (Namen) mitnehmen.
                loc_end = max(e for s, e in locations if s <= city_start < e)
                results.append(self._result(m.start(), min(end, loc_end), self.PLZ_CITY_SCORE,
                                            "PLZ + Ort (Ort von NER bestätigt)"))
            else:
                results.append(self._result(m.start(), end, self.PLZ_CITY_WEAK_SCORE,
                                            "PLZ + großgeschriebenes Wort (unbestätigt)"))
        return results

    def _result(self, start, end, score, why):
        explanation = AnalysisExplanation(recognizer=self.name, original_score=score,
                                          pattern_name=why, textual_explanation=why)
        return RecognizerResult(self.ENTITY, start, end, score, analysis_explanation=explanation,
                                recognition_metadata={RecognizerResult.RECOGNIZER_NAME_KEY: self.name,
                                                      RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: self.id})


# --- GLiNER (Transformer-NER) --------------------------------------------------------------

_GLINER_MODELS = {}


def _shared_gliner_class():
    """Erst bei Bedarf importieren: torch/gliner sind groß und nur für die GLiNER-Engine nötig."""
    from presidio_analyzer.predefined_recognizers import GLiNERRecognizer

    class SharedGLiNERRecognizer(GLiNERRecognizer):
        """GLiNERRecognizer, der das Modell zwischen Sprachinstanzen teilt.

        Presidio braucht je Sprache eine Erkenner-Instanz; jede würde sonst das Modell
        (~1,2 GB) erneut laden. Außerdem fragt er nur die konfigurierten Labels ab.
        """

        def _GLiNERRecognizer__create_input_labels(self, entities):  # noqa: N802
            # Presidio hängt sonst alle angefragten Entitätsnamen (CRYPTO, US_SSN, DE_PLZ …)
            # als Zero-Shot-Labels an; GLiNER deutet „CRYPTO“ als Währung und markiert
            # Eurobeträge. Muster-Entitäten bleiben den Regex-Erkennern überlassen.
            return list(self.gliner_labels)

        def load(self):
            key = (self.model_name, self.map_location)
            if key not in _GLINER_MODELS:
                super().load()
                _GLINER_MODELS[key] = self.gliner
            self.gliner = _GLINER_MODELS[key]

    return SharedGLiNERRecognizer


def SharedGLiNERRecognizer(**kwargs):  # noqa: N802 – wird in engines.json wie eine Klasse benannt
    return _shared_gliner_class()(**kwargs)


# --- E-Mail ohne Pflicht zur öffentlichen Domain-Endung -------------------------------------

def _email_base():
    from presidio_analyzer.predefined_recognizers import EmailRecognizer
    return EmailRecognizer


class LenientEmailRecognizer(_email_base()):
    """EmailRecognizer, der Adressen mit nicht öffentlicher Endung nicht verwirft.

    Presidio prüft die Domain gegen die Public Suffix List und verwirft z. B.
    „sabine.wagner@firma.example“ (reserviert, RFC 2606) oder interne Domains wie
    „@firma.local“ / „@corp.intern“ komplett. Hier: öffentliche Endung -> sicher (1,0),
    sonst bleibt der Grundscore des Musters (0,5) – über der Standardschwelle, aber
    erkennbar unsicherer.
    """

    def validate_result(self, pattern_text):
        return True if super().validate_result(pattern_text) else None


def gliner_model(model_name="urchade/gliner_multi_pii-v1"):
    """Das (geteilte) GLiNER-Modell direkt, z. B. für die Kontextbestätigung der Profile.

    Nutzt das Modell der GLiNER-Engine, falls schon geladen; sonst wird es einmal geladen.
    """
    for (name, _), model in _GLINER_MODELS.items():
        if name == model_name:
            return model
    from gliner import GLiNER
    model = GLiNER.from_pretrained(model_name, map_location="cpu")
    _GLINER_MODELS[(model_name, "cpu")] = model
    return model


# --- Beschriftete Felder: „Steuer-ID: 86 095 742 719“, „Passwort (!): …“ ---------------------

# Die Beschriftung verrät, was folgt – zuverlässiger als Format und Prüfziffer. Die
# Presidio-Erkenner verwerfen z. B. eine Steuer-ID mit Leerzeichen, einen Ausweis mit falscher
# Prüfziffer oder eine Kennnummer aus einem Land, dessen Sprache der Textblock nicht hat.
# Für eine Pseudonymisierung zählt aber nur: Steht hier eine Kennung einer Person?
# Gruppe: (Entität, Score, Beschriftungen, Wert-Art). Wert-Arten:
#   id     Kennung aus Ziffern/Großbuchstaben, ggf. mit Leerzeichen gruppiert, mind. 3 Ziffern
#   phone  Telefonnummer in beliebiger Schreibweise
#   token  ein Wort ohne Leerzeichen (Passwort, Benutzername)
#   line   Rest der Zeile (Staatsangehörigkeit, Diagnose …)
_LABEL_GROUPS = [
    ("CREDIT_CARD", 0.8, ["Kreditkarte(?:nnummer)?", "Kartennummer", "Credit ?card(?: number)?",
                          "Card number", "Visa", "Mastercard", "Amex", "American Express"], "id"),
    ("IBAN_CODE", 0.8, ["IBAN"], "id"),
    ("ID_NUMBER", 0.8, [
        # Deutschland
        "Steuer-?ID", "Steuer-?Identifikationsnummer", "Identifikationsnummer", "Steuernummer",
        "Sozialversicherungs-?nummer", "SV-Nummer", "Rentenversicherungs-?nummer",
        "Personalausweis(?:nummer)?", "Ausweis(?:nummer)?", "Reisepass(?:nummer)?", "Passnummer",
        "Führerschein(?:nummer)?", "Krankenversicherten-?nummer", "Versicherten-?nummer",
        "Versicherungs-?nummer", "Personalnummer", "Mitarbeiter-?nummer", "Kunden-?nummer",
        "Mitglieds-?nummer", "Vertrags-?nummer", "Patienten-?nummer", "Konto-?nummer",
        "Kfz-Kennzeichen", "Kennzeichen", "Fahrzeug", "LANR", "BSNR", "USt-?IdNr\\.?",
        # englisch / international
        "Social Security(?: Number)?", "SSN", "ITIN", "TIN", "National Insurance(?: number)?", "NINO",
        "NHS(?: number)?", "Passport(?: number)?", "Passaporto", "Pasaporte", "Paszport",
        "Driver'?s Licen[cs]e(?: number)?", "Driving Licen[cs]e(?: number)?", "Licen[cs]e number",
        "Medical Licen[cs]e", "Bank account(?: number)?", "Account number", "Routing number",
        "Sort code", "Voter ID", "ID(?: number| card| no\\.?)?", "Carta d'identità",
        "Codice Fiscale", "Partita IVA", "NIF", "NIE", "DNI", "PESEL", "NIP", "Henkilötunnus",
        "NRIC", "FIN", "UEN", "ABN", "ACN", "TFN", "Medicare(?: number)?", "PAN", "Aadhaar",
        "RRN", "TNIN", "Personnummer", "CPF", "SIN",
    ], "id"),
    ("PHONE_NUMBER", 0.85, ["Telefon(?:nummer)?", "Tel\\.?", "Telefax", "Fax", "Mobil(?:nummer)?",
                            "Handy(?:nummer)?", "Festnetz(?:nummer)?", "Phone(?: number)?", "Mobile",
                            "Cell(?: phone)?", "Fon"], "phone"),
    ("CREDENTIAL", 1.0, ["Passwort", "Kennwort", "Password", "Passphrase", "PIN", "CVC2?", "CVV2?",
                         "TAN", "Secret", "API[- ]?Key", "Token", "Zugangscode"], "token"),
    ("USERNAME", 1.0, ["Benutzername", "Benutzerkennung", "Anmeldename", "Username", "User ?name",
                       "Login", "User-?ID"], "token"),
    ("NRP", 1.0, ["Staatsangehörigkeit(?:en)?", "Nationalität", "Nationality", "Citizenship",
                  "Religion", "Konfession", "Religionszugehörigkeit", "Glaube",
                  "Ethnie", "Ethnicity", "Parteizugehörigkeit", "Party membership"], "line"),
    ("HEALTH", 1.0, ["Diagnosen?", "Diagnosis", "Befund", "Erkrankung(?:en)?", "Vorerkrankung(?:en)?",
                     "Medikation", "Medikamente", "Medication", "Allergien?", "Behinderung",
                     "Grad der Behinderung", "GdB"], "line"),
]
# Ziffern- und Buchstabengruppen einer Kennung; Leerzeichen nur zwischen Gruppen, die Ziffern
# enthalten oder kurze Großbuchstabenblöcke sind („12 140387 W 012“, „JG 10 37 59 A“) – so
# endet „Personalnummer 004711 bitte prüfen“ vor „bitte“.
_ID_GROUP = r"(?:[A-Za-z0-9./\-]*\d[A-Za-z0-9./\-]*|[A-Z]{1,3}(?![A-Za-z]))"  # Ziffernblock zuerst
_VALUES = {
    "id": rf"(?P<value>{_ID_GROUP}(?: {_ID_GROUP}){{0,7}})",
    "phone": r"(?P<value>\+?[\d(][\d ()/.\-]{4,}\d)",
    "token": r"(?P<value>[^\s,;]{2,})",
    "line": r"(?P<value>[^\n]*\S)",
}


class LabeledValueRecognizer(EntityRecognizer):
    """Werte hinter Feldbeschriftungen (sprachunabhängig, für jede Sprache registrierbar).

    Mit Doppelpunkt („Label: Wert“, auch „Label (Zusatz): Wert“) gilt jeder passende Wert;
    ohne Doppelpunkt („Personalnummer 004711“, „CVC 123“) nur Werte mit Ziffern.
    """

    def __init__(self, supported_language="de", **kwargs):
        self._patterns = []
        for entity, score, labels, kind in _LABEL_GROUPS:
            label = rf"(?<![\w-])(?:{'|'.join(labels)})(?![\w-])"
            with_colon = rf"{label}[ \t]*(?:\([^)\n]{{0,40}}\)[ \t]*)?:[ \t]*{_VALUES[kind]}"
            self._patterns.append((entity, score, re.compile(with_colon, re.IGNORECASE)))
            if kind in ("id", "phone", "token"):
                bare = rf"{label}[ \t]+(?=[^\s]*\d){_VALUES[kind]}"
                self._patterns.append((entity, score, re.compile(bare, re.IGNORECASE)))
        super().__init__(supported_entities=sorted({g[0] for g in _LABEL_GROUPS}),
                         supported_language=supported_language, name="LabeledValueRecognizer", **kwargs)

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        results, seen = [], set()
        for entity, score, pattern in self._patterns:
            if entity not in entities:
                continue
            for m in pattern.finditer(text):
                start, end = m.span("value")
                value = m["value"]
                if entity in ("ID_NUMBER", "CREDIT_CARD", "IBAN_CODE") and sum(c.isdigit() for c in value) < 3:
                    continue
                if (start, end) in seen:
                    continue
                seen.add((start, end))
                why = f"Feldbeschriftung „{m.group(0)[:m.start('value') - m.start()].strip(' :')}“"
                results.append(RecognizerResult(entity, start, end, score, analysis_explanation=AnalysisExplanation(
                    recognizer=self.name, original_score=score, pattern_name=why, textual_explanation=why)))
        return results


# --- Besondere Kategorien (Art. 9 DSGVO) im Fließtext ------------------------------------------

# Bewusst kurze Wortlisten: häufige Diagnosen, Religionen, Herkunftsangaben. Eine vollständige
# medizinische Erkennung bräuchte ein Fachmodell; das hier fängt die typischen Fälle in Akten
# und Gesprächsnotizen ab. Beschriftete Felder („Diagnose: …“) deckt LabeledValueRecognizer ab.
_HEALTH_TERMS = [
    r"Diabetes(?: mellitus)?(?: Typ \d| Type \d)?", r"Krebs", r"\w*[Kk]arzinom", r"\w*[Tt]umor",
    r"Leukämie", r"Depressionen?", r"Burn-?out", r"Angststörung", r"Schizophrenie",
    r"bipolare Störung", r"ADHS", r"ADHD", r"Autismus", r"Demenz", r"Alzheimer", r"Parkinson",
    r"Multiple Sklerose", r"Epilepsie", r"Asthma", r"COPD", r"HIV", r"AIDS",
    r"Hepatitis [A-E]", r"Long[- ]Covid", r"Bluthochdruck", r"Hypertonie", r"Herzinfarkt",
    r"Schlaganfall", r"Herzinsuffizienz", r"Rheuma", r"Morbus \w+", r"Schwangerschaft",
    r"cancer", r"depression", r"dementia", r"epilepsy", r"stroke", r"heart attack",
    r"hypertension", r"multiple sclerosis", r"schizophrenia", r"pregnan(?:t|cy)",
]
_RELIGION_TERMS = [
    r"römisch-katholisch", r"katholisch", r"evangelisch(?:-lutherisch)?", r"protestantisch",
    r"orthodox", r"muslimisch", r"islamisch", r"jüdisch", r"buddhistisch", r"hinduistisch",
    r"konfessionslos", r"atheistisch", r"Roman Catholic", r"Catholic", r"Protestant",
    r"Muslim", r"Jewish", r"Buddhist", r"Hindu", r"Sikh",
]
_SENSITIVE = [
    ("HEALTH", 0.7, re.compile(rf"(?<![\w-])(?:{'|'.join(_HEALTH_TERMS)})(?![\w-])")),
    # Medikament mit Dosis: „Metformin 1000 mg“, „Ibuprofen 400mg“
    ("HEALTH", 0.7, re.compile(r"(?<![\w-])[A-ZÄÖÜ][a-zäöüß]{3,}[ \t]+\d+(?:[.,]\d+)?[ \t]?(?:mg|µg|mcg|ml|IE|I\.E\.)(?![\w])")),
    ("NRP", 0.7, re.compile(rf"(?<![\w-])(?:{'|'.join(_RELIGION_TERMS)})(?![\w])")),
    # Herkunftsangaben „Irish-American“, „Afro-Deutsche“
    ("NRP", 0.7, re.compile(r"(?<![\w-])[A-Z][a-z]+-(?:American|Americans|Amerikaner(?:in)?|Deutsche[rn]?)(?![\w])")),
]


class SensitiveTermsRecognizer(EntityRecognizer):
    """Gesundheitsangaben (HEALTH) und Religion/Herkunft (NRP) per Wortliste."""

    def __init__(self, supported_language="de", **kwargs):
        super().__init__(supported_entities=["HEALTH", "NRP"], supported_language=supported_language,
                         name="SensitiveTermsRecognizer", **kwargs)

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        results = []
        for entity, score, pattern in _SENSITIVE:
            if entity not in entities:
                continue
            for m in pattern.finditer(text):
                why = "Wortliste besondere Kategorien (Art. 9 DSGVO)"
                results.append(RecognizerResult(entity, m.start(), m.end(), score, analysis_explanation=AnalysisExplanation(
                    recognizer=self.name, original_score=score, pattern_name=why, textual_explanation=why)))
        return results


# --- Daten in Worten, Uhrzeiten, verschleierte E-Mail-Adressen -------------------------------

def _pattern_base():
    from presidio_analyzer import Pattern, PatternRecognizer
    return Pattern, PatternRecognizer


_Pattern, _PatternRecognizer = _pattern_base()
_MONTHS_DE = r"(?:Januar|Jänner|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember|Jan|Feb|Mär|Apr|Jun|Jul|Aug|Sept?|Okt|Nov|Dez)\.?"
_MONTHS_EN = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?"
_WEEKDAYS = r"(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"


class WrittenDateRecognizer(_PatternRecognizer):
    """Datumsangaben mit Monatsnamen und Uhrzeiten, die Presidios DateRecognizer nicht kennt."""

    def __init__(self, supported_language="de", **kwargs):
        patterns = [
            _Pattern("Datum DE", rf"(?<!\w)(?:{_WEEKDAYS},?\s+(?:den\s+)?)?\d{{1,2}}\.\s?{_MONTHS_DE}(?:\s+\d{{4}})?(?!\w)", 0.6),
            _Pattern("Datum EN", rf"(?<!\w)(?:{_WEEKDAYS},?\s+)?{_MONTHS_EN}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}(?!\w)", 0.6),
            _Pattern("Datum EN 2", rf"(?<!\w)\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS_EN}\s+\d{{4}}(?!\w)", 0.6),
            _Pattern("Uhrzeit", r"(?<![\w:.])\d{1,2}(?:[:.]\d{2})?\s?Uhr(?!\w)", 0.5),
            _Pattern("Uhrzeit EN", r"(?<![\w:.])\d{1,2}(?::\d{2})?\s?(?:a\.m\.|p\.m\.|am|pm)(?!\w)", 0.5),
            # „12.11.“ ohne Jahr; nicht in Versions- oder Gliederungsnummern („1.2.3“)
            _Pattern("Datum ohne Jahr", r"(?<![\w.])(?:0?[1-9]|[12]\d|3[01])\.(?:0?[1-9]|1[0-2])\.(?![\w.])", 0.5),
        ]
        super().__init__(supported_entity="DATE_TIME", patterns=patterns, supported_language=supported_language,
                         name="WrittenDateRecognizer", global_regex_flags=re.DOTALL | re.MULTILINE, **kwargs)


class ObfuscatedEmailRecognizer(_PatternRecognizer):
    """„anna (at) example (dot) com“, „anna[at]example.com“, „anna ät example punkt de“."""

    def __init__(self, supported_language="de", **kwargs):
        at = r"\s*(?:\(\s*(?:at|ät|@)\s*\)|\[\s*(?:at|ät|@)\s*\]|\{\s*(?:at|ät|@)\s*\}|\s(?:at|ät)\s)\s*"
        dot = r"(?:\s*(?:\(\s*(?:dot|punkt)\s*\)|\[\s*(?:dot|punkt)\s*\]|\s(?:dot|punkt)\s)\s*|\.)"
        patterns = [_Pattern("verschleierte E-Mail", rf"(?<![\w.])[\w.+-]+{at}[\w-]+(?:{dot}[\w-]+)+", 0.8)]
        super().__init__(supported_entity="EMAIL_ADDRESS", patterns=patterns, supported_language=supported_language,
                         name="ObfuscatedEmailRecognizer", global_regex_flags=re.IGNORECASE, **kwargs)


def StrictDeKfzRecognizer(supported_language="de", **kwargs):  # noqa: N802
    """Kfz-Kennzeichen nur in Großbuchstaben.

    Presidio sucht ohne Beachtung der Groß-/Kleinschreibung und markierte so „ist am 12“
    (aus „Die nächste Kontrolle ist am 12.11.“) als Kennzeichen.
    """
    from presidio_analyzer.predefined_recognizers import DeKfzRecognizer
    recognizer = DeKfzRecognizer(supported_language=supported_language, **kwargs)
    recognizer.global_regex_flags = re.DOTALL | re.MULTILINE
    recognizer.name = "StrictDeKfzRecognizer"
    return recognizer
