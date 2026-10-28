"""QR 진입점: 시간 창 서명 검증 → 42 OAuth authorize 로 302."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import _common as c


class handler(c.Handler):  # Vercel 규약: 클래스명 handler
    def get(self) -> None:
        q = self.query()
        event_id = q.get("e", "")
        check = c.check_qr(event_id, q.get("t", ""), q.get("s", ""))
        if check == "expired":
            self.message(
                400,
                "QR이 만료되었어요",
                "QR은 30초마다 바뀌어요. 화면의 최신 QR을 다시 스캔해 주세요.",
            )
            return
        if check != "ok":
            self.message(400, "유효하지 않은 QR", "현장에 표시된 QR 코드를 다시 스캔해 주세요.")
            return
        state = c.new_state()
        self.redirect(
            c.authorize_url(state),
            [
                c.set_cookie("st", state, c.FLOW_TTL),
                # 회차·스캔 시각을 서명해 둔다. 로그인 중 QR 시간 창이 지나도 유효
                c.set_cookie("ev", c.scan_cookie(event_id), c.FLOW_TTL),
                c.clear_cookie("am"),  # 학생 흐름에서는 관리자 모드 해제
            ],
        )
