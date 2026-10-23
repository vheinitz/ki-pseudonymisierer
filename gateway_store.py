"""Protokoll des LLM-Gateways: Austausche und deduplizierte Inhaltsblöcke (SQLite).

Claude Code schickt bei jedem Schritt den kompletten Verlauf. Jeder Inhaltsblock (Systemprompt,
Werkzeugdefinition, Nachrichtenteil, Werkzeugergebnis, Antwort) wird deshalb nur einmal unter
seinem Hash gespeichert; ein Austausch verweist auf seine Blöcke. Die Analyse prüft so jeden
Inhalt nur einmal, und dieselbe Struktur dient später als Zwischenspeicher fürs Anonymisieren.

Enthält die Kommunikation im Klartext – Datei unter data/ (Rechte 700/600). Zugangsdaten
(Authorization, x-api-key, Cookies) werden nie gespeichert.
"""

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
SECRET_HEADERS = {"authorization", "x-api-key", "cookie", "set-cookie", "proxy-authorization"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchanges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    duration_ms  INTEGER,
    method       TEXT, path TEXT, status INTEGER,
    model        TEXT,
    streamed     INTEGER,
    request_meta TEXT,   -- Header (ohne Zugangsdaten) und Parameter außer Inhalten, JSON
    usage        TEXT,   -- Token-Verbrauch aus der Antwort, JSON
    error        TEXT
);
CREATE TABLE IF NOT EXISTS blocks (
    hash        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,   -- system, tool_definition, user_text, assistant_text,
                                 -- tool_use, tool_result, thinking, response_text, response_tool_use, other
    content     TEXT NOT NULL,
    first_seen  INTEGER NOT NULL,           -- exchanges.id
    analyzed_at TEXT,
    findings    TEXT                        -- JSON-Liste der Fundstellen
);
CREATE TABLE IF NOT EXISTS exchange_blocks (
    exchange_id INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    direction   TEXT NOT NULL,              -- request / response
    hash        TEXT NOT NULL,
    PRIMARY KEY (exchange_id, direction, position)
);
CREATE INDEX IF NOT EXISTS idx_blocks_unanalyzed ON blocks(analyzed_at);
CREATE INDEX IF NOT EXISTS idx_exchange_blocks_hash ON exchange_blocks(hash);
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _hash(kind, content):
    return hashlib.sha256(f"{kind}\x1f{content}".encode("utf-8")).hexdigest()


class GatewayStore:
    def __init__(self, data_dir=DATA_DIR):
        data_dir = Path(data_dir)
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = data_dir / "gateway.sqlite"
        new = not path.exists()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        if new:
            os.chmod(path, 0o600)
        self._db.execute("PRAGMA journal_mode=WAL")  # Gateway schreibt, Analyse/Web lesen parallel
        self._db.executescript(_SCHEMA)
        self._lock = threading.Lock()

    # --- Schreiben (Gateway) -------------------------------------------------------------------

    def log_exchange(self, *, started_at, duration_ms, method, path, status, headers, request_body,
                     response_blocks, streamed, usage=None, error=None):
        meta = {"headers": {k: v for k, v in headers.items() if k.lower() not in SECRET_HEADERS}}
        model, request_blocks = None, []
        if isinstance(request_body, dict):
            model = request_body.get("model")
            meta["params"] = {k: v for k, v in request_body.items()
                              if k not in ("system", "messages", "tools")}
            request_blocks = list(request_blocks_of(request_body))
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO exchanges (started_at, duration_ms, method, path, status, model, streamed,"
                " request_meta, usage, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (started_at, duration_ms, method, path, status, model, int(streamed),
                 json.dumps(meta, ensure_ascii=False), json.dumps(usage) if usage else None, error))
            exchange_id = cur.lastrowid
            for direction, blocks in (("request", request_blocks), ("response", response_blocks)):
                for position, (kind, content) in enumerate(blocks):
                    h = _hash(kind, content)
                    self._db.execute("INSERT OR IGNORE INTO blocks (hash, kind, content, first_seen)"
                                     " VALUES (?, ?, ?, ?)", (h, kind, content, exchange_id))
                    self._db.execute("INSERT INTO exchange_blocks VALUES (?, ?, ?, ?)",
                                     (exchange_id, position, direction, h))
            self._db.commit()
        return exchange_id

    # --- Analyse ---------------------------------------------------------------------------------

    def unanalyzed(self, limit=50):
        with self._lock:
            return self._db.execute(
                "SELECT hash, kind, content FROM blocks WHERE analyzed_at IS NULL"
                " ORDER BY first_seen LIMIT ?", (limit,)).fetchall()

    def save_findings(self, block_hash, findings):
        with self._lock:
            self._db.execute("UPDATE blocks SET analyzed_at = ?, findings = ? WHERE hash = ?",
                             (_now(), json.dumps(findings, ensure_ascii=False), block_hash))
            self._db.commit()

    # --- Auswertung (Weboberfläche) --------------------------------------------------------------

    def _read(self, sql, params=()):
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def summary(self):
        exchanges, first, last = self._read(
            "SELECT COUNT(*), MIN(started_at), MAX(started_at) FROM exchanges")[0]
        blocks, analyzed = self._read("SELECT COUNT(*), COUNT(analyzed_at) FROM blocks")[0]
        return {"exchanges": exchanges, "first": first, "last": last,
                "blocks": blocks, "analyzed": analyzed}

    def exchanges(self, limit=100):
        rows = self._read(
            "SELECT e.id, e.started_at, e.duration_ms, e.method, e.path, e.status, e.model, e.usage,"
            " e.error, (SELECT COUNT(*) FROM blocks b WHERE b.first_seen = e.id) AS new_blocks"
            " FROM exchanges e ORDER BY e.id DESC LIMIT ?", (limit,))
        cols = ["id", "started_at", "duration_ms", "method", "path", "status", "model", "usage",
                "error", "new_blocks"]
        return [dict(zip(cols, r)) | {"usage": json.loads(r[7]) if r[7] else None} for r in rows]

    def findings(self):
        """Fundstellen über alle Blöcke, gruppiert nach (Typ, Text)."""
        grouped = {}
        rows = self._read(
            "SELECT b.hash, b.kind, b.findings, b.first_seen,"
            " (SELECT COUNT(*) FROM exchange_blocks x WHERE x.hash = b.hash),"
            " (SELECT started_at FROM exchanges WHERE id = b.first_seen)"
            " FROM blocks b WHERE b.findings IS NOT NULL AND b.findings != '[]'")
        for block_hash, kind, findings, first_seen, sent, first_at in rows:
            for f in json.loads(findings):
                key = (f["entity_type"], f["text"])
                g = grouped.setdefault(key, {"entity_type": f["entity_type"], "text": f["text"],
                                             "max_score": 0, "blocks": 0, "sent": 0, "kinds": set(),
                                             "first_at": first_at, "examples": []})
                g["max_score"] = max(g["max_score"], f["score"])
                g["blocks"] += 1
                g["sent"] += sent
                g["kinds"].add(kind)
                g["first_at"] = min(g["first_at"], first_at)
                if len(g["examples"]) < 3:
                    g["examples"].append({"hash": block_hash, "kind": kind, "start": f["start"],
                                          "end": f["end"]})
        out = sorted(grouped.values(), key=lambda g: (-g["sent"], g["entity_type"], g["text"]))
        for g in out:
            g["kinds"] = sorted(g["kinds"])
        return out

    def block(self, block_hash):
        rows = self._read("SELECT kind, content, findings FROM blocks WHERE hash = ?", (block_hash,))
        if not rows:
            return None
        row = rows[0]
        return {"kind": row[0], "content": row[1], "findings": json.loads(row[2] or "[]")}


# --- Zerlegung der Messages-API in Blöcke -------------------------------------------------------

def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text_of(c.get("text", c.get("content", "")) if isinstance(c, dict) else c)
                         for c in content)
    return json.dumps(content, ensure_ascii=False)


def request_blocks_of(body):
    """(kind, Text) für alles, was in einer Messages-API-Anfrage an das Modell geht."""
    system = body.get("system")
    if system:
        for part in (system if isinstance(system, list) else [{"text": system}]):
            yield "system", part.get("text", "") if isinstance(part, dict) else str(part)
    for tool in body.get("tools") or []:
        yield "tool_definition", json.dumps(tool, ensure_ascii=False, sort_keys=True)
    for message in body.get("messages") or []:
        role = message.get("role", "user")
        content = message.get("content")
        if isinstance(content, str):
            yield f"{role}_text", content
            continue
        for part in content or []:
            t = part.get("type")
            if t == "text":
                yield f"{role}_text", part.get("text", "")
            elif t == "tool_use":
                yield "tool_use", json.dumps({"name": part.get("name"), "input": part.get("input")},
                                             ensure_ascii=False)
            elif t == "tool_result":
                yield "tool_result", _text_of(part.get("content", ""))
            elif t in ("thinking", "redacted_thinking"):
                yield "thinking", part.get("thinking", "") or "[redacted]"
            elif t in ("image", "document"):
                yield "other", f"[{t}: {part.get('source', {}).get('media_type', '?')}]"
            else:
                yield "other", json.dumps(part, ensure_ascii=False)[:20000]


def response_blocks_of_json(body):
    for part in body.get("content") or []:
        if part.get("type") == "text":
            yield "response_text", part.get("text", "")
        elif part.get("type") == "tool_use":
            yield "response_tool_use", json.dumps({"name": part.get("name"), "input": part.get("input")},
                                                  ensure_ascii=False)
        elif part.get("type") == "thinking":
            yield "thinking", part.get("thinking", "")


def response_blocks_of_sse(raw):
    """Antwort aus einem SSE-Stream zusammensetzen: (Blöcke, usage)."""
    parts, usage = {}, {}
    for event in raw.split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
        if not data:
            continue
        try:
            ev = json.loads(data)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "message_start":
            usage.update(ev.get("message", {}).get("usage") or {})
        elif t == "content_block_start":
            block = ev.get("content_block", {})
            parts[ev["index"]] = {"type": block.get("type"), "name": block.get("name"),
                                  "text": block.get("text", "") or block.get("thinking", ""), "json": ""}
        elif t == "content_block_delta":
            d, p = ev.get("delta", {}), parts.setdefault(ev["index"], {"type": "text", "text": "", "json": ""})
            p["text"] += d.get("text", "") + d.get("thinking", "")
            p["json"] += d.get("partial_json", "")
        elif t == "message_delta":
            usage.update(ev.get("usage") or {})
    blocks = []
    for _, p in sorted(parts.items()):
        if p["type"] == "tool_use":
            try:
                tool_input = json.loads(p["json"] or "{}")
            except ValueError:
                tool_input = p["json"]
            blocks.append(("response_tool_use", json.dumps({"name": p["name"], "input": tool_input},
                                                           ensure_ascii=False)))
        elif p["type"] == "thinking":
            blocks.append(("thinking", p["text"]))
        elif p["text"]:
            blocks.append(("response_text", p["text"]))
    return blocks, usage
