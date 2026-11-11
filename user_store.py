"""Per-Telegram-user state in Azure Table Storage (managed identity).

Row: PartitionKey="user", RowKey=<telegram user id>
In a group chat the login is shared (see bot.py, "rows"): RowKey=<chat id> holds everything but the conversation,
RowKey="<chat id>:<user id>" holds each member's conversation (state, pending_*, *_json of the flow, kg_min/kg_max).
  prov, city, pv_id, pv_label     store selection
  stores_json                     {pv_id: label} offered at the last /citta
  cats_json                       {cat_id: label} offered at the last /cartone
  code_enc, code_set_at           access code (Fernet-encrypted) + ISO timestamp (UTC)
  state                           "", "await_prov", "await_city", "await_code", "await_kg", "await_plate", "await_truck_count",
                                  "await_edit_kg"
  pending_prov                    province typed at /citta, waiting for the city name
  pending_cat                     category chosen, waiting for kg
  cat_limits_json                 {cat_id: [min, max]} per-category weight limits learned from site rejections
  kg_min, kg_max                  limits of the site's weight input, read at the last /cartone (strings)
  pending_kg                      out-of-range weight waiting for the user's confirmation
  edit_json                       {site id: [number, content, kg]} cartons offered at the last /modifica
  pending_edit                    site id of the carton being edited (its new content goes in pending_cat)
  truck_json                      site_client.TruckForm (JSON) shown at the last /camion, "" when no load in progress
  pending_plate                   vehicle plate typed for the load, waiting for confirmation
  pending_count                   cartons to load when only a part is delivered ("" = all pending)
  last_plate                      plate of the last confirmed load (offered again as a button)
Access codes are dropped after CRED_TTL_DAYS (default 7). An expired code leaves code_enc="" with code_set_at
still set: that is how the bot can say "expired" instead of "missing".

Rows carry two keys that are never stored: code_expired, and _etag (the row version read from the table, used
by claim() so that two updates handled at the same time cannot both run the same irreversible action).
"""

import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError, ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode
from azure.identity import DefaultAzureCredential
from cryptography.fernet import Fernet, InvalidToken

TABLE = "botusers"
PK = "user"
TTL = timedelta(days=int(os.environ.get("CRED_TTL_DAYS", "7")))
TRANSIENT = ("code_expired", "_etag")  # never written to the table


@lru_cache(maxsize=1)
def _table():
    svc = TableServiceClient(endpoint=os.environ["TABLES_ENDPOINT"], credential=DefaultAzureCredential())
    return svc.create_table_if_not_exists(TABLE)


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(os.environ["CRED_KEY"].encode())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get(user_id: int | str) -> dict:
    try:
        entity = _table().get_entity(PK, str(user_id))
    except ResourceNotFoundError:
        return {"PartitionKey": PK, "RowKey": str(user_id)}
    e = dict(entity)
    e["_etag"] = (entity.metadata or {}).get("etag") or ""
    if e.get("code_enc") and is_expired(e):
        e["code_enc"] = ""  # code_set_at stays, to remember that there was a code and it expired
        save(e)
    if not e.get("code_enc") and e.get("code_set_at"):
        e["code_expired"] = True
    return e


def _stored(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in TRANSIENT}


def save(row: dict) -> None:
    row["_etag"] = _table().upsert_entity(_stored(row), mode=UpdateMode.REPLACE).get("etag") or ""


def claim(row: dict) -> bool:
    """Save the row only if nobody changed it since it was read. False = another update got there first.

    Call it (with the flow already reset in `row`) right before an irreversible action: of two updates handled
    at the same time (double tap, Telegram retry) only one wins. Same single write as save(), no extra storage.
    """
    etag = row.get("_etag")
    if not etag:  # new row, never read from the table
        save(row)
        return True
    try:
        res = _table().update_entity(_stored(row), mode=UpdateMode.REPLACE, etag=etag,
                                     match_condition=MatchConditions.IfNotModified)
    except (ResourceModifiedError, ResourceNotFoundError):  # changed meanwhile / deleted meanwhile (/esci)
        return False
    except HttpResponseError as e:
        if e.status_code != 412:
            raise
        return False
    row["_etag"] = res.get("etag") or ""
    return True


def delete(user_id: int | str) -> None:
    _table().delete_entity(PK, str(user_id))


def delete_members(chat_id: int) -> None:
    """Drop the rows of every member of a group ("<chat id>:<user id>")."""
    prefix = f"{int(chat_id)}:"
    for e in _table().query_entities(f"PartitionKey eq '{PK}' and RowKey ge '{prefix}' and RowKey lt '{prefix[:-1]};'",
                                     select=["PartitionKey", "RowKey"]):
        _table().delete_entity(e["PartitionKey"], e["RowKey"])


def is_expired(row: dict) -> bool:
    ts = row.get("code_set_at")
    if not ts:
        return True
    return _now() - datetime.fromisoformat(ts) > TTL


def expires_at(row: dict) -> datetime | None:
    ts = row.get("code_set_at")
    return datetime.fromisoformat(ts) + TTL if ts else None


def set_code(row: dict, code: str) -> None:
    row["code_enc"] = _fernet().encrypt(code.encode()).decode()
    row["code_set_at"] = _now().isoformat()


def get_code(row: dict) -> str | None:
    enc = row.get("code_enc")
    if not enc:
        return None
    try:
        return _fernet().decrypt(enc.encode()).decode()
    except InvalidToken:  # CRED_KEY was changed: the stored code is unreadable, ask for it again
        return None


def clear_code(row: dict) -> None:
    row["code_enc"] = ""
    row.pop("code_set_at", None)


def purge_expired() -> int:
    """Drop access codes older than TTL. Returns how many were removed."""
    n = 0
    for e in _table().query_entities("code_enc ne ''", select=None):
        e = dict(e)
        if is_expired(e):
            e["code_enc"] = ""  # code_set_at stays, see get()
            save(e)
            n += 1
    return n
