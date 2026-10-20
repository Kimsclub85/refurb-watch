"""애플 Christiana Mall(R102) 아이폰 듀오 매장 픽업 감시.

pickup-message 엔드포인트를 한 번에 8개 SKU 로 읽어서, 각 SKU 의
`pickupDisplay` / `pickupSearchQuote` 가 바뀌면 ntfy 로 알린다.
지금(2026-10-20)은 전부 "Check back for availability on 10.24" 상태.

열리면 문구에서 최단 픽업일을 뽑고, 7일 보관을 가정해 11/4 픽업이
되는지 같이 적는다. 날짜를 직접 고를 수 있는 범위는 이 API 에 없어서
checkout 화면에서만 확인된다 (알림에 그렇게 적는다).

- 한 번 실행하면 DURATION 초 동안 INTERVAL 초 간격으로 돈다.
  workflow 가 끝날 때 다음 실행을 스스로 띄운다 (cron 은 몇 시간씩 밀려서).
- 상태는 STATE 파일(JSON)에 남겨 다음 실행이 이어받는다.
- 애플은 IP 당 ~30회 후 HTTP 541 로 막으므로 간격은 60초 이상.
- 표준 라이브러리만 사용한다.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

STORE = os.environ.get("PU_STORE", "R102")
SKUS = {
    "MK1E4LL/A": "256GB 스타화이트",
    "MK1F4LL/A": "256GB 나이트스카이",
    "MK1G4LL/A": "512GB 스타화이트",
    "MK1H4LL/A": "512GB 나이트스카이",
    "MK1J4LL/A": "1TB 스타화이트",
    "MK1L4LL/A": "1TB 나이트스카이",
    "MK1Q4LL/A": "2TB 스타화이트",
    "MK1R4LL/A": "2TB 나이트스카이",
}
TARGET = "MK1E4LL/A"
NEED = date(2026, 11, 4)   # 이 날 픽업해야 함
HOLD_DAYS = 7              # checkout 에서 확인한 보관 기간
BUY_URL = "https://www.apple.com/shop/buy-iphone/iphone-duo"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")
US_EAST = timezone(timedelta(hours=-4))  # 11/1 전까지 EDT

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
STATE = os.environ.get("PU_STATE", "pickup_state.json")
DURATION = int(os.environ.get("DURATION", "270"))
INTERVAL = int(os.environ.get("INTERVAL", "60"))
ANNOUNCE = os.environ.get("ANNOUNCE") == "1"

MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def fetch():
    q = [("pl", "true"), ("mts.0", "regular"), ("store", STORE), ("cppart", "UNLOCKED/US")]
    q += [(f"parts.{i}", s) for i, s in enumerate(SKUS)]
    url = "https://www.apple.com/shop/retail/pickup-message?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json", "Referer": BUY_URL})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def scan():
    """{sku: {"display", "quote"}} 또는 실패 시 (None, 이유)."""
    try:
        data = fetch()
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        return None, str(e)
    stores = (data.get("body") or {}).get("stores") or []
    st = next((s for s in stores if s.get("storeNumber") == STORE), None)
    if not st:
        return None, "응답에 매장 없음"
    out = {}
    for sku in SKUS:
        pa = (st.get("partsAvailability") or {}).get(sku) or {}
        out[sku] = {"display": pa.get("pickupDisplay", "?"),
                    "quote": pa.get("pickupSearchQuote", "")}
    return out, None


def is_open(info):
    q = info["quote"].lower()
    return info["display"] == "available" or not (
        q.startswith("check back") or "unavailable" in q or not q)


def quote_date(quote):
    """'Available Today' / 'Tomorrow' / 'Fri 10/24' / 'Oct 24' → date (미국 동부 기준)."""
    today = datetime.now(US_EAST).date()
    q = quote.lower()
    if "today" in q:
        return today
    if "tomorrow" in q:
        return today + timedelta(days=1)
    m = re.search(r"(\d{1,2})[/.](\d{1,2})", q)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"\b(" + "|".join(MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\b", q)
        if not m:
            return None
        mo, d = MONTHS[m.group(1)], int(m.group(2))
    try:
        return date(today.year, mo, d)
    except ValueError:
        return None


def verdict(quote):
    d = quote_date(quote)
    if d is None:
        return "날짜를 못 읽음 → 문구 직접 확인"
    hold = d + timedelta(days=HOLD_DAYS)
    head = f"최단 {d:%m/%d}, {HOLD_DAYS}일 보관이면 {hold:%m/%d}까지"
    if d > NEED:
        return f"{head} → 11/4 불가 (최단일이 11/4 이후)"
    if hold >= NEED:
        return f"{head} → 11/4 가능 (최단일 자동배정 가정)"
    return (f"{head} → 최단일로 잡히면 11/4 전에 만료. "
            "checkout 에서 날짜를 11/4 로 고를 수 있는지 확인 필요")


def ntfy(title, message, priority=4):
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "priority": priority, "tags": ["iphone"], "click": BUY_URL,
               "actions": [{"action": "view", "label": "구매 페이지", "url": BUY_URL}]}
    if DRY_RUN or not NTFY_TOPIC:
        print("[DRY]", json.dumps(payload, ensure_ascii=False))
        return
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except urllib.error.URLError as e:
        print("ntfy 실패:", e, file=sys.stderr)


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"items": {}, "fail_streak": 0}


def save_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


def line(sku, info):
    return f"{SKUS[sku]}: {info['quote'] or info['display']}"


def step(st):
    cur, err = scan()
    if cur is None:
        st["fail_streak"] = st.get("fail_streak", 0) + 1
        st["last_err"] = err
        print("scan 실패", st["fail_streak"], err)
        if st["fail_streak"] == 6:
            ntfy("⚠️ R102 픽업 감시: 연속 실패",
                 f"애플 pickup-message 를 6번 연속 못 읽음 ({err}). 541 차단일 수 있음.", priority=3)
        return
    st["fail_streak"] = 0
    st["last_err"] = None
    prev = st.get("items", {})
    # 첫 실행(이전 상태 없음)이면 이미 열린 SKU 만 알린다
    changed = ([s for s in cur if prev.get(s) != cur[s]] if prev
               else [s for s in cur if is_open(cur[s])])
    newly_open = [s for s in changed
                  if is_open(cur[s]) and not (prev.get(s) and is_open(prev[s]))]
    if changed:
        opened = [s for s in cur if is_open(cur[s])]
        tgt = cur[TARGET]
        body = []
        if is_open(tgt):
            body.append(f"[목표 {SKUS[TARGET]}] {tgt['quote']}")
            body.append(verdict(tgt["quote"]))
        body += [line(s, cur[s]) for s in changed if s != TARGET or not is_open(tgt)]
        if opened:
            body.append("날짜 선택 범위는 API 에 없음 → 장바구니 → 픽업 단계에서 확인.")
        title = ("🍎 R102 듀오 픽업 열림" if newly_open else "R102 듀오 픽업 문구 바뀜")
        ntfy(title, "\n".join(body), priority=5 if newly_open else 3)
    st["items"] = cur
    st["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print(time.strftime("%H:%M:%S"), cur[TARGET], "changed=" + ",".join(changed))


def main():
    st = load_state()
    if ANNOUNCE:
        cur, err = scan()
        msg = (line(TARGET, cur[TARGET]) + f"\n(8개 SKU 동일 여부: "
               f"{'예' if len({json.dumps(v) for v in cur.values()}) == 1 else '아니오'})"
               if cur else f"애플 응답 실패: {err}")
        ntfy("🔔 R102 듀오 픽업 감시 시작", msg, priority=3)
    end = time.time() + DURATION
    while True:
        step(st)
        save_state(st)
        # 541 이면 애플이 이 IP 를 10~15분 막은 것 → 쉬었다가 다시
        wait = 600 if st.get("last_err") == "HTTP 541" else INTERVAL
        if time.time() + wait > end:
            break
        time.sleep(wait)


if __name__ == "__main__":
    main()
