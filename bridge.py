#!/usr/bin/env python3
"""
MailBridge — entrypoint.

Loads configuration, sets up logging, and runs the main scheduler loop that
dispatches per-account sync workers.

Usage::

    python bridge.py [--config config/config.yaml] [--passwords config/passwords.yaml]
                     [--state state.json] [--once] [--verbose] [--webpanel]

Run as a systemd service or manually from the command line.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict

from config import AppConfig, load_config
from gmail import build_gmail_delivery
from logger import setup_logging
from state import (
    load_state,
    save_state,
    get_cumulative_stats,
    set_cumulative_stats,
    reset_all_cumulative_stats,
)
from worker import AccountWorker, StatusRegistry


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_stop_event = False
_watchdog_enabled = False
_notify_socket: str | None = None
_last_watchdog: float = 0.0
_watchdog_interval: float = 0.0


def _handle_signal(signum, frame):
    global _stop_event
    log = logging.getLogger("bridge")
    log.info("Received signal %s, initiating graceful shutdown...", signum)
    _stop_event = True


def _sd_notify(state_bytes: bytes) -> None:
    """Send a notification to systemd via the notify socket."""
    ns = _notify_socket
    if not ns:
        return
    import socket

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.sendto(state_bytes, ns)
    finally:
        sock.close()


def _watchdog_ping() -> None:
    """Send WATCHDOG=1 if interval has elapsed. Safe to call anywhere."""
    global _last_watchdog
    if not _watchdog_enabled:
        return
    now = time.time()
    if now - _last_watchdog >= _watchdog_interval:
        _sd_notify(b"WATCHDOG=1")
        _last_watchdog = now


# ---------------------------------------------------------------------------
# Main scheduler
# ---------------------------------------------------------------------------


def run_cycle(
    config: AppConfig,
    shared_state: Dict[str, Any],
    status_registry: StatusRegistry,
    log: logging.Logger,
) -> None:
    """
    Run one sync cycle: dispatch all account workers via a thread pool.

    Each worker gets its own GmailDelivery instance (IMAP connections are not
    thread-safe). The pool waits for all workers to finish before returning.
    """
    global _stop_event

    if _stop_event:
        return

    log.info("Starting sync cycle for %d account(s)", len(config.accounts))

    with ThreadPoolExecutor(max_workers=config.max_concurrency) as executor:
        futures = {}
        for i, account in enumerate(config.accounts):
            if _stop_event:
                break
            # Stagger logins to avoid overwhelming the wp.pl IMAP server
            if i > 0:
                # Sleep in 1s chunks so we ping watchdog and can stop gracefully
                for _ in range(30):
                    if _stop_event:
                        break
                    _watchdog_ping()
                    time.sleep(1)
                if _stop_event:
                    break

            # Each worker gets its own Gmail delivery backend
            try:
                gmail = build_gmail_delivery(config.gmail, config.retry)
            except Exception as exc:
                log.error(
                    "Failed to build Gmail backend for %s: %s", account.id, exc
                )
                continue

            worker = AccountWorker(
                account=account,
                app_config=config,
                gmail=gmail,
                shared_state=shared_state,
                status_registry=status_registry,
            )
            future = executor.submit(worker.run_once)
            futures[future] = (account.id, gmail)

        # Wait for all workers and clean up Gmail connections
        for future in as_completed(futures):
            if _stop_event:
                break
            account_id, gmail = futures[future]
            try:
                stats = future.result()
                log.debug("%s cycle stats: %s", account_id, stats)
            except Exception as exc:
                log.exception("Worker %s crashed: %s", account_id, exc)
            finally:
                try:
                    gmail.close()
                except Exception:
                    pass

        # Persist cumulative stats from StatusRegistry into shared_state
        snapshot = status_registry.snapshot()
        for acc_id, info in snapshot.items():
            set_cumulative_stats(
                shared_state,
                acc_id,
                copied=info.get("total_copied", 0),
                errors=info.get("total_errors", 0),
            )

    log.info("Sync cycle completed")


def main() -> int:
    """Entrypoint. Returns exit code (0 = success, 1 = error)."""
    # --- Argument parsing ---
    parser = argparse.ArgumentParser(
        description="MailBridge — WP.pl → Gmail email sync service"
    )
    parser.add_argument(
        "--config",
        default="config/config.yaml",
        help="Path to config.yaml (default: config/config.yaml)",
    )
    parser.add_argument(
        "--passwords",
        default="config/passwords.yaml",
        help="Path to passwords.yaml (default: config/passwords.yaml)",
    )
    parser.add_argument(
        "--state",
        default="state.json",
        help="Path to state.json (default: state.json)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one cycle and exit (no scheduler loop)",
    )
    parser.add_argument(
        "--webpanel", "--dashboard",
        action="store_true",
        dest="webpanel",
        help="Start optional web dashboard on http://0.0.0.0:8080",
    )
    parser.add_argument(
        "--webpanel-port",
        type=int,
        default=8080,
        help="Web dashboard port (default: 8080)",
    )
    parser.add_argument(
        "--resync",
        action="store_true",
        help="Reset last_uid to 0 for all accounts and re-import all messages (one run)",
    )
    parser.add_argument(
        "--reset-stats",
        action="store_true",
        help="Zero out all cumulative error/copied stats",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    args = parser.parse_args()

    # --- Setup ---
    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(log_dir="logs", level=log_level)

    log = logging.getLogger("bridge")

    # Load config & state
    try:
        config: AppConfig = load_config(args.config, args.passwords)
    except (ValueError, FileNotFoundError) as exc:
        log.error("Configuration error: %s", exc)
        return 1

    shared_state = load_state(args.state)

    # --reset-stats: zero out cumulative error/copied counters
    if args.reset_stats:
        log.warning("--reset-stats: zeroing all cumulative stats")
        reset_all_cumulative_stats(shared_state)
        save_state(args.state, shared_state)
        log.warning("Cumulative stats reset.")

    # --resync: reset all last_uid to 0 so all messages are re-imported
    if args.resync:
        log.warning("--resync enabled: resetting last_uid to 0 for all accounts")
        for acc_id, acc_data in shared_state.items():
            if acc_id.startswith("_"):
                continue  # skip internal keys like _stats
            if isinstance(acc_data, dict):
                for folder, folder_data in acc_data.items():
                    if isinstance(folder_data, dict) and "last_uid" in folder_data:
                        # Skip Spam — we don't want to re-import thousands of spam messages
                        if folder.upper() == "SPAM":
                            log.info("  Skipping %s/%s (Spam)", acc_id, folder)
                            continue
                        folder_data["last_uid"] = 0
        save_state(args.state, shared_state)
        log.warning("State reset complete. Running one sync cycle to re-import all messages.")

    cumulative = get_cumulative_stats(shared_state)
    status_registry = StatusRegistry(cumulative_stats=cumulative)

    # Pre-register accounts in config order so the web panel preserves order
    for acc in config.accounts:
        status_registry.update(acc.id, "idle")

    # Handle --once differently: run one cycle, save state, exit
    if args.once:
        log.info("MailBridge starting (--once mode)")
        run_cycle(config, shared_state, status_registry, log)
        save_state(args.state, shared_state)
        log.info("MailBridge finished (--once)")
        return 0

    # --- Signal handlers ---
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("MailBridge starting (poll_interval=%ds)", config.poll_interval)

    # --- Optional: systemd watchdog integration ---
    global _watchdog_enabled, _notify_socket, _last_watchdog, _watchdog_interval
    _notify_socket = os.environ.get("NOTIFY_SOCKET")
    if _notify_socket:
        try:
            _sd_notify(b"READY=1")
            watchdog_usec_raw = os.environ.get("WATCHDOG_USEC")
            if watchdog_usec_raw:
                watchdog_usec = int(watchdog_usec_raw)
                _watchdog_enabled = True
                _watchdog_interval = watchdog_usec / 2_000_000
                _last_watchdog = time.time()
                log.info(
                    "systemd watchdog enabled (interval=%d µs)", watchdog_usec
                )
        except Exception as exc:
            log.warning("systemd notify setup failed: %s", exc)

    # --- Optional: web dashboard ---
    web_panel = None
    if args.webpanel:
        try:
            from webpanel import WebPanel

            web_panel = WebPanel(
                status_registry,
                host="0.0.0.0",
                port=args.webpanel_port,
            )
            web_panel.start()
            log.info("Web dashboard started on http://0.0.0.0:%d", args.webpanel_port)
        except Exception as exc:
            log.warning("Failed to start web dashboard: %s", exc)

    # --- Main loop ---
    _last_watchdog = time.time()
    try:
        while not _stop_event:
            cycle_start = time.time()

            run_cycle(config, shared_state, status_registry, log)
            save_state(args.state, shared_state)

            if _stop_event:
                break

            # Sleep until next cycle (accounting for cycle duration)
            elapsed = time.time() - cycle_start
            sleep_time = max(1, config.poll_interval - elapsed)
            log.debug("Sleeping %.1fs until next cycle", sleep_time)

            # Sleep in small chunks to allow responsive shutdown and watchdog pings
            while sleep_time > 0 and not _stop_event:
                _watchdog_ping()
                chunk = min(1, sleep_time)
                time.sleep(chunk)
                sleep_time -= chunk

    finally:
        log.info("Shutting down...")
        if web_panel:
            try:
                web_panel.stop()
                log.info("Web dashboard stopped.")
            except Exception:
                pass
        save_state(args.state, shared_state)
        log.info("State saved. MailBridge stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
