import base64
import html
import io
import json
import re
import time
import urllib.parse
from email.message import Message

import pytest

import _common
from conftest import SECRET, Client, FakeGas, FakeIntra, Response


def qr_path(event_id: str, sig: str | None = None, window: int | None = None) -> str:
    w = _common.qr_window() if window is None else window
    s = _common.qr_sig(event_id, w) if sig is None else sig
    return "/?" + urllib.parse.urlencode({"e": event_id, "t": w, "s": s})


def student_login(client: Client, event_id: str, code: str) -> tuple[str, Response]:
    r = client.get(qr_path(event_id))
    state = client.state_from(r)
    return state, client.get(f"/callback?code={code}&state={state}")


# ---------- 학생 출석 ----------


def test_student_checkin_success(client: Client, gas: FakeGas) -> None:
    r = client.get(qr_path("day1-am"))
    assert r.status == 302
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.location).query)
    assert q["scope"] == ["public"]
    assert q["redirect_uri"] == ["https://example.vercel.app/callback"]
    for name in ("st", "ev"):
        c = r.cookie(name)
        assert c is not None
        assert "Max-Age=600" in c
        for attr in ("HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
            assert attr in c

    r = client.get(f"/callback?code=code-alice&state={q['state'][0]}")
    assert r.status == 200
    assert "출석 완료" in r.body
    assert "Alice Kim (alice)" in r.body
    assert '<span class="badge present">출석</span>' in r.body
    assert "오전 세션 1차" in r.body
    assert "<form" not in r.body
    assert list(gas.rows) == [("day1-am", "alice")]
    assert gas.rows["day1-am", "alice"]["status"] == "present"
    # 사용한 흐름 쿠키 만료
    for name in ("st", "ev", "am"):
        assert "Max-Age=0" in (r.cookie(name) or "")


def test_student_duplicate(client: Client, gas: FakeGas) -> None:
    student_login(client, "day1-am", "code-alice")
    _, r = student_login(client, "day1-am", "code-alice")
    assert r.status == 200
    assert "이미 기록됐어요" in r.body
    assert "출석" in r.body
    assert len(gas.rows) == 1


@pytest.mark.parametrize("event_id", ["day0-am", "unknown"])
def test_past_or_unknown_session_not_recorded(client: Client, gas: FakeGas, event_id: str) -> None:
    _, r = student_login(client, event_id, "code-alice")
    assert r.status == 400
    assert "오늘 진행하는 회차의 QR이 아닙니다" in r.body
    assert not gas.rows


@pytest.mark.parametrize(
    "make_path",
    [
        lambda: "/",
        lambda: "/?e=day1-am",
        lambda: qr_path("day1-am", "0" * 16),
        lambda: qr_path("day1-am", _common.qr_sig("day1-am", _common.qr_window())[:15]),
        # 예전 고정 서명(HMAC(id)) QR 은 더 이상 통하지 않는다
        lambda: qr_path("day1-am", _common._mac("day1-am")[:16]),
        # 시간 창 누락·형식 오류·미래 창
        lambda: "/?" + urllib.parse.urlencode({"e": "day1-am", "s": _common._mac("day1-am")[:16]}),
        lambda: qr_path("day1-am", window=-1),
        lambda: qr_path("day1-am", window=_common.qr_window() + 1),
        lambda: qr_path("day1-am", "가나다"),  # 비 ASCII 서명도 500 아닌 400
        lambda: qr_path("bad.id"),  # 형식 위반은 서명 맞아도 거부
        lambda: qr_path("a" * 41),
    ],
)
def test_bad_signature_rejected(client: Client, make_path) -> None:
    r = client.get(make_path())
    assert r.status == 400
    assert "유효하지 않은 QR" in r.body
    assert r.set_cookies == []


def test_qr_rotation_window(client: Client, gas: FakeGas) -> None:
    now = _common.qr_window()
    # 직전 창은 허용 (화면 새로고침 직전 스캔)
    r = client.get(qr_path("day1-am", window=now - 1))
    assert r.status == 302
    # ev 쿠키는 회차·스캔 시각을 서명해 담는다 (로그인 중 창이 바뀌어도 콜백에서 유효)
    target = _common.scan_target(client.jar["ev"])
    assert target is not None
    assert target[0] == "day1-am"
    assert abs(target[1] - time.time()) < 5
    # 그보다 오래된 QR(공유된 사진)은 만료 안내
    r = client.get(qr_path("day1-am", window=now - 2))
    assert r.status == 400
    assert "QR이 만료되었어요" in r.body
    assert r.set_cookies == []


def test_qr_page_error_keeps_refreshing(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    client.jar["adm"] = _common.admin_session_cookie("admin1")

    def boom(_: object) -> None:
        raise _common.UpstreamError

    monkeypatch.setattr(_common, "gas", boom)
    r = client.get("/qr")
    assert r.status == 502
    assert 'http-equiv="refresh"' in r.body


def test_state_mismatch(client: Client, gas: FakeGas, intra: FakeIntra) -> None:
    client.get(qr_path("day1-am"))
    r = client.get("/callback?code=code-alice&state=forged")
    assert r.status == 400
    assert "만료" in r.body
    assert intra.calls == []
    assert gas.calls == []


def test_state_cookie_missing(client: Client, intra: FakeIntra) -> None:
    state = client.state_from(client.get(qr_path("day1-am")))
    client.jar.pop("st")
    r = client.get(f"/callback?code=code-alice&state={state}")
    assert r.status == 400
    assert intra.calls == []


@pytest.mark.parametrize("kind", ["old-format", "other-purpose", "expired", "swapped-id"])
def test_tampered_ev_cookie(client: Client, gas: FakeGas, kind: str) -> None:
    state = client.state_from(client.get(qr_path("day1-am")))
    now = int(time.time())
    data = {"id": "day1-am", "at": now - 3600, "exp": now + 600}
    sig = client.jar["ev"].partition(".")[2]
    client.jar["ev"] = {
        "old-format": "day1-am." + _common._mac("day1-am")[:16],
        "other-purpose": _common.seal("rsn", data),  # 스캔 시각 앞당기기 시도
        "expired": _common.seal("ev", {**data, "exp": now - 1}),
        "swapped-id": _forge({"id": "day0-am", "at": now, "exp": now + 600}) + "." + sig,
    }[kind]
    r = client.get(f"/callback?code=code-alice&state={state}")
    assert r.status == 400
    assert gas.calls == []


def test_checkin_sends_scan_time(client: Client, gas: FakeGas) -> None:
    # 로그인이 오래 걸려도 판정은 QR 을 스캔한 시각 기준
    before = int(time.time())
    student_login(client, "day1-am", "code-alice")
    call = next(x for x in gas.calls if x["action"] == "checkin")
    assert before <= call["at"] <= int(time.time())
    assert call["id"] == "day1-am"


def test_oauth_denied(client: Client, gas: FakeGas) -> None:
    state = client.state_from(client.get(qr_path("day1-am")))
    r = client.get(f"/callback?error=access_denied&state={state}")
    assert r.status == 400
    assert gas.calls == []


def test_output_is_escaped(client: Client) -> None:
    _, r = student_login(client, "xss", "code-bob")
    assert r.status == 200
    assert "<script>" not in r.body
    assert "<img" not in r.body
    assert "&lt;script&gt;" in r.body
    assert "&lt;img src=x onerror=alert(1)&gt;" in r.body


def test_upstream_errors_hide_details(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: object) -> None:
        raise RuntimeError("secret-detail gas-token-value")

    monkeypatch.setattr(_common, "gas", boom)
    _, r = student_login(client, "day1-am", "code-alice")
    assert r.status == 502
    assert "secret-detail" not in r.body
    assert "Traceback" not in r.body

    _, r = student_login(client, "day1-am", "unknown-code")
    assert r.status == 502


def test_missing_env_is_generic_500(client: Client, env: pytest.MonkeyPatch) -> None:
    env.delenv("FT_UID")
    r = client.get(qr_path("day1-am"))
    assert r.status == 500
    assert "FT_UID" not in r.body
    assert "Traceback" not in r.body


def test_unexpected_exception_is_generic_500(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_: str) -> str:
        raise KeyError("ft-secret-value")

    monkeypatch.setattr(_common, "authorize_url", boom)
    r = client.get(qr_path("day1-am"))
    assert r.status == 500
    assert "ft-secret-value" not in r.body


def reason_token_in(body: str) -> str:
    m = re.search(r'name="t" value="([^"]+)"', body)
    assert m is not None
    return html.unescape(m.group(1))


def test_late_status(client: Client, gas: FakeGas) -> None:
    gas.now_status = "late"
    _, r = student_login(client, "day1-am", "code-alice")
    assert r.status == 200
    assert "지각으로 기록됐어요" in r.body
    assert "<form" not in r.body
    assert gas.rows["day1-am", "alice"]["status"] == "late"


def test_absent_after_cutoff_asks_reason(client: Client, gas: FakeGas) -> None:
    gas.now_status = "absent"
    _, r = student_login(client, "day1-am", "code-alice")
    assert r.status == 200
    assert "결석으로 기록됐어요" in r.body
    assert 'action="/reason"' in r.body
    assert "form-action 'self'" in r.headers["Content-Security-Policy"]
    token = reason_token_in(r.body)

    res = client.post("/reason", {"t": token, "reason": "  지하철\n 지연 <b>x</b> "})
    assert res.status == 200
    assert "사유가 전달됐어요" in res.body
    assert "<b>" not in res.body
    assert gas.rows["day1-am", "alice"]["reason"] == "지하철 지연 <b>x</b>"
    assert gas.calls[-1] == {
        "action": "reason",
        "login": "alice",
        "id": "day1-am",
        "reason": "지하철 지연 <b>x</b>",
    }

    # 사유 제출 후 재스캔: 폼 없이 이미 기록됨
    _, r = student_login(client, "day1-am", "code-alice")
    assert "이미 기록됐어요" in r.body
    assert "<form" not in r.body


def test_scan_after_auto_absent_records_time_and_asks_reason(client: Client, gas: FakeGas) -> None:
    gas.rows["day1-am", "alice"] = {"status": "absent", "time": "", "reason": ""}
    _, r = student_login(client, "day1-am", "code-alice")
    assert r.status == 200
    assert "결석으로 기록됐어요" in r.body
    assert gas.rows["day1-am", "alice"]["time"]
    assert 'action="/reason"' in r.body


def test_not_member_rejected(client: Client, gas: FakeGas) -> None:
    gas.learners.discard("alice")
    _, r = student_login(client, "day1-am", "code-alice")
    assert r.status == 403
    assert "학습자 명단에 없습니다" in r.body
    assert not gas.rows


@pytest.mark.parametrize("kind", ["garbage", "expired", "other-purpose", "bad-login"])
def test_reason_bad_token(client: Client, gas: FakeGas, kind: str) -> None:
    now = int(time.time())
    token = {
        "garbage": "abc.def",
        "expired": _common.seal("rsn", {"login": "alice", "id": "day1-am", "exp": now - 1}),
        "other-purpose": _common.admin_session_cookie("alice"),
        "bad-login": _common.seal("rsn", {"login": "../x", "id": "day1-am", "exp": now + 99}),
    }[kind]
    r = client.post("/reason", {"t": token, "reason": "사유"})
    assert r.status == 400
    assert "만료" in r.body
    assert gas.calls == []


@pytest.mark.parametrize("text", ["", "   ", "가" * 201])
def test_reason_length(client: Client, gas: FakeGas, text: str) -> None:
    token = _common.reason_token("alice", "day1-am")
    r = client.post("/reason", {"t": token, "reason": text})
    assert r.status == 400
    assert gas.calls == []


def test_reason_only_for_absent(client: Client, gas: FakeGas) -> None:
    student_login(client, "day1-am", "code-alice")  # 출석
    r = client.post("/reason", {"t": _common.reason_token("alice", "day1-am"), "reason": "x"})
    assert r.status == 400
    assert "결석으로 기록된 회차에만" in r.body
    assert gas.rows["day1-am", "alice"]["reason"] == ""


def test_reason_rejects_get_and_oversized(client: Client, gas: FakeGas) -> None:
    assert client.get("/reason").status == 405
    big = "t=x&reason=" + "a" * 9000
    assert client.post("/reason", big).status == 400
    assert gas.calls == []


def test_shared_device_flagged_silently(client: Client, gas: FakeGas) -> None:
    _, r = student_login(client, "day1-am", "code-alice")
    dv = r.cookie("dv")
    assert dv is not None
    for attr in ("Max-Age=3600", "HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
        assert attr in dv
    assert gas.calls[-1]["action"] == "checkin"
    assert "shared" not in gas.calls[-1]

    # 같은 브라우저로 다른 계정 출석 → 출석은 정상, 학생 화면엔 표시 없음
    _, r = student_login(client, "day1-am", "code-bob")
    assert r.status == 200
    assert "출석 완료" in r.body
    assert "중복" not in r.body
    assert gas.calls[-1]["shared"] == ["alice"]
    assert gas.notes["day1-am", "bob"] == ["alice"]
    assert gas.notes["day1-am", "alice"] == ["bob"]

    # 다른 회차는 별개
    gas.sessions.append({"id": "day1-pm", "name": "오후", "date": gas.sessions[0]["date"]})
    student_login(client, "day1-pm", "code-alice")
    assert "shared" not in gas.calls[-1]


def test_device_cookie_forged_or_expired_ignored(client: Client, gas: FakeGas) -> None:
    now = int(time.time())
    for token in (
        _common.seal("dv", {"seen": [["day1-am", "alice"]], "exp": now - 1}),
        _common.seal("rsn", {"seen": [["day1-am", "alice"]], "exp": now + 99}),
        _forge({"seen": [["day1-am", "alice"]], "exp": now + 99}) + ".00",
        _common.seal("dv", {"seen": [["day1-am", "../x"], "bad", 3], "exp": now + 99}),
    ):
        client.jar["dv"] = token
        student_login(client, "day1-am", "code-bob")
        assert "shared" not in gas.calls[-1]
        gas.rows.clear()


def test_device_cookie_not_set_when_not_recorded(client: Client, gas: FakeGas) -> None:
    gas.learners.discard("alice")
    _, r = student_login(client, "day1-am", "code-alice")
    assert r.status == 403
    assert r.cookie("dv") is None


# ---------- 관리자 ----------


def admin_login(client: Client, code: str = "code-admin") -> tuple[Response, Response]:
    r = client.get("/qr")
    state = client.state_from(r)
    return r, client.get(f"/callback?code={code}&state={state}")


def test_admin_login_and_qr(client: Client, gas: FakeGas) -> None:
    r, cb = admin_login(client)
    am = r.cookie("am")
    assert am is not None
    assert "HttpOnly" in am
    assert "Secure" in am
    assert "SameSite=Lax" in am
    assert "Max-Age=0" in (r.cookie("ev") or "")

    assert cb.status == 302
    assert cb.location == "/qr"
    adm = cb.cookie("adm")
    assert adm is not None
    for attr in ("Max-Age=3600", "HttpOnly", "Secure", "SameSite=Lax", "Path=/"):
        assert attr in adm
    # 관리자 로그인은 출석을 기록하지 않는다
    assert all(call["action"] != "checkin" for call in gas.calls)
    assert not gas.rows

    page = client.get("/qr")
    assert page.status == 200
    assert page.headers["Cache-Control"] == "no-store"
    assert "<svg" in page.body
    assert "오전 세션 1차" in page.body
    assert "출석 ~09:00" in page.body
    assert "지각 ~09:10" in page.body
    assert f'<meta http-equiv="refresh" content="{_common.QR_REFRESH}">' in page.body
    assert 'viewBox="' in page.body  # 크기 고정 없이 CSS로 맞춰 가운데 정렬
    assert "어제 세션" not in page.body
    assert "<img" not in page.body  # 이벤트명 이스케이프
    w = _common.qr_window()
    sigs = {f"t={x}&amp;s={_common.qr_sig('day1-am', x)}" for x in (w, w - 1)}
    assert any(f"https://example.vercel.app/?e=day1-am&amp;{x}" in page.body for x in sigs)


def test_admin_session_payload(client: Client) -> None:
    admin_login(client)
    body, _, sig = client.jar["adm"].partition(".")
    data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert data["login"] == "admin1"
    assert data["role"] == "admin"
    assert 3500 < data["exp"] - time.time() <= 3600
    assert len(sig) == 64


def test_non_admin_gets_403_and_no_checkin(client: Client, gas: FakeGas) -> None:
    _, cb = admin_login(client, "code-alice")
    assert cb.status == 403
    assert cb.cookie("adm") is None or "Max-Age=0" in (cb.cookie("adm") or "")
    assert gas.calls == []
    assert "adm" not in client.jar
    # 여전히 QR 볼 수 없음 → 다시 로그인으로
    r = client.get("/qr")
    assert r.status == 302
    assert "오전 세션" not in r.body


def test_qr_requires_login(client: Client, gas: FakeGas) -> None:
    r = client.get("/qr")
    assert r.status == 302
    assert r.headers["Cache-Control"] == "no-store"
    assert r.body == ""
    assert gas.calls == []  # 비인증 상태에서 이벤트 조회 안 함


def _forge(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.mark.parametrize(
    "cookie",
    [
        "garbage",
        "",
        # 서명 없는 페이로드
        _forge({"login": "admin1", "exp": int(time.time()) + 999, "role": "admin"}) + ".00",
        # 다른 비밀키로 서명
        "WRONG",
        # 만료
        "EXPIRED",
        # role 누락
        "NOROLE",
        # 다른 용도(mode) 쿠키 재사용
        "MODE",
    ],
)
def test_forged_or_expired_adm_cookie(
    client: Client, gas: FakeGas, env: pytest.MonkeyPatch, cookie: str
) -> None:
    now = int(time.time())
    if cookie == "WRONG":
        env.setenv("SESSION_SECRET", "another-secret")
        cookie = _common.admin_session_cookie("admin1")
        env.setenv("SESSION_SECRET", SECRET)
    elif cookie == "EXPIRED":
        cookie = _common.seal("adm", {"login": "admin1", "exp": now - 1, "role": "admin"})
    elif cookie == "NOROLE":
        cookie = _common.seal("adm", {"login": "admin1", "exp": now + 999})
    elif cookie == "MODE":
        cookie = _common.admin_mode_cookie("x")
    client.jar["adm"] = cookie
    r = client.get("/qr")
    assert r.status == 302  # 재로그인 유도
    assert "Max-Age=0" in (r.cookie("adm") or "")
    assert gas.calls == []


def test_tampered_login_in_adm_cookie(client: Client, gas: FakeGas) -> None:
    admin_login(client)
    _, _, sig = client.jar["adm"].partition(".")
    payload = _forge({"login": "admin1", "exp": int(time.time()) + 99999, "role": "admin"})
    client.jar["adm"] = f"{payload}.{sig}"
    assert client.get("/qr").status == 302


def test_removed_admin_session_invalid(client: Client, env: pytest.MonkeyPatch) -> None:
    admin_login(client)
    assert client.get("/qr").status == 200
    env.setenv("ADMIN_LOGINS", "other")
    assert client.get("/qr").status == 302


@pytest.mark.parametrize("value", [None, "", " , "])
def test_admin_logins_unset_denies_all(
    client: Client, env: pytest.MonkeyPatch, intra: FakeIntra, gas: FakeGas, value: str | None
) -> None:
    # 미리 유효한 세션·관리자 모드 쿠키를 확보해 둔 뒤 설정을 비운다
    client.jar["adm"] = _common.admin_session_cookie("admin1")
    r = client.get("/qr")
    assert r.status == 200
    state = "s" * 32
    client.jar["st"] = state
    client.jar["am"] = _common.admin_mode_cookie(state)

    if value is None:
        env.delenv("ADMIN_LOGINS")
    else:
        env.setenv("ADMIN_LOGINS", value)

    r = client.get("/qr")
    assert r.status == 403
    assert "오전 세션" not in r.body
    gas.calls.clear()
    client.jar["st"] = state
    client.jar["am"] = _common.admin_mode_cookie(state)
    cb = client.get(f"/callback?code=code-admin&state={state}")
    assert cb.status == 403
    assert cb.cookie("adm") is not None
    assert "Max-Age=0" in (cb.cookie("adm") or "")
    assert gas.calls == []


def test_student_opening_qr_cannot_become_admin_or_check_in(client: Client, gas: FakeGas) -> None:
    # 학생이 QR 스캔 후 /qr 도 열었다면, 마지막 흐름(관리자 모드)만 유효
    client.get(qr_path("day1-am"))
    r = client.get("/qr")
    state = client.state_from(r)
    cb = client.get(f"/callback?code=code-alice&state={state}")
    assert cb.status == 403
    assert not gas.rows


def test_stale_admin_mode_cookie_does_not_hijack_student_flow(client: Client, gas: FakeGas) -> None:
    # 관리자 모드 쿠키가 남아 있어도 state 에 묶여 있으므로 학생 흐름은 정상 출석
    client.get("/qr")
    stale_am = client.jar["am"]
    state = client.state_from(client.get(qr_path("day1-am")))
    client.jar["am"] = stale_am
    r = client.get(f"/callback?code=code-alice&state={state}")
    assert r.status == 200
    assert "출석 완료" in r.body
    assert len(gas.rows) == 1


def test_admin_cookie_is_not_event_signature(env: pytest.MonkeyPatch) -> None:
    # 이벤트 서명(HMAC(id))과 쿠키 서명(HMAC("adm:"+body))은 도메인 분리
    body = _forge({"login": "admin1", "exp": int(time.time()) + 999, "role": "admin"})
    assert _common.unseal("adm", f"{body}.{_common._mac(body)}") is None
    assert _common.unseal("adm", f"{body}.{_common._mac('adm:' + body)}") is not None


# ---------- 외부 호출 래퍼 ----------


def test_gas_wrapper(env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict = {}

    def fake_request(url: str, *, data: bytes | None = None, headers=None, timeout=None):
        assert data is not None
        sent.update(url=url, body=json.loads(data), headers=headers, timeout=timeout)
        return {"ok": True, "events": []}

    monkeypatch.setattr(_common, "_request", fake_request)
    assert _common.gas({"action": "today"}) == {"ok": True, "events": []}
    assert sent["body"] == {"action": "today", "token": "gas-token-value"}
    assert sent["url"].startswith("https://script.google.com/")
    assert sent["timeout"] == _common.GAS_TIMEOUT

    monkeypatch.setattr(_common, "_request", lambda *a, **k: {"ok": False, "msg": "unauthorized"})
    with pytest.raises(_common.UpstreamError):
        _common.gas({"action": "today"})


def test_intra_user_wrapper(env: pytest.MonkeyPatch, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, bytes | None, dict]] = []

    def fake_request(url: str, *, data=None, headers=None):
        calls.append((url, data, headers or {}))
        if url.endswith("/oauth/token"):
            return {"access_token": "tok"}
        return {"login": "Alice", "displayname": "Alice Kim"}

    monkeypatch.setattr(_common, "_request", fake_request)
    assert _common.intra_user("abc") == ("alice", "Alice Kim")
    token_body = calls[0][1]
    assert token_body is not None
    form = urllib.parse.parse_qs(token_body.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["abc"]
    assert calls[1][2]["Authorization"] == "Bearer tok"

    monkeypatch.setattr(_common, "_request", lambda *a, **k: {"login": "../evil"})
    with pytest.raises(_common.UpstreamError):
        _common.intra_user("abc")


def test_request_rejects_non_https(env: pytest.MonkeyPatch) -> None:
    env.setenv("GAS_URL", "file:///etc/passwd")
    with pytest.raises(_common.ConfigError):
        _common.gas({"action": "today"})


def test_request_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class Res(io.BytesIO):
        pass

    seen: list = []

    def fake_urlopen(req, timeout):
        seen.append(req)
        return Res(b'{"ok": true}')

    monkeypatch.setattr(_common.urllib.request, "urlopen", fake_urlopen)
    assert _common._request("https://example.com") == {"ok": True}
    # 42 API 는 기본 Python-urllib UA 를 403 으로 막는다
    assert seen[0].get_header("User-agent") == _common.USER_AGENT


def test_request_http_error_is_upstream_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_urlopen(req, timeout):
        raise _common.urllib.error.HTTPError(req.full_url, 403, "Forbidden", Message(), None)

    monkeypatch.setattr(_common.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(_common.UpstreamError):
        _common._request("https://api.intra.42.fr/oauth/token?code=secret-code")
    err = capsys.readouterr().err
    assert "api.intra.42.fr HTTP 403" in err
    assert "secret-code" not in err
