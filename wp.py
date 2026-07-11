"""
MailBridge — WP.pl IMAP client.

Provides `WPMailbox`, a context-managed IMAP client for a single wp.pl account.
Handles connect, select, incremental UID search, raw message fetch, and
move-to-trash with automatic MOVE vs COPY+EXPUNGE fallback.
"""

from __future__ import annotations

import imaplib
import logging
import ssl
import time
from typing import List, Optional, Tuple

from config import RetryConfig

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WPError(Exception):
    """Base exception for WP mailbox operations."""


class WPConnectionError(WPError):
    """Raised when a connection cannot be established or is lost."""


class WPTransientError(WPError):
    """Raised for temporary failures that should be retried."""


class WPPermanentError(WPError):
    """Raised for non-retryable failures."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_uidvalidity(conn: imaplib.IMAP4_SSL) -> Optional[int]:
    """
    Extract UIDVALIDITY from the connection's internal response buffer.
    Uses ``conn.response('UIDVALIDITY')`` which was populated during SELECT.
    """
    try:
        result = conn.response('UIDVALIDITY')
        if result and len(result) > 1 and result[1]:
            return int(result[1][0])
    except (ValueError, TypeError, IndexError):
        pass
    # Fallback: check untagged_responses['OK'] for [UIDVALIDITY ...]
    try:
        for ok_line in conn.untagged_responses.get('OK', []):
            text = ok_line.decode('utf-8', errors='replace') if isinstance(ok_line, bytes) else str(ok_line)
            if 'UIDVALIDITY' in text:
                val = text.split('UIDVALIDITY')[1].split(']')[0].strip()
                return int(val)
    except (ValueError, TypeError, IndexError):
        pass
    return None


def _parse_uidnext(conn: imaplib.IMAP4_SSL) -> Optional[int]:
    """Extract UIDNEXT from the connection's internal response buffer."""
    try:
        result = conn.response('UIDNEXT')
        if result and len(result) > 1 and result[1]:
            return int(result[1][0])
    except (ValueError, TypeError, IndexError):
        pass
    try:
        for ok_line in conn.untagged_responses.get('OK', []):
            text = ok_line.decode('utf-8', errors='replace') if isinstance(ok_line, bytes) else str(ok_line)
            if 'UIDNEXT' in text:
                val = text.split('UIDNEXT')[1].split(']')[0].strip()
                return int(val)
    except (ValueError, TypeError, IndexError):
        pass
    return None


def _parse_uid_list(response_lines) -> List[int]:
    """
    Parse SEARCH response into a list of integer UIDs.
    IMAP SEARCH returns space-separated UIDs in one or more lines.
    """
    uids: List[int] = []
    for line in response_lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        # Only process lines that look like search results (not status)
        if line.startswith("* SEARCH"):
            parts = line.split()
            for part in parts[2:]:
                try:
                    uids.append(int(part))
                except ValueError:
                    pass
    return uids


# ---------------------------------------------------------------------------
# WPMailbox
# ---------------------------------------------------------------------------


class WPMailbox:
    """
    IMAP client for a single wp.pl mailbox.

    Usage::

        with WPMailbox(host, port, email, password, timeout, retry_config) as mb:
            uidv, uidnext = mb.select("INBOX")
            new_uids = mb.search_new_uids(last_uid)
            raw = mb.fetch_rfc822(new_uids[0])
            mb.move_to_trash(new_uids[0], "Trash")
    """

    def __init__(
        self,
        host: str,
        port: int,
        email: str,
        password: str,
        timeout: int = 30,
        retry_config: Optional[RetryConfig] = None,
    ):
        self._host = host
        self._port = port
        self._email = email
        self._password = password
        self._timeout = timeout
        self._retry = retry_config or RetryConfig()
        self._conn: Optional[imaplib.IMAP4_SSL] = None
        self._log = logging.getLogger("wp")

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "WPMailbox":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.logout()
        return False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Establish IMAP4_SSL connection and login with retry."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                ctx = ssl.create_default_context()
                self._conn = imaplib.IMAP4_SSL(
                    self._host, self._port, timeout=self._timeout, ssl_context=ctx
                )
                self._conn.login(self._email, self._password)
                self._log.debug(
                    "Connected to %s:%d as %s", self._host, self._port, self._email
                )
                return
            except imaplib.IMAP4.error as exc:
                last_exc = exc
                msg = str(exc)
                if "authentication" in msg.lower() or "login" in msg.lower():
                    raise WPConnectionError(
                        f"Authentication failed for {self._email}: {exc}"
                    ) from exc
                self._log.warning(
                    "Connection attempt %d/%d failed: %s",
                    attempt,
                    self._retry.max_attempts,
                    exc,
                )
            except (OSError, ssl.SSLError, imaplib.IMAP4.abort) as exc:
                last_exc = exc
                self._log.warning(
                    "Connection attempt %d/%d failed: %s",
                    attempt,
                    self._retry.max_attempts,
                    exc,
                )
            # Clean up failed connection
            try:
                if self._conn:
                    self._conn.shutdown()
            except Exception:
                pass
            self._conn = None
            if attempt < self._retry.max_attempts:
                delay = min(
                    self._retry.base_delay * (2 ** (attempt - 1)),
                    self._retry.max_delay,
                )
                time.sleep(delay)

        raise WPConnectionError(
            f"Could not connect to {self._host}:{self._port} after "
            f"{self._retry.max_attempts} attempts"
        ) from last_exc

    def logout(self) -> None:
        """Gracefully close the IMAP connection."""
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def noop(self) -> None:
        """Send NOOP to keep the connection alive and check health."""
        if not self._conn:
            raise WPConnectionError("Not connected")
        try:
            self._conn.noop()
        except (imaplib.IMAP4.abort, OSError) as exc:
            raise WPConnectionError(f"NOOP failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Mailbox selection
    # ------------------------------------------------------------------

    def select(
        self, mailbox: str = "INBOX", readonly: bool = False
    ) -> Tuple[int, Optional[int]]:
        """
        Select a mailbox. Returns (uidvalidity, uidnext).

        uidnext may be None if the server doesn't report it.
        """
        if not self._conn:
            raise WPConnectionError("Not connected")
        try:
            typ, data = self._conn.select(f'"{mailbox}"', readonly=readonly)
            if typ != "OK":
                raise WPTransientError(f"SELECT {mailbox} failed: {typ!r} {data!r}")
            uidvalidity = _parse_uidvalidity(self._conn)
            uidnext = _parse_uidnext(self._conn)
            if uidvalidity is None:
                raise WPTransientError(
                    f"Could not determine UIDVALIDITY for {mailbox}"
                )
            return uidvalidity, uidnext
        except (imaplib.IMAP4.abort, OSError) as exc:
            raise WPConnectionError(f"Connection lost during SELECT: {exc}") from exc
        except WPError:
            raise
        except imaplib.IMAP4.error as exc:
            raise WPTransientError(f"SELECT {mailbox} error: {exc}") from exc

    def list_folders(self) -> List[str]:
        """Return list of available mailbox folder names."""
        if not self._conn:
            raise WPConnectionError("Not connected")
        try:
            typ, data = self._conn.list()
            if typ != "OK":
                raise WPTransientError(f"LIST failed: {typ!r}")
            folders: List[str] = []
            for line in data:
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                # IMAP LIST response format: * LIST (...) "/" "folder_name"
                parts = line.split('"')
                if len(parts) >= 2:
                    folder = parts[-2] if parts[-1] == "" else parts[-1]
                    if folder:
                        folders.append(folder)
            return folders
        except (imaplib.IMAP4.abort, OSError) as exc:
            raise WPConnectionError(f"Connection lost during LIST: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            raise WPTransientError(f"LIST error: {exc}") from exc

    # ------------------------------------------------------------------
    # UID search
    # ------------------------------------------------------------------

    def search_new_uids(self, last_uid: int) -> List[int]:
        """
        Search for UIDs greater than *last_uid*.

        Uses ``UID SEARCH UID (last_uid+1):*``.
        """
        if not self._conn:
            raise WPConnectionError("Not connected")
        try:
            search_from = max(1, last_uid + 1)  # IMAP UIDs start at 1
            criteria = f"UID {search_from}:*"
            typ, data = self._conn.uid("SEARCH", None, criteria)
            if typ != "OK":
                raise WPTransientError(f"UID SEARCH failed: {typ!r} {data!r}")
            return _parse_uid_list(data)
        except (imaplib.IMAP4.abort, OSError) as exc:
            raise WPConnectionError(
                f"Connection lost during SEARCH: {exc}"
            ) from exc
        except WPError:
            raise
        except imaplib.IMAP4.error as exc:
            raise WPTransientError(f"SEARCH error: {exc}") from exc

    def search_by_message_id(self, message_id: str) -> List[int]:
        """Search for messages with a given Message-ID header. Returns list of UIDs."""
        if not self._conn:
            raise WPConnectionError("Not connected")
        try:
            criteria = f'HEADER Message-ID "{message_id}"'
            typ, data = self._conn.uid("SEARCH", None, criteria)
            if typ != "OK":
                raise WPTransientError(
                    f"UID SEARCH (Message-ID) failed: {typ!r} {data!r}"
                )
            return _parse_uid_list(data)
        except (imaplib.IMAP4.abort, OSError) as exc:
            raise WPConnectionError(
                f"Connection lost during SEARCH: {exc}"
            ) from exc
        except WPError:
            raise
        except imaplib.IMAP4.error as exc:
            raise WPTransientError(f"SEARCH error: {exc}") from exc

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------

    def fetch_rfc822(self, uid: int) -> bytes:
        """
        Fetch the full RFC 822 message for *uid*.

        Uses ``UID FETCH uid (BODY.PEEK[])`` so the message is NOT marked \\Seen.
        """
        if not self._conn:
            raise WPConnectionError("Not connected")
        try:
            typ, data = self._conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if typ != "OK":
                raise WPTransientError(
                    f"UID FETCH {uid} failed: {typ!r}"
                )
            # IMAP returns: [(b'... FETCH (UID X BODY.PEEK[] {N}', raw_data), b')']
            # The raw message is typically in the second element of the first tuple
            # followed by a closing paren.
            raw = b""
            found_body = False
            for item in data:
                if isinstance(item, tuple):
                    # The tuple contains (header_line, body_data)
                    for sub in item:
                        if isinstance(sub, bytes):
                            if b"BODY.PEEK[]" in sub or found_body:
                                found_body = True
                                raw += sub
                elif isinstance(item, bytes):
                    if found_body:
                        raw += item

            if not raw:
                # Fallback: concatenate everything after the FETCH marker
                for item in data:
                    if isinstance(item, tuple):
                        raw = item[1]
                        if isinstance(raw, bytes):
                            break

            if not raw:
                raise WPTransientError(f"UID {uid}: could not extract message body")

            return raw
        except (imaplib.IMAP4.abort, OSError) as exc:
            raise WPConnectionError(
                f"Connection lost during FETCH {uid}: {exc}"
            ) from exc
        except WPError:
            raise
        except imaplib.IMAP4.error as exc:
            raise WPTransientError(f"FETCH {uid} error: {exc}") from exc

    # ------------------------------------------------------------------
    # Move / trash
    # ------------------------------------------------------------------

    def move_to_trash(self, uid: int, trash_mailbox: str) -> None:
        """
        Move message *uid* to *trash_mailbox*.

        Prefers ``UID MOVE`` if the server advertises the ``MOVE`` capability;
        otherwise falls back to ``UID COPY`` + ``UID STORE +FLAGS (\\Deleted)``
        + ``EXPUNGE``.
        """
        if not self._conn:
            raise WPConnectionError("Not connected")
        try:
            capabilities = self._conn.capability()
            cap_str = (
                capabilities[0].decode("utf-8", errors="replace")
                if capabilities and capabilities[0]
                else ""
            )
            if "MOVE" in cap_str:
                self._uid_move(uid, trash_mailbox)
            else:
                self._uid_copy_delete(uid, trash_mailbox)
        except (imaplib.IMAP4.abort, OSError) as exc:
            raise WPConnectionError(
                f"Connection lost during move_to_trash {uid}: {exc}"
            ) from exc
        except WPError:
            raise
        except imaplib.IMAP4.error as exc:
            raise WPTransientError(
                f"move_to_trash {uid} error: {exc}"
            ) from exc

    def _uid_move(self, uid: int, trash_mailbox: str) -> None:
        """Use the MOVE extension."""
        typ, data = self._conn.uid("MOVE", str(uid), f'"{trash_mailbox}"')
        if typ != "OK":
            raise WPTransientError(f"UID MOVE {uid} -> {trash_mailbox} failed: {typ!r}")

    def _uid_copy_delete(self, uid: int, trash_mailbox: str) -> None:
        """Fallback: COPY → STORE +FLAGS (\\Deleted) → EXPUNGE."""
        typ, data = self._conn.uid("COPY", str(uid), f'"{trash_mailbox}"')
        if typ != "OK":
            raise WPTransientError(
                f"UID COPY {uid} -> {trash_mailbox} failed: {typ!r}"
            )
        typ, data = self._conn.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
        if typ != "OK":
            raise WPTransientError(f"UID STORE \\Deleted {uid} failed: {typ!r}")
        self._conn.expunge()

    # ------------------------------------------------------------------
    # Mark seen (optional)
    # ------------------------------------------------------------------

    def mark_seen(self, uid: int) -> None:
        """Mark a message as \\Seen."""
        if not self._conn:
            raise WPConnectionError("Not connected")
        try:
            typ, data = self._conn.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
            if typ != "OK":
                raise WPTransientError(f"UID STORE \\Seen {uid} failed: {typ!r}")
        except (imaplib.IMAP4.abort, OSError) as exc:
            raise WPConnectionError(
                f"Connection lost during mark_seen {uid}: {exc}"
            ) from exc
        except WPError:
            raise
        except imaplib.IMAP4.error as exc:
            raise WPTransientError(f"mark_seen {uid} error: {exc}") from exc
