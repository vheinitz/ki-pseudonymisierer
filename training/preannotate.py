"""Texte mit Presidio vorannotieren, damit sie anschließend manuell korrigiert werden.

Die Vorannotation ist nur ein Vorschlag: Ein Modell, das ungeprüft auf Presidio-Ausgaben
trainiert wird, lernt Presidios Fehler mit. Erst die korrigierten Daten sind Trainings-
und Evaluationsdaten (Goldstandard).

Eingabe:  .txt-Dateien (Datei oder Verzeichnis, rekursiv) oder .jsonl mit Feld "text".
Ausgabe:  JSONL im Doccano-Format  {"id", "text", "label": [[start, end, LABEL], ...], "meta"}
          oder Label-Studio-JSON (--format labelstudio, Vorschläge als "predictions").

Beispiel:
  .venv/bin/python training/preannotate.py texte/ -o training/data/preannotated.jsonl --language de
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pii_service  # noqa: E402


def read_documents(paths):
    for path in paths:
        files = sorted(path.rglob("*.txt")) + sorted(path.rglob("*.jsonl")) if path.is_dir() else [path]
        for file in files:
            if file.suffix == ".jsonl":
                for n, line in enumerate(file.read_text(encoding="utf-8").splitlines()):
                    if line.strip():
                        yield f"{file.name}:{n}", json.loads(line)["text"]
            else:
                yield file.name, file.read_text(encoding="utf-8")


def split_paragraphs(doc_id, text, max_chars):
    """Lange Dokumente an Leerzeilen teilen – kürzere Einheiten lassen sich besser prüfen."""
    if max_chars <= 0 or len(text) <= max_chars:
        yield doc_id, text
        return
    for n, part in enumerate(p.strip() for p in re.split(r"\n\s*\n", text)):
        if part:
            yield f"{doc_id}#{n}", part


def non_overlapping(findings):
    """NER braucht überlappungsfreie Spans: höherer Score gewinnt, bei Gleichstand der längere."""
    chosen = []
    for f in sorted(findings, key=lambda f: (-f["score"], f["start"] - f["end"])):
        if all(f["end"] <= c["start"] or f["start"] >= c["end"] for c in chosen):
            chosen.append(f)
    return sorted(chosen, key=lambda f: f["start"])


def to_labelstudio(record, model_version):
    return {
        "data": {"text": record["text"], "id": record["id"]},
        "predictions": [{
            "model_version": model_version,
            "result": [
                {
                    "from_name": "label", "to_name": "text", "type": "labels",
                    "value": {"start": s, "end": e, "text": record["text"][s:e], "labels": [label]},
                    "score": score,
                }
                for (s, e, label), score in zip(record["label"], record["meta"]["scores"])
            ],
        }],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--engine", default="presidio-gliner")
    ap.add_argument("--language", default="de")
    ap.add_argument("--threshold", type=float, default=0.3,
                    help="eher niedrig: ein falscher Vorschlag ist schneller gelöscht als ein fehlender ergänzt")
    ap.add_argument("--entities", nargs="*", help="nur diese Entitäten (Standard: alle)")
    ap.add_argument("--max-chars", type=int, default=2000,
                    help="längere Dokumente an Leerzeilen teilen (0 = nie)")
    ap.add_argument("--format", choices=["doccano", "labelstudio"], default="doccano")
    args = ap.parse_args()

    analyzer = pii_service.EngineRegistry().get(args.engine)
    records, counts = [], {}
    for doc_id, text in read_documents(args.inputs):
        for unit_id, unit in split_paragraphs(doc_id, text, args.max_chars):
            results = analyzer.analyze(text=unit, language=args.language,
                                       entities=args.entities, score_threshold=args.threshold)
            spans = non_overlapping([
                {"start": r.start, "end": r.end, "label": r.entity_type, "score": round(r.score, 3)}
                for r in results
            ])
            for s in spans:
                counts[s["label"]] = counts.get(s["label"], 0) + 1
            records.append({
                "id": unit_id,
                "text": unit,
                "label": [[s["start"], s["end"], s["label"]] for s in spans],
                "meta": {"source": "presidio", "engine": args.engine,
                         "scores": [s["score"] for s in spans]},
            })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "labelstudio":
        tasks = [to_labelstudio(r, f"presidio/{args.engine}") for r in records]
        args.output.write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        with args.output.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"{len(records)} Texteinheiten -> {args.output}")
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {label:<22} {n}")


if __name__ == "__main__":
    main()
