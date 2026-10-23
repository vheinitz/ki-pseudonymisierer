"""LLM-Gateway für Claude Code – Phase 1: alles unverändert durchlassen und protokollieren.

Start:   .venv/bin/python gateway.py            (Port 8787, Ziel https://api.anthropic.com)
Nutzung: ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
Steuerung (ohne Modellaufruf):  curl -s localhost:8787/_gateway/status
                                curl -s -X POST localhost:8787/_gateway/logging/off   (bzw. /on)

Weiterleitung: Methode, Pfad, Header und Body unverändert; nur Hop-by-Hop-Header werden
ersetzt, und die Antwort wird unkomprimiert angefordert (Accept-Encoding: identity), damit
sie protokolliert werden kann. Gestreamte Antworten (SSE) gehen Stück für Stück sofort an
den Client; protokolliert wird nach Ende des Streams. Zugangsdaten werden nie gespeichert.
Was davon privat war, ermittelt ein eigener Prozess: gateway_analyze.py.
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import gateway_store

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
              "trailers", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}

state = {"logging": True, "started_at": None, "exchanges": 0, "errors": 0}
state_lock = threading.Lock()


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = "https://api.anthropic.com"
    store = None
    session = requests.Session()

    def log_message(self, fmt, *args):  # Zugriffsprotokoll knapp auf stderr
        sys.stderr.write(f"{datetime.now():%H:%M:%S} {self.command} {self.path} {fmt % args}\n")

    # --- Steuerung ------------------------------------------------------------------------------

    def _control(self):
        action = self.path.removeprefix("/_gateway/").strip("/")
        with state_lock:
            if action == "logging/on":
                state["logging"] = True
            elif action == "logging/off":
                state["logging"] = False
            elif action != "status":
                return self._send_json(404, {"error": f"unbekannt: {action}",
                                             "befehle": ["status", "logging/on", "logging/off"]})
            body = dict(state, upstream=self.upstream, phase="1: alles unverändert durchlassen, keine Anonymisierung")
        self._send_json(200, body)

    def _send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- Weiterleitung --------------------------------------------------------------------------

    def _forward(self):
        if self.path.startswith("/_gateway/"):
            return self._control()
        started, t0 = datetime.now(timezone.utc).isoformat(timespec="milliseconds"), time.monotonic()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["Accept-Encoding"] = "identity"

        raw, status, streamed, error = bytearray(), 502, False, None
        try:
            upstream = self.session.request(self.command, self.upstream + self.path, headers=headers,
                                            data=body or None, stream=True, timeout=(15, 600))
            status = upstream.status_code
            streamed = upstream.headers.get("Content-Type", "").startswith("text/event-stream")
            self.send_response(status)
            for k, v in upstream.headers.items():
                if k.lower() not in HOP_BY_HOP and k.lower() != "content-encoding":
                    self.send_header(k, v)
            if self.command == "HEAD":  # HEAD-Antworten haben keinen Rumpf
                self.send_header("Content-Length", upstream.headers.get("Content-Length", "0"))
                self.end_headers()
                return
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in upstream.iter_content(chunk_size=None):
                if chunk:
                    raw.extend(chunk)
                    self.wfile.write(b"%X\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError) as exc:
            error = f"Client hat die Verbindung beendet: {exc}"
        except requests.RequestException as exc:
            error = f"Upstream nicht erreichbar: {exc}"
            self._send_json(502, {"type": "error", "error": {"type": "api_error", "message": error}})
        finally:
            self._log(started, t0, body, bytes(raw), status, streamed, error)

    def _log(self, started, t0, body, raw, status, streamed, error):
        with state_lock:
            state["exchanges"] += 1
            state["errors"] += bool(error) or status >= 400
            if not state["logging"]:
                return
        try:
            request_body = json.loads(body) if body else None
        except ValueError:
            request_body = None
        text = raw.decode("utf-8", errors="replace")
        usage, response_blocks = None, []
        try:
            if streamed:
                response_blocks, usage = gateway_store.response_blocks_of_sse(text)
            elif text:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    response_blocks = list(gateway_store.response_blocks_of_json(parsed))
                    usage = parsed.get("usage")
                    if parsed.get("type") == "error":
                        error = error or json.dumps(parsed.get("error"), ensure_ascii=False)
        except ValueError:
            response_blocks = [("other", text[:20000])]
        try:
            self.store.log_exchange(
                started_at=started, duration_ms=int((time.monotonic() - t0) * 1000),
                method=self.command, path=self.path, status=status, headers=dict(self.headers),
                request_body=request_body, response_blocks=response_blocks, streamed=streamed,
                usage=usage, error=error)
        except Exception as exc:  # Protokollfehler dürfen die Weiterleitung nie stören
            sys.stderr.write(f"Protokollierung fehlgeschlagen: {exc}\n")

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _forward


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("ANON_GATEWAY_PORT", 8787)))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--upstream", default=os.environ.get("ANON_GATEWAY_UPSTREAM", "https://api.anthropic.com"))
    ap.add_argument("--data-dir", default=os.environ.get("PII_DATA_DIR", gateway_store.DATA_DIR))
    ap.add_argument("--no-logging", action="store_true", help="Protokollierung beim Start aus")
    args = ap.parse_args()

    GatewayHandler.upstream = args.upstream.rstrip("/")
    GatewayHandler.store = gateway_store.GatewayStore(args.data_dir)
    state["logging"] = not args.no_logging
    state["started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    server = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    server.daemon_threads = True
    print(f"Gateway: http://{args.host}:{args.port} -> {GatewayHandler.upstream} "
          f"(Protokoll: {'an' if state['logging'] else 'aus'})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
