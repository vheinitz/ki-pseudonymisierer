#!/usr/bin/env bash
# Richtet alle Fremdpakete und Modelle ein. Mehrfach aufrufbar: Vorhandenes wird übersprungen.
#
#   ./setup.sh                 Python-Umgebung, Pakete, spaCy-, GLiNER- und fastText-Modell
#   ./setup.sh --mit-stimmen   zusätzlich Piper und die Stimmen für doc/vertonen.py (~300 MB)
#   ./setup.sh --nur-spacy     ohne GLiNER/torch (nur die spaCy-Engine, ~2 GB weniger)
#
# Danach starten:  .venv/bin/python app.py            (Entwicklung, http://127.0.0.1:5000)
#                  .venv/bin/gunicorn -c gunicorn.conf.py app:app
set -euo pipefail
cd "$(dirname "$0")"

MIT_STIMMEN=0
NUR_SPACY=0
for arg in "$@"; do
  case "$arg" in
    --mit-stimmen) MIT_STIMMEN=1 ;;
    --nur-spacy) NUR_SPACY=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unbekannte Option: $arg" >&2; exit 2 ;;
  esac
done

PY=${PYTHON:-python3}
VENV=.venv
PIP="$VENV/bin/python -m pip"
step() { printf '\n==> %s\n' "$*"; }

step "Python-Umgebung ($VENV)"
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "Python >= 3.10 nötig"'
if [ ! -x "$VENV/bin/python" ]; then
  # Manche Distributionen liefern venv ohne ensurepip; dann pip über get-pip.py nachrüsten.
  if ! "$PY" -m venv "$VENV" 2>/dev/null; then
    "$PY" -m venv --without-pip "$VENV"
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$VENV/get-pip.py"
    "$VENV/bin/python" "$VENV/get-pip.py" -q
    rm -f "$VENV/get-pip.py"
  fi
fi
$PIP install -q --upgrade pip

if [ "$NUR_SPACY" = 0 ]; then
  step "torch (CPU-Build – ohne diesen Schritt zieht gliner mehrere GB CUDA-Bibliotheken)"
  $PIP install -q torch --index-url https://download.pytorch.org/whl/cpu
fi

step "Python-Pakete (requirements.txt)"
if [ "$NUR_SPACY" = 1 ]; then
  grep -vE '^(gliner|sentencepiece)\b' requirements.txt > "$VENV/requirements-nur-spacy.txt"
  $PIP install -q -r "$VENV/requirements-nur-spacy.txt"
else
  $PIP install -q -r requirements.txt
fi

step "spaCy-Modelle"
for model in de_core_news_sm en_core_web_sm de_core_news_lg en_core_web_lg; do
  if "$VENV/bin/python" -c "import $model" 2>/dev/null; then
    echo "  $model vorhanden"
  else
    "$VENV/bin/python" -m spacy download "$model" -q
  fi
done

step "fastText-Sprachidentifikation (models/lid/lid.176.bin, 126 MB)"
mkdir -p models/lid
if [ ! -s models/lid/lid.176.bin ]; then
  curl -fL --progress-bar -o models/lid/lid.176.bin.part \
    https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.bin
  mv models/lid/lid.176.bin.part models/lid/lid.176.bin
else
  echo "  vorhanden"
fi

if [ "$NUR_SPACY" = 0 ]; then
  step "GLiNER-Modell urchade/gliner_multi_pii-v1 (~1,2 GB, Hugging-Face-Cache)"
  # Einmal online laden; danach schaltet pii_service.py Hugging Face auf offline.
  HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 "$VENV/bin/python" - <<'EOF'
from gliner import GLiNER
GLiNER.from_pretrained("urchade/gliner_multi_pii-v1")
print("  geladen")
EOF
fi

if [ "$MIT_STIMMEN" = 1 ]; then
  step "Piper und Stimmen für doc/vertonen.py (models/piper)"
  $PIP install -q piper-tts
  mkdir -p models/piper
  base=https://huggingface.co/rhasspy/piper-voices/resolve/main
  for voice in de/de_DE/thorsten/high/de_DE-thorsten-high \
               en/en_US/ryan/high/en_US-ryan-high \
               ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium; do
    name=$(basename "$voice")
    for ext in onnx onnx.json; do
      [ -s "models/piper/$name.$ext" ] && continue
      curl -fL --progress-bar -o "models/piper/$name.$ext" "$base/$voice.$ext"
    done
  done
fi

step "Systemprogramme (optional, werden nicht installiert)"
check() { if command -v "$1" >/dev/null; then echo "  ✓ $1"; else echo "  – $1 fehlt: $2"; fi; }
check soffice "Upload von .doc/.odt/.rtf (Paket libreoffice)"
check ffmpeg  "nur für doc/vertonen.py"
check claude  "nur für „2. KI-Anfrage“ mit Claude Code"
check pi      "nur für „2. KI-Anfrage“ mit pi"

step "Fertig"
echo "  Start: $VENV/bin/python app.py   →  http://127.0.0.1:5000"
echo "  Test:  $VENV/bin/python -m unittest tests.test_benchmark -v"
