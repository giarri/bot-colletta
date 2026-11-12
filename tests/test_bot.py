"""Conversation tests for bot.py: fake Telegram updates, fake site session, in-memory user store.

No network, no azure/cryptography packages needed:
  python -m pytest tests/test_bot.py -q
"""

import copy
import dataclasses
import json
import os
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- fake user_store (the real one needs azure + cryptography), installed before importing bot ----

fake_store = types.ModuleType("user_store")
fake_store.ROWS = {}
fake_store.VERSIONS = {}  # RowKey -> number of saves (stands in for the table's etag)


def _get(user_id):
    row = copy.deepcopy(fake_store.ROWS.get(str(user_id), {"PartitionKey": "user", "RowKey": str(user_id)}))
    if str(user_id) in fake_store.VERSIONS:
        row["_etag"] = fake_store.VERSIONS[str(user_id)]
    return row


def _save(row):
    stored = {k: v for k, v in row.items() if k not in ("code_expired", "_etag")}
    for k, v in stored.items():  # Table Storage: keep values small strings
        assert isinstance(v, str), f"{k} is not a string: {v!r}"
        assert len(v) < 30000, k
    fake_store.VERSIONS[row["RowKey"]] = row["_etag"] = fake_store.VERSIONS.get(row["RowKey"], 0) + 1
    fake_store.ROWS[row["RowKey"]] = copy.deepcopy(stored)


def _claim(row):
    """Like the real one: saves only if the row was not saved by someone else since it was read."""
    if row.get("_etag") != fake_store.VERSIONS.get(row["RowKey"]):
        return False
    _save(row)
    return True


def _set_code(row, code):
    row["code_enc"] = "enc:" + code
    row["code_set_at"] = datetime.now(timezone.utc).isoformat()


def _clear_code(row):
    row["code_enc"] = ""
    row.pop("code_set_at", None)


fake_store.get = _get
fake_store.save = _save
fake_store.claim = _claim
fake_store.delete = lambda user_id: fake_store.ROWS.pop(str(user_id), None)
fake_store.delete_members = lambda chat_id: [fake_store.ROWS.pop(k) for k in list(fake_store.ROWS) if k.startswith(f"{chat_id}:")]
fake_store.set_code = _set_code
fake_store.get_code = lambda row: row["code_enc"][4:] if row.get("code_enc") else None
fake_store.clear_code = _clear_code
fake_store.expires_at = lambda row: (datetime.fromisoformat(row["code_set_at"]) + timedelta(days=7)
                                     if row.get("code_set_at") else None)
sys.modules["user_store"] = fake_store

import bot  # noqa: E402

UID = 4242
CHAT = 4242

CATS = [("18", "Tonno in scatola"), ("19", "Colazione e merenda"), ("20", "Passata e sughi"),
        ("21", "Legumi/verdure in scatola"), ("22", "Olio"), ("23", "Omogeneizzati"), ("24", "Alimenti infanzia"),
        ("25", "Pasta"), ("26", "Riso"), ("27", "Zucchero"), ("28", "Latte"), ("29", "Biscotti"), ("32", "Varie")]


@dataclass
class Carton:
    number: str
    kg: str
    content: str
    done: str
    truck: str


@dataclass
class TruckForm:  # same fields as site_client.TruckForm
    pending: int = 3
    max_carton: str = "5"
    count_min: int = 1
    count_max: int = 3
    first: str = "3"
    last: str = "5"
    total_kg: str = "36"
    trip: str = "2"
    summary: list = field(default_factory=lambda: [("2", "Pasta", "24"), ("1", "Olio <extra>", "12")])


@dataclass
class EditForm:  # same fields as site_client.EditForm
    cartons: list = field(default_factory=lambda: [("1153", "4", "Tonno in scatola", "12.00"), ("1160", "6", "Pasta", "9.50")])
    categories: list = field(default_factory=lambda: list(CATS))
    kg_min: float = 1.0
    kg_max: float = 22.0


class World:
    """What the fake site looks like, plus a log of what the bot did to it."""

    def __init__(self):
        self.calls = []        # (method, args...) on the fake session, in order
        self.added = []        # add_carton calls
        self.loads = []        # load_truck calls
        self.cats = list(CATS)
        self.limits = (1.0, 22.0)
        self.forms = [TruckForm()]  # truck_form() results, one per session; the last one repeats
        self.cartons_after = [Carton("1", "10,00", "Pasta", "10:00", "10:30"), Carton("2", "9,00", "Riso", "10:05", "10:30"),
                              Carton("3", "12,00", "Pasta", "11:00", "11:40"), Carton("4", "12,00", "Pasta", "11:01", "11:40"),
                              Carton("5", "12,00", "Olio", "11:02", "11:40")]
        self.load_error = None
        self.edit_form = EditForm()
        self.changed = []      # edit_carton calls
        self.edit_error = None
        self.add_error = None
        self.closed = False
        self.sessions = 0
        self.tg = []           # (method, payload)

    # ---- Telegram side ----
    def sent(self):
        return [p for m, p in self.tg if m == "sendMessage"]

    def last(self):
        return self.sent()[-1]

    def edits(self):
        return [p for m, p in self.tg if m == "editMessageText"]

    def buttons(self, payload=None):
        payload = payload or self.last()
        return [[(b["text"], b["callback_data"]) for b in r] for r in payload.get("reply_markup", {}).get("inline_keyboard", [])]

    def datas(self, payload=None):
        return [d for r in self.buttons(payload) for _, d in r]

    @property
    def row(self):
        return fake_store.ROWS[str(UID)]


@pytest.fixture
def w(monkeypatch):
    world = World()
    fake_store.ROWS.clear()
    fake_store.VERSIONS.clear()
    fake_store.ROWS[str(UID)] = {"PartitionKey": "user", "RowKey": str(UID), "prov": "mi", "city": "arese", "pv_id": "493",
                                 "pv_label": "CONAD/Viale dei Platani 6", "code_enc": "enc:SECRET",
                                 "code_set_at": datetime.now(timezone.utc).isoformat(), "state": ""}

    class FakeSession:
        def __init__(self, prov, city, pv_id, code):
            assert (prov, city, pv_id, code) == ("mi", "arese", "493", "SECRET")
            world.sessions += 1
            self.on_store_page = True

        def title(self):
            return "Arese/CONAD"

        def categories(self):
            return list(world.cats)

        def closed(self):
            return world.closed

        def next_number(self):
            return "6"

        def cartons(self):
            return list(world.cartons_after)

        def kg_limits(self):
            assert self.on_store_page, "kg_limits() must be called before truck_form()"
            world.calls.append(("kg_limits",))
            return world.limits

        def add_carton(self, cat, kg, confirm=False):
            world.calls.append(("add_carton", cat, kg, confirm))
            if world.add_error and not confirm:
                raise world.add_error
            world.added.append((cat, kg, confirm))
            return Carton("6", f"{kg:.2f}".replace(".", ","), dict(world.cats)[cat], "12:00", "")

        def edit_form(self):
            return world.edit_form

        def edit_carton(self, carton_id, cat, kg):
            if world.edit_error:
                raise world.edit_error
            world.changed.append((carton_id, cat, kg))
            number = {c[0]: c[1] for c in world.edit_form.cartons}[carton_id]
            return Carton(number, f"{kg:.2f}".replace(".", ","), dict(world.cats)[cat], "12:00", "")

        def truck_form(self):
            self.on_store_page = False
            world.calls.append(("truck_form",))
            return world.forms.pop(0) if len(world.forms) > 1 else world.forms[0]

        def load_truck(self, form, plate, count=None):
            world.calls.append(("load_truck", plate, count))
            if world.load_error:
                raise world.load_error
            world.loads.append((form, plate, count))
            return list(world.cartons_after)

    def no_network(*a, **k):
        raise AssertionError("network access in tests")

    monkeypatch.setattr("requests.sessions.Session.request", no_network)
    monkeypatch.setattr(bot.site, "StoreSession", FakeSession)
    monkeypatch.setattr(bot.site, "list_stores", no_network)
    monkeypatch.setattr(bot, "tg", lambda method, **payload: world.tg.append((method, payload)) or {})
    return world


_ids = iter(range(1000, 10 ** 9))


def say(text, chat_type="private", chat_id=CHAT):
    bot.handle_update({"update_id": next(_ids), "message": {
        "message_id": next(_ids), "text": text, "chat": {"id": chat_id, "type": chat_type}, "from": {"id": UID}}})


def tap(data, message_id=77):
    bot.handle_update({"update_id": next(_ids), "callback_query": {
        "id": str(next(_ids)), "data": data, "from": {"id": UID},
        "message": {"message_id": message_id, "chat": {"id": CHAT, "type": "private"}}}})


def second_update_during(monkeypatch, method, update):
    """Run `update` (a double tap / Telegram retry) while the first one is inside the site call `method`."""
    original = getattr(bot.site.StoreSession, method)
    nested = []

    def slow(self, *a, **k):
        if not nested:
            nested.append(1)
            update()
        return original(self, *a, **k)

    monkeypatch.setattr(bot.site.StoreSession, method, slow)


STORES = [("493", "CONAD/Viale dei Platani 6"), ("954", "EUROSPIN/Via Alfredo Papa 1")]
LOGGED_COMMANDS = ["/cartone", "/lista", "/camion", "/modifica", "/stato", "/esci"]


@pytest.fixture
def new_user(w, monkeypatch):
    fake_store.ROWS.clear()
    w.asked = []
    monkeypatch.setattr(bot.site, "list_stores", lambda prov, city: w.asked.append((prov, city)) or (city.lower(), STORES))
    return w


# ---------------- /cartone ----------------

def test_cartone_closed_or_unreadable_points_to_the_site(w):
    w.cats = []
    say("/cartone")  # the site shows no contents: no "retry", a link to the site
    text = w.last()["text"]
    assert "Non riesco a leggere" in text and "https://www.itud.eu/gnca/mi/arese/" in text and "iprova" not in text
    assert not w.buttons() and w.row["state"] == ""
    w.closed = True
    say("/cartone")  # the store declared the collection finished
    text = w.last()["text"]
    assert "terminata" in text and "https://www.itud.eu/gnca/mi/arese/" in text and "Non riesco" not in text


def test_crash_message_points_to_the_site(w):
    update = {"update_id": 1, "message": {"message_id": 5, "text": "/lista", "chat": {"id": CHAT, "type": "private"},
                                          "from": {"id": UID}}}
    bot.send_crash(update)
    text = w.last()["text"]
    assert "Errore interno" in text and "https://www.itud.eu/gnca/mi/arese/" in text and "iprova" not in text
    w.row.update(prov="", city="")  # city not chosen yet: nothing to link
    bot.send_crash(update)
    assert w.last()["text"] == "⚠️ Errore interno."


def test_cartone_flow(w):
    say("/cartone")
    rows = w.buttons()
    assert rows[0][0] == ("🐟 Tonno in scatola", "cat:18") and rows[-1] == [("❌ Annulla", "cat:x")]
    assert w.datas()[:-1] == [f"cat:{cid}" for cid, _ in CATS] and json.loads(w.row["cats_json"]) == dict(CATS)
    tap("cat:25")
    assert "🍝 Pasta" in w.edits()[-1]["text"] and "di solito" not in w.last()["text"]
    for bad in ("abc", "-3", "0", "12,345", "1e1"):
        say(bad)
        assert "Peso non valido" in w.last()["text"] and w.row["state"] == "await_kg"
    assert not w.added
    say("12.50 kg")
    assert w.added == [("25", 12.5, False)] and w.row["state"] == "" and "registrato" in w.last()["text"]
    say("/cartone")
    tap("cat:25")
    tap("cat:x")
    assert w.row["state"] == "" and w.edits()[-1]["text"] == "Operazione annullata."


def test_category_emojis():
    emojis = [bot._cat_emoji(lbl) for _, lbl in CATS]
    assert len(set(emojis)) == len(CATS) == 13 and bot.CAT_EMOJI_DEFAULT not in emojis
    assert bot._cat_emoji("OLIO d'oliva") == bot._cat_emoji("Olio")  # case-insensitive
    assert bot._cat_emoji("Prodotti per l'igiene") == bot._cat_emoji("Sorriso") == bot.CAT_EMOJI_DEFAULT


def test_weight_out_of_range_only_warns(w):
    say("/cartone")
    tap("cat:25")
    say("30")
    assert not w.added and w.row["state"] == "await_kg" and w.row["pending_kg"] == "30"
    assert "fuori dai limiti" in w.last()["text"] and w.datas() == ["kg:ok", "cat:x"]
    tap("kg:ok")
    assert w.added == [("25", 30.0, True)] and w.row["state"] == "" and w.row["pending_kg"] == ""
    tap("kg:ok")  # stale button
    assert len(w.added) == 1 and "non è più valido" in w.last()["text"] and "/cartone" in w.last()["text"]


def test_site_category_limits_warn_and_are_remembered(w):
    say("/cartone")
    tap("cat:32")
    w.add_error = bot.site.WeightRejected(4.0, 14.0)
    say("1,5")  # fine for the 1-22 input, refused by the site for this category
    assert not w.added and "🛒 Varie" in w.last()["text"] and "tra 4 e 14 kg" in w.last()["text"]
    tap("kg:ok")
    assert w.added == [("32", 1.5, True)]
    say("/cartone")
    tap("cat:32")
    n = len(w.calls)
    say("15")
    assert "tra 4 e 14 kg" in w.last()["text"]  # learned
    assert len(w.calls) == n and w.datas() == ["kg:ok", "cat:x"]  # warned without contacting the site


def test_add_carton_errors_close_the_flow(w):
    for error in (bot.site.SiteError("boh <b>"), bot.requests.Timeout("lento")):  # site refusal, network error
        say("/cartone")
        tap("cat:25")
        w.add_error = error
        say("12")
        assert "non ha confermato il cartone" in w.last()["text"] and "/lista" in w.last()["text"]
        assert "&lt;b&gt;" in w.last()["text"] or "lento" in w.last()["text"]
        w.add_error = None
        say("12")  # typing the weight again must not register a second carton blindly
        assert not w.added and w.row["state"] == ""


def test_double_submit_registers_one_carton(w, monkeypatch):
    say("/cartone")
    tap("cat:25")
    say("30")
    second_update_during(monkeypatch, "add_carton", lambda: tap("kg:ok"))  # double tap while the first is running
    tap("kg:ok")
    assert w.added == [("25", 30.0, True)]
    say("/cartone")
    tap("cat:26")
    stale = fake_store.get(UID)  # a second update read the row before the first one claimed it
    say("12")
    bot.receive_kg(CHAT, stale, "12")
    assert w.added[1:] == [("26", 12.0, False)]


# ---------------- /modifica ----------------

def test_modifica_changes_content_and_weight(w):
    say("/modifica")
    assert w.datas() == ["ed:1160", "ed:1153", "ed:x"] and w.buttons()[0][0][0] == "N. 6 – Pasta, 9,5 kg"
    tap("ed:1153")
    assert "Cartone n. <b>4</b>" in w.edits()[-1]["text"] and "12 kg" in w.edits()[-1]["text"]
    assert w.buttons()[0] == [("↩️ Lascia Tonno in scatola", "ec:18")] and w.datas().count("ec:18") == 1
    assert set(w.datas()) == {f"ec:{cid}" for cid, _ in CATS} | {"ed:x"}
    tap("ec:25")
    assert w.row["state"] == "await_edit_kg" and w.datas() == ["ek:same", "ed:x"]
    for bad in ("abc", "30"):
        say(bad)
        assert "Peso non valido" in w.last()["text"] and not w.changed
    say("10,5")
    assert w.changed == [("1153", "25", 10.5)] and w.row["state"] == "" and w.row["edit_json"] == ""
    assert "modificato" in w.last()["text"] and "Prima: Tonno in scatola, 12 kg" in w.last()["text"]
    say("/modifica")  # same content, new weight: still an edit
    tap("ed:1160")
    tap("ec:25")
    say("9")
    assert w.changed[1:] == [("1160", "25", 9.0)] and "modificato" in w.last()["text"]


def test_modifica_nothing_changed_does_not_touch_the_site(w):
    for finish in (lambda: tap("ek:same"), lambda: say("9,5"), lambda: say("9.50")):  # leave button, or the same weight typed
        say("/modifica")
        tap("ed:1160")
        tap("ec:25")  # same content
        sessions = w.sessions
        finish()
        text = w.last()["text"]
        assert "Nessuna modifica" in text and "n. <b>6</b>" in text and "Pasta" in text and "9,5 kg" in text
        assert "modificato" not in text
        assert not w.changed and w.sessions == sessions and w.row["state"] == "" and w.row["edit_json"] == ""


def test_modifica_nothing_to_edit_errors_and_double_tap(w, monkeypatch):
    w.edit_form = None
    say("/modifica")
    assert "Nessun cartone da modificare" in w.last()["text"]
    tap("ed:1153")  # old button
    assert "non è più valido" in w.last()["text"] and "/modifica" in w.last()["text"]
    w.edit_form = EditForm()
    say("/modifica")
    tap("ed:1153")
    tap("ec:32")
    w.edit_error = bot.site.WeightRejected(4.0, 14.0)
    say("2")
    assert "tra 4 e 14 kg" in w.last()["text"] and w.row["state"] == "await_edit_kg"  # asks the weight again
    w.edit_error = bot.requests.Timeout("lento")
    say("5")
    assert "non ha confermato la modifica" in w.last()["text"] and "n. 4" in w.last()["text"] and w.row["state"] == ""
    w.edit_error = None
    say("5")
    assert not w.changed
    say("/modifica")
    tap("ed:1160")
    tap("ec:26")  # a real change (Pasta -> Riso)
    second_update_during(monkeypatch, "edit_carton", lambda: tap("ek:same"))
    tap("ek:same")
    assert len(w.changed) == 1


# ---------------- /camion ----------------

def start_truck(w, plate="AB123CD"):
    say("/camion")
    say(plate)


def test_camion_all_cartons(w):
    say("/spedisci")
    assert w.row["state"] == "await_plate" and "Pasta" in w.last()["text"] and "targa del camion</b>." in w.last()["text"]
    say("ab 123 cd")
    assert w.row["pending_plate"] == "AB123CD"
    assert w.datas() == ["trk:all", "trk:part", "trk:no"]
    tap("trk:all")
    assert [(p, c) for _, p, c in w.loads] == [("AB123CD", None)]
    assert w.row["last_plate"] == "AB123CD" and w.row["truck_json"] == ""
    assert "Caricati <b>3</b>" in w.last()["text"]
    tap("trk:all")  # double tap
    assert len(w.loads) == 1 and "non è più valido" in w.last()["text"] and "/camion" in w.last()["text"]


def test_camion_partial_and_double_tap(w, monkeypatch):
    start_truck(w)
    tap("trk:part")
    say("9")
    assert "Numero non valido" in w.last()["text"]
    say("2")
    assert w.datas() == ["trk:ok", "trk:no"]
    second_update_during(monkeypatch, "load_truck", lambda: tap("trk:ok"))
    tap("trk:ok")
    assert [(p, c) for _, p, c in w.loads] == [("AB123CD", 2)]


def test_camion_plates(w):
    for plate in ("AB123C", "AB123CDE", "x", "AB<23CD", "AB-23CD"):
        start_truck(w, plate)
        assert "7 caratteri" in w.last()["text"] and w.row["state"] == "await_plate" and not w.row.get("pending_plate")
    w.row["last_plate"] = "ZZ999ZZ"
    say("/camion")
    assert w.datas() == ["trk:last", "trk:no"]
    tap("trk:last")
    tap("trk:all")
    assert w.loads[0][1] == "ZZ999ZZ"


def test_camion_not_submitted(w):
    start_truck(w)
    tap("trk:no")  # cancelled
    tap("trk:all")
    assert not w.loads and w.row["truck_json"] == ""
    w.forms = [TruckForm(), TruckForm(pending=4, max_carton="6", count_max=4)]  # a carton was added meanwhile
    start_truck(w)
    tap("trk:all")
    assert not w.loads and "cambiati" in w.last()["text"] and "Non ho caricato nulla" in w.last()["text"] and w.row["truck_json"] == ""
    w.forms = [None]
    say("/camion")
    assert "Nessun cartone" in w.last()["text"] and w.row["state"] == ""


def test_camion_errors_close_the_flow(w):
    for n, error in enumerate((bot.site.SiteError("boh"), bot.requests.Timeout("lento")), 1):
        w.load_error = error
        start_truck(w)
        tap("trk:all")
        assert "non ha confermato il carico" in w.last()["text"] and w.row["truck_json"] == "" and "last_plate" not in w.row
        tap("trk:all")  # the button is dead: the user is sent to /lista, not to a second blind attempt
        assert len([c for c in w.calls if c[0] == "load_truck"]) == n


def test_camion_lists_the_waiting_cartons_with_their_numbers(w):
    say("/camion")  # the fake list has no waiting carton while the form says 3: site summary, labelled as counts
    assert "Quanti" in w.last()["text"] and "N°" not in w.last()["text"] and "Olio &lt;extra&gt;" in w.last()["text"]
    w.cartons_after = w.cartons_after[:2] + [Carton("5", "12,00", "Olio <x>", "11:02", ""), Carton("3", "12,00", "Pasta", "11:00", ""),
                                             Carton("4", "11,50", "Pasta", "11:01", " ")]
    say("/camion")
    text = w.last()["text"]
    rows = text[text.index("<pre>"):text.index("</pre>")].splitlines()
    assert rows[0].split() == ["<pre>N°", "kg", "contenuto"]
    assert [r.split(maxsplit=2) for r in rows[1:]] == [["3", "12,00", "Pasta"], ["4", "11,50", "Pasta"], ["5", "12,00", "Olio &lt;x&gt;"]]


# ---------------- misc ----------------

GROUP = -1001
OTHER = 999


def gsay(text, uid=UID, message_id=None):
    bot.handle_update({"update_id": next(_ids), "message": {
        "message_id": message_id or next(_ids), "text": text, "chat": {"id": GROUP, "type": "supergroup"}, "from": {"id": uid}}})


def gtap(data, uid=UID, message_id=77):
    bot.handle_update({"update_id": next(_ids), "callback_query": {
        "id": str(next(_ids)), "data": data, "from": {"id": uid},
        "message": {"message_id": message_id, "chat": {"id": GROUP, "type": "supergroup"}}}})


@pytest.fixture
def g(w):
    """A group that is already logged in: the fixture's login moves from the user's row to the group's row."""
    login = fake_store.ROWS.pop(str(UID))
    login.pop("state")
    fake_store.ROWS[str(GROUP)] = {**login, "RowKey": str(GROUP)}
    return w


def mrow(uid=UID):
    """The row of a member of the group: only that member's conversation."""
    return fake_store.ROWS.get(f"{GROUP}:{uid}", {})


def test_group_flow_and_button_owner(g):
    gsay("/cartone", message_id=500)
    assert g.last()["chat_id"] == GROUP and g.datas()[0] == f"cat:18|{UID}" and g.datas()[-1] == f"cat:x|{UID}"
    gtap(f"cat:25|{UID}")
    assert mrow()["state"] == "await_kg" and "Pasta" in g.edits()[-1]["text"]
    gsay("12,5")
    assert g.added == [("25", 12.5, False)] and mrow()["state"] == "" and "registrato" in g.last()["text"]
    assert set(mrow()) <= bot.FLOW_KEYS | {"PartitionKey", "RowKey"}  # no login data in a member's row
    assert str(GROUP) not in fake_store.VERSIONS  # and a member's steps never rewrite the group's row


def test_group_login_is_shared_and_flows_are_not(g):
    gsay("/cartone", uid=OTHER)  # OTHER never logged in: the group did
    assert g.datas()[0] == f"cat:18|{OTHER}"
    gsay("/cartone")
    gtap(f"cat:26|{OTHER}", uid=OTHER)
    gtap(f"cat:25|{UID}")
    assert (mrow()["pending_cat"], mrow(OTHER)["pending_cat"]) == ("25", "26")
    gsay("12")
    gsay("9", uid=OTHER)
    assert g.added == [("25", 12.0, False), ("26", 9.0, False)]


def test_group_code_typed_by_one_member_logs_everybody_in(g):
    del fake_store.ROWS[str(GROUP)]["code_enc"], fake_store.ROWS[str(GROUP)]["code_set_at"]
    fake_store.ROWS[f"{GROUP}:{UID}"] = {"PartitionKey": "user", "RowKey": f"{GROUP}:{UID}", "state": "await_code"}
    gsay("/lista", uid=OTHER)
    assert "codice di accesso" in g.last()["text"] and g.sessions == 0
    gsay("SECRET")
    assert fake_store.ROWS[str(GROUP)]["code_enc"] == "enc:SECRET" and "code_enc" not in mrow()
    assert [p["scope"] for m, p in g.tg if m == "setMyCommands"] == [{"type": "chat", "chat_id": GROUP}]  # the group's menu
    fake_store.ROWS[f"{GROUP}:{OTHER}"]["state"] = ""
    gsay("/lista", uid=OTHER)
    assert "Arese/CONAD" in g.last()["text"]


def test_group_esci_logs_the_whole_group_out(g):
    gsay("/cartone")
    gsay("/cartone", uid=OTHER)
    gsay("/esci", uid=OTHER)
    assert "gruppo" in g.last()["text"] and not fake_store.ROWS
    assert [p["scope"] for m, p in g.tg if m == "deleteMyCommands"] == [{"type": "chat", "chat_id": GROUP}]
    gsay("/lista")
    assert g.sessions == 2 and mrow()["state"] == "await_prov"  # nobody is logged in any more


def test_group_does_not_touch_private_rows(g):
    fake_store.ROWS[str(UID)] = {"PartitionKey": "user", "RowKey": str(UID), "prov": "to", "city": "ivrea", "state": ""}
    before = dict(fake_store.ROWS[str(UID)])
    gsay("/cartone")
    gtap(f"cat:25|{UID}")
    assert fake_store.ROWS[str(UID)] == before


def test_group_foreign_tap_refused(g):
    gsay("/cartone")
    before = (len(g.tg), dict(mrow()))
    gtap(f"cat:25|{UID}", uid=OTHER)
    assert g.tg[before[0]:] == [("answerCallbackQuery", {"callback_query_id": g.tg[-1][1]["callback_query_id"],
                                                         "text": bot.NOT_YOURS})]
    assert mrow() == before[1] and not mrow(OTHER)
    gtap("cat:25")  # no suffix: works as today
    assert mrow()["state"] == "await_kg"


def test_group_ignores_chatter_without_state(g):
    gsay("ciao a tutti")
    gsay("12")
    gsay("MI")
    assert not g.sent() and not mrow()
    fake_store.ROWS.clear()
    gsay("ciao")
    assert not g.sent() and not fake_store.ROWS


def test_group_state_text_is_handled(g):
    gsay("/cartone")
    gtap(f"cat:25|{UID}")
    gsay("abc")
    assert "Peso non valido" in g.last()["text"]
    gsay("12", uid=OTHER)  # somebody else's chatter: ignored
    gsay("/annulla")
    gsay("12")  # state cleared: ignored
    assert len(g.sent()) == 4 and not g.added  # list, ask kg, invalid, cancelled


def test_group_unknown_command_ignored(g):
    gsay("/boh")
    gsay("/foo@whatever")
    assert not g.sent()


def test_group_command_suffix(g, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "tg", lambda method, **p: calls.append(method) or g.tg.append((method, p))
                        or ({"ok": True, "result": {"username": "MyColletteBot"}} if method == "getMe" else {}))
    bot._get_me.cache_clear()
    gsay("/cartone@OtherBot")
    assert not g.sent()
    gsay("/cartone@mycollettebot")
    assert len(g.sent()) == 1 and "Tonno" in g.last()["reply_markup"]["inline_keyboard"][0][0]["text"]
    gsay("/cartone")  # no suffix: no getMe
    assert calls.count("getMe") == 1  # cached
    bot._get_me.cache_clear()


def test_group_getme_failure_accepts_command(g):
    bot._get_me.cache_clear()
    gsay("/cartone@OtherBot")
    assert len(g.sent()) == 1  # fake tg returns {} for getMe
    bot._get_me.cache_clear()


def test_group_replies_and_private_unchanged(w):
    fake_store.ROWS[str(GROUP)] = {**fake_store.ROWS[str(UID)], "RowKey": str(GROUP)}
    gsay("/lista", message_id=321)
    assert w.last()["reply_parameters"] == {"message_id": 321, "allow_sending_without_reply": True}
    say("/cartone")  # context is fresh: nothing leaks from the group update
    assert "reply_parameters" not in w.last() and w.datas()[0] == "cat:18"
    assert [p["scope"] for m, p in w.tg if m == "setMyCommands"] == []
    say("/aiuto")
    assert [p["scope"] for m, p in w.tg if m == "setMyCommands"] == [{"type": "chat", "chat_id": CHAT}]


def test_lista_escapes_site_text(w):
    w.cartons_after = []
    say("/lista")
    assert "Nessun cartone" in w.last()["text"]
    msg = bot._fmt_cartons([Carton("1", "10,00", "Pasta", "10:00", "10:30"), Carton("2", "9,00", "Riso", "10:05", "")])
    assert "1     10,00 10:00 10:30  Pasta" in msg and "2      9,00 10:05   -    Riso" in msg  # normal list: unchanged
    msg = bot._fmt_cartons([Carton("<i>3", "1&2", "<u>x", "<a>", "<b>A&B"), Carton("4", "5", "Olio <e>", "10:00", "")])
    assert "<b>A&amp;B" not in msg and "&lt;b&gt;A&amp;B" in msg and "&lt;i&gt;3" in msg and "1&amp;2" in msg and "&lt;a&gt;" in msg
    assert "&lt;u&gt;x" in msg and "Olio &lt;e&gt;" in msg and msg.count("<pre>") == 1 and "<b>A" not in msg.replace("<b>2", "")


def test_lista_stato_and_help(w):
    say("/lista")
    assert "5 cartoni" in w.last()["text"]
    for cmd in ("/lista", "/stato"):
        say(cmd)
        assert 'href="https://www.itud.eu/gnca/mi/arese/">Apri il sito</a> – codice di accesso: <code>SECRET</code>' in w.last()["text"]
    say("/aiuto")
    assert "/camion" in w.last()["text"] and "/modifica" in w.last()["text"] and "/citta" not in w.last()["text"]


def test_failed_send_tells_the_user(w, monkeypatch):
    def refuse(method, **payload):
        w.tg.append((method, payload))
        return {"ok": False, "error_code": 400} if "<b>" in payload.get("text", "") else {"ok": True}

    monkeypatch.setattr(bot, "tg", refuse)
    say("/lista")
    assert w.last()["text"] == bot.SEND_FAILED and "parse_mode" not in w.last()


# ---------------- city / store / access code ----------------

def test_login_by_link_then_logout(new_user):
    w = new_user
    say("/aiuto")
    assert "/citta" in w.last()["text"] and "/cartone" not in w.last()["text"]
    say("ecco il link: http://itud.eu/gnca/MI/Arese")
    assert [(p, c.lower()) for p, c in w.asked] == [("mi", "arese")] and w.datas() == ["pv:493", "pv:954"]
    assert 'href="https://www.itud.eu/gnca/mi/arese/"' in w.last()["text"] and "codice di accesso:" not in w.last()["text"]
    tap("pv:493")
    assert w.row["state"] == "await_code" and "codice di accesso" in w.last()["text"]
    menus = lambda: [(m, [c["command"] for c in p.get("commands", [])], p["scope"]) for m, p in w.tg if "MyCommands" in m]
    w.tg.clear()
    say("SECRET")
    assert w.row["code_enc"] == "enc:SECRET" and w.row["state"] == ""
    assert menus() == [("setMyCommands", [c for c, _ in bot.COMMANDS_LOGGED], {"type": "chat", "chat_id": CHAT})]
    assert not [m for m, _ in w.tg if m == "deleteMessage"]  # the code message is left in the chat
    say("/esci")
    assert menus()[-1][0] == "deleteMyCommands" and str(UID) not in fake_store.ROWS


def test_citta_guided_province_then_city(new_user, monkeypatch):
    w = new_user

    def lookup(prov, city):
        w.asked.append((prov, city))
        if city != "Arese":
            raise bot.site.CityNotFound(city)
        return "arese", STORES

    monkeypatch.setattr(bot.site, "list_stores", lookup)
    say("/citta")
    assert w.row["state"] == "await_prov" and "sigla della provincia" in w.last()["text"]
    say("Milano")
    assert not w.asked and w.row["state"] == "await_prov" and "due lettere" in w.last()["text"]
    say("Mi")
    assert w.row["state"] == "await_city" and w.row["pending_prov"] == "mi" and "Provincia <b>MI</b>" in w.last()["text"]
    say("are<se")
    assert "Non trovo <b>are&lt;se</b>" in w.last()["text"] and w.row["state"] == "await_city" and w.row["pending_prov"] == "mi"
    say("Arese")
    assert w.asked == [("mi", "are<se"), ("mi", "Arese")] and w.datas() == ["pv:493", "pv:954"]
    assert (w.row["prov"], w.row["city"], w.row["state"], w.row["pending_prov"]) == ("mi", "arese", "", "")


def test_citta_shortcuts_and_link_at_any_step(new_user):
    w = new_user
    say("/citta")
    say("mi arese")  # both answers at once
    assert w.asked == [("mi", "arese")]
    say("/citta va")
    assert w.row["state"] == "await_city" and w.row["pending_prov"] == "va"
    say("https://www.itud.eu/gnca/mi/arese/")  # a link wins over the pending question
    assert w.asked[-1] == ("mi", "arese") and w.row["pending_prov"] == ""
    say("/citta boh")
    assert w.row["state"] == "await_prov" and "Non ho capito" in w.last()["text"]


def test_new_user_is_guided_from_the_start_and_can_cancel(new_user):
    w = new_user
    say("/start")
    assert "sigla della provincia" in w.last()["text"] and w.row["state"] == "await_prov"
    say("/annulla")
    assert w.row["state"] == ""
    say("https://evil.example/gnca/mi/arese/")  # links to other sites are just text
    assert not w.asked and "Prima scegli città" in w.last()["text"] and w.row["state"] == "await_prov"


def test_store_buttons(new_user, monkeypatch):
    w = new_user
    monkeypatch.setattr(bot.site, "list_stores", lambda prov, city: (city, STORES[:1]))
    say("/citta mi arese")  # a single store is picked automatically
    assert w.row["pv_id"] == "493" and w.row["state"] == "await_code"
    many = [(str(i), f"Negozio {i}") for i in range(120)]
    monkeypatch.setattr(bot.site, "list_stores", lambda prov, city: (city, many))
    say("/citta mi milano")  # many stores are split in several messages
    assert [len(w.datas(m)) for m in w.sent()[-3:]] == [50, 50, 20] and len(json.loads(w.row["stores_json"])) == 120
    tap("pv:119")
    assert w.row["pv_id"] == "119" and w.row["state"] == "await_code"


# ---------------- commands before / after the login ----------------

def test_command_lists_are_split_by_login():
    before, after, logged = set(dict(bot.COMMANDS)), set(dict(bot.COMMANDS_LOGGED)), {c[1:] for c in LOGGED_COMMANDS}
    assert {"citta", "codice"} <= before and not {"citta", "codice"} & after
    assert logged <= after and not logged & before and logged <= set(bot.LOGGED_ONLY)


def test_citta_and_codice_are_refused_after_login(w):
    before = copy.deepcopy(w.row)
    for cmd in ("/citta", "/citta mi arese", "/codice", "https://www.itud.eu/gnca/mi/arese/"):
        say(cmd)
        assert "/esci" in w.last()["text"] and "CONAD" in w.last()["text"] and w.row == before
    tap("pv:954")  # an old store button
    assert w.row == before


def test_logged_commands_before_login_continue_the_login(new_user):
    w = new_user
    for cmd in LOGGED_COMMANDS:  # no store yet: ask the province
        fake_store.ROWS.clear()
        say(cmd)
        assert not w.sessions and w.row["state"] == "await_prov" and "sigla della provincia" in w.last()["text"]
    for cmd in LOGGED_COMMANDS:  # store chosen, no code: ask the code (and /esci deletes nothing)
        fake_store.ROWS[str(UID)] = {"PartitionKey": "user", "RowKey": str(UID), "prov": "mi", "city": "arese", "pv_id": "493",
                                     "pv_label": "CONAD", "code_enc": "", "state": ""}
        say(cmd)
        assert not w.sessions and w.row["state"] == "await_code" and "codice di accesso" in w.last()["text"]
    say("/codice")  # available again
    assert "Scrivi qui il codice" in w.last()["text"]
    assert "https://www.itud.eu/gncaGestione" in w.last()["text"]  # where to find the code


# ---------------- WhatsApp channel ----------------

WA = "wa393331234567"


@pytest.fixture
def wa(w, monkeypatch):
    w.wa = []  # payloads sent to the Cloud API
    monkeypatch.setattr(bot.wa, "api", lambda payload: w.wa.append(payload) or True)
    fake_store.ROWS[WA] = {**fake_store.ROWS.pop(str(UID)), "RowKey": WA}
    return w


def wa_say(text):
    for u in bot.wa.updates({"entry": [{"changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "messages": [{"from": WA[2:], "id": f"wamid.{next(_ids)}", "type": "text",
                                                           "text": {"body": text}}]}}]}]}):
        bot.handle_update(u, channel="wa")


def wa_tap(data, kind="list_reply"):
    for u in bot.wa.updates({"entry": [{"changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "messages": [{"from": WA[2:], "id": f"wamid.{next(_ids)}", "type": "interactive",
                                                           "interactive": {"type": kind, kind: {"id": data, "title": "x"}}}]}}]}]}):
        bot.handle_update(u, channel="wa")


def wa_ids(payload):
    action = payload["interactive"]["action"]
    return ([b["reply"]["id"] for b in action["buttons"]] if "buttons" in action
            else [r["id"] for s in action["sections"] for r in s["rows"]])


def wa_body(payload):
    return payload["text"]["body"] if payload["type"] == "text" else payload["interactive"]["body"]["text"]


def test_whatsapp_carton_flow(wa):
    w = wa
    wa_say("cartone")  # no slash needed
    lists = w.wa
    assert [p["interactive"]["type"] for p in lists] == ["list", "list"] and all(p["to"] == WA[2:] for p in lists)
    assert [len(wa_ids(p)) for p in lists] == [10, 4]  # 13 categories + cancel: 10 rows per list message
    assert wa_ids(lists[0])[0] == "cat:18" and wa_ids(lists[1])[-1] == "cat:x"
    assert "Cartone n. *6*" in wa_body(lists[0]) and "<b>" not in wa_body(lists[0])
    row = lists[0]["interactive"]["action"]["sections"][0]["rows"][3]
    assert len(row["title"]) <= 24 and row["description"] == "🫘 Legumi/verdure in scatola"  # cut title, whole label below
    w.wa.clear()
    wa_tap("cat:25")
    assert [wa_body(p) for p in w.wa] == ["📦 Contenuto: *🍝 Pasta*", "Quanti kg pesa?"]  # a new message instead of an edit
    w.wa.clear()
    wa_say("12,5")
    assert w.added == [("25", 12.5, False)] and not w.tg  # nothing goes to Telegram
    done = w.wa[-1]
    assert done["interactive"]["type"] == "button" and wa_ids(done) == ["cmd:cartone", "cmd:lista"]
    w.wa.clear()
    wa_tap("cmd:lista", "button_reply")  # a command button works like the typed command
    assert "Arese/CONAD" in wa_body(w.wa[-1]) and "```" in wa_body(w.wa[-1])


def test_whatsapp_out_of_range_weight_has_reply_buttons(wa):
    w = wa
    wa_say("/cartone")
    wa_tap("cat:25")
    wa_say("40")
    assert not w.added and wa_ids(w.wa[-1]) == ["kg:ok", "cat:x"] and w.wa[-1]["interactive"]["type"] == "button"
    wa_tap("kg:ok", "button_reply")
    assert w.added == [("25", 40.0, True)]


def test_whatsapp_new_user_is_guided_from_the_first_message(new_user, monkeypatch):
    w = new_user
    w.wa = []
    monkeypatch.setattr(bot.wa, "api", lambda payload: w.wa.append(payload) or True)
    wa_say("Ciao!")
    assert "Bot Colletta Alimentare" in wa_body(w.wa[0]) and wa_ids(w.wa[0]) == ["cmd:citta", "cmd:codice", "cmd:annulla"]
    assert "sigla della provincia" in wa_body(w.wa[-1]) and fake_store.ROWS[WA]["state"] == "await_prov"
    wa_say("mi")
    wa_say("Arese")
    assert w.asked == [("mi", "Arese")] and wa_ids(w.wa[-1]) == ["pv:493", "pv:954"]
    assert w.wa[-1]["interactive"]["type"] == "list"  # store names are too long for reply buttons
    assert "Apri il sito: https://www.itud.eu/gnca/mi/arese/" in wa_body(w.wa[-1])
    wa_tap("pv:954")
    assert fake_store.ROWS[WA]["pv_id"] == "954" and fake_store.ROWS[WA]["state"] == "await_code"
    assert str(UID) not in fake_store.ROWS and not w.tg


def test_whatsapp_truck_needs_the_confirmation_button(wa):
    w = wa
    wa_say("camion")
    assert fake_store.ROWS[WA]["state"] == "await_plate" and wa_ids(w.wa[-1]) == ["trk:no"]
    wa_say("ab 123 cd")
    assert not w.loads and wa_ids(w.wa[-1]) == ["trk:all", "trk:part", "trk:no"]
    wa_tap("trk:all", "button_reply")
    assert [(plate, count) for _, plate, count in w.loads] == [("AB123CD", None)]
    wa_tap("trk:all", "button_reply")  # the old button is still there: tapping it again loads nothing
    assert len(w.loads) == 1 and "non è più valido" in wa_body(w.wa[-1])


def test_whatsapp_send_failure_tells_the_user(wa, monkeypatch):
    w = wa
    monkeypatch.setattr(bot.wa, "api", lambda payload: w.wa.append(payload) or payload["type"] == "text")
    wa_say("cartone")
    assert w.wa[-1]["type"] == "text" and "Non sono riuscito" in wa_body(w.wa[-1])


def test_telegram_is_unchanged_after_a_whatsapp_update(wa):
    w = wa
    wa_say("lista")
    fake_store.ROWS[str(UID)] = {**fake_store.ROWS[WA], "RowKey": str(UID)}
    n = len(w.wa)
    say("/lista")
    assert len(w.wa) == n and "<b>Arese/CONAD</b>" in w.last()["text"]
