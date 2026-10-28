"""OAuth 콜백: state 검증 후 관리자 로그인 / 학생 출석 모드로 분기."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import _common as c

# 콜백에서 사용한 흐름 쿠키는 결과와 무관하게 만료
CLEAR = [c.clear_cookie("st"), c.clear_cookie("ev"), c.clear_cookie("am")]


class handler(c.Handler):  # Vercel 규약: 클래스명 handler
    def get(self) -> None:
        q, ck = self.query(), self.cookies()
        state = q.get("state", "")
        if not state or not c.same(state, ck.get("st", "")):
            # 진단용: 값은 남기지 않고 쿠키 이름만
            why = "no-state" if not state else "no-st" if "st" not in ck else "st-mismatch"
            print(f"state check failed: {why} cookies={sorted(ck)}", file=sys.stderr)
            self.message(
                400,
                "요청이 만료되었습니다",
                "QR을 다시 스캔하거나 처음부터 다시 시도해 주세요.",
                CLEAR,
            )
            return
        code = q.get("code", "")
        if not code or "error" in q:
            self.message(400, "로그인이 취소되었습니다", "다시 시도해 주세요.", CLEAR)
            return
        if c.is_admin_mode(ck.get("am", ""), state):
            self.admin_login(code)
        else:
            self.student_checkin(code, ck.get("ev", ""), ck.get("dv", ""))

    # ----- 관리자: 세션 발급만, 출석 기록 없음 -----
    def admin_login(self, code: str) -> None:
        deny = [*CLEAR, c.clear_cookie("adm")]
        admins = c.admin_logins()
        if not admins:
            self.message(
                403, "접근 권한이 없습니다", "관리자 계정으로 등록되어 있지 않습니다.", deny
            )
            return
        try:
            login, _ = c.intra_user(code)
        except Exception:
            self.message(
                502,
                "로그인 실패",
                "42 로그인 처리 중 오류가 발생했습니다. 다시 시도해 주세요.",
                deny,
            )
            return
        if login not in admins:
            self.message(
                403, "접근 권한이 없습니다", "관리자 계정으로 등록되어 있지 않습니다.", deny
            )
            return
        self.redirect(
            "/qr", [*CLEAR, c.set_cookie("adm", c.admin_session_cookie(login), c.ADMIN_TTL)]
        )

    # ----- 학생: 출석 기록 -----
    def student_checkin(self, code: str, ev: str, dv: str) -> None:
        target = c.scan_target(ev)
        if target is None:
            self.message(
                400, "유효하지 않은 QR", "현장에 표시된 QR 코드를 다시 스캔해 주세요.", CLEAR
            )
            return
        event_id, scanned_at = target
        try:
            login, name = c.intra_user(code)
        except Exception:
            self.message(
                502,
                "로그인 실패",
                "42 로그인 처리 중 오류가 발생했습니다. 다시 시도해 주세요.",
                CLEAR,
            )
            return
        # 같은 브라우저로 이 회차에 이미 출석한 다른 계정 (학생에게는 알리지 않고 시트 비고에만)
        seen = c.device_seen(dv)
        shared = sorted({who for sid, who in seen if sid == event_id and who != login})
        payload = {"action": "checkin", "login": login, "id": event_id, "at": scanned_at}
        if shared:
            payload["shared"] = shared
        try:
            # 출석·지각 판정은 로그인 완료가 아니라 스캔 시각 기준
            res = c.gas(payload)
        except Exception:
            self.message(502, "출석 기록 실패", "잠시 후 QR을 다시 스캔해 주세요.", CLEAR)
            return

        result = res.get("result")
        if result == "not_today":
            msg = "오늘 진행하는 회차의 QR이 아닙니다. 출석이 기록되지 않았습니다."
            self.message(400, "출석할 수 없는 QR", msg, CLEAR)
            return
        if result == "not_member":
            msg = f"{login} 계정은 학습자 명단에 없습니다. 운영진에게 문의해 주세요."
            self.message(403, "명단에 없는 계정", msg, CLEAR)
            return
        if result not in ("recorded", "dup"):
            self.message(502, "출석 기록 실패", "잠시 후 QR을 다시 스캔해 주세요.", CLEAR)
            return

        status = str(res.get("status", ""))
        need_reason = res.get("reason_needed") is True
        if result == "dup":
            title, text = "이미 기록됐어요", "이 회차는 이미 기록되어 있어요."
            if need_reason:
                text += " 늦은 사정이 있었다면 아래에 남겨 주세요."
        else:
            title, text = RECORDED.get(status, ("기록됐어요", "출석 기록이 저장됐어요."))
        who = f"{name} ({login})" if name != login else login
        kind = status if status in BADGES else "other"
        badge = BADGES.get(status) or str(res.get("label", ""))
        rows = [
            ("이름", c.esc(who)),
            ("행사", c.esc(str(res.get("name", "")))),
            ("상태", f'<span class="badge {kind}">{c.esc(badge)}</span>' if badge else ""),
            ("시각", c.esc(str(res.get("time", "")))),
        ]
        extra = c.rows_html(rows)
        if need_reason:
            extra += reason_form(c.reason_token(login, event_id))
        mood = "info" if status == "absent" else "warn" if status == "late" else "ok"
        dv_cookie = c.set_cookie("dv", c.device_cookie([*seen, (event_id, login)]), c.DEVICE_TTL)
        self.message(200, title, text, [*CLEAR, dv_cookie], extra, mood=mood)


RECORDED = {
    "present": ("출석 완료!", "오늘도 와 주셔서 고마워요."),
    "late": (
        "지각으로 기록됐어요",
        "지각 인정 시간 안에 도착했어요. 다음엔 조금만 더 일찍 만나요.",
    ),
    "absent": (
        "조금 늦으셨네요",
        "출석 인정 시간이 지나 이번 회차는 결석으로 기록됐어요. "
        "사정이 있었다면 아래에 남겨 주세요. 운영진이 확인할게요.",
    ),
}
# 시트 헤더(예: "결석(무단, 노쇼)") 대신 학생에게 보여 줄 짧은 이름
BADGES = {"present": "출석", "late": "지각", "absent": "결석"}


def reason_form(token: str) -> str:
    return (
        '<form method="post" action="/reason">'
        f'<input type="hidden" name="t" value="{c.esc(token)}">'
        '<label for="reason">어떤 사정이 있었나요?</label>'
        f'<p class="hint">최대 {c.REASON_MAX}자 · 운영진만 볼 수 있어요</p>'
        f'<textarea id="reason" name="reason" rows="3" maxlength="{c.REASON_MAX}" required '
        'placeholder="예: 지하철이 지연됐어요"></textarea>'
        '<button type="submit">사유 보내기</button></form>'
    )
