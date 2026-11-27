# SPDX-License-Identifier: GPL-3.0-or-later
"""Password login for remote use (Tailscale Funnel / serve, or any reverse proxy).

A request straight from this machine (loopback, no proxy headers) passes as before. Anything else — a request
from another address, or one a proxy forwarded (X-Forwarded-For, Forwarded, Tailscale-*) — needs a session cookie
from /login. With no password set, remote requests are refused outright, so turning on a proxy before setting a
password exposes nothing.

    python -m rxapp.auth set-password      # prompts; stored as scrypt in <data>/auth.json

The cookie is "<expiry>.<hmac>" signed with a random secret kept in auth.json; changing the password makes a new
secret, which signs every device out. Covers HTTP and WebSocket alike (it is plain ASGI middleware).
"""
import base64
import getpass
import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, quote

COOKIE = "rxapp_session"
SESSION_SEC = 30 * 86400
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
PROXY_HEADERS = (b"x-forwarded-for", b"x-forwarded-host", b"forwarded", b"x-real-ip")
FAIL_WINDOW_SEC, FAIL_MAX = 600, 10        # at most 10 wrong passwords per 10 minutes (all sources together)


def _scrypt(pw: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


class Auth:
    def __init__(self, path: Path):
        self.path = path
        self.fails = []
        self._cfg, self._mtime = None, None

    def cfg(self):
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return None
        if m != self._mtime:
            try:
                self._cfg, self._mtime = json.loads(self.path.read_text()), m
            except ValueError:
                return None
        return self._cfg

    def set_password(self, pw: str):
        salt = secrets.token_bytes(16)
        d = {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(_scrypt(pw, salt)).decode(),
             "secret": secrets.token_hex(32)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def check_password(self, pw: str) -> bool:
        c = self.cfg()
        if not c:
            return False
        now = time.time()
        self.fails = [t for t in self.fails if now - t < FAIL_WINDOW_SEC]
        if len(self.fails) >= FAIL_MAX:
            return False
        ok = hmac.compare_digest(_scrypt(pw, base64.b64decode(c["salt"])), base64.b64decode(c["hash"]))
        if not ok:
            self.fails.append(now)
        return ok

    def _sign(self, exp: int) -> str:
        return hmac.new(bytes.fromhex(self.cfg()["secret"]), str(exp).encode(), hashlib.sha256).hexdigest()

    def token(self) -> str:
        exp = int(time.time()) + SESSION_SEC
        return f"{exp}.{self._sign(exp)}"

    def valid(self, tok: str) -> bool:
        if not tok or not self.cfg():
            return False
        exp, _, sig = tok.partition(".")
        try:
            e = int(exp)
        except ValueError:
            return False
        return e > time.time() and hmac.compare_digest(sig, self._sign(e))


def is_remote(scope) -> bool:
    client = (scope.get("client") or ("", 0))[0]
    if client not in LOOPBACK:
        return True
    for k, _ in scope.get("headers", []):
        if k in PROXY_HEADERS or k.startswith(b"tailscale-"):
            return True
    return False


def _cookie(scope) -> str:
    for k, v in scope.get("headers", []):
        if k == b"cookie":
            for part in v.decode("latin-1").split(";"):
                n, _, val = part.strip().partition("=")
                if n == COOKIE:
                    return val
    return ""


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>rxapp login</title><style>
body{{font:16px system-ui,sans-serif;background:#f4f6f8;color:#1d2833;display:flex;min-height:100vh;margin:0;align-items:center;justify-content:center}}
form{{background:#fff;border:1px solid #cbd3d8;border-radius:10px;padding:28px 24px;width:min(320px,90vw);box-shadow:0 2px 10px rgba(0,0,0,.06)}}
h1{{font-size:20px;margin:0 0 16px}}input{{width:100%;box-sizing:border-box;font:inherit;padding:10px;border:1px solid #cbd3d8;border-radius:6px;margin-bottom:12px}}
button{{width:100%;font:inherit;padding:10px;border:0;border-radius:6px;background:#1f5fbf;color:#fff;cursor:pointer}}.err{{color:#b42318;font-size:14px;margin:0 0 12px}}
</style></head><body><form method="post" action="/login"><h1>rxapp</h1>{err}
<input type="hidden" name="next" value="{next}">
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
<button type="submit">Sign in</button></form></body></html>"""


class AuthMiddleware:
    def __init__(self, app, auth: Auth):
        self.app, self.auth = app, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not is_remote(scope):
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if scope["type"] == "http" and path == "/login":
            return await self.login(scope, receive, send)
        if scope["type"] == "http" and path == "/logout":
            return await self._respond(send, 303, b"", [(b"location", b"/login"),
                                       (b"set-cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax".encode())])
        if self.auth.valid(_cookie(scope)):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 4401})
        if path.startswith("/api/"):
            return await self._respond(send, 401, b'{"detail":"login required"}', [(b"content-type", b"application/json")])
        nxt = path + ("?" + scope["query_string"].decode("latin-1") if scope.get("query_string") else "")
        return await self._respond(send, 303, b"", [(b"location", ("/login?next=" + quote(nxt, safe="")).encode())])

    async def login(self, scope, receive, send):
        qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        nxt, err = qs.get("next", ["/"])[0], ""
        if scope["method"] == "POST":
            body = b""
            while True:
                m = await receive()
                body += m.get("body", b"")
                if not m.get("more_body") or len(body) > 8192:
                    break
            form = parse_qs(body.decode("utf-8", "replace"))
            nxt = form.get("next", [nxt])[0]
            if not self.auth.cfg():
                err = "No password is set. On the Mac run: python -m rxapp.auth set-password"
            elif self.auth.check_password(form.get("password", [""])[0]):
                safe = nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"
                ck = f"{COOKIE}={self.auth.token()}; Path=/; Max-Age={SESSION_SEC}; HttpOnly; Secure; SameSite=Lax"
                return await self._respond(send, 303, b"", [(b"location", safe.encode()), (b"set-cookie", ck.encode())])
            else:
                err = "Wrong password."
        page = PAGE.format(err=f'<p class="err">{html.escape(err)}</p>' if err else "", next=html.escape(nxt, quote=True))
        return await self._respond(send, 200, page.encode(), [(b"content-type", b"text/html; charset=utf-8"),
                                                              (b"cache-control", b"no-store")])

    @staticmethod
    async def _respond(send, status, body, headers):
        await send({"type": "http.response.start", "status": status, "headers": headers + [(b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def main(argv):
    from .config import CONFIG
    if argv[1:2] != ["set-password"]:
        print("usage: python -m rxapp.auth set-password")
        return 2
    pw = getpass.getpass("New rxapp password: ")
    if len(pw) < 10:
        print("Use at least 10 characters.")
        return 1
    if getpass.getpass("Again: ") != pw:
        print("They don't match.")
        return 1
    Auth(CONFIG.data_dir / "auth.json").set_password(pw)
    print("Saved. Every device is signed out; sign in again with the new password.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
