# -*- coding: utf-8 -*-
"""SQLite 저장소. 모든 데이터는 같은 폴더의 data/avionics.db 에 저장된다."""
import json
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
# Vercel 등 웹 배포본: 파일시스템이 읽기 전용(/tmp 만 쓰기 가능)이고 편집 기능 없이 열람만 한다.
# 내용은 작성자 PC 에서 publish.py 로 만든 published/snapshot.json 을 시작할 때 읽어 온다.
PUBLIC = bool(os.environ.get("VERCEL") or os.environ.get("AVIONICS_PUBLIC"))
DB_PATH = os.environ.get("AVIONICS_DB") or (
    "/tmp/avionics.db" if PUBLIC else os.path.join(BASE, "data", "avionics.db"))
SNAPSHOT_PATH = os.path.join(BASE, "published", "snapshot.json")

FIELDS = ["레이더", "전자전(EW)", "센서융합·IRST·EO", "데이터링크·통신", "조종석·HMD",
          "임무컴퓨터·소프트웨어(OFP)", "항법·생존성", "무장 연동"]
TYPES = ["신기술", "기능 추가", "성능개량"]
MATURITY = ["개념·개발 중", "시험 중", "양산·배치 중"]
GRADES = ["상", "중", "하"]
STATUSES = ["미검토", "검토 완료", "보고서 반영"]

# 기본 우리 공군 기종 목록(작성자가 기준 정보 화면에서 추가·삭제·수정)
DEFAULT_AIRCRAFT = ["F-35A", "F-15K", "KF-16", "F-16C/D", "KF-21", "FA-50", "F-5E/F"]

DEFAULT_SOURCES = """제조사 보도자료: Lockheed Martin, Boeing, Northrop Grumman, RTX(Raytheon), BAE Systems, Saab, Leonardo, Thales, Dassault 등
무료로 공개된 국방·항공 매체 기사 (Aviation Week, Janes 등은 무료 공개 기사만)
국내 국방 기관 공개 자료 (국방기술품질원, 국방과학연구소 등의 공개 간행물·보도자료)
공개 세미나·학술행사 발표 자료"""

DRAFT_SECTIONS = ["소요 배경 및 필요성", "관련 기술 동향 (국외 사례)", "운용 개념", "요구 능력·성능 (개략)",
                  "적용 대상 기종 및 도입 고려사항", "획득 방안 (구매 / 개조 / 국내개발)", "출처"]


# ── 주차 계산 ─────────────────────────────────────────
def week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_range(key: str):
    y, w = key.split("-W")
    mon = date.fromisocalendar(int(y), int(w), 1)
    return mon, mon + timedelta(days=6)


def last_week_key(today: date = None) -> str:
    today = today or date.today()
    return week_key(today - timedelta(days=7))


def this_week_key(today: date = None) -> str:
    return week_key(today or date.today())


# ── 연결 ─────────────────────────────────────────────
def conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS aircraft (id INTEGER PRIMARY KEY, name TEXT UNIQUE, sort INTEGER);
        CREATE TABLE IF NOT EXISTS baseline (
            id INTEGER PRIMARY KEY, aircraft TEXT, field TEXT, level_text TEXT,
            input_mode TEXT, source TEXT, UNIQUE(aircraft, field));
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY, week TEXT, title_orig TEXT, title_ko TEXT,
            source_name TEXT, url TEXT, published TEXT, fields TEXT, foreign_aircraft TEXT,
            type TEXT, summary TEXT, maturity TEXT, collect_mode TEXT, status TEXT,
            memo TEXT, data_class TEXT, link_ok INTEGER, evals TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY, week TEXT, item_ids TEXT, body TEXT, status TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY, week TEXT, item_id INTEGER, template_version TEXT,
            sections TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, week TEXT, started TEXT, finished TEXT, mode TEXT,
            result TEXT, count INTEGER);
        CREATE TABLE IF NOT EXISTS collect_requests (
            id INTEGER PRIMARY KEY, week TEXT, requested TEXT, done TEXT);
        """)
        if not c.execute("SELECT 1 FROM aircraft").fetchone():
            for i, n in enumerate(DEFAULT_AIRCRAFT):
                c.execute("INSERT INTO aircraft(name, sort) VALUES(?,?)", (n, i))
    if PUBLIC:
        load_snapshot()
    if not get_setting("share_token"):
        set_setting("share_token", secrets.token_urlsafe(12))
    if not get_setting("sources"):
        set_setting("sources", DEFAULT_SOURCES)


def get_setting(k, default=None):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return r["value"] if r else default


def set_setting(k, v):
    with conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))


# ── 기종·기준 정보 ───────────────────────────────────
def aircraft_list():
    with conn() as c:
        return [r["name"] for r in c.execute("SELECT name FROM aircraft ORDER BY sort, id")]


def set_aircraft_list(names):
    names = [n.strip() for n in names if n.strip()]
    with conn() as c:
        c.execute("DELETE FROM aircraft")
        for i, n in enumerate(dict.fromkeys(names)):
            c.execute("INSERT INTO aircraft(name, sort) VALUES(?,?)", (n, i))


def baseline_map():
    with conn() as c:
        rows = c.execute("SELECT * FROM baseline").fetchall()
    return {(r["aircraft"], r["field"]): dict(r) for r in rows}


def save_baseline(aircraft, field, level_text, input_mode, source):
    with conn() as c:
        c.execute("""INSERT INTO baseline(aircraft, field, level_text, input_mode, source) VALUES(?,?,?,?,?)
                     ON CONFLICT(aircraft, field) DO UPDATE SET level_text=excluded.level_text,
                     input_mode=excluded.input_mode, source=excluded.source""",
                  (aircraft, field, level_text, input_mode, source))


def baseline_text():
    """AI 프롬프트용 기준 정보 요약(입력된 것만)."""
    bm = baseline_map()
    lines = []
    for ac in aircraft_list():
        for f in FIELDS:
            b = bm.get((ac, f))
            if b and (b["level_text"] or "").strip():
                tag = "추정" if b["input_mode"] == "추정" else "작성자 입력"
                lines.append(f"- {ac} / {f}: {b['level_text']} ({tag})")
    return "\n".join(lines) or "(입력된 기준 정보 없음 — 기종 일반 정보 수준에서만 판단하고 '기준 정보 미입력'이라고 명시)"


# ── 소식 ─────────────────────────────────────────────
def _item(r):
    if not r:
        return None
    d = dict(r)
    d["fields"] = json.loads(d["fields"] or "[]")
    d["evals"] = json.loads(d["evals"] or "[]")
    return d


def items_for_week(week, field=None):
    with conn() as c:
        rows = c.execute("SELECT * FROM items WHERE week=? ORDER BY id", (week,)).fetchall()
    out = [_item(r) for r in rows]
    if field:
        out = [i for i in out if field in i["fields"]]
    return out


def get_item(iid):
    with conn() as c:
        return _item(c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone())


def get_items(ids):
    return [i for i in (get_item(x) for x in ids) if i]


def url_exists(url):
    if not url:
        return False
    with conn() as c:
        return bool(c.execute("SELECT 1 FROM items WHERE url=?", (url,)).fetchone())


def save_item(d, iid=None):
    cols = ["week", "title_orig", "title_ko", "source_name", "url", "published", "fields", "foreign_aircraft",
            "type", "summary", "maturity", "collect_mode", "status", "memo", "data_class", "link_ok", "evals"]
    d = dict(d)
    d["fields"] = json.dumps(d.get("fields") or [], ensure_ascii=False)
    d["evals"] = json.dumps(d.get("evals") or [], ensure_ascii=False)
    d.setdefault("status", "미검토")
    d.setdefault("data_class", "공개")
    vals = [d.get(k) for k in cols]
    with conn() as c:
        if iid:
            c.execute(f"UPDATE items SET {', '.join(k + '=?' for k in cols)} WHERE id=?", vals + [iid])
            return iid
        cur = c.execute(f"INSERT INTO items({', '.join(cols)}, created) VALUES({', '.join('?' * len(cols))}, ?)",
                        vals + [datetime.now().isoformat(timespec="seconds")])
        return cur.lastrowid


def set_item_status(iid, status):
    with conn() as c:
        c.execute("UPDATE items SET status=? WHERE id=?", (status, iid))


def delete_item(iid):
    with conn() as c:
        c.execute("DELETE FROM items WHERE id=?", (iid,))


def weeks_with_data():
    with conn() as c:
        rows = c.execute("SELECT week FROM items UNION SELECT week FROM reports ORDER BY week DESC").fetchall()
    return [r["week"] for r in rows]


# ── 보고서·초안 ───────────────────────────────────────
def get_report(week):
    with conn() as c:
        r = c.execute("SELECT * FROM reports WHERE week=?", (week,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["item_ids"] = json.loads(d["item_ids"] or "[]")
    return d


def get_report_by_id(rid):
    with conn() as c:
        r = c.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["item_ids"] = json.loads(d["item_ids"] or "[]")
    return d


def save_report(week, item_ids, body, status):
    now = datetime.now().isoformat(timespec="seconds")
    with conn() as c:
        if c.execute("SELECT 1 FROM reports WHERE week=?", (week,)).fetchone():
            c.execute("UPDATE reports SET item_ids=?, body=?, status=?, updated=? WHERE week=?",
                      (json.dumps(item_ids), body, status, now, week))
        else:
            c.execute("INSERT INTO reports(week, item_ids, body, status, updated) VALUES(?,?,?,?,?)",
                      (week, json.dumps(item_ids), body, status, now))


def shared_reports():
    with conn() as c:
        rows = c.execute("SELECT * FROM reports WHERE status IN ('확정','공유') ORDER BY week DESC").fetchall()
    return [dict(r) for r in rows]


def get_draft(week):
    with conn() as c:
        r = c.execute("SELECT * FROM drafts WHERE week=?", (week,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["sections"] = json.loads(d["sections"] or "{}")
    return d


def save_draft(week, item_id, sections, template_version="임시 목차"):
    now = datetime.now().isoformat(timespec="seconds")
    with conn() as c:
        if c.execute("SELECT 1 FROM drafts WHERE week=?", (week,)).fetchone():
            c.execute("UPDATE drafts SET item_id=?, sections=?, template_version=?, updated=? WHERE week=?",
                      (item_id, json.dumps(sections, ensure_ascii=False), template_version, now, week))
        else:
            c.execute("INSERT INTO drafts(week, item_id, template_version, sections, updated) VALUES(?,?,?,?,?)",
                      (week, item_id, template_version, json.dumps(sections, ensure_ascii=False), now))


# ── 수집 실행 기록 ───────────────────────────────────
def log_run(week, mode, started, result, count):
    with conn() as c:
        c.execute("INSERT INTO runs(week, started, finished, mode, result, count) VALUES(?,?,?,?,?,?)",
                  (week, started, datetime.now().isoformat(timespec="seconds"), mode, result, count))


def last_runs(n=5):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,))]


# ── 웹 배포용 스냅샷(확정·공유 보고서와 그 소식만) ─────────────
ITEM_COLS = ["id", "week", "title_orig", "title_ko", "source_name", "url", "published", "fields",
             "foreign_aircraft", "type", "summary", "maturity", "collect_mode", "status", "memo",
             "data_class", "link_ok", "evals", "created"]


def export_snapshot():
    """확정·공유 보고서와 거기 들어간 소식만 내보낸다. 기준 정보·초안·작성자 메모는 넣지 않는다."""
    with conn() as c:
        reps = [dict(r) for r in c.execute(
            "SELECT week, item_ids, body, status, updated FROM reports WHERE status IN ('확정','공유')")]
        ids = sorted({i for r in reps for i in json.loads(r["item_ids"] or "[]")})
        items = []
        for iid in ids:
            r = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if r:
                d = {k: r[k] for k in ITEM_COLS}
                d["memo"] = ""
                items.append(d)
    return {"share_token": get_setting("share_token"), "exported": datetime.now().isoformat(timespec="seconds"),
            "reports": reps, "items": items}


def load_snapshot():
    """웹 배포본 시작 시: 스냅샷을 빈 /tmp DB 에 넣는다(이미 넣었으면 건너뜀)."""
    if not os.path.exists(SNAPSHOT_PATH) or get_setting("snapshot_loaded"):
        return
    with open(SNAPSHOT_PATH, encoding="utf-8") as f:
        snap = json.load(f)
    with conn() as c:
        for d in snap.get("items", []):
            c.execute(f"INSERT OR REPLACE INTO items({', '.join(ITEM_COLS)}) VALUES({', '.join('?' * len(ITEM_COLS))})",
                      [d.get(k) for k in ITEM_COLS])
        for r in snap.get("reports", []):
            c.execute("INSERT INTO reports(week, item_ids, body, status, updated) VALUES(?,?,?,?,?)",
                      (r["week"], r["item_ids"], r["body"], r["status"], r["updated"]))
    if snap.get("share_token"):
        set_setting("share_token", snap["share_token"])
    set_setting("snapshot_loaded", snap.get("exported", "1"))


# ── 수집 요청(API 키 없이 Claude 앱 예약 작업이 처리) ──────────
def add_request(week):
    """같은 주차의 대기 요청이 있으면 새로 만들지 않는다. 새로 만들었으면 True."""
    with conn() as c:
        if c.execute("SELECT 1 FROM collect_requests WHERE week=? AND done IS NULL", (week,)).fetchone():
            return False
        c.execute("INSERT INTO collect_requests(week, requested) VALUES(?,?)",
                  (week, datetime.now().isoformat(timespec="seconds")))
        return True


def pending_requests():
    with conn() as c:
        return [r["week"] for r in c.execute(
            "SELECT DISTINCT week FROM collect_requests WHERE done IS NULL ORDER BY week")]


def finish_request(week):
    with conn() as c:
        c.execute("UPDATE collect_requests SET done=? WHERE week=? AND done IS NULL",
                  (datetime.now().isoformat(timespec="seconds"), week))
