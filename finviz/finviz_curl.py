import re
import os
from pathlib import Path
from curl_cffi import requests as curl_requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
EMAIL    = os.getenv("FINVIZ_USERNAME")
PASSWORD = os.getenv("FINVIZ_PASSWORD")

API_KEYS_PATH = Path(__file__).parent / "api_keys.py"
LOGIN_URL     = "https://finviz.com/login_submit"
ACCOUNT_URL   = "https://elite.finviz.com/api_explanation"


def login() -> curl_requests.Session:
    """Login to FinViz Elite and return authenticated session."""
    session = curl_requests.Session()

    print("[FinViz] Logging in...")
    response = session.post(
        LOGIN_URL,
        data={"email": EMAIL, "password": PASSWORD},
        impersonate="chrome"
    )

    if "logout" in response.text.lower() or response.status_code == 200:
        print("OK: Login successful.")
    else:
        print(f"ERROR: Login failed. Status: {response.status_code}")
        raise SystemExit(1)

    return session


def get_token(session: curl_requests.Session) -> str:
    """Navigate to account page and parse API token."""
    print("[FinViz] Fetching account page...")
    response = session.get(ACCOUNT_URL, impersonate="chrome")

    # Token is in JSON script tag: "userToken":"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    token_match = re.search(r'"userToken"\s*:\s*"([a-f0-9\-]{36})"', response.text)
    if token_match:
        return token_match.group(1)

    print("ERROR: Could not find token on account page.")
    print("[Debug] Full page HTML saved to debug_page.html")
    with open("debug_page.html", "w", encoding="utf-8") as f:
        f.write(response.text)
    raise SystemExit(1)


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
    session = login()
    token   = get_token(session)
    update_api_keys(token)
    print("Done: Token regeneration complete!")
