"""사업장 보강: 건축물대장(대지지분·현재 용적률/건폐율)과 아파트 실거래가(매매·전월세·분양권/입주권).

모두 data.go.kr 오픈API라 인증키(환경변수 DATA_GO_KR_KEY, 로컬은 .env)가 필요하다.
건축물대장은 '법정동코드 + 지번'으로 조회하므로 먼저 사업장 주소를 그 형태로 바꾼다. 실거래 API는 시군구·월 단위로 받는다.

  state/trades.jsonl                 매매 최근 12개월(최근 3개월은 매일 다시 받음) — 사업장·단지 연결 포함
  state/trades_history/<시군구>.jsonl 매매 이력 2006-01 ~ 12개월 창 직전 달 (일 예산 안에서 며칠에 나눠 받음)
  state/trades_history_state.json    이력 진행 위치: 시군구별로 받은 달 범위, 오늘 쓴 호출 수
  state/rents.jsonl                  전월세 최근 12개월
  state/silv_trades.jsonl            분양권·입주권 최근 36개월 — 관리처분인가 이후 사업장에 연결된 것만
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
def bjd_table_all() -> list[tuple[str, str, str]]:
    """[(10자리 법정동코드, '서울특별시 강남구 대치동', '존재'|'폐지')] — 시도·시군구 단위 코드 포함."""
    b, _ = cr.datagokr_file("15123287")  # 국토교통부_법정동코드
    d = cr.read_csv_any(b)
    return list(zip(d["법정동코드"], d["법정동명"], d["폐지여부"]))


@lru_cache(maxsize=1)
def bjd_table() -> list[tuple[str, str]]:
    """[(10자리 법정동코드, '서울특별시 강남구 대치동')] — 현재 존재하는 동·리만."""
    return [(c, n) for c, n, st in bjd_table_all() if st == "존재" and not c.endswith("00000")]


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
RENTS_FILE = STATE / "rents.jsonl"
RENT_MONTHS_FILE = STATE / "rent_months.json"
SILV_FILE = STATE / "silv_trades.jsonl"
SILV_MONTHS_FILE = STATE / "silv_months.json"
HISTORY_DIR = STATE / "trades_history"
HISTORY_STATE_FILE = STATE / "trades_history_state.json"
MANUAL_NEW_NAMES = cr.BASE / "manual_new_names.csv"  # 사업장 → 신축 단지명(수기): 입주권·분양권 거래 연결용
BLD_API = "https://apis.data.go.kr/1613000/BldRgstHubService/"
TRADE_API = "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade"
RENT_API = "https://apis.data.go.kr/1613000/RTMSDataSvcAptRent/getRTMSDataSvcAptRent"
SILV_API = "https://apis.data.go.kr/1613000/RTMSDataSvcSilvTrade/getRTMSDataSvcSilvTrade"
TRADE_WINDOW = 12   # 매매·전월세: 최근 12개월 거래를 유지
TRADE_REFRESH = 3   # 신고 기한(30일)이 있어 최근 3개월은 매일 다시 받는다
SILV_WINDOW = 36    # 분양권·입주권: 관리처분인가 뒤 몇 년씩 거래되므로 3년
HISTORY_START = "200601"   # 매매 이력 시작 달(국토부 공개 범위)
HISTORY_MARGIN = 1         # 이력은 노후 기준보다 1년 뒤 준공까지 받아 둔다(해가 바뀌어도 바로 보이게; 그 뒤 여유 있게 다시 받음)
HISTORY_DAILY_CAP = 8000   # 하루 이력 호출 상한 — 매매 API 일 한도 10,000건에서 12개월 창(하루 2회 실행 × 약 300건)을 뺀 값
RTMS_PAGE = 2000    # 실거래 API 한 쪽 행 수(허용됨) — 한 달치가 대부분 한 번에 온다
WORKERS = 3         # 동시 요청 수
MAX_RPS = 5         # 초당 요청 상한 — 넘으면 data.go.kr이 429(Too Many Requests)를 돌려준다
_local = threading.local()
_rate_lock = threading.Lock()
_next_slot = [0.0]


class ApiError(Exception):
    pass


class QuotaExceeded(ApiError):
    """data.go.kr 일일 트래픽 초과(returnReasonCode 22) — 오늘은 그 서비스를 더 부르지 않는다."""


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
    except Exception as e:
        raise ApiError(str(e).replace(key or "\0", "***")) from None
    _check_gateway_error(r)
    return r


def _check_gateway_error(r) -> None:
    """data.go.kr 게이트웨이 오류(한도 초과·미등록 키 등)는 HTTP 200에 <OpenAPI_ServiceResponse> 본문으로 온다."""
    head = r.content[:400]
    if b"OpenAPI_ServiceResponse" not in head and b"returnReasonCode" not in head:
        return
    m = re.search(rb"<returnReasonCode>(\d+)</returnReasonCode>", r.content)
    msg = re.search(rb"<returnAuthMsg>([^<]*)</returnAuthMsg>", r.content)
    code = m.group(1).decode() if m else "?"
    text = msg.group(1).decode("utf-8", "replace") if msg else ""
    if code == "22" or "LIMITED_NUMBER" in text:
        raise QuotaExceeded(f"일일 한도 초과 ({text or code})")
    raise ApiError(f"게이트웨이 오류 {code} {text}")


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
    for pid, names in new_names().items():  # 신축 단지명(수기) — 입주권·분양권, 준공 후 매매가 이 이름으로 신고된다
        if pid in out:
            out[pid] |= names
    return out


@lru_cache(maxsize=1)
def new_names() -> dict[str, set[str]]:
    """manual_new_names.csv: project_id,new_name — 공공 출처에 없는 신축 단지명(예: 둔촌주공 → 올림픽파크포레온)."""
    out: dict[str, set[str]] = {}
    if not MANUAL_NEW_NAMES.exists():
        return out
    import csv
    with open(MANUAL_NEW_NAMES, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            pid, name = (row.get("project_id") or "").strip(), (row.get("new_name") or "").strip()
            if pid and name and cr.zone_key(name):
                out.setdefault(pid, set()).add(cr.zone_key(name))
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


def _rtms_items(url: str, label: str, sgg: str, ym: str) -> tuple[list[dict], int]:
    """실거래 API(매매·전월세·분양권 공통): 시군구·월의 모든 item → ([{태그: 값}], 호출 수)."""
    items, page = [], 1
    while True:
        r = _get(url, {"LAWD_CD": sgg, "DEAL_YMD": ym, "numOfRows": str(RTMS_PAGE), "pageNo": str(page)})
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            raise ApiError(f"{label} {sgg} {ym}: XML이 아닌 응답 ({r.text[:100]})") from None
        code = root.findtext(".//resultCode")
        if code not in ("000", "00"):
            raise ApiError(f"{label} {sgg} {ym}: {code} {root.findtext('.//resultMsg')}")
        got = [{c.tag: (c.text or "").strip() for c in it} for it in root.findall(".//item")]
        items += got
        total = int(root.findtext(".//totalCount") or 0)
        if page * RTMS_PAGE >= total or not got:
            return items, page
        page += 1


def _deal_date(g: dict) -> str:
    return f"{g.get('dealYear')}-{int(_num(g.get('dealMonth'))):02d}-{int(_num(g.get('dealDay'))):02d}"


def _place(g: dict, sgg: str) -> dict:
    return {"sgg": g.get("sggCd") or sgg, "dong": g.get("umdNm"), "jibun": g.get("jibun"), "apt": g.get("aptNm"),
            "area": round(_num(g.get("excluUseAr")), 2), "floor": int(_num(g.get("floor"))),
            "date": _deal_date(g), "built": int(_num(g.get("buildYear"))) or None}


def _sale_row(g: dict, sgg: str) -> dict:
    return {**_place(g, sgg), "price": int(_num(g.get("dealAmount"))),
            "dong_no": (g.get("aptDong") or "").strip() or None,
            "deal_type": (g.get("dealingGbn") or "").strip() or None}


def fetch_trades(sgg: str, ym: str) -> list[dict]:
    """아파트 매매 실거래(해제된 거래 제외). 가격 단위 만원."""
    items, _ = _rtms_items(TRADE_API, "실거래", sgg, ym)
    return [_sale_row(g, sgg) for g in items if g.get("cdealType") != "O"]


def fetch_trades_counted(sgg: str, ym: str) -> tuple[list[dict], int]:
    items, calls = _rtms_items(TRADE_API, "실거래", sgg, ym)
    return [_sale_row(g, sgg) for g in items if g.get("cdealType") != "O"], calls


def fetch_rents(sgg: str, ym: str) -> list[dict]:
    """아파트 전월세 실거래. deposit(보증금)·monthly(월세) 단위 만원, monthly 0이면 전세.
    contract: 신규/갱신(2021-06 이후 신고분), renewal_right: 계약갱신요구권 사용, term: 계약기간('24.01~26.01')."""
    items, _ = _rtms_items(RENT_API, "전월세", sgg, ym)
    rows = []
    for g in items:
        monthly = int(_num(g.get("monthlyRent")))
        rows.append({**_place(g, sgg), "deposit": int(_num(g.get("deposit"))), "monthly": monthly,
                     "kind": "전세" if monthly == 0 else "월세",
                     "contract": (g.get("contractType") or "").strip() or None,
                     "renewal_right": True if (g.get("useRRRight") or "").strip() else None,
                     "term": (g.get("contractTerm") or "").strip() or None,
                     "pre_deposit": int(_num(g.get("preDeposit"))) or None,
                     "pre_monthly": int(_num(g.get("preMonthlyRent"))) or None})
    return rows


def fetch_silv(sgg: str, ym: str) -> list[dict]:
    """아파트 분양권·입주권 전매 실거래(해제 제외). ownershipGbn이 '입'이면 입주권, 비어 있으면 분양권.
    aptNm은 신축 단지명(예: 올림픽파크포레온), jibun은 신축 지번이라 기존 사업장 지번과 다를 수 있다."""
    items, _ = _rtms_items(SILV_API, "분양권", sgg, ym)
    rows = []
    for g in items:
        if g.get("cdealType") == "O":
            continue
        r = _place(g, sgg)
        r.pop("built", None)
        rows.append({**r, "price": int(_num(g.get("dealAmount"))),
                     "kind": "입주권" if (g.get("ownershipGbn") or "").startswith("입") else "분양권",
                     "deal_type": (g.get("dealingGbn") or "").strip() or None})
    return rows


OLD_AGE = 30  # 노후 단지 기준(년)


def old_cutoff(today: str) -> int:
    """이 해 이전(포함)에 지어진 단지를 노후 단지로 본다: 2026년 → 1996년 이전."""
    return int(today[:4]) - OLD_AGE


@lru_cache(maxsize=1)
def capital_sggs() -> tuple[str, ...]:
    """수도권(서울·인천·경기) 실거래 조회용 시군구 코드 — 동을 직접 가진 코드만(구가 있는 시는 구 코드)."""
    return tuple(sorted({c[:5] for c, _ in bjd_table() if c[:2] in ("11", "28", "41")}))


class ProjectLinker:
    """거래 1건 → 사업장. 같은 시군구·법정동에서 ① 지번이 같으면 연결, ② 아니면 단지명이 충분히 비슷하고 후보가 하나일 때만.
    결과는 (시군구, 동, 지번, 단지명)별로 캐시한다(이력 100만 건도 조합은 수만 개)."""

    def __init__(self, lots: dict[str, dict], names: dict[str, set[str]]):
        self.lots, self.names = lots, names
        self.by_dong: dict[tuple, list[str]] = {}
        for pid, lot in lots.items():
            self.by_dong.setdefault((lot["sigungu_cd"], lot["dong"]), []).append(pid)
        self._cache: dict[tuple, tuple[str | None, str | None]] = {}

    def link(self, sgg: str, dong: str, jibun: str, apt: str, loose_lot: bool = False) -> tuple[str | None, str | None]:
        key = (sgg, dong, jibun, apt, loose_lot)
        if key not in self._cache:
            self._cache[key] = self._link(sgg, dong, jibun, apt, loose_lot)
        return self._cache[key]

    def _link(self, sgg, dong, jibun, apt, loose_lot):
        cands = self.by_dong.get((sgg, dong), [])
        if not cands:
            return None, None
        exact = [p for p in cands if self.lots[p]["jibun"] == jibun]
        if len(exact) == 1:
            return exact[0], "지번"
        # 이름으로는 보수적으로: 같거나, 사업장 이름에서 앞의 동 이름만 뺀 형태('진주' ↔ '여의도진주')일 때만.
        # 거래 쪽 이름이 더 길면('워커힐' → '워커힐푸르지오', '목동2' → '목동2차아이파크') 다른 단지로 본다.
        key = cr.zone_key(apt or "")
        hits = [p for p in cands if len(key) >= 2 and any(
            k == key or (k.endswith(key) and len(k) - len(key) <= 4 and not re.search(r"\d", k[:-len(key)]))
            or key in (k + "맨션", k + "맨숀")  # '삼익파크' ↔ '삼익파크맨션'
            for k in self.names.get(p, ()))]
        if len(hits) == 1:
            return hits[0], "단지명"
        if loose_lot:
            # 입주권: 신축 지번은 '60-3'처럼 부번이 새로 붙는다 → 사업장 대표 지번이 '60 일원'(부번 0)이면 본번만 맞춰 본다
            bun = re.match(r"\s*(산)?\s*(\d+)", jibun or "")
            if bun:
                same = [p for p in cands if self.lots[p]["ji"] == "0000" and self.lots[p]["plat_gb"] == ("1" if bun.group(1) else "0")
                        and int(self.lots[p]["bun"]) == int(bun.group(2))]
                if len(same) == 1:
                    return same[0], "본번"
        return None, None


def match_trades(trades: list[dict], lots: dict[str, dict], names: dict[str, set[str]],
                 old_before: int | None = None, linker: ProjectLinker | None = None, loose: bool = False) -> list[dict]:
    """거래 목록에 project_id·match를 붙인다(ProjectLinker 규칙).
    old_before가 있으면 사업장에 연결되지 않은 거래도 그 해 이전에 지어진 단지(노후 단지)면 남긴다."""
    linker = linker or ProjectLinker(lots, names)
    out = []
    for t in trades:
        pid, how = linker.link(t["sgg"], t["dong"], t["jibun"], t["apt"], loose)
        if pid:
            out.append({**t, "project_id": pid, "match": how})
        elif old_before and t.get("built") and t["built"] <= old_before:
            out.append({**t, "project_id": None, "match": None})
    return out


def _ym(t: dict) -> str:
    return t["date"][:7].replace("-", "")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _lots_sig(lots: dict[str, dict], names: dict[str, set[str]], sgg: str) -> str:
    """시군구 안 사업장 지번·이름 구성의 서명 — 바뀌면(새 사업장, 수기 신축명 추가 등) 그 시군구를 다시 받아 다시 연결한다."""
    body = sorted((p, lot_key(l), sorted(names.get(p, ()))) for p, l in lots.items() if l["sigungu_cd"] == sgg)
    return hashlib.sha1(json.dumps(body, ensure_ascii=False).encode()).hexdigest()[:12]


def _fetch_many(jobs: list[tuple[str, str]], fetch, errors: list[str]) -> dict[tuple, list[dict]]:
    """(시군구, 월) 목록을 받는다. 일일 한도에 걸리면 그 뒤 요청은 보내지 않는다."""
    fetched: dict[tuple, list[dict]] = {}
    stop = threading.Event()

    def job(item):
        if stop.is_set():
            return item, None
        try:
            return item, fetch(*item)
        except QuotaExceeded as e:
            stop.set()
            errors.append(f"{item}: {e}")
            return item, None
        except (ApiError, ET.ParseError) as e:
            errors.append(f"{item}: {e}")
            return item, None

    with ThreadPoolExecutor(WORKERS) as ex:
        for item, rows in ex.map(job, jobs):
            if rows is not None:
                fetched[item] = rows
    return fetched


_SORT = lambda t: (t.get("project_id") or "", t["sgg"], t["dong"], t["jibun"], t["apt"], t["date"], t["area"],  # noqa: E731
                   t["floor"], t.get("price", 0), t.get("deposit", 0), t.get("monthly", 0))


def _update_window(label: str, fetch, data_file: Path, months_file: Path, lots: dict[str, dict],
                   names: dict[str, set[str]], today: str, failed: list, window: int = TRADE_WINDOW,
                   sggs: list[str] | None = None, keep_unlinked: bool = True,
                   loose: bool = False) -> tuple[list[dict], list[dict], bool]:
    """최근 window개월 창을 유지하는 공통 갱신: 최근 TRADE_REFRESH개월과 아직 없는 달만 받고, 사업장·단지에 연결해 저장.
    → (이전 목록, 새 목록, 받은 것이 있었는지). keep_unlinked=False면 사업장에 연결된 거래만 남긴다(분양권: 단지 연결은 안 함)."""
    months = months_back(today, window)
    fresh = set(months[:TRADE_REFRESH])
    state = _load_json(months_file)
    sggs = sorted(sggs if sggs is not None else set(capital_sggs()) | {lot["sigungu_cd"] for lot in lots.values()})
    cutoff = old_cutoff(today) if keep_unlinked else None
    sig = {s: _lots_sig(lots, names, s) for s in sggs}
    jobs = [(s, ym) for s in sggs for ym in months
            if ym in fresh or ym not in state.get(s, {}).get("months", {}) or state[s].get("lots") != sig[s]]
    old = list(_read_jsonl(data_file))
    if not jobs and set(state) == set(sggs):
        print(f"[{label}] 새로 받을 달 없음")
        return old, old, False
    print(f"[{label}] 시군구 {len(sggs)}곳 · 요청 {len(jobs)}건")
    errors: list[str] = []
    fetched = _fetch_many(jobs, fetch, errors)
    done = set(fetched)
    linker = ProjectLinker(lots, names)
    keep = [t for t in old if (t["sgg"], _ym(t)) not in done and _ym(t) in months and t["sgg"] in sig]
    keep = match_trades(keep, lots, names, cutoff, linker, loose)  # 저장된 거래도 현재 규칙·지번으로 다시 연결(규칙이 바뀌어도 일관되게)
    new = match_trades([t for rows in fetched.values() for t in rows], lots, names, cutoff, linker, loose)
    merged = sorted(keep + new, key=_SORT)
    if keep_unlinked:
        assign_complexes(merged)
    for s, ym in done:  # 받은 달만 표시(날짜를 적으면 매일 파일이 바뀌어 의미 없는 커밋이 생긴다)
        state.setdefault(s, {"months": {}})["months"][ym] = True
    for s in list(state):
        if s not in sig:
            state.pop(s)  # 더 볼 필요 없는 시군구(분양권: 관리처분 이후 사업장이 없어진 곳)
    for s in sggs:
        state.setdefault(s, {"months": {}})["lots"] = sig[s]
        state[s]["months"] = {ym: True for ym in state[s]["months"] if ym in months}
    _write_jsonl(data_file, merged)
    _save_json(months_file, state)
    if errors:
        failed.append(f"{label} {len(errors)}건 실패 (예: {errors[0]})")
    return old, merged, True


def update_trades(lots: dict[str, dict], names: dict[str, set[str]], today: str, log: list, failed: list) -> bool:
    """매매 최근 12개월. 수도권 83개 시군구 전체(노후 단지까지)."""
    old, merged, got = _update_window("실거래", fetch_trades, TRADES_FILE, TRADE_MONTHS_FILE, lots, names, today, failed)
    if not got:
        return False
    before, after = _count(old), _count(merged)
    for pid in sorted(after):
        if after[pid] > before.get(pid, 0):
            log.append((today, pid, "실거래 추가", "trade_count_12m", str(before.get(pid, 0)), str(after[pid]),
                        "국토부 실거래가"))
    print(f"[실거래] 저장 {len(merged)}건 — 사업장 연결 {sum(after.values())}건({len(after)}곳), "
          f"노후 단지(≤{old_cutoff(today)}년 준공) {sum(1 for t in merged if not t['project_id'])}건")
    return merged != old


def update_rents(lots: dict[str, dict], names: dict[str, set[str]], today: str, log: list, failed: list) -> bool:
    """전월세 최근 12개월(매매와 같은 범위·연결 규칙). 전세가율은 내보낼 때 매매 중위와 함께 계산한다."""
    old, merged, got = _update_window("전월세", fetch_rents, RENTS_FILE, RENT_MONTHS_FILE, lots, names, today, failed)
    if not got:
        return False
    linked = sum(1 for t in merged if t["project_id"])
    print(f"[전월세] 저장 {len(merged)}건 (전세 {sum(1 for t in merged if t['kind'] == '전세')}건) — 사업장 연결 {linked}건")
    return merged != old


SILV_FROM = cr.STAGE_ORDER["관리처분인가"]


def silv_sggs(lots: dict[str, dict], stages: dict[str, str]) -> list[str]:
    """분양권·입주권을 받을 시군구: 관리처분인가 이후(준공·청산 포함) 사업장이 대표 지번을 가진 곳."""
    return sorted({lot["sigungu_cd"] for pid, lot in lots.items()
                   if cr.STAGE_ORDER.get(stages.get(pid) or "", -1) >= SILV_FROM})


def update_silv(lots: dict[str, dict], names: dict[str, set[str]], stages: dict[str, str], today: str,
                log: list, failed: list) -> bool:
    """분양권·입주권 최근 36개월 — 관리처분인가 이후 사업장이 있는 시군구만, 사업장에 연결된 거래만 저장."""
    late = {pid: lot for pid, lot in lots.items() if cr.STAGE_ORDER.get(stages.get(pid) or "", -1) >= SILV_FROM}
    sggs = silv_sggs(lots, stages)
    if not sggs:
        return False
    old, merged, got = _update_window("분양권", fetch_silv, SILV_FILE, SILV_MONTHS_FILE, late, names, today, failed,
                                      window=SILV_WINDOW, sggs=sggs, keep_unlinked=False, loose=True)
    if not got:
        return False
    before, after = _count(old), _count(merged)
    for pid in sorted(after):
        if after[pid] > before.get(pid, 0):
            log.append((today, pid, "입주권·분양권 거래 추가", "silv_count", str(before.get(pid, 0)), str(after[pid]),
                        "국토부 분양권전매 실거래가"))
    print(f"[분양권] 시군구 {len(sggs)}곳 · 저장 {len(merged)}건 — 사업장 {len(after)}곳 "
          f"(입주권 {sum(1 for t in merged if t['kind'] == '입주권')}건)")
    return merged != old


# ---------------------------------------------------------------- 매매 이력 (2006년~)
def history_months(today: str) -> list[str]:
    """이력이 맡는 달: HISTORY_START부터 12개월 창 바로 앞 달까지(최신 달이 앞)."""
    top = months_back(today, TRADE_WINDOW + 1)[-1]
    out, y, m = [], int(top[:4]), int(top[4:])
    while f"{y}{m:02d}" >= HISTORY_START:
        out.append(f"{y}{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out


def _history_todo(ranges: dict[str, list[str]], sggs: list[str], months: list[str]) -> list[tuple[str, str]]:
    """아직 없는 (시군구, 달). 시군구별 받은 범위 [lo, hi]는 이어져 있어야 하므로, 창에서 막 빠져나온 달(hi 위)을 오래된 순으로
    먼저 붙이고, 그 다음 lo 아래를 최신 달부터(같은 달 안에서는 시군구 순) 받는다 → 최근 몇 년이 모든 시군구에서 먼저 채워진다."""
    top_gap: list[tuple[str, str]] = []
    below: dict[str, set[str]] = {}
    for s in sggs:
        lo, hi = ranges.get(s) or (None, None)
        if lo is None:
            below[s] = set(months)
        else:
            top_gap += [(s, ym) for ym in sorted(months) if ym > hi]
            below[s] = {ym for ym in months if ym < lo}
    return top_gap + [(s, ym) for ym in months for s in sggs if ym in below[s]]


def _merge_range(lo: str | None, hi: str | None, done: set[str]) -> list[str]:
    """받은 달을 기존 범위에 이어 붙인다. 이어지지 않는 달(중간에 실패한 경우)은 상태에 적지 않아 다음에 다시 받는다."""
    if lo is None:
        lo = hi = max(done)
    while _next_month(hi) in done:
        hi = _next_month(hi)
    while _prev_month(lo) in done:
        lo = _prev_month(lo)
    return [lo, hi]


def _history_keep(rows: list[dict], linker: ProjectLinker, built_max: int, old_lots: set[str]) -> list[dict]:
    """이력에 남길 거래: 사업장에 연결되거나, built_max년 이전 준공이거나, 노후 단지 지번(부동산원·건축물대장)의 거래.
    사업장·단지 연결은 저장하지 않고 내보낼 때 다시 계산한다(규칙이 바뀌어도 파일은 그대로)."""
    import identity
    out = []
    for t in rows:
        pid, _ = linker.link(t["sgg"], t["dong"], t["jibun"], t["apt"])
        if pid or (t["built"] and t["built"] <= built_max) or identity.jibun_key(t["sgg"], t["dong"], t["jibun"]) in old_lots:
            out.append({k: v for k, v in t.items() if v is not None and k != "sgg"})
    return out


def old_lots(built_max: int) -> set[str]:
    """부동산원 식별정보·건축물대장에서 사용승인 built_max년 이전인 아파트의 지번."""
    import identity
    out = set()
    for r in _read_jsonl(identity.REB_FILE):
        if r.get("lot") and r.get("type") == "아파트" and r.get("use_apr") and int(r["use_apr"][:4]) <= built_max:
            out.add(r["lot"])
    reg = STATE / "complexes" / "registry.jsonl"
    for r in _read_jsonl(reg):
        d = r.get("use_apr") or r.get("use_apr_title")
        if d and int(d[:4]) <= built_max:
            out.add(r["key"])
    return out


def history_file(sgg: str) -> Path:
    return HISTORY_DIR / f"{sgg}.jsonl"


_HSORT = lambda t: (t["date"], t["dong"], t["jibun"], t["apt"], t["area"], t["floor"], t["price"], t.get("dong_no") or "")  # noqa: E731


def update_history(lots: dict[str, dict], names: dict[str, set[str]], today: str, failed: list,
                   budget: int | None = None, deadline: float | None = None) -> bool:
    """2006년부터의 매매 이력을 시군구·월 단위로 받아 state/trades_history/<시군구>.jsonl에 쌓는다.

    하루 예산(HISTORY_DAILY_CAP, 날짜별 호출 수를 상태 파일에 적어 두 번째 실행도 합산)과 실행 시간(deadline, monotonic)
    안에서만 받고, 진행 위치는 시군구별 [받은 첫 달, 받은 끝 달]로 둔다. 최신 달부터 받으므로 최근 몇 년이 먼저 채워진다.
    노후 기준이 해가 바뀌어 올라가면(built_max 변경) 범위를 비워 처음부터 다시 받는다(파일은 유지, 달마다 교체)."""
    st = _load_json(HISTORY_STATE_FILE)
    built_max = old_cutoff(today) + HISTORY_MARGIN
    if st.get("built_max") != built_max:
        if st.get("ranges"):
            print(f"[이력] 노후 기준이 {st.get('built_max')}→{built_max}년으로 바뀜 — 범위를 비우고 다시 받는다")
        st = {"built_max": built_max, "ranges": {}, "day": st.get("day"), "calls": st.get("calls", 0)}
    if st.get("day") != today:
        st["day"], st["calls"] = today, 0
    sggs = sorted(set(capital_sggs()) | {lot["sigungu_cd"] for lot in lots.values()})
    months = history_months(today)
    ranges: dict[str, list[str]] = st.setdefault("ranges", {})
    todo = _history_todo(ranges, sggs, months)
    allowed = HISTORY_DAILY_CAP - st["calls"]
    if budget is not None:
        allowed = min(allowed, budget)
    if not todo:
        print(f"[이력] 매매 이력 완료 — {months[-1][:4]}년~{months[0][:4]}년, 시군구 {len(sggs)}곳")
        return False
    if allowed <= 0 or (deadline is not None and time.monotonic() >= deadline):
        print(f"[이력] 남은 요청 {len(todo)}건 — 오늘 예산/시간 소진(호출 {st['calls']}건), 내일 이어서")
        return False
    jobs = todo[:allowed]
    print(f"[이력] 남은 요청 {len(todo)}건 중 {len(jobs)}건 시도 (오늘 호출 {st['calls']}건 사용)")
    linker = ProjectLinker(lots, names)
    olds = old_lots(built_max)
    errors: list[str] = []
    stop = threading.Event()
    broken: set[str] = set()   # 실패한 시군구는 그 뒤 달을 받지 않는다(범위가 끊기지 않게)
    calls = [0]
    lock = threading.Lock()

    def job(item):
        if stop.is_set() or item[0] in broken or (deadline is not None and time.monotonic() >= deadline):
            return item, None
        try:
            rows, n = fetch_trades_counted(*item)
            with lock:
                calls[0] += n
            return item, rows
        except QuotaExceeded as e:
            stop.set()
            errors.append(f"{item}: {e}")
            return item, None
        except (ApiError, ET.ParseError) as e:
            broken.add(item[0])
            errors.append(f"{item}: {e}")
            return item, None

    got: dict[str, dict[str, list[dict]]] = {}   # sgg → {ym: rows}
    with ThreadPoolExecutor(WORKERS) as ex:
        for (s, ym), rows in ex.map(job, jobs):
            if rows is None:
                broken.add(s)
                continue
            got.setdefault(s, {})[ym] = _history_keep(rows, linker, built_max, olds)
    st["calls"] += calls[0]
    n_rows = 0
    for s, by_ym in got.items():
        path = history_file(s)
        old = [t for t in _read_jsonl(path) if _ym(t) not in by_ym]
        new = [t for rows in by_ym.values() for t in rows]
        n_rows += len(new)
        _write_jsonl(path, sorted(old + new, key=_HSORT))
        lo, hi = ranges.get(s) or (None, None)
        ranges[s] = _merge_range(lo, hi, set(by_ym))
    _save_json(HISTORY_STATE_FILE, st)
    left = len(_history_todo(ranges, sggs, months))
    print(f"[이력] {sum(len(v) for v in got.values())}개 시군구·달 받음(호출 {calls[0]}건, 남긴 거래 {n_rows}건) — 남은 요청 {left}건")
    if errors:
        failed.append(f"매매 이력 {len(errors)}건 실패 (예: {errors[0]})")
    return bool(got)


def _next_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[4:])
    return f"{y + 1}01" if m == 12 else f"{y}{m + 1:02d}"


def _prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[4:])
    return f"{y - 1}12" if m == 1 else f"{y}{m - 1:02d}"


def assign_complexes(trades: list[dict]) -> None:
    """거래마다 부동산원 단지(identity)를 붙인다: 같은 지번에 단지가 하나면 그대로, 여럿이면 단지명 → 동 번호 순."""
    import identity  # enrich를 import하는 모듈이라 여기서 늦게 불러온다
    if not identity.REB_FILE.exists():
        return
    idx = identity.Index()
    for t in trades:
        lot = identity.jibun_key(t["sgg"], t["dong"], t["jibun"])
        t["complex_id"], t["complex_match"] = idx.match_trade(lot, t["apt"], t.get("dong_no"))


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


def rent_summary(rents: list[dict], sales_by_area: list[dict] | None = None) -> dict:
    """전월세 12개월 요약: 전용면적별 전세 중위·건수, 월세 건수·중위, 매매 중위가 있으면 전세가율(%).
    대표 전세가율은 전세 계약이 가장 많은 면적대(매매 중위가 있는 것 중)의 값."""
    groups: dict[int, list[dict]] = {}
    for r in rents:
        groups.setdefault(round(r["area"]), []).append(r)
    sale_med = {a["area"]: a["median"] for a in (sales_by_area or [])}
    by_area, best = [], None
    for area in sorted(groups):
        g = groups[area]
        je = sorted((r for r in g if r["kind"] == "전세"), key=lambda r: (r["date"], r["deposit"]))
        wo = [r for r in g if r["kind"] == "월세"]
        row = {"area": area, "pyeong": round(area / 3.3058, 1), "jeonse_n": len(je), "wolse_n": len(wo)}
        if je:
            deps = [r["deposit"] for r in je]
            row.update(jeonse_median=int(statistics.median(deps)), jeonse_min=min(deps), jeonse_max=max(deps),
                       jeonse_latest=je[-1]["deposit"], jeonse_latest_date=je[-1]["date"])
            if sale_med.get(area):
                row["jeonse_ratio"] = round(row["jeonse_median"] / sale_med[area] * 100, 1)
                if best is None or len(je) > best["jeonse_n"]:
                    best = row
        if wo:
            row.update(wolse_deposit_median=int(statistics.median(r["deposit"] for r in wo)),
                       wolse_monthly_median=int(statistics.median(r["monthly"] for r in wo)))
        by_area.append(row)
    je_all = [r for r in by_area if r.get("jeonse_median")]
    top = best or (max(je_all, key=lambda r: r["jeonse_n"]) if je_all else None)
    out = {"rent_count_12m": len(rents), "rents_by_area": by_area}
    if top:
        out.update(jeonse_median=top["jeonse_median"], jeonse_area=top["area"], jeonse_ratio=top.get("jeonse_ratio"))
    return out


# ---------------------------------------------------------------- 진입점
def update(con, today: str, log: list, failed: list) -> bool:
    """update_daily에서 호출(매매 이력은 update_history로 따로 — 실행 시간 예산을 마지막에 쓴다). 바뀐 것이 있으면 True."""
    lots, names = project_lots(con), project_names(con)
    stages = {pid: json.loads(rec).get("stage") for pid, rec in con.execute("SELECT project_id, record FROM projects")}
    print(f"[보강] 지번을 아는 사업장 {len(lots)}곳")
    changed = update_buildings(lots, today, log, failed)
    changed = update_trades(lots, names, today, log, failed) or changed
    changed = update_rents(lots, names, today, log, failed) or changed
    return update_silv(lots, names, stages, today, log, failed) or changed


def fields(records: dict[str, dict], book=None) -> dict[str, dict]:
    """export 때 사업장 레코드에 붙일 보강 필드 {project_id: {...}}. records: {project_id: 사업장 레코드}.
    book(tradebook.Book)이 있으면 매매 이력·전월세·분양권 요약도 붙인다."""
    out: dict[str, dict] = {}
    for b in _read_jsonl(BUILDINGS_FILE):
        out[b["project_id"]] = {k: v for k, v in b.items()
                                if k not in ("project_id", "lot", "fetched_at", "found") and v is not None}
    if book is None:
        by_pid: dict[str, list[dict]] = {}
        for t in _read_jsonl(TRADES_FILE):
            if t["project_id"]:
                by_pid.setdefault(t["project_id"], []).append(t)
        for pid, ts in by_pid.items():
            use, basis = project_trade_filter(ts, records.get(pid, {}), out.get(pid, {}))
            if use:
                out.setdefault(pid, {}).update(trade_summary(use), trade_basis=basis)
        return out
    for pid in set(book.pid_sales) | set(book.pid_rents) | set(book.pid_silv):
        rec = records.get(pid, {})
        extra = book.project_fields(pid, rec, out.get(pid, {}))
        if extra:
            out.setdefault(pid, {}).update(extra)
    return out


def project_trade_filter(ts: list[dict], rec: dict, bld: dict) -> tuple[list[dict], str]:
    """사업장 거래 중 쓸 것: 준공이면 전부(신축), 아니면 기존 단지 준공연도보다 훨씬 늦게 지어진 건물(같은 지번의 신축)은 뺀다."""
    if rec.get("stage") == "준공":
        return ts, "준공 후 신축"
    old = rec.get("existing_built_year") or int((bld.get("bld_approved") or "0")[:4]) or None
    return [t for t in ts if not (old and t.get("built") and t["built"] > old + 5)], "기존 단지"


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
