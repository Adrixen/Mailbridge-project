"""
MailBridge — per-account sync worker.

Each ``AccountWorker`` runs a sync cycle for one account: connect to WP,
reconcile UIDVALIDITY, fetch new UIDs, and for each new message run the
fail-safe pipeline:

    1. Fetch RFC822 from WP (BODY.PEEK[])
    2. Deliver to Gmail
    3. Verify delivery success
    4. Move original to WP Trash
    5. Persist updated last_uid

Per-message errors are isolated; the worker never advances ``last_uid`` past
an unconfirmed message. With ``dry_run`` enabled, steps 2 and 4 are logged
but not executed.
"""

from __future__ import annotations

import email
import logging
import time
from typing import Any, Dict, List, Optional

import state as state_mod
from config import AccountConfig, AppConfig
from gmail import DeliveryResult, GmailDelivery, _extract_message_id
from logger import get_account_logger
from wp import (
    WPConnectionError,
    WPMailbox,
    WPPermanentError,
    WPTransientError,
)


class AccountWorker:
    """
    Sync worker for a single WP → Gmail account.

    :param account: account configuration
    :param app_config: global application configuration
    :param gmail: shared GmailDelivery backend (should be per-worker instance)
    :param shared_state: dict holding the full state.json content (shared across workers)
    :param status_registry: optional StatusRegistry for the web panel
    """

    def __init__(
        self,
        account: AccountConfig,
        app_config: AppConfig,
        gmail: GmailDelivery,
        shared_state: Dict[str, Any],
        status_registry: Optional["StatusRegistry"] = None,
    ):
        self._account = account
        self._app = app_config
        self._gmail = gmail
        self._state = shared_state
        self._status = status_registry
        self._log = get_account_logger(account.id)

        self._stats = {
            "copied": 0,
            "errors": 0,
            "skipped": 0,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> Dict[str, int]:
        """
        Run one sync cycle. Returns stats dict with keys: copied, errors, skipped.
        """
        self._stats = {"copied": 0, "errors": 0, "skipped": 0}
        self._report_status("syncing")

        try:
            with WPMailbox(
                host=self._app.wp.imap_host,
                port=self._app.wp.imap_port,
                email=self._account.email,
                password=self._account.password,
                timeout=self._app.connect_timeout,
                retry_config=self._app.retry,
            ) as wp:
                # Validate trash folder exists
                self._validate_trash_folder(wp)

                # Determine which folders to sync
                if self._app.wp.sync_all_folders:
                    all_folders = wp.list_folders()
                    folders = [
                        f for f in all_folders
                        if f not in self._app.wp.exclude_folders
                    ]
                    self._log.info(
                        "Auto-discovered %d folder(s), excluded %d: %s",
                        len(all_folders),
                        len(all_folders) - len(folders),
                        folders,
                    )
                else:
                    folders = self._account.folders

                # Sync each configured folder
                for folder in folders:
                    self._log.info("Syncing folder: %s", folder)
                    try:
                        self._sync_folder(wp, folder)
                    except (WPConnectionError, WPTransientError, WPPermanentError) as exc:
                        self._log.error(
                            "Folder %s sync aborted: %s", folder, exc
                        )
                        self._stats["errors"] += 1
                        self._report_status("error", str(exc))

        except WPConnectionError as exc:
            self._log.error("Connection failed for %s: %s", self._account.email, exc)
            self._stats["errors"] += 1
            self._report_status("error", str(exc))
        except Exception as exc:
            self._log.exception("Unexpected error syncing %s", self._account.email)
            self._stats["errors"] += 1
            self._report_status("error", str(exc))

        self._log.info(
            "Cycle done: %d copied, %d errors, %d skipped (last_uid varies per folder)",
            self._stats["copied"],
            self._stats["errors"],
            self._stats["skipped"],
        )
        self._report_status("idle")
        return self._stats

    # ------------------------------------------------------------------
    # Folder sync
    # ------------------------------------------------------------------

    def _sync_folder(self, wp: WPMailbox, folder: str) -> None:
        """Run the full sync pipeline for a single folder."""
        uidvalidity, uidnext = wp.select(folder, readonly=False)

        # --- Determine Gmail target mailbox for this folder ---
        gmail_target = self._gmail_mailbox_for(folder)
        self._log.debug("Gmail target for '%s': %s", folder, gmail_target)

        # --- UIDVALIDITY reconciliation ---
        stored_validity = state_mod.get_uidvalidity(self._state, self._account.id, folder)
        stale_last_uid = state_mod.get_last_uid(self._state, self._account.id, folder)
        last_uid = stale_last_uid

        if stored_validity is None:
            # First run
            self._log.info(
                "First run for folder '%s': UIDVALIDITY=%s, UIDNEXT=%s",
                folder,
                uidvalidity,
                uidnext,
            )
            state_mod.set_uidvalidity(self._state, self._account.id, folder, uidvalidity)
            if self._app.initial_import:
                # Import all existing mail
                last_uid = 0
                self._log.info("initial_import=true: importing all existing mail")
            elif uidnext is not None and uidnext > 1:
                # Skip existing history; set last_uid to UIDNEXT - 1
                last_uid = uidnext - 1
                state_mod.set_last_uid(self._state, self._account.id, folder, last_uid)
                self._log.info(
                    "Skipping existing mail: set last_uid=%d (UIDNEXT-1)", last_uid
                )
            else:
                # Empty folder (UIDNEXT=0 or 1) — nothing to skip
                last_uid = 0
                state_mod.set_last_uid(self._state, self._account.id, folder, 0)
        elif stored_validity != uidvalidity:
            # UIDVALIDITY changed — mailbox was rebuilt
            self._log.warning(
                "UIDVALIDITY changed for '%s': was %d, now %d. Resetting last_uid=0. "
                "Deduplication will prevent Gmail duplicates.",
                folder,
                stored_validity,
                uidvalidity,
            )
            state_mod.set_uidvalidity(self._state, self._account.id, folder, uidvalidity)
            last_uid = 0
            state_mod.set_last_uid(self._state, self._account.id, folder, 0)

        # --- Search new UIDs ---
        self._log.debug("Searching UIDs > %d in '%s'", last_uid, folder)
        new_uids = wp.search_new_uids(last_uid)

        if not new_uids:
            self._log.debug("No new messages in '%s'", folder)
            return

        self._log.info(
            "%d new message(s) found in '%s': %s",
            len(new_uids),
            folder,
            new_uids,
        )

        # --- Process each UID in ascending order ---
        for uid in sorted(new_uids):
            try:
                self._process_message(wp, uid, folder, gmail_target)
            except (WPConnectionError, WPTransientError) as exc:
                self._log.error(
                    "Transient error on uid=%d in '%s': %s. Stopping batch.",
                    uid,
                    folder,
                    exc,
                )
                self._stats["errors"] += 1
                break  # stop batch on connection/transient issues
            except Exception as exc:
                self._log.exception(
                    "Unexpected error processing uid=%d in '%s'. Skipping.",
                    uid,
                    folder,
                )
                self._stats["errors"] += 1
                # Continue with next message (per-message isolation)

    # ------------------------------------------------------------------
    # Per-message pipeline
    # ------------------------------------------------------------------

    def _process_message(self, wp: WPMailbox, uid: int, folder: str, gmail_target: str) -> None:
        """
        Execute the fail-safe per-message pipeline for a single UID.

        Steps 2 and 4 are skipped if ``dry_run`` is enabled.
        """
        # Step 1: Fetch RFC822
        raw = wp.fetch_rfc822(uid)

        # Size guard
        if len(raw) > self._app.max_message_bytes:
            self._log.warning(
                "uid=%d: message size %d exceeds max_message_bytes=%d. Skipping.",
                uid,
                len(raw),
                self._app.max_message_bytes,
            )
            self._stats["skipped"] += 1
            # Still advance last_uid so we don't retry this oversized message forever
            state_mod.set_last_uid(self._state, self._account.id, folder, uid)
            return

        # Extract Message-ID for dedupe / logging
        msg_id = _extract_message_id(raw)
        self._log.debug("uid=%d msgid=%s size=%d", uid, msg_id, len(raw))

        # Dedupe check (optional, config-gated)
        if self._app.dedupe_by_message_id and msg_id:
            if self._gmail.message_exists(msg_id):
                self._log.info(
                    "uid=%d msgid=%s already exists in Gmail. Skipping delivery.",
                    uid,
                    msg_id,
                )
                self._stats["skipped"] += 1
                # Move to trash anyway (it was already delivered in a previous run)
                if not self._app.dry_run:
                    wp.move_to_trash(uid, self._app.wp.trash_mailbox)
                state_mod.set_last_uid(self._state, self._account.id, folder, uid)
                return

        # Step 2: Deliver to Gmail INBOX, then optionally label
        label = gmail_target if gmail_target.upper() != "INBOX" else None

        if self._app.dry_run:
            inbox_target = "INBOX"
            self._log.info(
                "[DRY-RUN] uid=%d: would deliver to %s and label as '%s' (msgid=%s)",
                uid, inbox_target, label or "(none)", msg_id
            )
            result = DeliveryResult(ok=True, message_id=msg_id)
        else:
            result = self._gmail.deliver(raw, mailbox="INBOX")

        if not result.ok:
            self._log.error(
                "uid=%d: Gmail delivery failed: %s", uid, result.error
            )
            self._stats["errors"] += 1
            return  # Do NOT advance last_uid — retry next cycle

        self._log.info("uid=%d delivered to INBOX (msgid=%s)", uid, msg_id)

        # Apply label if target is not INBOX
        if label and not self._app.dry_run:
            try:
                # Use Message-ID if available, fall back to APPENDUID
                self._gmail.add_label(msg_id, label, uid=result.uid)
                self._log.info("uid=%d labeled as '%s'", uid, label)
            except Exception as exc:
                self._log.warning(
                    "uid=%d: failed to label as '%s': %s", uid, label, exc
                )

        # Step 3: Delivery verified — move to Trash (skip for Spam folder)
        if folder.upper() == "SPAM":
            self._log.info("uid=%d left in WP Spam folder (as configured)", uid)
        elif self._app.dry_run:
            self._log.info(
                "[DRY-RUN] uid=%d: would move to WP Trash '%s'",
                uid,
                self._app.wp.trash_mailbox,
            )
        else:
            wp.move_to_trash(uid, self._app.wp.trash_mailbox)
            self._log.info("uid=%d moved to %s", uid, self._app.wp.trash_mailbox)

        # Optional: mark as \Seen
        if self._app.wp.mark_seen_after_copy:
            try:
                wp.mark_seen(uid)
            except Exception as exc:
                self._log.warning("uid=%d: mark_seen failed (non-fatal): %s", uid, exc)

        # Step 4: Persist last_uid
        state_mod.set_last_uid(self._state, self._account.id, folder, uid)

        self._stats["copied"] += 1

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _gmail_mailbox_for(self, folder: str) -> str:
        """
        Determine the Gmail IMAP mailbox/label for a given WP folder.

        Rules:
          - "Spam" → ``[Gmail]/Spam`` (Gmail's built-in spam folder)
          - All other folders → ``self._account.append_mailbox`` (e.g. "WP.PL/account1@wp.pl")
        """
        if folder.upper() == "SPAM":
            return "[Gmail]/Spam"
        return self._account.append_mailbox

    def _validate_trash_folder(self, wp: WPMailbox) -> None:
        """Verify the trash folder exists, fail fast if not."""
        folders = wp.list_folders()
        self._log.info("Available WP folders: %s", folders)
        trash = self._app.wp.trash_mailbox
        if trash not in folders:
            self._log.error(
                "Trash folder '%s' not found on WP. Available folders: %s",
                trash,
                folders,
            )
            raise WPPermanentError(
                f"Trash folder '{trash}' not found. Available: {folders}. "
                "Update wp.trash_mailbox in config.yaml."
            )

    def _report_status(self, status: str, error: Optional[str] = None) -> None:
        """Update the shared status registry for the web panel."""
        if self._status:
            self._status.update(
                self._account.id,
                status=status,
                last_error=error,
                stats=self._stats.copy(),
            )


# ---------------------------------------------------------------------------
# Status registry (for web panel)
# ---------------------------------------------------------------------------


class StatusRegistry:
    """
    Thread-safe registry for per-account status, consumed by the web panel.

    Usage::

        registry = StatusRegistry(cumulative_stats={...})
        worker = AccountWorker(..., status_registry=registry)
    """

    def __init__(self, cumulative_stats: Optional[Dict[str, Dict[str, int]]] = None):
        import threading

        self._lock = threading.Lock()
        self._accounts: Dict[str, Dict[str, Any]] = {}
        # Initialize cumulative totals from persisted state
        if cumulative_stats:
            for acc_id, stats in cumulative_stats.items():
                self._accounts[acc_id] = {
                    "status": "idle",
                    "last_sync": None,
                    "total_copied": stats.get("copied", 0),
                    "total_errors": stats.get("errors", 0),
                    "last_error": None,
                }

    def update(
        self,
        account_id: str,
        status: str,
        last_error: Optional[str] = None,
        stats: Optional[Dict[str, int]] = None,
    ) -> None:
        with self._lock:
            entry = self._accounts.setdefault(
                account_id,
                {
                    "status": "unknown",
                    "last_sync": None,
                    "total_copied": 0,
                    "total_errors": 0,
                    "last_error": None,
                },
            )
            entry["status"] = status
            entry["last_sync"] = time.time()
            if last_error:
                entry["last_error"] = last_error
            if stats:
                entry["total_copied"] = entry.get("total_copied", 0) + stats.get(
                    "copied", 0
                )
                entry["total_errors"] = entry.get("total_errors", 0) + stats.get(
                    "errors", 0
                )
                entry["last_run"] = {
                    "copied": stats.get("copied", 0),
                    "errors": stats.get("errors", 0),
                }
                # Keep last 5 runs
                history = entry.setdefault("run_history", [])
                history.append(entry["last_run"])
                if len(history) > 5:
                    history.pop(0)

    def snapshot(self) -> Dict[str, Any]:
        """Return a copy of the current status for all accounts."""
        with self._lock:
            return dict(self._accounts)



