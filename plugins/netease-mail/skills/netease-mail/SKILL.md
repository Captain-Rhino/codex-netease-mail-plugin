---
name: netease-mail
description: Manage NetEase Mail, 163.com, 126.com, and yeah.net mailboxes from Codex through the local NetEase Mail MCP plugin. Use when the user asks to summarize, search, read, triage, draft replies, or send email with NetEase Mail or NetEase Mail Master.
---

# NetEase Mail

## Overview

Use this skill to work with NetEase-backed mailboxes through the local `netease-mail` MCP server. The plugin talks to the mail account with IMAP for reading and optional SMTP for sending; it does not automate the NetEase Mail Master desktop app directly.

## Configuration

Before using live mailbox tools, verify configuration with `get_config_status`.

Required for IMAP read access:

- `NETEASE_MAIL_ADDRESS`: mailbox address for the NetEase account
- `NETEASE_MAIL_AUTH_CODE` or `NETEASE_MAIL_IMAP_PASSWORD`: NetEase client authorization code

Optional overrides:

- `NETEASE_MAIL_IMAP_HOST`, `NETEASE_MAIL_IMAP_PORT`
- `NETEASE_MAIL_SMTP_HOST`, `NETEASE_MAIL_SMTP_PORT`
- `NETEASE_MAIL_SMTP_PASSWORD`: SMTP authorization code; falls back to `NETEASE_MAIL_AUTH_CODE`
- `NETEASE_MAIL_ENABLE_SMTP=true`: required before the send tool will send

If configuration is missing, explain which variables are missing. Do not ask for the authorization code in chat unless the user explicitly wants setup help.

## Safe Workflow

1. For mailbox summaries, call `search_emails` first with a narrow date range and a sensible `max_results`.
2. Expand only relevant messages with `read_email`.
3. Treat snippets and search results as summaries, not full-thread truth.
4. Draft replies in chat or call `prepare_draft`; this does not save or send mail.
5. Call `send_email` only after the user explicitly confirms sending. Pass `confirm_send: true`; otherwise the tool refuses to send.
6. Never delete, move, mark read, or otherwise mutate messages; this first version intentionally does not expose those operations.

## Notes

- IMAP folders can vary by account and language. Use `list_mailboxes` when a mailbox name is unclear.
- Search is strongest for recent-message workflows. The server performs date search on IMAP, then filters sender, subject, and text locally.
- The SMTP permission is intentionally separate and disabled by default because sending mail is higher risk than summarizing mail.
