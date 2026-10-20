# -*- coding: utf-8 -*-
"""웹 배포본(Vercel)에 올릴 내용을 만든다.

  python publish.py   → published/snapshot.json 갱신 (확정·공유 보고서와 그 소식만)

그다음 GitHub 에 커밋·푸시하면 Vercel 이 다시 배포한다.
기준 정보(우리 기종 수준), 소요제기 초안, 작성자 메모, 미확정 보고서는 올라가지 않는다.
"""
import json
import os
import sys

import store

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

if __name__ == "__main__":
    store.init()
    snap = store.export_snapshot()
    os.makedirs(os.path.dirname(store.SNAPSHOT_PATH), exist_ok=True)
    with open(store.SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    weeks = ", ".join(r["week"] for r in snap["reports"]) or "없음"
    print(f"공개 보고서 {len(snap['reports'])}건({weeks}), 소식 {len(snap['items'])}건 → {store.SNAPSHOT_PATH}")
    if not snap["reports"]:
        print("※ 확정·공유된 보고서가 없습니다. ② 주간 보고서에서 '확정' 또는 '확정 + 공유'로 저장한 뒤 다시 실행하세요.")
