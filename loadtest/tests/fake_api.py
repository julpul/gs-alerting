"""Minimal stand-in for the API, RabbitMQ management and Prometheus.

Used to smoke-test the k6 scripts without the real stack:

    python3 loadtest/tests/fake_api.py 18080 &
    API_URL=http://localhost:18080 RABBIT_URL=http://localhost:18080 PROM_URL= \
        DURATION=10s RATE=20 k6 run loadtest/k6/t1_latency.js

Answers 202 to every POST under /v1/ingest and /v1/alerts/raise (batch: {accepted, eventIds}),
and reports a backlog of FAKE_READY messages (default 0) on events.in / deliveries.sms.
"""

import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path.endswith("/batch"):
            events = json.loads(raw)
            if isinstance(events, dict):
                events = events["events"]
            self._send(202, {"accepted": len(events), "eventIds": [str(uuid.uuid4()) for _ in events]})
        else:
            self._send(202, {"eventId": str(uuid.uuid4())})

    def do_GET(self) -> None:
        queue = {
            "messages_ready": int(os.environ.get("FAKE_READY", "0")),
            "messages_unacknowledged": 0,
            "messages": 0,
        }
        if self.path == "/api/queues":
            self._send(200, [{"name": "events.in", **queue}, {"name": "deliveries.sms", **queue}])
        elif self.path.startswith("/api/queues/"):
            self._send(200, queue)
        else:
            self._send(200, {"status": "success", "data": {"result": []}})

    def log_message(self, *_args: object) -> None:
        return


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
