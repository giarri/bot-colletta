"""WhatsApp channel (Meta Cloud API): webhook parsing, signature check, sending.

The conversation lives in bot.py and is written for Telegram: HTML text, inline keyboards, /commands. This module
translates in both directions, so the handlers do not know which app the user is on:

- incoming: updates() turns a WhatsApp webhook payload into Telegram-shaped updates (a typed text becomes a
  "message", a tapped button or list row a "callback_query"; a tapped command button a "message" with "/command");
- outgoing: send() turns HTML into WhatsApp markup and the buttons into reply buttons (up to 3) or list messages
  (10 rows each, as many messages as needed). A message without buttons gets one button per /command it mentions,
  which stands in for Telegram's tappable commands and command menu.

WhatsApp cannot edit a message or remove its buttons: bot.py sends a new message instead of editing, and old
buttons stay tappable (the handlers already answer "non è più valido" to a stale one).

A user is identified by "wa<phone number>" (chat id and user id: WhatsApp has only 1:1 chats here).

Settings: WA_TOKEN (access token), WA_PHONE_ID (id of the sending number), WA_APP_SECRET (signs the webhook calls),
WA_VERIFY_TOKEN (webhook registration), WA_API_VERSION (optional).
"""

import hashlib
import hmac
import html
import logging
import os
import re

import requests

TIMEOUT = 15
PREFIX = "wa"
CMD = "cmd:"  # id prefix of a button that stands for a typed command

# Limits of the Cloud API (lengths are counted in UTF-16 units to stay on the safe side with emoji)
BUTTONS_MAX, BUTTON_TITLE = 3, 20
ROWS_MAX, ROW_TITLE, ROW_DESCRIPTION = 10, 24, 72
BODY_MAX, TEXT_MAX = 1024, 4096

LIST_BUTTON = "Scegli"
MORE = "Altre scelte:"
# a /command in a text, not a piece of a URL path
COMMAND_RE = re.compile(r"(?<![\w/:.])/([a-zà]+)\b")


# ---------------- incoming ----------------

def valid_signature(body: bytes, header: str) -> bool:
    """X-Hub-Signature-256: 'sha256=' + HMAC-SHA256 of the raw body, keyed with the app secret."""
    secret = os.environ.get("WA_APP_SECRET", "")
    if not secret or not header:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), header.strip().encode())


def valid_verification(mode: str, token: str) -> bool:
    """The GET that Meta sends when the webhook is registered."""
    expected = os.environ.get("WA_VERIFY_TOKEN", "")
    return mode == "subscribe" and bool(expected) and hmac.compare_digest(expected.encode(), (token or "").encode())


def updates(payload: dict) -> list[dict]:
    """Telegram-shaped updates for the user messages in a webhook payload (delivery statuses etc. are skipped)."""
    out = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if change.get("field") != "messages" or value.get("messaging_product") != "whatsapp":
                continue
            for m in value.get("messages") or []:
                sender = re.sub(r"\D", "", str(m.get("from") or ""))
                if not sender:
                    continue
                uid = PREFIX + sender
                chat = {"id": uid, "type": "private"}
                text, data = None, None
                if m.get("type") == "text":
                    text = (m.get("text") or {}).get("body")
                elif m.get("type") == "interactive":
                    inter = m.get("interactive") or {}
                    data = (inter.get("button_reply") or inter.get("list_reply") or {}).get("id")
                elif m.get("type") == "button":  # quick reply of a template message
                    text = (m.get("button") or {}).get("text")
                if data and data.startswith(CMD):
                    text, data = "/" + data[len(CMD):], None
                if data:
                    out.append({"callback_query": {"id": m.get("id"), "from": {"id": uid}, "data": data,
                                                   "message": {"message_id": m.get("id"), "chat": chat}}})
                elif text:
                    out.append({"message": {"message_id": m.get("id"), "text": text, "chat": chat, "from": {"id": uid}}})
    return out


# ---------------- outgoing ----------------

def to_text(s: str) -> str:
    """Telegram HTML -> WhatsApp markup."""
    s = re.sub(r"(?s)<pre>\n?(.*?)\n?</pre>", lambda m: "```" + m.group(1) + "```", s)
    s = re.sub(r'(?s)<a href="([^"]*)">(.*?)</a>', r"\2: \1", s)
    s = re.sub(r"</?b>", "*", s)
    s = re.sub(r"</?i>", "_", s)
    s = re.sub(r"</?code>", "`", s)
    return html.unescape(s)


def _len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _cut(s: str, n: int) -> str:
    s = " ".join(s.split())
    if _len(s) <= n:
        return s
    while _len(s) > n - 1:
        s = s[:-1]
    return s.rstrip() + "…"


def api(payload: dict) -> bool:
    url = (f"https://graph.facebook.com/{os.environ.get('WA_API_VERSION', 'v24.0')}/"
           f"{os.environ['WA_PHONE_ID']}/messages")
    r = requests.post(url, json={"messaging_product": "whatsapp", "recipient_type": "individual", **payload},
                      headers={"Authorization": "Bearer " + os.environ["WA_TOKEN"]}, timeout=TIMEOUT)
    if r.status_code != 200:
        logging.error("WhatsApp %s failed: %s %s", payload.get("type"), r.status_code, r.text[:300])
    return r.status_code == 200


def _send_text(to: str, text: str) -> bool:
    ok = True
    while text:
        part = text
        if len(part) > TEXT_MAX:  # split on a line break when there is one
            cut = text.rfind("\n", 0, TEXT_MAX)
            part = text[:cut if cut > 0 else TEXT_MAX]
        text = text[len(part):].lstrip("\n")
        ok = api({"to": to, "type": "text", "text": {"body": part, "preview_url": False}}) and ok
    return ok


def _send_choices(to: str, body: str, options: list[tuple[str, str, str]]) -> bool:
    """options: (label, id, description). Reply buttons when they fit, list messages otherwise."""
    if len(options) <= BUTTONS_MAX and all(_len(label) <= BUTTON_TITLE for label, _, _ in options):
        return api({"to": to, "type": "interactive", "interactive": {
            "type": "button", "body": {"text": body},
            "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": label}} for label, i, _ in options]}}})
    ok = True
    for start in range(0, len(options), ROWS_MAX):
        rows = []
        for label, i, description in options[start:start + ROWS_MAX]:
            row = {"id": i, "title": _cut(label, ROW_TITLE)}
            if description or _len(label) > ROW_TITLE:  # the whole label, when the title had to be cut
                row["description"] = _cut(description or label, ROW_DESCRIPTION)
            rows.append(row)
        ok = api({"to": to, "type": "interactive", "interactive": {
            "type": "list", "body": {"text": body if start == 0 else MORE},
            "action": {"button": LIST_BUTTON, "sections": [{"rows": rows}]}}}) and ok
    return ok


def send(chat_id: str, text: str, buttons: list[list[tuple[str, str]]] | None = None,
         commands: dict[str, str] | None = None) -> bool:
    """Send a bot message. `commands` ({name: description}): the /commands that get a button when the message
    has no buttons of its own. Returns False when WhatsApp refused (a part of) it."""
    to = str(chat_id).removeprefix(PREFIX)
    body = to_text(text)
    options = [(label, data, "") for row in buttons or [] for label, data in row]
    if not options and commands:
        names = list(dict.fromkeys(m.group(1) for m in COMMAND_RE.finditer(text) if m.group(1) in commands))
        options = [(name.capitalize(), CMD + name, commands[name] if len(names) > BUTTONS_MAX else "") for name in names]
    if not options:
        return _send_text(to, body)
    ok = True
    if _len(body) > BODY_MAX:  # too long for a message with buttons: the text first, its last line with the buttons
        head, _, body = body.rpartition("\n")
        if not head or _len(body) > BODY_MAX:
            head, body = (head + "\n" + body).strip(), LIST_BUTTON + ":"
        ok = _send_text(to, head)
    return _send_choices(to, body, options) and ok
