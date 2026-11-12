"""Tests for whatsapp.py alone (no bot, no network):  python -m pytest tests/test_whatsapp.py -q"""

import hashlib
import hmac
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import whatsapp as wa  # noqa: E402


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(wa, "api", lambda payload: out.append(payload) or True)
    return out


def payload(*messages, field="messages"):
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": field, "value": {
        "messaging_product": "whatsapp", "messages": list(messages)}}]}]}


def test_signature(monkeypatch):
    body = b'{"entry": []}'
    monkeypatch.setenv("WA_APP_SECRET", "s3cret")
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert wa.valid_signature(body, good)
    assert not wa.valid_signature(body + b" ", good) and not wa.valid_signature(body, "") and not wa.valid_signature(body, "sha256=à")
    monkeypatch.delenv("WA_APP_SECRET")
    assert not wa.valid_signature(body, good)  # no secret configured: everything is refused


def test_verification(monkeypatch):
    monkeypatch.setenv("WA_VERIFY_TOKEN", "tok")
    assert wa.valid_verification("subscribe", "tok")
    assert not wa.valid_verification("subscribe", "other") and not wa.valid_verification("x", "tok")
    monkeypatch.setenv("WA_VERIFY_TOKEN", "")
    assert not wa.valid_verification("subscribe", "")


def test_updates():
    ups = wa.updates(payload(
        {"from": "393331234567", "id": "wamid.1", "type": "text", "text": {"body": "12,5"}},
        {"from": "393331234567", "id": "wamid.2", "type": "interactive",
         "interactive": {"type": "list_reply", "list_reply": {"id": "cat:25", "title": "Pasta"}}},
        {"from": "393331234567", "id": "wamid.3", "type": "interactive",
         "interactive": {"type": "button_reply", "button_reply": {"id": "cmd:lista", "title": "Lista"}}},
        {"from": "393331234567", "id": "wamid.4", "type": "image", "image": {"id": "x"}}))
    assert [next(iter(u)) for u in ups] == ["message", "callback_query", "message"]  # the image is ignored
    assert ups[0]["message"]["text"] == "12,5" and ups[0]["message"]["from"]["id"] == "wa393331234567"
    assert ups[0]["message"]["chat"] == {"id": "wa393331234567", "type": "private"}
    assert ups[1]["callback_query"]["data"] == "cat:25" and ups[1]["callback_query"]["message"]["chat"]["id"] == "wa393331234567"
    assert ups[2]["message"]["text"] == "/lista"
    assert wa.updates({"entry": [{"changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "statuses": [{"status": "delivered"}]}}]}]}) == []
    assert wa.updates(payload({"from": "1", "id": "x", "type": "text", "text": {"body": "hi"}}, field="other")) == []
    assert wa.updates({}) == []


def test_to_text():
    assert wa.to_text("Cartone n. <b>4</b>: <i>prima</i> <code>12,5</code>") == "Cartone n. *4*: _prima_ `12,5`"
    assert wa.to_text("<pre>N° kg\n1  &lt;12&gt; A &amp; B\n</pre>") == "```N° kg\n1  <12> A & B```"
    assert wa.to_text('🔗 <a href="https://x.it/a/">Apri il sito</a>') == "🔗 Apri il sito: https://x.it/a/"


def test_plain_text_and_command_buttons(sent):
    cmds = {"cartone": "Registra un cartone", "lista": "Vedi i cartoni", "annulla": "Annulla", "esci": "Esci"}
    wa.send("wa39333", "Quanti kg pesa?", None, cmds)
    assert sent[-1] == {"to": "39333", "type": "text", "text": {"body": "Quanti kg pesa?", "preview_url": False}}
    wa.send("wa39333", "Scrivi /cartone, poi /lista. Sito: https://www.itud.eu/gnca/mi/lista/ o /altro", None, cmds)
    assert [b["reply"] for b in sent[-1]["interactive"]["action"]["buttons"]] == [
        {"id": "cmd:cartone", "title": "Cartone"}, {"id": "cmd:lista", "title": "Lista"}]
    wa.send("wa39333", "/cartone /lista /annulla /esci", None, cmds)  # more than 3: a list with the descriptions
    rows = sent[-1]["interactive"]["action"]["sections"][0]["rows"]
    assert sent[-1]["interactive"]["type"] == "list" and rows[0] == {"id": "cmd:cartone", "title": "Cartone",
                                                                     "description": "Registra un cartone"}
    wa.send("wa39333", "Scrivi /cartone")  # no commands given (an "edited" message): plain text
    assert sent[-1]["type"] == "text"


def test_limits(sent):
    wa.send("wa1", "Scegli", [[("✅ Sì", "y"), ("❌ No", "n")]])
    assert sent[-1]["interactive"]["type"] == "button"
    wa.send("wa1", "Scegli", [[("Un titolo decisamente troppo lungo per un pulsante", "a")], [("No", "n")]])
    row = sent[-1]["interactive"]["action"]["sections"][0]["rows"][0]
    assert sent[-1]["interactive"]["type"] == "list" and len(row["title"]) <= 24 and row["title"].endswith("…")
    assert row["description"] == "Un titolo decisamente troppo lungo per un pulsante"
    del sent[:]
    wa.send("wa1", "Tocca:", [[(f"Negozio {i}", f"pv:{i}")] for i in range(23)])
    assert [len(p["interactive"]["action"]["sections"][0]["rows"]) for p in sent] == [10, 10, 3]
    assert [p["interactive"]["body"]["text"] for p in sent] == ["Tocca:", wa.MORE, wa.MORE]
    del sent[:]
    long = "\n".join(f"riga {i}" for i in range(300)) + "\nScrivi la targa."
    wa.send("wa1", long, [[("❌ Annulla", "trk:no")]])
    assert [p["type"] for p in sent] == ["text", "interactive"] and sent[1]["interactive"]["body"]["text"] == "Scrivi la targa."
    assert sent[0]["text"]["body"].endswith("riga 299")
    del sent[:]
    wa.send("wa1", "x" * 9000)
    assert [len(p["text"]["body"]) for p in sent] == [4096, 4096, 808]
