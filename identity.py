"""단지 식별 레이어 — 한국부동산원 「공동주택 단지 식별정보」(공시대상 공동주택 전수)를 기준으로 삼는다.

왜 필요한가: 지번 하나에 단지가 여럿일 수 있고(예: 산본동 1155-7의 '가야3주공'과 '가야주공(임대)'),
출처마다 같은 단지를 다른 이름으로 부른다. 그래서 단지 1개 = 부동산원 COMPLEX_PK 로 고정하고,
지번·K-apt·건축물대장·실거래는 여기에 연결한다.

  state/complexes/reb.jsonl          수도권 아파트·연립 전수: 이름 3개, 주소, PNU, 세대·동수, 사용승인일 (분기 갱신, 약 85회 호출)
  state/complexes/reb_dongs.jsonl    필요한 단지의 동 목록·층수와 단지명 변경 이력
                                     (지번을 공유하는 단지, 30년 이상 단지, 사업장과 같은 지번의 단지 — 하루 DONG_BUDGET개씩)
API: api.odcloud.kr/api/AptIdInfoSvc/v1 (getAptInfo / getDongInfo / getHistInfo). 개발계정 40,000건/일.
부동산원 단지명(COMPLEX_NM1)은 국토부 실거래 단지명과 같은 체계라 실거래 연결의 기준 이름으로 쓴다.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import collect_reconstruction as cr
import enrich as en

REB_API = "https://api.odcloud.kr/api/AptIdInfoSvc/v1/"
REB_FILE = en.STATE / "complexes" / "reb.jsonl"
REB_DONGS_FILE = en.STATE / "complexes" / "reb_dongs.jsonl"
ID_STATE_FILE = en.STATE / "complexes" / "identity_state.json"
DONG_BUDGET = 1500  # 하루에 동 목록·이력을 새로 받는 단지 수(단지당 2회 호출 → 약 10분)
TYPES = {"1": "아파트", "2": "연립", "3": "다세대"}


def quarter(today: str) -> str:
    return f"{today[:4]}Q{(int(today[5:7]) - 1) // 3 + 1}"


# ---------------------------------------------------------------- 키·주소 변환 (다른 모듈도 쓴다)
def complex_key(bjd: str, bun: int, ji: int, plat_gb: str = "0") -> str:
    """지번 키 '법정동코드-산여부-본번-부번'. enrich.lot_key(사업장 대표 지번)와 같은 형식."""
    return f"{bjd}-{plat_gb}-{bun:04d}-{ji:04d}"


def pnu_to_lot(pnu) -> str | None:
    """PNU 19자리(법정동10 + 대지구분1[1=일반,2=산] + 본번4 + 부번4) → 지번 키."""
    p = str(pnu or "")
    if len(p) != 19 or not p.isdigit():
        return None
    return complex_key(p[:10], int(p[11:15]), int(p[15:19]), "1" if p[10] == "2" else "0")


@lru_cache(maxsize=1)
def dong_index() -> dict[tuple[str, str], str]:
    """(시군구코드 5자리, 법정동명) → 법정동코드. 실거래 레코드(sggCd, umdNm)를 코드로 바꿀 때 쓴다."""
    out = {}
    for code, name in en.bjd_table():
        if code[:2] in ("11", "28", "41"):
            out[(code[:5], name.split()[-1])] = code
            if code[8:] != "00":  # 리: '봉담읍 와우리'처럼 읍면+리로도 찾을 수 있게
                out[(code[:5], " ".join(name.split()[-2:]))] = code
    return out


def jibun_key(sgg: str, dong: str, jibun: str) -> str | None:
    code = dong_index().get((sgg, dong))
    m = re.fullmatch(r"\s*(산)?\s*(\d+)(?:-(\d+))?\s*", jibun or "")
    if not code or not m:
        return None
    return complex_key(code, int(m.group(2)), int(m.group(3) or 0), "1" if m.group(1) else "0")


@lru_cache(maxsize=1)
def bjd_canonical() -> dict[str, str]:
    """폐지된 법정동코드 → 현재 코드. 부동산원 PNU는 옛 코드(예: 인천 서구→서해구 개편 전)를 쓰는 경우가 있어
    건축물대장·실거래(현재 코드)와 지번 키를 맞추려면 바꿔 줘야 한다. 같은 시도 안에서 동 이름이 하나뿐일 때만 매핑."""
    rows = [(c, n, st) for c, n, st in en.bjd_table_all() if c[:2] in ("11", "28", "41") and not c.endswith("00000")]
    current: dict[tuple[str, str], list[str]] = {}
    for c, n, st in rows:
        if st == "존재":
            parts = n.split()
            current.setdefault((parts[0], parts[-1]), []).append(c)
            if len(parts) >= 3:
                current.setdefault((parts[0], " ".join(parts[-2:])), []).append(c)  # 읍면 + 리
    out = {}
    for c, n, st in rows:
        if st != "존재":
            parts = n.split()
            cands = current.get((parts[0], " ".join(parts[-2:])), []) if len(parts) >= 3 else []
            cands = cands or current.get((parts[0], parts[-1]), [])
            if len(set(cands)) == 1:
                out[c] = cands[0]
    return out


def canonical_bjd(code: str | None) -> str | None:
    return bjd_canonical().get(code or "", code) if code else None


@lru_cache(maxsize=1)
def capital_sgg_names() -> tuple[tuple[str, str], ...]:
    """[(시군구코드, '경기도 수원시 장안구')] — 부동산원 주소 LIKE 검색용. 구가 있는 시는 구 단위."""
    names = {c: n for c, n in en.bjd_table()}
    out: dict[str, str] = {}
    for code, name in en.bjd_table():
        if code[:2] in ("11", "28", "41") and code[5:] != "00000" and code[:5] not in out:
            out[code[:5]] = names.get(code[:5] + "00000") or " ".join(name.split()[:2])
    return tuple(sorted(out.items()))


# ---------------------------------------------------------------- 수집
def _get(path: str, params: dict) -> dict:
    key = en.api_key()
    en._throttle()
    try:
        r = en._session().get(REB_API + path, params={"serviceKey": key, **params}, timeout=cr.TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise en.ApiError(str(e).replace(key or "\0", "***")) from None


def _load_state() -> dict:
    return json.loads(ID_STATE_FILE.read_text(encoding="utf-8")) if ID_STATE_FILE.exists() else {}


def _save_state(st: dict) -> None:
    ID_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ID_STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
                             encoding="utf-8", newline="\n")


def _row(x: dict) -> dict:
    names = [str(n).strip() for n in (x.get("COMPLEX_NM1"), x.get("COMPLEX_NM2"), x.get("COMPLEX_NM3"))
             if n and str(n).strip()]
    d = str(x.get("USEAPR_DT") or "").strip()
    pnu = str(x.get("PNU") or "")
    bjd = canonical_bjd(pnu[:10]) if len(pnu) == 19 else None  # 옛 법정동코드는 현재 코드로
    lot = pnu_to_lot(pnu)
    if lot and bjd and bjd != pnu[:10]:
        lot = bjd + lot[10:]
    return {"pk": str(x["COMPLEX_PK"]), "names": list(dict.fromkeys(names)), "address": x.get("ADRES"),
            "road_address": x.get("ROAD_ADRES") or None, "pnu": pnu or None, "lot": lot,
            "bjd": bjd, "units": int(en._num(x.get("UNIT_CNT"))) or None,
            "dong_cnt": int(en._num(x.get("DONG_CNT"))) or None,
            "use_apr": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if re.fullmatch(r"\d{8}", d) else None,
            "type": TYPES.get(str(x.get("COMPLEX_GB_CD")), str(x.get("COMPLEX_GB_CD")))}


def update_reb(today: str, failed: list) -> bool:
    """분기마다 수도권 아파트·연립 전수를 새로 받는다(다세대 제외).

    주소 문자열은 형식이 들쑥날쑥해서('경기도 수원권선구 권선동', 인천은 개편 전 구 이름) 시군구 이름으로 거르지 않고
    시도 이름으로만 받은 뒤(3개 시도, 5,000행씩 약 40쪽) 지역은 PNU의 법정동코드로 정한다. 실패한 시도는 이전 행을 유지."""
    st = _load_state()
    if st.get("reb_quarter") == quarter(today) and REB_FILE.exists():
        return False
    old = list(en._read_jsonl(REB_FILE))
    rows: dict[str, dict] = {}
    done, errors = set(), []
    for prefix, sido in (("11", "서울특별시"), ("28", "인천광역시"), ("41", "경기도")):
        page, ok = 1, True
        while True:
            try:
                d = _get("getAptInfo", {"page": page, "perPage": 5000, "cond[ADRES::LIKE]": sido})
            except en.ApiError as e:
                errors.append(f"{sido}: {e}")
                ok = False
                break
            for x in d.get("data", []):
                if str(x.get("COMPLEX_GB_CD")) in ("1", "2"):
                    r = _row(x)
                    if r["bjd"] and r["bjd"][:2] == prefix:
                        rows[r["pk"]] = r
            if d.get("currentCount", 0) < 5000 or page > 60:
                break
            page += 1
        if ok:
            done.add(prefix)
    for r in old:  # 이번에 못 받은 시도는 이전 결과 유지
        if (r.get("bjd") or "")[:2] not in done:
            rows.setdefault(r["pk"], r)
    if not rows:
        failed.append(f"식별정보 전체 실패 (예: {errors[0] if errors else '?'})")
        return False
    en._write_jsonl(REB_FILE, (rows[k] for k in sorted(rows)))
    if errors:
        failed.append(f"식별정보 {len(errors)}개 시도 실패 (예: {errors[0]})")
    else:
        st["reb_quarter"] = quarter(today)
        _save_state(st)
    n_apt = sum(1 for r in rows.values() if r["type"] == "아파트")
    print(f"[식별정보] 수도권 공동주택 {len(rows)}단지 (아파트 {n_apt}) — 시도 {len(done)}/3")
    return rows != {r["pk"]: r for r in old}


def needed_pks(cutoff_year: int, project_lots: set[str]) -> list[str]:
    """동 목록이 필요한 단지: 지번을 공유하는 아파트, 노후 아파트, 사업장과 같은 지번의 아파트."""
    rows = [r for r in en._read_jsonl(REB_FILE) if r["type"] == "아파트"]
    per_lot: dict[str, int] = {}
    for r in rows:
        per_lot[r["lot"]] = per_lot.get(r["lot"], 0) + 1
    return [r["pk"] for r in rows if per_lot.get(r["lot"], 0) > 1 or r["lot"] in project_lots
            or (r["use_apr"] and int(r["use_apr"][:4]) <= cutoff_year)]


def update_dongs(today: str, failed: list, project_lots: set[str], budget: int | None = DONG_BUDGET) -> bool:
    have = {r["pk"]: r for r in en._read_jsonl(REB_DONGS_FILE)}
    todo = [pk for pk in needed_pks(en.old_cutoff(today), project_lots) if pk not in have]
    todo = todo if budget is None else todo[:budget]
    if not todo:
        return False
    print(f"[식별정보] 동 목록·이력 {len(todo)}단지 조회")
    errors: list[str] = []

    def job(pk: str):
        try:
            d = _get("getDongInfo", {"page": 1, "perPage": 500, "cond[COMPLEX_PK::EQ]": pk})
            h = _get("getHistInfo", {"page": 1, "perPage": 100, "cond[COMPLEX_PK::EQ]": pk})
        except en.ApiError as e:
            errors.append(f"{pk}: {e}")
            return None
        dongs = [{"no": str(x.get("DONG_NM1") or "").strip(),
                  "name": str(x.get("DONG_NM2") or x.get("DONG_NM1") or "").strip(),
                  "floors": int(en._num(x.get("GRND_FLR_CNT"))) or None} for x in d.get("data", [])]
        hist = [{"year": x.get("NM_CHAN_YEAR"), "old": x.get("COMPLEX_EX_NM"), "new": x.get("COMPLEX_PR_NM")}
                for x in h.get("data", [])]
        return {"pk": pk, "dongs": dongs, "hist": hist, "fetched": today[:7]}

    with ThreadPoolExecutor(en.WORKERS) as ex:
        for rec in ex.map(job, todo):
            if rec:
                have[rec["pk"]] = rec
    en._write_jsonl(REB_DONGS_FILE, (have[k] for k in sorted(have)))
    if errors:
        failed.append(f"식별정보 동 목록 {len(errors)}단지 실패 (예: {errors[0]})")
    return True


def update(today: str, failed: list, project_lots: set[str], dong_budget: int | None = DONG_BUDGET) -> bool:
    changed = update_reb(today, failed)
    return update_dongs(today, failed, project_lots, dong_budget) or changed


# ---------------------------------------------------------------- 색인·연결
class Index:
    """reb.jsonl + reb_dongs.jsonl 을 메모리에 올려 지번·이름·동 번호로 단지를 찾는다."""

    def __init__(self):
        self.by_pk = {r["pk"]: r for r in en._read_jsonl(REB_FILE)}
        self.extra = {r["pk"]: r for r in en._read_jsonl(REB_DONGS_FILE)}
        self.by_lot: dict[str, list[dict]] = {}
        for r in self.by_pk.values():
            if r["lot"]:
                self.by_lot.setdefault(r["lot"], []).append(r)
        self._keys: dict[str, set[str]] = {}
        self._dongs: dict[str, set[str]] = {}

    def apartments_on(self, lot: str | None) -> list[dict]:
        return [r for r in self.by_lot.get(lot or "", []) if r["type"] == "아파트"]

    def names(self, pk: str) -> list[str]:
        r = self.by_pk.get(pk, {})
        hist = [h.get("old") for h in self.extra.get(pk, {}).get("hist", [])] + \
               [h.get("new") for h in self.extra.get(pk, {}).get("hist", [])]
        return list(dict.fromkeys(n for n in [*r.get("names", []), *hist] if n and str(n).strip()))

    def name_keys(self, pk: str) -> set[str]:
        if pk not in self._keys:
            self._keys[pk] = {k for k in (cr.zone_key(n) for n in self.names(pk)) if k}
        return self._keys[pk]

    def dong_numbers(self, pk: str) -> set[str]:
        if pk not in self._dongs:
            self._dongs[pk] = {re.sub(r"\D", "", d["no"]) for d in self.extra.get(pk, {}).get("dongs", [])
                               if re.sub(r"\D", "", d["no"])}
        return self._dongs[pk]

    def match_trade(self, lot: str | None, apt_name: str, dong_no) -> tuple[str | None, str | None]:
        """실거래 1건 → 단지. 같은 지번에 단지가 하나면 그대로, 여럿이면 단지명 → 동 번호 순으로 고른다."""
        cands = self.apartments_on(lot)
        if not cands:
            return None, None
        if len(cands) == 1:
            return cands[0]["pk"], "지번"
        key = cr.zone_key(apt_name)
        by_name = [c for c in cands if key and key in self.name_keys(c["pk"])]
        if len(by_name) == 1:
            return by_name[0]["pk"], "단지명"
        dn = re.sub(r"\D", "", str(dong_no or ""))
        if dn:
            by_dong = [c for c in cands if dn in self.dong_numbers(c["pk"])]
            if len(by_dong) == 1:
                return by_dong[0]["pk"], "동"
        return None, "지번공통"

    def match_name(self, lot: str | None, name: str, threshold: float = 0.6,
                   households: int | None = None) -> str | None:
        """K-apt·대장처럼 지번과 이름만 있는 레코드 → 단지. 같은 지번에 단지가 여럿이면 이름이 가장 비슷한 하나,
        이름으로 못 가리면 세대수가 같은 하나(산본동 1155-7: K-apt '산본가야2차' 900세대 ↔ 부동산원 '가야주공(임대)' 900세대)."""
        cands = self.apartments_on(lot)
        if len(cands) == 1:
            return cands[0]["pk"]
        key = cr.zone_key(name)
        scored = sorted(((max((cr._score(key, k) for k in self.name_keys(c["pk"])), default=0), c["pk"])
                         for c in cands), reverse=True)
        if scored and scored[0][0] >= threshold and (len(scored) == 1 or scored[1][0] < scored[0][0] - 0.05):
            return scored[0][1]
        if households:
            same = [c["pk"] for c in cands if c.get("units") and abs(c["units"] - households) <= max(2, households * 0.02)]
            if len(same) == 1:
                return same[0]
        return None

    def dong_range(self, pk: str) -> str | None:
        nums = sorted(int(n) for n in self.dong_numbers(pk) if n.isdigit())
        if not nums:
            return None
        if len(nums) == 1:
            return f"{nums[0]}동"
        return f"{nums[0]}~{nums[-1]}동" if nums[-1] - nums[0] + 1 == len(nums) else f"{nums[0]}~{nums[-1]}동({len(nums)}개)"
