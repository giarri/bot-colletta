#!/usr/bin/env python3
"""One-command deploy:  python3 deploy.py

1. zips the app (every *.py in this folder except this script, plus host.json and requirements.txt)
2. zip-deploys it to the Function App (remote build)
3. checks /api/health
4. updates the Telegram command menu from bot.COMMANDS (needs TELEGRAM_TOKEN in .env; skipped otherwise)

Only needs Python 3 and the Azure CLI (`az login` done). Run it from this folder.
"""

import ast
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

RG, APP = "rg-bot-colletta", "func-bot-colletta-bfdc1a"
HERE = pathlib.Path(__file__).resolve().parent
ZIP = "app.zip"


def main() -> None:
    files = sorted(p.name for p in HERE.glob("*.py") if p.name != "deploy.py") + ["host.json", "requirements.txt"]
    with zipfile.ZipFile(HERE / ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(HERE / f, f)
    print("zipped:", ", ".join(files))

    # relative --src on purpose: `az` may be the Windows CLI, which can't read /mnt/... paths
    subprocess.run(["az", "functionapp", "deployment", "source", "config-zip", "-g", RG, "-n", APP,
                    "--src", ZIP, "--build-remote", "true"], cwd=HERE, check=True)

    for attempt in range(6):  # the app may answer 5xx for a while right after a deploy
        try:
            with urllib.request.urlopen(f"https://{APP}.azurewebsites.net/api/health", timeout=60) as r:
                print("health:", r.status)
            break
        except (urllib.error.URLError, TimeoutError) as e:
            print("health: not ready yet:", e)
            if attempt == 5:
                sys.exit("health check failed: command menu not updated")
            time.sleep(10)

    env = {k.strip(): v.strip().strip("\"'") for k, v in
           (line.split("=", 1) for line in (HERE / ".env").read_text().splitlines()
            if "=" in line and not line.lstrip().startswith("#"))} if (HERE / ".env").exists() else {}
    token = env.get("TELEGRAM_TOKEN")
    if not token:
        print("no TELEGRAM_TOKEN in .env: command menu not updated")
        return
    src = (HERE / "bot.py").read_text(encoding="utf-8")
    commands = [{"command": c, "description": d}
                for c, d in ast.literal_eval(re.search(r"(?s)^COMMANDS = (\[.*?^\])", src, re.M).group(1))]
    for scope in ({}, {"language_code": "it"}):
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/setMyCommands",
                                     data=json.dumps({"commands": commands, **scope}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            ok = json.load(r).get("ok")
        print("command menu", scope.get("language_code", "default") + ":", "ok" if ok else "FAILED")
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
