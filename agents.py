"""KI-Anfrage: anonymisierten Text an einen lokal installierten KI-Agenten (claude, pi) senden.

Die Agenten laufen als Hintergrundprozess im nicht-interaktiven Modus, der Text geht über
die Standardeingabe (nicht als Argument – sonst stünde er in der Prozessliste, und
`claude --tools ""` würde ein folgendes Argument als Werkzeugnamen schlucken).
Konfiguriert werden sie in agents.json: je Agent Programm, Typ (bestimmt Sitzungs- und
Ausgabeformat) und zusätzliche Argumente, z. B. ein anderes Modell.

Sitzungen: Die Software vergibt die Sitzungs-ID selbst (UUID) und führt eine Liste der
Sitzungen, die sie angelegt hat (data/agent_sessions.json) – nur diese werden zum Fortsetzen
angeboten. „Ohne Sitzung“ speichert beim Agenten nichts.

Die Agenten laufen in einem leeren Arbeitsverzeichnis (data/agent_workdir) und, wie in
agents.json voreingestellt, ohne Werkzeuge: Sie sollen antworten, nicht Dateien lesen
oder Befehle ausführen.
"""

import json
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
AGENTS_FILE = BASE_DIR / "agents.json"
DATA_DIR = BASE_DIR / "data"


def load_agents(path=AGENTS_FILE):
    agents = json.loads(Path(path).read_text(encoding="utf-8"))
    for a in agents:
        if a.get("type") not in ("claude", "pi"):
            raise ValueError(f"Agent {a.get('id')}: type muss claude oder pi sein")
        a["available"] = shutil.which(a["executable"]) is not None
    return agents


class SessionStore:
    def __init__(self, data_dir=DATA_DIR):
        data_dir = Path(data_dir)
        self._path = data_dir / "agent_sessions.json"
        self.workdir = data_dir / "agent_workdir"
        self.pi_sessions = data_dir / "agent_sessions_pi"
        for d in (self.workdir, self.pi_sessions):
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def all(self, agent_id=None):
        with self._lock:
            sessions = json.loads(self._path.read_text(encoding="utf-8")) if self._path.exists() else []
        if agent_id:
            sessions = [s for s in sessions if s["agent"] == agent_id]
        return sorted(sessions, key=lambda s: s["last_used"], reverse=True)

    def get(self, session_id):
        return next((s for s in self.all() if s["id"] == session_id), None)

    def touch(self, session_id, agent_id, title):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            sessions = json.loads(self._path.read_text(encoding="utf-8")) if self._path.exists() else []
            entry = next((s for s in sessions if s["id"] == session_id), None)
            if entry is None:
                entry = {"id": session_id, "agent": agent_id, "title": title, "created": now, "turns": 0}
                sessions.append(entry)
            entry["last_used"] = now
            entry["turns"] += 1
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(sessions, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path)


def _command(agent, mode, session_id, store):
    """mode: new | resume | none."""
    cmd = [agent["executable"], "-p"] + list(agent.get("args", []))
    if agent["type"] == "claude":
        cmd += ["--output-format", "json"]
        if mode == "new":
            cmd += ["--session-id", session_id]
        elif mode == "resume":
            cmd += ["--resume", session_id]
        else:
            cmd += ["--no-session-persistence"]
    else:  # pi: --session-id legt an oder setzt fort
        if mode == "none":
            cmd += ["--no-session"]
        else:
            cmd += ["--session-dir", str(store.pi_sessions), "--session-id", session_id]
    return cmd


def ask(agent, text, session, store):
    """session: "new", "none" oder die ID einer von dieser Software angelegten Sitzung.

    Gibt {answer, session_id, mode, duration_s} zurück.
    """
    if not agent["available"]:
        raise ValueError(f"Programm „{agent['executable']}“ nicht gefunden")
    if session == "new":
        mode, session_id = "new", str(uuid.uuid4())
    elif session == "none":
        mode, session_id = "none", None
    else:
        known = store.get(session)
        if not known or known["agent"] != agent["id"]:
            raise ValueError("Unbekannte Sitzung – nur Sitzungen dieser Software können fortgesetzt werden")
        mode, session_id = "resume", session
    t0 = time.monotonic()
    try:
        proc = subprocess.run(_command(agent, mode, session_id, store), input=text, capture_output=True,
                              text=True, cwd=store.workdir, timeout=agent.get("timeout", 600))
    except subprocess.TimeoutExpired:
        raise ValueError(f"Keine Antwort nach {agent.get('timeout', 600)} s – abgebrochen")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise ValueError(f"{agent['label']} meldet Fehler (Code {proc.returncode}): {' '.join(detail)}")
    if agent["type"] == "claude":
        result = json.loads(proc.stdout)
        if result.get("is_error"):
            raise ValueError(f"{agent['label']}: {result.get('result') or result.get('subtype')}")
        answer = result.get("result", "")
    else:
        answer = proc.stdout.strip()
    if mode != "none":
        # Titel aus dem anonymisierten Text (nach dem Hinweis für die KI) – nie Klartext
        body = text.split("\n---\n", 1)[-1].strip()
        store.touch(session_id, agent["id"], " ".join(body.split())[:60] or "(leer)")
    return {"answer": answer, "session_id": session_id, "mode": mode,
            "duration_s": round(time.monotonic() - t0, 1)}
