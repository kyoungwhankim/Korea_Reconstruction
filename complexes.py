"""수도권 30년 이상 노후 아파트 단지 목록.

재건축 사업장(collect_reconstruction / update_daily)과 별개로, 아직 사업이 시작되지 않은 단지까지
'재건축 가능성이 있는 노후 단지'를 모은다. 단지는 '법정동코드 + 지번'으로 구분한다.

  - 건축물대장 총괄표제부를 법정동마다 나열 → 단지명·세대수·대지면적·용적률·건폐율·사용승인일
    (분기마다 한 바퀴: 매일 DONG_BUDGET개 법정동씩 새로 받는다)
  - 아파트 매매 실거래가(enrich.py가 수도권 전체를 받아 둔 것) → 준공연도·거래가
  - 준공연도가 어디에도 없는 큰 단지는 동별 표제부로 사용승인일을 채운다(매일 YEAR_BUDGET개까지)
  - K-apt(공동주택관리정보시스템) 단지 목록(분기)·기본정보(단지별 1회) → 공식 단지명·사용승인일·세대수·동수·최고층·난방·시공사
  - 부동산원 단지 식별정보(identity.py) → 단지 구분의 기준, 이름 3개·옛 이름, 동 목록, 세대수, 사용승인일
"""
from __future__ import annotations

import json
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import collect_reconstruction as cr
import enrich as en
import identity
from identity import Index, complex_key, jibun_key  # noqa: F401

REGISTRY_FILE = en.STATE / "complexes" / "registry.jsonl"
DONGS_FILE = en.STATE / "complexes" / "registry_dongs.json"
SITE_FILE = cr.BASE / "docs" / "data" / "apartments.json"
CSV_FILE = cr.OUT / "old_apartments_latest.csv"
MIN_HOUSEHOLDS = 20   # 이보다 작은 공동주택(소규모 다세대 등)은 뺀다
DONG_BUDGET = 100     # 하루에 새로 받는 법정동 수 (≈2,900곳 → 약 한 달에 한 바퀴, 분기마다 반복)
YEAR_BUDGET = 150     # 하루에 사용승인일을 채우는 단지 수
MIN_YEAR = 1960      # 이보다 이른 준공연도는 오기로 본다
KAPT_BUDGET = 3000   # 하루에 K-apt 기본정보를 새로 받는 단지 수(서비스별 일 한도 5,000건, 초당 5건 → 약 10분)


# ---------------------------------------------------------------- 법정동
@lru_cache(maxsize=1)
def capital_dongs() -> tuple[tuple[str, str], ...]:
    """건축물대장을 조회할 수도권 법정동(동·리). 아래에 리가 있는 읍·면 코드는 빼고 리를 쓴다."""
    rows = [(c, n) for c, n in en.bjd_table() if c[:2] in ("11", "28", "41")]
    has_ri = {c[:8] for c, _ in rows if c[8:] != "00"}
    return tuple((c, n) for c, n in rows if not (c[8:] == "00" and c[:8] in has_ri))


def quarter(today: str) -> str:
    return identity.quarter(today)


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
    trade_years = {k for k, _ in _trade_groups()} | {r["key"] for r in en._read_jsonl(KAPT_FILE) if r.get("use_apr")} \
        | {r["lot"] for r in en._read_jsonl(identity.REB_FILE) if r.get("use_apr")}  # 부동산원이 아는 지번은 제외
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
def _trade_groups():
    """지번 키 → 거래 목록."""
    groups: dict[str, list[dict]] = {}
    for t in en._read_jsonl(en.TRADES_FILE):
        k = jibun_key(t["sgg"], t["dong"], t["jibun"])
        if k:
            groups.setdefault(k, []).append(t)
    return groups.items()


_SUFFIXED = re.compile(r"(아파트|APT|맨션|맨숀|빌라|타운|하우스|파크|빌|캐슬|팰리스|자이|힐스|뷰|시티|프라자|연립)$", re.I)


def _clean(n) -> str:
    """부동산원 이름의 '은마아파트(29동)' 같은 꼬리를 뗀다."""
    return re.sub(r"\s*\(\d+\s*동\)\s*$", "", str(n or "")).strip()


def unit_alias(nm1: str, dong_numbers: set[str], kapt_name: str | None) -> str | None:
    """'가야주공' + 동 501~516 + K-apt '가야1차' → '가야주공5단지1차'.

    1기 신도시 등 대단지 주공은 동 번호의 백 자리가 단지 번호인 관행이 뚜렷하다(501동 = 5단지).
    101~1xx동은 보통 단지와 무관한 일반 번호라 백 자리 1은 쓰지 않는다. 추정값이므로 별칭 종류를 '동번호 추정'으로 둔다.
    """
    if "주공" not in nm1 or re.search(r"\d+\s*단지", nm1):
        return None
    nums = sorted(int(n) for n in dong_numbers if n.isdigit())
    if len(nums) < 2 or nums[-1] - nums[0] >= 100:
        return None
    hund = {n // 100 for n in nums}
    if len(hund) != 1:
        return None
    h = next(iter(hund))
    if not 2 <= h <= 20:
        return None
    base = re.sub(r"\d+(?=주공)", "", re.sub(r"\(.*?\)", "", nm1)).strip()
    paren = "".join(re.findall(r"\(.*?\)", nm1))
    m = re.search(r"(\d+)\s*차", kapt_name or "")
    return f"{base}{h}단지{m.group(1) + '차' if m else ''}{paren}"


def complex_names(reb_names: list[str], hist_names: list[str], kapt_name: str | None,
                  trade_names: list[str], reg_name: str | None, dong_numbers: set[str]) -> tuple[str, list[tuple[str, str]]]:
    """(보여줄 이름, [(별칭, 종류)]) — 'N단지'·'N차'가 들어간 이름을 우선 보여준다."""
    nm1 = _clean(reb_names[0]) if reb_names else None
    guess = unit_alias(nm1, dong_numbers, kapt_name) if nm1 else None
    # 보여줄 이름: ① 동 번호로 추정한 'N단지N차'(차수까지 있을 때) ② 'N단지'·'N차'가 든 이름 ③ 부동산원 1순위 이름
    ordered = ([guess] if guess and re.search(r"\d+\s*차", guess) else []) + [_clean(n) for n in reb_names[1:]] + [kapt_name]
    unitish = [n for n in ordered if n and re.search(r"\d+\s*(단지|차)", n)]
    display = unitish[0] if unitish else (nm1 or kapt_name or (trade_names or [reg_name or "?"])[0])
    if display == nm1 and nm1 and not _SUFFIXED.search(nm1) and not re.search(r"\(.*\)$", nm1):
        display = nm1 + "아파트"
    aliases: dict[str, str] = {}

    def add(n, kind):
        n = _clean(n)
        if n and n not in aliases:
            aliases[n] = kind
    for n in reb_names:
        add(n, "부동산원")
    for n in hist_names:
        add(n, "옛 이름")
    if kapt_name:
        add(kapt_name, "K-apt")
    for n in trade_names:
        add(n, "실거래")
    if reg_name:
        add(reg_name, "건축물대장")
    if guess:
        add(guess, "동번호 추정")
        if kapt_name and re.search(r"\d+\s*차", kapt_name):
            add(re.sub(r"\d+\s*단지", "", guess), "동번호 추정")  # '가야주공1차'
    base_names = list(aliases)
    for n in base_names:  # 표기 변형: 띄어쓰기, '아파트' 붙이기/떼기
        add(n.replace(" ", ""), "표기 변형")
        if not _SUFFIXED.search(n) and not re.search(r"\(.*\)$", n):
            add(n + "아파트", "표기 변형")
    return display, list(aliases.items())


def build(today: str, projects: list[dict], lots: dict[str, dict], book=None) -> list[dict]:
    """노후 단지 목록. 단지 1개 = 부동산원 단지(아파트). 부동산원에 없는 지번은 지번 단위 항목으로 남긴다.
    projects: 내보내는 사업장 레코드, lots: 사업장 대표 지번(enrich.project_lots), book: 거래 장부(tradebook.Book)."""
    import tradebook
    cutoff = en.old_cutoff(today)
    idx = Index()
    book = book or tradebook.Book(today, lots, {}, {p["project_id"]: p for p in projects})
    reg = {r["key"]: r for r in en._read_jsonl(REGISTRY_FILE)}
    kapt_by_lot: dict[str, list[dict]] = {}
    for k in en._read_jsonl(KAPT_FILE):
        if k.get("key"):
            kapt_by_lot.setdefault(k["key"], []).append(k)
    trades_cx: dict[str, list[dict]] = {}
    trades_lot: dict[str, list[dict]] = {}   # 단지를 못 정한 거래(지번 공통) — 사업장·준공연도 판단용(최근 12개월)
    for t in en._read_jsonl(en.TRADES_FILE):
        lot = jibun_key(t["sgg"], t["dong"], t["jibun"])
        if not lot:
            continue
        if t.get("complex_id"):
            trades_cx.setdefault(t["complex_id"], []).append(t)
        else:
            trades_lot.setdefault(lot, []).append(t)
    proj_lot = {en.lot_key(l): pid for pid, l in lots.items()}
    proj = {p["project_id"]: p for p in projects}
    bjd_names = {c: n for c, n in en.bjd_table()}

    entries: list[tuple[str, dict | None, str]] = [(r["pk"], r, r["lot"]) for r in idx.by_pk.values()
                                                   if r["type"] == "아파트" and r["lot"]]
    reb_lots = set(idx.by_lot)
    for lot in sorted(set(reg) | set(trades_lot)):  # 부동산원에 없는 지번(소규모·누락) → 지번 단위 항목
        if lot not in reb_lots:
            entries.append((f"lot:{lot}", None, lot))

    out = []
    for cid, r, lot in entries:
        siblings = idx.apartments_on(lot)
        regrow = reg.get(lot, {})
        ts = list(trades_cx.get(cid, []))
        shared = trades_lot.get(lot, [])
        if r is None:
            ts += shared
        # K-apt: 같은 지번의 등록 단지 중 이름이 맞는 것
        kapt = None
        for k in kapt_by_lot.get(lot, []):
            if (r is None and len(kapt_by_lot[lot]) == 1) or (r is not None and idx.match_name(lot, k["name"]) == cid):
                kapt = k
                break
        # 준공연도: 부동산원 사용승인일 > K-apt > 실거래 건축년도 > 건축물대장
        year = None
        for cand in ([r.get("use_apr")] if r else []) + ([kapt.get("use_apr")] if kapt else []):
            if cand and int(cand[:4]) >= MIN_YEAR:
                year = int(cand[:4])
                break
        if year is None:
            tyears = [t["built"] for t in ts if t.get("built") and t["built"] >= MIN_YEAR]
            ryears = [int(d[:4]) for d in (regrow.get("use_apr"), regrow.get("use_apr_title")) if d and int(d[:4]) >= MIN_YEAR]
            year = min(tyears) if tyears else (min(ryears) if ryears else None)
        if year is None or year > cutoff:
            continue
        units = (r or {}).get("units") or (kapt or {}).get("households") or (regrow.get("households") if len(siblings) <= 1 else None)
        if units is not None and units < MIN_HOUSEHOLDS:
            continue
        bjd, plat, bun, ji = lot.split("-")
        parts = bjd_names.get(bjd, "").split()
        if len(parts) < 2:  # 폐지된 법정동 코드 등 — 지역을 알 수 없으면 뺀다
            continue
        trade_names = [n for n, _ in Counter(t["apt"] for t in ts).most_common()]
        hist = [h.get(k) for h in idx.extra.get(cid, {}).get("hist", []) for k in ("old", "new")]
        name, aliases = complex_names((r or {}).get("names", []), [h for h in hist if h], (kapt or {}).get("name"),
                                      trade_names, regrow.get("name"), idx.dong_numbers(cid))
        sib_units = [s.get("units") for s in siblings]
        land = regrow.get("land_area_m2")
        land_share = round(land / sum(sib_units), 1) if land and siblings and all(sib_units) else \
            (round(land / units, 1) if land and units and not siblings else None)
        pid = proj_lot.get(lot) or next((t["project_id"] for t in ts if t.get("project_id")), None)
        if pid and pid not in proj:
            pid = None
        row = {
            "complex_id": cid, "name": name, "aliases": [a for a, _ in aliases],
            "name_variants": " | ".join(dict.fromkeys([name, *(a for a, k in aliases if k != "표기 변형")])),
            "region_sido": parts[0] if parts else None,
            "region_sigungu": " ".join(parts[1:-1]) if len(parts) > 2 else None,
            "dong": parts[-1] if parts else None,
            "jibun": ("산 " if plat == "1" else "") + str(int(bun)) + (f"-{int(ji)}" if int(ji) else ""),
            "lot": lot, "siblings": len(siblings) if r is not None else 0,
            "sibling_names": " | ".join(_clean(s["names"][0]) for s in siblings if s["pk"] != cid and s.get("names")) or None,
            "dong_range": idx.dong_range(cid) if r is not None else None,
            "road_address": (r or {}).get("road_address") or (kapt or {}).get("road_address"),
            "built_year": year, "age": int(today[:4]) - year,
            "households": units, "buildings": (r or {}).get("dong_cnt") or (kapt or {}).get("buildings") or regrow.get("buildings"),
            "land_area_m2": land, "land_share_m2": land_share,
            "far": regrow.get("far"), "bcr": regrow.get("bcr"),
            "top_floor": (kapt or {}).get("top_floor"), "heating": (kapt or {}).get("heating"),
            "builder": (kapt or {}).get("builder"), "kapt_code": (kapt or {}).get("kapt_code"),
            "reb_pk": (r or {}).get("pk"),
            "project_id": pid, "stage": proj[pid].get("stage") if pid else None,
            "project_name": proj[pid].get("zone_name") if pid else None,
            "sources": " · ".join(s for s, ok in [("부동산원", r is not None), ("K-apt", bool(kapt)),
                                                 ("건축물대장", bool(regrow)), ("실거래", bool(ts))] if ok),
            "shared_trades": len(shared) if r is not None and siblings and len(siblings) > 1 else 0,
        }
        # 거래: 12개월 매매 요약 + 2006년~ 이력 건수 + 전월세(전세가율). 건별 목록은 단지별 파일(tradebook.write_files)
        row.update(book.complex_fields(cid, lot, r is not None))
        out.append(row)
    return out


SITE_SKIP = ("trades_by_area_text", "trades_by_area", "rents_by_area")  # 면적대별 표는 단지별 파일에만


def write(rows: list[dict], today: str) -> None:
    import pandas as pd
    keys = [k for k in rows[0] if k not in ("trades_by_area", "rents_by_area", "aliases", "trades")] if rows else []
    pd.DataFrame(rows).reindex(columns=keys).to_csv(CSV_FILE, index=False, encoding="utf-8-sig", lineterminator="\n")
    site = []
    for r in rows:
        x = {k: cr.jsonable(v) for k, v in r.items() if k not in SITE_SKIP}
        x["search_keys"] = " ".join(dict.fromkeys(k for k in (cr.search_key(n) for n in [r["name"], *r["aliases"]]) if k))
        site.append({k: v for k, v in x.items() if v not in (None, "", [], 0) or k in ("siblings", "shared_trades")})
    data = {"generated_at": today, "count": len(site), "old_before": en.old_cutoff(today), "apartments": site}
    SITE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SITE_FILE.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                         encoding="utf-8", newline="\n")
    linked = sum(1 for r in rows if r.get("project_id"))
    print(f"[노후단지] {len(rows)}곳 (사업장 연결 {linked}곳, 부동산원 기준 {sum(1 for r in rows if r.get('reb_pk'))}곳)"
          f" → {SITE_FILE.relative_to(cr.BASE)}, {CSV_FILE.name}")


def update(today: str, failed: list, project_lots=frozenset(), dong_budget: int | None = DONG_BUDGET,
           year_budget: int | None = YEAR_BUDGET, kapt_budget: int | None = KAPT_BUDGET,
           reb_dong_budget: int | None = identity.DONG_BUDGET) -> bool:
    changed = identity.update(today, failed, set(project_lots), reb_dong_budget)
    changed = update_registry(today, failed, dong_budget) or changed
    changed = update_kapt(today, failed, kapt_budget) or changed
    return fill_years(today, failed, year_budget) or changed


# ---------------------------------------------------------------- K-apt (공동주택관리정보시스템)
KAPT_API = "https://apis.data.go.kr/1613000/"
KAPT_LIST_FILE = en.STATE / "complexes" / "kapt_list.jsonl"
KAPT_FILE = en.STATE / "complexes" / "kapt.jsonl"
KAPT_STATE_FILE = en.STATE / "complexes" / "kapt_state.json"
_kapt_local = threading.local()


class KaptUnavailable(Exception):
    """인증키가 아직 K-apt 서비스에 등록되지 않음(활용신청 승인 전·반영 대기)."""


class KaptQuotaExceeded(Exception):
    """K-apt 일일 호출 한도(개발계정 서비스별 5,000건) 소진 — 오늘은 더 호출하지 않는다."""


def _kapt_session():
    """429(한도 초과)는 재시도해도 소용없으므로 5xx만 재시도하는 전용 세션."""
    if not hasattr(_kapt_local, "s"):
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        s = cr.make_session()
        s.mount("https://", HTTPAdapter(max_retries=Retry(
            total=3, connect=1, read=1, status=3, backoff_factor=2, status_forcelist=(500, 502, 503, 504),
            allowed_methods=None, raise_on_status=False)))
        _kapt_local.s = s
    return _kapt_local.s


def _kapt(op: str, params: dict) -> tuple[list[dict], int]:
    """K-apt 응답(JSON/XML 어느 쪽이든) → (item 목록, totalCount). data.go.kr 공통 오류 본문은 HTTP 200으로도 온다."""
    key = en.api_key()
    en._throttle()
    try:
        r = _kapt_session().get(KAPT_API + op, params={"serviceKey": key, **params}, timeout=cr.TIMEOUT)
    except Exception as e:
        raise en.ApiError(str(e).replace(key or "\0", "***")) from None
    text = r.text.strip()
    err = re.search(r'"errMsg"\s*:\s*"([^"]+)"|<errMsg>([^<]+)</errMsg>', text)
    if err or r.status_code == 429:
        msg = (err.group(1) or err.group(2)) if err else "HTTP 429"
        rc = re.search(r'"returnReasonCode"\s*:\s*"(\d+)"|<returnReasonCode>(\d+)<', text)
        rc = (rc.group(1) or rc.group(2)) if rc else ""
        if "LIMITED_NUMBER" in msg or rc == "22" or r.status_code == 429:
            raise KaptQuotaExceeded(f"{op}: {msg}")
        if "NOT_REGISTERED" in msg or rc == "30":
            raise KaptUnavailable(f"{op}: {msg}")
        raise en.ApiError(f"{op}: {rc} {msg}")
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


def _kapt_state() -> dict:
    return json.loads(KAPT_STATE_FILE.read_text(encoding="utf-8")) if KAPT_STATE_FILE.exists() else {}


def _save_kapt_state(st: dict) -> None:
    KAPT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    KAPT_STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
                               encoding="utf-8", newline="\n")


def update_kapt_list(today: str, failed: list) -> bool:
    """분기마다 수도권 시군구별 K-apt 단지 목록(단지코드·이름·법정동코드)을 받는다. 시군구 단위로 완료를 기록해
    한도에 걸리면 다음 날 이어서 받는다."""
    st = _kapt_state()
    done = st.setdefault("list", {})
    todo = [s for s in en.capital_sggs() if done.get(s) != quarter(today)]
    if not todo:
        return False
    rows = {r["kapt_code"]: r for r in en._read_jsonl(KAPT_LIST_FILE)}
    errors, got = [], 0
    try:
        for sgg in todo:
            page, fresh = 1, []
            while True:
                try:
                    items, total = _kapt("AptListService4/getSigunguAptList4",
                                         {"sigunguCode": sgg, "numOfRows": "1000", "pageNo": str(page)})
                except en.ApiError as e:
                    errors.append(f"{sgg}: {e}")
                    fresh = None
                    break
                fresh += [{"kapt_code": i.get("kaptCode"), "name": i.get("kaptName"), "bjd": i.get("bjdCode"),
                           "sgg": sgg} for i in items if i.get("kaptCode")]
                if page * 1000 >= total or not items:
                    break
                page += 1
            if fresh is None:
                continue
            for k in [k for k, r in rows.items() if r.get("sgg") == sgg]:  # 그 시군구의 이전 행은 새 결과로 교체
                rows.pop(k)
            for r in fresh:
                rows[r["kapt_code"]] = r
            done[sgg] = quarter(today)
            got += 1
    except KaptQuotaExceeded as e:
        print(f"[K-apt] 단지 목록: 일일 한도 도달 — 내일 이어서 ({e})")
    en._write_jsonl(KAPT_LIST_FILE, (rows[k] for k in sorted(rows)))
    _save_kapt_state(st)
    if errors:
        failed.append(f"K-apt 단지 목록 {len(errors)}곳 실패 (예: {errors[0]})")
    print(f"[K-apt] 수도권 단지 목록 {len(rows)}곳 (이번에 {got}개 시군구 갱신, 남은 곳 {len(todo) - got})")
    return got > 0


def _kapt_key(bjd: str | None, addr: str | None) -> str | None:
    """'서울특별시 강남구 대치동 316 은마아파트' + 법정동코드 → 지번 키(법정동코드-산-본번-부번)."""
    if not bjd or len(str(bjd)) != 10 or not addr:
        return None
    m = re.search(r"(?:동|가|리)\s*(산)?\s*(\d+)(?:-(\d+))?(?=\s|$|[^\d-])", str(addr))
    return complex_key(str(bjd), int(m.group(2)), int(m.group(3) or 0), "1" if m.group(1) else "0") if m else None


def update_kapt_info(today: str, failed: list, budget: int | None = KAPT_BUDGET) -> bool:
    """기본정보(사용승인일·세대수·동수·최고층·난방·시공사·주소)를 아직 없는 단지만, 하루 budget개까지 받는다.
    한도에 걸리면 받은 것까지 저장하고 멈춘다."""
    have = {r["kapt_code"]: r for r in en._read_jsonl(KAPT_FILE)}
    todo = [r for r in en._read_jsonl(KAPT_LIST_FILE) if r["kapt_code"] not in have]
    todo = todo if budget is None else todo[:budget]
    if not todo:
        return False
    print(f"[K-apt] 기본정보 {len(todo)}곳 조회")
    errors: list[str] = []
    stop = threading.Event()

    def job(r):
        if stop.is_set():
            return None
        try:
            items, _ = _kapt("AptBasisInfoServiceV5/getAphusBassInfoV5", {"kaptCode": r["kapt_code"]})
        except KaptQuotaExceeded:
            stop.set()
            return None
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
                "units_60": int(en._num(i.get("kaptMparea60"))) or None,
                "units_85": int(en._num(i.get("kaptMparea85"))) or None,
                "units_135": int(en._num(i.get("kaptMparea135"))) or None,
                "units_136": int(en._num(i.get("kaptMparea136"))) or None,
                "key": _kapt_key(bjd, i.get("kaptAddr")), "fetched": today[:7]}

    got = 0
    with ThreadPoolExecutor(en.WORKERS) as ex:
        for rec in ex.map(job, todo):
            if rec:
                have[rec["kapt_code"]] = rec
                got += 1
    en._write_jsonl(KAPT_FILE, (have[k] for k in sorted(have)))
    if stop.is_set():
        print(f"[K-apt] 기본정보: 일일 한도 도달 — {got}곳 저장, 내일 이어서")
    if errors:
        failed.append(f"K-apt 기본정보 {len(errors)}곳 실패 (예: {errors[0]})")
    return got > 0


def update_kapt(today: str, failed: list, budget: int | None = KAPT_BUDGET) -> bool:
    try:
        changed = update_kapt_list(today, failed)
        return update_kapt_info(today, failed, budget) or changed
    except KaptUnavailable:
        print("[K-apt] 인증키가 아직 K-apt API에 등록되지 않음 — 건너뜀")
        return False
    except KaptQuotaExceeded as e:
        print(f"[K-apt] 일일 한도 도달 — 내일 이어서 ({e})")
        return False
