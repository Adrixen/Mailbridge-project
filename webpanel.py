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
         color: var(--text); min-height: 100vh; padding: 1.5rem; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 0.25rem; }}
  .subtitle {{ color: var(--muted); margin-bottom: 1rem; font-size: 0.85rem; }}
  .toolbar {{ display: flex; gap: 0.75rem; align-items: center; margin-bottom: 1.25rem; flex-wrap: wrap; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 1rem; }}
  .card {{ background: var(--card); border-radius: 10px; padding: 1rem;
          border: 1px solid #334155; }}
  .card h3 {{ font-size: 1rem; margin-bottom: 0.5rem; color: var(--blue); }}
  .row {{ display: flex; justify-content: space-between; padding: 0.3rem 0;
          border-bottom: 1px solid #1e293b; font-size: 0.8rem; }}
  .row:last-child {{ border-bottom: none; }}
  .label {{ color: var(--muted); }}
  .status {{ font-weight: 600; text-transform: uppercase; font-size: 0.75rem;
            padding: 2px 8px; border-radius: 4px; }}
  .status.syncing {{ background: #1e3a5f; color: var(--blue); }}
  .status.idle {{ background: #14532d; color: var(--green); }}
  .status.error {{ background: #450a0a; color: var(--red); }}
  .status.unknown {{ background: #334155; color: var(--muted); }}
  .btn {{ background: var(--blue); color: #0f172a; border: none; padding: 0.4rem 1rem;
         border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 0.8rem; }}
  .btn:hover {{ opacity: 0.85; }}
  .btn.danger {{ background: var(--red); }}
  .error-msg {{ color: var(--red); font-size: 0.75rem; margin-top: 0.2rem; }}
  .history {{ display: flex; gap: 0.35rem; flex-wrap: wrap; margin-top: 0.25rem; }}
  .history-dot {{ padding: 1px 6px; border-radius: 3px; font-size: 0.7rem; }}
  .history-dot.ok {{ background: #14532d; color: var(--green); }}
  .history-dot.err {{ background: #450a0a; color: var(--red); }}
  .no-accounts {{ text-align: center; color: var(--muted); padding: 2rem; }}

  /* Logs section */
  .log-section {{ margin-top: 1.5rem; }}
  .log-section h2 {{ font-size: 1.1rem; margin-bottom: 0.5rem; }}
  #log-box {{ background: #0a0f1a; border: 1px solid #334155; border-radius: 8px;
             padding: 0.75rem; font-family: 'Courier New', monospace; font-size: 0.75rem;
             max-height: 600px; overflow-y: auto; color: var(--muted); line-height: 1.5; }}
  .log-line {{ white-space: pre-wrap; word-break: break-all; }}
  .auto-refresh {{ display: flex; align-items: center; gap: 0.5rem; font-size: 0.8rem; color: var(--muted); }}
</style>
</head>
<body>
<h1>📬 MailBridge</h1>
<p class="subtitle">WP.pl → Gmail sync dashboard — {now}</p>

<div class="toolbar">
  <button class="btn" onclick="location.reload()">🔄 Refresh page</button>
  <label class="auto-refresh"><input type="checkbox" id="autoRefresh" checked onchange="toggleAutoRefresh()"> Auto-refresh (5s)</label>
</div>

<div class="grid">
{cards}
</div>

<div class="log-section">
  <h2>📋 Live logs</h2>
  <div id="log-box"><div class="log-line">Waiting for logs...</div></div>
</div>

<p class="subtitle" style="margin-top:1rem;">Page generated at {now}</p>

<script>
let refreshTimer = null;
function toggleAutoRefresh() {{
  if (document.getElementById('autoRefresh').checked) {{
    startAutoRefresh();
  }} else {{
    stopAutoRefresh();
  }}
}}
function startAutoRefresh() {{
  stopAutoRefresh();
  refreshTimer = setInterval(() => {{ fetchLogs(); }}, 5000);
}}
function stopAutoRefresh() {{
  if (refreshTimer) {{ clearInterval(refreshTimer); refreshTimer = null; }}
}}
async function fetchLogs() {{
  try {{
    const res = await fetch('/api/logs');
    const text = await res.text();
    const box = document.getElementById('log-box');
    const wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 50;
    box.innerHTML = text.split('\\n').filter(l => l.trim()).map(l => `<div class="log-line">${{l}}</div>`).join('');
    if (wasAtBottom) box.scrollTop = box.scrollHeight;
  }} catch(e) {{}}
}}
startAutoRefresh();
</script>
</body>
</html>"""

_CARD_TEMPLATE = """<div class="card">
  <h3>{email}</h3>
  <div class="row"><span class="label">Status</span><span class="status {status_cls}">{status}</span></div>
  <div class="row"><span class="label">Last sync</span><span>{last_sync}</span></div>
  <div class="row"><span class="label">Last run</span><span>{last_run}</span></div>
  <div class="row"><span class="label">Total (all time)</span><span>{total_copied} copied, {total_errors} errors</span></div>
  <div class="row"><span class="label">Last 5 runs</span><span class="history">{history_dots}</span></div>
  {error_row}
</div>"""


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class StatusHandler(BaseHTTPRequestHandler):
    """HTTP handler that renders the status page."""

    # Class-level reference set by the server factory
    status_registry: "StatusRegistry" = None  # type: ignore[assignment]

    def log_message(self, format, *args):
        log.debug("HTTP %s", format % args)

    def do_GET(self):
        if self.path == "/":
            self._serve_page()
        elif self.path == "/api/status":
            self._serve_json()
        elif self.path.startswith("/api/logs"):
            self._serve_logs()
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
            for acc_id, info in snapshot.items():
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

                # Last run stats
                last_run = info.get("last_run", {})
                if last_run:
                    last_run_str = f'{last_run.get("copied", 0)} copied, {last_run.get("errors", 0)} errors'
                else:
                    last_run_str = "—"

                # History dots (last 5 runs)
                history = info.get("run_history", [])
                history_dots = ""
                for h in history:
                    if h.get("errors", 0) > 0:
                        history_dots += '<span class="history-dot err" title="' + str(h.get("copied", 0)) + ' copied, ' + str(h.get("errors", 0)) + ' errors">✗</span>'
                    elif h.get("copied", 0) > 0:
                        history_dots += '<span class="history-dot ok" title="' + str(h.get("copied", 0)) + ' copied">✓</span>'
                    else:
                        history_dots += '<span class="history-dot" style="background:#1e293b;color:#475569" title="0 copied, 0 errors">−</span>'
                if not history_dots:
                    history_dots = '<span style="color:var(--muted);font-size:0.75rem;">—</span>'

                cards += _CARD_TEMPLATE.format(
                    email=acc_id,
                    status=status,
                    status_cls=status,
                    last_sync=last_sync_str,
                    last_run=last_run_str,
                    total_copied=info.get("total_copied", 0),
                    total_errors=info.get("total_errors", 0),
                    history_dots=history_dots,
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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_logs(self):
        """Return the full mailbridge log file."""
        log_path = os.path.join(os.path.dirname(__file__), "logs", "mailbridge.log")
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except (FileNotFoundError, OSError):
            content = "Log file not found."

        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
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
