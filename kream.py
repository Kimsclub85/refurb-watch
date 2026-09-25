#!/usr/bin/env python3
"""
KREAM 리퍼 아이패드 프로 감시기.

조건: 'Apple Refurbished' 등 리퍼 등급 [S+ / S / A] 이고 판매가 ≤ MAX_PRICE, 판매중(InStock).
이 조건에 새로 들어오는 순간(신규 등록 / 품절→재입고 / 가격이 선 아래로 내려옴)만 ntfy로 푸시한다.
탭하면 kream.co.kr/products/{id} → 크림 앱이 깔려 있으면 앱으로 열린다.

KREAM은 TLS 지문으로 봇을 막는다. urllib/curl은 간헐 500 → curl_cffi impersonate="chrome" 필요.
데이터는 검색 결과 SSR의 JSON-LD ItemList (페이지당 30개, &cursor=N, &price=0-MAX).
KREAM의 '리퍼'는 애플 인증 리퍼가 아니라 판매자 등급 중고다.
"""

import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from curl_cffi import requests as creq

from watch import ntfy, won

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "kream_state.json")

KEYWORD = "리퍼비시 아이패드 프로"
MAX_PRICE = 999_000
GRADES = ("S+", "S", "A")          # A등급 이상. V등급은 정의 미확인이라 제외.
EXCLUDE = ("키보드", "펜슬", "케이스", "Keyboard", "Pencil")
GONE_AFTER = 2
MAX_PAGES = 10
FAIL_ALERT_AFTER = 3

SEARCH_URL = ("https://kream.co.kr/search?keyword=" + urllib.parse.quote(KEYWORD)
              + f"&price=0-{MAX_PRICE}")

# 세대 → 칩. 이름에 칩이 직접 있으면 그걸 우선.
CHIP_11 = {1: "A12X", 2: "A12Z", 3: "M1", 4: "M2", 5: "M4"}
CHIP_129 = {1: "A9X", 2: "A10X", 3: "A12X", 4: "A12Z", 5: "M1", 6: "M2", 7: "M4", 8: "M5"}


def chip_of(name):
    m = re.search(r"\b(M\d|A1\d[XZ]?)\b", name)
    if m:
        return m.group(1)
    g = re.search(r"(\d)\s*세대", name) or re.search(r"프로\s*(\d)\b", name)
    if not g:
        return None
    gen = int(g.group(1))
    if re.search(r"12\.9|13\s*(형|인치)?\s*\d?\s*\d*세대|프로 13", name):
        return CHIP_129.get(gen)
    if re.search(r"프로 11|11\s*(형|인치)", name):
        return CHIP_11.get(gen)
    if gen == 6:
        return "M2"  # 6세대는 12.9형뿐
    return None      # 크기 없이 2~5세대면 11/12.9 구분 불가


def grade_of(name):
    m = re.search(r"\[(S\+|S|A|B|V|C)\s*(?:등급|급)\]", name)
    return m.group(1) if m else None


def fetch_all():
    s = creq.Session(impersonate="chrome")
    found = {}
    for page in range(1, MAX_PAGES + 1):
        r = s.get(SEARCH_URL + f"&cursor={page}", timeout=25)
        if r.status_code != 200:
            raise RuntimeError(f"page {page} HTTP {r.status_code}")
        lst = None
        for m in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', r.text, re.S):
            ld = json.loads(m.group(1))
            if ld.get("@type") == "ItemList":
                lst = ld.get("itemListElement") or []
        if lst is None:
            if page == 1:
                raise RuntimeError("page 1: ItemList 없음 (구조 변경?)")
            break  # 마지막 페이지 다음은 빈 결과 페이지
        new = 0
        for e in lst:
            it = e["item"]
            pid = it["url"].rstrip("/").split("/")[-1]
            if pid not in found:
                new += 1
            found[pid] = {
                "name": it.get("alternateName") or it.get("name", ""),
                "brand": (it.get("brand") or {}).get("name", ""),
                "price": int(it["offers"]["price"]),
                "stock": it["offers"]["availability"].rsplit("/", 1)[-1] == "InStock",
                "url": it["url"],
            }
        if not new:
            break
        time.sleep(0.8)
    return found


def matches(p):
    n = p["name"]
    return (p["stock"] and p["price"] <= MAX_PRICE and grade_of(n) in GRADES
            and ("아이패드 프로" in n or "iPad Pro" in n)
            and not any(x in n for x in EXCLUDE))


CHIP_RANK = {"M5": 9, "M4": 8, "M2": 7, "M1": 6, "M1+": 5, "A12Z": 3, "A12X": 2, "A10X": 1, "A9X": 0}


def priority(chip):
    """M2 이상 긴급, M1 높음, A12X/Z 보통, 그보다 구형·미상은 무음(2)."""
    r = CHIP_RANK.get(chip, 0)
    return 5 if r >= 7 else 4 if r >= 5 else 3 if r >= 2 else 2


def label(p):
    chip = chip_of(p["name"])
    if chip is None and "5G" in p["name"]:
        chip = "M1+"  # 셀룰러 5G는 M1 세대부터
    name = re.sub(r"^\[[^\]]+\]\s*(리퍼비시\s*)?(애플\s*)?", "", p["name"])
    return chip, name


def main():
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except FileNotFoundError:
        state = {"items": {}, "initialized": False}
    items = state["items"]

    try:
        cur = fetch_all()
        if not cur:
            raise RuntimeError("결과 0건")
    except Exception as e:
        # 허브가 조용히 죽는 걸 막는다: 3회 연속 실패 시 한 번 알림
        state["fail_streak"] = state.get("fail_streak", 0) + 1
        if state["fail_streak"] == FAIL_ALERT_AFTER:
            ntfy("⚠️ 크림 감시 연속 실패", f"{FAIL_ALERT_AFTER}회 연속 조회 실패: {e}\n크림 차단 또는 허브 네트워크 확인",
                 click=SEARCH_URL, tags=["warning"], priority=4)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"KREAM 조회 실패 — 상태 보존: {e}", file=sys.stderr)
        sys.exit(1)
    if state.get("fail_streak", 0) >= FAIL_ALERT_AFTER:
        ntfy("✅ 크림 감시 복구", "다시 정상 조회 중.", tags=["white_check_mark"], priority=2)
    state["fail_streak"] = 0

    events = []
    for pid, p in cur.items():
        prev = items.get(pid)
        now_ok = matches(p)
        was_ok = bool(prev and prev.get("ok") and prev.get("missing", 0) < GONE_AFTER)
        if now_ok and not was_ok:
            if prev is None:
                why = "🆕 신규"
            elif not prev.get("stock") or prev.get("missing", 0) >= GONE_AFTER:
                why = "🔁 재입고"
            elif prev.get("price", 0) > MAX_PRICE:
                why = "💸 가격↓"
            else:
                why = "✅ 조건충족"
            events.append((why, pid, prev.get("price") if prev else None))
        elif now_ok and was_ok and p["price"] < prev["price"]:
            events.append(("💸 가격↓", pid, prev["price"]))
        items[pid] = {**p, "ok": now_ok, "missing": 0,
                      "first_seen": (prev or {}).get("first_seen", NOW.isoformat(timespec="minutes"))}

    for pid, rec in items.items():
        if pid not in cur:
            rec["missing"] = rec.get("missing", 0) + 1

    live = sorted((p | {"id": k} for k, p in cur.items() if matches(p)),
                  key=lambda p: (-CHIP_RANK.get(label(p)[0], 0), p["price"]))
    if not state.get("initialized"):
        lines = [f"{won(p['price'])} [{grade_of(p['name'])}] {label(p)[0] or '?'} · {label(p)[1][:40]}" for p in live[:8]]
        ntfy(f"🛒 크림 아이패드 프로 감시 시작 — 지금 조건 충족 {len(live)}개",
             "\n".join(lines) or "현재 조건 충족 없음. 뜨면 알려줌.",
             click=SEARCH_URL, tags=["shopping_cart"], priority=4)
        state["initialized"] = True
    else:
        for why, pid, old in events[:15]:
            p = cur[pid]
            chip, name = label(p)
            msg = f"{won(p['price'])}" + (f" (이전 {won(old)})" if old and old != p["price"] else "")
            msg += f"\n등급 {grade_of(p['name'])} · 칩 {chip or '미상'}\n{p['brand']} · 크림 판매자 중고"
            ntfy(f"{why} · 크림 {name[:52]}", msg, click=p["url"],
                 tags=["shopping_cart"], priority=priority(chip))

    state["items"] = items
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"{NOW:%Y-%m-%d %H:%M} kream total={len(cur)} match={len(live)} events={[(e[0], e[1]) for e in events]}")


if __name__ == "__main__":
    main()
