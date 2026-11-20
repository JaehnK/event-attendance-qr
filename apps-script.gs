/**
 * 출석 웹앱 — 출석 관리 스프레드시트에 바인딩(확장 프로그램 → Apps Script)해서 사용.
 * 스크립트 속성: TOKEN = Vercel 의 GAS_TOKEN 과 같은 값
 * 최초 1회: 편집기에서 setupTrigger 실행 → 5분마다 markAbsent 가
 *   ① 오늘 회차의 학습자별 빈 행을 미리 만들고 ② 마감 지난 회차의 빈 행을 결석(무단) 처리.
 * 시트 메뉴 "출석 → 앞으로 7일 회차 명단 생성": 공가·사전 결석을 미리 체크할 수 있게 행을 먼저 만든다.
 *
 * 열은 1행 헤더 이름으로 찾는다 (순서 무관, 공백 무시).
 *  학습자명단  : user_id | user_login | 이름 | 기수
 *  행사일정    : 행사ID | 회차ID | 행사명 | 회차일자 | 출석인정시간 | 지각인정시간 | 차수 | 클러스터접속제한
 *               (클러스터접속제한 은 선택. Y/TRUE/체크면 그 회차는 클러스터에 접속 중인 학습자의 출석을 기록하지 않는다)
 *  행사참여현황: user_id | user_login | 이름 | 기수 | 행사ID | 행사명 | 회차ID | 회차일자 | 차수
 *               | 출석 | 지각 | 공가 | 결석(사전…) | 결석(무단…) | 중도포기 | 출석시각 | 사유 | 비고
 *  (비고 는 선택. 있으면 같은 브라우저로 여러 계정이 출석한 경우 "기기 중복: <계정>" 을 양쪽 행에 남김)
 *
 * 판정(스캔 시각 기준): 출석인정시간까지 출석, 지각인정시간까지 지각, 그 이후 결석(무단)+사유 입력.
 *       스캔은 미리 만든 내 행에 상태·출석시각을 채운다. 관리자가 미리 체크한 상태는 덮어쓰지 않는다.
 *       지각인정시간 + 30분이 지나면 상태가 빈 행을 결석(무단)으로 체크.
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
const PREFILL_DAYS = 7; // 메뉴로 미리 만드는 기간
const RETRY_MAX = 3; // 시간 트리거 일시 오류 재시도 (총 시도 횟수)
const RETRY_WAIT_MS = 2000; // 재시도 간격 (시도 횟수만큼 곱해 늘어남)

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

  // 클러스터 접속 제한 회차: 이 스캔이 출석을 채우게 될 때만 막는다 (이미 처리된 행은 아래에서 그대로 dup)
  if (req.cluster === true && s.restrict && (i < 0 || fillable_(rec, i))) {
    return { ok: true, result: 'blocked_cluster', name: sessionName_(s) };
  }

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
  const timeEmpty = !row[rec.c.time].trim();
  // 미리 만든 빈 행, 또는 자동 결석 행(출석시각 없음): 스캔 시각 기준 상태로 채운다
  if (fillable_(rec, i)) {
    setText_(rec.t.sh.getRange(i + 2, rec.c.time + 1), time);
    if (scanStatus !== status) setStatus_(rec, i, scanStatus);
    return Object.assign(base, {
      result: 'recorded',
      status: scanStatus,
      label: rec.label[scanStatus],
      time: time,
      reason_needed: scanStatus === 'absent' && reasonEmpty,
    });
  }
  // 이미 처리된 행(관리자가 미리 공가 등 체크 포함): 상태는 그대로, 출석시각만 비어 있으면 남긴다
  if (timeEmpty) setText_(rec.t.sh.getRange(i + 2, rec.c.time + 1), time);
  return Object.assign(base, {
    result: 'dup',
    status: status,
    label: rec.label[status] || '',
    time: timeEmpty ? time : row[rec.c.time].trim(),
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

/**
 * 시간 트리거(5분): 오늘 회차 빈 행 생성 → 지각인정시간 + 30분 지난 회차의 빈 행을 결석(무단).
 * 구글 쪽 일시 오류("server error")는 재시도한다. 몇 번 해도 안 되면 그대로 던져 실패 알림이 가게 한다.
 */
function markAbsent() {
  withRetry_('markAbsent', markAbsentOnce_);
}

/** 몇 번을 다시 실행해도 결과가 같다 (이미 있는 행은 건너뛰고, 상태가 빈 행만 결석 처리). */
function markAbsentOnce_() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    console.warn('markAbsent: 락 대기 30초 초과, 이번 회차는 건너뜀');
    return;
  }
  try {
    const todays = todaySessions_();
    if (!todays.length) return;
    const added = prefill_(todays);
    const m = minutesOf_(new Date());
    const due = new Set(todays.filter((s) => m >= s.late + ABSENT_AFTER_MIN).map((s) => s.id));
    let absent = 0;
    if (due.size) {
      const rec = records_();
      const idxs = [];
      rec.t.display.forEach((r, i) => {
        if (due.has(r[rec.c.id].trim()) && rowStatus_(rec, rec.t.values[i]) === 'none') idxs.push(i);
      });
      setStatusRows_(rec, idxs, 'absent');
      absent = idxs.length;
    }
    if (added || absent) console.log('markAbsent: 행 ' + added + '개 생성, 결석 ' + absent + '명 처리');
  } finally {
    lock.releaseLock();
  }
}

/** 일시 오류용 재시도. 락은 fn 안에서 잡고 풀므로 대기 중에는 스캔 요청이 락을 쓸 수 있다. */
function withRetry_(label, fn) {
  const started = Date.now();
  for (let attempt = 1; ; attempt++) {
    try {
      return fn();
    } catch (err) {
      console.error(
        label + ' 실패 ' + attempt + '/' + RETRY_MAX + ' (' + (Date.now() - started) + 'ms): ' +
          ((err && err.stack) || err)
      );
      if (attempt >= RETRY_MAX) throw err;
      Utilities.sleep(RETRY_WAIT_MS * attempt);
    }
  }
}

/** 시트 메뉴: 오늘부터 PREFILL_DAYS 일 안의 회차 행을 미리 만든다. */
function prefillUpcoming() {
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  let n;
  try {
    const from = today_();
    const to = Utilities.formatDate(new Date(Date.now() + PREFILL_DAYS * 86400000), TZ, 'yyyy-MM-dd');
    n = prefill_(sessionsBetween_(from, to));
  } finally {
    lock.releaseLock();
  }
  SpreadsheetApp.getActiveSpreadsheet().toast(n + '개 행을 만들었습니다.', '출석 명단 생성');
}

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('출석')
    .addItem('앞으로 ' + PREFILL_DAYS + '일 회차 명단 생성', 'prefillUpcoming')
    .addToUi();
}

/** 회차 × 학습자 중 행이 없는 조합만 상태가 빈 행으로 추가 (회차별, 명단 순서). 추가한 행 수. */
function prefill_(sessions) {
  if (!sessions.length) return 0;
  const people = learners_();
  const rec = records_();
  const seen = new Set(rec.t.display.map((r) => key_(r[rec.c.id], r[rec.c.login])));
  const items = [];
  sessions.forEach((s) =>
    people.forEach((who) => {
      const k = key_(s.id, who.login);
      if (seen.has(k)) return;
      seen.add(k);
      items.push({ who: who, s: s, status: 'none', time: '' });
    })
  );
  if (items.length) append_(rec, items);
  return items.length;
}

/** 편집기에서 1회 실행: markAbsent 5분 주기 트리거 등록 (중복 제거). 바로 한 번 실행해 오늘 행도 만든다. */
function setupTrigger() {
  ScriptApp.getProjectTriggers()
    .filter((t) => t.getHandlerFunction() === 'markAbsent')
    .forEach((t) => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('markAbsent').timeBased().everyMinutes(5).create();
  markAbsent();
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

function today_() {
  return Utilities.formatDate(new Date(), TZ, 'yyyy-MM-dd');
}

/** 오늘(Asia/Seoul) 회차. */
function todaySessions_() {
  const d = today_();
  return sessionsBetween_(d, d);
}

/** 회차일자가 from~to(yyyy-MM-dd, 포함)인 회차. 시간 셀은 표시값으로 읽는다 (1899년 시간대 오차 회피). */
function sessionsBetween_(from, to) {
  const t = table_(SESSIONS);
  const c = {
    event: need_(t, '행사ID'),
    id: need_(t, '회차ID'),
    title: need_(t, '행사명'),
    date: need_(t, '회차일자'),
    att: need_(t, '출석인정시간'),
    late: need_(t, '지각인정시간'),
    round: t.col('차수'),
    restrict: t.col('클러스터접속제한'),
  };
  const out = [];
  t.display.forEach((r) => {
    const id = r[c.id].trim();
    const date = ymd_(r[c.date]);
    if (!ID_RE.test(id) || !date || date < from || date > to) return;
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
      date: date,
      round: c.round >= 0 ? r[c.round].trim() : '',
      att: att,
      late: Math.max(att, late),
      restrict: c.restrict >= 0 && flag_(r[c.restrict]),
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

/** 스캔이 이 행의 상태를 채우는가: 미리 만든 빈 행, 또는 출석시각 없는 자동 결석 행. */
function fillable_(rec, i) {
  const status = rowStatus_(rec, rec.t.values[i]);
  return status === 'none' || (status === 'absent' && !rec.t.display[i][rec.c.time].trim());
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
  setStatusRows_(rec, [i], status);
}

/** 여러 행의 상태를 한 번에. 연속한 행은 한 덩어리로 묶어 상태 열마다 setValues 한 번 (행마다 쓰면 느리다). */
function setStatusRows_(rec, idxs, status) {
  if (!idxs.length) return;
  const runs = [];
  idxs.forEach((i) => {
    const last = runs[runs.length - 1];
    if (last && last[1] === i - 1) last[1] = i;
    else runs.push([i, i]);
  });
  runs.forEach((run) => {
    const n = run[1] - run[0] + 1;
    Object.keys(STATUS).forEach((k) => {
      const col = rec.c.status[k];
      if (col < 0) return;
      const vals = Array.from({ length: n }, () => [k === status]);
      rec.t.sh.getRange(run[0] + 2, col + 1, n, 1).setValues(vals);
    });
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

/** 시트의 켜짐 표시: 체크박스(TRUE) 또는 Y/YES/O/1/예. 비어 있거나 그 외는 꺼짐. */
function flag_(v) {
  return /^(true|y|yes|o|1|예)$/i.test(String(v).trim());
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
