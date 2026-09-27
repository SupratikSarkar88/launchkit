#!/usr/bin/env python3
"""Reconnect Google (Search Console + Analytics). Same as menu option 'Connect accounts'."""
import launch
launch.load_env()
if launch.os.environ.get("GOOGLE_CLIENT_ID"): launch.google_login()
else: launch.connect_accounts(want_ga=False)
