"""Benchmark: Was bleibt nach dem Pseudonymisieren im Klartext stehen?

Aufruf:  .venv/bin/python -m unittest tests.test_benchmark -v
         BENCH_ENGINE=presidio-spacy .venv/bin/python -m unittest tests.test_benchmark

Je Text in tests/benchmark/ eine *.expected.json (Format siehe dort). Geprüft wird das
Ergebnis, nicht die Fundstellenliste: Ein Wert gilt als geschützt, wenn er im Ausgabetext nicht
mehr vorkommt – egal unter welchem Typ. Das ist die Frage, die für die KI-Anfrage zählt.
Der Test lädt die Modelle (GLiNER, spaCy) und braucht daher etwa eine Minute.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BENCH = Path(__file__).resolve().parent / "benchmark"
sys.path.insert(0, str(BENCH.parent.parent))

ENGINE = os.environ.get("BENCH_ENGINE", "presidio-gliner")
# Einstellungen wie in der Oberfläche voreingestellt
REQUEST = {"engine": ENGINE, "language": "auto", "score_threshold": 0.35, "names_only": True,
           "propagate_names": True, "table_names": True, "default_operator": {"type": "placeholder"}}


class Benchmark(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Eigenes Datenverzeichnis: der Test darf keine Zuordnungen oder Listen des Nutzers lesen
        cls._data = tempfile.TemporaryDirectory()
        os.environ["PII_DATA_DIR"] = cls._data.name
        import pii_service
        cls.pii_service = pii_service
        cls.engines = pii_service.EngineRegistry()

    @classmethod
    def tearDownClass(cls):
        cls._data.cleanup()

    def _check(self, expected_file):
        spec = json.loads((BENCH / expected_file).read_text(encoding="utf-8"))
        text = (BENCH / spec["text"]).read_text(encoding="utf-8")
        output = self.pii_service.anonymize(self.engines, dict(REQUEST, text=text))["text"]

        leaked = [s for s in spec["must_mask"] if s in output]
        over = [s for s in spec["must_keep"] if s not in output]
        gaps = spec.get("known_gaps", {})
        still_leaking = [s for s in gaps.get("must_mask", {}) if s in output]
        fixed = [s for s in gaps.get("must_mask", {}) if s not in output]
        fixed += [s for s in gaps.get("must_keep", {}) if s in output]

        total = len(spec["must_mask"]) + len(gaps.get("must_mask", {}))
        protected = total - len(leaked) - len(still_leaking)
        print(f"\n[{ENGINE}] {spec['text']}: {protected}/{total} geschützt"
              + (f" ({protected / total:.0%})" if total else "")
              + f", bekannte Lücken: {len(still_leaking)}"
              + (f", jetzt behoben (in die Sollliste übernehmen): {fixed}" if fixed else ""))

        self.assertEqual(leaked, [], f"im Klartext geblieben ({spec['text']})")
        self.assertEqual(over, [], f"fälschlich ersetzt ({spec['text']})")

    def test_kundenakte(self):
        self._check("kundenakte.expected.json")

    def test_geschaeftstext(self):
        self._check("geschaeftstext.expected.json")


if __name__ == "__main__":
    unittest.main()
