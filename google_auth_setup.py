#!/usr/bin/env python3
"""One-time: get a Google refresh token for Search Console, Site Verification and GA4 Admin.

  pip install google-auth-oauthlib
  python3 google_auth_setup.py            # opens a browser, writes GOOGLE_REFRESH_TOKEN into .env
"""
import os, re
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/siteverification",
    "https://www.googleapis.com/auth/webmasters",
    "https://www.googleapis.com/auth/analytics.edit",
]
env = Path(__file__).resolve().parent / ".env"
text = env.read_text() if env.exists() else ""
get = lambda k: os.environ.get(k) or (re.search(rf"^{k}=(.*)$", text, re.M) or [None, ""])[1].strip().strip('"')

cfg = {"installed": {"client_id": get("GOOGLE_CLIENT_ID"), "client_secret": get("GOOGLE_CLIENT_SECRET"),
                     "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
                     "redirect_uris": ["http://localhost"]}}
if not cfg["installed"]["client_id"]:
    raise SystemExit("Put GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first.")

creds = InstalledAppFlow.from_client_config(cfg, SCOPES).run_local_server(port=0, access_type="offline", prompt="consent")
line = f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}"
text = re.sub(r"^GOOGLE_REFRESH_TOKEN=.*$", line, text, flags=re.M) if "GOOGLE_REFRESH_TOKEN=" in text else text.rstrip() + "\n" + line + "\n"
env.write_text(text)
print("Saved GOOGLE_REFRESH_TOKEN to .env")
