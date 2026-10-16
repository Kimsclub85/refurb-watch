"""학복스(에브리유니즈) 아이폰 듀오 화이트 256 통신폰 재고 감시.

이벤트 페이지 /event/20260913_mno_iphone_duo_earlybird 의 상품 행을
web-api 로 직접 읽어서 `hasStock` 이 False → True 로 바뀌거나,
조건에 맞는 새 상품(예: U+ 추가)이 생기면 ntfy 로 알린다.

- 한 번 실행하면 DURATION 초 동안 INTERVAL 초 간격으로 돈다.
- 상태는 STATE 파일(JSON)에 남겨 다음 실행이 이어받는다.
- 표준 라이브러리만 사용한다.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://web-api.univstore.com/api/v2/events/path/{path}/rows/{row}/items?offset={off}"
EVENT_PATH = os.environ.get("HB_EVENT", "20260913_mno_iphone_duo_earlybird")
ROWS = range(0, 16)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0 Safari/537.36")

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
STATE = os.environ.get("HB_STATE", "hakboks_state.json")
DURATION = int(os.environ.get("DURATION", "270"))
INTERVAL = int(os.environ.get("INTERVAL", "30"))
ANNOUNCE = os.environ.get("ANNOUNCE") == "1"


def fetch_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json",
        "Referer": "https://www.univstore.com/", "Origin": "https://www.univstore.com"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def is_target(it):
    name = (it.get("name") or "") + " " + (it.get("frontName") or "")
    spec = (it.get("code") or "") + " " + (it.get("nameOption") or "")
    return (("duo" in name.lower() or "듀오" in name)
            and "256" in spec and ("화이트" in spec or "white" in spec.lower()))


def scan():
    """조건에 맞는 상품 {id: info}. 행을 하나도 못 읽으면 None."""
    found, ok = {}, 0
    for row in ROWS:
        off = 0
        while off is not None:
            try:
                d = fetch_json(API.format(path=EVENT_PATH, row=row, off=off))
            except urllib.error.HTTPError as e:
                if e.code != 404:  # 없는 행은 404 — 정상
                    print(f"row {row}: {e}", file=sys.stderr)
                break
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                print(f"row {row}: {e}", file=sys.stderr)
                break
            if d.get("statusCode") != "SUCCESS":
                break
            ok += 1
            res = d.get("result") or {}
            for it in res.get("items") or []:
                if is_target(it):
                    found[str(it["id"])] = {
                        "carrier": it.get("brandName"), "name": it.get("name"),
                        "spec": it.get("code"), "price": it.get("price1"),
                        "plan": it.get("planName"), "hasStock": bool(it.get("hasStock")),
                    }
            off = res.get("nextOffset")
    return found if ok else None


def ntfy(title, message, click=None, priority=4):
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "priority": priority, "tags": ["iphone"]}
    if click:
        payload["click"] = click
        payload["actions"] = [{"action": "view", "label": "상품 페이지", "url": click}]
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


def step(st):
    cur = scan()
    if cur is None:
        st["fail_streak"] = st.get("fail_streak", 0) + 1
        print("scan 실패", st["fail_streak"])
        if st["fail_streak"] == 6:
            ntfy("⚠️ 학복스 감시: API 연속 실패", "web-api.univstore.com 을 6번 연속 못 읽음. 차단됐을 수 있음.",
                 click="https://www.univstore.com/event/" + EVENT_PATH, priority=3)
        return
    st["fail_streak"] = 0
    prev = st.get("items", {})
    for iid, info in cur.items():
        old = prev.get(iid)
        url = f"https://www.univstore.com/item/{iid}"
        label = f"{info['carrier']} {info['name']} {info['spec']}"
        if info["hasStock"] and not (old and old.get("hasStock")):
            ntfy(f"📱 학복스 듀오 재고 뜸 — {info['carrier']}",
                 f"{label} · {info['price']:,}원 · {info['plan']}\n탭하면 상품 페이지.",
                 click=url, priority=5)
        elif old is None and prev:
            ntfy(f"학복스 듀오 새 상품 — {info['carrier']}",
                 f"{label} (재고 {'있음' if info['hasStock'] else '없음'})", click=url, priority=3)
        elif old and old.get("hasStock") and not info["hasStock"]:
            ntfy(f"학복스 듀오 품절 — {info['carrier']}", label, click=url, priority=2)
    # 일부 행만 일시적으로 실패해도 '새 상품' 알림이 반복되지 않게 이전 목록에 덮어쓴다
    st["items"] = {**prev, **cur}
    st["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print(time.strftime("%H:%M:%S"), {k: (v["carrier"], v["hasStock"]) for k, v in cur.items()})


def main():
    st = load_state()
    if ANNOUNCE:
        cur = scan() or {}
        lines = [f"{v['carrier']} {v['spec']} 재고={'O' if v['hasStock'] else 'X'}" for v in cur.values()]
        ntfy("🔔 학복스 듀오 감시 시작", "\n".join(lines) or "대상 상품을 못 찾음",
             click="https://www.univstore.com/event/" + EVENT_PATH, priority=3)
    end = time.time() + DURATION
    while True:
        step(st)
        save_state(st)
        if time.time() + INTERVAL > end:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
