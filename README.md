# 📬 MailBridge

**WP.pl → Gmail email sync service for Raspberry Pi 4B**

A production-grade Python service that copies e-mails from multiple wp.pl IMAP
mailboxes into a single Gmail account, then moves the originals on WP to Trash
— but only after Gmail has confirmed receipt.

---

## Features

- ✅ Copy original RFC822 messages (headers, `Message-ID`, `Date`, attachments,
  HTML) from N wp.pl inboxes to one Gmail account
- ✅ **Never lose mail**: delete/trash on WP happens *only after* Gmail confirms
  the message was stored
- ✅ Incremental sync (only new messages), resilient to restarts
- ✅ Robust against network drops, IMAP disconnects, transient errors
- ✅ Handle 12+ accounts concurrently
- ✅ Run 24/7 as a systemd service under a dedicated non-root user
- ✅ Full logging (file + `journalctl`)
- ✅ Optional lightweight web dashboard

---

## Requirements

- Python 3.9+
- Raspberry Pi (or any Linux machine)
- WP.pl accounts with IMAP enabled
- Gmail account with 2FA enabled and an App Password

---

## Quick Start

### 1. Create the dedicated user

```bash
sudo useradd -r -m -d /opt/mailbridge mailbridge
sudo mkdir -p /opt/mailbridge
sudo chown mailbridge:mailbridge /opt/mailbridge
```

### 2. Deploy the code

```bash
sudo cp -r mailbridge/* /opt/mailbridge/
sudo chown -R mailbridge:mailbridge /opt/mailbridge
```

### 3. Set up Python virtual environment

```bash
cd /opt/mailbridge
sudo -u mailbridge python3 -m venv venv
sudo -u mailbridge venv/bin/pip install -r requirements.txt
```

### 4. Configure

```bash
sudo -u mailbridge cp config/config.example.yaml config/config.yaml
sudo -u mailbridge cp config/passwords.example.yaml config/passwords.yaml
sudo -u mailbridge nano config/config.yaml     # fill in accounts
sudo -u mailbridge nano config/passwords.yaml  # fill in passwords
sudo -u mailbridge chmod 600 config/passwords.yaml
```

### 5. Gmail App Password

1. Go to [Google Account Security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** if not already enabled
3. Go to **App Passwords**
4. Generate a new App Password for "Mail" → "Other (custom name)"
5. Copy the 16-character password into `config/passwords.yaml` under `gmail.app_password`

### 6. WP.pl IMAP Setup

1. Log into each wp.pl account
2. Enable IMAP in settings (if not already enabled)
3. Use the account password in `config/passwords.yaml` (or create app passwords if WP supports them)

### 7. Verify WP Trash folder name

Run once to verify the trash folder:

```bash
sudo -u mailbridge /opt/mailbridge/venv/bin/python /opt/mailbridge/bridge.py --once -v
```

Check the log output for "Available WP folders" and confirm that
`wp.trash_mailbox` in `config/config.yaml` matches the actual trash folder name
(likely `"Trash"` for Polish WP.pl).

### 8. Test with dry run

Set `dry_run: true` in `config/config.yaml` and run:

```bash
sudo -u mailbridge /opt/mailbridge/venv/bin/python /opt/mailbridge/bridge.py --once -v
```

This will log all actions without actually delivering or deleting anything.

### 9. Install as a systemd service

```bash
sudo cp /opt/mailbridge/mailbridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mailbridge
```

### 10. Monitor

```bash
# Check status
sudo systemctl status mailbridge

# Follow logs
sudo journalctl -fu mailbridge

# View file logs
sudo tail -f /opt/mailbridge/logs/mailbridge.log

# Restart
sudo systemctl restart mailbridge
```

---

## Configuration Reference

### `config/config.yaml`

| Key | Default | Description |
|-----|---------|-------------|
| `poll_interval` | `120` | Seconds between sync cycles |
| `max_concurrency` | `12` | Max simultaneous account workers |
| `connect_timeout` | `30` | IMAP socket timeout (seconds) |
| `dry_run` | `false` | If true, only log (no delivery/trash) |
| `dedupe_by_message_id` | `true` | Skip delivery if Message-ID already in Gmail |
| `max_message_bytes` | `52428800` | 50 MB max message size |
| `initial_import` | `false` | Import all existing mail on first run |
| `gmail.method` | `append` | `append` (App Password) or `api` (OAuth) |
| `gmail.spam_senders` | `[]` | Force-move imported messages from listed senders to Gmail Spam |
| `gmail.spam_subject_keywords` | `[]` | Force-move imported messages to Gmail Spam if subject contains any keyword |
| `gmail.spam_body_keywords` | `[]` | Force-move imported messages to Gmail Spam if body contains any keyword |
| `wp.trash_mailbox` | `Trash` | WP trash folder name |
| `wp.mark_seen_after_copy` | `false` | Mark messages \\Seen after copy |
| `wp.extra_folders` | `[]` | Additional folders to sync (e.g. `["Sent"]`) |

### Sender blocklist to Gmail Spam

If Gmail account-level filters/blocks do not affect imported messages, use
`gmail.spam_senders` in `config/config.yaml`.

Supported rule formats:

- full address: `bad.sender@example.com`
- domain with @: `@annoying-domain.com`
- bare domain: `spamdomain.net`

Keyword filters:

- `gmail.spam_subject_keywords`: case-insensitive substring match against Subject
- `gmail.spam_body_keywords`: case-insensitive substring match against decoded text/plain and text/html body parts

Example:

```yaml
gmail:
  spam_senders:
    - "bad.sender@example.com"
    - "@annoying-domain.com"
    - "spamdomain.net"
  spam_subject_keywords:
    - "make money"
    - "free gift"
  spam_body_keywords:
    - "limited time offer"
    - "crypto investment"
```

### `config/passwords.yaml` (chmod 600)

```yaml
gmail:
  app_password: "xxxx xxxx xxxx xxxx"
accounts:
  account1: "password1"
  account2: "password2"
```

---

## Web Dashboard (optional)

The web panel is built into the codebase (`webpanel.py`). To enable it, you
can integrate it into `bridge.py` or run it as a separate process.

Once enabled, visit `http://<raspberry-pi-ip>:8080` on your LAN.

⚠️ **Firewall note**: The dashboard binds to `0.0.0.0:8080`. Ensure your
firewall restricts access to your LAN only.

---

## Project Structure

```
mailbridge/
├── bridge.py            # entrypoint: config load, scheduler, worker orchestration
├── config.py            # dataclasses + loaders/validators
├── state.py             # atomic state.json persistence
├── wp.py                # WP.pl IMAP client
├── gmail.py             # Gmail delivery backends (APPEND + API)
├── worker.py            # Per-account sync worker + pipeline
├── logger.py            # Structured logging setup
├── webpanel.py          # Optional HTTP dashboard
├── config/
│   ├── config.yaml          # Non-secret configuration (gitignored)
│   ├── config.example.yaml  # Template
│   ├── passwords.yaml       # Secrets (chmod 600, gitignored)
│   └── passwords.example.yaml
├── state.json           # Runtime state (gitignored)
├── requirements.txt
├── mailbridge.service   # systemd unit
├── .gitignore
├── logs/                # Rotating log files
└── README.md
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Authentication failed" for WP | Check `config/passwords.yaml` account passwords. Ensure IMAP is enabled on wp.pl. |
| "Authentication failed" for Gmail | Verify 2FA is enabled and App Password is correct. |
| "Trash folder 'X' not found" | Update `wp.trash_mailbox` in `config/config.yaml` to match the actual folder name. |
| Messages not appearing in Gmail | Check `gmail.append_mailbox` in `config/config.yaml`. Check Gmail spam folder. |
| Duplicate messages in Gmail | Enable `dedupe_by_message_id: true` in `config/config.yaml`. |
| Service won't start | Run `journalctl -u mailbridge -n 50` for error details. |
| UIDVALIDITY changed warning | Normal if WP rebuilt the mailbox. Deduplication prevents Gmail duplicates. |

---

## Security

- Passwords stored in `config/passwords.yaml` (chmod 600), never logged
- Gmail uses App Password (revocable, IMAP-only) — never your main Google password
- All IMAP connections over TLS (port 993)
- Service runs as dedicated `mailbridge` user (never root)
- systemd hardening directives applied
- Never logs passwords or full message bodies

---

## License

MIT
