"""Azure Functions entry point (Python v2 programming model).

- POST /api/telegram   Telegram webhook (checks X-Telegram-Bot-Api-Secret-Token)
- GET  /api/health     smoke test
- timer (daily 03:00 UTC) purges access codes older than CRED_TTL_DAYS
"""

import hmac
import logging
import os

import azure.functions as func

import bot
import user_store

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.route(route="telegram", methods=["POST"])
def telegram_webhook(req: func.HttpRequest) -> func.HttpResponse:
    expected = os.environ.get("WEBHOOK_SECRET", "")
    received = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected or not hmac.compare_digest(expected, received):
        return func.HttpResponse("forbidden", status_code=403)
    try:
        update = req.get_json()
    except ValueError:
        return func.HttpResponse("bad request", status_code=400)
    try:
        bot.handle_update(update)
    except Exception:
        # Always 200 so Telegram doesn't retry the same update forever.
        logging.exception("Error handling update %s", update.get("update_id"))
        chat = (update.get("message") or (update.get("callback_query") or {}).get("message") or {}).get("chat", {})
        if chat.get("id"):
            try:
                bot.send(chat["id"], "⚠️ Errore interno, riprova tra poco.")
            except Exception:
                pass
    return func.HttpResponse("ok", status_code=200)


@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse("ok", status_code=200)


@app.timer_trigger(schedule="0 0 3 * * *", arg_name="timer", run_on_startup=False)
def purge_expired_codes(timer: func.TimerRequest) -> None:
    n = user_store.purge_expired()
    logging.info("Purged %d expired access codes", n)
