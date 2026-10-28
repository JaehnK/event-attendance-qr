"""로컬 HTTP 서버로 핸들러를 띄우고, 42 API·Apps Script 는 가짜로 대체한다."""

import datetime as dt
import http.client
import threading
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

import _common
import callback
import qr
import reason
import start

SECRET = "test-secret-" + "x" * 40
KST = dt.timezone(dt.timedelta(hours=9))
TODAY = dt.datetime.now(KST).strftime("%Y-%m-%d")
YESTERDAY = (dt.datetime.now(KST) - dt.timedelta(days=1)).strftime("%Y-%m-%d")


LABELS = {"present": "출석", "late": "지각", "absent": "결석(무단, 노쇼)", "excused": "공가"}


@dataclass
class FakeGas:
    """apps-script.gs 계약을 흉내내는 메모리 구현. 스캔 시각 판정은 now_status 로 대체."""

    sessions: list[dict[str, str]] = field(
        default_factory=lambda: [
            {"id": "day1-am", "name": "오전 세션 1차", "date": TODAY},
            {"id": "day0-am", "name": "어제 세션", "date": YESTERDAY},
            {"id": "xss", "name": "<img src=x onerror=alert(1)>", "date": TODAY},
        ]
    )
    learners: set[str] = field(default_factory=lambda: {"alice", "bob"})
    now_status: str = "present"
    # (회차ID, login) → {"status", "time", "reason"}
    rows: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    notes: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def today(self) -> list[dict[str, str]]:
        return [{**e, "att": "09:00", "late": "09:10"} for e in self.sessions if e["date"] == TODAY]

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        action = payload["action"]
        if action == "today":
            return {"ok": True, "events": self.today()}
        login, sid = payload["login"], payload["id"]
        key = (sid, login)
        if action == "reason":
            row = self.rows.get(key)
            if not row or row["status"] != "absent":
                return {"ok": True, "saved": False}
            row["reason"] = payload["reason"]
            return {"ok": True, "saved": True}

        assert action == "checkin"
        sessions = {e["id"]: e for e in self.today()}
        if sid not in sessions:
            return {"ok": True, "result": "not_today"}
        if login not in self.learners:
            return {"ok": True, "result": "not_member"}
        now = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        base = {"ok": True, "name": sessions[sid]["name"]}
        row = self.rows.get(key)
        # 비고: 같은 기기로 출석한 계정을 양쪽 행에
        for other in payload.get("shared", []):
            self.notes.setdefault(key, []).append(other)
            if (sid, other) in self.rows:
                self.notes.setdefault((sid, other), []).append(login)
        if row is None:
            row = self.rows[key] = {"status": self.now_status, "time": now, "reason": ""}
            result = "recorded"
        elif row["status"] == "absent" and not row["time"]:
            row["time"] = now  # 자동 결석 후 도착
            result = "recorded"
        else:
            result = "dup"
        return {
            **base,
            "result": result,
            "status": row["status"],
            "label": LABELS[row["status"]],
            "time": row["time"],
            "reason_needed": row["status"] == "absent" and not row["reason"],
        }


@dataclass
class FakeIntra:
    """인가 코드 → 사용자. 'bad' 코드는 42 오류."""

    users: dict[str, tuple[str, str]] = field(
        default_factory=lambda: {
            "code-alice": ("alice", "Alice Kim"),
            "code-bob": ("bob", "Bob <script>alert(1)</script>"),
            "code-admin": ("admin1", "Admin One"),
        }
    )
    calls: list[str] = field(default_factory=list)

    def __call__(self, code: str) -> tuple[str, str]:
        self.calls.append(code)
        if code not in self.users:
            raise _common.UpstreamError
        return self.users[code]


@dataclass
class Response:
    status: int
    headers: http.client.HTTPMessage
    body: str

    @property
    def location(self) -> str:
        return self.headers.get("Location", "")

    @property
    def set_cookies(self) -> list[str]:
        return self.headers.get_all("Set-Cookie") or []

    def cookie(self, name: str) -> str | None:
        for c in self.set_cookies:
            if c.startswith(f"{name}="):
                return c
        return None


class Client:
    """vercel.json rewrites 처럼 경로별로 핸들러에 보내고, 쿠키를 보관하는 브라우저 흉내."""

    def __init__(self, ports: dict[str, int]) -> None:
        self.ports = ports
        self.jar: dict[str, str] = {}

    def get(self, path: str) -> Response:
        return self.request("GET", path)

    def post(self, path: str, form: dict[str, str] | str) -> Response:
        body = form if isinstance(form, str) else urllib.parse.urlencode(form)
        return self.request("POST", path, body.encode())

    def request(self, method: str, path: str, body: bytes | None = None) -> Response:
        route = urllib.parse.urlsplit(path).path
        port = self.ports.get(route, self.ports["/"])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {}
        if self.jar:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.jar.items())
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        resp = Response(res.status, res.headers, res.read().decode())
        conn.close()
        for c in resp.set_cookies:
            pair = c.split(";", 1)[0]
            name, _, value = pair.partition("=")
            if "Max-Age=0" in c:
                self.jar.pop(name, None)
            else:
                self.jar[name] = value
        return resp

    def state_from(self, resp: Response) -> str:
        assert resp.status == 302
        assert resp.location.startswith("https://api.intra.42.fr/oauth/authorize?")
        return urllib.parse.parse_qs(urllib.parse.urlsplit(resp.location).query)["state"][0]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    values = {
        "FT_UID": "uid",
        "FT_SECRET": "ft-secret-value",
        "REDIRECT_URI": "https://example.vercel.app/callback",
        "GAS_URL": "https://script.google.com/macros/s/xyz/exec",
        "GAS_TOKEN": "gas-token-value",
        "SESSION_SECRET": SECRET,
        "BASE_URL": "https://example.vercel.app",
        "ADMIN_LOGINS": " Admin1 , other ",
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    return monkeypatch


@pytest.fixture
def gas(monkeypatch: pytest.MonkeyPatch) -> FakeGas:
    fake = FakeGas()
    monkeypatch.setattr(_common, "gas", fake)
    return fake


@pytest.fixture
def intra(monkeypatch: pytest.MonkeyPatch) -> FakeIntra:
    fake = FakeIntra()
    monkeypatch.setattr(_common, "intra_user", fake)
    return fake


@pytest.fixture
def client(env: pytest.MonkeyPatch, gas: FakeGas, intra: FakeIntra) -> Iterator[Client]:
    servers = {
        "/": ThreadingHTTPServer(("127.0.0.1", 0), start.handler),
        "/callback": ThreadingHTTPServer(("127.0.0.1", 0), callback.handler),
        "/qr": ThreadingHTTPServer(("127.0.0.1", 0), qr.handler),
        "/reason": ThreadingHTTPServer(("127.0.0.1", 0), reason.handler),
    }
    for s in servers.values():
        threading.Thread(target=s.serve_forever, args=(0.01,), daemon=True).start()
    yield Client({k: s.server_address[1] for k, s in servers.items()})
    for s in servers.values():
        s.shutdown()
        s.server_close()
