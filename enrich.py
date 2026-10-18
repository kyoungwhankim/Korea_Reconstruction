"""사업장 보강: 건축물대장(대지지분·현재 용적률/건폐율)과 아파트 매매 실거래가.

둘 다 data.go.kr 오픈API라 인증키(환경변수 DATA_GO_KR_KEY, 로컬은 .env)가 필요하다.
두 API 모두 '법정동코드 + 지번'으로 조회하므로, 먼저 사업장 주소를 그 형태로 바꾼다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import collect_reconstruction as cr

ENV_FILE = cr.BASE / ".env"


def api_key() -> str | None:
    """DATA_GO_KR_KEY: 환경변수(GitHub Actions secret) → 없으면 로컬 .env. 값은 출력하지 않는다."""
    key = os.environ.get("DATA_GO_KR_KEY")
    if not key and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DATA_GO_KR_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return key or None


# ---------------------------------------------------------------- 주소 → 법정동코드·지번
@lru_cache(maxsize=1)
def bjd_table() -> list[tuple[str, str]]:
    """[(10자리 법정동코드, '서울특별시 강남구 대치동')] — 현재 존재하는 동·리만."""
    b, _ = cr.datagokr_file("15123287")  # 국토교통부_법정동코드
    d = cr.read_csv_any(b)
    d = d[d["폐지여부"].eq("존재") & ~d["법정동코드"].str.endswith("00000")]
    return list(zip(d["법정동코드"], d["법정동명"]))


# 동 이름: '대치동', '산곡1동'(행정동), '성수동1가', '종로6가', '와우리'
_LOT = re.compile(r"(?P<dong>[가-힣]+(?:동\d+가|\d+가|\d*동|리|가))\s*(?P<san>산)?\s*(?P<bun>\d+)(?:\s*-\s*(?P<ji>\d+))?")


def parse_lot(sido: str, sigungu: str, address: str) -> dict | None:
    """'대치동316' / '경기도 안양시 동안구 호계동 570' / '산곡1동 180-329번지 일원' → 법정동코드·번·지.

    반환: {"bjd": 10자리, "sigungu_cd": 5, "bjdong_cd": 5, "plat_gb": "0"|"1", "bun": "0316", "ji": "0000",
          "dong": "대치동", "jibun": "316"}  — 못 찾으면 None
    """
    if not address:
        return None
    text = re.sub(r"번지", " ", str(address))
    # '소성로 72(학익동 413-1 번지 일원)'처럼 괄호 안에 지번이 있는 경우를 먼저 본다
    candidates = re.findall(r"\(([^)]*)\)", text) + [text]
    sgg = re.sub(r"\s+", "", sigungu or "")
    city = cr.sgg_key(sgg, sido)  # 경기: '안양' / 서울·인천: '강남구'
    for part in candidates:
        for m in _LOT.finditer(part):
            dong = m.group("dong")
            names = [dong]
            if re.search(r"\d+동$", dong):          # 행정동 '석수2동' → 법정동 '석수동'
                names.append(re.sub(r"\d+동$", "동", dong))
            prefix = part[:m.start()]                # '경기도 안양시 동안구 ' 같은 앞부분(있으면 정확히 일치시킨다)
            for name in names:
                code = _find_bjd(sido, city, name, prefix)
                if code:
                    ji = m.group("ji") or "0"
                    return {"bjd": code, "sigungu_cd": code[:5], "bjdong_cd": code[5:],
                            "plat_gb": "1" if m.group("san") else "0",
                            "bun": f"{int(m.group('bun')):04d}", "ji": f"{int(ji):04d}",
                            "dong": name, "jibun": m.group("bun") + (f"-{m.group('ji')}" if m.group("ji") else "")}
    return None


def _find_bjd(sido: str, city: str, dong: str, prefix: str) -> str | None:
    hits = [(c, n) for c, n in bjd_table() if n.startswith(sido) and n.endswith(" " + dong)]
    if not hits:
        return None
    gu_words = [w for w in re.split(r"\s+", prefix) if re.search(r"(시|구|군)$", w)]
    narrowed = [(c, n) for c, n in hits if all(w in n for w in gu_words)] if gu_words else hits
    narrowed = narrowed or hits
    in_city = [(c, n) for c, n in narrowed if city and city in n.replace(" ", "")]
    pick = in_city or narrowed
    return pick[0][0] if len({c for c, _ in pick}) == 1 else None  # 같은 이름의 동이 여러 구에 있으면 포기


# ---------------------------------------------------------------- API 호출 공통
STATE = cr.BASE / "state"
BUILDINGS_FILE = STATE / "buildings.jsonl"
TRADES_FILE = STATE / "trades.jsonl"
TRADE_MONTHS_FILE = STATE / "trade_months.json"
BLD_API = "https://apis.data.go.kr/1613000/BldRgstHubService/"
TRADE_API = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
TRADE_WINDOW = 12   # 최근 12개월 거래만 유지
TRADE_REFRESH = 3   # 신고 기한(30일)이 있어 최근 3개월은 매일 다시 받는다
WORKERS = 3         # 동시 요청 수
MAX_RPS = 5         # 초당 요청 상한 — 넘으면 data.go.kr이 429(Too Many Requests)를 돌려준다
_local = threading.local()
_rate_lock = threading.Lock()
_next_slot = [0.0]


class ApiError(Exception):
    pass


def _session():
    if not hasattr(_local, "s"):
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        s = cr.make_session()  # 429는 더 오래·더 여러 번 기다린다(Retry-After 헤더가 있으면 따른다)
        s.mount("https://", HTTPAdapter(max_retries=Retry(
            total=5, connect=1, read=1, status=5, backoff_factor=4, respect_retry_after_header=True,
            status_forcelist=(429, 500, 502, 503, 504), allowed_methods=None, raise_on_status=False)))
        _local.s = s
    return _local.s


def _throttle() -> None:
    with _rate_lock:
        now = time.monotonic()
        wait = _next_slot[0] - now
        _next_slot[0] = max(now, _next_slot[0]) + 1 / MAX_RPS
    if wait > 0:
        time.sleep(wait)


def _get(url: str, params: dict):
    """인증키를 붙여 호출. 오류 메시지에서 키는 지운다(로그·Actions 출력에 남지 않게)."""
    key = api_key()
    _throttle()
    try:
        r = _session().get(url, params={"serviceKey": key, **params}, timeout=cr.TIMEOUT)
        r.raise_for_status()
        return r
    except Exception as e:
        raise ApiError(str(e).replace(key or "\0", "***")) from None


def _lot_params(lot: dict) -> dict:
    return {"sigunguCd": lot["sigungu_cd"], "bjdongCd": lot["bjdong_cd"], "platGbCd": lot["plat_gb"],
            "bun": lot["bun"], "ji": lot["ji"]}


def lot_key(lot: dict) -> str:
    return f"{lot['bjd']}-{lot['plat_gb']}-{lot['bun']}-{lot['ji']}"


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- 사업장 → 대표 지번
def project_lots(con) -> dict[str, dict]:
    """활성 출처 행의 주소(우선순위 순)에서 처음 해석되는 지번을 사업장의 대표 지번으로 쓴다.
    단독주택 재건축으로 빠진 사업장은 조회하지 않는다."""
    apartments = {pid for pid, rec in con.execute("SELECT project_id, record FROM projects")
                  if json.loads(rec).get("_apartment")}
    rows: dict[str, list[dict]] = {}
    for pid, payload in con.execute("SELECT project_id, payload FROM source_rows WHERE removed_at IS NULL"):
        if pid in apartments:
            rows.setdefault(pid, []).append(json.loads(payload))
    lots = {}
    for pid, ps in rows.items():
        for p in sorted(ps, key=lambda p: cr.PRIORITY.get(p["source"], 99)):
            lot = parse_lot(p["region_sido"], p["region_sigungu"], p.get("address"))
            if lot:
                lots[pid] = lot
                break
    return lots


def project_names(con) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for pid, payload in con.execute("SELECT project_id, payload FROM source_rows WHERE removed_at IS NULL"):
        name = json.loads(payload)["zone_name"]
        keys = cr.name_keys([name])
        # 괄호 안 단지명도 이름으로 쓴다: '괴안3-5구역 (삼익세라믹아파트)' → '삼익세라믹'
        keys |= {cr.zone_key(x) for x in re.findall(r"\(([^)]*)\)", name) if not re.search(r"동 제외|재촉|통합", x)}
        out.setdefault(pid, set()).update(k for k in keys if k)
    return out


# ---------------------------------------------------------------- 건축물대장
def _bld_items(op: str, lot: dict) -> list[dict]:
    items, page = [], 1
    while True:
        r = _get(BLD_API + op, {**_lot_params(lot), "numOfRows": "100", "pageNo": str(page), "_type": "json"})
        try:
            body = r.json()["response"]
        except ValueError:
            raise ApiError(f"{op}: JSON이 아닌 응답 ({r.text[:120]})") from None
        if body["header"]["resultCode"] != "00":
            raise ApiError(f"{op}: {body['header']['resultCode']} {body['header'].get('resultMsg')}")
        got = body["body"].get("items") or {}
        got = got.get("item", []) if isinstance(got, dict) else []
        got = got if isinstance(got, list) else [got]
        items += got
        if len(got) < 100:
            return items
        page += 1


def building_summary(lot: dict) -> dict:
    """총괄표제부(단지 전체) 우선, 없거나 비어 있으면 동별 표제부를 합쳐 쓴다."""
    recap = _bld_items("getBrRecapTitleInfo", lot)
    titles = _bld_items("getBrTitleInfo", lot)
    homes = [t for t in titles if t.get("mainPurpsCdNm") == "공동주택" or _num(t.get("hhldCnt")) > 0]
    plat = max([_num(i.get("platArea")) for i in recap + titles] or [0])
    hhld = sum(_num(i.get("hhldCnt")) for i in recap) or sum(_num(t.get("hhldCnt")) for t in homes)
    far = max([_num(i.get("vlRat")) for i in recap] or [0]) or max([_num(t.get("vlRat")) for t in homes] or [0])
    bcr = max([_num(i.get("bcRat")) for i in recap] or [0]) or max([_num(t.get("bcRat")) for t in homes] or [0])
    days = sorted(d for d in (str(t.get("useAprDay", "")).strip() for t in homes) if re.fullmatch(r"\d{8}", d))
    names = sorted({re.sub(r"\s*\(?\d+동\)?$", "", str(i.get("bldNm", "")).strip()) for i in (recap or homes)} - {""})
    out = {"found": bool(recap or titles), "bld_name": " / ".join(names[:3]) or None,
           "land_area_m2": round(plat, 1) or None, "bld_households": int(hhld) or None,
           "bld_far": round(far, 2) or None, "bld_bcr": round(bcr, 2) or None,
           "bld_approved": f"{days[0][:4]}-{days[0][4:6]}-{days[0][6:]}" if days else None,
           "bld_buildings": len(homes) or None}
    if out["land_area_m2"] and out["bld_households"]:
        out["land_share_m2"] = round(out["land_area_m2"] / out["bld_households"], 1)
    return out


def update_buildings(lots: dict[str, dict], today: str, log: list, failed: list) -> bool:
    """지번이 새로 생겼거나 바뀐 사업장만 조회한다(건축물대장은 거의 바뀌지 않음)."""
    known = {r["project_id"]: r for r in _read_jsonl(BUILDINGS_FILE)}
    todo = {pid: lot for pid, lot in lots.items() if known.get(pid, {}).get("lot") != lot_key(lot)}
    stale = set(known) - set(lots)  # 지번을 잃은 사업장은 정보를 지운다
    if not todo and not stale:
        print("[건축물대장] 새로 조회할 사업장 없음")
        return False
    print(f"[건축물대장] {len(todo)}개 사업장 조회")
    errors = []

    def job(item):
        pid, lot = item
        try:
            return pid, {"project_id": pid, "lot": lot_key(lot), "fetched_at": today, **building_summary(lot)}
        except ApiError as e:
            errors.append(f"{pid}: {e}")
            return pid, None

    with ThreadPoolExecutor(WORKERS) as ex:
        for pid, rec in ex.map(job, sorted(todo.items())):
            if rec:
                if rec.get("land_share_m2") and not known.get(pid, {}).get("land_share_m2"):
                    log.append((today, pid, "건축물대장 반영", "land_share_m2", None, str(rec["land_share_m2"]),
                                "건축물대장"))
                known[pid] = rec
    for pid in stale:
        known.pop(pid)
    _write_jsonl(BUILDINGS_FILE, (known[k] for k in sorted(known)))
    if errors:
        failed.append(f"건축물대장 {len(errors)}건 실패 (예: {errors[0]})")
    return True


# ---------------------------------------------------------------- 실거래가
def months_back(today: str, n: int) -> list[str]:
    y, m = int(today[:4]), int(today[5:7])
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out


def fetch_trades(sgg: str, ym: str) -> list[dict]:
    rows, page = [], 1
    while True:
        r = _get(TRADE_API, {"LAWD_CD": sgg, "DEAL_YMD": ym, "numOfRows": "1000", "pageNo": str(page)})
        root = ET.fromstring(r.content)
        code = root.findtext(".//resultCode")
        if code not in ("000", "00"):
            raise ApiError(f"실거래 {sgg} {ym}: {code} {root.findtext('.//resultMsg')}")
        items = root.findall(".//item")
        for it in items:
            g = {c.tag: (c.text or "").strip() for c in it}
            if g.get("cdealType") == "O":  # 해제된 거래
                continue
            rows.append({"sgg": g.get("sggCd") or sgg, "dong": g.get("umdNm"), "jibun": g.get("jibun"),
                         "apt": g.get("aptNm"), "area": round(_num(g.get("excluUseAr")), 2),
                         "price": int(_num(g.get("dealAmount"))), "floor": int(_num(g.get("floor"))),
                         "date": f"{g.get('dealYear')}-{int(_num(g.get('dealMonth'))):02d}"
                                 f"-{int(_num(g.get('dealDay'))):02d}",
                         "built": int(_num(g.get("buildYear"))) or None})
        total = int(root.findtext(".//totalCount") or 0)
        if page * 1000 >= total or not items:
            return rows
        page += 1


OLD_AGE = 30  # 노후 단지 기준(년)


def old_cutoff(today: str) -> int:
    """이 해 이전(포함)에 지어진 단지를 노후 단지로 본다: 2026년 → 1996년 이전."""
    return int(today[:4]) - OLD_AGE


@lru_cache(maxsize=1)
def capital_sggs() -> tuple[str, ...]:
    """수도권(서울·인천·경기) 실거래 조회용 시군구 코드 — 동을 직접 가진 코드만(구가 있는 시는 구 코드)."""
    return tuple(sorted({c[:5] for c, _ in bjd_table() if c[:2] in ("11", "28", "41")}))


def match_trades(trades: list[dict], lots: dict[str, dict], names: dict[str, set[str]],
                 old_before: int | None = None) -> list[dict]:
    """같은 시군구·법정동에서 ① 지번이 같으면 연결, ② 아니면 단지명이 충분히 비슷하고 후보가 하나일 때만 연결.
    old_before가 있으면 사업장에 연결되지 않은 거래도 그 해 이전에 지어진 단지(노후 단지)면 남긴다."""
    by_dong: dict[tuple, list[str]] = {}
    for pid, lot in lots.items():
        by_dong.setdefault((lot["sigungu_cd"], lot["dong"]), []).append(pid)
    out = []
    for t in trades:
        cands = by_dong.get((t["sgg"], t["dong"]), [])
        exact = [p for p in cands if lots[p]["jibun"] == t["jibun"]]
        pid, how = (exact[0], "지번") if len(exact) == 1 else (None, None)
        if not pid and cands:
            # 이름으로는 보수적으로: 같거나, 사업장 이름에서 앞의 동 이름만 뺀 형태('진주' ↔ '여의도진주')일 때만.
            # 거래 쪽 이름이 더 길면('워커힐' → '워커힐푸르지오', '목동2' → '목동2차아이파크') 다른 단지로 본다.
            key = cr.zone_key(t["apt"])
            hits = [p for p in cands if len(key) >= 2 and any(
                k == key or (k.endswith(key) and len(k) - len(key) <= 4 and not re.search(r"\d", k[:-len(key)]))
                or key in (k + "맨션", k + "맨숀")  # '삼익파크' ↔ '삼익파크맨션'
                for k in names.get(p, ()))]
            if len(hits) == 1:
                pid, how = hits[0], "단지명"
        if pid:
            out.append({**t, "project_id": pid, "match": how})
        elif old_before and t.get("built") and t["built"] <= old_before:
            out.append({**t, "project_id": None, "match": None})
    return out


def update_trades(lots: dict[str, dict], names: dict[str, set[str]], today: str, log: list, failed: list) -> bool:
    months = months_back(today, TRADE_WINDOW)
    fresh = set(months[:TRADE_REFRESH])
    state = json.loads(TRADE_MONTHS_FILE.read_text(encoding="utf-8")) if TRADE_MONTHS_FILE.exists() else {}
    sggs = sorted(set(capital_sggs()) | {lot["sigungu_cd"] for lot in lots.values()})  # 노후 단지까지: 수도권 전체
    cutoff = old_cutoff(today)
    # 시군구의 사업장 지번 구성이 바뀌면(새 사업장 등) 그 시군구는 기간 전체를 다시 받아 다시 연결한다
    lots_sig = {s: hashlib.sha1(json.dumps(sorted((p, lot_key(l)) for p, l in lots.items() if l["sigungu_cd"] == s),
                                           ensure_ascii=False).encode()).hexdigest()[:12] for s in sggs}
    jobs = [(s, ym) for s in sggs for ym in months
            if ym in fresh or ym not in state.get(s, {}).get("months", {}) or state[s].get("lots") != lots_sig[s]]
    if not jobs:
        print("[실거래] 새로 받을 달 없음")
        return False
    print(f"[실거래] 시군구 {len(sggs)}곳 · 요청 {len(jobs)}건")
    old = list(_read_jsonl(TRADES_FILE))
    fetched: dict[tuple, list[dict]] = {}
    errors = []

    def job(item):
        try:
            return item, fetch_trades(*item)
        except (ApiError, ET.ParseError) as e:
            errors.append(f"{item}: {e}")
            return item, None

    with ThreadPoolExecutor(WORKERS) as ex:
        for item, rows in ex.map(job, jobs):
            if rows is not None:
                fetched[item] = rows
    done = set(fetched)
    ym_of = lambda t: t["date"][:7].replace("-", "")  # noqa: E731
    keep = [t for t in old if (t["sgg"], ym_of(t)) not in done and ym_of(t) in months]
    keep = match_trades(keep, lots, names, cutoff)  # 저장된 거래도 현재 규칙·지번으로 다시 연결(규칙이 바뀌어도 일관되게)
    new = match_trades([t for rows in fetched.values() for t in rows], lots, names, cutoff)
    merged = sorted(keep + new, key=lambda t: (t["project_id"] or "", t["sgg"], t["dong"], t["jibun"], t["apt"],
                                                t["date"], t["area"], t["floor"], t["price"]))
    before, after = _count(old), _count(merged)
    for pid in sorted(after):
        if after[pid] > before.get(pid, 0):
            log.append((today, pid, "실거래 추가", "trade_count_12m", str(before.get(pid, 0)), str(after[pid]),
                        "국토부 실거래가"))
    for s, ym in done:  # 받은 달만 표시(날짜를 적으면 매일 파일이 바뀌어 의미 없는 커밋이 생긴다)
        state.setdefault(s, {"months": {}})["months"][ym] = True
    for s in sggs:
        state.setdefault(s, {"months": {}})["lots"] = lots_sig[s]
        state[s]["months"] = {ym: True for ym in state[s]["months"] if ym in months}
    _write_jsonl(TRADES_FILE, merged)
    TRADE_MONTHS_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                 encoding="utf-8", newline="\n")
    print(f"[실거래] 저장 {len(merged)}건 — 사업장 연결 {sum(after.values())}건({len(after)}곳), "
          f"노후 단지(≤{cutoff}년 준공) {sum(1 for t in merged if not t['project_id'])}건")
    if errors:
        failed.append(f"실거래 {len(errors)}건 실패 (예: {errors[0]})")
    return merged != old


def _count(trades) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in trades:
        if t["project_id"]:
            out[t["project_id"]] = out.get(t["project_id"], 0) + 1
    return out


def trade_summary(trades: list[dict]) -> dict:
    """전용면적(㎡, 반올림)별 건수·중위·최저·최고·최근 거래. 가격 단위: 만원."""
    groups: dict[int, list[dict]] = {}
    for t in trades:
        groups.setdefault(round(t["area"]), []).append(t)
    by_area = []
    for area in sorted(groups):
        g = sorted(groups[area], key=lambda t: (t["date"], t["price"]))
        prices = [t["price"] for t in g]
        by_area.append({"area": area, "pyeong": round(area / 3.3058, 1), "n": len(g),
                        "median": int(statistics.median(prices)), "min": min(prices), "max": max(prices),
                        "latest_price": g[-1]["price"], "latest_date": g[-1]["date"], "latest_floor": g[-1]["floor"]})
    latest = max(trades, key=lambda t: (t["date"], t["price"]))
    ppp = [t["price"] / (t["area"] / 3.3058) for t in trades if t["area"]]
    return {"trade_count_12m": len(trades), "trade_latest_date": latest["date"], "trade_latest_price": latest["price"],
            "trade_latest_area": round(latest["area"]), "trade_median_ppp": int(statistics.median(ppp)) if ppp else None,
            "trades_by_area": by_area,
            "trades_by_area_text": "; ".join(f"{a['area']}㎡ 중위 {a['median']:,}만원({a['n']}건)" for a in by_area)}


# ---------------------------------------------------------------- 진입점
def update(con, today: str, log: list, failed: list) -> bool:
    """update_daily에서 호출. 바뀐 것이 있으면 True."""
    lots, names = project_lots(con), project_names(con)
    print(f"[보강] 지번을 아는 사업장 {len(lots)}곳")
    changed = update_buildings(lots, today, log, failed)
    return update_trades(lots, names, today, log, failed) or changed


def fields(records: dict[str, dict]) -> dict[str, dict]:
    """export 때 사업장 레코드에 붙일 보강 필드 {project_id: {...}}. records: {project_id: 사업장 레코드}"""
    out: dict[str, dict] = {}
    for b in _read_jsonl(BUILDINGS_FILE):
        out[b["project_id"]] = {k: v for k, v in b.items()
                                if k not in ("project_id", "lot", "fetched_at", "found") and v is not None}
    by_pid: dict[str, list[dict]] = {}
    for t in _read_jsonl(TRADES_FILE):
        if t["project_id"]:
            by_pid.setdefault(t["project_id"], []).append(t)
    for pid, ts in by_pid.items():
        rec = records.get(pid, {})
        if rec.get("stage") == "준공":
            basis, use = "준공 후 신축", ts
        else:
            # 기존 단지 준공연도보다 훨씬 늦게 지어진 건물의 거래는 같은 지번의 신축(분할 준공 등) → 제외
            old = rec.get("existing_built_year") or int((out.get(pid, {}).get("bld_approved") or "0")[:4]) or None
            use = [t for t in ts if not (old and t.get("built") and t["built"] > old + 5)]
            basis = "기존 단지"
        if use:
            out.setdefault(pid, {}).update(trade_summary(use), trade_basis=basis)
    return out


def _read_jsonl(path: Path):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
