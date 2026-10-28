"""공용 코드: 환경변수, 서명/쿠키, 42 API·Apps Script 호출, HTML 응답.

Vercel은 `_`로 시작하는 파일을 함수로 만들지 않으므로 각 엔드포인트가 import 해서 쓴다.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from http.server import BaseHTTPRequestHandler
from typing import Any

FT_API = "https://api.intra.42.fr"
EVENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
LOGIN_RE = re.compile(r"[a-z0-9-]{2,20}")
FLOW_TTL = 600  # OAuth 진행용 쿠키(st, ev, am)
ADMIN_TTL = 3600  # 관리자 세션 쿠키(adm)
DEVICE_TTL = 3600  # 기기 쿠키(dv): 이 브라우저에서 출석한 (회차, 계정) 목록
DEVICE_MAX = 20
QR_WINDOW = 30  # QR 서명 시간 창(초). 현재·직전 창만 유효 → 공유 사진은 최대 1분 내 만료
QR_REFRESH = 20  # 관리자 QR 화면 자동 새로고침(초)
REASON_TTL = 1800  # 결석 사유 제출 토큰
REASON_MAX = 200
FORM_MAX = 8192
HTTP_TIMEOUT = 10
GAS_TIMEOUT = 30  # Apps Script 는 동시 출석 시 잠금 대기(최대 25초)가 있어 더 길게
USER_AGENT = "event-attendance/1.0"  # 42 API 는 기본 Python-urllib UA 를 403 으로 막는다


class ConfigError(Exception):
    """필수 환경변수 누락 (사용자에게 원인을 노출하지 않는다)."""


class UpstreamError(Exception):
    """42 API / Apps Script 응답 이상."""


# ---------- 환경변수 ----------


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError
    return value


def admin_logins() -> frozenset[str]:
    """ADMIN_LOGINS 미설정 시 빈 집합 → 모든 관리자 접근 거부."""
    raw = os.environ.get("ADMIN_LOGINS", "")
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


# ---------- 서명 ----------


def _mac(msg: str) -> str:
    return hmac.new(env("SESSION_SECRET").encode(), msg.encode(), hashlib.sha256).hexdigest()


def same(a: object, b: object) -> bool:
    """상수 시간 문자열 비교 (비 ASCII 입력에도 예외 없이 False)."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode(), b.encode())


def qr_window(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // QR_WINDOW)


def qr_sig(event_id: str, window: int) -> str:
    """화면 QR 서명. 접두사로 고정 이벤트 서명·쿠키 서명과 도메인 분리."""
    return _mac(f"qr:{event_id}:{window}")[:16]


def check_qr(event_id: str, window: str, sig: str) -> str:
    """'ok' | 'expired' | 'invalid'. 현재·직전 시간 창만 허용."""
    if not EVENT_ID_RE.fullmatch(event_id) or not re.fullmatch(r"\d{1,12}", window):
        return "invalid"
    w = int(window)
    if not same(sig, qr_sig(event_id, w)):
        return "invalid"
    now = qr_window()
    if w > now:
        return "invalid"
    return "ok" if w >= now - 1 else "expired"


def seal(kind: str, data: dict[str, Any]) -> str:
    """서명된 쿠키 값. kind 를 MAC 에 포함해 이벤트 서명·다른 쿠키와 도메인 분리."""
    raw = json.dumps(data, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{body}.{_mac(f'{kind}:{body}')}"


def unseal(kind: str, token: str) -> dict[str, Any] | None:
    """서명·만료 검증 실패 시 None."""
    body, _, sig = token.partition(".")
    if not body or not same(sig, _mac(f"{kind}:{body}")):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    if not isinstance(exp, int) or exp <= time.time():
        return None
    return data


def scan_cookie(event_id: str) -> str:
    """학생 흐름 쿠키(ev). 판정 기준인 스캔 시각(at)을 서버가 서명해 둔다."""
    now = int(time.time())
    return seal("ev", {"id": event_id, "at": now, "exp": now + FLOW_TTL})


def scan_target(token: str) -> tuple[str, int] | None:
    """ev 쿠키 → (회차ID, 스캔 시각). 위조·만료 시 None."""
    data = unseal("ev", token)
    if not data:
        return None
    event_id, at = data.get("id"), data.get("at")
    if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
        return None
    if not isinstance(at, int):
        return None
    return event_id, at


def device_seen(token: str) -> list[tuple[str, str]]:
    """dv 쿠키 → [(회차ID, login)]. 위조·만료·형식 오류는 빈 목록."""
    data = unseal("dv", token)
    seen = data.get("seen") if data else None
    if not isinstance(seen, list):
        return []
    out = []
    for item in seen:
        if (
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(x, str) for x in item)
            and EVENT_ID_RE.fullmatch(item[0])
            and LOGIN_RE.fullmatch(item[1])
        ):
            out.append((item[0], item[1]))
    return out


def device_cookie(seen: list[tuple[str, str]]) -> str:
    recent = list(dict.fromkeys(seen))[-DEVICE_MAX:]
    return seal("dv", {"seen": [list(x) for x in recent], "exp": int(time.time()) + DEVICE_TTL})


def admin_session_cookie(login: str) -> str:
    return seal("adm", {"login": login, "exp": int(time.time()) + ADMIN_TTL, "role": "admin"})


def admin_session(token: str) -> str | None:
    """유효한 관리자 세션이면 login. 현재 ADMIN_LOGINS 에서 빠진 계정도 거부."""
    data = unseal("adm", token)
    if not data or data.get("role") != "admin":
        return None
    login = data.get("login")
    if not isinstance(login, str) or login not in admin_logins():
        return None
    return login


def admin_mode_cookie(state: str) -> str:
    """관리자 로그인 모드 표시. 이번 OAuth state 에 묶어 다른 흐름에 재사용되지 않게 한다."""
    return seal("mode", {"mode": "admin", "st": state, "exp": int(time.time()) + FLOW_TTL})


def is_admin_mode(token: str, state: str) -> bool:
    data = unseal("mode", token)
    return bool(data) and data.get("mode") == "admin" and same(data.get("st"), state)


def reason_token(login: str, event_id: str) -> str:
    """결석 사유 폼용. 쿠키 없이 (login, 회차) 에 묶인 서명 토큰."""
    return seal("rsn", {"login": login, "id": event_id, "exp": int(time.time()) + REASON_TTL})


def reason_target(token: str) -> tuple[str, str] | None:
    data = unseal("rsn", token)
    if not data:
        return None
    login, event_id = data.get("login"), data.get("id")
    if not isinstance(login, str) or not LOGIN_RE.fullmatch(login):
        return None
    if not isinstance(event_id, str) or not EVENT_ID_RE.fullmatch(event_id):
        return None
    return login, event_id


def new_state() -> str:
    return secrets.token_urlsafe(24)


# ---------- 쿠키 ----------


def set_cookie(name: str, value: str, max_age: int) -> str:
    return f"{name}={value}; Max-Age={max_age}; Path=/; HttpOnly; Secure; SameSite=Lax"


def clear_cookie(name: str) -> str:
    return set_cookie(name, "", 0)


def parse_cookies(header: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name and name not in jar:
            jar[name] = value.strip()
    return jar


# ---------- 외부 호출 ----------


def _request(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = HTTP_TIMEOUT,
) -> Any:
    if not url.startswith("https://"):
        raise ConfigError
    # https 만 허용 (위에서 확인)
    req = urllib.request.Request(  # noqa: S310
        url,
        data=data,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:  # noqa: S310
            return json.load(res)
    except urllib.error.HTTPError as exc:
        # 진단용: 호스트와 상태 코드만 (쿼리·본문은 남기지 않음)
        host = urllib.parse.urlsplit(url).hostname
        print(f"upstream {host} HTTP {exc.code}", file=sys.stderr)
        raise UpstreamError from None


def authorize_url(state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": env("FT_UID"),
            "redirect_uri": env("REDIRECT_URI"),
            "response_type": "code",
            "scope": "public",
            "state": state,
        }
    )
    return f"{FT_API}/oauth/authorize?{query}"


def intra_user(code: str) -> tuple[str, str]:
    """인가 코드 → (login, 표시 이름)."""
    form = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": env("FT_UID"),
            "client_secret": env("FT_SECRET"),
            "code": code,
            "redirect_uri": env("REDIRECT_URI"),
        }
    ).encode()
    tok = _request(
        f"{FT_API}/oauth/token",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    access = tok.get("access_token") if isinstance(tok, dict) else None
    if not isinstance(access, str) or not access:
        raise UpstreamError
    me = _request(f"{FT_API}/v2/me", headers={"Authorization": f"Bearer {access}"})
    login = me.get("login") if isinstance(me, dict) else None
    if not isinstance(login, str) or not LOGIN_RE.fullmatch(login.lower()):
        raise UpstreamError
    name = me.get("displayname")
    return login.lower(), name if isinstance(name, str) and name else login


def gas(payload: dict[str, Any]) -> dict[str, Any]:
    """Apps Script 웹앱 호출. ok 가 아니면 UpstreamError."""
    body = json.dumps({**payload, "token": env("GAS_TOKEN")}).encode()
    res = _request(
        env("GAS_URL"),
        data=body,
        headers={"Content-Type": "application/json"},
        timeout=GAS_TIMEOUT,
    )
    if not isinstance(res, dict) or res.get("ok") is not True:
        raise UpstreamError
    return res


# ---------- HTML ----------

esc = html.escape

_CSS = """
:root{
  color-scheme:light dark;
  --bg:#f4f5f7;--card:#fff;--text:#1d2129;--muted:#6b7280;--line:#e5e7eb;
  --accent:#3b5bdb;--accent-text:#fff;
  --ok:#2f9e44;--ok-bg:#ebfbee;--warn:#e67700;--warn-bg:#fff4e6;
  --info:#3b5bdb;--info-bg:#edf2ff;--error:#e03131;--error-bg:#fff5f5;
  font-family:system-ui,-apple-system,"Apple SD Gothic Neo","Noto Sans KR",sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#111318;--card:#1b1e25;--text:#e9ecef;--muted:#9aa1ab;--line:#2c313a;
  --accent:#748ffc;--accent-text:#111318;
  --ok:#69db7c;--ok-bg:#1e3324;--warn:#ffa94d;--warn-bg:#3a2a17;
  --info:#91a7ff;--info-bg:#20263d;--error:#ff8787;--error-bg:#3b1f22;
}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;padding:32px 16px;line-height:1.6;
  background:var(--bg);color:var(--text);display:flex;align-items:flex-start;justify-content:center}
main{width:100%;max-width:440px;background:var(--card);border-radius:20px;padding:32px 24px;
  box-shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.06);text-align:center}
main.wide{max-width:1200px;background:transparent;box-shadow:none;padding:0}
h1{font-size:1.4rem;margin:0 0 8px;line-height:1.35;word-break:keep-all}
p{margin:0 0 4px}
.lead{color:var(--muted);word-break:keep-all}
.icon{width:56px;height:56px;border-radius:50%;margin:0 auto 16px;display:grid;place-items:center;
  font-size:1.6rem;font-weight:700}
.icon.ok{background:var(--ok-bg);color:var(--ok)}.icon.warn{background:var(--warn-bg);color:var(--warn)}
.icon.info{background:var(--info-bg);color:var(--info)}
.icon.error{background:var(--error-bg);color:var(--error)}
dl{margin:24px 0 0;text-align:left;border-top:1px solid var(--line)}
dl div{display:flex;justify-content:space-between;gap:16px;padding:10px 2px;
  border-bottom:1px solid var(--line)}
dt{color:var(--muted)}dd{margin:0;text-align:right;font-weight:600;word-break:break-all}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.9rem}
.badge.present{background:var(--ok-bg);color:var(--ok)}.badge.late{background:var(--warn-bg);color:var(--warn)}
.badge.absent,.badge.other{background:var(--info-bg);color:var(--info)}
form{display:grid;gap:10px;margin-top:28px;text-align:left}
label{font-weight:600}
.hint{color:var(--muted);font-size:.85rem;margin-top:-4px}
textarea{font:inherit;color:inherit;background:var(--bg);
  border:1px solid var(--line);border-radius:12px;padding:12px;width:100%;
  resize:vertical;min-height:96px}
textarea:focus{outline:2px solid var(--accent);outline-offset:1px;border-color:transparent}
button{font:inherit;font-weight:600;border:0;border-radius:12px;padding:12px 16px;cursor:pointer;
  background:var(--accent);color:var(--accent-text)}
.muted{color:var(--muted);font-size:.85rem;word-break:break-all}
.top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
  flex-wrap:wrap;margin:0 4px 12px}
.top h1{margin:0}
.timer{height:4px;border-radius:2px;background:var(--line);overflow:hidden;margin:0 4px 24px}
.timer i{display:block;height:100%;background:var(--accent);transform-origin:left;
  animation:countdown var(--refresh) linear forwards}
@keyframes countdown{from{transform:scaleX(1)}to{transform:scaleX(0)}}
@media (prefers-reduced-motion:reduce){.timer i{animation:none}}
.grid{display:grid;gap:24px;grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr))}
.qr{background:var(--card);border-radius:20px;padding:28px 24px;text-align:center;
  box-shadow:0 1px 2px rgba(0,0,0,.04),0 8px 24px rgba(0,0,0,.06)}
.qr h2{font-size:1.5rem;margin:0 0 16px;word-break:keep-all}
.qr svg{display:block;width:min(100%,70vh,560px);height:auto;aspect-ratio:1;
  margin:0 auto;border-radius:12px}
.chips{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin-top:16px}
.chips span{padding:4px 12px;border-radius:999px;font-weight:600}
details{margin-top:12px}summary{cursor:pointer}
"""

_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


ICONS = {"ok": "✓", "warn": "!", "info": "i", "error": "\u00d7"}


def page(title: str, body: str, *, wide: bool = False, refresh: int = 0) -> bytes:
    """title 은 이스케이프한다. body 는 호출 측에서 이스케이프된 HTML 이어야 한다."""
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh > 0 else ""
    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"{meta}<title>{esc(title)}</title><style>{_CSS}</style></head>"
        f'<body><main class="{"wide" if wide else ""}">{body}</main></body></html>'
    ).encode()


def rows_html(rows: Sequence[tuple[str, str]]) -> str:
    """(라벨, 이미 이스케이프된 HTML 값) 목록 → 정의 목록."""
    items = "".join(f"<div><dt>{esc(k)}</dt><dd>{v}</dd></div>" for k, v in rows if v)
    return f"<dl>{items}</dl>"


class Handler(BaseHTTPRequestHandler):
    """공통 응답/예외 처리. 각 엔드포인트는 get() / post() 를 구현한다."""

    def do_GET(self) -> None:
        self._run(self.get)

    def do_POST(self) -> None:
        self._run(self.post)

    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except ConfigError:
            print("config error", file=sys.stderr)
            self.message(
                500, "서버 오류", "일시적인 오류가 발생했습니다. 운영진에게 문의해 주세요."
            )
        except Exception as exc:
            # 메시지·스택트레이스는 응답과 로그에 남기지 않는다 (시크릿 포함 가능성)
            print(f"unhandled {type(exc).__name__}", file=sys.stderr)
            self.message(
                500, "서버 오류", "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
            )

    def get(self) -> None:
        self.message(405, "잘못된 요청", "허용되지 않는 요청입니다.")

    def post(self) -> None:
        self.message(405, "잘못된 요청", "허용되지 않는 요청입니다.")

    def log_message(self, format: str, *args: Any) -> None:
        """기본 접근 로그는 쿼리(code, 서명)를 남기므로 끈다."""

    def query(self) -> dict[str, str]:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        return {k: v[0] for k, v in qs.items()}

    def form(self) -> dict[str, str] | None:
        """application/x-www-form-urlencoded 본문. 크기 초과·형식 오류 시 None."""
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return None
        if not 0 <= n <= FORM_MAX:
            return None
        raw = self.rfile.read(n).decode("utf-8", "replace")
        qs = urllib.parse.parse_qs(raw, max_num_fields=10)
        return {k: v[0] for k, v in qs.items()}

    def cookies(self) -> dict[str, str]:
        return parse_cookies(self.headers.get("Cookie", ""))

    def send(
        self, status: int, body: bytes, cookies: Sequence[str] = (), location: str = ""
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _CSP)
        if location:
            self.send_header("Location", location)
        for c in cookies:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    def message(
        self,
        status: int,
        title: str,
        text: str,
        cookies: Sequence[str] = (),
        extra: str = "",
        mood: str = "",
        refresh: int = 0,
    ) -> None:
        """extra 는 이미 이스케이프된 HTML. mood 미지정 시 상태 코드로 정한다."""
        mood = mood or ("ok" if status < 300 else "warn" if status < 500 else "error")
        body = (
            f'<div class="icon {mood}" aria-hidden="true">{ICONS[mood]}</div>'
            f'<h1>{esc(title)}</h1><p class="lead">{esc(text)}</p>{extra}'
        )
        self.send(status, page(title, body, refresh=refresh), cookies)

    def redirect(self, location: str, cookies: Sequence[str] = ()) -> None:
        self.send(302, b"", cookies, location)
