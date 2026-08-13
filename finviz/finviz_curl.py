import re
import os
from pathlib import Path
from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from .errors import FinvizTokenError, FinvizNotConfigured

load_dotenv(Path(__file__).parent.parent / ".env")
EMAIL    = os.getenv("FINVIZ_USERNAME")
PASSWORD = os.getenv("FINVIZ_PASSWORD")

API_KEYS_PATH = Path(__file__).parent / "api_keys.py"
LOGIN_URL     = "https://finviz.com/login_submit"
ACCOUNT_URL   = "https://elite.finviz.com/api_explanation"


def login() -> curl_requests.Session:
    """Login to FinViz Elite and return authenticated session.

    Raises rather than exiting: new_finviz.fetch_and_save now calls this to get
    a token automatically, so it runs inside the dashboard and the collector.
    SystemExit there would not be caught by their `except Exception` handlers —
    it derives from BaseException — and would take the whole process down.
    """
    if not EMAIL or not PASSWORD:
        raise FinvizNotConfigured(
            "no FINVIZ_USERNAME / FINVIZ_PASSWORD in .env, so a token cannot be "
            "fetched automatically. Copy .env.example to .env and fill both in."
        )

    session = curl_requests.Session()

    print("[FinViz] Logging in...")
    response = session.post(
        LOGIN_URL,
        data={"email": EMAIL, "password": PASSWORD},
        impersonate="chrome"
    )

    if response.status_code != 200:
        raise FinvizTokenError(
            f"FinViz login failed (HTTP {response.status_code})."
        )
    print("OK: Login successful.")
    return session


def get_token(session: curl_requests.Session) -> str:
    """Navigate to account page and parse API token."""
    print("[FinViz] Fetching account page...")
    response = session.get(ACCOUNT_URL, impersonate="chrome")

    # Token is in JSON script tag: "userToken":"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    token_match = re.search(r'"userToken"\s*:\s*"([a-f0-9\-]{36})"', response.text)
    if token_match:
        return token_match.group(1)

    # Reached when the credentials were wrong: FinViz answers 200 with the
    # login page again, so the POST above cannot tell success from failure on
    # status alone — the missing token here is what proves it.
    with open("debug_page.html", "w", encoding="utf-8") as f:
        f.write(response.text)
    raise FinvizTokenError(
        "logged in but no API token on the account page — usually a wrong "
        "FINVIZ_USERNAME / FINVIZ_PASSWORD, or an account without Elite. "
        "The page returned was saved to debug_page.html."
    )


def update_api_keys(token: str):
    """Update FINVIZ_AUTH_TOKEN in api_keys.py, creating the file if needed.

    api_keys.py is gitignored (this script rewrites it), so on a fresh clone it
    may not exist yet — read_text() alone would raise FileNotFoundError here.
    """
    content = API_KEYS_PATH.read_text(encoding="utf-8") if API_KEYS_PATH.exists() else 'FINVIZ_AUTH_TOKEN = ""\n'
    new_content, n = re.subn(
        r'FINVIZ_AUTH_TOKEN\s*=\s*".*?"',
        f'FINVIZ_AUTH_TOKEN = "{token}"',
        content
    )
    if n == 0:                      # file exists but has no token line — append one
        new_content = content.rstrip("\n") + f'\nFINVIZ_AUTH_TOKEN = "{token}"\n'
    API_KEYS_PATH.write_text(new_content, encoding="utf-8")
    print(f"OK: api_keys.py updated with new token: {token}")


if __name__ == "__main__":
    import sys
    try:
        session = login()
        token   = get_token(session)
        update_api_keys(token)
    except (FinvizTokenError, FinvizNotConfigured) as e:
        # Run by hand, so report the cause on one line instead of a traceback.
        sys.exit(f"ERROR: {e}")
    print("Done: Token regeneration complete!")
