"""Google OAuth 콜백: 관리자 세션 발급만 (출석 기록 없음)."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import _common as c

CLEAR = [c.clear_cookie("ast")]
DENY = [*CLEAR, c.clear_cookie("adm")]


class handler(c.Handler):  # Vercel 규약: 클래스명 handler
    def get(self) -> None:
        q, ck = self.query(), self.cookies()
        state = q.get("state", "")
        if not state or not c.same(state, ck.get("ast", "")):
            self.message(400, "요청이 만료되었습니다", "/qr 에서 다시 로그인해 주세요.", DENY)
            return
        code = q.get("code", "")
        if not code or "error" in q:
            self.message(400, "로그인이 취소되었습니다", "다시 시도해 주세요.", DENY)
            return
        admins = c.admin_emails()
        if not admins:
            self.message(
                403, "접근 권한이 없습니다", "관리자 계정으로 등록되어 있지 않습니다.", DENY
            )
            return
        try:
            email = c.google_email(code)
        except Exception:
            self.message(
                502,
                "로그인 실패",
                "Google 로그인 처리 중 오류가 발생했습니다. 다시 시도해 주세요.",
                DENY,
            )
            return
        if email not in admins:
            self.message(
                403, "접근 권한이 없습니다", "관리자 계정으로 등록되어 있지 않습니다.", DENY
            )
            return
        self.redirect(
            "/qr", [*CLEAR, c.set_cookie("adm", c.admin_session_cookie(email), c.ADMIN_TTL)]
        )
