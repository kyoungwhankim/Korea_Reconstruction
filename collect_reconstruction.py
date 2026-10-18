"""수도권(서울·경기·인천) 아파트 재건축 사업 진행 현황 수집기.

공시(정부·지자체) 출처만 사용한다. 인증키 없이 동작한다.
  1) 국토교통부 「전국 도시정비사업 통합 데이터」 (data.go.kr 15160169, CSV)
  2) 서울시 「정비사업 추진현황」 엑셀 (열린데이터광장 OA-22856, 분기별, 단계별 인가일 포함)
  3) 서울시 「도시계획사업(서울플랜+) 정비사업 현황」 (열린데이터광장 OA-23112, 신속통합기획·준공 포함)
  4) 경기도 「일반 정비 사업 추진 현황」 (경기데이터드림, 분기별, 단계별 일자 포함)
  5) 부천시 「정비사업 추진현황」 엑셀 (부천시 정비사업 통합 홈페이지)
  6) 인천광역시 「도시 및 주거환경 정비사업 추진현황」 (data.go.kr 15055212, 월별, 현재 단계만)
  7) 인천 미추홀구·연수구 정비사업 추진현황 (data.go.kr 15150904, 15039862, 단계별 일자)

사용법:
    pip install requests pandas openpyxl
    python update_daily.py --init               # 최초 1회: 전체 구축 + 상태 DB(state/) 생성
    python update_daily.py                      # 매일: 바뀐 출처·행·사업장만 갱신 (변경 이력 output/changes/)
    python search_reconstruction.py 은마        # 어떤 이름으로든 검색
    python collect_reconstruction.py            # (검증용) 상태와 무관하게 전체를 새로 만들어 _full_YYYYMMDD 로 저장

출력 (output/):
  capital_area_apartment_reconstruction_YYYYMMDD.csv  사업장당 1행. aliases 열에 모든 별칭이 있어 엑셀 Ctrl+F로도 검색됨
  reconstruction_aliases_YYYYMMDD.csv                  별칭 1개당 1행 (alias → project_id). 다른 표와 이름으로 조인할 때 사용
  excluded_single_house_YYYYMMDD.csv                   단독주택 재건축으로 판단해 제외한 사업장
manual_aliases.csv 에 행을 추가하면 자동으로 못 묶은 사업장을 합치거나 통칭(예: '압구정 현대')을 별칭으로 붙일 수 있다.
"""
from __future__ import annotations

import hashlib
import io
import json
import numbers
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd
import requests

BASE = Path(__file__).resolve().parent
RAW = BASE / "raw"
OUT = BASE / "output"
UA = {"User-Agent": "Mozilla/5.0 (reconstruction-status-collector)"}

# ---------------------------------------------------------------- 표준 단계
STAGES = [
    (0, "예정구역·후보지"),   # 기본계획상 정비예정구역, 신속통합기획·인천 후보지 선정
    (1, "안전진단"),          # 재건축 안전진단 추진·통과 (정비계획 전)
    (2, "정비계획수립"),      # 신통 기획완료 / 정비계획 입안·열람공고
    (3, "정비구역지정"),
    (4, "추진위구성"),
    (5, "조합설립인가"),      # 공공·신탁 방식은 사업시행자 지정
    (6, "건축심의"),
    (7, "사업시행인가"),
    (8, "관리처분인가"),
    (9, "이주·철거"),
    (10, "착공"),
    (11, "분양"),             # 일반분양 (착공 후)
    (12, "준공"),
    (13, "이전고시·청산"),    # 이전고시, 조합 해산·청산
]
# 단계 이름을 바꾸거나 쪼갠 이력 — 스키마 변경 실행(--migrate) 때 '단계 변경'으로 세지 않는다
RENAMED_STAGES = {("정비계획수립", "안전진단"), ("관리처분인가", "이주·철거"), ("착공", "분양"), ("준공", "이전고시·청산")}
STAGE_ORDER = {name: i for i, name in STAGES}
STAGE_ALIASES = {
    "대상지선정": "예정구역·후보지", "예정구역": "예정구역·후보지", "후보지": "예정구역·후보지",
    "사업지선정": "예정구역·후보지",
    "기획완료": "정비계획수립", "열람공고": "정비계획수립", "정비계획수립": "정비계획수립", "정비계획": "정비계획수립",
    "안전진단": "안전진단", "재건축진단": "안전진단", "예비안전진단": "안전진단", "정밀안전진단": "안전진단",
    "안전진단통과": "안전진단", "기본계획": "정비계획수립", "기본계획수립": "정비계획수립",
    "구역지정": "정비구역지정", "정비구역지정": "정비구역지정", "정비계획수립및구역지정": "정비구역지정",
    "정비구역": "정비구역지정",
    "추진위": "추진위구성", "추진위구성": "추진위구성", "추진위원회": "추진위구성", "추진위원회승인": "추진위구성",
    "조합설립추진위원회승인": "추진위구성", "추진위승인": "추진위구성",
    "조합설립": "조합설립인가", "조합설립인가": "조합설립인가", "사업시행자지정": "조합설립인가",
    "건축심의": "건축심의",
    "사업시행": "사업시행인가", "사업시행인가": "사업시행인가", "사업시행계획인가": "사업시행인가",
    "관리처분": "관리처분인가", "관리처분인가": "관리처분인가", "관리처분계획인가": "관리처분인가",
    "이주": "이주·철거", "철거": "이주·철거", "이주·철거": "이주·철거",
    "착공": "착공", "공사중": "착공", "착공신고": "착공",
    "분양": "분양", "일반분양": "분양", "분양승인": "분양",
    "준공": "준공", "준공(일부)": "준공", "준공인가": "준공", "부분준공인가": "준공", "부분준공": "준공",
    "이전고시": "이전고시·청산", "청산": "이전고시·청산", "해산": "이전고시·청산", "조합해산": "이전고시·청산",
    "조합청산": "이전고시·청산", "이전고시·청산": "이전고시·청산",
}


def norm_stage(raw) -> str | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = re.sub(r"^\d+\)", "", str(raw)).strip()
    s = re.sub(r"\s+", "", s)
    if s in STAGE_ALIASES:
        return STAGE_ALIASES[s]
    if re.search(r"용역|진행중|추진중", s) and re.search(r"정비계획|구역지정", s):
        return "정비계획수립"                         # '정비계획 수립 및 구역지정 용역 진행중'
    # '후보지 2차(추진위승인)', '정비구역지정(추진위승인)' → 언급된 단계 중 가장 진전된 것
    hits = [v for k, v in STAGE_ALIASES.items() if k in s]
    return max(hits, key=STAGE_ORDER.get) if hits else None


# ---------------------------------------------------------------- 매칭 키
_SIDO = {"서울": "서울특별시", "경기": "경기도", "인천": "인천광역시"}


def norm_sido(s: str) -> str:
    for k, v in _SIDO.items():
        if str(s).startswith(k):
            return v
    return str(s)


GG_CITIES = ["수원", "성남", "의정부", "안양", "부천", "광명", "평택", "동두천", "안산", "고양", "과천", "구리",
             "남양주", "오산", "시흥", "군포", "의왕", "하남", "용인", "파주", "이천", "안성", "김포", "화성",
             "광주", "양주", "포천", "여주", "연천", "가평", "양평"]
# 2026-07 인천 행정체제 개편: 중구·동구 → 제물포구·영종구, 서구 → 서해구·검단구
ICN_RENAMED = {"중구": "제물포구", "동구": "제물포구", "서구": "서해구"}


def sgg_key(s: str, sido: str = "") -> str:
    """'안양시 동안구' / '안양동안구' / '안양시' → 비교용 키.

    경기도는 출처마다 시 단위('안양시')와 구 단위('안양동안구')가 섞여 있어 시 단위로 맞춘다.
    """
    s = re.sub(r"\s+", "", str(s))
    if sido == "경기도":
        for c in sorted(GG_CITIES, key=len, reverse=True):
            if s.startswith(c):
                return c
    if sido == "인천광역시":
        s = ICN_RENAMED.get(s, s)
    return re.sub(r"시(?=.+[구군]$)", "", s)  # 수원시장안구 → 수원장안구


_CIRCLED = {chr(0x2460 + i): str(i + 1) for i in range(20)} | {"Ⅰ": "1", "Ⅱ": "2", "Ⅲ": "3", "Ⅳ": "4", "Ⅴ": "5"}


def zone_key(name: str) -> str:
    s = "".join(_CIRCLED.get(ch, ch) for ch in str(name))
    s = re.sub(r"\(재촉\)|\(통합재건축\)", "", s)
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"(?<=[가-힣])A(?=$|\d|[(,·])", "", s)        # 인천 표기 '남광로얄A'
    s = re.sub(r"아파트지구특별계획구역|특별계획구역|재정비촉진|촉진", "", s)
    s = re.sub(r"(주택)?재건축(정비)?(사업)?(조합)?|정비사업|정비구역|아파트|APT|주택|일대|번지", "", s, flags=re.I)
    s = s.replace("맨숀", "맨션").replace("시영", "시영")
    s = re.sub(r"제(?=\d)", "", s)
    s = re.sub(r"(?<=\d)차", "", s)
    s = re.sub(r"(?<=\d)단지", "", s)
    s = re.sub(r"구역$", "", s)
    s = re.sub(r"(?<=\d)[·,.∙](?=\d)", "_", s)              # '1·2단지' ≠ '12단지' → '1_2'
    s = re.sub(r"[·,.~()\[\]&?]", "", s)                     # '38-1'의 하이픈은 남긴다
    return s


def _strip_paren(name: str) -> str:
    return zone_key(re.sub(r"\(.*?\)", "", str(name)))


def _score(a: str, b: str) -> float:
    """같은 시군구 안의 두 키가 같은 단지일 가능성 (0~1, 보수적으로)."""
    from difflib import SequenceMatcher
    if not a or not b:
        return 0.0
    if a == b or (sorted(a) == sorted(b) and re.findall(r"\d+", a) == re.findall(r"\d+", b)):
        return 1.0                                            # 서빙고신동아 ↔ 신동아서빙고 (신반포12 ≠ 신반포21)
    short, long_ = sorted((a, b), key=len)
    rest = long_[: -len(short)]
    if long_.endswith(short) and len(short) >= 2 and len(rest) <= 4 and not re.search(r"\d", rest):
        return 0.95                                           # 목화 ↔ 여의도목화, 장미 ↔ 성수동장미
    if len(short) >= 3 and short in long_:
        i = long_.index(short)
        nxt = long_[i + len(short): i + len(short) + 1]
        if not (short[-1].isdigit() and nxt.isdigit()):       # 신반포2 ⊄ 신반포22
            return 0.9
    r = SequenceMatcher(None, a, b).ratio()
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):       # 번호가 다르면 다른 단지일 가능성이 큼
        r *= 0.8
    return r


APT_HINT = re.compile(r"아파트|APT|단지|주공|맨션|차$|\d차|현대|한양|우성|삼익|미성|한신|쌍용|대림|극동|럭키|신동아|롯데|삼성|선경|미도|시범|장미|경남|대우|동아|코오롱|진흥|청구|라이프|건영|삼호|한보|은마|목동|여의도|압구정|개포|잠실|반포|대치|상계|중계|월계|노원|도곡|수서|일원|가락|문정|오금|방이|명일|고덕|둔촌|이촌|서빙고|청담|삼풍|서초|방배|신반포")


# ---------------------------------------------------------------- 다운로드
# 접속(connect)은 짧게 기다리고 응답(read)은 넉넉히: 해외 IP를 막는 사이트에서 1분씩 멈추지 않게 한다.
TIMEOUT = (10, 90)


def make_session() -> requests.Session:
    """요청 단위 재시도(접속 실패·5xx·429 → 1~2회, 지수 대기)가 걸린 세션."""
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    retry = Retry(total=2, connect=1, read=1, status=2, backoff_factor=3,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=None, raise_on_status=False)
    s = requests.Session()
    s.headers.update(UA)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


SESSION = make_session()


def http_get(url: str, **kw) -> requests.Response:
    r = SESSION.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def http_post(url: str, data: dict, referer: str) -> requests.Response:
    r = SESSION.post(url, data=data, headers={"Referer": referer}, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def save(name: str, content: bytes) -> Path:
    RAW.mkdir(parents=True, exist_ok=True)
    p = RAW / name
    p.write_bytes(content)
    return p


def datagokr_file(page_id: str) -> tuple[bytes, str]:
    """data.go.kr 파일데이터: 상세 페이지에서 최신 atchFileId를 찾아 내려받는다. (bytes, 데이터셋 제목)"""
    html = http_get(f"https://www.data.go.kr/data/{page_id}/fileData.do").text
    m = re.search(r"atchFileId=(FILE_\d+)&(?:amp;)?fileDetailSn=(\d+)", html)
    if not m:
        raise RuntimeError(f"data.go.kr {page_id}: 다운로드 링크를 찾지 못함")
    title = re.search(r"<title>([^<|]+)", html).group(1).strip()
    url = (f"https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId={m.group(1)}"
           f"&fileDetailSn={m.group(2)}&insertDataPrcus=N")
    return http_get(url).content, title


def read_csv_any(b: bytes) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return pd.read_csv(io.BytesIO(b), encoding=enc, dtype=str)
        except UnicodeDecodeError:
            continue
    raise RuntimeError("CSV 인코딩 판별 실패")


def date_from_title(title: str) -> str | None:
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", title)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


# ---------------------------------------------------------------- 출처 1: 국토부
def load_molit() -> pd.DataFrame:
    b, title = datagokr_file("15160169")
    save("molit_national.csv", b)
    df = read_csv_any(b)
    df.columns = ["sido", "sgg", "zone", "stage", "type", "operator", "units"]
    df = df[df["sido"].map(norm_sido).isin(_SIDO.values())]
    df = df[df["type"].str.contains("재건축", na=False)]  # 단독주택 재건축은 병합 후 제외(분류 보조용)
    return pd.DataFrame({
        "region_sido": df["sido"].map(norm_sido),
        "region_sigungu": df["sgg"].str.strip(),
        "zone_name": df["zone"].str.strip(),
        "project_type": df["type"].str.replace(r"^\d+\)", "", regex=True),
        "operator_type": df["operator"].str.replace(r"^\d+\)", "", regex=True),
        "stage_raw": df["stage"],
        "planned_units": pd.to_numeric(df["units"].str.strip(), errors="coerce"),
        "source": "국토부_전국도시정비사업통합데이터",
        "source_updated_at": date_from_title(title),
    })


# ---------------------------------------------------------------- 출처 2: 서울 추진현황 엑셀
SEOUL_FILE_PAGE = "https://data.seoul.go.kr/dataList/OA-22856/F/1/datasetView.do"


def load_seoul_status() -> pd.DataFrame:
    html = http_get(SEOUL_FILE_PAGE).text
    files = re.findall(r"title=\"([^\"]+\.xlsx)\" onclick=\"javascript:downloadFile\('(\d+)'\)", html)
    if not files:
        raise RuntimeError("서울 추진현황: 파일 목록을 찾지 못함")
    fname, seq = max(files, key=lambda f: int(f[1]))  # 가장 최근 등록본
    inf_seq = re.search(r'name="frmFile".*?name="infSeq" value="(\d+)"', html, re.S).group(1)
    b = http_post("https://datafile.seoul.go.kr/bigfile/iot/inf/nio_download.do?&useCache=false",
                  {"infId": "OA-22856", "seqNo": "", "seq": seq, "infSeq": inf_seq}, SEOUL_FILE_PAGE).content
    save("seoul_status.xlsx", b)
    m = re.search(r"(\d{2})년\s*(\d{1,2})월", fname)
    asof = f"20{m.group(1)}-{int(m.group(2)):02d}-말" if m else None

    x = pd.read_excel(io.BytesIO(b), header=None, skiprows=4).iloc[:, :26]
    x.columns = ["code", "no", "sgg", "zone", "addr_jibun", "addr_road", "public_private", "jaechok",
                 "biz_type", "stage", "existing_units", "designated_first", "designated_at", "committee_at",
                 "union_approved_at", "arch_review_at", "plan_approved_first", "plan_approved_at",
                 "mgmt_approved_first", "mgmt_approved_at", "relocation_start", "relocation_end",
                 "started_at", "planned_units", "units_sale", "units_rent"]
    x = x[x["zone"].notna() & x["biz_type"].isin(["공동주택재건축", "아파트지구재건축", "단독주택재건축"])]

    def d(col):  # 최초/변경(최종) 중 최초 일자 기준 (변경일은 별도 보존)
        return pd.to_datetime(x[col], errors="coerce").dt.date

    return pd.DataFrame({
        "region_sido": "서울특별시",
        "region_sigungu": x["sgg"],
        "zone_name": x["zone"].str.strip(),
        "project_type": x["biz_type"],
        "operator_type": x["public_private"],
        "stage_raw": x["stage"],
        "address": x["addr_jibun"],
        "existing_units": pd.to_numeric(x["existing_units"], errors="coerce"),
        "planned_units": pd.to_numeric(x["planned_units"], errors="coerce"),
        "units_sale": pd.to_numeric(x["units_sale"], errors="coerce"),
        "units_rent": pd.to_numeric(x["units_rent"], errors="coerce"),
        "designated_at": d("designated_first"),
        "designated_changed_at": d("designated_at"),
        "committee_at": d("committee_at"),
        "union_approved_at": d("union_approved_at"),
        "arch_review_at": d("arch_review_at"),
        "plan_approved_at": d("plan_approved_first"),
        "mgmt_approved_at": d("mgmt_approved_first"),
        "relocation_started_at": d("relocation_start"),
        "started_at": d("started_at"),
        "seoul_code": x["code"],
        "source": "서울시_정비사업추진현황(OA-22856)",
        "source_updated_at": asof,
    })


# ---------------------------------------------------------------- 출처 2b: 서울 정비사업 정보몽땅 사업현황
MONGTTANG_LIST = "https://cleanup.seoul.go.kr/cleanup/bsnssttus/lscrMainIndx.do"


def load_seoul_mongttang() -> pd.DataFrame:
    """정비사업 정보몽땅 '사업현황' 공개 목록: 서울 전체 사업장의 현재 진행단계·조합원수·조합 카페 주소.

    분기 엑셀(OA-22856)보다 자주 바뀌고 안전진단·분양·이전고시·해산 같은 단계도 있어 서울 '현재 단계'의 1차 출처.
    공개 목록 페이지만 읽고(약 12쪽), 요청 사이에 쉰다. 재건축(공동·단독 미구분)만 가져온다.
    """
    import html as html_mod
    import time
    rows, page = [], 1
    while True:
        text = http_get(MONGTTANG_LIST, params={"cpage": page, "pageSize": 100}).text
        got = 0
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
            cells = [html_mod.unescape(re.sub(r"<[^>]+>|\s+", " ", c)).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(cells) < 8:
                continue
            got += 1
            cafe = re.search(r"cafeOpenPopup\('([^']+)'\)", tr)
            rows.append({"no": cells[0], "gu": cells[1], "kind": cells[2], "name": cells[3], "addr": cells[4],
                         "stage": cells[5], "members": cells[6], "cafe": cafe.group(1) if cafe else None})
        if got < 100 or page > 40:
            break
        page += 1
        time.sleep(0.7)
    d = pd.DataFrame(rows)
    save("seoul_mongttang.csv", d.to_csv(index=False).encode("utf-8-sig"))
    d = d[d["kind"].eq("재건축")].copy()
    d["zone"] = (d["name"].str.replace(r"\((?:신탁|사업대행|공공)[^)]*\)", "", regex=True)
                 .str.replace(r"\s*(주택)?\s*재건축\s*(정비)?\s*사업(\s*조합(\s*설립\s*추진위원회)?)?(\s*청산위원회)?\s*$", "", regex=True)
                 .str.replace(r"\s*정비사업\s*$", "", regex=True).str.strip())
    # 같은 사업장이 두 번 실린 경우: 자치구 + 대표지번 + 이름이 같으면 가장 진전된 단계(같으면 조합원 많은 쪽) 하나만 남긴다
    d["_key"] = d["gu"] + "|" + d["addr"].str.replace(r"\s+", "", regex=True) + "|" + d["zone"].map(zone_key)
    d["_order"] = d["stage"].map(lambda s: STAGE_ORDER.get(norm_stage(s) or "", -1))
    d["_members"] = pd.to_numeric(d["members"].str.replace(r"[^\d]", "", regex=True), errors="coerce").fillna(0)
    d = d.sort_values(["_key", "_order", "_members"], ascending=[True, False, False])
    alt = d.groupby("_key")["cafe"].agg(lambda s: ", ".join(c for c in s.tolist()[1:] if c))
    d = d.drop_duplicates("_key", keep="first")
    d["alt_cafes"] = d["_key"].map(alt).replace("", None)
    return pd.DataFrame({
        "region_sido": "서울특별시",
        "region_sigungu": d["gu"].str.strip(),
        "zone_name": d["zone"],
        "project_type": "재건축",
        "type_basis": "정보몽땅은 공동/단독 미구분",
        "stage_raw": d["stage"].str.strip(),
        "address": d["addr"].str.strip(),
        "members_count": pd.to_numeric(d["members"].str.replace(r"[^\d]", "", regex=True), errors="coerce"),
        "homepage": d["cafe"].map(lambda c: f"https://cleanup.seoul.go.kr/cafe/mainIndx.do?cafeUrl={c}" if c else None),
        "alt_cafes": d["alt_cafes"],
        "source_pk": d["cafe"].where(d["cafe"].notna(), "no" + d["no"]),
        "source": "서울시_정비사업정보몽땅_사업현황",
        "source_updated_at": None,  # 목록 페이지에 기준일이 없다(매일 확인). 날짜를 넣으면 레코드가 매일 바뀐다
    })


# ---------------------------------------------------------------- 출처 3: 서울플랜+
SEOUL_PLAN_PAGE = "https://data.seoul.go.kr/dataList/OA-23112/S/1/datasetView.do"


def load_seoul_plan() -> pd.DataFrame:
    b = http_post("https://datafile.seoul.go.kr/bigfile/iot/sheet/csv/download.do", {
        "srvType": "S", "infId": "OA-23112", "serviceKind": "1", "pageNo": "1", "gridTotalCnt": "",
        "ssUserId": "SAMPLE_VIEW", "strWhere": "", "strOrderby": "BASIC_INFO_SEQ DESC",
        "filterCol": "필터선택", "txtFilter": ""}, SEOUL_PLAN_PAGE).content
    save("seoul_plan.csv", b)
    d = read_csv_any(b)
    kind = d["소분류 코드명"]
    is_recon = kind.isin(["재건축(공동)", "재건축(단독)"])
    is_fast = kind.eq("신속통합기획") & d["사업명칭"].str.contains(APT_HINT, na=False) \
        & ~d["사업명칭"].str.contains(r"재개발|구역$|\d구역|번지|일대|역세권", na=False)
    d = d[is_recon | is_fast].copy()
    d["type_basis"] = kind[d.index].map(
        {"재건축(공동)": "출처 분류", "재건축(단독)": "출처 분류", "신속통합기획": "신통 대상지 — 단지명으로 재건축 추정"})
    return pd.DataFrame({
        "region_sido": "서울특별시",
        "region_sigungu": d["지차체 코드명"],
        "zone_name": d["사업명칭"].str.replace(r"\s*재건축(정비)?사업$", "", regex=True).str.strip(),
        "project_type": d["소분류 코드명"].replace({"재건축(공동)": "공동주택재건축"}),
        "type_basis": d["type_basis"],
        "stage_raw": d["추진단계 코드명"],
        "stage_date": pd.to_datetime(d["추진현황"].str[:10], errors="coerce").dt.date,
        "address": d["사업위치"],
        "planned_units": pd.to_numeric(d["공급세대"], errors="coerce"),
        "homepage": d["사업장 홈페이지 주소"],
        "site_area_m2": num(d["사업면적"]),
        "zoning_before": d["용도지역_기존"],
        "zoning_after": d["용도지역_변경"],
        "gfa_planned_m2": num(d["연면적"]),
        "far_planned": num(d["용적률"]),
        "bcr_planned": num(d["건폐률"]),
        "floors_planned": floors_above(d["층수"]),
        "height_planned_m": num(d["높이(m)"]),
        "units_sale": num(d["분양"]),
        "units_rent": num(d["임대(장기전세)"]),
        "source_pk": d["기본정보_pk"],
        "source": "서울시_서울플랜+정비사업현황(OA-23112)",
        "source_updated_at": date.today().isoformat(),
    })


# ---------------------------------------------------------------- 공통: 일자 파싱
def parse_date(v):
    """'20250108', '2025-01-08', '07.07.06.', '14.02.17.\\n18.09.03.'(최초\\n변경) → date. 첫 일자 사용."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if hasattr(v, "date") and not isinstance(v, str):
        return v.date()
    s = str(v).strip()
    m = re.search(r"(19|20)(\d{2})[-./]?\s*(\d{1,2})[-./]?\s*(\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1) + m.group(2)), int(m.group(3)), int(m.group(4))
    else:
        m = re.search(r"(?<!\d)(\d{2})\.\s*(\d{1,2})\.\s*(\d{1,2})", s)  # 부천: YY.MM.DD.
        if not m:
            return None
        yy = int(m.group(1))
        y, mo, d = (2000 + yy if yy < 70 else 1900 + yy), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def dates(series: pd.Series) -> pd.Series:
    return series.map(parse_date)


def num(series: pd.Series) -> pd.Series:
    """'268.32' / '40이하' / '1,234' / ' ' → 첫 숫자(float). 0이나 빈 값은 '모름'으로 본다."""
    def one(v):
        m = re.search(r"\d[\d,]*(?:\.\d+)?", str(v)) if v is not None else None
        x = float(m.group(0).replace(",", "")) if m else None
        return x if x else None
    return series.map(one)


def floors_above(series: pd.Series) -> pd.Series:
    """층수 표기('2/33', '지하4층/지상49층', '12동/23층', '35') → 지상 최고층."""
    def one(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        t = str(v)
        m = re.search(r"지상\s*(\d+)", t) or re.search(r"(\d+)\s*층", t) or re.search(r"/\s*(\d+)", t) \
            or re.fullmatch(r"\s*(\d+)\s*", t)
        return int(m.group(1)) if m else None
    return series.map(one)


def buildings(series: pd.Series) -> pd.Series:
    """'12동/23층' → 12"""
    return series.map(lambda v: int(m.group(1)) if (m := re.search(r"(\d+)\s*동", str(v))) else None)


# ---------------------------------------------------------------- 출처 4: 경기도 일반정비사업
GG_INF_ID = "S62GFEEN7JMLMA0PH6CF19108891"


def load_gyeonggi() -> pd.DataFrame:
    """경기데이터드림 시트 조회 API(웹 화면이 쓰는 것과 동일, 로그인 불필요)로 전체 행을 받는다."""
    s = make_session()  # CSRF 토큰이 쿠키 세션에 묶여 있어 전용 세션을 쓴다
    page = f"https://data.gg.go.kr/portal/data/service/selectServicePage.do?infId={GG_INF_ID}&infSeq=1"
    html = s.get(page, timeout=TIMEOUT).text
    token = re.search(r'name="_csrf" content="([^"]+)"', html).group(1)
    meta = s.get("https://data.gg.go.kr/portal/data/sheet/selectSheetMeta.do",
                 params={"infId": GG_INF_ID, "infSeq": 1}, timeout=TIMEOUT).json()
    r = s.post("https://data.gg.go.kr/portal/data/sheet/searchSheetData.do?page=1",
               data={"_csrf": token, "CSRFToken": token, "rows": 20000, "infId": GG_INF_ID, "infSeq": 1},
               headers={"X-CSRF-TOKEN": token, "X-Requested-With": "XMLHttpRequest", "Referer": page},
               timeout=TIMEOUT)
    r.raise_for_status()
    save("gyeonggi_general.json", r.content)
    cols = {c["SaveName"]: c["Header"] for c in meta["columns"] if c["SaveName"] != "number"}
    g = pd.DataFrame(r.json()["data"]).rename(columns=cols)
    g = g.apply(lambda c: c.str.strip() if c.dtype == object else c).replace("", None)
    g = g[g["사업유형"].astype(str).str.contains("재건축")]
    upd = re.search(r"(20\d{2}[-.]\d{2}[-.]\d{2})", html)
    return pd.DataFrame({
        "region_sido": "경기도",
        "region_sigungu": g["시군명"],
        "zone_name": g["정비구역명"],
        "project_type": "재건축",
        "type_basis": "경기도 자료는 공동/단독 구분 없음",
        "operator_type": g["사업시행자"],
        "stage_raw": g["사업단계"],
        "address": g["위치"],
        "existing_units": pd.to_numeric(g["(기존주택)세대수"], errors="coerce"),
        "existing_built_year": num(g["(기존주택)준공년도"]),
        "existing_buildings": num(g["(기존주택)동수"]),
        "site_area_m2": num(g["구역면적(㎡)"]),
        "far_existing": num(g["(기존)용적률"]),
        "far_planned": num(g["(신축)용적률"]),
        "units_sale": num(g["일반분양세대수"]),
        "units_rent": num(g["임대세대수"]),
        "owners_count": num(g["토지등소유자수(명)"]),
        "members_count": num(g["조합원수(명)"]),
        "planned_units": pd.to_numeric(g["사업시행세대수총계"], errors="coerce"),
        "designated_at": dates(g["정비구역지정일자(최초지정)"]),
        "designated_changed_at": dates(g["정비구역지정일자(변경지정)"]),
        "committee_at": dates(g["추진위승인일자"]),
        "union_approved_at": dates(g["조합설립인가일자"]),
        "plan_approved_at": dates(g["사업시행인가일자"]),
        "mgmt_approved_at": dates(g["관리처분인가일자"]),
        "started_at": dates(g["착공일자"]),
        "completed_at": dates(g["준공일자"]),
        "source": "경기도_일반정비사업추진현황",
        "source_updated_at": upd.group(1) if upd else None,
    })


# ---------------------------------------------------------------- 출처 5: 부천시
BUCHEON_POST = ("https://www.bucheon.go.kr/site/program/board/basicboard/view"
                "?menuid=173001002003&pagesize=10&boardtypeid=28574&boardid=1201659")


def load_bucheon() -> pd.DataFrame:
    html = http_get(BUCHEON_POST).text
    m = re.search(r'fileDownload\?dirname=([^&"]+)&(?:amp;)?filename=([^"\']+?\.xlsx)', html)
    if not m:
        raise RuntimeError("부천시 게시글에서 엑셀 링크를 찾지 못함")
    url = f"https://www.bucheon.go.kr/site/inc/file/fileDownload?dirname={m.group(1)}&filename={m.group(2)}"
    b = http_get(url).content
    save("bucheon.xlsx", b)
    fname = requests.utils.unquote(m.group(2))
    asof = re.search(r"(20\d{2})\.(\d{1,2})\.(\d{1,2})", fname)
    x = pd.read_excel(io.BytesIO(b), sheet_name="재건축", header=None, skiprows=3, dtype=object).iloc[:, :21]
    x.columns = ["no", "zone", "contact", "loc", "units", "units_sale", "units_rent", "bldg", "bcr", "far",
                 "committee", "designated", "union", "union_head", "plan", "mgmt", "started", "sale_appr",
                 "completed", "dissolved", "stage"]
    x = x[pd.to_numeric(x["no"], errors="coerce").notna()]

    def clean(s):
        return s.astype(str).str.replace(r"\s+", " ", regex=True).str.strip()

    return pd.DataFrame({
        "region_sido": "경기도",
        "region_sigungu": "부천시",
        "zone_name": clean(x["zone"]).str.replace(r"\s*재건축$", "", regex=True),
        "project_type": "재건축",
        "type_basis": "부천시 재건축 시트",
        "stage_raw": clean(x["stage"]),
        "address": clean(x["loc"]).str.replace(r"\s*\(.*$", "", regex=True),
        "planned_units": pd.to_numeric(x["units"], errors="coerce"),
        "units_sale": pd.to_numeric(x["units_sale"], errors="coerce"),
        "units_rent": pd.to_numeric(x["units_rent"], errors="coerce"),
        "site_area_m2": num(x["loc"].astype(str).str.extract(r"\(([\d,\.]+)\)")[0]),
        "buildings_planned": buildings(x["bldg"]),
        "floors_planned": floors_above(x["bldg"]),
        "bcr_planned": num(x["bcr"]),
        "far_planned": num(x["far"]),
        "committee_at": dates(x["committee"]),
        "designated_at": dates(x["designated"]),
        "union_approved_at": dates(x["union"]),
        "plan_approved_at": dates(x["plan"]),
        "mgmt_approved_at": dates(x["mgmt"]),
        "started_at": dates(x["started"]),
        "completed_at": dates(x["completed"]),
        "source": "부천시_정비사업추진현황",
        "source_updated_at": "-".join(f"{int(p):02d}" for p in asof.groups()) if asof else None,
    })


# ---------------------------------------------------------------- 출처 6~7: 인천
def load_incheon() -> pd.DataFrame:
    b, title = datagokr_file("15055212")
    save("incheon_city.csv", b)
    d = read_csv_any(b)
    d.columns = [c.replace(" ", "").strip() for c in d.columns]
    d = d[d["사업유형"].str.contains("재건축", na=False)]
    return pd.DataFrame({
        "region_sido": "인천광역시",
        "region_sigungu": d["구명"].str.strip(),
        "zone_name": d["구역명"].str.strip(),
        "project_type": "재건축",
        "type_basis": "인천시 자료는 공동/단독 구분 없음",
        "stage_raw": d["진행단계"],
        "address": d["위치"],
        "source": "인천시_도시및주거환경정비사업추진현황",
        "source_updated_at": date_from_title(title),
    })


def load_incheon_detail() -> pd.DataFrame:
    """인천시 추정분담금정보시스템 정비사업개요: 면적·동수·세대수·건폐율·용적률(계획)."""
    b, title = datagokr_file("15080866")
    save("incheon_detail.csv", b)
    d = read_csv_any(b)
    d.columns = [c.replace(" ", "").strip() for c in d.columns]
    d = d[d["사업유형"].str.contains("재건축", na=False)]
    return pd.DataFrame({
        "region_sido": "인천광역시",
        "region_sigungu": d["구별"].str.strip(),
        "zone_name": d["구역명"].str.strip(),
        "project_type": "재건축",
        "stage_raw": d["추진단계"],
        "address": d["위치"],
        "site_area_m2": num(d["면적(제곱미터)"]),
        "buildings_planned": num(d["동수"]),
        "planned_units": num(d["세대수"]),
        "bcr_planned": num(d["건폐율(퍼센트)"]),
        "far_planned": num(d["용적률(퍼센트)"]),
        "source": "인천시_정비사업개요(추정분담금)",
        "source_updated_at": date_from_title(title),
    })


def load_michuhol() -> pd.DataFrame:
    b, title = datagokr_file("15150904")
    save("incheon_michuhol.csv", b)
    d = read_csv_any(b)
    d = d[d["사업유형"].str.contains("재건축", na=False)]
    return pd.DataFrame({
        "region_sido": "인천광역시",
        "region_sigungu": "미추홀구",
        "zone_name": d["구역명"].str.strip(),
        "project_type": "재건축",
        "stage_raw": d["현 추진사항"],
        "address": d["위치"],
        "designated_at": dates(d["정비구역지정"]),
        "committee_at": dates(d["추진위원회승인"]),
        "union_approved_at": dates(d["조합설립인가"]),
        "plan_approved_at": dates(d["사업시행인가"]),
        "mgmt_approved_at": dates(d["관리처분계획인가"]),
        "started_at": dates(d["착공"]),
        "homepage": d["사무실 인터넷주소"],
        "source": "미추홀구_정비사업추진현황",
        "source_updated_at": date_from_title(title),
    })


def load_yeonsu() -> pd.DataFrame:
    b, title = datagokr_file("15039862")
    save("incheon_yeonsu.csv", b)
    d = read_csv_any(b)
    d = d[d["구분"].str.contains("재건축", na=False)]
    return pd.DataFrame({
        "region_sido": "인천광역시",
        "region_sigungu": "연수구",
        "zone_name": d["구역명"].str.strip(),
        "project_type": "재건축",
        "address": d["대지위치"],
        "planned_units": pd.to_numeric(d["계획세대수"], errors="coerce"),
        "designated_at": dates(d["정비구역고시"]),
        "union_approved_at": dates(d["조합설립인가일"]),
        "plan_approved_at": dates(d["사업시행인가일"]),
        "mgmt_approved_at": dates(d["관리처분인가일"]),
        "source": "연수구_도시정비사업현황",
        "source_updated_at": date_from_title(title),
    })


# ---------------------------------------------------------------- 출처 목록(우선순위 순) + 변경 감지용 probe
@dataclass
class Source:
    key: str                                    # 상태 파일 이름(state/source_rows/<key>.jsonl)
    name: str                                   # 각 loader가 채우는 source 열 값과 같아야 한다
    load: Callable[[], pd.DataFrame]
    probe: Callable[[], str | None] | None = None  # 파일을 받지 않고 '바뀌었는지'만 알려주는 값(파일ID·파일명 등)
    host: str = ""                              # 접속 실패 시 같은 호스트의 다른 출처는 이번 회차에 건너뛴다


def _probe_datagokr(page_id: str) -> str | None:
    html = http_get(f"https://www.data.go.kr/data/{page_id}/fileData.do").text
    m = re.search(r"atchFileId=(FILE_\d+)&(?:amp;)?fileDetailSn=(\d+)", html)
    t = re.search(r"<title>([^<|]+)", html)
    return f"{m.group(1)}#{m.group(2)}|{t.group(1).strip() if t else ''}" if m else None


def probe_seoul_status() -> str | None:
    files = re.findall(r"title=\"([^\"]+\.xlsx)\" onclick=\"javascript:downloadFile\('(\d+)'\)",
                       http_get(SEOUL_FILE_PAGE).text)
    return "|".join(max(files, key=lambda f: int(f[1]))) if files else None


def probe_bucheon() -> str | None:
    m = re.search(r'filename=([^"\']+?\.xlsx)', http_get(BUCHEON_POST).text)
    return m.group(1) if m else None


SOURCES = [
    Source("seoul_status", "서울시_정비사업추진현황(OA-22856)", load_seoul_status, probe_seoul_status, host="data.seoul.go.kr"),
    Source("seoul_plan", "서울시_서울플랜+정비사업현황(OA-23112)", load_seoul_plan, host="datafile.seoul.go.kr"),   # 갱신일 메타데이터가 없어 내용 해시로 판단
    Source("seoul_mongttang", "서울시_정비사업정보몽땅_사업현황", load_seoul_mongttang, host="cleanup.seoul.go.kr"),
    Source("bucheon", "부천시_정비사업추진현황", load_bucheon, probe_bucheon, host="www.bucheon.go.kr"),
    Source("gyeonggi", "경기도_일반정비사업추진현황", load_gyeonggi, host="data.gg.go.kr"),               # 〃
    Source("incheon", "인천시_도시및주거환경정비사업추진현황", load_incheon, lambda: _probe_datagokr("15055212"), host="www.data.go.kr"),
    Source("incheon_detail", "인천시_정비사업개요(추정분담금)", load_incheon_detail,
           lambda: _probe_datagokr("15080866"), host="www.data.go.kr"),
    Source("michuhol", "미추홀구_정비사업추진현황", load_michuhol, lambda: _probe_datagokr("15150904"), host="www.data.go.kr"),
    Source("yeonsu", "연수구_도시정비사업현황", load_yeonsu, lambda: _probe_datagokr("15039862"), host="www.data.go.kr"),
    Source("molit", "국토부_전국도시정비사업통합데이터", load_molit, lambda: _probe_datagokr("15160169"), host="www.data.go.kr"),
]
PRIORITY = {s.name: i for i, s in enumerate(SOURCES)}  # 앞일수록 우선(일자·세대수 등은 앞 출처 값을 쓴다)


# ---------------------------------------------------------------- 출처 행 → 저장용 payload
VOLATILE = {"source_updated_at"}  # 행이 '바뀌었는지' 비교할 때 제외하는 열


def jsonable(v):
    """pandas/numpy/date 값을 JSON에 넣을 수 있는 값으로. 저장·비교 결과가 매번 같도록 형식을 고정한다."""
    if v is None or (not isinstance(v, (str, list, tuple, dict)) and pd.isna(v)):
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (list, dict)):
        return v
    if isinstance(v, numbers.Integral):
        return int(v)
    if isinstance(v, numbers.Real):
        return int(v) if float(v).is_integer() else float(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    if isinstance(v, str):
        return v.strip() or None
    return str(v)


def row_keys(df: pd.DataFrame) -> list[str]:
    """출처 안에서 한 행을 계속 같은 행으로 알아보는 키. 출처가 고유번호를 주면 그것을, 아니면 지역+구역명."""
    keys, seen = [], {}
    for _, r in df.iterrows():
        if jsonable(r.get("seoul_code")) is not None:
            k = f"code:{jsonable(r['seoul_code'])}"
        elif jsonable(r.get("source_pk")) is not None:
            k = f"pk:{jsonable(r['source_pk'])}"
        else:
            k = f"{sgg_key(r['region_sigungu'], r['region_sido'])}|{zone_key(r['zone_name'])}"
        seen[k] = seen.get(k, 0) + 1
        keys.append(k if seen[k] == 1 else f"{k}#{seen[k]}")
    return keys


def fetch_payloads(src: Source) -> dict[str, dict]:
    """출처를 내려받아 {row_key: payload}로."""
    df = src.load().reset_index(drop=True)
    keys = row_keys(df)
    return {k: {c: jsonable(v) for c, v in row.items()} for k, (_, row) in zip(keys, df.iterrows())}


def row_hash(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k not in VOLATILE}
    return hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def rows_frame(items: list[tuple[str, dict]]) -> pd.DataFrame:
    """[(row_key, payload)] → 병합 계산용 DataFrame. 전체 구축과 증분 갱신이 똑같은 입력 형태를 쓰게 한다."""
    df = pd.DataFrame([{**p, "_row_key": k} for k, p in items])
    for c in ["address", "stage_date", "stage_raw", "project_type", "source_updated_at", "zone_name"]:
        if c not in df:
            df[c] = None
    df["stage_std"] = df["stage_raw"].map(norm_stage)
    df["_area"] = df["region_sido"] + "|" + pd.Series(
        [sgg_key(g, s) for g, s in zip(df["region_sigungu"], df["region_sido"])], index=df.index)
    return df


# ---------------------------------------------------------------- 병합
STAGE_DATE_COL = {"정비구역지정": "designated_at", "추진위구성": "committee_at", "조합설립인가": "union_approved_at",
                  "건축심의": "arch_review_at", "사업시행인가": "plan_approved_at", "관리처분인가": "mgmt_approved_at",
                  "착공": "started_at", "준공": "completed_at"}
DETAIL_COLS = [  # 단지·사업 지표 (planned = 정비계획상 신축 계획값, existing = 현재 단지)
    "site_area_m2", "zoning_before", "zoning_after", "far_existing", "far_planned", "bcr_planned",
    "gfa_planned_m2", "floors_planned", "height_planned_m", "buildings_planned", "units_sale", "units_rent",
    "existing_built_year", "existing_buildings", "owners_count", "members_count"]
ENRICH_COLS = [  # 오픈API 보강(enrich.py): 건축물대장·실거래가 — 상태 레코드와 따로 저장, 내보낼 때만 붙인다
    "bld_name", "land_area_m2", "bld_households", "land_share_m2", "bld_far", "bld_bcr", "bld_approved",
    "bld_buildings", "trade_basis", "trade_count_12m", "trade_latest_date", "trade_latest_price",
    "trade_latest_area", "trade_median_ppp", "trades_by_area_text"]
DATE_COLS = ["designated_at", "committee_at", "union_approved_at", "arch_review_at", "plan_approved_at",
             "mgmt_approved_at", "relocation_started_at", "started_at", "completed_at"]
OUT_COLS = ["project_id", "region_sido", "region_sigungu", "zone_name", "aliases", "name_variants", "project_type",
            "stage", "stage_order", "stage_date", *DATE_COLS, "designated_changed_at", "existing_units",
            "planned_units", *DETAIL_COLS, *ENRICH_COLS, "operator_type", "address", "homepage", "type_basis",
            "stage_by_source", "stage_source", "stage_evidence", "sources", "source_updated_at", "match_note",
            "status", "search_keys"]


MANUAL_FILE = BASE / "manual_aliases.csv"


def load_manual() -> pd.DataFrame:
    """manual_aliases.csv: 자동으로 못 묶은 같은 사업장, 공식 자료에 없는 통칭(예: '압구정 현대')을 사람이 추가."""
    if not MANUAL_FILE.exists():
        return pd.DataFrame(columns=["_area", "name_key", "alias", "alias_key"])
    m = pd.read_csv(MANUAL_FILE, dtype=str, encoding="utf-8-sig").dropna(subset=["name", "alias"])
    m["_area"] = m["region_sido"] + "|" + [sgg_key(g, s) for g, s in zip(m["region_sigungu"], m["region_sido"])]
    m["name_key"] = m["name"].map(zone_key)
    m["alias_key"] = m["alias"].map(zone_key)
    return m


def cluster_keys(rows: pd.DataFrame) -> pd.Series:
    """표기가 다른 같은 단지를 하나의 키로 묶는다. (초기 구축 때만 사용 — 이후엔 연결을 고정한다)

    같은 시군구 안에서만 비교하고, 같은 출처에 함께 실린 두 사업장은 서로 다른 사업장으로 보아
    절대 합치지 않는다. 서로가 서로의 유일한 최선 후보일 때만 합친다(모호하면 합치지 않음).
    """
    srcs = (rows["source"] + "/" + rows["project_type"].fillna("")).groupby(rows["_k"]).agg(set)
    alt = rows.groupby("_k")["zone_name"].agg(lambda s: name_keys(s))
    parent = {k: k for k in srcs.index}

    def find(k):
        while parent[k] != k:
            k = parent[k]
        return k

    for _, g in rows.groupby("_area"):
        keys = list(dict.fromkeys(g["_k"]))
        changed = True
        while changed:  # 서로가 서로의 유일한 최선 후보일 때만 합치고, 더 이상 안 바뀔 때까지 반복
            changed = False
            clusters: dict[str, list[str]] = {}
            for k in keys:
                clusters.setdefault(find(k), []).append(k)
            c_src = {r: set().union(*(srcs[k] for k in m)) for r, m in clusters.items()}
            c_alt = {r: set().union(*(alt[k] for k in m)) for r, m in clusters.items()}
            best: dict[str, tuple[float, str | None, bool]] = {}
            roots = list(clusters)
            for r in roots:
                scored = sorted(((max(_score(x, y) for x in c_alt[r] for y in c_alt[o]), o)
                                 for o in roots if o != r and not (c_src[r] & c_src[o])), reverse=True)
                if scored and scored[0][0] >= 0.75:
                    tie = len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.03
                    best[r] = (scored[0][0], scored[0][1], tie)
            for r, (sc, o, tie) in best.items():
                if tie or o not in best or best[o][1] != r or best[o][2]:
                    continue
                if find(r) != r or find(o) != o:
                    continue
                parent[o] = r
                changed = True
    # 수동 지정: name과 alias가 둘 다 사업장으로 존재하면 하나로 합친다
    for _, m in load_manual().iterrows():
        ka, kb = f"{m['_area']}|{m['name_key']}", f"{m['_area']}|{m['alias_key']}"
        if ka in parent and kb in parent and find(ka) != find(kb):
            parent[find(kb)] = find(ka)
    return rows["_k"].map(find)


def name_keys(names) -> set[str]:
    return {_strip_paren(x) for x in names} | {zone_key(x) for x in names}


def initial_project_ids(rows: pd.DataFrame) -> pd.Series:
    """전체 행을 처음 묶어 project_id를 붙인다(초기 구축). 이후 증분 갱신에서는 이 연결을 바꾸지 않는다."""
    rows = rows.copy()
    rows["_k"] = rows["_area"] + "|" + rows["zone_name"].map(zone_key)
    rows["_k"] = cluster_keys(rows)
    ids: dict[str, str] = {}
    for k, g in rows.groupby("_k", sort=False):
        top = _by_priority(g).iloc[0]
        ids[k] = unique_project_id(top, set(ids.values()))
    return rows["_k"].map(ids)


def unique_project_id(row, used: set[str]) -> str:
    base = pid = make_project_id(row)
    n = 2
    while pid in used:
        pid, n = f"{base}-{n}", n + 1
    return pid


def _by_priority(g: pd.DataFrame) -> pd.DataFrame:
    return g.assign(_p=g["source"].map(PRIORITY).fillna(99)).sort_values(["_p", "_row_key"], kind="stable")


def _txt(v) -> str:
    """빈 값을 NaN/None 구분 없이 '-'로 (어떤 행 묶음으로 계산해도 같은 문자열이 나오게)."""
    v = jsonable(v)
    return "-" if v is None else str(v)


def build_record(g: pd.DataFrame, manual: pd.DataFrame) -> dict:
    """한 사업장에 연결된 출처 행들 → 사업장 1행. 같은 입력이면 항상 같은 결과."""
    g = _by_priority(g)
    rec = {}
    for c in OUT_COLS:  # 열이 없어도 키는 항상 만든다(입력 행 구성과 무관하게 같은 모양의 레코드)
        v = g[c].dropna() if c in g else ()
        rec[c] = v.iloc[0] if len(v) else None
    # 현재 단계 = 출처들 중 가장 진전된 단계 (출처마다 기준일이 달라서)
    g_st = g.dropna(subset=["stage_std"])
    if len(g_st):
        best = g_st.loc[g_st["stage_std"].map(STAGE_ORDER).idxmax()]
        rec["stage"] = best["stage_std"]
        rec["stage_order"] = STAGE_ORDER[best["stage_std"]]
        rec["stage_source"] = best["source"]                       # 이 단계를 알려준 출처
        rec["stage_evidence"] = best.get("homepage") if "정보몽땅" in best["source"] else None
        # 최신 단계를 알려준 출처(서울플랜+ 등)의 단계 일자로 비어 있는 단계별 일자를 채운다
        col = STAGE_DATE_COL.get(best["stage_std"])
        if col and not rec.get(col) and pd.notna(best.get("stage_date")):
            rec[col] = best["stage_date"]
    rec["stage_by_source"] = "; ".join(f"{s.split('_')[0]}={_txt(r)}" for s, r in zip(g["source"], g["stage_raw"]))
    rec["name_variants"] = " | ".join(dict.fromkeys(g["zone_name"]))
    rec["_aliases"] = build_aliases(list(dict.fromkeys(g["zone_name"])),
                                    list(dict.fromkeys(g["address"].dropna())), rec.get("region_sigungu"))
    keys = set(g["zone_name"].map(zone_key))
    for alias in manual.loc[manual["_area"].eq(g["_area"].iloc[0]) & manual["name_key"].isin(keys), "alias"]:
        if alias not in dict(rec["_aliases"]):
            rec["_aliases"].append((alias, "수동 별칭"))
    rec["sources"] = "; ".join(g["source"])
    rec["source_updated_at"] = "; ".join(f"{s.split('(')[0]}:{_txt(u)}" for s, u in zip(g["source"], g["source_updated_at"]))
    rec["match_note"] = "단일 출처" if len(g) == 1 else f"{len(g)}개 출처 병합"
    types = g["project_type"].dropna().astype(str)
    rec["project_type"] = " / ".join(dict.fromkeys(types))
    # 아파트 재건축만 남긴다: 어느 출처든 공동주택·아파트로 분류했거나, 이름이 공동주택을 가리키거나,
    # 단독주택이라고 한 출처가 없을 때. (국토부는 '○○아파트'도 단독주택으로 분류한 사례가 있어 이름을 함께 본다)
    apt_name = g["zone_name"].astype(str).str.contains(r"아파트|APT|(?<=[가-힣])A(?![a-zA-Z])|단지|주공|맨션|맨숀|연립")
    rec["_apartment"] = bool(types.str.contains("공동|아파트|신속통합").any() or apt_name.any()
                             or not types.str.contains("단독").any())
    rec["aliases"] = " | ".join(a for a, _ in rec["_aliases"])
    rec["search_keys"] = " ".join(dict.fromkeys(search_key(a) for a, _ in rec["_aliases"]))
    rec["status"] = "활성"
    rec = {k: (v if k == "_aliases" else jsonable(v)) for k, v in rec.items()}
    rec["_aliases"] = [list(a) for a in rec["_aliases"]]
    return rec


def records_frame(recs: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """사업장 레코드들 → (아파트 재건축 표, 단독주택이라 제외한 표)."""
    res = pd.DataFrame(recs)
    excluded = res[~res["_apartment"].astype(bool)].reindex(
        columns=["project_id", "region_sido", "region_sigungu", "zone_name", "name_variants", "project_type",
                 "stage", "sources"])
    res = res[res["_apartment"].astype(bool)].reindex(columns=OUT_COLS + ["_aliases", "trades_by_area"])
    for c in ["stage_order", "existing_units", "planned_units", "units_sale", "units_rent", "floors_planned",
              "buildings_planned", "existing_built_year", "existing_buildings", "owners_count", "members_count",
              "bld_households", "bld_buildings", "trade_count_12m", "trade_latest_price", "trade_latest_area",
              "trade_median_ppp"]:
        res[c] = pd.to_numeric(res[c], errors="coerce").astype("Int64")
    res = res.sort_values(["region_sido", "region_sigungu", "stage_order", "zone_name", "project_id"],
                          ascending=[False, True, False, True, True])
    return res, excluded


def write_outputs(res: pd.DataFrame, excluded: pd.DataFrame, tag: str) -> None:
    OUT.mkdir(exist_ok=True)
    path = OUT / f"capital_area_apartment_reconstruction_{tag}.csv"
    res.drop(columns=["_aliases", "trades_by_area"]).to_csv(path, index=False, encoding="utf-8-sig", lineterminator="\n")
    at = alias_table(res)
    at.to_csv(OUT / f"reconstruction_aliases_{tag}.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    excluded.to_csv(OUT / f"excluded_single_house_{tag}.csv", index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[저장] 사업장 {len(res)}개 → {path.name}, 별칭 {len(at)}개, 제외 {len(excluded)}개")
    if tag == "latest":
        write_site_data(res)


SITE_DATA = BASE / "docs" / "data" / "projects.json"
SITE_FIELDS = ["project_id", "region_sido", "region_sigungu", "zone_name", "name_variants", "project_type", "stage",
               "stage_order", "stage_date", *DATE_COLS, "existing_units", "planned_units", *DETAIL_COLS,
               *[c for c in ENRICH_COLS if c != "trades_by_area_text"], "trades_by_area",
               "operator_type", "address", "homepage", "stage_source", "stage_evidence", "stage_by_source",
               "sources", "source_updated_at", "search_keys"]


def write_site_data(res: pd.DataFrame) -> None:
    """정적 페이지(docs/)가 읽는 JSON. 빈 값은 빼서 크기를 줄이고, 키 순서를 고정해 diff가 작게 나오게 한다."""
    rows = []
    for _, r in res.iterrows():
        row = {c: jsonable(r.get(c)) for c in SITE_FIELDS}
        row["aliases"] = [a for a, _ in r["_aliases"]]
        # 세대당 구역면적: 대지지분의 참고치(구역에 도로·공원이 포함될 수 있어 실제 대지지분보다 클 수 있음)
        if row.get("site_area_m2") and row.get("existing_units"):
            row["site_area_per_unit_m2"] = round(row["site_area_m2"] / row["existing_units"], 1)
        rows.append({k: v for k, v in row.items() if v not in (None, "", [])})
    data = {"generated_at": date.today().isoformat(), "count": len(rows), "projects": rows,
            "sources": [{"key": s.key, "name": s.name} for s in SOURCES]}
    SITE_DATA.parent.mkdir(parents=True, exist_ok=True)
    SITE_DATA.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                         encoding="utf-8", newline="\n")
    print(f"[저장] 페이지 데이터 {len(rows)}개 → {SITE_DATA.relative_to(BASE)}")


def full_build(sources: list[Source] = SOURCES) -> tuple[dict[str, dict[str, dict]], pd.Series, list[dict]]:
    """모든 출처를 처음부터 읽어 묶는다. → ({출처: {row_key: payload}}, 행별 project_id, 사업장 레코드)"""
    fetched: dict[str, dict[str, dict]] = {}
    for src in sources:
        print(f"[수집] {src.name}")
        try:
            fetched[src.name] = fetch_payloads(src)
        except Exception as e:  # 한 출처가 실패해도 나머지로 결과를 만든다
            print(f"   ! 실패: {e}", file=sys.stderr)
            continue
        print(f"   → {len(fetched[src.name])}건")
    rows = rows_frame([(k, p) for s in fetched.values() for k, p in s.items()])
    rows["project_id"] = initial_project_ids(rows)
    manual = load_manual()
    recs = [{**build_record(g, manual), "project_id": pid} for pid, g in rows.groupby("project_id", sort=False)]
    return fetched, rows.set_index(["source", "_row_key"])["project_id"], recs


# ---------------------------------------------------------------- 별칭(검색용 이름)
SIDO_ABBR = {"서울특별시": "서울", "경기도": "경기", "인천광역시": "인천"}
_SUFFIX = r"(아파트|APT|재건축(정비)?(사업)?(조합)?|정비사업|정비구역|일대|일원)"


def _core(name: str) -> str:
    """'은마아파트 재건축정비사업' → '은마' (사람이 읽는 형태의 핵심 이름)."""
    s = "".join(_CIRCLED.get(ch, ch) for ch in str(name)).strip()
    s = re.sub(r"\s*" + _SUFFIX + r"\s*", " ", s, flags=re.I)
    s = re.sub(r"(?<=[가-힣])A(?=$|\s|\d)", "", s)
    s = re.sub(r"\s+", " ", s).strip(" ,·")
    return s


def _dongs(address) -> list[str]:
    """'대치동 316' / '산곡1동 180-329번지 일원' / '청학동 96-4' → ['대치동'] / ['산곡동', '산곡1동']."""
    out = []
    for m in re.finditer(r"([가-힣]{1,4}?)(\d*)(동|가)(?=\s|\d|$)", str(address or "")):
        stem, num, suf = m.groups()
        if stem in ("아파트", "일") or len(stem) < 1:
            continue
        out.append(stem + suf)
        if num:
            out.append(stem + num + suf)
    return list(dict.fromkeys(out))


def _split_members(name: str) -> list[str]:
    """'경우현(개포경남,우성3차,현대1차)' → ['개포경남', '우성3차', '현대1차'],
    '잠실우성1,2,3차' → ['잠실우성1차', '잠실우성2차', '잠실우성3차'], '신반포19차,25차' → ['신반포19차', '신반포25차']."""
    out = []
    inner = re.findall(r"\(([^)]*)\)", name)
    outer = re.sub(r"\(.*?\)", "", name)
    for part in [outer, *inner]:
        bits = [b.strip() for b in re.split(r"[,·∙/]", part) if b.strip()]
        if len(bits) < 2:
            continue
        m = re.match(r"^(\D+)\d", bits[0])
        stem = m.group(1) if m else ""
        m = re.search(r"\d(차|단지)$", bits[-1])
        suffix = m.group(1) if m else ""
        for i, b in enumerate(bits):
            if stem and re.fullmatch(r"\d+", b):                  # '잠실우성1,2,3차'의 '2' → '잠실우성2차'
                b = stem + b + suffix
            elif stem and re.fullmatch(r"\d+(차|단지)", b):       # '신반포19차,25차'의 '25차'
                b = stem + b
            elif i == 0 and suffix and re.search(r"\d$", b):      # 첫 항목 '잠실우성1' → '잠실우성1차'
                b = b + suffix
            out.append(b)
    return out


def build_aliases(names: list[str], addresses: list, sgg: str) -> list[tuple[str, str]]:
    """한 사업장의 모든 출처 표기로부터 검색용 별칭 목록 [(별칭, 종류)]을 만든다."""
    al: dict[str, str] = {}

    def add(a: str, kind: str):
        a = re.sub(r"\s+", " ", str(a)).strip(" ,·")
        a = re.sub(r"\s+\)", ")", a)
        if (len(re.sub(r"\s", "", a)) >= 2 and not re.match(r"^[-\d\s]", a) and a.count("(") == a.count(")")
                and a not in al):
            al[a] = kind

    dongs = [d for a in addresses for d in _dongs(a)]
    sgg_short = re.sub(r"(특별시|광역시|시|군|구)$", "", re.sub(r"\s+", "", sgg or "")) if sgg else ""
    for n in names:
        n = str(n).strip()
        add(n, "출처 표기")
        for p in re.findall(r"\(([^)]*)\)", n):                   # '망우1(염광아파트)' → '염광아파트'
            if not re.fullmatch(r"(재촉|통합재건축|\d+~\d+동.*|.*동 제외)", p):
                add(p, "괄호 안 이름")
                add(_core(p), "괄호 안 이름")
        for c in [_core(re.sub(r"\(.*?\)", "", n))]:
            if not c:
                continue
            add(c, "표기 변형")
            add(c.replace(" ", ""), "표기 변형")
            if not re.search(r"구역$|번지$|\d$|지구$", c) or re.search(r"단지$|차$", c):
                add(c + "아파트", "표기 변형")
            add(c + " 재건축", "표기 변형")
            m = re.match(r"^(.*?)(제)?(\d+)(-\d+)?구역$", c)        # '송내1-1구역' ↔ '송내 제1-1구역' ↔ '송내1-1'
            if m:
                add(f"{m.group(1)}{m.group(3)}{m.group(4) or ''}", "표기 변형")
                add(f"{m.group(1)} 제{m.group(3)}{m.group(4) or ''}구역", "표기 변형")
            m = re.match(r"^(.*?)(\d+)(단지|차)$", c)              # '잠실주공5단지' ↔ '잠실주공5'
            if m:
                add(m.group(1) + m.group(2), "표기 변형")
            c3 = re.sub(r"신도시|특별정비(구역)?", " ", c).strip()   # '평촌신도시A-17특별정비' → '평촌 A-17'
            if c3 != c:
                add(c3, "표기 변형")
                add(c3 + "구역", "표기 변형")
            if "주공" in c and not c.startswith("주공"):              # '잠실주공5단지' → '잠실5단지'
                add(c.replace("주공", ""), "표기 변형")
            c2 = re.sub(r"아파트지구특별계획구역|특별계획구역", "", c)  # '압구정아파트지구특별계획구역3' → '압구정3구역'
            if c2 != c:
                add(c2, "표기 변형")
                add(re.sub(r"(\d+)$", r"\1구역", c2), "표기 변형")
            for d in dongs:                                       # '은마' + '대치동' → '대치동 은마', '대치은마'
                stem = d[:-1]
                if not c.startswith(stem):
                    add(f"{d} {c}", "동 이름 결합")
                    add(f"{stem}{c.replace(' ', '')}", "동 이름 결합")
                elif c.startswith(stem) and len(c) > len(stem) + 1:
                    add(c[len(stem):].strip(), "동 이름 제거")      # '대치은마' → '은마'
            if sgg_short and not c.startswith(sgg_short) and len(c) <= 6:
                add(f"{sgg_short} {c}", "시군구 결합")
        for m in _split_members(n):
            add(m, "구성 단지")
            add(_core(m), "구성 단지")
    return list(al.items())


def search_key(s: str) -> str:
    """검색 비교용 정규화: 공백·'아파트'·'재건축' 등 제거 (zone_key와 같은 규칙)."""
    return zone_key(s)


def make_project_id(row) -> str:
    return f"{SIDO_ABBR.get(row['region_sido'], row['region_sido'])}-{sgg_key(row['region_sigungu'], row['region_sido'])}-{zone_key(row['zone_name'])}"


def alias_table(res: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in res.iterrows():
        for a, kind in r["_aliases"]:
            rows.append({"project_id": r["project_id"], "alias": a, "alias_key": search_key(a), "alias_type": kind,
                         "region_sido": r["region_sido"], "region_sigungu": r["region_sigungu"],
                         "zone_name": r["zone_name"], "stage": r["stage"]})
    t = pd.DataFrame(rows).drop_duplicates(["project_id", "alias"])
    # 같은 별칭이 여러 사업장에 걸리면 표시해 둔다(예: '미성아파트'는 여러 구에 존재)
    n = t.groupby("alias_key")["project_id"].transform("nunique")
    t["ambiguous"] = n.gt(1)
    return t


def main() -> None:
    """전체를 처음부터 다시 만든다(검증·비교용). 매일 갱신은 update_daily.py를 쓴다."""
    _, _, recs = full_build()
    res, excluded = records_frame(recs)
    write_outputs(res, excluded, f"full_{date.today():%Y%m%d}")
    print(res.groupby(["region_sido", "stage"], sort=False).size().unstack(fill_value=0).to_string())


if __name__ == "__main__":
    main()
