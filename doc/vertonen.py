"""Screencast vertonen: Sprechertext mit Timecodes → Piper-Sprachausgabe → Tonspur im Video.

Aufruf:  .venv/bin/python doc/vertonen.py doc/pres.webm doc/pres.sprechertext.txt
Ergebnis: doc/pres_vertont.webm (das Original bleibt unverändert); mit pres.sprechertext_en.txt
          entsprechend doc/pres_vertont_en.webm und die englische Stimme.

Sprechertext: eine Zeile je Einsatz, Timecodes beziehen sich auf das ORIGINALvideo.
  m:ss | Text     Satz beginnt bei m:ss, das Video läuft weiter. Reicht die Zeit bis zum
                  nächsten Einsatz nicht, bleibt das Bild am nächsten Einsatz so lange stehen,
                  bis der Satz zu Ende ist – Sätze überlagern sich nie.
  m:ss ! | Text   Bild bleibt bei m:ss stehen, solange der Text gesprochen wird (für
                  Erklärungen, zu denen im Video nichts passiert, z. B. Einleitung, Schluss).
Das Video wird dadurch länger; Ton und Bild bleiben synchron.

Klang: Piper erzeugt Zischlaute bis 10 kHz und eine Resonanz um 6 kHz, die als Rauschen
auffallen. Deshalb weniger Rauschanteil in der Synthese, De-Esser, Tiefpass und feste
Pegelanpassung (keine dynamische Normierung, die das Rauschen in leisen Stellen anhebt).
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
# Stimme nach Suffix des Sprechertexts (pres.sprechertext_en.txt → en); ohne Suffix Deutsch
VOICES = {"de": BASE / "models/piper/de_DE-thorsten-high.onnx",
          "en": BASE / "models/piper/en_US-ryan-high.onnx",
          "ru": BASE / "models/piper/ru_RU-dmitri-medium.onnx"}
# piper aus der Projektumgebung (setup.sh --mit-stimmen), sonst aus dem Suchpfad
PIPER = next((str(p) for p in [Path(sys.executable).parent / "piper"] if p.exists()), None) or shutil.which("piper") or "piper"
GAP = 0.4    # Pause zwischen zwei Sätzen
FPS = 30     # Bildschirmaufnahmen haben variable Bildrate; für Standbilder vereinheitlichen
CLEAN = ("highpass=f=70,deesser=i=0.6:m=0.5:f=0.5,"
         "lowpass=f=7500,lowpass=f=7500,lowpass=f=7500,volume=-3dB")  # 3× = steile Flanke


def parse_script(path):
    cues = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"(\d+):(\d{1,2}(?:\.\d+)?)\s*(!?)\s*\|\s*(.+)", line)
        if not m:
            sys.exit(f"Zeile {n} nicht lesbar (erwartet „m:ss | Text“ oder „m:ss ! | Text“): {line}")
        cues.append((int(m[1]) * 60 + float(m[2]), bool(m[3]), m[4]))
    return sorted(cues, key=lambda c: c[0])


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return float(out)


def synthesize(text, wav, voice, length_scale):
    raw = wav.with_suffix(".raw.wav")
    subprocess.run([PIPER, "-m", str(voice), "--length-scale", str(length_scale),
                    "--noise-scale", "0.4", "--noise-w-scale", "0.6", "--sentence-silence", "0.3",
                    "-f", str(raw)], input=text, text=True, capture_output=True, check=True)
    # -v fatal: Piper-WAVs enden mit einem halben Sample, ffmpeg meldet das bei jedem Satz
    subprocess.run(["ffmpeg", "-v", "fatal", "-y", "-i", str(raw), "-af", CLEAN, "-ar", "48000", str(wav)],
                   check=True)
    return duration(wav)


def fmt(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("script")
    ap.add_argument("-o", "--output")
    ap.add_argument("--voice", help="Piper-Stimme (.onnx); Standard nach Sprachsuffix des Sprechertexts")
    ap.add_argument("--length-scale", type=float, default=1.12, help=">1 langsamer und ruhiger")
    args = ap.parse_args()

    video = Path(args.video)
    m = re.search(r"_([a-z]{2})$", Path(args.script).stem)
    lang = m[1] if m else "de"
    args.voice = args.voice or str(VOICES[lang])
    suffix = f"_{lang}" if m else ""
    output = Path(args.output or video.with_name(f"{video.stem}_vertont{suffix}.webm"))
    video_len = duration(video)
    cues = parse_script(args.script)
    tmp = Path(tempfile.mkdtemp(prefix="vertonen_"))

    # Standbilder: (Originalzeit, Dauer, Art). „over“ (Überlauf des vorigen Satzes) liegt VOR
    # dem Einsatz an derselben Stelle, „freeze“ (Satz mit !) läuft WÄHREND des Satzes.
    stills, spoken = [], []
    for i, (start, freeze, text) in enumerate(cues):
        wav = tmp / f"cue_{i:03d}.wav"
        length = synthesize(text, wav, args.voice, args.length_scale)
        next_t = cues[i + 1][0] if i + 1 < len(cues) else video_len
        if freeze:
            stills.append((start, length + GAP, "freeze"))
            note = f"Standbild {length + GAP:.1f} s"
        elif (over := length + GAP - (next_t - start)) > 0:
            stills.append((next_t, over, "over"))
            note = f"+ Standbild {over:.1f} s bei {fmt(next_t)}"
        else:
            note = ""
        spoken.append((start, wav, length, note, text))

    clips = []
    for start, wav, length, note, text in spoken:
        out = start + sum(d for t, d, kind in stills if t < start or (t == start and kind == "over"))
        clips.append((out, wav))
        print(f"{fmt(start)} → {fmt(out)}  {length:4.1f} s  {note:<28} {text[:55]}")
    holds = {}
    for t, d, _ in stills:
        holds[t] = holds.get(t, 0) + d

    # Video: Abschnitte zwischen Standbildern, an jedem Standbild das letzte Bild klonen
    # parts: (von, bis, Standbild am Ende); ein Standbild am Videoende hängt am letzten Abschnitt
    parts, a = [], 0.0
    for p in sorted(holds):
        b = min(p, video_len)
        if parts and b <= a and a >= video_len:
            parts[-1] = (parts[-1][0], parts[-1][1], parts[-1][2] + holds[p])
            continue
        parts.append((a, b, holds[p]))
        a = b
    if a < video_len:
        parts.append((a, video_len, 0))
    vf = [f"[0:v]fps={FPS},crop=trunc(iw/2)*2:trunc(ih/2)*2,split={len(parts)}" +
          "".join(f"[s{k}]" for k in range(len(parts)))]
    for k, (a, b, h) in enumerate(parts):
        pad = f",tpad=stop_mode=clone:stop_duration={h:.3f}" if h else ""
        if b - a < 1 / FPS:  # Standbild am Anfang: erstes Bild halten
            vf.append(f"[s{k}]trim=end_frame=1,setpts=PTS-STARTPTS{pad}[v{k}]")
        else:
            vf.append(f"[s{k}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS{pad}[v{k}]")
    vf.append("".join(f"[v{k}]" for k in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0[vout]")

    total = video_len + sum(holds.values())
    inputs = ["-i", str(video)]
    for k, (t, wav) in enumerate(clips, 1):
        inputs += ["-i", str(wav)]
        ms = int(t * 1000)
        vf.append(f"[{k}:a]adelay={ms}|{ms},apad[a{k}]")
    vf.append("".join(f"[a{k}]" for k in range(1, len(clips) + 1)) +
              f"amix=inputs={len(clips)}:normalize=0,alimiter=limit=0.9,atrim=0:{total:.3f}[aout]")

    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(vf),
                    "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-deadline", "good",
                    "-cpu-used", "5", "-c:a", "libopus", "-b:a", "96k", str(output)], check=True)
    print(f"\n→ {output}  ({fmt(video_len)} → {fmt(total)}, Stimme {Path(args.voice).stem})")


if __name__ == "__main__":
    main()
