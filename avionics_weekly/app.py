# -*- coding: utf-8 -*-
"""국외 전투기 항공전자 주간 기술동향 분석 도구 (MVP)

실행:  python app.py            → 이 PC에서만 사용 (http://127.0.0.1:5000)
       python app.py --share    → 같은 네트워크의 다른 PC·휴대폰에서 보기 전용 링크 열람 가능
       python app.py --collect  → 지금 바로 지난주 소식 수집(창 없이)
"""
import argparse
import io
import re
import socket
import threading
import time
from datetime import date, datetime, timedelta

from flask import (Flask, abort, flash, redirect, render_template, request, send_file, url_for)
from markupsafe import Markup, escape

import ai
import hwpx_export
import store

app = Flask(__name__)
app.secret_key = "avionics-weekly-local"
store.init()

COLLECT_LOCK = threading.Lock()
COLLECT_STATE = {"running": False, "message": ""}


# ── 권한: 편집 화면은 이 PC(127.0.0.1)에서만 ────────────────────
def is_local():
    return request.remote_addr in ("127.0.0.1", "::1", "localhost")


@app.before_request
def guard():
    p = request.path
    if p.startswith("/static/") or p.startswith("/view/"):
        return None
    if store.PUBLIC:                 # 웹 배포본: 편집 화면 없음, 첫 화면은 보고서 목록
        if p == "/":
            return redirect(url_for("view_list", token=store.get_setting("share_token")))
        abort(404)
    if not is_local():
        abort(403)


@app.context_processor
def inject():
    return {"FIELDS": store.FIELDS, "TYPES": store.TYPES, "MATURITY": store.MATURITY,
            "GRADES": store.GRADES, "STATUSES": store.STATUSES, "has_key": ai.has_key(),
            "collect_state": COLLECT_STATE}


@app.template_filter("nl2br")
def nl2br(s):
    return Markup("<br>".join(escape(s or "").split("\n")))


@app.template_filter("render_body")
def render_body(body):
    """보고서 본문(# / ## / 일반)을 간단한 HTML 로 표시."""
    out = []
    for line in (body or "").splitlines():
        s = line.rstrip()
        if s.startswith("## "):
            out.append(f"<h3>{escape(s[3:])}</h3>")
        elif s.startswith("# "):
            out.append(f"<h2>{escape(s[2:])}</h2>")
        elif s.strip():
            t = escape(s)
            t = Markup(re.sub(r"(https?://[^\s<]+)", r'<a href="\1" target="_blank" rel="noopener">\1</a>', str(t)))
            out.append(f"<p>{t}</p>")
    return Markup("\n".join(out))


def week_label(week):
    mon, sun = store.week_range(week)
    return f"{week} ({mon:%m.%d.}~{sun:%m.%d.})"


app.jinja_env.globals["week_label"] = week_label


def week_choices(cur):
    ws = set(store.weeks_with_data()) | {store.last_week_key(), store.this_week_key(), cur}
    return sorted(ws, reverse=True)


# ── 자동 수집 ─────────────────────────────────────────────
def run_collect(week, mode="수동"):
    if not COLLECT_LOCK.acquire(blocking=False):
        return "이미 수집 중입니다."
    started = datetime.now().isoformat(timespec="seconds")
    COLLECT_STATE.update(running=True, message=f"{week} 수집 중… (수 분 걸릴 수 있음)")
    msg = ""
    try:
        items = ai.collect_week(week)
        n = 0
        for d in items:
            if store.url_exists(d["url"]):
                continue
            d.update(week=week, collect_mode="자동", status="미검토", memo="", data_class="공개",
                     link_ok=ai.check_link(d["url"]))
            store.save_item(d)
            n += 1
        msg = f"{week} 수집 완료: 새 소식 {n}건"
        store.log_run(week, mode, started, "성공", n)
        return msg
    except Exception as e:  # noqa
        msg = f"수집 실패: {e}"
        store.log_run(week, mode, started, msg[:300], 0)
        return msg
    finally:
        COLLECT_STATE.update(running=False, message=msg)
        COLLECT_LOCK.release()


def scheduler_loop():
    """앱이 켜져 있으면 1분마다 확인. 이번 주 설정 요일·시각이 지났고 아직 자동 수집을 안 했으면 지난주 소식을 수집."""
    while True:
        try:
            cfg = ai.config()
            if cfg.get("auto_collect") and ai.has_key():
                now = datetime.now()
                today = now.date()
                this_mon = today - timedelta(days=today.weekday())
                hh, mm = map(int, cfg["collect_time"].split(":"))
                due = datetime.combine(this_mon + timedelta(days=int(cfg["collect_weekday"])), datetime.min.time()).replace(hour=hh, minute=mm)
                tw = store.this_week_key(today)
                last_try = store.get_setting("last_auto_try", "")
                retry_ok = not last_try or (now - datetime.fromisoformat(last_try)) >= timedelta(hours=1)
                if now >= due and store.get_setting("last_auto_week") != tw and retry_ok:
                    store.set_setting("last_auto_try", now.isoformat(timespec="seconds"))
                    msg = run_collect(store.last_week_key(today), mode="자동")
                    if "수집 완료" in msg:             # 실패하면 1시간 뒤 다시 시도
                        store.set_setting("last_auto_week", tw)
        except Exception as e:  # noqa
            print("[scheduler]", e)
        time.sleep(60)


# ── 화면 1: 주간 소식함 ──────────────────────────────────
@app.route("/")
def inbox():
    week = request.args.get("week") or store.last_week_key()
    field = request.args.get("field") or ""
    items = store.items_for_week(week, field or None)
    report = store.get_report(week)
    selected = set(report["item_ids"]) if report else set()
    return render_template("inbox.html", week=week, field=field, items=items, weeks=week_choices(week),
                           selected=selected, runs=store.last_runs(3), pending=store.pending_requests())


@app.post("/collect")
def collect_now():
    week = request.form.get("week") or store.last_week_key()
    if not ai.has_key():
        new = store.add_request(week)
        flash(f"{week} 수집 요청을 " + ("등록했습니다. " if new else "이미 등록해 두었습니다. ")
              + "Claude 앱 → 예약 작업 → '항공전자 주간 수집'에서 '지금 실행'을 누르면 처리됩니다.")
        return redirect(url_for("inbox", week=week))
    threading.Thread(target=run_collect, args=(week, "수동"), daemon=True).start()
    flash(f"{week} 수집을 시작했습니다. 몇 분 뒤 새로고침하세요.")
    return redirect(url_for("inbox", week=week))


def item_from_form(f, base=None):
    d = dict(base or {})
    d.update(
        week=f.get("week"), title_orig=f.get("title_orig", "").strip(), title_ko=f.get("title_ko", "").strip(),
        source_name=f.get("source_name", "").strip(), url=f.get("url", "").strip(), published=f.get("published", "").strip(),
        fields=f.getlist("fields"), foreign_aircraft=f.get("foreign_aircraft", "").strip(), type=f.get("type"),
        summary=f.get("summary", "").strip(), maturity=f.get("maturity"), status=f.get("status") or "미검토",
        memo=f.get("memo", "").strip())
    evals = []
    for i in range(int(f.get("eval_count", 0))):
        ac = f.get(f"eval_aircraft_{i}", "").strip()
        if ac and not f.get(f"eval_delete_{i}"):
            evals.append({"aircraft": ac, "weakness": f.get(f"eval_weakness_{i}", "").strip(),
                          "considerations": f.get(f"eval_cons_{i}", "").strip(),
                          "grade": f.get(f"eval_grade_{i}", "중")})
    d["evals"] = evals
    d.setdefault("collect_mode", "직접")
    d.setdefault("data_class", "공개")
    return d


@app.route("/item/<int:iid>", methods=["GET", "POST"])
def item_detail(iid):
    item = store.get_item(iid) or abort(404)
    if request.method == "POST":
        d = item_from_form(request.form, item)
        if d["url"] != item["url"]:
            d["link_ok"] = ai.check_link(d["url"])
        store.save_item(d, iid)
        flash("저장했습니다.")
        return redirect(url_for("item_detail", iid=iid))
    return render_template("item.html", item=item, aircraft=store.aircraft_list(), new=False)


@app.route("/item/new", methods=["GET", "POST"])
def item_new():
    week = request.values.get("week") or store.last_week_key()
    if request.method == "POST":
        f = request.form
        if f.get("mode") == "ai":
            if not ai.has_key():
                flash("API 키가 없습니다. 항목을 직접 입력하세요.")
                return redirect(url_for("item_new", week=week))
            try:
                d = ai.analyze_manual(f.get("title", ""), f.get("url", ""), f.get("body", ""))
            except Exception as e:  # noqa
                flash(f"AI 분석 실패: {e}")
                return redirect(url_for("item_new", week=week))
            d.update(week=week, collect_mode="직접", status="미검토", memo="", data_class="공개",
                     link_ok=ai.check_link(d["url"]))
        else:
            d = item_from_form(f)
            d["collect_mode"] = "직접"
            d["link_ok"] = ai.check_link(d["url"])
        iid = store.save_item(d)
        flash("소식을 추가했습니다. 내용을 확인하세요.")
        return redirect(url_for("item_detail", iid=iid))
    blank = {"week": week, "title_orig": "", "title_ko": "", "source_name": "", "url": "", "published": "",
             "fields": [], "foreign_aircraft": "", "type": "신기술", "summary": "", "maturity": "개념·개발 중",
             "status": "미검토", "memo": "", "evals": [], "collect_mode": "직접", "link_ok": None, "id": None}
    return render_template("item.html", item=blank, aircraft=store.aircraft_list(), new=True)


@app.post("/item/<int:iid>/delete")
def item_delete(iid):
    item = store.get_item(iid) or abort(404)
    store.delete_item(iid)
    flash("삭제했습니다.")
    return redirect(url_for("inbox", week=item["week"]))


@app.post("/select")
def select_for_report():
    week = request.form["week"]
    ids = [int(x) for x in request.form.getlist("pick")]
    if not ids:
        flash("보고서에 넣을 소식을 1건 이상 고르세요(권장 2~3건).")
        return redirect(url_for("inbox", week=week))
    items = store.get_items(ids)
    body = compose_report(week, items)
    old = store.get_report(week)
    store.save_report(week, ids, body, "초안")
    for i in store.items_for_week(week):
        if i["id"] in ids:
            store.set_item_status(i["id"], "보고서 반영")
        elif i["status"] == "보고서 반영":
            store.set_item_status(i["id"], "검토 완료")
    flash("선택한 소식으로 보고서 초안을 " + ("다시 " if old else "") + "만들었습니다.")
    return redirect(url_for("report", week=week))


# ── 화면 2: 주간 보고서 ──────────────────────────────────
def compose_report(week, items):
    mon, sun = store.week_range(week)
    L = [f"# 1. 개요",
         f"기간: {mon:%Y.%m.%d.} ~ {sun:%Y.%m.%d.} / 수록: {len(items)}건 (공개 자료 기준)",
         "분야: " + ", ".join(sorted({f for i in items for f in i['fields']})), "",
         "# 2. 주요 기술동향"]
    for n, i in enumerate(items, 1):
        L += [f"## {n}) [{'/'.join(i['fields'])}] {i['title_ko'] or i['title_orig']}",
              f"○ 대상: {i['foreign_aircraft']} / 유형: {i['type']} / 성숙도: {i['maturity']}",
              f"○ 요약: {i['summary']}"]
        for e in i["evals"]:
            L.append(f"○ 적용 가능성 [{e['grade']}] {e['aircraft']}: {e['weakness']}")
            if e.get("considerations"):
                L.append(f"   - 도입 고려사항: {e['considerations']}")
        L += [f"○ 출처: {i['source_name']}, {i['published']}, {i['url']}", ""]
    L += ["# 3. 종합 의견", "[작성 필요: 이번 주 동향의 시사점과 후속 조치]"]
    return "\n".join(L)


@app.route("/report", methods=["GET", "POST"])
def report():
    week = request.values.get("week") or store.last_week_key()
    rep = store.get_report(week)
    if request.method == "POST":
        if not rep:
            abort(400)
        status = request.form.get("status") or rep["status"]
        store.save_report(week, rep["item_ids"], request.form.get("body", ""), status)
        flash(f"보고서를 저장했습니다 (상태: {status}).")
        return redirect(url_for("report", week=week))
    items = store.get_items(rep["item_ids"]) if rep else []
    token = store.get_setting("share_token")
    share_path = url_for("view_report", token=token, week=week)
    return render_template("report.html", week=week, rep=rep, items=items, weeks=week_choices(week),
                           share_urls=share_urls(share_path))


def share_urls(path):
    port = ai.config()["port"]
    urls = [f"http://127.0.0.1:{port}{path}"]
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))          # 실제 전송 없음: 이 PC의 네트워크 IP 확인용
        ip = s.getsockname()[0]
        s.close()
    except Exception:  # noqa
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa
            ip = ""
    if ip and not ip.startswith("127."):
        urls.append(f"http://{ip}:{port}{path}")
    return urls


@app.route("/report/<week>.hwpx")
def report_hwpx(week):
    rep = store.get_report(week) or abort(404)
    return _report_file(week, rep)


def _report_file(week, rep):
    mon, sun = store.week_range(week)
    data = hwpx_export.build_hwpx(
        title="국외 전투기 항공전자 주간 기술동향",
        subtitle=f"{week} ({mon:%Y.%m.%d.}~{sun:%Y.%m.%d.})",
        meta_lines=[f"작성일: {date.today():%Y-%m-%d}   |   상태: {rep['status']}   |   공개 자료 기준"],
        blocks=hwpx_export.text_to_blocks(rep["body"]))
    return send_file(io.BytesIO(data), as_attachment=True, download_name=f"주간보고서_{week}.hwpx",
                     mimetype="application/hwp+zip")


# ── 화면 3: 소요제기 초안 ────────────────────────────────
@app.route("/draft", methods=["GET", "POST"])
def draft():
    week = request.values.get("week") or store.last_week_key()
    d = store.get_draft(week)
    if request.method == "POST":
        act = request.form.get("action")
        if act in ("make", "make_ai"):
            item = store.get_item(int(request.form["item_id"])) or abort(404)
            if act == "make_ai":
                try:
                    sections = ai.draft_sections(item)
                except Exception as e:  # noqa
                    flash(f"AI 초안 실패: {e} → 기본 초안으로 만들었습니다.")
                    sections = basic_draft(item)
            else:
                sections = basic_draft(item)
            store.save_draft(week, item["id"], sections)
            flash("초안을 만들었습니다. 내용을 확인·수정하세요.")
        else:
            sections = {s: request.form.get(f"sec_{i}", "") for i, s in enumerate(store.DRAFT_SECTIONS)}
            store.save_draft(week, d["item_id"], sections)
            flash("초안을 저장했습니다.")
        return redirect(url_for("draft", week=week))
    rep = store.get_report(week)
    candidates = store.get_items(rep["item_ids"]) if rep else []
    if not candidates:
        candidates = store.items_for_week(week)
    item = store.get_item(d["item_id"]) if d else None
    return render_template("draft.html", week=week, d=d, item=item, candidates=candidates,
                           sections=store.DRAFT_SECTIONS, weeks=week_choices(week))


def basic_draft(i):
    ev = "\n".join(f"- {e['aircraft']} [{e['grade']}]: {e['weakness']}" for e in i["evals"]) or "- [작성 필요]"
    cons = "\n".join(f"- {e['aircraft']}: {e['considerations']}" for e in i["evals"] if e.get("considerations")) or "- [작성 필요]"
    return {
        store.DRAFT_SECTIONS[0]: f"- 우리 공군 기종 관련 분야({', '.join(i['fields'])})의 능력 보완 필요\n{ev}\n- [작성 필요: 소요 필요성 구체화]",
        store.DRAFT_SECTIONS[1]: f"- {i['foreign_aircraft']}: {i['title_ko'] or i['title_orig']} ({i['type']}, {i['maturity']})\n- {i['summary']}",
        store.DRAFT_SECTIONS[2]: "- [작성 필요: 운용 개념]",
        store.DRAFT_SECTIONS[3]: "- [작성 필요: 요구 능력·성능(개략)]",
        store.DRAFT_SECTIONS[4]: f"{ev}\n도입 고려사항\n{cons}",
        store.DRAFT_SECTIONS[5]: "- [작성 필요: 구매 / 개조 / 국내개발 중 검토]",
        store.DRAFT_SECTIONS[6]: f"- {i['source_name']}, {i['published']}, {i['url']}",
    }


@app.route("/draft/<week>.hwpx")
def draft_hwpx(week):
    d = store.get_draft(week) or abort(404)
    item = store.get_item(d["item_id"])
    blocks = []
    for n, s in enumerate(store.DRAFT_SECTIONS, 1):
        blocks.append(("h1", f"{n}. {s}"))
        blocks += [("body", ln) for ln in (d["sections"].get(s) or "").splitlines() if ln.strip()]
        blocks.append(("blank", ""))
    data = hwpx_export.build_hwpx(
        title="소요제기 문서 초안",
        subtitle=(item["title_ko"] or item["title_orig"]) if item else week,
        meta_lines=[f"작성일: {date.today():%Y-%m-%d}   |   양식: {d['template_version']}   |   공개 자료 기준 초안"],
        blocks=blocks)
    return send_file(io.BytesIO(data), as_attachment=True, download_name=f"소요제기초안_{week}.hwpx",
                     mimetype="application/hwp+zip")


# ── 화면 4: 기준 정보 관리 ───────────────────────────────
@app.route("/baseline", methods=["GET", "POST"])
def baseline():
    acs = store.aircraft_list()
    if request.method == "POST":
        f = request.form
        if f.get("action") == "aircraft":
            store.set_aircraft_list(f.get("aircraft_list", "").splitlines())
            flash("기종 목록을 저장했습니다.")
        elif f.get("action") == "sources":
            store.set_setting("sources", f.get("sources", ""))
            flash("수집 출처 목록을 저장했습니다.")
        else:
            ac = f.get("aircraft")
            for i, fld in enumerate(store.FIELDS):
                store.save_baseline(ac, fld, f.get(f"level_{i}", "").strip(), f.get(f"mode_{i}", "직접"),
                                    f.get(f"source_{i}", "").strip())
            flash(f"{ac} 기준 정보를 저장했습니다.")
            return redirect(url_for("baseline", aircraft=ac))
        return redirect(url_for("baseline"))
    cur = request.args.get("aircraft") or (acs[0] if acs else "")
    bm = store.baseline_map()
    token = store.get_setting("share_token")
    return render_template("baseline.html", acs=acs, cur=cur, bm=bm, sources=store.get_setting("sources"),
                           cfg=ai.config(), view_urls=share_urls(url_for("view_list", token=token)),
                           runs=store.last_runs(5))


# ── 보기 전용 (다른 사람·휴대폰) ─────────────────────────
def check_token(token):
    if token != store.get_setting("share_token"):
        abort(404)


@app.route("/view/<token>/")
def view_list(token):
    check_token(token)
    return render_template("view_list.html", reports=store.shared_reports(), token=token)


@app.route("/view/<token>/<week>")
def view_report(token, week):
    check_token(token)
    rep = store.get_report(week)
    if not rep or rep["status"] not in ("확정", "공유"):
        return render_template("view_report.html", rep=None, week=week, token=token, items=[])
    return render_template("view_report.html", rep=rep, week=week, token=token, items=store.get_items(rep["item_ids"]))


@app.route("/view/<token>/<week>.hwpx")
def view_report_hwpx(token, week):
    check_token(token)
    rep = store.get_report(week)
    if not rep or rep["status"] not in ("확정", "공유"):
        abort(404)
    return _report_file(week, rep)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--share", action="store_true", help="같은 네트워크에서 보기 전용 링크 열람 허용")
    ap.add_argument("--collect", action="store_true", help="지금 지난주 소식을 수집하고 종료")
    ap.add_argument("--week", help="--collect 와 함께: 수집할 주차(예: 2026-W42)")
    a = ap.parse_args()
    if a.collect:
        print(run_collect(a.week or store.last_week_key(), mode="명령줄"))
        raise SystemExit
    threading.Thread(target=scheduler_loop, daemon=True).start()
    port = int(ai.config()["port"])
    host = "0.0.0.0" if a.share else "127.0.0.1"
    print(f"\n  ▶ 작성자 화면: http://127.0.0.1:{port}/")
    if a.share:
        print("  ▶ 보기 전용 링크는 [기준 정보] 화면 아래쪽에 있습니다.")
    print("  (끄려면 이 창에서 Ctrl+C)\n")
    app.run(host=host, port=port, debug=False, threaded=True)
