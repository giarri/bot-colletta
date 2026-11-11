"""Conversation logic (Italian UI). Entry point: handle_update(update_dict)."""

import contextvars
import dataclasses
import functools
import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation

from urllib.parse import unquote

import requests

import site_client as site
import user_store as store

TG_TIMEOUT = 15
PLATE_LEN = 7  # characters of a vehicle plate (spaces removed)
EDIT_LIST_MAX = 40  # cartons offered at /modifica (the most recent ones)
TRUCK_LIST_MAX = 40  # cartons listed one by one at /camion
STORES_PER_MESSAGE = 50  # store buttons in one message (Telegram refuses keyboards that are too big)

EXAMPLE_URL = "https://www.itud.eu/gnca/mi/arese/"
# a link to the site's page of a city: .../gnca/<province>/<city>/ (only on the configured site, never other hosts)
SITE_URL_RE = re.compile(r"(?i)(?:https?://)?(?:www\.)?" + re.escape(re.sub(r"^https?://(www\.)?", "", site.BASE))
                         + r"/([a-z]{2})/([^/\s?#]+)")

HELP = (  # before the login (and after /esci)
    "🥫 <b>Bot Colletta Alimentare</b>\n\n"
    "<b>Per entrare</b>\n"
    "1️⃣ /citta – scegli provincia e città "
    f"(oppure incolla il link del sito della tua città, es. <code>{EXAMPLE_URL}</code>)\n"
    "2️⃣ Tocca il tuo punto vendita\n"
    "3️⃣ Scrivi il <b>codice di accesso</b> (/codice per riscriverlo)\n\n"
    "/annulla – annulla quello che stai facendo\n\n"
    "Il codice di accesso resta salvato al massimo 7 giorni."
)

HELP_LOGGED = (  # after the login
    "🥫 <b>Bot Colletta Alimentare</b>\n\n"
    "/cartone – registra un cartone\n"
    "/lista – vedi i cartoni registrati\n"
    "/camion – carica i cartoni sul camion (chiedo targa e conferma)\n"
    "/modifica – correggi contenuto e peso di un cartone non ancora sul camion\n"
    "/stato – vedi città, punto vendita e link al sito\n"
    "/annulla – annulla quello che stai facendo\n"
    "/esci – esci e cancella tutti i tuoi dati (serve anche per cambiare città, punto vendita o codice)\n\n"
    "Il codice di accesso resta salvato al massimo 7 giorni."
)

# Command menus. COMMANDS is the default one (set by deploy.py): what a user sees before the login.
# COMMANDS_LOGGED replaces it in the chat of a logged-in user (see _menu).
COMMANDS = [
    ("citta", "Scegli città e punto vendita"),
    ("codice", "Scrivi il codice di accesso"),
    ("annulla", "Annulla quello che stai facendo"),
    ("aiuto", "Istruzioni"),
]

COMMANDS_LOGGED = [
    ("cartone", "Registra un cartone"),
    ("lista", "Vedi i cartoni registrati"),
    ("camion", "Carica i cartoni sul camion"),
    ("modifica", "Correggi contenuto e peso di un cartone"),
    ("stato", "Vedi città, punto vendita e link al sito"),
    ("annulla", "Annulla quello che stai facendo"),
    ("esci", "Esci e cancella tutti i tuoi dati"),
    ("aiuto", "Istruzioni"),
]

# command -> canonical name; the first group works only after the login, the second only before it
LOGGED_ONLY = {"cartone": "cartone", "lista": "lista", "camion": "camion", "spedisci": "camion", "consegna": "camion",
               "modifica": "modifica", "stato": "stato", "esci": "esci", "logout": "esci"}
LOGIN_ONLY = {"citta": "citta", "città": "citta", "city": "citta", "codice": "codice"}

KNOWN_COMMANDS = {"start", "aiuto", "help", "annulla", *LOGGED_ONLY, *LOGIN_ONLY}

CANCEL = "❌ Annulla"
CANCELLED = "Operazione annullata."
ASK_PROV = ("Scrivi la <b>sigla della provincia</b>, es. <code>MI</code>\n")
NO_STORE = "Prima scegli città e punto vendita.\n"
PROV_RE = re.compile(r"[A-Za-z]{2}")
STALE_CARTON = "Questo pulsante non è più valido. Scrivi /cartone per ricominciare."
STALE_EDIT = "Questo pulsante non è più valido. Scrivi /modifica per ricominciare."
STALE_TRUCK = "Questo pulsante non è più valido. Scrivi /camion per ricominciare."
SEND_FAILED = "⚠️ Non sono riuscito a mostrarti la risposta. Riprova, oppure scrivi /annulla e ricomincia."
LOST_TRUCK = "Non trovo più i cartoni da caricare. Scrivi /camion per ricominciare."  # typed text, no button involved

# Emoji for a category, chosen by keyword at the start of a word of its label (first match wins).
# Categories come from the site and may change: unknown ones get CAT_EMOJI_DEFAULT.
CAT_EMOJI = [
    ("tonno", "🐟"), ("colazione", "☕"), ("passat", "🫙"), ("pomodoro", "🫙"), ("sugh", "🫙"), ("legumi", "🫘"), ("verdur", "🫘"),
    ("olio", "🫒"), ("omogeneizzat", "🍼"), ("infanzia", "👶"), ("pasta", "🍝"), ("riso", "🍚"),
    ("zucchero", "🍬"), ("latte", "🥛"), ("biscott", "🍪"), ("varie", "🛒"),
]
CAT_EMOJI_DEFAULT = "📦"


# ---------------- Telegram API ----------------

def tg(method: str, **payload) -> dict:
    token = os.environ["TELEGRAM_TOKEN"]
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=TG_TIMEOUT)
    if r.status_code != 200:
        logging.error("Telegram %s failed: %s %s", method, r.status_code, r.text[:300])
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"ok": r.status_code == 200}


@dataclasses.dataclass(frozen=True)
class Ctx:
    """Per-update context, set at the start of each update (a ContextVar: the host may run invocations concurrently)."""
    group: bool = False
    user_id: int = 0
    reply_to: int | None = None  # message the bot answers to (groups only)


_ctx: contextvars.ContextVar[Ctx] = contextvars.ContextVar("ctx", default=Ctx())
NOT_YOURS = "Questi pulsanti sono di un altro utente."


def _set_ctx(chat: dict, user_id: int, reply_to: int | None = None) -> bool:
    is_group = chat.get("type") in ("group", "supergroup")
    _ctx.set(Ctx(is_group, user_id, reply_to if is_group else None))  # always fresh: nothing leaks from the previous update
    return is_group


@functools.lru_cache(maxsize=1)
def _get_me() -> str:
    res = tg("getMe")
    name = (res.get("result") or {}).get("username") if isinstance(res, dict) else None
    if not name:
        raise LookupError("getMe failed")  # raising keeps lru_cache from caching a failure
    return name.lower()


def _bot_username() -> str | None:
    try:
        return _get_me()
    except Exception:
        return None  # getMe failed: the caller accepts the command


def send(chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
    ctx = _ctx.get()
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    if ctx.group and ctx.reply_to:
        payload["reply_parameters"] = {"message_id": ctx.reply_to, "allow_sending_without_reply": True}
    if buttons:
        suffix = f"|{ctx.user_id}" if ctx.group else ""  # in a group the buttons belong to one user
        payload["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d + suffix} for t, d in row]
                                                       for row in buttons]}
    res = tg("sendMessage", **payload)
    if isinstance(res, dict) and res.get("ok") is False and res.get("error_code") != 403:  # 403: the user blocked the bot
        tg("sendMessage", chat_id=chat_id, text=SEND_FAILED,
           **({"reply_parameters": payload["reply_parameters"]} if "reply_parameters" in payload else {}))  # never leave the user without an answer


def edit(chat_id: int, message_id: int, text: str) -> None:
    tg("editMessageText", chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML")


def drop_buttons(chat_id: int, message_id: int) -> None:
    tg("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})


def _menu(chat_id: int, logged: bool) -> None:
    """Show in this chat only the commands that work now (one Telegram call, done only when the status changes)."""
    scope = {"type": "chat", "chat_id": chat_id}  # also in a group: the login is shared, so is the menu
    if logged:
        tg("setMyCommands", scope=scope, commands=[{"command": c, "description": d} for c, d in COMMANDS_LOGGED])
    else:
        tg("deleteMyCommands", scope=scope)  # back to the default menu


# ---------------- rows ----------------
# Private chat: one row per user. Group: the login is shared, the conversation is not. One row for the group
# (RowKey = chat id: store, access code, learned limits, last plate) plus one per member (RowKey = "<chat id>:<user id>")
# with only FLOW_KEYS, so that two volunteers can register cartons at the same time without mixing them up.
# The handlers see a single merged dict; "_group" (never stored) is the group row as last read or written.

FLOW_KEYS = {"state", "pending_prov", "pending_cat", "pending_kg", "pending_edit", "pending_plate", "pending_count",
             "truck_json", "edit_json", "cats_json", "kg_min", "kg_max"}
ROW_META = {"PartitionKey", "RowKey", "_etag"}


def _load(user_id: int, chat_id: int, group: bool) -> dict:
    if not group:
        return store.get(user_id)
    g, m = store.get(chat_id), store.get(f"{chat_id}:{user_id}")
    row = {k: v for k, v in m.items() if k in FLOW_KEYS | ROW_META}
    row["_group"] = {k: v for k, v in g.items() if k not in FLOW_KEYS and k != "_etag"}
    row.update((k, v) for k, v in row["_group"].items() if k not in ROW_META)
    return row


def _split(row: dict) -> tuple[dict, dict]:
    """(member row, group row) to store for a merged group row."""
    member = {k: v for k, v in row.items() if k in FLOW_KEYS | ROW_META}
    shared = {k: v for k, v in row.items() if k not in FLOW_KEYS | ROW_META and k != "_group"}
    return member, {"PartitionKey": row["_group"]["PartitionKey"], "RowKey": row["_group"]["RowKey"], **shared}


def _save_shared(row: dict, shared: dict) -> None:
    if shared != row["_group"]:  # written only when the login data changed: a member's steps never overwrite it
        store.save(shared)
        shared.pop("_etag", None)
        row["_group"] = shared


def _save(row: dict) -> None:
    if "_group" not in row:
        store.save(row)
        return
    member, shared = _split(row)
    _save_shared(row, shared)
    store.save(member)
    row["_etag"] = member["_etag"]


def _claim(row: dict) -> bool:
    """store.claim() on the user's own row (in a group: the member's, where the conversation lives)."""
    if "_group" not in row:
        return store.claim(row)
    member, shared = _split(row)
    if not store.claim(member):
        return False
    row["_etag"] = member["_etag"]
    _save_shared(row, shared)
    return True


# Appended to every message that asks for the access code
CODE_HINT = "\nLo trovi su https://www.itud.eu/gncaGestione"


def _logged(row: dict) -> bool:
    return bool(row.get("pv_id") and row.get("code_enc"))


def _already_logged(chat_id: int, row: dict) -> None:
    send(chat_id, f"Sei già collegato a <b>{esc(row.get('pv_label', ''))}</b>.\n"
                  "Per cambiare città, punto vendita o codice scrivi prima /esci.")


def esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------- helpers ----------------

def _cat_emoji(label: str) -> str:
    low = str(label).lower()
    for keyword, emoji in CAT_EMOJI:
        if re.search(r"(?<![^\W\d_])" + re.escape(keyword), low):  # not in the middle of a word ("sorriso")
            return emoji
    return CAT_EMOJI_DEFAULT


def _cat_show(label: str) -> str:
    """Category label as shown to the user (HTML-escaped). Stored labels stay emoji-free."""
    return f"{_cat_emoji(label)} {esc(label)}"


def _cartoni(n, bold: bool = False) -> str:
    """'1 cartone', '3 cartoni'."""
    return (f"<b>{n}</b>" if bold else f"{n}") + (" cartone" if str(n) == "1" else " cartoni")


def _reset_flow(row: dict) -> None:
    """Forget any conversation in progress (carton or truck)."""
    row.update(state="", pending_cat="", pending_kg="", truck_json="", pending_plate="", pending_count="", pending_prov="",
               edit_json="", pending_edit="")


def _dec(value, default) -> Decimal:
    for v in (value, default):
        try:
            d = Decimal(str(v).strip().replace(",", "."))
        except InvalidOperation:
            continue
        if d.is_finite():
            return d
    return Decimal(0)


def _num(d: Decimal) -> str:
    """Italian formatting without useless zeros: 22.00 -> '22', 0.50 -> '0,5'."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def _cat_limits(row: dict) -> dict:
    """{cat_id: [min, max]} learned from the site's rejections (it only reveals them when it refuses a weight)."""
    try:
        d = json.loads(row.get("cat_limits_json") or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _kg_limits(row: dict) -> tuple[Decimal, Decimal]:
    """Expected (min, max) for the pending category: the site's weight input, narrowed by what it told us."""
    lo, hi = _dec(row.get("kg_min") or None, site.KG_MIN), _dec(row.get("kg_max") or None, site.KG_MAX)
    cat = _cat_limits(row).get(row.get("pending_cat") or "")
    if cat:
        lo, hi = max(lo, _dec(cat[0], lo)), min(hi, _dec(cat[1], hi))
    return lo, hi


def _kg_range(row: dict) -> str:
    lo, hi = _kg_limits(row)
    return f"tra {_num(lo)} e {_num(hi)} kg"


def _parse_kg(text: str) -> Decimal | None:
    t = re.sub(r"\s*kg\.?$", "", text.strip().lower()).replace(",", ".")
    if not re.fullmatch(r"[0-9]{1,4}(\.[0-9]{1,2})?", t) or Decimal(t) <= 0:
        return None
    return Decimal(t)


def _open_session(chat_id: int, row: dict) -> site.StoreSession | None:
    """Log in for this user, or explain what's missing. Returns None if not possible."""
    if not row.get("pv_id"):
        _ask_prov(chat_id, row, NO_STORE)
        return None
    code = store.get_code(row)
    if not code:
        why = ("Il codice di accesso è scaduto (lo conservo al massimo 7 giorni)." if row.get("code_expired")
               else "Manca il codice di accesso.")
        if row.get("code_enc"):  # stored but unreadable: no longer logged in
            store.clear_code(row)
        _reset_flow(row)
        row["state"] = "await_code"
        _save(row)
        _menu(chat_id, False)
        send(chat_id, f"{why}\nScrivi qui il codice di accesso di <b>{esc(row.get('pv_label', ''))}</b>." + CODE_HINT)
        return None
    try:
        return site.StoreSession(row["prov"], row["city"], row["pv_id"], code)
    except site.LoginFailed:
        store.clear_code(row)
        _reset_flow(row)
        row["state"] = "await_code"
        _save(row)
        _menu(chat_id, False)
        send(chat_id, "❌ Il codice di accesso non funziona più: forse è stato cambiato.\n"
                      "Scrivi qui il nuovo codice di accesso." + CODE_HINT)
        return None


def _fmt_cartons(cartons: list[site.Carton], limit: int = 40) -> str:
    if not cartons:
        return "Nessun cartone registrato. Scrivi /cartone per registrare il primo."
    total = 0.0
    for c in cartons:
        try:
            total += float(c.kg.replace(".", "").replace(",", "."))
        except ValueError:
            pass
    lines = [f"<b>{_cartoni(len(cartons))} – {total:.2f} kg</b>".replace(".", ","), "<pre>N°   kg     ora   camion contenuto"]
    for c in cartons[:limit]:
        # Site text: pad on the visible text first, then escape (so "<" or "&" can't break the HTML or the columns)
        num, kg, done, truck = (esc(f"{v:{f}}") for v, f in ((c.number, "<4"), (c.kg, ">6"), (c.done, "<5"), (c.truck or "  -  ", "<6")))
        lines.append(f"{num} {kg} {done} {truck} {esc(c.content)}")
    if len(cartons) > limit:
        lines.append(f"… altri {len(cartons) - limit}")
    lines.append("</pre>")
    return "\n".join(lines)


# ---------------- commands ----------------

def _site_link(row: dict) -> str:
    """Link to the city's site and, when logged in, the access code to type there (tap to copy)."""
    link = f'🔗 <a href="{esc(site.city_url(row["prov"], row["city"]))}">Apri il sito</a>'
    code = store.get_code(row) if row.get("code_enc") else None
    return link + (f" – codice di accesso: <code>{esc(code)}</code>" if code else "")


def _parse_city(text: str) -> tuple[str, str] | None:
    """(province, city) from a pasted site link or from 'mi arese'."""
    m = SITE_URL_RE.search(text)
    if m:
        return m.group(1).lower(), unquote(m.group(2))
    parts = text.split(maxsplit=1)
    if len(parts) == 2 and PROV_RE.fullmatch(parts[0]):
        return parts[0].lower(), parts[1]
    return None


def _ask_prov(chat_id: int, row: dict, intro: str = "") -> None:
    """Step 1 of choosing the city: ask the province."""
    _reset_flow(row)
    row["state"] = "await_prov"
    _save(row)
    send(chat_id, intro + ASK_PROV)


def _ask_city(chat_id: int, row: dict, prov: str, intro: str = "") -> None:
    """Step 2: the province is known, ask the city."""
    _reset_flow(row)
    row.update(state="await_city", pending_prov=prov.lower())
    _save(row)
    send(chat_id, intro + f"Provincia <b>{esc(prov.upper())}</b>. Ora scrivi il nome della <b>città</b>, es. <code>Arese</code>")


def cmd_citta(chat_id: int, row: dict, args: str) -> None:
    """Choose the city. Guided (province, then city); a pasted link, 'mi arese' or 'mi' skip the steps they answer."""
    where = _parse_city(args)
    if where:
        _set_city(chat_id, row, *where)
    elif PROV_RE.fullmatch(args):
        _ask_city(chat_id, row, args)
    else:
        _ask_prov(chat_id, row, "Non ho capito.\n" if args else "")


def receive_prov(chat_id: int, row: dict, text: str) -> None:
    where = _parse_city(text)  # "mi arese" answers both questions at once
    if where:
        _set_city(chat_id, row, *where)
    elif PROV_RE.fullmatch(text):
        _ask_city(chat_id, row, text)
    else:
        send(chat_id, "Mi servono solo le <b>due lettere</b> della provincia, es. <code>MI</code> per Milano. "
                      "Riscrivile, oppure scrivi /annulla.")


def receive_city(chat_id: int, row: dict, text: str) -> None:
    prov = row.get("pending_prov")
    if prov:
        _set_city(chat_id, row, prov, text)
    else:
        _ask_prov(chat_id, row)


def _set_city(chat_id: int, row: dict, prov: str, city: str) -> None:
    """Look the city up on the site and show its stores; not found = ask the city again."""
    try:
        slug, stores = site.list_stores(prov, city)
    except site.CityNotFound:
        _ask_city(chat_id, row, prov, f"⚠️ Non trovo <b>{esc(city[:60])}</b> in provincia di <b>{esc(prov.upper())}</b>.\n"
                                      "Riprova qui sotto, oppure scrivi /citta per cambiare provincia.\n\n")
        return
    _reset_flow(row)
    row.update(prov=prov, city=slug, pv_id="", pv_label="", cat_limits_json="", stores_json=json.dumps(dict(stores), ensure_ascii=False))
    store.clear_code(row)
    _save(row)
    head = f"📍 <b>{esc(slug.title())} ({esc(prov.upper())})</b> – {_site_link(row)}\n"
    if len(stores) == 1:  # nothing to choose
        send(chat_id, head.rstrip())
        _pick_store(chat_id, row, stores[0][0])
    else:
        rows = [[(label[:60], f"pv:{pv}")] for pv, label in stores]
        send(chat_id, head + "Tocca il tuo punto vendita:", rows[:STORES_PER_MESSAGE])
        for i in range(STORES_PER_MESSAGE, len(rows), STORES_PER_MESSAGE):
            send(chat_id, "Altri punti vendita:", rows[i:i + STORES_PER_MESSAGE])


def _pick_store(chat_id: int, row: dict, pv_id: str) -> None:
    label = json.loads(row.get("stores_json") or "{}")[pv_id]
    _reset_flow(row)
    row.update(pv_id=pv_id, pv_label=label, state="await_code")
    store.clear_code(row)
    _save(row)
    send(chat_id, f"🏪 Punto vendita: <b>{esc(label)}</b>\nOra scrivi qui il <b>codice di accesso</b>." + CODE_HINT)


def cmd_codice(chat_id: int, row: dict) -> None:
    if not row.get("pv_id"):
        _ask_prov(chat_id, row, NO_STORE)
        return
    _reset_flow(row)
    row["state"] = "await_code"
    _save(row)
    send(chat_id, f"Scrivi qui il codice di accesso di <b>{esc(row['pv_label'])}</b>." + CODE_HINT)


def receive_code(chat_id: int, row: dict, msg: dict) -> None:
    code = (msg.get("text") or "").strip()
    try:
        sess = site.StoreSession(row["prov"], row["city"], row["pv_id"], code)
    except site.LoginFailed:
        send(chat_id, "❌ Codice di accesso sbagliato. Riscrivilo, oppure scrivi /annulla." + CODE_HINT)
        return
    store.set_code(row, code)
    row["state"] = ""
    _save(row)
    _menu(chat_id, True)
    n = len(sess.cartons())
    send(chat_id, f"✅ Codice di accesso corretto: <b>{esc(sess.title())}</b>\n"
                  f"Cartoni già registrati: {n}\n\n"
                  "Scrivi /cartone per registrare un cartone.")


def _save_kg_limits(row: dict, sess: site.StoreSession) -> None:
    lo, hi = sess.kg_limits()  # must be read on the store page, i.e. right after login
    row.update(kg_min=str(lo), kg_max=str(hi))


def cmd_cartone(chat_id: int, row: dict) -> None:
    sess = _open_session(chat_id, row)
    if not sess:
        return
    cats = sess.categories()
    if not cats:
        send(chat_id, "⚠️ Non riesco a leggere dal sito l'elenco dei contenuti. Riprova tra qualche minuto con /cartone.")
        return
    _reset_flow(row)
    _save_kg_limits(row, sess)
    row.update(cats_json=json.dumps(dict(cats), ensure_ascii=False))
    _save(row)
    buttons = [[(f"{_cat_emoji(lbl)} {lbl}", f"cat:{cid}") for cid, lbl in cats[i:i + 2]] for i in range(0, len(cats), 2)]
    buttons.append([(CANCEL, "cat:x")])
    send(chat_id, f"📦 Cartone n. <b>{sess.next_number() or '?'}</b>\nCosa contiene? Tocca un pulsante:", buttons)


def _cat_label(row: dict) -> str:
    cat = row.get("pending_cat")
    return json.loads(row.get("cats_json") or "{}").get(cat, cat)


def _warn_kg(chat_id: int, row: dict, kg: Decimal) -> None:
    """Out of the expected range: never refuse, just warn and let the user confirm (as the site does)."""
    row["pending_kg"] = str(kg)
    _save(row)
    send(chat_id, f"⚠️ <b>{_num(kg)} kg</b> è fuori dai limiti per <b>{_cat_show(_cat_label(row))}</b> ({_kg_range(row)}).\n"
                  "Se il peso è giusto, tocca «Registra comunque». Altrimenti scrivi il peso corretto.",
         [[("✅ Registra comunque", "kg:ok")], [(CANCEL, "cat:x")]])


def receive_kg(chat_id: int, row: dict, text: str) -> None:
    kg = _parse_kg(text)
    if kg is None:
        send(chat_id, "Peso non valido. Scrivi solo il numero dei kg, es. <code>12,5</code>, oppure scrivi /annulla.")
        return
    lo, hi = _kg_limits(row)
    if lo <= kg <= hi:
        _submit_carton(chat_id, row, kg)
    else:
        _warn_kg(chat_id, row, kg)


def _submit_carton(chat_id: int, row: dict, kg: Decimal, confirm: bool = False) -> None:
    cat = row.get("pending_cat")
    sess = _open_session(chat_id, row)
    if not sess:
        return
    # Close the flow before the POST: a double tap or a Telegram retry handled meanwhile finds nothing to
    # submit (or loses the claim), and after a network error the user cannot blindly send the weight again.
    _reset_flow(row)
    if not _claim(row):
        return  # another update is already registering this carton and will answer
    try:
        c = sess.add_carton(cat, float(kg), confirm=confirm)
    except site.WeightRejected as e:  # per-category limits, known only to the server: remember them
        limits = _cat_limits(row)
        limits[cat] = [str(e.lo), str(e.hi)]
        row.update(cat_limits_json=json.dumps(limits), state="await_kg", pending_cat=cat)  # nothing saved: back to the weight
        _warn_kg(chat_id, row, kg)
        return
    except (site.SiteError, requests.RequestException) as e:  # e.g. a timeout: the carton may be there or not
        logging.exception("add_carton failed")
        send(chat_id, f"⚠️ Il sito non ha confermato il cartone. Scrivi /lista per controllare se è stato registrato "
                      f"prima di riprovare con /cartone.\n<i>{esc(str(e)[:300])}</i>")
        return
    send(chat_id, f"✅ Cartone n. <b>{esc(c.number)}</b> registrato: {_cat_show(c.content)}, {esc(c.kg)} kg (ore {esc(c.done)})\n"
                  "Scrivi /cartone per il prossimo, /lista per vederli tutti.")


# ---------------- edit a carton (site's Utility tab) ----------------

def _edits(row: dict) -> dict:
    """{site id: [number, content, kg]} offered at the last /modifica."""
    try:
        d = json.loads(row.get("edit_json") or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _kg_show(kg) -> str:
    """'12.00' / '12,50' as shown by the site -> '12' / '12,5'."""
    return _num(_dec(kg, 0))


def cmd_modifica(chat_id: int, row: dict) -> None:
    sess = _open_session(chat_id, row)
    if not sess:
        return
    form = sess.edit_form()
    _reset_flow(row)
    if not form:
        _save(row)
        send(chat_id, "Nessun cartone da modificare: si possono correggere solo quelli non ancora caricati sul camion.")
        return
    cartons = sorted(form.cartons, key=lambda c: int(c[1]), reverse=True)[:EDIT_LIST_MAX]  # most recent first
    row.update(edit_json=json.dumps({cid: [nr, content, kg] for cid, nr, content, kg in cartons}, ensure_ascii=False),
               cats_json=json.dumps(dict(form.categories), ensure_ascii=False), kg_min=str(form.kg_min), kg_max=str(form.kg_max))
    _save(row)
    buttons = [[(f"N. {nr} – {content}, {_kg_show(kg)} kg"[:60], f"ed:{cid}")] for cid, nr, content, kg in cartons]
    send(chat_id, "✏️ <b>Modifica cartone</b>\nQuale cartone vuoi correggere? Tocca un pulsante:", buttons + [[(CANCEL, "ed:x")]])


def edit_callback(chat_id: int, row: dict, message_id: int, kind: str, value: str) -> None:
    """ed:<carton> -> choose the new content (ec:<category>) -> new weight (typed, or ek:same)."""
    if value == "x":
        if row.get("prov") or row.get("state"):
            _reset_flow(row)
            _save(row)
        edit(chat_id, message_id, CANCELLED)
        return
    edits, cats = _edits(row), json.loads(row.get("cats_json") or "{}")
    carton = edits.get(value if kind == "ed" else row.get("pending_edit") or "")
    if not carton or (kind == "ec" and value not in cats) or (kind == "ek" and (value != "same" or row.get("pending_cat") not in cats)):
        send(chat_id, STALE_EDIT)
        return
    nr, content, kg = carton
    if kind == "ed":
        row.update(pending_edit=value, pending_cat="", state="")
        _save(row)
        edit(chat_id, message_id, f"✏️ Cartone n. <b>{esc(nr)}</b>: {_cat_show(content)}, {_kg_show(kg)} kg")
        same = [[(f"↩️ Lascia {content}"[:60], f"ec:{cid}")] for cid, lbl in cats.items() if lbl == content][:1]
        others = [(f"{_cat_emoji(lbl)} {lbl}", f"ec:{cid}") for cid, lbl in cats.items() if lbl != content]
        send(chat_id, "Qual è il contenuto giusto? Tocca un pulsante:",
             same + [others[i:i + 2] for i in range(0, len(others), 2)] + [[(CANCEL, "ed:x")]])
    elif kind == "ec":
        row.update(pending_cat=value, state="await_edit_kg")
        _save(row)
        edit(chat_id, message_id, f"📦 Contenuto: <b>{_cat_show(cats[value])}</b>")
        send(chat_id, "Qual è il peso giusto? Scrivi il numero dei kg, es. <code>12,5</code>",
             [[(f"↩️ Lascia {_kg_show(kg)} kg", "ek:same")], [(CANCEL, "ed:x")]])
    else:
        drop_buttons(chat_id, message_id)
        _submit_edit(chat_id, row, _dec(kg, 0))


def receive_edit_kg(chat_id: int, row: dict, text: str) -> None:
    kg = _parse_kg(text)
    lo, hi = _dec(row.get("kg_min") or None, site.KG_MIN), _dec(row.get("kg_max") or None, site.KG_MAX)
    if kg is None or not lo <= kg <= hi:
        send(chat_id, f"Peso non valido: scrivi un numero tra {_num(lo)} e {_num(hi)}, es. <code>12,5</code>, oppure scrivi /annulla.")
        return
    _submit_edit(chat_id, row, kg)


def _submit_edit(chat_id: int, row: dict, kg: Decimal) -> None:
    cid, cat, saved = row.get("pending_edit") or "", row.get("pending_cat") or "", row.get("edit_json") or ""
    carton = _edits(row).get(cid)
    if not carton or not cat or kg <= 0:
        _reset_flow(row)
        _save(row)
        send(chat_id, STALE_EDIT)
        return
    if _cat_label(row) == carton[1] and _dec(carton[2], 0) == kg:  # nothing changes: don't bother the site
        _reset_flow(row)
        _save(row)
        send(chat_id, f"ℹ️ Nessuna modifica: il cartone n. <b>{esc(carton[0])}</b> resta {_cat_show(carton[1])}, {_kg_show(carton[2])} kg.")
        return
    sess = _open_session(chat_id, row)
    if not sess:
        return
    _reset_flow(row)  # close the flow before the POST, as in _submit_carton
    if not _claim(row):
        return  # another update is already sending this change and will answer
    try:
        c = sess.edit_carton(cid, cat, float(kg))
    except site.WeightRejected as e:  # refused for this content: back to the weight question
        row.update(state="await_edit_kg", pending_edit=cid, pending_cat=cat, edit_json=saved)
        _save(row)
        send(chat_id, f"⚠️ Il sito non accetta <b>{_num(kg)} kg</b> per <b>{_cat_show(_cat_label(row))}</b>: "
                      f"deve essere tra {_num(_dec(e.lo, 0))} e {_num(_dec(e.hi, 0))} kg.\nScrivi un altro peso, oppure scrivi /annulla.")
        return
    except (site.SiteError, requests.RequestException) as e:
        logging.exception("edit_carton failed")
        send(chat_id, f"⚠️ Il sito non ha confermato la modifica. Scrivi /lista per controllare il cartone n. {esc(carton[0])}.\n"
                      f"<i>{esc(str(e)[:300])}</i>")
        return
    send(chat_id, f"✏️ Cartone n. <b>{esc(c.number)}</b> modificato: {_cat_show(c.content)}, {esc(c.kg)} kg\n"
                  f"<i>Prima: {esc(carton[1])}, {_kg_show(carton[2])} kg</i>")


def cmd_lista(chat_id: int, row: dict) -> None:
    sess = _open_session(chat_id, row)
    if sess:
        send(chat_id, f"<b>{esc(sess.title())}</b>\n" + _fmt_cartons(sess.cartons()) + "\n" + _site_link(row))


def cmd_stato(chat_id: int, row: dict) -> None:
    if not row.get("prov"):
        _ask_prov(chat_id, row, "Non hai ancora scelto la città.\n")
        return
    exp = store.expires_at(row) if row.get("code_enc") else None
    code = f"salvato, scade il {exp:%d/%m/%Y %H:%M} UTC" if exp else "non salvato (scrivi /codice per inserirlo)"
    send(chat_id, f"Città: <b>{esc(row['prov'].upper())} / {esc(row['city'].title())}</b>\n"
                  f"Punto vendita: <b>{esc(row.get('pv_label') or 'non scelto')}</b>\n{_site_link(row)}")


# ---------------- truck (site's Utility tab) ----------------

def _truck(row: dict) -> dict | None:
    """The TruckForm saved at /camion time (as a dict), or None."""
    try:
        form = json.loads(row.get("truck_json") or "null")
    except ValueError:
        return None
    return form if isinstance(form, dict) and "pending" in form else None


def cmd_camion(chat_id: int, row: dict) -> None:
    sess = _open_session(chat_id, row)
    if not sess:
        return
    # the cartons still to load, with their real numbers: read on the store page, i.e. before truck_form()
    waiting = sorted((c for c in sess.cartons() if not (c.truck or "").strip()),
                     key=lambda c: int(c.number) if c.number.isdigit() else 0)
    form = sess.truck_form()
    _reset_flow(row)
    if not form:
        _save(row)
        send(chat_id, "Nessun cartone da caricare sul camion.")
        return
    f = dataclasses.asdict(form)
    row.update(truck_json=json.dumps(f, ensure_ascii=False), state="await_plate")
    _save(row)
    lines = ["🚚 <b>Carico sul camion</b>",
             f"Da caricare: {_cartoni(f['pending'], bold=True)} (dal n. {esc(f['first'])} al n. {esc(f['last'])}), "
             f"<b>{esc(f['total_kg'])} kg</b> in tutto.",
             f"Viaggio n. <b>{esc(f['trip'])}</b>"]
    if len(waiting) == f["pending"]:  # one line per carton
        lines.append("<pre>N°   kg      contenuto")
        lines += [f"{esc(c.number):<4} {esc(c.kg):>7} {esc(c.content)}" for c in waiting[:TRUCK_LIST_MAX]]
        if len(waiting) > TRUCK_LIST_MAX:
            lines.append(f"… altri {len(waiting) - TRUCK_LIST_MAX}")
        lines.append("</pre>")
    elif f["summary"]:  # the list does not match the form: fall back to the site's summary (cartons per content)
        lines.append("<pre>Quanti  kg      contenuto")
        lines += [f"{esc(nr):<7} {esc(kg):>7} {esc(prod)}" for nr, prod, kg in f["summary"]]
        lines.append("</pre>")
    last = row.get("last_plate")
    lines.append("Scrivi la <b>targa del camion</b>"
                 + (", oppure tocca la targa già usata." if last else "."))
    send(chat_id, "\n".join(lines), ([[(f"🚚 {last}", "trk:last")]] if last else []) + [[(CANCEL, "trk:no")]])


def receive_plate(chat_id: int, row: dict, text: str) -> None:
    form = _truck(row)
    if not form:
        _reset_flow(row)
        _save(row)
        send(chat_id, LOST_TRUCK)
        return
    plate = "".join(text.split()).upper()
    if not re.fullmatch(r"[A-Z0-9]{%d}" % PLATE_LEN, plate):
        if row.get("state") != "await_plate":  # stale "last plate" button: keep waiting for a plate
            row["state"] = "await_plate"
            _save(row)
        send(chat_id, f"Targa non valida: deve avere {PLATE_LEN} caratteri (solo lettere e numeri), es. <code>AB123CD</code>. "
                      "Riscrivila, oppure scrivi /annulla.")
        return
    row.update(pending_plate=plate, pending_count="", state="")
    _save(row)
    n = form["pending"]
    buttons = [[(f"✅ Carica tutti ({n})", "trk:all")]]
    if form["count_max"] > 1:
        buttons.append([("🔢 Solo una parte", "trk:part")])
    send(chat_id, f"Carico {_cartoni(n, bold=True)} (dal n. {esc(form['first'])} al n. {esc(form['last'])}, "
                  f"{esc(form['total_kg'])} kg) sul camion <b>{esc(plate)}</b>, viaggio n. {esc(form['trip'])}?\n"
                  "Tocca un pulsante per confermare.", buttons + [[(CANCEL, "trk:no")]])


def receive_truck_count(chat_id: int, row: dict, text: str) -> None:
    form, plate = _truck(row), row.get("pending_plate")
    if not form or not plate:
        _reset_flow(row)
        _save(row)
        send(chat_id, LOST_TRUCK)
        return
    t = text.strip()
    if not re.fullmatch(r"[0-9]{1,6}", t) or not form["count_min"] <= int(t) <= form["count_max"]:
        send(chat_id, f"Numero non valido. Scrivi un numero da {form['count_min']} a {form['count_max']}, oppure scrivi /annulla.")
        return
    row.update(pending_count=str(int(t)), state="")
    _save(row)
    send(chat_id, f"Carico <b>{int(t)}</b> dei {_cartoni(form['pending'])} sul camion <b>{esc(plate)}</b>, "
                  f"viaggio n. {esc(form['trip'])}?\nTocca «Conferma».", [[("✅ Conferma", "trk:ok")], [(CANCEL, "trk:no")]])


def _truck_submit(chat_id: int, row: dict, message_id: int, saved: dict, plate: str, count: int | None) -> None:
    """The irreversible step: only reached from a confirmation button."""
    sess = _open_session(chat_id, row)
    if not sess:
        return
    form = sess.truck_form()
    if form is None or any(str(getattr(form, k)) != str(saved.get(k)) for k in ("pending", "max_carton", "trip")):
        _reset_flow(row)
        _save(row)
        edit(chat_id, message_id, "⚠️ Carico non fatto.")
        send(chat_id, "⚠️ Sul sito i cartoni sono cambiati nel frattempo (ne sono stati aggiunti o caricati altri). "
                      "Non ho caricato nulla. Scrivi /camion per ricominciare.")
        return
    _reset_flow(row)  # close the flow before the POST, as in _submit_carton
    if not _claim(row):
        return  # another update is already loading these cartons and will answer
    try:
        cartons = sess.load_truck(form, plate, count)
    except ValueError as e:
        edit(chat_id, message_id, "⚠️ Carico non fatto.")
        send(chat_id, f"⚠️ Il sito non accetta questo numero di cartoni. Non ho caricato nulla. "
                      f"Scrivi /camion per ricominciare.\n"
                      f"<i>{esc(str(e)[:300])}</i>")
        return
    except (site.SiteError, requests.RequestException) as e:  # e.g. a timeout: the load may be done or not
        logging.exception("load_truck failed")
        edit(chat_id, message_id, "⚠️ Carico non confermato.")
        send(chat_id, f"⚠️ Il sito non ha confermato il carico. Scrivi /lista per controllare se i cartoni sono sul camion.\n"
                      f"<i>{esc(str(e)[:300])}</i>")
        return
    row["last_plate"] = plate
    _save(row)
    done = count if count is not None else form.pending
    left = sum(1 for c in cartons if not (c.truck or "").strip())
    edit(chat_id, message_id, f"🚚 Camion <b>{esc(plate)}</b> – viaggio n. {esc(form.trip)}")
    send(chat_id, f"✅ {'Caricato' if done == 1 else 'Caricati'} {_cartoni(done, bold=True)} sul camion {esc(plate)} "
                  f"(viaggio n. {esc(form.trip)}).\n"
                  + (f"Restano da caricare: {_cartoni(left)}." if left else "Non restano cartoni da caricare.")
                  + "\nScrivi /lista per vederli tutti.")


def truck_callback(chat_id: int, row: dict, message_id: int, value: str) -> None:
    if value == "no":
        if row.get("prov") or row.get("state"):
            _reset_flow(row)
            _save(row)
        edit(chat_id, message_id, CANCELLED)
        return
    form, plate = _truck(row), row.get("pending_plate")
    if value == "last":
        if not form or not row.get("last_plate"):
            send(chat_id, STALE_TRUCK)
            return
        drop_buttons(chat_id, message_id)
        receive_plate(chat_id, row, row["last_plate"])
    elif value == "part":
        if not form or not plate or form["count_max"] <= 1:
            send(chat_id, STALE_TRUCK)
            return
        row.update(state="await_truck_count", pending_count="")
        _save(row)
        edit(chat_id, message_id, f"🔢 Carico solo una parte – camion <b>{esc(plate)}</b>, viaggio n. {esc(form['trip'])}")
        send(chat_id, f"Quanti cartoni carico? Scrivi un numero da {form['count_min']} a {form['count_max']}.")
    elif value in ("all", "ok"):
        count = row.get("pending_count") or ""
        if not form or not plate or (value == "ok" and not count.isdigit()):
            send(chat_id, STALE_TRUCK)
            return
        _truck_submit(chat_id, row, message_id, form, plate, int(count) if value == "ok" else None)


# ---------------- dispatch ----------------

def handle_message(msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    user_id = (msg.get("from") or {}).get("id", 0)
    group = _set_ctx(msg["chat"], user_id, msg.get("message_id"))
    if not text or not user_id:
        return
    row = _load(user_id, chat_id, group)

    if text.startswith("/"):
        cmd, _, args = text.partition(" ")
        cmd, _, target = cmd[1:].partition("@")
        cmd = cmd.lower()
        if group:
            if target and (own := _bot_username()) and target.lower() != own:
                return  # addressed to another bot
            if cmd not in KNOWN_COMMANDS:
                return  # maybe for another bot
        args = args.strip()
        logged = _logged(row)
        if cmd in ("start", "aiuto", "help"):
            _menu(chat_id, logged)
            send(chat_id, HELP_LOGGED if logged else HELP)
            if cmd == "start" and not row.get("pv_id"):  # new user: begin the setup right away
                _ask_prov(chat_id, row)
        elif cmd == "annulla":
            if row.get("prov") or row.get("state"):
                _reset_flow(row)
                _save(row)
            send(chat_id, CANCELLED)
        elif cmd in LOGGED_ONLY and not logged:
            _open_session(chat_id, row)  # not logged in: it never reaches the site, it asks for what is missing
        elif cmd in LOGIN_ONLY and logged:
            _already_logged(chat_id, row)
        elif LOGIN_ONLY.get(cmd) == "citta":
            cmd_citta(chat_id, row, args)
        elif LOGIN_ONLY.get(cmd) == "codice":
            cmd_codice(chat_id, row)
        elif LOGGED_ONLY.get(cmd) == "cartone":
            cmd_cartone(chat_id, row)
        elif LOGGED_ONLY.get(cmd) == "lista":
            cmd_lista(chat_id, row)
        elif LOGGED_ONLY.get(cmd) == "camion":
            cmd_camion(chat_id, row)
        elif LOGGED_ONLY.get(cmd) == "modifica":
            cmd_modifica(chat_id, row)
        elif LOGGED_ONLY.get(cmd) == "stato":
            cmd_stato(chat_id, row)
        elif LOGGED_ONLY.get(cmd) == "esci":
            if group:  # the login is the group's: everybody is out
                store.delete(chat_id)
                store.delete_members(chat_id)
            else:
                store.delete(user_id)
            _menu(chat_id, False)
            send(chat_id, "🗑️ Ho scollegato il gruppo e cancellato tutti i suoi dati. Per rientrare scrivete /citta." if group
                 else "🗑️ Sei uscito e ho cancellato tutti i tuoi dati. Per rientrare scrivi /citta.")
        else:
            send(chat_id, "Non conosco questo comando. Scrivi /aiuto per vedere cosa puoi fare.")
        return

    state = row.get("state")
    if group and not state:
        return  # ordinary group chatter
    if SITE_URL_RE.search(text):  # a pasted site link (re)starts the setup, like /citta: only before the login
        if _logged(row):
            _already_logged(chat_id, row)
        else:
            cmd_citta(chat_id, row, text)
    elif state == "await_prov":
        receive_prov(chat_id, row, text)
    elif state == "await_city":
        receive_city(chat_id, row, text)
    elif state == "await_code":
        receive_code(chat_id, row, msg)
    elif state == "await_kg":
        receive_kg(chat_id, row, text)
    elif state == "await_plate":
        receive_plate(chat_id, row, text)
    elif state == "await_edit_kg":
        receive_edit_kg(chat_id, row, text)
    elif state == "await_truck_count":
        receive_truck_count(chat_id, row, text)
    else:
        if row.get("pv_id"):
            send(chat_id, "Non ho capito. Scrivi /cartone per registrare un cartone, oppure /aiuto per le istruzioni.")
        else:
            _ask_prov(chat_id, row, NO_STORE)


def handle_callback(cq: dict) -> None:
    msg = cq.get("message") or {}
    group = _set_ctx(msg.get("chat") or {}, cq["from"]["id"])
    data, _, owner = (cq.get("data") or "").rpartition("|")
    if not owner.isdigit():  # no owner suffix (private chats, old messages)
        data, owner = cq.get("data") or "", ""
    if owner and int(owner) != cq["from"]["id"]:
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=NOT_YOURS)
        return
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    chat_id = msg.get("chat", {}).get("id")
    if not chat_id:
        return
    row = _load(cq["from"]["id"], chat_id, group)
    kind, _, value = data.partition(":")

    if kind == "pv":
        if _logged(row):  # an old store button: the store can only be changed after /esci
            _already_logged(chat_id, row)
            return
        stores = json.loads(row.get("stores_json") or "{}")
        if value not in stores:
            _ask_prov(chat_id, row, "Questo pulsante non è più valido.\n")
            return
        drop_buttons(chat_id, msg["message_id"])
        _pick_store(chat_id, row, value)
    elif kind == "cat":
        if value == "x":
            if row.get("prov") or row.get("state"):
                _reset_flow(row)
                _save(row)
            edit(chat_id, msg["message_id"], CANCELLED)
            return
        cats = json.loads(row.get("cats_json") or "{}")
        if value not in cats:
            send(chat_id, STALE_CARTON)
            return
        _reset_flow(row)
        row.update(pending_cat=value, state="await_kg")
        _save(row)
        edit(chat_id, msg["message_id"], f"📦 Contenuto: <b>{_cat_show(cats[value])}</b>")
        send(chat_id, "Quanti kg pesa?")
    elif kind == "kg":
        kg = _parse_kg(row.get("pending_kg") or "")
        if value != "ok" or row.get("state") != "await_kg" or not row.get("pending_cat") or kg is None:
            send(chat_id, STALE_CARTON)
            return
        edit(chat_id, msg["message_id"], f"⚖️ Peso confermato: <b>{_num(kg)} kg</b>")
        _submit_carton(chat_id, row, kg, confirm=True)
    elif kind in ("ed", "ec", "ek"):
        edit_callback(chat_id, row, msg["message_id"], kind, value)
    elif kind == "trk":
        truck_callback(chat_id, row, msg["message_id"], value)


def handle_update(update: dict) -> None:
    if "message" in update:
        handle_message(update["message"])
    elif "callback_query" in update:
        handle_callback(update["callback_query"])
