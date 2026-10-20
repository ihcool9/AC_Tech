# -*- coding: utf-8 -*-
"""API 키 없이 Claude 앱(예약 작업·대화)이 수집한 소식을 앱 데이터에 넣는 도구.
Claude 가 COLLECT_GUIDE.md 의 순서대로 이 스크립트를 실행한다.

  python claude_collect.py todo                      → 이번에 수집할 주차 목록(요청된 주차 + 지난주)
  python claude_collect.py context --week 2026-W42  → 수집 기간·분야·기종·기준 정보·출처·이미 저장된 링크
  python claude_collect.py save --week 2026-W42 --file items.json [--mode 예약]
                                                    → 검증(기간·링크·중복) 후 저장, 결과 출력
"""
import argparse
import json
import sys
from datetime import date

import ai
import store

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def cmd_todo(_):
    weeks = store.pending_requests()
    lw = store.last_week_key()
    collected = {r["week"] for r in store.last_runs(50) if r["result"] == "성공" and r["mode"].startswith("Claude")}
    if lw not in weeks and lw not in collected:
        weeks.append(lw)
    for w in sorted(set(weeks)):
        print(f"{w}  ({'요청됨' if w in store.pending_requests() else '지난주 정기 수집'})")
    if not weeks:
        print("(수집할 주차 없음: 지난주는 이미 수집했고 대기 중인 요청도 없음)")


def cmd_context(a):
    week = a.week or store.last_week_key()
    mon, sun = store.week_range(week)
    have = store.items_for_week(week)
    print(f"[대상 주차] {week}  기간: {mon:%Y-%m-%d} ~ {sun:%Y-%m-%d} (이 기간에 발행된 자료만)")
    print(f"[이미 저장된 소식] {len(have)}건 → 목표는 합계 10~15건(부족하면 적어도 됨)")
    for i in have:
        print(f"  - {i['url']}")
    print(f"\n[분야] (fields 는 이 중 1~2개, 글자 그대로) {json.dumps(store.FIELDS, ensure_ascii=False)}")
    print(f"[유형] {json.dumps(store.TYPES, ensure_ascii=False)}")
    print(f"[기술 성숙도] {json.dumps(store.MATURITY, ensure_ascii=False)}")
    print(f"[우리 공군 기종] {json.dumps(store.aircraft_list(), ensure_ascii=False)}")
    print(f"[우리 기종 기준 정보(개략)]\n{store.baseline_text()}")
    print(f"\n[참고 출처]\n{store.get_setting('sources')}")
    print("\n" + ai.GRADE_RULE)
    print("\n[저장 형식] JSON 배열. " + ai.ITEM_SCHEMA)


def _in_range(published, mon, sun):
    try:
        return mon <= date.fromisoformat(published[:10]) <= sun
    except ValueError:
        return False


def cmd_save(a):
    week = a.week
    mon, sun = store.week_range(week)
    with open(a.file, encoding="utf-8-sig") as f:      # Windows 메모장·PowerShell 이 붙이는 BOM 허용
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("items", [])
    saved, skipped = 0, []
    for raw in data:
        d = ai._normalize(raw)
        name = d["title_ko"] or d["title_orig"] or d["url"]
        if not d["url"]:
            skipped.append((name, "출처 링크 없음")); continue
        if store.url_exists(d["url"]):
            skipped.append((name, "이미 저장됨")); continue
        if not _in_range(d["published"], mon, sun):
            skipped.append((name, f"발행일 {d['published'] or '없음'}이 기간 밖")); continue
        ok = ai.check_link(d["url"])
        if not ok:
            skipped.append((name, "링크가 열리지 않음")); continue
        d.update(week=week, collect_mode="자동", status="미검토", memo="", data_class="공개", link_ok=ok)
        store.save_item(d)
        saved += 1
        print(f"저장: {name}")
    for name, why in skipped:
        print(f"제외: {name} — {why}")
    store.log_run(week, f"Claude {a.mode}", date.today().isoformat(), "성공", saved)
    store.finish_request(week)
    print(f"\n{week}: 새로 저장 {saved}건, 제외 {len(skipped)}건, 이 주차 합계 {len(store.items_for_week(week))}건")


def main():
    store.init()
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("todo")
    p = sub.add_parser("context"); p.add_argument("--week")
    p = sub.add_parser("save"); p.add_argument("--week", required=True); p.add_argument("--file", required=True)
    p.add_argument("--mode", default="수동", help="기록용: 예약 / 수동")
    a = ap.parse_args()
    {"todo": cmd_todo, "context": cmd_context, "save": cmd_save}[a.cmd](a)


if __name__ == "__main__":
    main()
