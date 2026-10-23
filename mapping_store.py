"""Zuordnungstabelle Platzhalter-ID -> Originalwert für die Re-Identifikation.

Die Tabelle enthält die personenbezogenen Daten im Klartext-Äquivalent und ist damit der
schützenswerteste Teil des Systems: Werte liegen Fernet-verschlüsselt (AES-128-CBC + HMAC)
in SQLite, der Schlüssel in einer eigenen Datei mit Rechten 600. Das schützt vor
versehentlichem Einsehen der Datenbank, nicht vor jemandem mit Zugriff auf das Verzeichnis.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

DATA_DIR = Path(__file__).resolve().parent / "data"


class PlaceholderConflict(Exception):
    """Eine ID ist bereits mit einem anderen Wert belegt (bei 48 Bit praktisch ausgeschlossen)."""


class MappingStore:
    def __init__(self, data_dir=DATA_DIR):
        data_dir = Path(data_dir)
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_file = data_dir / "mapping.key"
        if not key_file.exists():
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(Fernet.generate_key())
        self._fernet = Fernet(key_file.read_bytes())
        self._lock = threading.Lock()
        self._db = sqlite3.connect(data_dir / "mappings.sqlite", check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS placeholders (
                   id          TEXT PRIMARY KEY,
                   entity_type TEXT NOT NULL,
                   value       BLOB NOT NULL,
                   document    TEXT,
                   created_at  TEXT NOT NULL)"""
        )
        self._db.commit()

    def save(self, entries, document=None):
        """entries: [(id, entity_type, value)]. Bereits gespeicherte IDs bleiben unverändert."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            existing = self._lookup_locked([e[0] for e in entries])
            rows = []
            for pid, entity_type, value in entries:
                if pid in existing:
                    if existing[pid] != (entity_type, value):
                        raise PlaceholderConflict(
                            f"ID {pid} ist bereits mit einem anderen Wert belegt – "
                            "bitte „neue IDs“ wählen und erneut übernehmen.")
                    continue
                rows.append((pid, entity_type, self._fernet.encrypt(value.encode("utf-8")),
                             document, now))
            self._db.executemany("INSERT INTO placeholders VALUES (?, ?, ?, ?, ?)", rows)
            self._db.commit()
            return len(rows)

    def lookup(self, ids):
        """{id: (entity_type, value)} für die gefundenen IDs."""
        with self._lock:
            return self._lookup_locked(ids)

    def _lookup_locked(self, ids):
        ids = list(set(ids))
        found = {}
        for i in range(0, len(ids), 500):  # SQLite-Grenze für Parameter
            chunk = ids[i:i + 500]
            query = ("SELECT id, entity_type, value FROM placeholders WHERE id IN (%s)"
                     % ",".join("?" * len(chunk)))
            for pid, entity_type, value in self._db.execute(query, chunk):
                found[pid] = (entity_type, self._fernet.decrypt(value).decode("utf-8"))
        return found

    def stats(self):
        with self._lock:
            count, oldest = self._db.execute(
                "SELECT COUNT(*), MIN(created_at) FROM placeholders").fetchone()
        return {"count": count, "oldest": oldest}
