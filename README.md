# bot-colletta 🥫

**A Telegram bot that lets food-drive volunteers register donated cartons from their phone, in three taps.**

During the **Colletta Alimentare** (Italy's national food drive),
volunteers in each supermarket weigh every carton of donated food and record it in a web app. The web app is
form-based and awkward to use on a phone, standing next to a scale with a carton in your hands.
This bot puts the same workflow in a Telegram chat.

```
You   /cartone
Bot   📦 Cartone n. 4
      Cosa contiene? Tocca un pulsante:
      [🐟 Tonno in scatola] [☕ Colazione e merenda]
      [🫙 Passata e sughi]  [🍝 Pasta]  …
You   (tap 🍝 Pasta)
Bot   Quanti kg pesa?
You   12
Bot   ✅ Cartone n. 4 registrato: 🍝 Pasta, 12,00 kg (ore 17:50)
```

## What it does

| Command | What happens |
|---|---|
| `/cartone` | Register a carton: pick the content, type the weight. |
| `/lista` | See every carton registered for the store, with totals. |
| `/modifica` | Fix the content or weight of a carton not yet shipped. |
| `/camion` | Load the pending cartons on the truck, behind an explicit confirmation. |
| `/citta`, `/codice`, `/esci` | Guided login (province, city, store, access code) and logout. |

It works in private chats and in **group chats**, where a store's volunteers share one login while each keeps
their own conversation, so two people can register cartons at the same time without stepping on each other.

## The interesting parts

- **No API to call.** The target web app only speaks HTML forms. I reverse-engineered its protocol and wrote a
  small client (`site_client.py`) that logs in, parses the pages and posts the forms. Categories, weight limits
  and store lists are read from the live pages, never hardcoded.
- **Never trust a write.** After every submission the bot re-reads the store page and checks that the carton,
  the edit or the truck load is really there before telling the volunteer it worked.
- **Stateless and serverless.** One Azure Function behind a Telegram webhook. Conversation state lives in
  Azure Table Storage, so the app scales to zero and costs almost nothing outside the drive weekend.
- **Secrets handled properly.** The bot token and keys sit in Key Vault and reach the app through managed
  identity. Volunteers' access codes are encrypted at rest and purged after 7 days by a daily timer. The webhook
  rejects any call without Telegram's secret token.
- **Tested offline.** 46 pytest tests run against saved HTML fixtures and fake Telegram and storage layers:
  no network, no Azure packages, a few seconds.
- **One-command deploy.** `python3 deploy.py` zips the app, deploys it, checks the health endpoint and updates
  the Telegram command menu.

## Architecture

```
Telegram ──webhook──▶ Azure Function (Python)
                          ├─▶ Table Storage   per-user conversation state, encrypted access codes
                          ├─▶ Key Vault       bot token, encryption key, webhook secret
                          └─▶ food-drive site login + form POSTs, one fresh session per update
daily timer ──▶ purge access codes older than 7 days
```

| File | Role |
|---|---|
| `function_app.py` | Azure entry points: webhook, health check, daily purge. |
| `bot.py` | Conversation logic, Italian texts, Telegram API helpers. |
| `site_client.py` | Client for the food-drive web app. |
| `user_store.py` | Per-user rows in Table Storage, with encryption and expiry. |
| `tests/` | Offline test suite and HTML fixtures. |

**Stack:** Python 3.11 · Azure Functions (Flex Consumption) · Table Storage · Key Vault · Application Insights ·
Telegram Bot API · pytest.

## Run the tests

```bash
python3 -m venv .venv && .venv/bin/pip install pytest requests
.venv/bin/python -m pytest -q
```

## More

[`docs/NOTES.md`](docs/NOTES.md) holds the working notes: the reverse-engineered site protocol, the design
decisions taken along the way, the Azure setup and the operational gotchas.
