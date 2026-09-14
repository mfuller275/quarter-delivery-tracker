"""One-time Azure DevOps sign-in to populate the persistent token cache.

Run this in the FOREGROUND on the server (so you can see the prompt), then the
background server can refresh silently as the same OS user:

    C:\\Apps\\quarter-delivery-tracker\\.venv\\Scripts\\python.exe signin.py

With AUTH_MODE=devicecode (recommended on servers) it prints a URL and a code:
open https://microsoft.com/devicelogin on ANY device, enter the code, and
complete your normal Microsoft MFA. On success it makes a test API call and
caches the token for the running dashboard.
"""
from __future__ import annotations

from backend.ado_client import ADOClient
from backend.config import get_settings


def main() -> int:
    settings = get_settings()
    print(f"Auth mode: {settings.auth_mode}")
    client = ADOClient(settings)
    print("Starting sign-in (follow any prompt shown above)...")
    client.login()
    ids = client.get_work_item_ids()
    print(f"Sign-in OK - token cached. Test query returned {len(ids)} work item id(s).")
    print("You can close this window; the dashboard will now work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
