"""One-time helper: run the OAuth dance and print a refresh token.

Usage (from the repo root):

    GMAIL_CLIENT_ID=... GMAIL_CLIENT_SECRET=... python scripts/get_gmail_refresh_token.py

Opens a browser, walks you through Google's consent screen, then prints the
refresh token to stdout. Paste it into `.env` as `GMAIL_REFRESH_TOKEN`.

The script does no file I/O on purpose — that way an accidental commit can't
leak the token, and you decide where it lands.
"""

from __future__ import annotations

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

from cole_leads.gmail import GMAIL_SCOPES


def main() -> int:
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "ERROR: GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in the environment.",
            file=sys.stderr,
        )
        return 2

    flow = InstalledAppFlow.from_client_config(
        client_config={
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=GMAIL_SCOPES,
    )
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",  # forces a refresh token even on re-auth
    )
    if not creds.refresh_token:
        print(
            "ERROR: Google did not return a refresh token. "
            "Re-run with `prompt=consent` (it's already set) and revoke "
            "previous consent at https://myaccount.google.com/permissions.",
            file=sys.stderr,
        )
        return 1

    print()
    print("=" * 60)
    print("Refresh token (paste into .env as GMAIL_REFRESH_TOKEN):")
    print()
    print(creds.refresh_token)
    print()
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
