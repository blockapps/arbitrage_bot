"""Lightweight HTTP health-check server (stdlib only)."""

import json
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler

log = logging.getLogger("arb_bot")

_bot_ref = None


def _set_bot(bot):
    global _bot_ref
    _bot_ref = bot


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return

        healthy = _bot_ref is not None and getattr(_bot_ref, "running", False)
        status_code = 200 if healthy else 500
        body = json.dumps({"status": healthy, "message": "pong"})

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, fmt, *args):
        pass


def start_health_server(bot, port=8080):
    """Start the health endpoint in a daemon thread."""
    _set_bot(bot)
    server = HTTPServer(("0.0.0.0", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info(f"health endpoint listening on :{port}/health")
    return server
