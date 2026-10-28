"""관리자 전용: 오늘 회차의 출석 QR 표시. 42 로그인 + ADMIN_LOGINS 로 인증."""

import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(__file__))

import segno

import _common as c


def event_url(event_id: str, window: int) -> str:
    query = urllib.parse.urlencode({"e": event_id, "t": window, "s": c.qr_sig(event_id, window)})
    return f"{c.env('BASE_URL').rstrip('/')}/?{query}"


def qr_card(ev: dict, window: int) -> str:
    event_id = str(ev.get("id", ""))
    url = event_url(event_id, window)
    # omitsize: viewBox 만 남겨 CSS 크기에 맞춰 늘어나게 (width/height 고정이면 왼쪽 위로 쏠림)
    svg = segno.make(url, error="m").svg_inline(border=4, light="#fff", omitsize=True)
    att, late = (c.esc(str(ev.get(k, ""))) for k in ("att", "late"))
    return (
        f'<section class="qr"><h2>{c.esc(str(ev.get("name", "")))}</h2>{svg}'
        '<div class="chips">'
        f'<span class="badge present">출석 ~{att}</span>'
        f'<span class="badge late">지각 ~{late}</span></div>'
        f'<details class="muted"><summary>링크</summary>{c.esc(url)}</details></section>'
    )


class handler(c.Handler):  # Vercel 규약: 클래스명 handler
    def get(self) -> None:
        if not c.admin_logins():  # 미설정 시 안전한 기본값: 전원 거부
            self.message(403, "접근 권한이 없습니다", "관리자만 접근할 수 있습니다.")
            return
        login = c.admin_session(self.cookies().get("adm", ""))
        if login is None:
            # 세션 없음/위조/만료 → 관리자 모드로 42 로그인
            state = c.new_state()
            self.redirect(
                c.authorize_url(state),
                [
                    c.set_cookie("st", state, c.FLOW_TTL),
                    c.set_cookie("am", c.admin_mode_cookie(state), c.FLOW_TTL),
                    c.clear_cookie("ev"),
                    c.clear_cookie("adm"),
                ],
            )
            return

        try:
            events = c.gas({"action": "today"}).get("events")
            if not isinstance(events, list):
                raise c.UpstreamError
        except c.ConfigError:
            raise
        except Exception:
            # 일시 오류여도 화면이 멈추지 않게 계속 새로고침
            msg = "잠시 후 자동으로 다시 시도합니다."
            self.message(502, "회차 조회 실패", msg, refresh=c.QR_REFRESH)
            return

        window = c.qr_window()
        cards = [
            qr_card(ev, window)
            for ev in events
            if isinstance(ev, dict) and c.EVENT_ID_RE.fullmatch(str(ev.get("id", "")))
        ]
        today = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 9 * 3600))
        top = (
            '<div class="top"><h1>출석 QR</h1>'
            f'<span class="muted">{today} · 관리자 {c.esc(login)} · '
            f"{c.QR_REFRESH}초마다 새 QR로 바뀝니다</span></div>"
            f'<div class="timer" style="--refresh:{c.QR_REFRESH}s"><i></i></div>'
        )
        if cards:
            body = top + f'<div class="grid">{"".join(cards)}</div>'
        else:
            body = top + (
                '<section class="qr"><h2>오늘 진행하는 회차가 없습니다</h2>'
                '<p class="lead">행사일정 시트의 회차일자·출석인정시간·지각인정시간을 확인하세요.'
                "</p></section>"
            )
        self.send(200, c.page("출석 QR", body, wide=True, refresh=c.QR_REFRESH))
