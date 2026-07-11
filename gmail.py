"""
MailBridge — Gmail delivery backends.

Provides a common ``GmailDelivery`` interface with two implementations:

* ``AppendBackend`` — delivers via IMAP ``APPEND`` using a Gmail App Password.
* ``ApiBackend`` — delivers via Gmail API ``users.messages.import`` (OAuth).

The factory ``build_gmail_delivery`` selects the backend based on config.
"""

from __future__ import annotations

import email
import imaplib
import logging
import ssl
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from config import GmailConfig, RetryConfig


def _parse_uid_list(response_lines) -> list[int]:
    """Parse IMAP SEARCH response into a list of integer UIDs."""
    uids: list[int] = []
    for line in response_lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        clean = line.replace("* SEARCH", "").strip()
        for part in clean.split():
            try:
                uids.append(int(part))
            except ValueError:
                pass
    return uids


@dataclass
class DeliveryResult:
    ok: bool
    message_id: Optional[str] = None
    uid: Optional[int] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class GmailDelivery(ABC):
    """Interface for delivering a raw RFC822 message into Gmail."""

    @abstractmethod
    def deliver(
        self,
        raw_rfc822: bytes,
        internaldate: Optional[str] = None,
        flags: Optional[tuple] = None,
        mailbox: Optional[str] = None,
    ) -> DeliveryResult:
        """
        Deliver a message to Gmail.

        :param raw_rfc822: the complete RFC 822 message as bytes
        :param internaldate: optional IMAP INTERNALDATE string
        :param flags: optional IMAP flags tuple, e.g. ('\\Seen',)
        :param mailbox: optional target mailbox override (e.g. per-account label)
        :return: DeliveryResult with success status
        """

    @abstractmethod
    def message_exists(self, message_id: str) -> bool:
        """
        Check whether a message with the given Message-ID already exists
        in the target Gmail mailbox. Used for idempotency/deduplication.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any resources (connections, sessions)."""

    @abstractmethod
    def add_label(self, message_id: str, label: str, uid: Optional[int] = None) -> None:
        """Apply a Gmail label to an already-delivered message by Message-ID."""


# ---------------------------------------------------------------------------
# IMAP APPEND backend
# ---------------------------------------------------------------------------


class AppendBackend(GmailDelivery):
    """
    Delivers messages to Gmail via IMAP ``APPEND``.

    Uses a dedicated IMAP connection (one per worker for thread safety).
    """

    def __init__(
        self,
        config: GmailConfig,
        retry_config: Optional[RetryConfig] = None,
    ):
        self._config = config
        self._retry = retry_config or RetryConfig()
        self._conn: Optional[imaplib.IMAP4_SSL] = None
        self._log = logging.getLogger("gmail.append")

    def _ensure_connected(self) -> imaplib.IMAP4_SSL:
        """Return a connected, authenticated IMAP session."""
        if self._conn is not None:
            try:
                self._conn.noop()
                return self._conn
            except Exception:
                self._conn = None  # reconnect below

        last_exc: Optional[Exception] = None
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                ctx = ssl.create_default_context()
                self._conn = imaplib.IMAP4_SSL(
                    self._config.imap_host,
                    self._config.imap_port,
                    ssl_context=ctx,
                )
                self._conn.login(self._config.email, self._config.app_password)
                self._log.debug(
                    "Connected to Gmail IMAP as %s", self._config.email
                )
                return self._conn
            except imaplib.IMAP4.error as exc:
                last_exc = exc
                msg = str(exc).lower()
                if "authentication" in msg or "login" in msg:
                    raise RuntimeError(
                        f"Gmail authentication failed for {self._config.email}. "
                        "Check your App Password."
                    ) from exc
            except (OSError, ssl.SSLError) as exc:
                last_exc = exc

            self._log.warning(
                "Gmail connect attempt %d/%d failed: %s",
                attempt,
                self._retry.max_attempts,
                last_exc,
            )
            self._conn = None
            if attempt < self._retry.max_attempts:
                delay = min(
                    self._retry.base_delay * (2 ** (attempt - 1)),
                    self._retry.max_delay,
                )
                time.sleep(delay)

        raise RuntimeError(
            f"Could not connect to Gmail IMAP after "
            f"{self._retry.max_attempts} attempts"
        ) from last_exc

    def deliver(
        self,
        raw_rfc822: bytes,
        internaldate: Optional[str] = None,
        flags: Optional[tuple] = None,
        mailbox: Optional[str] = None,
    ) -> DeliveryResult:
        try:
            conn = self._ensure_connected()
        except Exception as exc:
            return DeliveryResult(ok=False, error=str(exc))

        # Use per-account mailbox if given, else fall back to config default
        target = mailbox or self._config.append_mailbox

        # Build IMAP APPEND arguments
        # signature: append(mailbox, flags, date_time, message)
        flag_str = " ".join(flags) if flags else ""
        if not flag_str.startswith("("):
            flag_str = f"({flag_str})" if flags else "()"

        try:
            # imaplib's append() accepts None for date_time (uses current time)
            typ, data = conn.append(target, flag_str, internaldate, raw_rfc822)  # type: ignore[arg-type]
            if typ == "OK":
                # Try to extract Message-ID from raw for logging
                msg_id = _extract_message_id(raw_rfc822)
                # Try to extract APPENDUID from response for labeling fallback
                uid = _parse_appended_uid(data)
                return DeliveryResult(ok=True, message_id=msg_id, uid=uid)
            else:
                return DeliveryResult(
                    ok=False, error=f"APPEND failed: {typ!r} {data!r}"
                )
        except (imaplib.IMAP4.abort, OSError, imaplib.IMAP4.error) as exc:
            self._conn = None  # force reconnect next time
            return DeliveryResult(ok=False, error=str(exc))

    def message_exists(self, message_id: str) -> bool:
        """Search Gmail mailbox for a message by Message-ID header."""
        try:
            conn = self._ensure_connected()
        except Exception:
            return False  # assume not exists on connection error

        try:
            # Select the mailbox first
            conn.select(self._config.append_mailbox, readonly=True)
            criteria = f'HEADER Message-ID "{message_id}"'
            typ, data = conn.uid("SEARCH", None, criteria)
            if typ != "OK":
                return False
            for line in data:
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                # imaplib strips * SEARCH prefix - just check if there's content
                clean = line.replace("* SEARCH", "").strip()
                if clean and any(c.isdigit() for c in clean):
                    return True
            return False
        except imaplib.IMAP4.error:
            return False

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def add_label(self, message_id: str, label: str, uid: Optional[int] = None) -> None:
        """
        Apply a Gmail label to an already-delivered message.

        Searches INBOX for the message by Message-ID, then copies it to
        the target label folder (which adds the label in Gmail).

        If *uid* is provided (from an APPENDUID response), it's used
        directly as a fallback when the Message-ID is missing or the
        search fails.
        """
        conn = self._ensure_connected()
        try:
            conn.select("INBOX", readonly=False)
            found_uid = None

            # Try to find by Message-ID first
            if message_id:
                criteria = f'HEADER Message-ID "{message_id}"'
                typ, data = conn.uid("SEARCH", None, criteria)
                if typ == "OK":
                    uids = _parse_uid_list(data)
                    if uids:
                        found_uid = uids[0]

            # Fallback: use the APPENDUID if provided
            if found_uid is None and uid is not None:
                found_uid = uid

            if found_uid is None:
                self._log.warning(
                    "add_label: could not locate message %s in INBOX",
                    message_id or "(no Message-ID)",
                )
                return

            # Let imaplib handle quoting — don't add extra quotes
            typ, data = conn.uid("COPY", str(found_uid), label)
            if typ != "OK":
                raise RuntimeError(f"UID COPY to {label} failed: {typ!r}")
        except (imaplib.IMAP4.abort, OSError, imaplib.IMAP4.error) as exc:
            raise RuntimeError(f"add_label failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Gmail API backend (optional)
# ---------------------------------------------------------------------------


class ApiBackend(GmailDelivery):
    """
    Delivers messages via the Gmail API ``users.messages.import`` method.

    Requires OAuth credentials (credentials.json + token.json).
    Only instantiated when ``gmail.method == "api"``.
    """

    def __init__(self, config: GmailConfig):
        self._config = config
        self._service = None
        self._log = logging.getLogger("gmail.api")

    def _get_service(self):
        if self._service:
            return self._service

        try:
            from google.auth.transport.requests import Request  # type: ignore[import-untyped]
            from google.oauth2.credentials import Credentials  # type: ignore[import-untyped]
            from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]
            from googleapiclient.discovery import build  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "google-api-python-client and google-auth-* are required "
                "for Gmail API backend. pip install google-api-python-client "
                "google-auth-httplib2 google-auth-oauthlib"
            ) from exc

        SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
        creds = None

        if self._config.api.token_file:
            try:
                creds = Credentials.from_authorized_user_file(
                    self._config.api.token_file, SCOPES
                )
            except Exception:
                pass

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._config.api.credentials_file, SCOPES
                )
                creds = flow.run_local_server(port=0)
            # Save token
            if self._config.api.token_file:
                with open(self._config.api.token_file, "w") as fh:
                    fh.write(creds.to_json())

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def deliver(
        self,
        raw_rfc822: bytes,
        internaldate: Optional[str] = None,
        flags: Optional[tuple] = None,
        mailbox: Optional[str] = None,
    ) -> DeliveryResult:
        import base64

        try:
            service = self._get_service()
        except Exception as exc:
            return DeliveryResult(ok=False, error=str(exc))

        try:
            body = {
                "raw": base64.urlsafe_b64encode(raw_rfc822).decode("ascii"),
                "neverMarkSpam": True,
                "internalDateSource": "dateHeader",
            }
            result = (
                service.users()
                .messages()
                .import_(userId="me", body=body)
                .execute()
            )
            msg_id = result.get("id")
            return DeliveryResult(ok=True, message_id=msg_id)
        except Exception as exc:
            return DeliveryResult(ok=False, error=str(exc))

    def message_exists(self, message_id: str) -> bool:
        try:
            service = self._get_service()
        except Exception:
            return False

        try:
            result = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    q=f"rfc822msgid:{message_id}",
                    maxResults=1,
                )
                .execute()
            )
            return bool(result.get("messages"))
        except Exception:
            return False

    def close(self) -> None:
        self._service = None

    def add_label(self, message_id: str, label: str, uid: Optional[int] = None) -> None:
        """Apply a Gmail label (not implemented for API backend)."""
        pass  # API import handles labels natively


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_gmail_delivery(
    config: GmailConfig,
    retry_config: Optional[RetryConfig] = None,
) -> GmailDelivery:
    """
    Create the appropriate GmailDelivery backend based on ``config.method``.

    :param config: Gmail delivery configuration
    :param retry_config: retry settings (used by AppendBackend)
    :return: a GmailDelivery instance
    """
    if config.method == "api":
        return ApiBackend(config)
    return AppendBackend(config, retry_config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_message_id(raw_rfc822: bytes) -> Optional[str]:
    """Extract the Message-ID header from a raw RFC822 message."""
    try:
        msg = email.message_from_bytes(raw_rfc822)
        return msg.get("Message-ID")
    except Exception:
        return None


def _parse_appended_uid(data) -> Optional[int]:
    """Extract the UID from an APPEND response like [APPENDUID <val> <uid>]."""
    if not data:
        return None
    for item in data:
        if isinstance(item, bytes):
            text = item.decode("utf-8", errors="replace")
        else:
            text = str(item)
        if "APPENDUID" in text:
            try:
                parts = text.replace("[", "").replace("]", "").split()
                # parts: ['APPENDUID', '<uidvalidity>', '<uid>']
                if len(parts) >= 3:
                    return int(parts[2])
            except (IndexError, ValueError):
                pass
    return None
