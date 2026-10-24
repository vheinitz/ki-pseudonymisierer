# AI Pseudonymizer

*[Deutsch](README.md) · English*

A local web application that **pseudonymizes** text before it is sent to an AI: names,
companies, addresses, ID numbers and the like are replaced by placeholders such as
`<PERSON:3f9a2c71b04e>`, and the AI's answer is translated back automatically. Everything runs
on your own machine; only the pseudonymized text is sent to the AI.

Pseudonymization, not anonymization (Art. 4(5) GDPR): the mapping placeholder → original is
stored locally and encrypted so that the answer can be translated back.

The user interface is in German.

## Features

- **Detection:** [Microsoft Presidio](https://github.com/microsoft/presidio); for persons,
  organizations, locations and addresses either [GLiNER](https://github.com/urchade/GLiNER)
  (`gliner_multi_pii-v1`), the spaCy models `de/en_core_*_lg` or a custom spaCy model in
  `models/custom-ner` (annotation and conversion tools in `training/`; `engines.json`).
  Plus pattern recognizers for ID numbers of many countries, values
  after field labels ("Tax ID: …"), companies by legal form (GmbH, AG, Ltd …), postal
  addresses, health and religion data (special categories, Art. 9 GDPR).
- **Language detection per section** with fastText (`lid.176`); mixed-language texts are
  checked section by section with the matching model.
- **File upload:** .pdf, .docx, text files (.txt, .md, .csv, .json, .html …); .doc/.odt/.rtf
  via LibreOffice.
- **Custom lists:** names that are always detected; a replacement list for product and company
  terms (also inside words, with translations); profiles with presets and a list of known
  persons whose names may stay (e.g. deceased people).
- **AI request from within the app** to Claude Code or pi (`agents.json`), without tools,
  optionally without a session.
- **Gateway** (`gateway.py`, port 8787, `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude`):
  passes Claude Code traffic through unchanged and logs it; `gateway_analyze.py` checks the
  log afterwards for private data.

## Setup

```bash
./setup.sh                 # Python environment, packages, spaCy, GLiNER and fastText models (~3 GB)
./setup.sh --nur-spacy     # without GLiNER/torch
./setup.sh --mit-stimmen   # additionally Piper voices for doc/vertonen.py
```

Requirements: Python ≥ 3.10, bash and curl for `setup.sh`. Optional: LibreOffice (.doc upload),
`claude` or `pi` (AI request), ffmpeg (voice-over).

## Run

```bash
.venv/bin/python app.py                          # http://127.0.0.1:5000
.venv/bin/gunicorn -c gunicorn.conf.py app:app   # long-running
```

Runtime data (mapping table including its key, lists, gateway log) is stored in `data/` and
excluded from Git, because it contains real personal data.

## Benchmark

```bash
.venv/bin/python -m unittest tests.test_benchmark -v
```

`tests/benchmark/` contains a fictitious customer file with an expectation list (what must be
replaced, what must stay, known gaps) and a business text without personal data as a
false-positive check. No detection is complete: review the detected items in the
"Einstellungen & Details" tab before sending.

## License

MIT, see [LICENSE](LICENSE).

---
<sub>English README added 2026-10-23 on request ("ja, englische README bitte ergänzen").</sub>

<sub>Revised 2026-10-24 after checking against the code (request: „das Kürzen der READMEs mit KI-Politur … ok fahre fort“).</sub>
