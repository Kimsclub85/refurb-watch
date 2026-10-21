#!/usr/bin/env python3
"""
Apple 리퍼비쉬 감시기 (한국 + 미국).

KR  https://www.apple.com/kr/shop/refurbished/{mac,ipad,airpods,appletv,accessories}  전부 알림
US  https://www.apple.com/shop/refurbished/{mac,ipad}  스펙점수 US_MIN_SCORE 이상만 알림
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
US_MIN_SCORE = int(os.environ.get("US_MIN_SCORE", "55"))
SHIP_USD = 40             # 배대지 요금 가정 (노트북·태블릿 1대)
REGIONS = {
    "kr": {"path": "/kr", "cats": ("mac", "ipad", "airpods", "appletv", "accessories"),
           "cur": "KRW", "lang": "ko-KR,ko;q=0.9", "empty": "현재 재고가 없습니다",
           "ram_re": r"(\d+)GB 통합 메모리", "min_score": 0, "flag": "", "name": "한국"},
    "us": {"path": "", "cats": ("mac", "ipad"),
           "cur": "USD", "lang": "en-US,en;q=0.9", "empty": "currently unavailable",
           "ram_re": r"(\d+)GB unified memory", "min_score": US_MIN_SCORE, "flag": "🇺🇸 ", "name": "미국"},
}
CATEGORIES = REGIONS["kr"]["cats"]  # kream.py 등 외부 호환
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

GONE_AFTER = 2            # 연속 N회 성공 조회에서 안 보이면 '품절'로 본다 (깜빡임 방지)
NEW_PRICE_TTL_DAYS = 3    # 신품 가격 캐시 수명
MAX_ALERTS = 15           # 한 번에 개별 푸시 상한. 넘치면 요약 1건.
NEW_PRICE_BUDGET = 25     # 한 회차에 신품 가격을 새로 찾는 최대 개수 (첫 실행·TTL 만료가 몰려도 회차가 길어지지 않게)

NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"


# ───────────────────────── HTTP ─────────────────────────

def fetch(url, tries=3, lang="ko-KR,ko;q=0.9"):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept-Language": lang})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.geturl(), r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(2 * (i + 1))
    raise last


# ───────────────────────── 파싱 ─────────────────────────

BOOT_RE = re.compile(r"window\.REFURB_GRID_BOOTSTRAP\s*=\s*(\{.*?\});\s*</script>", re.S)


def parse_category(cat, body, empty_text="현재 재고가 없습니다", region="kr"):
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
        return [] if empty_text in body else None
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
            "region": region,
            "title": html.unescape(t.get("title", "")).replace("‑", "-").replace("\xa0", " "),
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
    "A12X": 38, "A12Z": 39, "A13": 30, "A14": 38, "A15": 42,
    "A16": 45, "A17 Pro": 50, "A18 Pro": 60,
}

# 칩 이름이 제목에 없는 iPad: 모델 + 세대로 칩을 정한다. "(4th Generation)" / "(4세대)"
IPAD_GEN_CHIP = {
    "ipadpro_11": {1: "A12X", 2: "A12Z", 3: "M1", 4: "M2"},
    "ipadpro_12_9": {3: "A12X", 4: "A12Z", 5: "M1", 6: "M2"},
    "ipadair": {4: "A14", 5: "M1"},
    "ipadmini": {6: "A15"},
    "ipad2017": {9: "A13", 10: "A14"},
}


def ipad_gen_chip(title, model):
    g = re.search(r"\((\d+)(?:st|nd|rd|th) Generation\)", title) or re.search(r"(\d+)세대", title)
    if not g:
        return None
    for k, table in IPAD_GEN_CHIP.items():
        if model.startswith(k):
            return table.get(int(g.group(1)))
    return None
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
    gc = re.search(r"(\d+)(?:코어|-Core) GPU", title)
    cc = re.search(r"(\d+)(?:코어|-Core) CPU", title)
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
    if chip is None and it["cat"] == "ipad":
        chip = ipad_gen_chip(t, d.get("refurbClearModel", ""))
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

def refurb_ram(url, reg=REGIONS["kr"]):
    """타일에 메모리 필터가 없는 모델(16형 MBP, Neo 등)은 상품 페이지 사양에서 읽는다."""
    try:
        _, body = fetch(url, lang=reg["lang"])
    except Exception:
        return None
    m = re.search(reg["ram_re"], body)
    return int(m.group(1)) if m else None


def new_price(refurb_part, reg=REGIONS["kr"]):
    """F????KH/A → M????KH/A (US는 LL/A) 신품 가격. 신품 판매 안 하면 None."""
    if not refurb_part.startswith("F"):
        return None  # G로 시작하는 건 CTO 구성 → 대응 신품 부품번호 없음
    mpart = "M" + refurb_part[1:]
    try:
        final, body = fetch(f"{BASE}{reg['path']}/shop/product/{mpart}", lang=reg["lang"])
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


def money(n, cur):
    return f"${n:,}" if cur == "USD" else won(n)


_FX = None


def usd_krw():
    """USD→KRW. 실패하면 None (원화 환산 줄만 빠진다)."""
    global _FX
    if _FX is None:
        try:
            _, body = fetch("https://open.er-api.com/v6/latest/USD", tries=2)
            _FX = float(json.loads(body)["rates"]["KRW"])
        except Exception:
            _FX = 0.0
    return _FX or None


def landed_krw(usd):
    """미국가 → 한국 도착가 추정: (가격 + 배대지) × 부가세 10% × 환율. 노트북·태블릿 관세 0%, 델라웨어 배대지(판매세 0) 가정."""
    fx = usd_krw()
    return round((usd + SHIP_USD) * 1.1 * fx, -3) if fx else None


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


def describe(rec, items=None):
    cur = "USD" if rec.get("region") == "us" else "KRW"
    bits = [money(rec["price"], cur)]
    if rec.get("new_price") and isinstance(rec["new_price"], int):
        bits.append(f"신품 {money(rec['new_price'], cur)} 대비 -{rec['discount']:.1f}%")
    extra = ""
    if cur == "USD":
        land = landed_krw(rec["price"])
        if land:
            extra = f"\n한국 도착 추정 {won(int(land))} (부가세·배대지 포함)"
            kr = (items or {}).get(rec.get("part", "")[:5] + "KH/A")
            if kr and kr.get("in_stock"):
                diff = kr["price"] - land
                extra += f"\n한국 리퍼 {won(kr['price'])} → {'-' if diff > 0 else '+'}{abs(diff) / 10000:.0f}만원"
    spec = [rec.get("chip") or "", f"{rec['ram']}GB" if rec.get("ram") else "",
            (f"{rec['ssd'] // 1024}TB" if rec["ssd"] >= 1024 else f"{rec['ssd']}GB") if rec.get("ssd") else "",
            str(rec.get("year") or "")]
    return " · ".join(bits) + "\n" + " / ".join(x for x in spec if x) + f"\n스펙점수 {rec['score']}" + extra


def priority_of(rec):
    d = rec.get("discount") or 0
    if rec["score"] >= 75 or d >= 20:
        return 5
    if rec["score"] >= 55 or d >= 17:
        return 4
    return 3


def short_name(title):
    s = re.sub(r"^(리퍼비쉬|Refurbished)\s*", "", title)
    s = s.replace("Apple ", "").replace(" 칩 모델", "").replace(" chip", "").replace(" Chip", "")
    s = re.sub(r"(\d+)코어 CPU 및 (\d+)코어 GPU", r"\1C/\2G", s)
    s = re.sub(r"\s*with (\d+)-Core CPU and (\d+)-Core GPU", r" (\1C/\2G)", s)
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
    ok_cats = []  # [(region, cat)]
    budget = [NEW_PRICE_BUDGET]

    for region, reg in REGIONS.items():
        for cat in reg["cats"]:
            try:
                _, body = fetch(f"{BASE}{reg['path']}/shop/refurbished/{cat}", lang=reg["lang"])
            except Exception as e:
                print(f"[{region}/{cat}] 조회 실패: {e}", file=sys.stderr)
                continue
            cur = parse_category(cat, body, reg["empty"], region)
            if cur is None:
                print(f"[{region}/{cat}] 파싱 실패 — 이번 회차 상태 보존", file=sys.stderr)
                continue
            ok_cats.append((region, cat))
            seen = set()
            for it in cur:
                p = it["part"]
                seen.add(p)
                rec = items.get(p)
                if rec is None:
                    ram = cap_gb(it["dims"].get("tsMemorySize"))
                    if it["cat"] == "mac" and not ram and (region == "kr" or "Pro" in it["title"] or "Max" in it["title"]):
                        ram = refurb_ram(it["url"], reg)
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
                rec.update({"cat": cat, "region": region, "title": it["title"], "price": it["price"],
                            "url": it["url"], "score": score, "chip": chip, "ssd": ssd, "year": year,
                            "in_stock": True, "missing": 0})

                # 신품 가격 (캐시)
                np_at = rec.get("new_price_at")
                stale = (not np_at or datetime.fromisoformat(np_at) < NOW - timedelta(days=NEW_PRICE_TTL_DAYS))
                wanted = score >= reg["min_score"]  # 미국은 알림 대상만 신품가를 찾는다
                if wanted and (stale or rec.get("new_price") == "ERR") and budget[0] > 0:
                    budget[0] -= 1
                    rec["new_price"] = new_price(p, reg)
                    rec["new_price_at"] = NOW.isoformat(timespec="minutes")
                npx = rec.get("new_price")
                rec["discount"] = round((1 - it["price"] / npx) * 100, 1) if isinstance(npx, int) and npx > 0 else None

                if ev and score >= reg["min_score"]:
                    events.append((ev, p, old_price))

            for p, rec in items.items():
                if rec.get("region", "kr") == region and rec.get("cat") == cat and p not in seen:
                    rec["missing"] = rec.get("missing", 0) + 1
                    if rec["missing"] >= GONE_AFTER and rec.get("in_stock"):
                        rec["in_stock"] = False
                        rec["gone_at"] = NOW.isoformat(timespec="minutes")

    if not ok_cats:
        print("모든 카테고리 조회 실패 — 상태 저장 안 함", file=sys.stderr)
        sys.exit(1)

    # 알림. 지역별로 첫 회차는 기준선만 저장한다.
    label = {"NEW": ("🆕 신규", "new"), "RESTOCK": ("🔁 재입고", "arrows_counterclockwise"),
             "DROP": ("💸 가격인하", "money_with_wings")}
    for region, reg in REGIONS.items():
        if not any(r == region for r, _ in ok_cats):
            continue
        key = "initialized" if region == "kr" else f"initialized_{region}"
        evs = sorted((e for e in events if items[e[1]].get("region", "kr") == region),
                     key=lambda e: -items[e[1]]["score"])
        refurb_url = f"{BASE}{reg['path']}/shop/refurbished"
        if not state.get(key):
            n = sum(1 for r in items.values() if r.get("in_stock") and r.get("region", "kr") == region)
            hi = sum(1 for r in items.values() if r.get("in_stock") and r.get("region", "kr") == region
                     and r["score"] >= reg["min_score"])
            ntfy(f"{reg['flag']}🍎 {reg['name']} 리퍼 감시 시작",
                 f"현재 재고 {n}개 (알림 대상 {hi}개)를 기준선으로 저장했어. 이제부터 신규·재입고·가격인하만 알려줌.",
                 click=refurb_url, tags=["apple"])
            state[key] = True
            continue
        cur = reg["cur"]
        for ev, p, old in evs[:MAX_ALERTS]:
            rec = items[p] | {"part": p}
            head, tag = label[ev]
            msg = describe(rec, items)
            if ev == "DROP":
                msg = f"{money(old, cur)} → " + msg
            ntfy(f"{reg['flag']}{head} · {short_name(rec['title'])}", msg, click=rec["url"],
                 tags=[tag], priority=priority_of(rec))
        if len(evs) > MAX_ALERTS:
            ntfy(f"{reg['flag']}➕ 외 {len(evs) - MAX_ALERTS}건", "STATUS.md 확인", click=refurb_url, tags=["apple"])

    state["items"] = items
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
    write_status(items)
    print(f"{NOW:%Y-%m-%d %H:%M} ok={ok_cats} events={[(e[0], e[1]) for e in events]}")


def write_status(items):
    live = [r | {"part": p} for p, r in items.items() if r.get("in_stock")]
    lines = ["# 애플 리퍼 재고 (한국 · 미국)", "",
             f"재고 변동 시점: {NOW:%Y-%m-%d %H:%M} KST · 재고 {len(live)}개", "",
             "스펙점수 = 칩(세대 반영) 45 · 메모리 30 · 저장 15 · 출시연도 10 (iPad는 메모리 대신 등급).",
             "할인율 = 같은 구성 신품의 현재 애플스토어 가격 대비. 빈칸 = 신품 없음(단종 또는 CTO).",
             f"미국은 스펙 {US_MIN_SCORE}점 이상만 표시·알림. 도착가 = (가격 + ${SHIP_USD}) × 1.1 × 환율 추정.", ""]
    for region, reg in REGIONS.items():
        for cat in reg["cats"]:
            rows = sorted((r for r in live if r.get("region", "kr") == region and r["cat"] == cat
                           and r["score"] >= reg["min_score"]), key=lambda r: (-r["score"], r["price"]))
            if not rows:
                continue
            us = region == "us"
            lines += [f"## {reg['flag']}{reg['name']} {cat} ({len(rows)})", "",
                      "| 점수 | 할인 | 가격 |" + (" 도착가 추정 |" if us else "") + " 제품 |",
                      "|---:|---:|---:|" + ("---:|" if us else "") + "---|"]
            for r in rows:
                disc = f"{r['discount']:.1f}%" if r.get("discount") is not None else ""
                name = short_name(r["title"]).replace("|", "/")
                land = landed_krw(r["price"]) if us else None
                lines.append(f"| {r['score']} | {disc} | {money(r['price'], reg['cur'])} |"
                             + (f" {won(int(land)) if land else ''} |" if us else "")
                             + f" [{name}]({r['url']}) |")
            lines.append("")
    with open(STATUS_PATH, "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
