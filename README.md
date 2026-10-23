# KI-Pseudonymisierer

*Deutsch · [English](README.en.md)*

Lokale Web-Anwendung, die Texte vor einer KI-Anfrage **pseudonymisiert**: Namen, Firmen,
Anschriften, Kennnummern usw. werden durch Platzhalter wie `<PERSON:3f9a2c71b04e>` ersetzt,
die Antwort der KI wird anschließend automatisch zurückübersetzt. Alles läuft auf dem eigenen
Rechner; an die KI geht nur der pseudonymisierte Text.

Pseudonymisierung, nicht Anonymisierung (Art. 4 Nr. 5 DSGVO): Die Zuordnung Platzhalter →
Original wird verschlüsselt lokal gespeichert, damit die Antwort zurückübersetzt werden kann.

## Was drin ist

- **Erkennung:** [Microsoft Presidio](https://github.com/microsoft/presidio) mit spaCy-Modellen
  (de/en) und [GLiNER](https://github.com/urchade/GLiNER) (Transformer) für Personen,
  Organisationen und Orte; Mustererkenner für Kennnummern vieler Länder, Werte hinter
  Feldbeschriftungen („Steuer-ID: …“), Firmen über Rechtsformen, Anschriften, Gesundheits-
  und Religionsangaben (Art. 9 DSGVO).
- **Sprache abschnittsweise** per fastText (`lid.176`), gemischtsprachige Texte werden je
  Abschnitt mit dem passenden Modell geprüft.
- **Datei-Upload:** .txt, .md, .pdf, .docx (.doc/.odt/.rtf über LibreOffice).
- **Eigene Listen:** Namen, die immer erkannt werden; Ersetzungsliste für Produkt- und
  Firmenbegriffe (auch mitten im Wort, mit Übersetzungen); Profile für Fachkontexte.
- **KI-Anfrage direkt aus der Anwendung** an Claude Code oder pi (`agents.json`), ohne Werkzeuge,
  wahlweise ohne Sitzung.
- **Gateway** (`gateway.py`): Durchleitung und Protokoll der Claude-Code-Kommunikation zur
  nachträglichen Prüfung (`gateway_analyze.py`).

## Einrichtung

```bash
./setup.sh                 # Python-Umgebung, Pakete, spaCy-, GLiNER- und fastText-Modell (~3 GB)
./setup.sh --nur-spacy     # ohne GLiNER/torch
./setup.sh --mit-stimmen   # zusätzlich Piper-Stimmen für doc/vertonen.py
```

Voraussetzung: Python ≥ 3.10, Linux oder macOS. Optional: LibreOffice (.doc-Upload),
`claude` oder `pi` (KI-Anfrage), ffmpeg (Vertonung).

## Start

```bash
.venv/bin/python app.py                          # http://127.0.0.1:5000
.venv/bin/gunicorn -c gunicorn.conf.py app:app   # Dauerbetrieb
```

Laufzeitdaten (Zuordnungstabelle samt Schlüssel, Listen, Gateway-Protokoll) liegen in `data/`
und sind von Git ausgeschlossen – sie enthalten echte personenbezogene Daten.

## Benchmark

```bash
.venv/bin/python -m unittest tests.test_benchmark -v
```

`tests/benchmark/` enthält eine fiktive Kundenakte mit Sollliste (was ersetzt sein muss, was
stehen bleiben muss, bekannte Lücken) und einen Geschäftstext ohne Personendaten als
Fehltreffer-Probe. Keine Erkennung ist vollständig – vor dem Senden die erkannten Angaben
im Reiter „Einstellungen & Details“ prüfen.

## Lizenz

MIT, siehe [LICENSE](LICENSE).

---
<sub>README erstellt 2026-10-23 auf Auftrag („Soll ich eine README.md anlegen?“ – „Ja, kurz“).</sub>
