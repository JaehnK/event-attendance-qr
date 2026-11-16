// apps-script.gs 를 가짜 SpreadsheetApp 위에서 실행하는 검증 하네스.
// 실행: node tests/apps_script_harness.js apps-script.gs
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const src = fs.readFileSync(process.argv[2] || 'apps-script.gs', 'utf8');

const fmtParts = (d) =>
  Object.fromEntries(
    new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
    }).formatToParts(d).map((p) => [p.type, p.value])
  );
const Utilities = {
  formatDate(d, tz, f) {
    const p = fmtParts(d);
    if (f === 'H:m') return `${Number(p.hour)}:${Number(p.minute)}`;
    return f.replace('yyyy', p.year).replace('MM', p.month).replace('dd', p.day)
      .replace('HH', p.hour).replace('mm', p.minute).replace('ss', p.second);
  },
  sleep() {},
};

function makeSheet(rows) {
  const cells = rows.map((r) => r.slice());
  const fmt = {};
  const sheet = {
    cells, fmt,
    getLastRow: () => {
      for (let i = cells.length - 1; i >= 0; i--) if (cells[i].some((v) => v !== '' && v != null)) return i + 1;
      return 0;
    },
    getLastColumn: () => Math.max(0, ...cells.map((r) => r.length)),
    getRange(r, c, nr = 1, nc = 1) {
      const get = (fn) => Array.from({ length: nr }, (_, i) =>
        Array.from({ length: nc }, (_, j) => fn((cells[r - 1 + i] || [])[c - 1 + j])));
      const rng = {
        getValues: () => get((v) => (v === undefined ? '' : v)),
        getDisplayValues: () => get((v) => (v === undefined || v === '' ? '' : v === true ? 'TRUE' : v === false ? 'FALSE' : String(v))),
        setNumberFormat(f) { for (let i = 0; i < nr; i++) for (let j = 0; j < nc; j++) fmt[`${r + i},${c + j}`] = f; return rng; },
        setDataValidation() { return rng; },
        setValues(vals) {
          vals.forEach((row, i) => row.forEach((v, j) => {
            while (cells.length < r + i) cells.push([]);
            if (typeof v === 'boolean') assert.notStrictEqual(fmt[`${r + i},${c + j}`], '@', 'checkbox cell must not be text');
            cells[r - 1 + i][c - 1 + j] = v;
          }));
          return rng;
        },
        setValue(v) { return rng.setValues([[v]]); },
      };
      return rng;
    },
  };
  return sheet;
}

const kst = (ms = Date.now()) => { const p = fmtParts(new Date(ms)); return { date: `${p.year}-${p.month}-${p.day}`, min: Number(p.hour) * 60 + Number(p.minute) }; };
const hm = (m) => { m = ((m % 1440) + 1440) % 1440; return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}`; };
const HEAD = ['user_id', 'user_login', '이름', '기수', '행사ID', '행사명', '회차ID', '회차일자', '차수', '출석', '지각', '공가', '결석(사전 고지,취소)', '결석(무단, 노쇼)', '중도포기', '출석시각', '사유'];

function setup({ att, late, note = false, extraSessions = [] }) {
  const { date } = kst();
  const head = note ? [...HEAD, '비고'] : HEAD;
  const blank = head.map((h) => (['출석', '지각', '공가', '결석(사전 고지,취소)', '결석(무단, 노쇼)', '중도포기'].includes(h) ? false : ''));
  const sheets = {
    학습자명단: makeSheet([['user_id', 'user_login', '이름', '기수'], [172416, 'jaehukim', '김재훈', '1'], [172351, 'SoosLee', '이수성', '1'], [172392, 'jinsecho', '최진성', '']]),
    행사일정: makeSheet([
      ['행사ID', '회차ID', '행사명', '주관', '구분', '회차일자', '출석인정시간', '지각인정시간', '차수'],
      ['2026-001', '2026-001-01', '테스트 행사', 'AI', '일반', date, att, late, '1'],
      ['2026-001', '2026-001-00', '어제 행사', 'AI', '일반', '2020-01-01', '9:00', '9:10', ''],
      ...extraSessions,
    ]),
    // 체크박스(FALSE)만 깔린 빈 행 → getLastRow 함정
    행사참여현황: makeSheet([head, blank, blank]),
  };
  const toasts = [];
  const ctx = {
    console: { ...console, log() {}, warn() {}, error() {} }, Utilities,
    SpreadsheetApp: {
      getActiveSpreadsheet: () => ({ getSheetByName: (n) => sheets[n] || null, toast: (m) => toasts.push(m) }),
      flush() {},
      newDataValidation: () => ({ requireCheckbox() { return this; }, build: () => ({}) }),
      getUi: () => ({ createMenu: () => ({ addItem() { return this; }, addToUi() {} }) }),
    },
    LockService: { getScriptLock: () => ({ waitLock() {}, tryLock: () => true, releaseLock() {} }) },
    PropertiesService: { getScriptProperties: () => ({ getProperty: (k) => (k === 'TOKEN' ? 'tok' : null) }) },
    ContentService: { MimeType: { JSON: 'json' }, createTextOutput: (s) => ({ s, setMimeType() { return this; } }) },
    ScriptApp: {},
  };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  const call = (body) => JSON.parse(ctx.doPost({ postData: { contents: JSON.stringify({ token: 'tok', ...body }) } }).s);
  const rows = () => sheets['행사참여현황'].cells.slice(1).filter((r) => r[1]);
  const row = (login, id = '2026-001-01') => rows().find((r) => r[1] === login && r[6] === id);
  const col = (name) => sheets['행사참여현황'].cells[0].indexOf(name);
  const status = (r) => ['출석', '지각', '공가', '결석(사전 고지,취소)', '결석(무단, 노쇼)', '중도포기'].filter((h) => r[col(h)] === true);
  return { ctx, sheets, call, rows, row, col, status, toasts };
}

const { min } = kst();
const now = () => Math.floor(Date.now() / 1000);
const ID = '2026-001-01';
let n = 0;
const ok = (name) => console.log(`ok ${++n} - ${name}`);
const check = (name, fn) => { fn(); ok(name); };

check('wrong token', () => {
  const { ctx } = setup({ att: hm(min + 60), late: hm(min + 70) });
  assert.strictEqual(JSON.parse(ctx.doPost({ postData: { contents: '{"token":"x","action":"today"}' } }).s).msg, 'unauthorized');
});

check('today lists only today sessions', () => {
  const { call } = setup({ att: hm(min + 60), late: hm(min + 70) });
  const t = call({ action: 'today' });
  assert.deepStrictEqual(t.events.map((e) => [e.id, e.name]), [[ID, '테스트 행사 1차']]);
});

check('check-in without prefill appends row (after checkbox-only rows)', () => {
  const { call, sheets, row, col, status } = setup({ att: hm(min + 60), late: hm(min + 70) });
  const r = call({ action: 'checkin', login: 'jaehukim', id: ID });
  assert.strictEqual(r.result, 'recorded'); assert.strictEqual(r.status, 'present');
  assert.strictEqual(sheets['행사참여현황'].cells[1][1], 'jaehukim');
  const x = row('jaehukim');
  assert.deepStrictEqual(status(x), ['출석']); assert.strictEqual(x[col('user_id')], '172416'); assert.ok(x[col('출석시각')]);
  assert.strictEqual(call({ action: 'checkin', login: 'JAEHUKIM', id: ID }).result, 'dup');
  assert.strictEqual(call({ action: 'checkin', login: 'stranger', id: ID }).result, 'not_member');
  assert.strictEqual(call({ action: 'checkin', login: 'jaehukim', id: '2026-001-00' }).result, 'not_today');
});

check('late / absent+reason by scan time', () => {
  let e = setup({ att: hm(min - 5), late: hm(min + 5) });
  assert.strictEqual(e.call({ action: 'checkin', login: 'jaehukim', id: ID }).status, 'late');
  e = setup({ att: hm(min - 20), late: hm(min - 10) });
  const r = e.call({ action: 'checkin', login: 'jaehukim', id: ID });
  assert.strictEqual(r.status, 'absent'); assert.strictEqual(r.reason_needed, true);
  assert.strictEqual(e.call({ action: 'reason', login: 'jaehukim', id: ID, reason: ' =1+1\n 늦잠 ' }).saved, true);
  assert.strictEqual(e.row('jaehukim')[e.col('사유')], '=1+1 늦잠');
  e = setup({ att: hm(min - 2), late: hm(min + 10) });
  const at = now() - 300;
  const p = e.call({ action: 'checkin', login: 'jaehukim', id: ID, at });
  assert.strictEqual(p.status, 'present');
  assert.strictEqual(p.time, Utilities.formatDate(new Date(at * 1000), '', 'yyyy-MM-dd HH:mm:ss'));
  assert.strictEqual(setup({ att: hm(min - 2), late: hm(min + 10) }).call({ action: 'checkin', login: 'jaehukim', id: ID, at: now() - 1200 }).status, 'late');
});

// ---------- 미리 채우기 ----------

check('trigger prefills today rows (status empty) before deadline, idempotent', () => {
  const { ctx, rows, status, col, row } = setup({ att: hm(min + 60), late: hm(min + 70) });
  ctx.markAbsent(); ctx.markAbsent();
  assert.deepStrictEqual(rows().map((r) => r[1]), ['jaehukim', 'sooslee', 'jinsecho']);
  rows().forEach((r) => { assert.deepStrictEqual(status(r), []); assert.strictEqual(r[col('출석시각')], ''); });
  assert.strictEqual(row('jinsecho')[col('기수')], '');
  assert.strictEqual(row('jaehukim')[col('회차일자')], kst().date);
});

check('scan fills prefilled row (no new row)', () => {
  const { ctx, call, rows, row, status, col } = setup({ att: hm(min + 60), late: hm(min + 70) });
  ctx.markAbsent();
  const r = call({ action: 'checkin', login: 'sooslee', id: ID });
  assert.strictEqual(r.result, 'recorded'); assert.strictEqual(r.status, 'present'); assert.strictEqual(r.label, '출석');
  assert.strictEqual(rows().length, 3);
  assert.deepStrictEqual(status(row('sooslee')), ['출석']); assert.ok(row('sooslee')[col('출석시각')]);
  assert.strictEqual(call({ action: 'checkin', login: 'sooslee', id: ID }).result, 'dup');
});

check('admin pre-marked 공가 is kept, scan only records time', () => {
  const { ctx, call, row, status, col } = setup({ att: hm(min + 60), late: hm(min + 70) });
  ctx.markAbsent();
  row('jinsecho')[col('공가')] = true;
  const r = call({ action: 'checkin', login: 'jinsecho', id: ID });
  assert.strictEqual(r.result, 'dup'); assert.strictEqual(r.label, '공가'); assert.strictEqual(r.reason_needed, false); assert.ok(r.time);
  assert.deepStrictEqual(status(row('jinsecho')), ['공가']); assert.ok(row('jinsecho')[col('출석시각')]);
});

check('after deadline: empty rows → absent, checked rows untouched', () => {
  const { ctx, call, row, status } = setup({ att: hm(min - 50), late: hm(min - 40) });
  ctx.markAbsent(); // prefill + 마감 지남
  assert.deepStrictEqual(status(row('jaehukim')), ['결석(무단, 노쇼)']);
  const e = setup({ att: hm(min + 60), late: hm(min + 70) });
  e.ctx.markAbsent();
  e.call({ action: 'checkin', login: 'jaehukim', id: ID });
  e.row('sooslee')[e.col('공가')] = true;
  // 마감 시각을 지난 것으로 바꿔 다시 실행
  e.sheets['행사일정'].cells[1][6] = hm(min - 50); e.sheets['행사일정'].cells[1][7] = hm(min - 40);
  e.ctx.markAbsent();
  assert.deepStrictEqual(e.status(e.row('jaehukim')), ['출석']);
  assert.deepStrictEqual(e.status(e.row('sooslee')), ['공가']);
  assert.deepStrictEqual(e.status(e.row('jinsecho')), ['결석(무단, 노쇼)']);
});

check('late arrival on auto-absent row: time recorded, reason asked', () => {
  const { ctx, call, row, col } = setup({ att: hm(min - 50), late: hm(min - 31) });
  ctx.markAbsent();
  const r = call({ action: 'checkin', login: 'jinsecho', id: ID });
  assert.strictEqual(r.result, 'recorded'); assert.strictEqual(r.status, 'absent'); assert.strictEqual(r.reason_needed, true);
  assert.ok(row('jinsecho')[col('출석시각')]);
});

check('auto-absent row corrected to scan-time status', () => {
  const { ctx, call, row, status } = setup({ att: hm(min - 40), late: hm(min - 5) });
  ctx.markAbsent(); // prefill (마감 전)
  ctx.setStatus_(ctx.records_(), ctx.records_().t.display.findIndex((r) => r[1] === 'jaehukim'), 'absent');
  const r = call({ action: 'checkin', login: 'jaehukim', id: ID, at: now() - 600 });
  assert.strictEqual(r.status, 'late'); assert.deepStrictEqual(status(row('jaehukim')), ['지각']);
});

check('learner added after prefill → scan appends', () => {
  const { ctx, call, sheets, rows } = setup({ att: hm(min + 60), late: hm(min + 70) });
  ctx.markAbsent();
  sheets['학습자명단'].cells.push([1, 'newbie', '신입', '2']);
  assert.strictEqual(call({ action: 'checkin', login: 'newbie', id: ID }).result, 'recorded');
  assert.strictEqual(rows().length, 4);
});

check('menu prefill covers next 7 days only, toast count', () => {
  const in3 = kst(Date.now() + 3 * 86400000).date;
  const in9 = kst(Date.now() + 9 * 86400000).date;
  const e = setup({ att: hm(min + 60), late: hm(min + 70), extraSessions: [
    ['2026-002', '2026-002-01', '미래 행사', '', '', in3, '10:00', '10:10', '2'],
    ['2026-002', '2026-002-02', '먼 미래', '', '', in9, '10:00', '10:10', '3'],
  ] });
  e.ctx.prefillUpcoming(); e.ctx.prefillUpcoming();
  assert.strictEqual(e.rows().filter((r) => r[6] === ID).length, 3);
  assert.strictEqual(e.rows().filter((r) => r[6] === '2026-002-01').length, 3);
  assert.strictEqual(e.rows().filter((r) => r[6] === '2026-002-02').length, 0);
  assert.strictEqual(e.row('jaehukim', '2026-002-01')[e.col('회차일자')], in3);
  assert.deepStrictEqual(e.toasts, ['6개 행을 만들었습니다.', '0개 행을 만들었습니다.']);
  // 미래 회차 QR 은 오늘 출석 불가
  assert.strictEqual(e.call({ action: 'checkin', login: 'jaehukim', id: '2026-002-01' }).result, 'not_today');
});

check('shared device noted on both rows (비고), works without column', () => {
  const e = setup({ att: hm(min + 60), late: hm(min + 70), note: true });
  e.ctx.markAbsent();
  e.call({ action: 'checkin', login: 'jaehukim', id: ID });
  e.call({ action: 'checkin', login: 'sooslee', id: ID, shared: ['jaehukim', '../evil'] });
  assert.strictEqual(e.row('sooslee')[e.col('비고')], '기기 중복: jaehukim');
  assert.strictEqual(e.row('jaehukim')[e.col('비고')], '기기 중복: sooslee');
  assert.strictEqual(setup({ att: hm(min + 60), late: hm(min + 70) }).call({ action: 'checkin', login: 'sooslee', id: ID, shared: ['jaehukim'] }).result, 'recorded');
});

// ---------- 일시 오류 재시도 / 일괄 쓰기 ----------

/** 시트 읽기를 n번 실패시키는 장치 ("server error" 흉내). */
function failReads(sheet, times) {
  const orig = sheet.getLastColumn;
  let left = times;
  sheet.getLastColumn = () => {
    if (left-- > 0) throw new Error("We're sorry, a server error occurred. Please wait a bit and try again.");
    return orig();
  };
  return () => times - Math.max(left, 0);
}

check('markAbsent retries transient errors, then succeeds', () => {
  const { ctx, sheets, rows } = setup({ att: hm(min + 60), late: hm(min + 70) });
  const fails = failReads(sheets['행사일정'], 2);
  ctx.markAbsent();
  assert.strictEqual(fails(), 2);
  assert.deepStrictEqual(rows().map((r) => r[1]), ['jaehukim', 'sooslee', 'jinsecho']);
});

check('markAbsent rethrows after RETRY_MAX so failure mail still fires', () => {
  const { ctx, sheets, rows } = setup({ att: hm(min + 60), late: hm(min + 70) });
  failReads(sheets['행사일정'], 99);
  assert.throws(() => ctx.markAbsent(), /server error/);
  assert.strictEqual(rows().length, 0);
});

check('absent marking writes contiguous rows in one batch, skips checked rows', () => {
  const e = setup({ att: hm(min + 60), late: hm(min + 70) });
  e.ctx.markAbsent(); // prefill (마감 전)
  e.row('sooslee')[e.col('공가')] = true; // 가운데 행을 미리 체크 → 구간이 둘로 갈라짐
  e.sheets['행사일정'].cells[1][6] = hm(min - 50); e.sheets['행사일정'].cells[1][7] = hm(min - 40);
  const sh = e.sheets['행사참여현황'];
  const orig = sh.getRange;
  let writes = 0;
  sh.getRange = (...a) => {
    const r = orig(...a);
    const sv = r.setValues;
    r.setValues = (v) => { writes++; return sv(v); };
    return r;
  };
  e.ctx.markAbsent();
  assert.deepStrictEqual(e.status(e.row('jaehukim')), ['결석(무단, 노쇼)']);
  assert.deepStrictEqual(e.status(e.row('sooslee')), ['공가']);
  assert.deepStrictEqual(e.status(e.row('jinsecho')), ['결석(무단, 노쇼)']);
  assert.strictEqual(writes, 12); // 구간 2개 × 상태 열 6개 (행마다 쓰던 때는 행 수 × 6)
});

check('parsers + missing column → generic error', () => {
  const e = setup({ att: '9:00', late: '9:10' });
  assert.strictEqual(e.ctx.hm_('오후 2:00'), 840); assert.strictEqual(e.ctx.hm_('12:30 AM'), 30); assert.strictEqual(e.ctx.ymd_('2026. 10. 7'), '2026-10-07');
  e.sheets['행사참여현황'].cells[0][e.col('사유')] = '메모';
  assert.deepStrictEqual(e.call({ action: 'checkin', login: 'jaehukim', id: ID }), { ok: false, msg: 'error' });
});

console.log(`all ${n} passed`);
