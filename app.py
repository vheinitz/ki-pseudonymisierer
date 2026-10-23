"""Web-Oberfläche zur PII-Anonymisierung mit Presidio.

Entwicklung:  .venv/bin/flask --app app run --debug
Betrieb:      .venv/bin/gunicorn -c gunicorn.conf.py app:app
"""

import os

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import requests

import agents
import deny_list
import file_extract
import gateway_store
import mapping_store
import pii_service
import placeholders
import profiles
import replacement_rules

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
engines = pii_service.EngineRegistry()
# PII_DATA_DIR: anderes Verzeichnis für die Zuordnungstabelle (z. B. für Tests)
data_dir = os.environ.get("PII_DATA_DIR", mapping_store.DATA_DIR)
store = mapping_store.MappingStore(data_dir)
rules = replacement_rules.RuleStore(data_dir)
profile_store = profiles.ProfileStore(data_dir)
deny_store = deny_list.DenyListStore(data_dir)
agent_sessions = agents.SessionStore(data_dir)
gateway_log = gateway_store.GatewayStore(data_dir)
GATEWAY_URL = os.environ.get("ANON_GATEWAY_URL", "http://127.0.0.1:8787")


@app.get("/")
def index():
    return render_template("index.html", upload_types=",".join(file_extract.SUPPORTED))


@app.get("/api/engines")
def list_engines():
    return jsonify(engines=engines.describe(), operators=pii_service.OPERATORS)


@app.get("/api/entities")
def list_entities():
    return jsonify(entities=engines.entities(request.args["engine"], request.args["language"]))


@app.post("/api/anonymize")
def anonymize():
    req = request.get_json()
    req["deny_list"] = deny_store.get()
    profile = profile_store.get(req["profile"]) if req.get("profile") else None
    return jsonify(pii_service.anonymize(engines, req, store, rules.all(), profile))


@app.post("/api/decrypt")
def decrypt():
    return jsonify(pii_service.decrypt(request.get_json()))


@app.post("/api/extract")
def extract():
    upload = request.files["file"]
    text, warnings = file_extract.extract_text(upload.filename, upload.read())
    return jsonify(text=text, filename=upload.filename, warnings=warnings)


@app.post("/api/restore")
def restore():
    req = request.get_json()
    return jsonify(placeholders.restore(req["text"], store, rules.all(), req.get("language", "auto")))


@app.get("/api/rules")
def get_rules():
    return jsonify(rules=rules.all())


@app.put("/api/rules")
def put_rules():
    return jsonify(rules=rules.replace_all(request.get_json()["rules"]))


@app.get("/api/agents")
def list_agents():
    return jsonify(agents=agents.load_agents(), sessions=agent_sessions.all())


@app.post("/api/agents/ask")
def ask_agent():
    req = request.get_json()
    agent = next((a for a in agents.load_agents() if a["id"] == req["agent"]), None)
    if agent is None:
        return jsonify(error=f"Unbekannter Agent: {req['agent']}"), 400
    if not req.get("text", "").strip():
        return jsonify(error="Kein Text zum Senden"), 400
    return jsonify(agents.ask(agent, req["text"], req.get("session", "new"), agent_sessions))


@app.get("/api/deny-list")
def get_deny_list():
    return jsonify(lines=deny_store.get())


@app.put("/api/deny-list")
def put_deny_list():
    return jsonify(lines=deny_store.put(request.get_json()["lines"]))


@app.get("/api/profiles")
def get_profiles():
    return jsonify(profiles=profile_store.all(), defaults={
        "confirm_labels": profiles.DEFAULT_CONFIRM_LABELS,
        "blocking_cues": profiles.DEFAULT_BLOCKING_CUES})


@app.put("/api/profiles")
def put_profiles():
    return jsonify(profiles=profile_store.replace_all(request.get_json()["profiles"]))


@app.get("/api/gateway/overview")
def gateway_overview():
    try:
        status = requests.get(f"{GATEWAY_URL}/_gateway/status", timeout=2).json()
    except requests.RequestException:
        status = None
    return jsonify(status=status, gateway_url=GATEWAY_URL, summary=gateway_log.summary(),
                   findings=gateway_log.findings(), exchanges=gateway_log.exchanges(50))


@app.post("/api/gateway/logging/<state>")
def gateway_logging(state):
    if state not in ("on", "off"):
        return jsonify(error="on oder off"), 400
    return jsonify(requests.post(f"{GATEWAY_URL}/_gateway/logging/{state}", timeout=2).json())


@app.get("/api/gateway/block/<block_hash>")
def gateway_block(block_hash):
    block = gateway_log.block(block_hash)
    return (jsonify(block), 200) if block else (jsonify(error="unbekannter Block"), 404)


@app.get("/api/rules/export")
def export_rules():
    return Response(replacement_rules.to_csv(rules.all()), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=ersetzungsliste.csv"})


@app.post("/api/rules/import")
def import_rules():
    imported = replacement_rules.from_csv(request.files["file"].read())
    merged = replacement_rules.merge(rules.all(), imported)
    return jsonify(rules=rules.replace_all(merged), imported=len(imported))


@app.get("/api/mappings")
def mapping_stats():
    return jsonify(store.stats())


@app.errorhandler(Exception)
def handle_error(exc):
    if isinstance(exc, HTTPException):
        return jsonify(error=f"{exc.code} {exc.name}"), exc.code
    app.logger.exception(exc)
    return jsonify(error=str(exc)), 400
