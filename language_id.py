"""Blockweise Sprachidentifikation mit fastText lid.176 und Glättung über Nachbarblöcke.

lid.176 mittelt gelernte Vektoren aus Wörtern und Zeichen-n-Grammen (2–4, mit Wortrand-
markern, also auch Präfixe/Suffixe), ohne Wortfolge. Bei kurzen Blöcken – Adresszeilen,
Namen, Telefonnummern – ist das Ergebnis unzuverlässig; solche Blöcke übernehmen die
Sprache des nächsten zuverlässigen Nachbarn.
"""

import re
import threading
from pathlib import Path

import fasttext

MODEL_PATH = Path(__file__).resolve().parent / "models" / "lid" / "lid.176.bin"

_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    with _lock:
        if _model is None:
            if not MODEL_PATH.exists():
                raise FileNotFoundError(
                    f"fastText-Modell fehlt: {MODEL_PATH} – herunterladen von "
                    "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin"
                )
            _model = fasttext.load_model(str(MODEL_PATH))
        return _model


def _split(text, granularity):
    """Blöcke als (start, end) – Zeilen, oder Sätze innerhalb von Zeilen."""
    for line in re.finditer(r"[^\n]+", text):
        if granularity != "sentence":
            yield line.start(), line.end()
            continue
        start = line.start()
        for boundary in re.finditer(r"(?<=[.!?])\s+(?=[\"„(]?[A-ZÄÖÜ])", line.group()):
            # Abkürzungen (Dr., Tel., z.B.) sind kein Satzende: das Wort vor dem Punkt
            # muss mindestens 4 Buchstaben haben und darf selbst keinen Punkt enthalten.
            previous = line.group()[:boundary.start()].split()[-1]
            if previous[-1] == "." and (len(previous) < 5 or "." in previous[:-1]):
                continue
            yield start, line.start() + boundary.start()
            start = line.start() + boundary.end()
        yield start, line.end()


def _predict(text):
    labels, probs = _get_model().predict(" ".join(text.split()), k=1)
    return (labels[0].removeprefix("__label__"), float(probs[0])) if labels else (None, 0.0)


def detect_blocks(text, supported, fallback, granularity="line", min_letters=15, min_score=0.6):
    """Sprache je Block bestimmen.

    Gibt (blocks, segments) zurück:
      blocks:   [{start, end, detected, score, reliable, language}] – zur Anzeige
      segments: [(start, end, language)] – zusammenhängende Bereiche gleicher Sprache,
                die gemeinsam analysiert werden (spaCy bekommt so mehr Kontext).
    language ist eine der unterstützten Sprachen; supported=None lässt jede erkannte
    Sprache zu (Rückübersetzung der Ersetzungsliste braucht kein spaCy-Modell).
    """
    blocks = []
    for start, end in _split(text, granularity):
        chunk = text[start:end]
        if not chunk.strip():
            continue
        detected, score = _predict(chunk)
        letters = sum(c.isalpha() for c in chunk)
        blocks.append({
            "start": start, "end": end, "detected": detected, "score": round(score, 3),
            "reliable": letters >= min_letters and score >= min_score,
        })
    if not blocks:
        return [], []

    # Glättung: unzuverlässige Blöcke erben vom nächsten zuverlässigen Nachbarn.
    # Ohne zuverlässigen Block: Sprache des Gesamttexts.
    reliable = [i for i, b in enumerate(blocks) if b["reliable"]]
    whole_text, _ = _predict(text)
    for i, b in enumerate(blocks):
        if b["reliable"]:
            lang = b["detected"]
        elif reliable:
            # Bei gleichem Abstand gewinnt der Nachbar, dessen Sprache der Block selbst
            # (unsicher) erkannt hat, sonst der vorherige.
            lang = blocks[min(reliable, key=lambda j: (
                abs(j - i), blocks[j]["detected"] != b["detected"], j > i))]["detected"]
        else:
            lang = whole_text
        b["language"] = lang if supported is None or lang in supported else fallback

    segments = []
    for b in blocks:
        if segments and segments[-1][2] == b["language"]:
            segments[-1] = (segments[-1][0], b["end"], b["language"])
        else:
            segments.append((b["start"], b["end"], b["language"]))
    return blocks, segments
