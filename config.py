"""
MailBridge — configuration loading and validation.

Reads config.yaml and passwords.yaml, merges secrets into the configuration
dataclasses, and validates all required fields.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RetryConfig:
    max_attempts: int = 5
    base_delay: int = 5          # seconds
    max_delay: int = 300         # seconds


@dataclass
class GmailApiConfig:
    credentials_file: str = "credentials.json"
    token_file: str = "token.json"


@dataclass
class GmailConfig:
    email: str = ""
    method: str = "append"       # "append" | "api"
    append_mailbox: str = "INBOX"
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    api: GmailApiConfig = field(default_factory=GmailApiConfig)
    app_password: str = ""       # merged from passwords.yaml


@dataclass
class WpConfig:
    imap_host: str = "imap.wp.pl"
    imap_port: int = 993
    source_mailbox: str = "INBOX"
    trash_mailbox: str = "Trash"
    mark_seen_after_copy: bool = False
    extra_folders: List[str] = field(default_factory=list)
    sync_all_folders: bool = False      # auto-discover all folders (except excluded)
    exclude_folders: List[str] = field(default_factory=lambda: ["Trash"])


@dataclass
class AccountConfig:
    id: str = ""
    email: str = ""
    password: str = ""           # merged from passwords.yaml
    append_mailbox: str = ""     # per-account Gmail folder; empty = use global
    folders: List[str] = field(default_factory=list)


@dataclass
class AppConfig:
    poll_interval: int = 120
    max_concurrency: int = 12
    connect_timeout: int = 30
    dry_run: bool = False
    dedupe_by_message_id: bool = True
    max_message_bytes: int = 52_428_800  # 50 MB
    initial_import: bool = False
    retry: RetryConfig = field(default_factory=RetryConfig)
    gmail: GmailConfig = field(default_factory=GmailConfig)
    wp: WpConfig = field(default_factory=WpConfig)
    accounts: List[AccountConfig] = field(default_factory=list)


# ---------------------------------------------------------------------------
# YAML loaders
# ---------------------------------------------------------------------------

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge override into base dict. override wins on conflict."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_yaml(path: str) -> Dict[str, Any]:
    """Load a YAML file, returning an empty dict if the file does not exist."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_passwords(path: str) -> Dict[str, Any]:
    """
    Load passwords.yaml and warn if permissions are looser than 600.
    """
    data = load_yaml(path)
    if os.path.exists(path):
        st = os.stat(path)
        if st.st_mode & (stat.S_IRGRP | stat.S_IROTH):
            logging.getLogger("config").warning(
                "passwords.yaml has overly permissive permissions (%o). "
                "Run: chmod 600 %s",
                st.st_mode & 0o777,
                path,
            )
    return data


def _parse_retry(raw: Optional[Dict[str, Any]]) -> RetryConfig:
    if not raw:
        return RetryConfig()
    return RetryConfig(
        max_attempts=int(raw.get("max_attempts", 5)),
        base_delay=int(raw.get("base_delay", 5)),
        max_delay=int(raw.get("max_delay", 300)),
    )


def _parse_gmail(raw: Dict[str, Any], passwords: Dict[str, Any]) -> GmailConfig:
    gmail_raw = raw.get("gmail", {})
    api_raw = gmail_raw.get("api", {})
    return GmailConfig(
        email=gmail_raw.get("email", ""),
        method=gmail_raw.get("method", "append"),
        append_mailbox=gmail_raw.get("append_mailbox", "INBOX"),
        imap_host=gmail_raw.get("imap_host", "imap.gmail.com"),
        imap_port=int(gmail_raw.get("imap_port", 993)),
        api=GmailApiConfig(
            credentials_file=api_raw.get("credentials_file", "credentials.json"),
            token_file=api_raw.get("token_file", "token.json"),
        ),
        app_password=passwords.get("gmail", {}).get("app_password", ""),
    )


def _parse_wp(raw: Dict[str, Any]) -> WpConfig:
    wp_raw = raw.get("wp", {})
    return WpConfig(
        imap_host=wp_raw.get("imap_host", "imap.wp.pl"),
        imap_port=int(wp_raw.get("imap_port", 993)),
        source_mailbox=wp_raw.get("source_mailbox", "INBOX"),
        trash_mailbox=wp_raw.get("trash_mailbox", "Trash"),
        mark_seen_after_copy=bool(wp_raw.get("mark_seen_after_copy", False)),
        extra_folders=wp_raw.get("extra_folders", []) or [],
        sync_all_folders=bool(wp_raw.get("sync_all_folders", False)),
        exclude_folders=wp_raw.get("exclude_folders", ["Trash"]) or [],
    )


def _parse_accounts(
    raw: Dict[str, Any], passwords: Dict[str, Any], wp_config: WpConfig
) -> List[AccountConfig]:
    accounts: List[AccountConfig] = []
    pwd_accounts = passwords.get("accounts", {})
    for entry in raw.get("accounts", []):
        acc_id = entry["id"]
        email = entry["email"]
        password = pwd_accounts.get(acc_id, "")
        folders = [wp_config.source_mailbox] + wp_config.extra_folders
        # Default subfolder: "WP.PL/<email>" e.g. "WP.PL/konto1@wp.pl"
        am = entry.get("append_mailbox", "")
        if not am:
            am = f"WP.PL/{email}"
        accounts.append(
            AccountConfig(
                id=acc_id,
                email=email,
                password=password,
                append_mailbox=am,
                folders=folders,
            )
        )
    return accounts


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(
    config_path: str,
    passwords_path: str,
) -> AppConfig:
    """Load and validate the full application configuration."""
    raw = load_yaml(config_path)
    passwords = load_passwords(passwords_path)

    wp = _parse_wp(raw)
    accounts = _parse_accounts(raw, passwords, wp)

    # Build config
    config = AppConfig(
        poll_interval=int(raw.get("poll_interval", 120)),
        max_concurrency=int(raw.get("max_concurrency", 12)),
        connect_timeout=int(raw.get("connect_timeout", 30)),
        dry_run=bool(raw.get("dry_run", False)),
        dedupe_by_message_id=bool(raw.get("dedupe_by_message_id", True)),
        max_message_bytes=int(raw.get("max_message_bytes", 52_428_800)),
        initial_import=bool(raw.get("initial_import", False)),
        retry=_parse_retry(raw.get("retry")),
        gmail=_parse_gmail(raw, passwords),
        wp=wp,
        accounts=accounts,
    )

    # --- Validation ---
    errors: List[str] = []

    if not config.gmail.email:
        errors.append("gmail.email is required in config.yaml")

    if config.gmail.method == "append" and not config.gmail.app_password:
        errors.append(
            "gmail.app_password is required in passwords.yaml when gmail.method=append"
        )

    if config.gmail.method not in ("append", "api"):
        errors.append('gmail.method must be "append" or "api"')

    if config.poll_interval < 1:
        errors.append("poll_interval must be > 0")

    for acc in config.accounts:
        if not acc.email:
            errors.append(f"Account '{acc.id}': email is required")
        if not acc.password:
            errors.append(
                f"Account '{acc.id}': password not found in passwords.yaml"
            )

    if errors:
        msg = "Configuration errors:\n  " + "\n  ".join(errors)
        raise ValueError(msg)

    return config
