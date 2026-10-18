"""수도권 30년 이상 노후 아파트 단지 목록.

재건축 사업장(collect_reconstruction / update_daily)과 별개로, 아직 사업이 시작되지 않은 단지까지
'재건축 가능성이 있는 노후 단지'를 모은다. 단지는 '법정동코드 + 지번'으로 구분한다.

  - 건축물대장 총괄표제부를 법정동마다 나열 → 단지명·세대수·대지면적·용적률·건폐율·사용승인일
    (분기마다 한 바퀴: 매일 DONG_BUDGET개 법정동씩 새로 받는다)
  - 아파트 매매 실거래가(enrich.py가 수도권 전체를 받아 둔 것) → 준공연도·거래가
  - 준공연도가 어디에도 없는 큰 단지는 동별 표제부로 사용승인일을 채운다(매일 YEAR_BUDGET개까지)
  - K-apt(공동주택관리정보시스템) 단지 목록(분기)·기본정보(단지별 1회) → 공식 단지명·사용승인일·세대수·동수·최고층·난방·시공사
"""
from __future__ import annotations

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import collect_reconstruction as cr
import enrich as en

REGISTRY_FILE = en.STATE / "complexes" / "registry.jsonl"
DONGS_FILE = en.STATE / "complexes" / "registry_dongs.json"
SITE_FILE = cr.BASE / "docs" / "data" / "apartments.json"
CSV_FILE = cr.OUT / "old_apartments_latest.csv"
MIN_HOUSEHOLDS = 20   # 이보다 작은 공동주택(소규모 다세대 등)은 뺀다
DONG_BUDGET = 100     # 하루에 새로 받는 법정동 수 (≈2,900곳 → 약 한 달에 한 바퀴, 분기마다 반복)
YEAR_BUDGET = 150     # 하루에 사용승인일을 채우는 단지 수
MIN_YEAR = 1960      # 이보다 이른 준공연도는 오기로 본다
KAPT_BUDGET = 1500   # 하루에 K-apt 기본정보를 새로 받는 단지 수(처음 한 번은 전부)


# ---------------------------------------------------------------- 법정동
@lru_cache(maxsize=1)
def capital_dongs() -> tuple[tuple[str, str], ...]:
    """건축물대장을 조회할 수도권 법정동(동·리). 아래에 리가 있는 읍·면 코드는 빼고 리를 쓴다."""
    rows = [(c, n) for c, n in en.bjd_table() if c[:2] in ("11", "28", "41")]
    has_ri = {c[:8] for c, _ in rows if c[8:] != "00"}
    return tuple((c, n) for c, n in rows if not (c[8:] == "00" and c[:8] in has_ri))


@lru_cache(maxsize=1)
def dong_index() -> dict[tuple[str, str], str]:
    """(시군구코드 5자리, 법정동명) → 법정동코드 10자리. 실거래 레코드(sggCd, umdNm)를 코드로 바꿀 때 쓴다."""
    out = {}
    for code, name in en.bjd_table():
        if code[:2] in ("11", "28", "41"):
            out[(code[:5], name.split()[-1])] = code
            if code[8:] != "00":  # 리: '봉담읍 와우리'처럼 읍면+리로도 찾을 수 있게
                out[(code[:5], " ".join(name.split()[-2:]))] = code
    return out


def quarter(today: str) -> str:
    return f"{today[:4]}Q{(int(today[5:7]) - 1) // 3 + 1}"


def complex_key(bjd: str, bun: int, ji: int, plat_gb: str = "0") -> str:
    """enrich.lot_key와 같은 형식: 사업장 대표 지번과 그대로 비교할 수 있다."""
    return f"{bjd}-{plat_gb}-{bun:04d}-{ji:04d}"


def jibun_key(sgg: str, dong: str, jibun: str) -> str | None:
    code = dong_index().get((sgg, dong))
    m = re.fullmatch(r"\s*(산)?\s*(\d+)(?:-(\d+))?\s*", jibun or "")
    if not code or not m:
        return None
    return complex_key(code, int(m.group(2)), int(m.group(3) or 0), "1" if m.group(1) else "0")


# ---------------------------------------------------------------- 건축물대장: 법정동 단위 나열
def _recap_dong(bjd: str) -> list[dict]:
    items, page = [], 1
    while True:
        r = en._get(en.BLD_API + "getBrRecapTitleInfo",
                    {"sigunguCd": bjd[:5], "bjdongCd": bjd[5:], "numOfRows": "100", "pageNo": str(page), "_type": "json"})
        try:
            body = r.json()["response"]
        except ValueError:
            raise en.ApiError(f"{bjd}: JSON이 아닌 응답 ({r.text[:100]})") from None
        if body["header"]["resultCode"] != "00":
            raise en.ApiError(f"{bjd}: {body['header']['resultCode']} {body['header'].get('resultMsg')}")
        got = body["body"].get("items") or {}
        got = got.get("item", []) if isinstance(got, dict) else []
        got = got if isinstance(got, list) else [got]
        items += got
        if len(got) < 100:
            break
        page += 1
    out = []
    for i in items:
        hh = int(en._num(i.get("hhldCnt")))
        if i.get("mainPurpsCdNm") != "공동주택" or hh < MIN_HOUSEHOLDS:
            continue
        day = lambda k: (lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}" if re.fullmatch(r"\d{8}", d) else None)(  # noqa: E731
            str(i.get(k, "")).strip())
        out.append({
            "key": complex_key(bjd, int(en._num(i.get("bun"))), int(en._num(i.get("ji"))), str(i.get("platGbCd") or "0")),
            "bjd": bjd, "name": str(i.get("bldNm") or "").strip() or None, "households": hh,
            "land_area_m2": round(en._num(i.get("platArea")), 1) or None,
            "far": round(en._num(i.get("vlRat")), 2) or None, "bcr": round(en._num(i.get("bcRat")), 2) or None,
            "buildings": int(en._num(i.get("mainBldCnt"))) or None,
            "use_apr": day("useAprDay"), "permit": day("pmsDay"),
        })
    return out


def update_registry(today: str, failed: list, budget: int | None = DONG_BUDGET) -> bool:
    """이번 분기에 아직 안 받은 법정동을 budget개까지 받는다(budget=None이면 전부 — 최초 구축용)."""
    q = quarter(today)
    done = json.loads(DONGS_FILE.read_text(encoding="utf-8")) if DONGS_FILE.exists() else {}
    todo = [c for c, _ in capital_dongs() if done.get(c) != q]
    todo = todo if budget is None else todo[:budget]
    if not todo:
        print(f"[노후단지] 건축물대장: {q} 법정동 {len(capital_dongs())}곳 모두 받음")
        return False
    print(f"[노후단지] 건축물대장: 법정동 {len(todo)}곳 조회 (남은 곳 {sum(done.get(c) != q for c, _ in capital_dongs())})")
    rows = {r["key"]: r for r in en._read_jsonl(REGISTRY_FILE)}
    errors = []

    def job(bjd):
        try:
            return bjd, _recap_dong(bjd)
        except en.ApiError as e:
            errors.append(f"{bjd}: {e}")
            return bjd, None

    changed = False
    with ThreadPoolExecutor(en.WORKERS) as ex:
        for bjd, got in ex.map(job, todo):
            if got is None:
                continue
            old = {k: r for k, r in rows.items() if r["bjd"] == bjd}
            for k in old:
                rows.pop(k)
            for r in got:  # 예전에 표제부로 채운 사용승인일은 유지
                if old.get(r["key"], {}).get("use_apr_title"):
                    r["use_apr_title"] = old[r["key"]]["use_apr_title"]
                rows[r["key"]] = r
            changed |= old != {r["key"]: r for r in got}
            done[bjd] = q
    en._write_jsonl(REGISTRY_FILE, (rows[k] for k in sorted(rows)))
    DONGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DONGS_FILE.write_text(json.dumps(done, sort_keys=True, indent=0) + "\n", encoding="utf-8", newline="\n")
    if errors:
        failed.append(f"노후단지 건축물대장 {len(errors)}곳 실패 (예: {errors[0]})")
    return changed or bool(todo)


def fill_years(today: str, failed: list, budget: int | None = YEAR_BUDGET) -> bool:
    """총괄표제부에 사용승인일이 없고 거래 기록에도 준공연도가 없는 단지는 동별 표제부에서 가장 이른 사용승인일을 찾는다."""
    rows = {r["key"]: r for r in en._read_jsonl(REGISTRY_FILE)}
    trade_years = {k for k, _ in _trade_groups()} | {r["key"] for r in en._read_jsonl(KAPT_FILE) if r.get("use_apr")}
    todo = [r for r in rows.values() if not r.get("use_apr") and "use_apr_title" not in r and r["key"] not in trade_years
            and r["households"] >= 100]
    todo = sorted(todo, key=lambda r: -r["households"])
    todo = todo if budget is None else todo[:budget]
    if not todo:
        return False
    print(f"[노후단지] 사용승인일 채우기 {len(todo)}곳")
    errors = []

    def job(r):
        bjd, plat, bun, ji = r["key"].split("-")
        lot = {"sigungu_cd": bjd[:5], "bjdong_cd": bjd[5:], "plat_gb": plat, "bun": bun, "ji": ji}
        try:
            titles = en._bld_items("getBrTitleInfo", lot)
        except en.ApiError as e:
            errors.append(f"{r['key']}: {e}")
            return r, None
        days = sorted(d for d in (str(t.get("useAprDay", "")).strip() for t in titles) if re.fullmatch(r"\d{8}", d))
        return r, (f"{days[0][:4]}-{days[0][4:6]}-{days[0][6:]}" if days else "")

    with ThreadPoolExecutor(en.WORKERS) as ex:
        for r, day in ex.map(job, todo):
            if day is not None:
                rows[r["key"]]["use_apr_title"] = day  # ""도 기록: 다시 묻지 않게
    en._write_jsonl(REGISTRY_FILE, (rows[k] for k in sorted(rows)))
    if errors:
        failed.append(f"노후단지 사용승인일 {len(errors)}곳 실패 (예: {errors[0]})")
    return True


# ---------------------------------------------------------------- 목록 만들기
def _trade_groups() -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for t in en._read_jsonl(en.TRADES_FILE):
        k = jibun_key(t["sgg"], t["dong"], t["jibun"])
        if k:
            groups.setdefault(k, []).append(t)
    return groups.items()


def build(today: str, projects: list[dict], lots: dict[str, dict]) -> list[dict]:
    """노후 단지 목록. projects: 내보내는 사업장 레코드, lots: 사업장 대표 지번(enrich.project_lots)."""
    cutoff = en.old_cutoff(today)
    reg = {r["key"]: r for r in en._read_jsonl(REGISTRY_FILE)}
    kapt = {r["key"]: r for r in en._read_jsonl(KAPT_FILE) if r.get("key")}
    trades = dict(_trade_groups())
    by_lot = {en.lot_key(l): pid for pid, l in lots.items()}
    proj = {p["project_id"]: p for p in projects}
    names = {c: n for c, n in en.bjd_table()}
    out = []
    for key in sorted(set(reg) | set(trades) | set(kapt)):
        r, ts, k = reg.get(key, {}), trades.get(key, []), kapt.get(key, {})
        # 준공연도: 실거래 건축년도·K-apt 사용승인일을 우선(건축물대장 사용승인일에는 '1937' 같은 오기가 있다).
        # 아파트는 1960년대부터 지어졌으므로 그 이전 값은 버린다.
        good_years = [t["built"] for t in ts if t.get("built") and t["built"] >= MIN_YEAR]
        good_years += [int(k["use_apr"][:4])] if k.get("use_apr") and int(k["use_apr"][:4]) >= MIN_YEAR else []
        reg_years = [y for y in (int(d[:4]) for d in (r.get("use_apr"), r.get("use_apr_title")) if d) if y >= MIN_YEAR]
        if not (good_years or reg_years):
            continue
        year = min(good_years) if good_years else min(reg_years)
        if year > cutoff:
            continue
        bjd, plat, bun, ji = key.split("-")
        parts = names.get(bjd, "").split()
        trade_names = Counter(t["apt"] for t in ts)
        pid = by_lot.get(key) or next((t["project_id"] for t in ts if t.get("project_id")), None)
        if pid and pid not in proj:
            pid = None
        name = k.get("name") or r.get("name") or (trade_names.most_common(1)[0][0] if trade_names else None)
        row = {
            "complex_id": key, "name": name or f"{parts[-1] if parts else ''} {int(bun)}",
            "name_variants": " | ".join(dict.fromkeys(x for x in [k.get("name"), r.get("name"), *trade_names] if x)),
            "region_sido": parts[0] if parts else None,
            "region_sigungu": " ".join(parts[1:-1]) if len(parts) > 2 else None,
            "dong": parts[-1] if parts else None,
            "jibun": ("산 " if plat == "1" else "") + str(int(bun)) + (f"-{int(ji)}" if int(ji) else ""),
            "built_year": year, "age": int(today[:4]) - year,
            "households": k.get("households") or r.get("households"),
            "buildings": k.get("buildings") or r.get("buildings"),
            "top_floor": k.get("top_floor"), "heating": k.get("heating"), "builder": k.get("builder"),
            "road_address": k.get("road_address"), "kapt_code": k.get("kapt_code"),
            "land_area_m2": r.get("land_area_m2"), "far": r.get("far"), "bcr": r.get("bcr"),
            "land_share_m2": round(r["land_area_m2"] / r["households"], 1)
            if r.get("land_area_m2") and r.get("households") else None,
            "project_id": pid, "stage": proj[pid].get("stage") if pid else None,
            "project_name": proj[pid].get("zone_name") if pid else None,
            "sources": " · ".join(s for s, ok in [("K-apt", bool(k)), ("건축물대장", bool(r)), ("실거래", bool(ts))] if ok),
        }
        if ts:
            row.update(en.trade_summary(ts))
        out.append(row)
    return out


def write(rows: list[dict], today: str) -> None:
    import pandas as pd
    keys = [k for k in rows[0] if k != "trades_by_area"] if rows else []
    pd.DataFrame(rows).reindex(columns=keys).to_csv(CSV_FILE, index=False, encoding="utf-8-sig", lineterminator="\n")
    site = []
    for r in rows:
        x = {k: cr.jsonable(v) for k, v in r.items() if k != "trades_by_area_text"}
        x["search_keys"] = " ".join(dict.fromkeys(
            cr.search_key(n) for n in re.split(r"\s*\|\s*", r.get("name_variants") or r["name"]) if n))
        site.append({k: v for k, v in x.items() if v not in (None, "", [])})
    data = {"generated_at": today, "count": len(site), "old_before": en.old_cutoff(today), "apartments": site}
    SITE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SITE_FILE.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                         encoding="utf-8", newline="\n")
    linked = sum(1 for r in rows if r.get("project_id"))
    print(f"[노후단지] {len(rows)}곳 (사업장 연결 {linked}곳) → {SITE_FILE.relative_to(cr.BASE)}, {CSV_FILE.name}")


def update(today: str, failed: list, dong_budget: int | None = DONG_BUDGET,
           year_budget: int | None = YEAR_BUDGET, kapt_budget: int | None = KAPT_BUDGET) -> bool:
    changed = update_registry(today, failed, dong_budget)
    changed = update_kapt(today, failed, kapt_budget) or changed
    return fill_years(today, failed, year_budget) or changed


# ---------------------------------------------------------------- K-apt (공동주택관리정보시스템)
KAPT_API = "https://apis.data.go.kr/1613000/"
KAPT_LIST_FILE = en.STATE / "complexes" / "kapt_list.jsonl"
KAPT_FILE = en.STATE / "complexes" / "kapt.jsonl"
KAPT_STATE_FILE = en.STATE / "complexes" / "kapt_state.json"


class KaptUnavailable(Exception):
    """인증키가 아직 K-apt 서비스에 등록되지 않음(활용신청 승인 전·반영 대기)."""


def _kapt(op: str, params: dict) -> tuple[list[dict], int]:
    """K-apt 응답(JSON/XML 어느 쪽이든) → (item 목록, totalCount)."""
    key = en.api_key()
    en._throttle()
    try:
        r = en._session().get(KAPT_API + op, params={"serviceKey": key, **params}, timeout=cr.TIMEOUT)
    except Exception as e:
        raise en.ApiError(str(e).replace(key or "\0", "***")) from None
    text = r.text.strip()
    if "SERVICE_KEY_IS_NOT_REGISTERED" in text:
        raise KaptUnavailable(op)
    if r.status_code != 200:
        raise en.ApiError(f"{op}: HTTP {r.status_code} {text[:120]}")
    if text.startswith("{"):
        body = r.json().get("response", {}).get("body", {})
        items = body.get("items")
        if isinstance(items, dict):
            items = items.get("item", [])
        if items is None and isinstance(body.get("item"), dict):  # 기본정보는 단일 item
            items = [body["item"]]
        items = items if isinstance(items, list) else ([items] if items else [])
        return items, int(body.get("totalCount") or len(items))
    root = en.ET.fromstring(r.content)
    code = root.findtext(".//resultCode")
    if code and code not in ("00", "000"):
        raise en.ApiError(f"{op}: {code} {root.findtext('.//resultMsg')}")
    items = [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]
    return items, int(root.findtext(".//totalCount") or len(items))


def update_kapt_list(today: str, failed: list) -> bool:
    """분기마다 수도권 시군구별 K-apt 단지 목록(단지코드·이름·법정동코드)을 새로 받는다."""
    st = json.loads(KAPT_STATE_FILE.read_text(encoding="utf-8")) if KAPT_STATE_FILE.exists() else {}
    if st.get("list_quarter") == quarter(today) and KAPT_LIST_FILE.exists():
        return False
    rows, errors = [], []
    for sgg in en.capital_sggs():
        page = 1
        while True:
            try:
                items, total = _kapt("AptListService4/getSigunguAptList4",
                                     {"sigunguCode": sgg, "numOfRows": "1000", "pageNo": str(page)})
            except en.ApiError as e:
                errors.append(f"{sgg}: {e}")
                break
            rows += [{"kapt_code": i.get("kaptCode"), "name": i.get("kaptName"), "bjd": i.get("bjdCode"),
                      "sgg": sgg} for i in items if i.get("kaptCode")]
            if page * 1000 >= total or not items:
                break
            page += 1
    if errors:
        failed.append(f"K-apt 단지 목록 {len(errors)}곳 실패 (예: {errors[0]})")
        if not rows:
            return False
    rows = sorted({r["kapt_code"]: r for r in rows}.values(), key=lambda r: r["kapt_code"])
    en._write_jsonl(KAPT_LIST_FILE, rows)
    if not errors:
        st["list_quarter"] = quarter(today)
    KAPT_STATE_FILE.write_text(json.dumps(st, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"[K-apt] 수도권 단지 목록 {len(rows)}곳")
    return True


def _kapt_key(bjd: str | None, addr: str | None) -> str | None:
    """'서울특별시 강남구 대치동 316 은마아파트' + 법정동코드 → 단지 키(법정동코드-산-본번-부번)."""
    if not bjd or len(str(bjd)) != 10 or not addr:
        return None
    m = re.search(r"(?:동|가|리)\s*(산)?\s*(\d+)(?:-(\d+))?(?=\s|$|[^\d-])", str(addr))
    return complex_key(str(bjd), int(m.group(2)), int(m.group(3) or 0), "1" if m.group(1) else "0") if m else None


def update_kapt_info(today: str, failed: list, budget: int | None = KAPT_BUDGET) -> bool:
    """기본정보(사용승인일·세대수·동수·최고층·난방·시공사·주소)를 아직 없는 단지만 받는다."""
    have = {r["kapt_code"]: r for r in en._read_jsonl(KAPT_FILE)}
    todo = [r for r in en._read_jsonl(KAPT_LIST_FILE) if r["kapt_code"] not in have]
    todo = todo if budget is None else todo[:budget]
    if not todo:
        return False
    print(f"[K-apt] 기본정보 {len(todo)}곳 조회")
    errors = []

    def job(r):
        try:
            items, _ = _kapt("AptBasisInfoServiceV5/getAphusBassInfoV5", {"kaptCode": r["kapt_code"]})
        except en.ApiError as e:
            errors.append(f"{r['kapt_code']}: {e}")
            return None
        i = items[0] if items else {}
        day = str(i.get("kaptUsedate") or "").strip()
        bjd = str(i.get("bjdCode") or r.get("bjd") or "")
        return {"kapt_code": r["kapt_code"], "name": i.get("kaptName") or r.get("name"), "bjd": bjd,
                "address": i.get("kaptAddr"), "road_address": i.get("doroJuso"),
                "use_apr": f"{day[:4]}-{day[4:6]}-{day[6:8]}" if re.fullmatch(r"\d{8}", day) else None,
                "households": int(en._num(i.get("kaptdaCnt"))) or None,
                "buildings": int(en._num(i.get("kaptDongCnt"))) or None,
                "top_floor": int(en._num(i.get("kaptTopFloor"))) or None,
                "heating": i.get("codeHeatNm") or None, "builder": i.get("kaptBcompany") or None,
                "key": _kapt_key(bjd, i.get("kaptAddr")), "fetched": today[:7]}

    with ThreadPoolExecutor(en.WORKERS) as ex:
        for rec in ex.map(job, todo):
            if rec:
                have[rec["kapt_code"]] = rec
    en._write_jsonl(KAPT_FILE, (have[k] for k in sorted(have)))
    if errors:
        failed.append(f"K-apt 기본정보 {len(errors)}곳 실패 (예: {errors[0]})")
    return True


def update_kapt(today: str, failed: list, budget: int | None = KAPT_BUDGET) -> bool:
    try:
        changed = update_kapt_list(today, failed)
        return update_kapt_info(today, failed, budget) or changed
    except KaptUnavailable:
        print("[K-apt] 인증키가 아직 K-apt API에 등록되지 않음 — 건너뜀")
        return False
