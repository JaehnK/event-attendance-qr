/**
 * 출석 웹앱 — 출석 관리 스프레드시트에 바인딩(확장 프로그램 → Apps Script)해서 사용.
 * 스크립트 속성: TOKEN = Vercel 의 GAS_TOKEN 과 같은 값
 * 최초 1회: 편집기에서 setupTrigger 실행 → 5분마다 markAbsent 로 미출석자 자동 결석 처리
 *
 * 열은 1행 헤더 이름으로 찾는다 (순서 무관, 공백 무시).
 *  학습자명단  : user_id | user_login | 이름 | 기수
 *  행사일정    : 행사ID | 회차ID | 행사명 | 회차일자 | 출석인정시간 | 지각인정시간 | 차수
 *  행사참여현황: user_id | user_login | 이름 | 기수 | 행사ID | 행사명 | 회차ID | 회차일자 | 차수
 *               | 출석 | 지각 | 공가 | 결석(사전…) | 결석(무단…) | 중도포기 | 출석시각 | 사유 | 비고
 *  (비고 는 선택. 있으면 같은 브라우저로 여러 계정이 출석한 경우 "기기 중복: <계정>" 을 양쪽 행에 남김)
 *
 * 판정(스캔 시각 기준): 출석인정시간까지 출석, 지각인정시간까지 지각, 그 이후 결석(무단)+사유 입력.
 *       지각인정시간 + 30분이 지나면 기록이 없는 학습자 전원을 결석(무단)으로 추가.
 */
const TZ = 'Asia/Seoul';
const LEARNERS = '학습자명단';
const SESSIONS = '행사일정';
const RECORDS = '행사참여현황';
const LOGIN_RE = /^[a-z0-9-]{2,20}$/;
const ID_RE = /^[A-Za-z0-9_-]{1,40}$/;
const ABSENT_AFTER_MIN = 30;
const REASON_MAX = 200;
const SCAN_MAX_AGE_SEC = 15 * 60; // 스캔 시각 허용 범위 (Vercel 로그인 흐름 쿠키 10분 + 여유)

// 상태 체크박스 열 (공백 제거한 헤더로 판별)
const STATUS = {
  present: (h) => h === '출석',
  late: (h) => h === '지각',
  excused: (h) => h === '공가',
  notified: (h) => h.startsWith('결석') && h.includes('사전'),
  absent: (h) => h.startsWith('결석') && h.includes('무단'),
  dropout: (h) => h === '중도포기',
};

function doPost(e) {
  let req;
  try {
    req = JSON.parse(e.postData.contents);
  } catch (err) {
    return json_({ ok: false, msg: 'bad request' });
  }
  const token = PropertiesService.getScriptProperties().getProperty('TOKEN');
  if (!token || !req || typeof req.token !== 'string' || !safeEqual_(req.token, token)) {
    return json_({ ok: false, msg: 'unauthorized' });
  }
  try {
    if (req.action === 'today') return json_({ ok: true, events: todayEvents_() });
    if (req.action === 'checkin') return json_(locked_(() => checkin_(req)));
    if (req.action === 'reason') return json_(locked_(() => reason_(req)));
    return json_({ ok: false, msg: 'unknown action' });
  } catch (err) {
    console.error(err);
    return json_({ ok: false, msg: 'error' });
  }
}

// ---------- 액션 ----------

function todayEvents_() {
  return todaySessions_().map((s) => ({
    id: s.id,
    name: sessionName_(s),
    date: s.date,
    att: fmtHm_(s.att),
    late: fmtHm_(s.late),
  }));
}

function checkin_(req) {
  const login = String(req.login || '').toLowerCase();
  const id = String(req.id || '');
  if (!LOGIN_RE.test(login) || !ID_RE.test(id)) return { ok: false, msg: 'bad request' };

  const s = todaySessions_().find((x) => x.id === id);
  if (!s) return { ok: true, result: 'not_today' };
  const who = learners_().find((x) => x.login === login);
  if (!who) return { ok: true, result: 'not_member' };

  const shared = (Array.isArray(req.shared) ? req.shared : [])
    .map((x) => String(x).toLowerCase())
    .filter((x) => LOGIN_RE.test(x) && x !== login)
    .slice(0, 5);
  const scanned = scanTime_(req.at);
  const time = Utilities.formatDate(scanned, TZ, 'yyyy-MM-dd HH:mm:ss');
  const scanStatus = statusAt_(s, minutesOf_(scanned));
  const rec = records_();
  const base = { ok: true, name: sessionName_(s) };
  const i = findRecord_(rec, id, login);

  flagShared_(rec, id, login, shared);
  const note = shared.length ? '기기 중복: ' + shared.join(', ') : '';

  if (i < 0) {
    const status = scanStatus;
    append_(rec, [{ who: who, s: s, status: status, time: time, note: note }]);
    return Object.assign(base, {
      result: 'recorded',
      status: status,
      label: rec.label[status],
      time: time,
      reason_needed: status === 'absent',
    });
  }

  if (note) addNote_(rec, i, note);
  const row = rec.t.display[i];
  const status = rowStatus_(rec, rec.t.values[i]);
  const reasonEmpty = !row[rec.c.reason].trim();
  if (status === 'absent' && !row[rec.c.time].trim()) {
    // 자동 결석 행: 스캔 시각을 남기고, 스캔이 마감 전이었다면 그 상태로 정정
    setText_(rec.t.sh.getRange(i + 2, rec.c.time + 1), time);
    if (scanStatus !== 'absent') setStatus_(rec, i, scanStatus);
    return Object.assign(base, {
      result: 'recorded',
      status: scanStatus,
      label: rec.label[scanStatus],
      time: time,
      reason_needed: scanStatus === 'absent' && reasonEmpty,
    });
  }
  return Object.assign(base, {
    result: 'dup',
    status: status,
    label: rec.label[status] || '',
    time: row[rec.c.time].trim(),
    reason_needed: status === 'absent' && reasonEmpty,
  });
}

/** 결석(무단) 기록에만 사유 저장. 같은 회차 내 재제출은 덮어쓴다. */
function reason_(req) {
  const login = String(req.login || '').toLowerCase();
  const id = String(req.id || '');
  const text = String(req.reason || '').replace(/\s+/g, ' ').trim().slice(0, REASON_MAX);
  if (!LOGIN_RE.test(login) || !ID_RE.test(id) || !text) return { ok: false, msg: 'bad request' };

  const rec = records_();
  const i = findRecord_(rec, id, login);
  if (i < 0 || rowStatus_(rec, rec.t.values[i]) !== 'absent') return { ok: true, saved: false };
  setText_(rec.t.sh.getRange(i + 2, rec.c.reason + 1), text);
  return { ok: true, saved: true };
}

/** 시간 트리거: 지각인정시간 + 30분이 지난 오늘 회차에서 기록 없는 학습자 → 결석(무단). */
function markAbsent() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return;
  try {
    const m = minutesOf_(new Date());
    const due = todaySessions_().filter((s) => m >= s.late + ABSENT_AFTER_MIN);
    if (!due.length) return;
    const people = learners_();
    const rec = records_();
    const seen = new Set(rec.t.display.map((r) => key_(r[rec.c.id], r[rec.c.login])));
    const items = [];
    due.forEach((s) =>
      people.forEach((who) => {
        const k = key_(s.id, who.login);
        if (seen.has(k)) return;
        seen.add(k);
        items.push({ who: who, s: s, status: 'absent', time: '' });
      })
    );
    if (items.length) append_(rec, items);
  } finally {
    lock.releaseLock();
  }
}

/** 편집기에서 1회 실행: markAbsent 5분 주기 트리거 등록 (중복 제거). */
function setupTrigger() {
  ScriptApp.getProjectTriggers()
    .filter((t) => t.getHandlerFunction() === 'markAbsent')
    .forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('markAbsent').timeBased().everyMinutes(5).create();
}

// ---------- 시트 읽기 ----------

/** 1행 헤더 기준 테이블. 값은 원본(values)과 표시 문자열(display) 둘 다. */
function table_(name) {
  const sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(name);
  if (!sh) throw new Error('missing sheet: ' + name);
  const lastCol = sh.getLastColumn();
  if (lastCol < 1) throw new Error('empty sheet: ' + name);
  const range = sh.getRange(1, 1, Math.max(sh.getLastRow(), 1), lastCol);
  const values = range.getValues();
  const display = range.getDisplayValues();
  const raw = display[0];
  const headers = raw.map(norm_);
  return {
    sh: sh,
    raw: raw,
    headers: headers,
    values: values.slice(1),
    display: display.slice(1),
    col: (h) => headers.indexOf(norm_(h)),
  };
}

function need_(t, h) {
  const i = t.col(h);
  if (i < 0) throw new Error('missing column: ' + h);
  return i;
}

/** 오늘(Asia/Seoul) 회차. 시간 셀은 표시값으로 읽는다 (1899년 시간대 오차 회피). */
function todaySessions_() {
  const t = table_(SESSIONS);
  const c = {
    event: need_(t, '행사ID'),
    id: need_(t, '회차ID'),
    title: need_(t, '행사명'),
    date: need_(t, '회차일자'),
    att: need_(t, '출석인정시간'),
    late: need_(t, '지각인정시간'),
    round: t.col('차수'),
  };
  const today = Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd');
  const out = [];
  t.display.forEach((r) => {
    const id = r[c.id].trim();
    if (!ID_RE.test(id) || ymd_(r[c.date]) !== today) return;
    const att = hm_(r[c.att]);
    const late = hm_(r[c.late]);
    if (att === null || late === null) {
      console.warn('시간 형식 오류: ' + id);
      return;
    }
    out.push({
      eventId: r[c.event].trim(),
      id: id,
      title: r[c.title].trim(),
      date: today,
      round: c.round >= 0 ? r[c.round].trim() : '',
      att: att,
      late: Math.max(att, late),
    });
  });
  return out;
}

function learners_() {
  const t = table_(LEARNERS);
  const c = {
    uid: t.col('user_id'),
    login: need_(t, 'user_login'),
    name: t.col('이름'),
    gen: t.col('기수'),
  };
  const get = (r, i) => (i >= 0 ? r[i].trim() : '');
  return t.display
    .map((r) => ({
      uid: get(r, c.uid),
      login: get(r, c.login).toLowerCase(),
      name: get(r, c.name),
      gen: get(r, c.gen),
    }))
    .filter((x) => x.login);
}

function records_() {
  const t = table_(RECORDS);
  const c = {
    uid: t.col('user_id'),
    login: need_(t, 'user_login'),
    name: t.col('이름'),
    gen: t.col('기수'),
    event: t.col('행사ID'),
    title: t.col('행사명'),
    id: need_(t, '회차ID'),
    date: t.col('회차일자'),
    round: t.col('차수'),
    time: need_(t, '출석시각'),
    reason: need_(t, '사유'),
    note: t.col('비고'),
    status: {},
  };
  const label = {};
  Object.keys(STATUS).forEach((k) => {
    const i = t.headers.findIndex(STATUS[k]);
    if (i < 0 && (k === 'present' || k === 'late' || k === 'absent')) {
      throw new Error('missing column: ' + k);
    }
    c.status[k] = i;
    if (i >= 0) label[k] = t.raw[i].trim();
  });
  return { t: t, c: c, label: label };
}

function findRecord_(rec, id, login) {
  const k = key_(id, login);
  return rec.t.display.findIndex((r) => key_(r[rec.c.id], r[rec.c.login]) === k);
}

function rowStatus_(rec, row) {
  const k = Object.keys(STATUS).find((s) => rec.c.status[s] >= 0 && isTrue_(row[rec.c.status[s]]));
  return k || 'none';
}

// ---------- 시트 쓰기 ----------

/** 기록 행 추가. 체크박스로 미리 채워진 빈 행이 있어도 user_login 기준 마지막 행 다음에 쓴다. */
function append_(rec, items) {
  const t = rec.t;
  const c = rec.c;
  const width = t.headers.length;
  let start = 2;
  for (let i = t.display.length - 1; i >= 0; i--) {
    if (t.display[i][c.login].trim() || t.display[i][c.id].trim()) {
      start = i + 3;
      break;
    }
  }
  const rows = items.map((it) => {
    const row = new Array(width).fill('');
    const set = (i, v) => {
      if (i >= 0) row[i] = v;
    };
    set(c.uid, it.who.uid);
    set(c.login, it.who.login);
    set(c.name, it.who.name);
    set(c.gen, it.who.gen);
    set(c.event, it.s.eventId);
    set(c.title, it.s.title);
    set(c.id, it.s.id);
    set(c.date, it.s.date);
    set(c.round, it.s.round);
    Object.keys(STATUS).forEach((k) => set(c.status[k], k === it.status));
    set(c.time, it.time);
    set(c.note, it.note || '');
    return row;
  });

  const range = t.sh.getRange(start, 1, rows.length, width);
  range.setNumberFormat('@'); // 회차ID·날짜 자동 변환, 수식 해석 방지
  const checkbox = SpreadsheetApp.newDataValidation().requireCheckbox().build();
  Object.keys(STATUS).forEach((k) => {
    const i = c.status[k];
    if (i >= 0) {
      t.sh.getRange(start, i + 1, rows.length, 1).setNumberFormat('General').setDataValidation(checkbox);
    }
  });
  range.setValues(rows);
  SpreadsheetApp.flush();
}

/** 같은 기기로 먼저 출석한 계정들의 행에도 이번 계정을 남긴다. */
function flagShared_(rec, id, login, shared) {
  shared.forEach((other) => {
    const j = findRecord_(rec, id, other);
    if (j >= 0) addNote_(rec, j, '기기 중복: ' + login);
  });
}

/** 비고 열에 중복 없이 덧붙인다 (열이 없으면 무시). */
function addNote_(rec, i, text) {
  const col = rec.c.note;
  if (col < 0) return;
  const cur = rec.t.display[i][col].trim();
  if (cur.split(' / ').indexOf(text) >= 0) return;
  const next = cur ? cur + ' / ' + text : text;
  rec.t.display[i][col] = next;
  setText_(rec.t.sh.getRange(i + 2, col + 1), next);
}

function setStatus_(rec, i, status) {
  Object.keys(STATUS).forEach((k) => {
    const col = rec.c.status[k];
    if (col >= 0) rec.t.sh.getRange(i + 2, col + 1).setValue(k === status);
  });
  SpreadsheetApp.flush();
}

function setText_(range, value) {
  range.setNumberFormat('@').setValue(value);
  SpreadsheetApp.flush();
}

// ---------- 유틸 ----------

function locked_(fn) {
  const lock = LockService.getScriptLock();
  lock.waitLock(25000); // 동시 요청 직렬화. Vercel 대기(30초)보다 짧게 (초과 시 예외 → error 응답)
  try {
    return fn();
  } finally {
    lock.releaseLock();
  }
}

/** Vercel 이 서명해 넘긴 스캔 시각(초). 없거나 범위를 벗어나면 지금. */
function scanTime_(at) {
  const now = Date.now();
  const ms = Number(at) * 1000;
  if (!Number.isFinite(ms) || ms > now + 60000 || ms < now - SCAN_MAX_AGE_SEC * 1000) {
    return new Date(now);
  }
  return new Date(ms);
}

function statusAt_(s, m) {
  if (m <= s.att) return 'present';
  if (m <= s.late) return 'late';
  return 'absent';
}

function sessionName_(s) {
  const round = /^\d+$/.test(s.round) ? s.round + '차' : s.round;
  return round ? s.title + ' ' + round : s.title;
}

function key_(id, login) {
  return String(id).trim() + '|' + String(login).trim().toLowerCase();
}

function norm_(s) {
  return String(s).replace(/\s+/g, '');
}

function isTrue_(v) {
  return v === true || String(v).trim().toUpperCase() === 'TRUE';
}

function minutesOf_(d) {
  const p = Utilities.formatDate(d, TZ, 'H:m').split(':');
  return Number(p[0]) * 60 + Number(p[1]);
}

/** "2026-10-07", "2026. 10. 7" 등 → "2026-10-07". 실패 시 ''. */
function ymd_(s) {
  const m = String(s).match(/(\d{4})\D+(\d{1,2})\D+(\d{1,2})/);
  if (!m) return '';
  return m[1] + '-' + ('0' + m[2]).slice(-2) + '-' + ('0' + m[3]).slice(-2);
}

/** "9:00", "09:10:00", "오후 2:00", "2:00 PM" → 자정 기준 분. 실패 시 null. */
function hm_(s) {
  const str = String(s);
  const m = str.match(/(\d{1,2}):(\d{2})/);
  if (!m) return null;
  let h = Number(m[1]);
  const min = Number(m[2]);
  if (/오후|PM/i.test(str) && h < 12) h += 12;
  if (/오전|AM/i.test(str) && h === 12) h = 0;
  if (h > 23 || min > 59) return null;
  return h * 60 + min;
}

function fmtHm_(m) {
  return ('0' + Math.floor(m / 60)).slice(-2) + ':' + ('0' + (m % 60)).slice(-2);
}

function safeEqual_(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(
    ContentService.MimeType.JSON
  );
}
