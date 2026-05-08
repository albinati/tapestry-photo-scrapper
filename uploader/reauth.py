#!/usr/bin/env python3
"""Bootstrap Google OAuth token with the scopes this pipeline needs.

ONLY use this for first-time setup of a standalone install. If you're
sharing a token with another service that owns its lifecycle (e.g. an
OpenClaw weekly renewal cron), DO NOT run this — it will overwrite their
token and break that service. The upload script doesn't need this script
at runtime; it consumes whichever token `config.toml`'s `token_path`
points at.

Starts a tiny HTTP server on 127.0.0.1:<port>, prints the authorization
URL, waits for the browser to redirect back with ?code=..., exchanges
it for tokens, and writes the token at ~/.config/google/token.json.
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import Flow

CREDS = os.path.expanduser("~/.config/google/credentials.json")
TOKEN = os.path.expanduser("~/.config/google/token.json")
URL_FILE = "/tmp/google_auth_url.txt"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    "https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata",
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
]


def pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    port = pick_port()
    redirect_uri = f"http://localhost:{port}/"

    with open(CREDS) as f:
        c = json.load(f)
    client_config = {
        "installed": {
            "client_id": c["client_id"],
            "client_secret": c["client_secret"],
            "auth_uri": c.get("auth_uri", "https://accounts.google.com/o/oauth2/auth"),
            "token_uri": c.get("token_uri", "https://oauth2.googleapis.com/token"),
            "redirect_uris": [redirect_uri],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    with open(URL_FILE, "w") as f:
        f.write(auth_url + "\n")

    print(f"\nAuthorize at this URL (also written to {URL_FILE}):\n")
    # Newlines around the URL keep it on its own line for clean copy/paste:
    print(auth_url)
    print(f"\nWaiting for redirect on http://localhost:{port}/ ...")

    # Try to launch the URL in the user's default browser. On WSL2,
    # webbrowser.open often fails silently; fall back to wslview / explorer.exe.
    opened = False
    try:
        opened = webbrowser.open(auth_url)
    except Exception:
        opened = False
    if not opened:
        for launcher in ("wslview", "xdg-open", "explorer.exe"):
            try:
                subprocess.Popen([launcher, auth_url],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                opened = True
                break
            except FileNotFoundError:
                continue
    if not opened:
        print("\n(Could not auto-launch a browser. Open the URL above manually.)")

    received = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            pass

        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            received.update(qs)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            if "code" in qs:
                msg = "Authorized — you can close this tab."
            else:
                msg = "No code in redirect — see terminal."
            self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    # Wait up to 5 minutes for the redirect.
    deadline = time.time() + 300
    while "code" not in received and time.time() < deadline:
        time.sleep(0.5)
    server.shutdown()

    if "code" not in received:
        print("Timed out waiting for authorization.", file=sys.stderr)
        sys.exit(1)

    code = received["code"][0]
    flow.fetch_token(code=code)
    creds = flow.credentials

    token_data = {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "scope": " ".join(creds.scopes),
        "scopes": list(creds.scopes),
        "token_type": "Bearer",
        "expiry_date": int(creds.expiry.timestamp() * 1000) if creds.expiry else None,
    }
    with open(TOKEN, "w") as f:
        json.dump(token_data, f, indent=2)
    os.chmod(TOKEN, 0o600)
    print(f"\nSaved token to {TOKEN}")
    print("Granted scopes:")
    for s in creds.scopes:
        print(" -", s)


if __name__ == "__main__":
    main()
