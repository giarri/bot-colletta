# bot-colletta

Telegram bot for the Colletta Alimentare, running on Azure Functions. See docs/NOTES.md for the protocol and setup.

## Always deploy

After every code change, once the tests pass, deploy without asking:

```
python3 deploy.py
```

Run it from this folder. It zips the app, zip-deploys it to the Function App, checks `/api/health` and updates the
Telegram command menu. Report the outcome (health status and command menu lines).

## Tests

```
.venv/bin/python -m pytest -q
```

`.venv` (WSL, ignored by git and by the deploy) has `pytest` and `requests`; recreate it with
`python3 -m venv .venv && .venv/bin/pip install pytest requests` if missing. The bot tests use a fake user store,
so no Azure packages are required.
