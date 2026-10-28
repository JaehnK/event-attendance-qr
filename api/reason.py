"""결석 사유 제출: 콜백 화면의 폼(서명 토큰)으로만 받는다."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import _common as c


class handler(c.Handler):  # Vercel 규약: 클래스명 handler
    def post(self) -> None:
        form = self.form()
        target = c.reason_target(form.get("t", "")) if form else None
        if form is None or target is None:
            self.message(400, "요청이 만료되었어요", "QR을 다시 스캔한 뒤 사유를 입력해 주세요.")
            return
        text = " ".join(form.get("reason", "").split())
        if not 1 <= len(text) <= c.REASON_MAX:
            msg = f"사유를 1~{c.REASON_MAX}자로 적어 주세요. 뒤로 가면 다시 보낼 수 있어요."
            self.message(400, "사유를 확인해 주세요", msg, mood="info")
            return
        login, event_id = target
        try:
            res = c.gas({"action": "reason", "login": login, "id": event_id, "reason": text})
        except Exception:
            self.message(502, "전송하지 못했어요", "잠시 후 뒤로 가서 다시 보내 주세요.")
            return
        if res.get("saved") is not True:
            self.message(
                400,
                "사유를 제출할 수 없습니다",
                "결석으로 기록된 회차에만 사유를 남길 수 있습니다.",
            )
            return
        extra = c.rows_html([("계정", c.esc(login)), ("사유", c.esc(text))])
        self.message(
            200, "사유가 전달됐어요", "운영진이 확인할게요. 이 창은 닫아도 돼요.", extra=extra
        )
