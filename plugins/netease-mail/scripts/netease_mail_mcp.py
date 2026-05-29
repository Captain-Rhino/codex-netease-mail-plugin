#!/usr/bin/env python3
"""Local MCP server for NetEase Mail accounts.

This server intentionally keeps the first version conservative:
- IMAP read/search is enabled when mailbox credentials are configured.
- SMTP sending is disabled unless NETEASE_MAIL_ENABLE_SMTP=true and the tool
  call includes confirm_send=true.
- No mailbox mutation tools are exposed.
"""

from __future__ import annotations

import base64
import datetime as dt
import email
import hashlib
import html
import imaplib
import json
import os
import re
import smtplib
import sys
import traceback
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.policy import default
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable


SERVER_NAME = "netease-mail"
SERVER_VERSION = "0.1.0"
MAX_RESULTS_CAP = 100
SEARCH_SCAN_CAP = 500
PENDING_DRAFTS: dict[str, dict[str, Any]] = {}


class PlainHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


@dataclass(frozen=True)
class MailConfig:
    address: str | None
    imap_password: str | None
    imap_host: str | None
    imap_port: int
    smtp_password: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_enabled: bool

    @property
    def configured_for_imap(self) -> bool:
        return bool(self.address and self.imap_password and self.imap_host)

    @property
    def configured_for_smtp(self) -> bool:
        return bool(self.address and self.smtp_password and self.smtp_host)


def infer_hosts(address: str | None) -> tuple[str | None, str | None]:
    if not address or "@" not in address:
        return None, None
    domain = address.rsplit("@", 1)[1].lower()
    known = {
        "163.com": ("imap.163.com", "smtp.163.com"),
        "126.com": ("imap.126.com", "smtp.126.com"),
        "yeah.net": ("imap.yeah.net", "smtp.yeah.net"),
    }
    if domain in known:
        return known[domain]
    return f"imap.{domain}", f"smtp.{domain}"


def get_config() -> MailConfig:
    address = os.getenv("NETEASE_MAIL_ADDRESS") or os.getenv("NETEASE_MAIL_USER")
    inferred_imap, inferred_smtp = infer_hosts(address)
    auth_code = os.getenv("NETEASE_MAIL_AUTH_CODE")
    imap_password = os.getenv("NETEASE_MAIL_IMAP_PASSWORD") or auth_code
    smtp_password = os.getenv("NETEASE_MAIL_SMTP_PASSWORD") or auth_code
    smtp_enabled = os.getenv("NETEASE_MAIL_ENABLE_SMTP", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return MailConfig(
        address=address,
        imap_password=imap_password,
        imap_host=os.getenv("NETEASE_MAIL_IMAP_HOST") or inferred_imap,
        imap_port=int(os.getenv("NETEASE_MAIL_IMAP_PORT", "993")),
        smtp_password=smtp_password,
        smtp_host=os.getenv("NETEASE_MAIL_SMTP_HOST") or inferred_smtp,
        smtp_port=int(os.getenv("NETEASE_MAIL_SMTP_PORT", "465")),
        smtp_enabled=smtp_enabled,
    )


def require_imap_config(config: MailConfig) -> None:
    missing = []
    if not config.address:
        missing.append("NETEASE_MAIL_ADDRESS")
    if not config.imap_password:
        missing.append("NETEASE_MAIL_AUTH_CODE or NETEASE_MAIL_IMAP_PASSWORD")
    if not config.imap_host:
        missing.append("NETEASE_MAIL_IMAP_HOST")
    if missing:
        raise ValueError("Missing IMAP configuration: " + ", ".join(missing))


def imap_utf7_encode(value: str) -> str:
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        raw = "".join(buf).encode("utf-16-be")
        token = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        out.append("&" + token + "-")
        buf.clear()

    for ch in value:
        code = ord(ch)
        if 0x20 <= code <= 0x7E and ch != "&":
            flush()
            out.append(ch)
        elif ch == "&":
            flush()
            out.append("&-")
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "&":
            out.append(value[index])
            index += 1
            continue
        end = value.find("-", index)
        if end == -1:
            out.append(value[index:])
            break
        token = value[index + 1 : end]
        if token == "":
            out.append("&")
        else:
            token = token.replace(",", "/")
            token += "=" * ((4 - len(token) % 4) % 4)
            try:
                out.append(base64.b64decode(token).decode("utf-16-be"))
            except Exception:
                out.append(value[index : end + 1])
        index = end + 1
    return "".join(out)


def connect_imap(config: MailConfig) -> imaplib.IMAP4_SSL:
    require_imap_config(config)
    client = imaplib.IMAP4_SSL(config.imap_host, config.imap_port)
    typ, data = client.login(config.address, config.imap_password)
    if typ != "OK":
        raise RuntimeError("IMAP login failed: " + repr(data))
    return client


def select_mailbox(client: imaplib.IMAP4_SSL, mailbox: str, readonly: bool = True) -> None:
    mailbox = mailbox or "INBOX"
    if mailbox.upper() == "INBOX":
        encoded = "INBOX"
    else:
        encoded = imap_utf7_encode(mailbox)
    typ, data = client.select(f'"{encoded}"', readonly=readonly)
    if typ != "OK":
        raise RuntimeError(f"Could not select mailbox {mailbox!r}: {data!r}")


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def address_list(values: list[str] | None) -> list[str]:
    result = []
    for name, addr in getaddresses(values or []):
        name = decode_mime(name)
        if name and addr:
            result.append(f"{name} <{addr}>")
        elif addr:
            result.append(addr)
        elif name:
            result.append(name)
    return result


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def html_to_text(value: str) -> str:
    parser = PlainHTMLParser()
    parser.feed(value)
    return clean_text(parser.text())


def get_message_body(msg: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disposition = part.get_content_disposition()
            if disposition == "attachment":
                continue
            content_type = part.get_content_type()
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                content = payload.decode(charset, errors="replace")
            if content_type == "text/plain":
                plain_parts.append(clean_text(str(content)))
            elif content_type == "text/html":
                html_parts.append(html_to_text(str(content)))
    else:
        content_type = msg.get_content_type()
        try:
            content = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            content = payload.decode(charset, errors="replace")
        if content_type == "text/html":
            html_parts.append(html_to_text(str(content)))
        else:
            plain_parts.append(clean_text(str(content)))
    body = "\n\n".join(part for part in plain_parts if part)
    if not body:
        body = "\n\n".join(part for part in html_parts if part)
    return body


def attachment_summaries(msg: Message) -> list[dict[str, Any]]:
    attachments = []
    for part in msg.walk():
        filename = decode_mime(part.get_filename())
        disposition = part.get_content_disposition()
        if disposition == "attachment" or filename:
            payload = part.get_payload(decode=True)
            attachments.append(
                {
                    "filename": filename or None,
                    "content_type": part.get_content_type(),
                    "size_bytes": len(payload) if payload else None,
                }
            )
    return attachments


def parse_email_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.isoformat()
    except Exception:
        return value


def parse_message(raw: bytes, uid: str | None = None, mailbox: str = "INBOX") -> dict[str, Any]:
    msg = BytesParser(policy=default).parsebytes(raw)
    body = get_message_body(msg)
    attachments = attachment_summaries(msg)
    return {
        "uid": uid,
        "mailbox": mailbox,
        "message_id": msg.get("message-id"),
        "subject": decode_mime(msg.get("subject")),
        "from": address_list(msg.get_all("from", [])),
        "to": address_list(msg.get_all("to", [])),
        "cc": address_list(msg.get_all("cc", [])),
        "date": parse_email_date(msg.get("date")),
        "snippet": body[:500],
        "body": body,
        "has_attachments": bool(attachments),
        "attachments": attachments,
    }


def fetch_raw_message(client: imaplib.IMAP4_SSL, uid: str) -> bytes:
    typ, data = client.uid("fetch", str(uid), "(BODY.PEEK[])")
    if typ != "OK":
        raise RuntimeError(f"Could not fetch UID {uid}: {data!r}")
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    raise RuntimeError(f"UID {uid} did not return a message body")


def tool_get_config_status(_: dict[str, Any]) -> dict[str, Any]:
    config = get_config()
    return {
        "address_configured": bool(config.address),
        "imap_password_configured": bool(config.imap_password),
        "imap_host": config.imap_host,
        "imap_port": config.imap_port,
        "imap_ready": config.configured_for_imap,
        "smtp_host": config.smtp_host,
        "smtp_port": config.smtp_port,
        "smtp_password_configured": bool(config.smtp_password),
        "smtp_enabled": config.smtp_enabled,
        "smtp_ready": config.smtp_enabled and config.configured_for_smtp,
        "note": "Secrets are never returned by this tool.",
    }


def parse_list_line(line: bytes) -> dict[str, Any]:
    text = line.decode("ascii", errors="replace")
    match = re.match(r'\((?P<flags>.*?)\)\s+"?(?P<delimiter>[^"]*)"?\s+(?P<name>.*)', text)
    if not match:
        return {"raw": text}
    raw_name = match.group("name").strip()
    if raw_name.startswith('"') and raw_name.endswith('"'):
        raw_name = raw_name[1:-1]
    return {
        "name": imap_utf7_decode(raw_name),
        "encoded_name": raw_name,
        "delimiter": match.group("delimiter") or None,
        "attributes": match.group("flags").split(),
    }


def tool_list_mailboxes(_: dict[str, Any]) -> dict[str, Any]:
    config = get_config()
    client = connect_imap(config)
    try:
        typ, data = client.list()
        if typ != "OK":
            raise RuntimeError("Could not list mailboxes: " + repr(data))
        return {"mailboxes": [parse_list_line(line) for line in data if isinstance(line, bytes)]}
    finally:
        client.logout()


def tool_search_emails(args: dict[str, Any]) -> dict[str, Any]:
    mailbox = str(args.get("mailbox") or "INBOX")
    newer_than_days = args.get("newer_than_days")
    max_results = min(int(args.get("max_results") or 25), MAX_RESULTS_CAP)
    from_include = [str(x).lower() for x in args.get("from_include") or []]
    from_exclude = [str(x).lower() for x in args.get("from_exclude") or []]
    subject_contains = str(args.get("subject_contains") or "").lower()
    text_contains = str(args.get("text_contains") or "").lower()

    criteria: list[str] = ["ALL"]
    if newer_than_days:
        since = dt.datetime.now().date() - dt.timedelta(days=int(newer_than_days))
        criteria = ["SINCE", since.strftime("%d-%b-%Y")]

    config = get_config()
    client = connect_imap(config)
    try:
        select_mailbox(client, mailbox, readonly=True)
        typ, data = client.uid("search", None, *criteria)
        if typ != "OK":
            raise RuntimeError("IMAP search failed: " + repr(data))
        all_uids = data[0].split() if data and data[0] else []
        scan_uids = list(reversed(all_uids[-SEARCH_SCAN_CAP:]))
        results = []
        scanned = 0
        for uid_bytes in scan_uids:
            if len(results) >= max_results:
                break
            uid = uid_bytes.decode("ascii")
            raw = fetch_raw_message(client, uid)
            parsed = parse_message(raw, uid=uid, mailbox=mailbox)
            scanned += 1
            hay_from = " ".join(parsed["from"]).lower()
            hay_subject = str(parsed["subject"]).lower()
            hay_text = (hay_subject + " " + str(parsed["snippet"])).lower()
            if from_include and not any(token in hay_from for token in from_include):
                continue
            if from_exclude and any(token in hay_from for token in from_exclude):
                continue
            if subject_contains and subject_contains not in hay_subject:
                continue
            if text_contains and text_contains not in hay_text:
                continue
            parsed.pop("body", None)
            results.append(parsed)
        return {
            "mailbox": mailbox,
            "criteria": criteria,
            "total_uids_matching_date": len(all_uids),
            "scanned": scanned,
            "returned": len(results),
            "emails": results,
        }
    finally:
        try:
            client.close()
        except Exception:
            pass
        client.logout()


def tool_read_email(args: dict[str, Any]) -> dict[str, Any]:
    uid = args.get("uid")
    if not uid:
        raise ValueError("uid is required")
    mailbox = str(args.get("mailbox") or "INBOX")
    config = get_config()
    client = connect_imap(config)
    try:
        select_mailbox(client, mailbox, readonly=True)
        raw = fetch_raw_message(client, str(uid))
        return parse_message(raw, uid=str(uid), mailbox=mailbox)
    finally:
        try:
            client.close()
        except Exception:
            pass
        client.logout()


def normalize_recipients(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def validated_email_payload(args: dict[str, Any]) -> dict[str, Any]:
    to = normalize_recipients(args.get("to"))
    subject = str(args.get("subject") or "").strip()
    body = str(args.get("body") or "")
    if not to:
        raise ValueError("to is required")
    if not subject:
        raise ValueError("subject is required")
    if not body.strip():
        raise ValueError("body is required")
    return {
        "to": to,
        "cc": normalize_recipients(args.get("cc")),
        "bcc": normalize_recipients(args.get("bcc")),
        "subject": subject,
        "body": body,
    }


def draft_id_for_payload(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "draft_" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def build_confirmation_card(payload: dict[str, Any], draft_id: str) -> str:
    lines = [
        "+-- Pending NetEase Email ----------------",
        f"| Draft ID: {draft_id}",
        f"| To: {', '.join(payload['to'])}",
    ]
    if payload["cc"]:
        lines.append(f"| Cc: {', '.join(payload['cc'])}")
    if payload["bcc"]:
        lines.append(f"| Bcc: {', '.join(payload['bcc'])}")
    lines.extend(
        [
            f"| Subject: {payload['subject']}",
            "|",
        ]
    )
    for body_line in str(payload["body"]).splitlines() or [""]:
        lines.append(f"| {body_line}")
    lines.extend(
        [
            "|",
            "| Reply yes/send/confirm to send.",
            "| Reply no/cancel to cancel.",
            "+-----------------------------------------",
        ]
    )
    return "\n".join(lines)


def tool_prepare_draft(args: dict[str, Any]) -> dict[str, Any]:
    payload = validated_email_payload(args)
    draft_id = draft_id_for_payload(payload)
    PENDING_DRAFTS[draft_id] = payload
    return {
        "status": "awaiting_confirmation",
        "draft_id": draft_id,
        "to": payload["to"],
        "cc": payload["cc"],
        "bcc": payload["bcc"],
        "subject": payload["subject"],
        "body": payload["body"],
        "confirmation_card": build_confirmation_card(payload, draft_id),
        "accepted_confirmations": ["yes", "send", "confirm"],
        "accepted_cancellations": ["no", "cancel"],
        "note": "This draft is stored in memory only and has not been sent.",
    }


def tool_send_email(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("confirm_send") is not True:
        raise ValueError("confirm_send must be true after explicit user confirmation")
    config = get_config()
    if not config.smtp_enabled:
        raise ValueError("SMTP sending is disabled. Set NETEASE_MAIL_ENABLE_SMTP=true to enable it.")
    if not config.configured_for_smtp:
        raise ValueError("Missing SMTP configuration: address, SMTP password, or SMTP host")

    payload = validated_email_payload(args)
    to = payload["to"]
    cc = payload["cc"]
    bcc = payload["bcc"]

    msg = EmailMessage()
    msg["From"] = config.address
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = payload["subject"]
    msg.set_content(payload["body"])

    recipients = to + cc + bcc
    with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port) as smtp:
        smtp.login(config.address, config.smtp_password)
        smtp.send_message(msg, from_addr=config.address, to_addrs=recipients)
    return {"status": "sent", "to": to, "cc": cc, "bcc_count": len(bcc), "subject": payload["subject"]}


def tool_send_prepared_draft(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("confirm_send") is not True:
        raise ValueError("confirm_send must be true after explicit user confirmation")
    draft_id = str(args.get("draft_id") or "").strip()
    if not draft_id:
        raise ValueError("draft_id is required")
    payload = PENDING_DRAFTS.get(draft_id)
    if payload is None:
        raise ValueError(f"Unknown or expired draft_id: {draft_id}")
    result = tool_send_email({**payload, "confirm_send": True})
    PENDING_DRAFTS.pop(draft_id, None)
    return {**result, "draft_id": draft_id}


def tool_cancel_prepared_draft(args: dict[str, Any]) -> dict[str, Any]:
    draft_id = str(args.get("draft_id") or "").strip()
    if not draft_id:
        raise ValueError("draft_id is required")
    existed = PENDING_DRAFTS.pop(draft_id, None) is not None
    return {"status": "cancelled" if existed else "not_found", "draft_id": draft_id}


TOOLS: dict[str, dict[str, Any]] = {
    "get_config_status": {
        "description": "Return NetEase Mail configuration status without exposing secrets.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_get_config_status,
    },
    "list_mailboxes": {
        "description": "List IMAP mailboxes for the configured NetEase Mail account.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_list_mailboxes,
    },
    "search_emails": {
        "description": "Search recent NetEase Mail messages with local sender, subject, and text filters.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mailbox": {"type": "string", "default": "INBOX"},
                "newer_than_days": {"type": "integer", "minimum": 1},
                "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS_CAP},
                "from_include": {"type": "array", "items": {"type": "string"}},
                "from_exclude": {"type": "array", "items": {"type": "string"}},
                "subject_contains": {"type": "string"},
                "text_contains": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "handler": tool_search_emails,
    },
    "read_email": {
        "description": "Read one email by IMAP UID from a mailbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mailbox": {"type": "string", "default": "INBOX"},
                "uid": {"type": "string"},
            },
            "required": ["uid"],
            "additionalProperties": False,
        },
        "handler": tool_read_email,
    },
    "prepare_draft": {
        "description": "Prepare an in-memory email draft and return a conversation confirmation card without sending it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "cc": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "bcc": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
        "handler": tool_prepare_draft,
    },
    "send_prepared_draft": {
        "description": "Send an in-memory draft created by prepare_draft. Requires explicit user confirmation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string"},
                "confirm_send": {"type": "boolean"},
            },
            "required": ["draft_id", "confirm_send"],
            "additionalProperties": False,
        },
        "handler": tool_send_prepared_draft,
    },
    "cancel_prepared_draft": {
        "description": "Cancel an in-memory draft created by prepare_draft without sending it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string"},
            },
            "required": ["draft_id"],
            "additionalProperties": False,
        },
        "handler": tool_cancel_prepared_draft,
    },
    "send_email": {
        "description": "Send email through NetEase SMTP. Requires explicit confirmation and SMTP to be enabled.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "cc": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "bcc": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "confirm_send": {"type": "boolean"},
            },
            "required": ["to", "subject", "body", "confirm_send"],
            "additionalProperties": False,
        },
        "handler": tool_send_email,
    },
}


def tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
        }
        for name, spec in TOOLS.items()
    ]


def read_message() -> dict[str, Any] | None:
    first = sys.stdin.buffer.readline()
    if first == b"":
        return None
    if first.lstrip().startswith(b"{"):
        return json.loads(first.decode("utf-8"))

    headers: dict[str, str] = {}
    line = first
    while line not in (b"\r\n", b"\n", b""):
        key, _, value = line.decode("ascii", errors="replace").partition(":")
        headers[key.lower()] = value.strip()
        line = sys.stdin.buffer.readline()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def write_message(message: dict[str, Any]) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def success_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    try:
        if method == "initialize":
            return success_response(
                request_id,
                {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return success_response(request_id, {})
        if method == "tools/list":
            return success_response(request_id, {"tools": tool_specs()})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name not in TOOLS:
                raise ValueError(f"Unknown tool: {name}")
            handler: Callable[[dict[str, Any]], dict[str, Any]] = TOOLS[name]["handler"]
            result = handler(args)
            return success_response(
                request_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, ensure_ascii=False, indent=2),
                        }
                    ]
                },
            )
        if method == "resources/list":
            return success_response(request_id, {"resources": []})
        if method == "prompts/list":
            return success_response(request_id, {"prompts": []})
        return error_response(request_id, -32601, f"Method not found: {method}")
    except Exception as exc:
        print(traceback.format_exc(), file=sys.stderr)
        return error_response(request_id, -32000, str(exc))


def main() -> None:
    while True:
        message = read_message()
        if message is None:
            break
        response = handle_request(message)
        if response is not None and message.get("id") is not None:
            write_message(response)


if __name__ == "__main__":
    main()
