"""Log in with OpenRouter (OAuth PKCE) so Golem spends the user's own credits.

The user approves in the browser, OpenRouter creates a key on the user's account, and
Golem stores it in ~/.config/golem/openrouter.key with mode 0600. OPENROUTER_API_KEY,
when set, wins over the stored key. The key is never printed, never logged, and never
reaches a sandbox: sandbox containers get no environment and no home directory.

Two ways to finish the login:
    browser   OpenRouter redirects to http://localhost:<port>/callback, served here
    headless  no callback: OpenRouter shows a code, the user pastes it (for Docker or SSH)

OpenRouter's PKCE flow has no spending-limit option. Golem's per-task cap in
authority.json still applies; a limit on the key itself is set on openrouter.ai.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTH_URL = "https://openrouter.ai/auth"
EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"
KEY_FILE = "openrouter.key"


class LoginError(RuntimeError):
    pass


def key_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "golem" / KEY_FILE


def api_key() -> str | None:
    """OPENROUTER_API_KEY if set, else the key `golem login` stored, else None."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    path = key_path()
    if path.is_file():
        return path.read_text(encoding="utf-8").strip() or None
    return None


def key_source() -> str:
    if os.environ.get("OPENROUTER_API_KEY", "").strip():
        return "OPENROUTER_API_KEY"
    return str(key_path()) if key_path().is_file() else "none"


def challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    return verifier, challenge_for(verifier)


def authorize_url(
    challenge: str, callback_url: str | None, state: str | None, label: str = "golem"
) -> str:
    params = {
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "key_label": label,
    }
    if callback_url:
        params["callback_url"] = callback_url
    if state:
        params["state"] = state
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def store(key: str) -> Path:
    """Write the key with mode 0600, atomically, so no other user can read it at any point."""
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = path.with_name(f".{KEY_FILE}.{os.getpid()}")
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key.strip() + "\n")
    os.replace(staging, path)
    return path


def exchange(code: str, verifier: str) -> str:
    body = {
        "code": code.strip(),
        "code_verifier": verifier,
        "code_challenge_method": "S256",
    }
    data = _post(EXCHANGE_URL, body)
    key = data.get("key")
    if not isinstance(key, str) or not key:
        raise LoginError("OpenRouter answered without a key")
    return key


def credits(key: str) -> dict:
    """{'total_credits': ..., 'total_usage': ...} for the account behind the key."""
    request = urllib.request.Request(
        CREDITS_URL, headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8")).get("data") or {}


def wait_for_code(server: http.server.HTTPServer, state: str, timeout: float) -> str:
    """Serve the callback until OpenRouter redirects back with a code for this state."""
    found: dict = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if urllib.parse.urlparse(self.path).path != "/callback":
                self.send_error(404)
                return
            if query.get("state", [""])[0] != state:
                found["error"] = "the callback's state did not match this login"
            elif "code" in query:
                found["code"] = query["code"][0]
            else:
                found["error"] = "OpenRouter returned no code (the login was declined?)"
            message = (
                "Golem: login received. You can close this tab."
                if "code" in found
                else f"Golem: {found['error']}"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message.encode("utf-8"))

        def log_message(self, format: str, *args) -> None:
            pass

    server.RequestHandlerClass = Callback
    deadline = time.monotonic() + timeout
    while not found and time.monotonic() < deadline:
        server.timeout = max(0.1, min(1.0, deadline - time.monotonic()))
        server.handle_request()
    if "code" in found:
        return found["code"]
    raise LoginError(found.get("error", f"no callback within {int(timeout)} s"))


def login(
    headless: bool = False,
    port: int = 0,
    open_browser: bool = True,
    timeout: float = 300.0,
    say=print,
    ask=input,
) -> Path:
    verifier, challenge = pkce_pair()
    if headless:
        say(
            "Open this URL, approve, and paste the code OpenRouter shows:\n\n  "
            + authorize_url(challenge, None, None)
            + "\n"
        )
        code = ask("code: ")
    else:
        state = secrets.token_urlsafe(16)
        server = http.server.HTTPServer(
            ("127.0.0.1", port), http.server.BaseHTTPRequestHandler
        )
        try:
            callback = f"http://localhost:{server.server_address[1]}/callback"
            url = authorize_url(challenge, callback, state)
            say(
                "Opening OpenRouter to approve a key for Golem. If no browser opens, visit:\n\n  "
                + url
                + "\n"
            )
            if open_browser:
                import webbrowser

                webbrowser.open(url)
            code = wait_for_code(server, state, timeout)
        finally:
            server.server_close()
    return store(exchange(code, verifier))


def _post(url: str, body: dict) -> dict:
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise LoginError(
            f"OpenRouter refused the code: HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:200]}"
        ) from None
