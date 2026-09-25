#!/usr/bin/env python3
"""
Apple 한국 리퍼비쉬 감시기.

https://www.apple.com/kr/shop/refurbished/{mac,ipad,airpods,appletv,accessories}
각 페이지에 박힌 window.REFURB_GRID_BOOTSTRAP JSON에서 재고 목록을 읽는다.

알림 이벤트
  NEW      처음 보는 부품번호
  RESTOCK  사라졌다가(연속 GONE_AFTER회 미노출) 다시 뜬 부품번호
  DROP     같은 부품번호의 가격 인하

두 축
  스펙점수 (0-100)  최신성 + 고스펙: 칩 성능(세대 반영) 45 · 메모리 30 · 저장공간 15 · 출시연도 10
                    iPad는 메모리 대신 등급(Pro>Air>mini>기본)
  할인율 (%)        같은 구성의 신품(F→M 부품번호) 현재 애플스토어 가격 대비. 실측만 표시.
                    신품이 없으면(단종 / CTO 구성) 빈칸.

알림은 ntfy로 보낸다. 탭하면 해당 리퍼 제품 주문 페이지가 바로 열린다.
표준 라이브러리만 사용한다.
"""

import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "state.json")
STATUS_PATH = os.path.join(ROOT, "STATUS.md")

BASE = "https://www.apple.com"
CATEGORIES = ("mac", "ipad", "airpods", "appletv", "accessories")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

GONE_AFTER = 2            # 연속 N회 성공 조회에서 안 보이면 '품절'로 본다 (깜빡임 방지)
NEW_PRICE_TTL_DAYS = 3    # 신품 가격 캐시 수명
MAX_ALERTS = 15           # 한 번에 개별 푸시 상한. 넘치면 요약 1건.

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"


# ───────────────────────── HTTP ─────────────────────────

def fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.geturl(), r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(2 * (i + 1))
    raise last


# ───────────────────────── 파싱 ─────────────────────────

BOOT_RE = re.compile(r"window\.REFURB_GRID_BOOTSTRAP\s*=\s*(\{.*?\});\s*</script>", re.S)


def parse_category(cat, body):
    """성공하면 [item...] (재고 0이면 []), 파싱 실패면 None."""
    m = BOOT_RE.search(body)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    tiles = d.get("tiles")
    if tiles is None:
        # 재고 없음 페이지는 tiles=None + 안내 문구. 그 외는 구조 변경으로 보고 실패 처리.
        return [] if "현재 재고가 없습니다" in body else None
    items = []
    for t in tiles:
        part = t.get("partNumber")
        try:
            price = int(float(t["price"]["currentPrice"]["raw_amount"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not part:
            continue
        url = t.get("productDetailsUrl", "").split("?")[0]
        items.append({
            "part": part,
            "cat": cat,
            "title": html.unescape(t.get("title", "")).replace("‑", "-"),
            "price": price,
            "url": BASE + url if url.startswith("/") else url,
            "dims": (t.get("filters") or {}).get("dimensions") or {},
        })
    return items


def cap_gb(s):
    """'512gb' / '1tb' / '1point5tb' / '1_5tb' → GB"""
    if not s:
        return None
    s = s.lower().replace("point", ".").replace("_", ".")
    m = re.match(r"([\d.]+)\s*(gb|tb)", s)
    if not m:
        return None
    v = float(m.group(1))
    return int(v * 1024) if m.group(2) == "tb" else int(v)


# ───────────────────────── 스펙 점수 ─────────────────────────

# 칩 성능 지표: 대략적인 Geekbench 6 멀티코어 기준 상대값 (M4 = 100).
# 표에 없는 칩은 코어 수로 추정한다. 절대값이 아니라 줄 세우기용.
CHIP_CPU = {
    "M1": 57, "M1 Pro": 84, "M1 Max": 84, "M1 Ultra": 122,
    "M2": 66, "M2 Pro": 99, "M2 Max": 100, "M2 Ultra": 143,
    "M3": 80, "M3 Pro": 102, "M3 Max": 143, "M3 Ultra": 187,
    "M4": 100, "M4 Pro": 153, "M4 Max": 176,
    "M5": 119, "M5 Pro": 180, "M5 Max": 200,
    "A16": 45, "A17 Pro": 50, "A18 Pro": 60,
}
CHIP_MAX = 200  # 정규화 기준 (이보다 크면 100점 캡)


def chip_of(title):
    m = re.search(r"\b(M\d+)(?:\s*(Pro|Max|Ultra))?\b", title)
    if m:
        return m.group(1) + (" " + m.group(2) if m.group(2) else "")
    m = re.search(r"\b(A\d+)(?:\s*(Pro|Bionic))?\b", title)
    if m:
        return m.group(1) + (" Pro" if m.group(2) == "Pro" else "")
    return None


def chip_index(chip, title):
    """CPU 지표 + GPU 코어 보정. 0~1."""
    cpu = CHIP_CPU.get(chip or "")
    gc = re.search(r"(\d+)코어 GPU", title)
    cc = re.search(r"(\d+)코어 CPU", title)
    if cpu is None:
        # 모르는 칩: 세대·코어로 추정
        gen = int(re.search(r"\d+", chip).group()) if chip and re.search(r"\d+", chip) else 1
        cpu = (int(cc.group(1)) if cc else 8) * 10 * (1 + 0.15 * (gen - 4))
    idx = cpu / CHIP_MAX
    if gc:  # 같은 칩이라도 GPU 많은 빈(bin)에 소폭 가산
        idx *= 1 + min(int(gc.group(1)), 80) / 400
    return min(idx, 1.0)


def log_norm(v, lo, hi):
    if not v:
        return 0.0
    return max(0.0, min(1.0, math.log2(v / lo) / math.log2(hi / lo)))


IPAD_TIER = {"ipadpro": 1.0, "ipadair": 0.7, "ipadmini": 0.5, "ipad": 0.35}


def spec_score(it, ram_gb):
    t, d = it["title"], it["dims"]
    chip = chip_of(t)
    ci = chip_index(chip, t)
    ssd = cap_gb(d.get("dimensionCapacity"))
    year = int(d.get("dimensionRelYear") or 0) or None
    recency = max(0.0, min(1.0, (year - (NOW.year - 5)) / 5)) if year else 0.0

    if it["cat"] == "mac":
        s = (0.45 * ci + 0.30 * log_norm(ram_gb, 8, 128)
             + 0.15 * log_norm(ssd, 128, 8192) + 0.10 * recency)
        if "Nano-texture" in t:
            s += 0.02
    elif it["cat"] == "ipad":
        model = d.get("refurbClearModel", "")
        tier = next((v for k, v in IPAD_TIER.items() if model.startswith(k)), 0.35)
        s = (0.45 * ci + 0.30 * tier + 0.15 * log_norm(ssd, 64, 2048) + 0.10 * recency)
        if d.get("dimensionconnectivity") == "wificell":
            s += 0.02
    else:
        s = 0.5 * recency
    return round(min(s, 1.0) * 100), chip, ssd, year


# ───────────────────────── 보조 조회 ─────────────────────────

def refurb_ram(url):
    """타일에 메모리 필터가 없는 모델(16형 MBP, Neo 등)은 상품 페이지 사양에서 읽는다."""
    try:
        _, body = fetch(url)
    except Exception:
        return None
    m = re.search(r"(\d+)GB 통합 메모리", body)
    return int(m.group(1)) if m else None


def new_price(refurb_part):
    """F????KH/A → M????KH/A 신품 가격. 신품 판매 안 하면 None."""
    if not refurb_part.startswith("F"):
        return None  # G로 시작하는 건 CTO 구성 → 대응 신품 부품번호 없음
    mpart = "M" + refurb_part[1:]
    try:
        final, body = fetch(f"{BASE}/kr/shop/product/{mpart}")
    except Exception:
        return "ERR"
    if "/search/" in final:
        return None
    esc = re.escape(mpart)
    m = (re.search(r'"partNumber":"%s","price":\{"fullPrice":([\d.]+)' % esc, body)
         or re.search(r'"price":([\d.]+),"sku":"%s"' % esc, body))
    return int(float(m.group(1))) if m else None


# ───────────────────────── 알림 ─────────────────────────

def won(n):
    return f"₩{n:,}"


def ntfy(title, message, click=None, tags=None, priority=3):
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
        payload["actions"] = [{"action": "view", "label": "주문 페이지", "url": click}]
    if DRY_RUN or not NTFY_TOPIC:
        print("[DRY]", json.dumps(payload, ensure_ascii=False))
        return True
    req = urllib.request.Request(NTFY_SERVER, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except urllib.error.URLError as e:
        print(f"ntfy 실패: {e}", file=sys.stderr)
        return False


def describe(rec):
    bits = [won(rec["price"])]
    if rec.get("new_price") and isinstance(rec["new_price"], int):
        bits.append(f"신품 {won(rec['new_price'])} 대비 -{rec['discount']:.1f}%")
    spec = [rec.get("chip") or "", f"{rec['ram']}GB" if rec.get("ram") else "",
            (f"{rec['ssd'] // 1024}TB" if rec["ssd"] >= 1024 else f"{rec['ssd']}GB") if rec.get("ssd") else "",
            str(rec.get("year") or "")]
    return " · ".join(bits) + "\n" + " / ".join(x for x in spec if x) + f"\n스펙점수 {rec['score']}"


def priority_of(rec):
    d = rec.get("discount") or 0
    if rec["score"] >= 75 or d >= 20:
        return 5
    if rec["score"] >= 55 or d >= 17:
        return 4
    return 3


def short_name(title):
    s = re.sub(r"^리퍼비쉬\s*", "", title)
    s = s.replace("Apple ", "").replace(" 칩 모델", "")
    s = re.sub(r"(\d+)코어 CPU 및 (\d+)코어 GPU", r"\1C/\2G", s)
    s = s.replace("Nano-texture 디스플레이", "나노텍스처").replace(" - ", " ")
    return re.sub(r"\s+", " ", s).strip()


# ───────────────────────── 메인 ─────────────────────────

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"items": {}, "initialized": False}


def main():
    state = load_state()
    items = state["items"]
    events = []
    ok_cats = []

    for cat in CATEGORIES:
        try:
            _, body = fetch(f"{BASE}/kr/shop/refurbished/{cat}")
        except Exception as e:
            print(f"[{cat}] 조회 실패: {e}", file=sys.stderr)
            continue
        cur = parse_category(cat, body)
        if cur is None:
            print(f"[{cat}] 파싱 실패 — 이번 회차 상태 보존", file=sys.stderr)
            continue
        ok_cats.append(cat)
        seen = set()
        for it in cur:
            p = it["part"]
            seen.add(p)
            rec = items.get(p)
            if rec is None:
                ram = cap_gb(it["dims"].get("tsMemorySize"))
                if it["cat"] == "mac" and not ram:
                    ram = refurb_ram(it["url"])
                rec = {"first_seen": NOW.isoformat(timespec="minutes"), "ram": ram}
                items[p] = rec
                ev = "NEW"
            elif rec.get("missing", 0) >= GONE_AFTER:
                ev = "RESTOCK"
            elif it["price"] < rec["price"]:
                ev = "DROP"
            else:
                ev = None
            old_price = rec.get("price")
            score, chip, ssd, year = spec_score(it, rec.get("ram"))
            rec.update({"cat": cat, "title": it["title"], "price": it["price"], "url": it["url"],
                        "score": score, "chip": chip, "ssd": ssd, "year": year,
                        "in_stock": True, "missing": 0})

            # 신품 가격 (캐시)
            np_at = rec.get("new_price_at")
            stale = (not np_at or datetime.fromisoformat(np_at) < NOW - timedelta(days=NEW_PRICE_TTL_DAYS))
            if stale or rec.get("new_price") == "ERR":
                rec["new_price"] = new_price(p)
                rec["new_price_at"] = NOW.isoformat(timespec="minutes")
            npx = rec.get("new_price")
            rec["discount"] = round((1 - it["price"] / npx) * 100, 1) if isinstance(npx, int) and npx > 0 else None

            if ev:
                events.append((ev, p, old_price))

        for p, rec in items.items():
            if rec.get("cat") == cat and p not in seen:
                rec["missing"] = rec.get("missing", 0) + 1
                if rec["missing"] >= GONE_AFTER and rec.get("in_stock"):
                    rec["in_stock"] = False
                    rec["gone_at"] = NOW.isoformat(timespec="minutes")

    if not ok_cats:
        print("모든 카테고리 조회 실패 — 상태 저장 안 함", file=sys.stderr)
        sys.exit(1)

    # 알림
    if not state.get("initialized"):
        n = sum(1 for r in items.values() if r.get("in_stock"))
        ntfy("🍎 리퍼 감시 시작", f"현재 재고 {n}개를 기준선으로 저장했어. 이제부터 신규·재입고·가격인하만 알려줌.",
             click=f"{BASE}/kr/shop/refurbished", tags=["apple"])
        state["initialized"] = True
    else:
        events.sort(key=lambda e: -items[e[1]]["score"])
        label = {"NEW": ("🆕 신규", "new"), "RESTOCK": ("🔁 재입고", "arrows_counterclockwise"),
                 "DROP": ("💸 가격인하", "money_with_wings")}
        for ev, p, old in events[:MAX_ALERTS]:
            rec = items[p]
            head, tag = label[ev]
            short = short_name(rec["title"])
            msg = describe(rec)
            if ev == "DROP":
                msg = f"{won(old)} → " + msg
            ntfy(f"{head} · {short}", msg, click=rec["url"], tags=[tag], priority=priority_of(rec))
        if len(events) > MAX_ALERTS:
            ntfy(f"➕ 외 {len(events) - MAX_ALERTS}건", "STATUS.md 확인",
                 click=f"{BASE}/kr/shop/refurbished", tags=["apple"])

    state["items"] = items
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    write_status(items, ok_cats, events)
    print(f"{NOW:%Y-%m-%d %H:%M} ok={ok_cats} events={[(e[0], e[1]) for e in events]}")


def write_status(items, ok_cats, events):
    live = [r | {"part": p} for p, r in items.items() if r.get("in_stock")]
    lines = [f"# 애플 KR 리퍼 재고", "",
             f"재고 변동 시점: {NOW:%Y-%m-%d %H:%M} KST · 재고 {len(live)}개", "",
             "스펙점수 = 칩(세대 반영) 45 · 메모리 30 · 저장 15 · 출시연도 10 (iPad는 메모리 대신 등급).",
             "할인율 = 같은 구성 신품의 현재 애플스토어 가격 대비. 빈칸 = 신품 없음(단종 또는 CTO).", ""]
    for cat in CATEGORIES:
        rows = sorted((r for r in live if r["cat"] == cat), key=lambda r: (-r["score"], r["price"]))
        if not rows:
            continue
        lines += [f"## {cat} ({len(rows)})", "", "| 점수 | 할인 | 가격 | 제품 |", "|---:|---:|---:|---|"]
        for r in rows:
            disc = f"{r['discount']:.1f}%" if r.get("discount") is not None else ""
            name = short_name(r["title"]).replace("|", "/")
            lines.append(f"| {r['score']} | {disc} | {won(r['price'])} | [{name}]({r['url']}) |")
        lines.append("")
    with open(STATUS_PATH, "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
