"""Nachträgliche Analyse des Gateway-Protokolls: Welche privaten Daten sind rausgegangen?

Prüft jeden noch nicht analysierten Inhaltsblock aus data/gateway.sqlite mit derselben
Erkennung wie die Weboberfläche (Engine, Sprache automatisch, Ersetzungsliste, Profil) und
speichert die Fundstellen am Block. Jeder Block wird nur einmal geprüft, egal wie oft er in
Anfragen wiederholt wurde. Läuft als eigener Prozess, damit das Gateway schlank bleibt.

  .venv/bin/python gateway_analyze.py            # einmal alles Offene prüfen
  .venv/bin/python gateway_analyze.py --follow   # dauerhaft, prüft neue Blöcke laufend
"""

import argparse
import os
import time

import gateway_store
import pii_service
import profiles
import replacement_rules

# Werkzeugdefinitionen sind statischer Text von Claude Code, keine Nutzerdaten.
SKIP_KINDS = {"tool_definition"}
MAX_CHARS = 200_000  # sehr große Werkzeugausgaben in Teilen prüfen


def analyze_block(engines, req_base, kind, content, rules, profile):
    findings = []
    for offset in range(0, max(len(content), 1), MAX_CHARS):
        part = content[offset:offset + MAX_CHARS]
        res = pii_service.anonymize(engines, dict(req_base, text=part), rules=rules, profile=profile)
        for f in res["findings"]:
            findings.append({k: f[k] for k in ("entity_type", "text", "score", "recognizer")}
                            | {"start": f["start"] + offset, "end": f["end"] + offset})
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--follow", action="store_true", help="dauerhaft laufen und neue Blöcke prüfen")
    ap.add_argument("--interval", type=float, default=10, help="Sekunden zwischen Prüfungen (--follow)")
    ap.add_argument("--engine", default="presidio-gliner")
    ap.add_argument("--threshold", type=float, default=0.35)
    ap.add_argument("--profile", help="Profilname (z. B. Wissenschaftsgeschichte)")
    ap.add_argument("--data-dir", default=os.environ.get("PII_DATA_DIR", gateway_store.DATA_DIR))
    args = ap.parse_args()

    store = gateway_store.GatewayStore(args.data_dir)
    engines = pii_service.EngineRegistry()
    rules = replacement_rules.RuleStore(args.data_dir).all()
    profile = None
    if args.profile:
        profile = next((p for p in profiles.ProfileStore(args.data_dir).all() if p["name"] == args.profile), None)
        if profile is None:
            raise SystemExit(f"Profil „{args.profile}“ nicht gefunden")
    req_base = {"engine": args.engine, "language": "auto", "score_threshold": args.threshold,
                "default_operator": {"operator": "replace", "params": {}}}

    while True:
        batch = store.unanalyzed(limit=20)
        for block_hash, kind, content in batch:
            t0 = time.monotonic()
            findings = [] if kind in SKIP_KINDS else analyze_block(engines, req_base, kind, content,
                                                                    rules, profile)
            store.save_findings(block_hash, findings)
            print(f"{kind:<18} {len(content):>7} Zeichen  {len(findings):>3} Fundstellen  "
                  f"{time.monotonic() - t0:5.1f}s", flush=True)
        if not batch:
            if not args.follow:
                break
            time.sleep(args.interval)
    s = store.summary()
    print(f"Fertig: {s['analyzed']}/{s['blocks']} Blöcke analysiert, {s['exchanges']} Austausche.")


if __name__ == "__main__":
    main()
