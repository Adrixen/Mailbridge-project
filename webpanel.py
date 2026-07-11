#!/usr/bin/env python3
"""
MailBridge — lightweight HTTP dashboard.

Uses stdlib ``http.server`` (no framework) to expose a read-only status page
on ``http://0.0.0.0:8080`` showing per-account sync state.

Run alongside ``bridge.py`` by passing a shared ``StatusRegistry``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from typing import Any, Dict, Optional

log = logging.getLogger("webpanel")

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MailBridge — Status</title>
<style>
  :root {{ --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --muted: #94a3b8;
          --green: #4ade80; --red: #f87171; --yellow: #fbbf24; --blue: #60a5fa; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; background: var(--bg);
         color: var(--text); min-height: 100vh; padding: 2rem; }}
  h1 {{ font-size: 1.75rem; margin-bottom: 0.5rem; }}
  .subtitle {{ color: var(--muted); margin-bottom: 2rem; font-size: 0.9rem; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
          gap: 1.25rem; }}
  .card {{ background: var(--card); border-radius: 12px; padding: 1.25rem;
          border: 1px solid #334155; }}
  .card h3 {{ font-size: 1.1rem; margin-bottom: 0.75rem; color: var(--blue); }}
  .row {{ display: flex; justify-content: space-between; padding: 0.35rem 0;
          border-bottom: 1px solid #1e293b; font-size: 0.875rem; }}
  .row:last-child {{ border-bottom: none; }}
  .label {{ color: var(--muted); }}
  .status {{ font-weight: 600; text-transform: uppercase; font-size: 0.8rem;
            padding: 2px 8px; border-radius: 4px; }}
  .status.syncing {{ background: #1e3a5f; color: var(--blue); }}
  .status.idle {{ background: #14532d; color: var(--green); }}
  .status.error {{ background: #450a0a; color: var(--red); }}
  .status.unknown {{ background: #334155; color: var(--muted); }}
  .btn {{ background: var(--blue); color: #0f172a; border: none; padding: 0.6rem 1.5rem;
         border-radius: 8px; font-weight: 600; cursor: pointer; font-size: 0.9rem; }}
  .btn:hover {{ opacity: 0.9; }}
  .error-msg {{ color: var(--red); font-size: 0.8rem; margin-top: 0.25rem; }}
  .refresh {{ text-align: center; margin: 1.5rem 0; }}
  .no-accounts {{ text-align: center; color: var(--muted); padding: 3rem; }}
</style>
</head>
<body>
<h1>📬 MailBridge</h1>
<p class="subtitle">WP.pl → Gmail sync dashboard — {now}</p>

<div class="refresh">
  <button class="btn" onclick="location.reload()">🔄 Refresh</button>
</div>

<div class="grid">
{cards}
</div>

<p class="subtitle" style="margin-top:2rem;">
  Page auto-generated at {now} · Refresh for latest status
</p>
</body>
</html>"""

_CARD_TEMPLATE = """<div class="card">
  <h3>{email}</h3>
  <div class="row"><span class="label">Status</span><span class="status {status_cls}">{status}</span></div>
  <div class="row"><span class="label">Last sync</span><span>{last_sync}</span></div>
  <div class="row"><span class="label">Total copied</span><span>{total_copied}</span></div>
  <div class="row"><span class="label">Total errors</span><span>{total_errors}</span></div>
  {error_row}
</div>"""


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class StatusHandler(BaseHTTPRequestHandler):
    """HTTP handler that renders the status page."""

    # Class-level reference set by the server factory
    status_registry: StatusRegistry = None

    def log_message(self, format, *args):
        log.debug("HTTP %s", format % args)

    def do_GET(self):
        if self.path == "/":
            self._serve_page()
        elif self.path == "/api/status":
            self._serve_json()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def _serve_page(self):
        snapshot = self.status_registry.snapshot() if self.status_registry else {}
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        cards = ""

        if not snapshot:
            cards = '<div class="no-accounts">No accounts configured yet.</div>'
        else:
            for acc_id, info in sorted(snapshot.items()):
                status = info.get("status", "unknown")
                last_sync = info.get("last_sync") or 0
                last_sync_str = (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_sync))
                    if last_sync
                    else "never"
                )
                error = info.get("last_error")
                error_row = (
                    f'<div class="error-msg">⚠ {error}</div>'
                    if error
                    else ""
                )
                cards += _CARD_TEMPLATE.format(
                    email=acc_id,
                    status=status,
                    status_cls=status,
                    last_sync=last_sync_str,
                    total_copied=info.get("total_copied", 0),
                    total_errors=info.get("total_errors", 0),
                    error_row=error_row,
                )

        html = _PAGE_TEMPLATE.format(now=now, cards=cards)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def _serve_json(self):
        snapshot = self.status_registry.snapshot() if self.status_registry else {}
        body = json.dumps(snapshot, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------


class WebPanel:
    """
    Lightweight HTTP dashboard server.

    Runs in a daemon thread; safe to start/stop alongside the main bridge process.

    Usage::

        panel = WebPanel(status_registry, host="0.0.0.0", port=8080)
        panel.start()
        ...
        panel.stop()
    """

    def __init__(
        self,
        status_registry: "StatusRegistry",
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        self._host = host
        self._port = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[Thread] = None

        # Inject registry into handler
        handler = type(
            "BoundStatusHandler",
            (StatusHandler,),
            {"status_registry": status_registry},
        )
        self._handler = handler

    def start(self) -> None:
        """Start the HTTP server in a daemon thread."""
        self._server = HTTPServer((self._host, self._port), self._handler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        log.info("Web panel listening on http://%s:%d", self._host, self._port)

    def stop(self) -> None:
        """Shut down the HTTP server."""
        if self._server:
            self._server.shutdown()
            self._server = None


# Fix forward reference at module level
from worker import StatusRegistry  # noqa: E402
