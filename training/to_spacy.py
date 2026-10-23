"""Korrigierte Annotationen in spaCy-Trainingsdaten (.spacy, DocBin) umwandeln.

Eingabe: Doccano-JSONL (wie von preannotate.py erzeugt) oder Label-Studio-JSON-Export.
Spans, die nicht auf Token-Grenzen liegen, werden auf ganze Token erweitert und gemeldet.

Beispiel (nur kontextabhängige Entitäten lernen, Muster-Entitäten bleiben bei Presidio):
  .venv/bin/python training/to_spacy.py training/data/reviewed.jsonl -o training/data \
      --labels PERSON LOCATION ORGANIZATION ADDRESS DATE_TIME
"""

import argparse
import json
import random
from pathlib import Path

import spacy
from spacy.tokens import DocBin


def load_records(path):
    raw = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        for line in raw.splitlines():
            if line.strip():
                r = json.loads(line)
                yield r["text"], [tuple(l) for l in r.get("label", [])]
        return
    for task in json.loads(raw):  # Label-Studio-Export
        annotations = [a for a in task.get("annotations", []) if not a.get("was_cancelled")]
        if not annotations:
            continue  # nur geprüfte Aufgaben übernehmen
        spans = [
            (r["value"]["start"], r["value"]["end"], r["value"]["labels"][0])
            for r in annotations[-1]["result"] if r.get("type") == "labels"
        ]
        yield task["data"]["text"], spans


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path)
    ap.add_argument("-o", "--output-dir", type=Path, required=True)
    ap.add_argument("--language", default="de")
    ap.add_argument("--labels", nargs="*", help="nur diese Labels übernehmen (Standard: alle)")
    ap.add_argument("--dev-ratio", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    nlp = spacy.blank(args.language)
    docs, expanded, dropped = [], 0, 0
    for text, spans in load_records(args.input):
        doc = nlp.make_doc(text)
        ents = []
        for start, end, label in sorted(spans):
            if args.labels and label not in args.labels:
                continue
            span = doc.char_span(start, end, label=label, alignment_mode="strict")
            if span is None:
                span = doc.char_span(start, end, label=label, alignment_mode="expand")
                expanded += 1
                print(f"  erweitert: {text[start:end]!r} -> {span.text if span else None!r} [{label}]")
            if span is None or (ents and span.start < ents[-1].end):
                dropped += 1
                continue
            ents.append(span)
        doc.ents = ents
        docs.append(doc)

    random.Random(args.seed).shuffle(docs)
    n_dev = max(1, int(len(docs) * args.dev_ratio)) if len(docs) > 1 else 0
    args.output_dir.mkdir(parents=True, exist_ok=True)
    DocBin(docs=docs[n_dev:]).to_disk(args.output_dir / "train.spacy")
    DocBin(docs=docs[:n_dev]).to_disk(args.output_dir / "dev.spacy")

    counts = {}
    for doc in docs:
        for ent in doc.ents:
            counts[ent.label_] = counts.get(ent.label_, 0) + 1
    print(f"{len(docs) - n_dev} train / {n_dev} dev -> {args.output_dir}  "
          f"(erweitert: {expanded}, verworfen: {dropped})")
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {label:<22} {n}")


if __name__ == "__main__":
    main()
