# bot-colletta

Working notes for the bot: the reverse-engineered site protocol, the owner's decisions, the Azure setup and the
operational gotchas. What the bot is and what it does: see the [README](../README.md).

> Status: **deployed and live** as **@gnca_cartoni_bot**. Open items in §4 "Open items".

---

## 1. Scope and owner decisions

- Telegram bot, webhook-based, on Azure Functions (Python) in the owner's Azure subscription. It drives the web
  app at `https://www.itud.eu/gnca/<prov>/<città>/` with form POSTs (§2).
- **Every user has their own credentials: store (PV, *punto vendita*) + access code**, not username/password.
  Province and city are configured by each user (test city: `mi` / `arese`). Onboarding: §3.
- Secrets live in Key Vault; never in code, README or plain app settings.
- **Italian UI.** No allow-list: anyone can use the bot, they need a valid store code anyway.
- Access codes are kept **max 7 days**, and they **expire quickly on the site side** → on a failed login the
  bot drops the code and asks for a new one.
- Testing against the live site is allowed by the owner, within the rules in §2.4.
- **"Utility" tab = load the cartons on the truck** → `/camion` (aliases `/spedisci`, `/consegna`), always behind
  a confirmation button.

---

## 2. Target site protocol (reverse-engineered, 2026-11-10)

Plain PHP app behind `aruba-proxy`, HTML forms, session cookie `PHPSESSID`.
**All requests are `application/x-www-form-urlencoded` POSTs to the same URL**,
the action is selected by the hidden field `NextStep`.

- Base URL: `https://www.itud.eu/gnca/mi/<city>/` — **trailing slash required**
  (without it: `301` to the *http* URL). `mi` = province of Milan; unknown city → `404`
  (e.g. `calasetta` → 404).
- Keep a cookie jar per session; log in again whenever the session is lost
  (detect: response is the login page again — it contains `name="CodiceAccesso"`).

### 2.1 Login page — `GET /gnca/mi/<city>/`
Title "Scelta PV". Form:
```
<select name="NrPV" required>   <option value="493">CONAD/Viale dei Platani 6 1/Arese …
<input  name="CodiceAccesso" type="text" required>
<input  name="NextStep" type="hidden" value="Login">
```
Stores for Arese: `493` CONAD Viale dei Platani 6 · `954` EUROSPIN Via Alfredo Papa 1 ·
`1937` LA GRANDE I Via Giuseppe Eugenio Luraghi 11.
Parse `<option value="(\d+)">label</option>` from `select[name=NrPV]` to build store buttons.

### 2.2 Login — `POST NrPV=<id>&CodiceAccesso=<code>&NextStep=Login`
Returns `200` with the store page (§2.3). A test store/code for Arese is in `.env`
(`TEST_CITY`, `TEST_PV`, `TEST_CODE`). A wrong code answers with the login page again.

### 2.3 Store page (after login)
Header: `<city>/<chain>/<address>` and three buttons (each its own form):
- `NextStep=PVUtility` ("U"tility) — opens the Utility page (read-only navigation), see §2.5
- `NextStep=PVListAll` (store name button) — returns the same store page/list (read-only)
- `NextStep=Logout` ("L"ogout)

**New carton form** (this is the write action):
```
<input  type="hidden" name="NumeroPrevisto" value="4">     ← next carton number (copy from page)
<select name="contenuto" required>                        ← category id
<input  type="number" name="kili" min=1.00 max=22.00 step=0.01 required>   ← weight kg
<input  type="hidden" name="NextStep" value="PVNuovoPacco">
```
Categories (`contenuto`): 18 Tonno in scatola · 19 Colazione e merenda ·
20 Conserve di pomodoro e sughi (shown as "Passata e sughi") · 21 Legumi/verdure in scatola · 22 Olio ·
23 Omogeneizzati · 24 Alimenti infanzia · 25 Pasta · 26 Riso · 27 Zucchero · 28 Latte ·
29 Biscotti · 32 Varie. **Parse them from the page, don't hardcode** (they may differ per city).
Decimal separator shown on the page is a comma (`12,00`); `kili` is sent with a **dot** (`5.50`) — verified.

**Weight limits — two levels, the bot enforces neither (owner decision): it warns and asks to confirm.**
1. the input's `min`/`max` (1.00–22.00): parsed from the page (`StoreSession.kg_limits()`); browser-side only,
   what the server does beyond them is untested;
2. **per-category limits, checked server-side only** and not present in the page. An out-of-range weight is
   not saved; the site answers with the same form plus
   `Il peso del cartone di <b>Varie</b> non e' compreso tra i limiti previsti (maggiore di 4.00 e minore di 14.00 ).`,
   an `Annulla` submit and a `Confermare` submit that re-posts with hidden `MediConfermati=MediConfermati`.
   `add_carton(cat, kg)` raises `WeightRejected(lo, hi)`; `add_carton(cat, kg, confirm=True)` re-submits with
   `MediConfermati` (**never tried on the live site**). The bot remembers the range per category
   (`cat_limits_json`), shows "⚠️ fuori dai limiti" with a "Registra comunque" button (`kg:ok`), or the user
   types another weight. Only *Varie* = 4–14 is known so far. Fixture: `tests/fixtures/weight_rejected.html`.

**Cartons table** columns: `Cartone` (number) · `kg` · `Contenuto` · `Fatto` (time packed) ·
`Camion` (time loaded on truck). Example rows seen: `3 | 12,00 | Legumi/verdure in scatola | 17:50 | 17:52`.

### 2.4 ⚠️ Rules for anyone testing against the site
- It is **live production data** of a real charity drive. The owner allows testing against it, but
  keep write actions (`PVNuovoPacco`, and especially `PVNuovoCamion` = truck loading, irreversible) to a
  minimum and tell the owner what you submitted.
- Verified: GET login page, `Login` (good + wrong code → login page again), `PVListAll`, `PVUtility`
  (opening the page, with and without pending cartons), `PVNuovoPacco` (success and per-category rejection),
  **`PVNuovoCamion`** (answers with the store page + "viaggio, con veicolo TEST001, numero 3 con 1 cartoni
  confermato."; the cartons get a `Camion` time).
- Test data written on 2026-11-10 to the test store by Claude: **carton n. 5 (Varie 5,50 kg)**, **carton n. 9
  (Tonno 12,00 kg)**, and **trip n. 3 with plate `TEST001`** loading carton 9. One rejected attempt
  (Varie 1,50 kg) saved nothing.
- **Not verified yet**: the `MediConfermati` override (used by the bot's "Registra comunque"), weights outside
  1–22, which cartons a partial truck load takes.
- **Never touch `PVTerminata`** ("dichiarare terminata la colletta", with `NrVolontari`): it appears on the
  Utility page when nothing is pending and closes the store's drive. The bot does not implement it.
- No brute-forcing or trying the test code on other stores.

### 2.5 Utility page — `POST NextStep=PVUtility` (fixture `tests/fixtures/utility.html`)
**Truck form** (present when some cartons are not loaded yet):
```
<input type="hidden" name="NextStep" value="PVNuovoCamion">
<input type="hidden" name="OldNrCartoni" value="2">          ← cartons not yet loaded
<input type="hidden" name="MaxCartone" value="5">            ← highest carton number
<input type="number" name="NrCartoni" min=1 max=2 value="2">  ← how many are delivered (default: all)
"Numerati da <b>4</b> a <b>5</b>?"  "Per un totale di <b>17.5</b> Kg.?"
<input type="number" name="IdentificativoCamion" value="2" readonly>   ← trip number, assigned by the site
<input type="Text" name="Veicolo" required minlength="7" maxlength="20"> ← vehicle plate (bot: exactly 7 letters/digits, spaces removed)
<input type="submit" value="Caricati">
```
followed by a table Nr / Prodotto / Kg. of the pending cartons grouped by product.
`StoreSession.truck_form()` parses it, `load_truck(form, plate, count)` submits it and then verifies on
`PVListAll` that the cartons got a `Camion` time.

**Edit form** `PVModificaPacco` (cartons not yet loaded only), used by `/modifica`:
```
<select name="NrPaccoModificato">  <option value="1153">4) Tonno in scatola  12.00 Kg</option>  ← value = site id, label = "<number>) <content> <kg> Kg"
<select name="contenutoModificato"> same category ids as `contenuto`
<input type="number" name="kiliModificato" min=1.00 max=22.00>          ← both new content and new weight are required
```
`StoreSession.edit_form()` parses it, `edit_carton(id, category, kg)` re-reads it, submits and then verifies on
`PVListAll` that the carton shows the new content and weight. **Not verified on the live site yet** (added
2026-11-11): the `PVModificaPacco` answer, and whether it applies the per-category weight limits (the client
treats a "limiti previsti" answer as `WeightRejected`; there is no "confirm anyway" for edits).
Bot flow: `/modifica` → carton buttons (`ed:<id>`) → content buttons (`ec:<cat>`, first one keeps the current
content) → weight typed in `await_edit_kg` or "keep" button (`ek:same`) → submit (no extra confirmation).

Also on the page, **not used by the bot**: `PVListTot` (totals), `PVDwl` / `PVDwlProspetto` (downloads).

---

## 3. Architecture (implemented)

```
Telegram ──webhook POST──▶ Azure Function  /api/telegram   (Python v2 model, anonymous,
                                │                         checks X-Telegram-Bot-Api-Secret-Token)
                                ├─▶ Table Storage "botusers" (managed identity)
                                ├─▶ Key Vault refs: TELEGRAM_TOKEN, CRED_KEY (Fernet), WEBHOOK_SECRET
                                └─▶ itud.eu form POSTs (fresh login per update — Functions are stateless)
timer 03:00 UTC daily ──▶ purge_expired_codes (drops access codes older than CRED_TTL_DAYS)
```

Files and their roles: see the README. Not in git: `.env`, **local secrets, never commit/print**
(`TELEGRAM_TOKEN`, `TEST_CITY`, `TEST_PV`, `TEST_CODE`). The webhook always returns 200 to Telegram. Errors raised
by `site_client.py`: `CityNotFound` / `LoginFailed` / `WeightRejected` / `SiteError`.

Conversation states (`state`): `""` → `await_code` (after store pick / `/codice` / expired code) → `""`;
`/cartone` → category button (one per site option + cancel) → `await_kg` → submit (out of range: warning +
`kg:ok` button) → `""`;
`/camion` → summary → `await_plate` (or "last plate" button) → confirm buttons (`trk:all`, or `trk:part` →
`await_truck_count` → `trk:ok`) → the truck form is re-read and must be unchanged → submit → `""`.
Works in private chats and in groups/supergroups. In a private chat everything is per Telegram user. In a group the
**login is shared** (owner decision, 2026-11-11): one member chooses the store and types the code, then every member
can work; `/esci` logs the whole group out. The **conversation stays per member** (state, pending carton, truck or
edit), so two volunteers can register cartons at the same time. Rows: `RowKey=<chat id>` for the group's login,
`RowKey=<chat id>:<user id>` for each member's conversation (`bot.py`, section "rows").
In groups the bot ignores ordinary chatter (typed text only while that user has a pending step), ignores unknown
commands and commands addressed to other bots (`/cmd@OtherBot`), answers as a reply to the user's message, and
adds `|<user_id>` to the callback data of its buttons: another member's tap gets "Questi pulsanti sono di un altro
utente." The command menu is set for the whole group (`chat` scope). The access code is typed (and visible) in the group.
**Group setup:** disable privacy mode in BotFather (`/setprivacy` -> Disable, then remove and re-add the bot to the
group) or make the bot a group admin; otherwise Telegram delivers only commands, not the typed weight, plate or code.

**Commands by login status (owner decision, 2026-11-11).** "Logged in" = a store is chosen and an access code
is stored. Before the login (and after `/esci`, or when the code expires / stops working) only `/citta` and
`/codice` work; `/cartone`, `/lista`, `/camion`, `/modifica`, `/stato`, `/esci` just continue the login (ask the province or
the code). After the login it is the opposite: `/citta`, `/codice`, a pasted site link and old store buttons
answer "già collegato … scrivi prima /esci". `/annulla`, `/aiuto`, `/start` always work. The command menu
follows: `COMMANDS` (default menu, set by `deploy.py`) is the pre-login list; on login the bot sets
`COMMANDS_LOGGED` for that chat (`setMyCommands` with scope `chat`) and removes it on logout
(`deleteMyCommands`); `/start` and `/aiuto` re-sync it.

**Onboarding (owner decision, 2026-11-11): guided in two steps.** `/citta` (or `/start` for a new user) asks
the **province sigla** (`await_prov`), then the **city name** (`await_city`, province kept in `pending_prov`)
→ store buttons (skipped when the city has one store) → access code. City not found → the city is asked again.
Shortcuts: `/citta mi` skips the first question, `/citta mi arese` (or `mi arese` as the province answer) both.
Pasting the city's site link (`https://www.itud.eu/gnca/mi/arese/`) still works at any time and is mentioned in
the province question. Only links on `SITE_BASE` are recognised. The site has no public list of provinces or
cities (`/gnca/` and `/gnca/mi/` answer 403), so the bot cannot offer one. The access-code message is **no longer
deleted** from the chat (owner: not that sensitive); the code is still stored encrypted for max 7 days.
`/stato`, `/lista` and the city message carry a link to the site.

### WhatsApp channel (added 2026-11-11, **not live yet**: see "Open items")

Same conversation, second channel; **Telegram behaviour is unchanged** (owner requirement). `whatsapp.py` translates
both ways and `bot.py` picks the channel per update (`handle_update(update, channel="wa")`, ContextVar `_channel`):

```
WhatsApp ──webhook──▶ /api/whatsapp   GET  = registration (hub.verify_token must equal WA_VERIFY_TOKEN)
                                      POST = messages, X-Hub-Signature-256 checked with WA_APP_SECRET
                          └─▶ whatsapp.updates() → Telegram-shaped updates → bot.handle_update(…, "wa")
bot.send / edit ──▶ whatsapp.send() ──▶ POST graph.facebook.com/<ver>/<WA_PHONE_ID>/messages (Bearer WA_TOKEN)
```

- **User key**: `RowKey = "wa<phone number>"` (so the table now holds phone numbers). 1:1 chats only, no groups.
- **Text**: HTML → WhatsApp markup (`*bold*`, `_italic_`, `` `code` ``, ``` blocks, links as "text: url").
- **Buttons**: up to 3 options with titles of max 20 characters → reply buttons; otherwise list messages of 10 rows
  each (13 categories + cancel = 2 messages). A row title is cut at 24 characters, the whole label goes in the
  description. A text longer than 1024 characters is sent first, its last line carries the buttons.
- **Commands**: the bare word works (`cartone`, `lista`, …; `/cartone` too). A message without buttons gets one
  button per `/command` it mentions (id `cmd:<name>`), more than 3 become a list with the descriptions: that
  replaces Telegram's tappable commands and command menu.
- **New user**: the first message, whatever it says, works like `/start`.
- **No edits**: where Telegram edits a message, WhatsApp gets a new one; buttons cannot be removed, a stale one
  is answered with "non è più valido" when tapped.
- The limits above (3 buttons, 20 / 24 / 72 characters, 10 rows, 1024 / 4096 characters) are **from memory of the
  Cloud API docs, not verified against the live API**: no message has been sent through WhatsApp yet.
- Meta app id `1930203954625442`; Graph API version `v24.0` (override with `WA_API_VERSION`).

---

## 4. Current state (2026-11-11)

### Azure (subscription "Azure subscription 1", all in `rg-bot-colletta`, **italynorth**)
| Resource | Notes |
|---|---|
| Function App `func-bot-colletta-bfdc1a` | Flex Consumption (FC1), Linux, Python 3.11, **512 MB instances** (was 2048 until 2026-11-11, see "Cost estimate"), system identity `2230cfc0-…`. Host `https://func-bot-colletta-bfdc1a.azurewebsites.net` |
| Storage `stbotcollettabfdc1a` | Functions host storage + table `botusers`. Function identity: *Storage Table Data Contributor* |
| Key Vault `kv-botcolletta-bfdc1a` | RBAC mode. Secrets `telegram-token`, `cred-key`, `webhook-secret`. Function: *Secrets User*; owner: *Secrets Officer* |
| App Insights `func-bot-colletta-bfdc1a` | logs / exceptions |
| App settings | `TELEGRAM_TOKEN`, `CRED_KEY`, `WEBHOOK_SECRET` (= Key Vault refs, all *Resolved*), `TABLES_ENDPOINT`, `SITE_BASE=https://www.itud.eu/gnca`, `CRED_TTL_DAYS=7` |
| Telegram | bot `@gnca_cartoni_bot` (since 2026-11-11; before: `@cacartony_bot`, webhook removed). Webhook → `/api/telegram` with secret token, `allowed_updates=[message, callback_query]`; command menu set (it + default) |

### Deploy / operate
```bash
python3 deploy.py     # zip → zip-deploy (remote build) → /api/health check → Telegram command menu
# rotate bot token: put new value in Key Vault (via --file), then REFRESH the Key Vault references and re-run
# setWebhook. A restart or a deploy is NOT enough: on 2026-11-11 the app kept the old token (every reply failed
# with Telegram 401 in App Insights) until this call:
#   az rest --method post --uri "https://management.azure.com/subscriptions/<sub>/resourceGroups/rg-bot-colletta/providers/Microsoft.Web/sites/func-bot-colletta-bfdc1a/config/configreferences/appsettings/refresh?api-version=2023-12-01"
# Check: a simulated update for an unknown chat must log "400 chat not found", not "401 Unauthorized".
az keyvault secret set --vault-name kv-botcolletta-bfdc1a -n telegram-token --file <tmpfile>
az functionapp restart -g rg-bot-colletta -n func-bot-colletta-bfdc1a
```
`setWebhook` needs `secret_token` = Key Vault `webhook-secret` (read it with `az keyvault secret show`,
never print it). `deploy.py` ships every root `*.py` (except itself), so new modules need no list to maintain.

### Cost estimate (2026-11-11)
Scenario: 1,000 users x 100 calls = **100,000 executions** in one month.

Flex Consumption bills executions plus memory x time (GB-s). Free grant per subscription per month: 250,000
executions and 100,000 GB-s. Rates used: $0.40 per million executions, $0.000026 per GB-s (third-party figures,
the higher of those found; another source says $0.000016; Microsoft's pricing page did not render the rates).

- Executions: 100,000 < 250,000 free -> $0.
- Compute = executions x instance GB x seconds per call, minus 100,000 free GB-s, x rate. Seconds per call is
  **assumed** 1-3 s (fresh site login on every update), not measured.

| Seconds per call | 2048 MB: GB-s -> cost | 512 MB: GB-s -> cost |
|---|---|---|
| 1 s | 200,000 -> ~$2.60 | 50,000 -> $0 |
| 2 s | 400,000 -> ~$7.80 | 100,000 -> $0 |
| 3 s | 600,000 -> ~$13 | 150,000 -> ~$1.30 |

Real cost should be lower: one instance serves several calls at once. Table Storage, Key Vault and App Insights
add cents at this volume.

Alternatives considered:
- **Always-on VM / VPS**: roughly $5-10 a month = $60-120 a year for a bot used one weekend; plus patching, HTTPS.
- **Azure Container Apps** (scale to zero), 0.25 vCPU / 0.5 GiB: fits the free grant (180,000 vCPU-s, 360,000 GiB-s,
  2 million requests a month) -> $0, but needs a container image and a rework of the purge timer.
- **Classic Consumption plan**: bigger free grant (1 million executions, 400,000 GB-s) -> $0, but the Linux
  version is (as far as recalled, not checked) being retired.

Decision: stay on Flex Consumption and lower the instance memory to 512 MB. Applied 2026-11-11:
```bash
az functionapp scale config set -g rg-bot-colletta -n func-bot-colletta-bfdc1a --instance-memory 512   # back: 2048
```
Checked after the change: `/api/health` 200, webhook without secret 403. A full Telegram conversation at 512 MB
was not tried yet.

### Open items
0. **WhatsApp go-live.** Code deployed, nothing configured: `/api/whatsapp` answers 403 until the settings exist.
   Missing: Key Vault secrets `wa-app-secret` (Meta app secret), `wa-verify-token` (any random string),
   `wa-token` (access token) and app settings `WA_APP_SECRET`, `WA_VERIFY_TOKEN`, `WA_TOKEN` (Key Vault refs) plus
   `WA_PHONE_ID`; then register the webhook in the Meta app (callback `https://func-bot-colletta-bfdc1a.azurewebsites.net/api/whatsapp`,
   field `messages`) and try a whole conversation. The app secret was pasted in a chat transcript: reset it in the
   Meta dashboard once everything works.
1. Owner should **revoke the old bot token** via @BotFather `/revoke` (it was pasted in a chat transcript); if it
   is the token in use, update Key Vault as described above.
2. Never tried on the live site: "Registra comunque" (the `MediConfermati` re-submit), `/modifica`
   (`PVModificaPacco`), a partial truck load, weights outside 1-22 kg (§2.4, §2.5).
3. A full Telegram conversation at 512 MB instance memory.
4. Optional: the other Utility functions (totals, downloads).

---

## 5. Environment gotchas
- Dev machine is **WSL**, but `az` is the **Windows** Azure CLI: output lines end in `\r`
  (strip before comparing, e.g. `| tr -d '\r'`), and pass **relative paths** (cwd is under
  `/mnt/c`) rather than `/mnt/...` paths to `az`.
- **westeurope rejects new resources** on this subscription ("region not accepting new
  customers") → use **italynorth**.
- Not installed: Azure Functions Core Tools (`func`), `zip`. Python 3 and Node are available.
- Corporate network: DNS resolution occasionally fails (seen for `management.azure.com`) — just retry.
- The Windows `az` has a **default resource group `Microsoft.resourcegroups.test`** in its config —
  always pass `-g rg-bot-colletta` (e.g. `az keyvault show` without `-g` fails).
- `az functionapp show` returns all-null fields for this Flex app from this CLI; use
  `az resource show --resource-type Microsoft.Web/sites ...` instead.
- A failed `az functionapp create` can leave a half-created site + orphan FC1 plan — delete both before retrying.
