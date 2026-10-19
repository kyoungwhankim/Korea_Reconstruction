"""사업장별 뉴스·고시 감시 — 단계 변화 '후보'를 모아 검토 대기열을 만든다. 단계를 자동으로 바꾸지는 않는다.

    python news.py                    # 매일 실행(워크플로): 오늘 아직 안 본 사업장만 조회하고 대기열을 다시 쓴다
    python news.py --dry-run          # 저장하지 않고 결과만 출력
    python news.py --only 은마,둔촌    # 이름에 이 말이 든 사업장만(테스트). --force: 오늘 이미 봤어도 다시 조회
    python news.py --no-naver --no-google --no-seoul   # 출처 끄기
    python news.py --rebuild          # 조회 없이 저장된 후보로 출력 파일만 다시 만든다(수동 반영·기각 뒤 정리)

출처 (모두 공개 자료. 재개발닷컴 같은 민간 사이트는 긁지 않는다)
  - 네이버 뉴스 검색: NAVER API HUB(naverapihub.apigw.ntruss.com). 환경변수 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET
    (GitHub secret, 로컬은 .env). 사업장당 1~2건 조회, 하루 NAVER_BUDGET건 안에서. 키가 없으면 건너뛴다.
  - Google News RSS: 키 없음. 사업장당 1건, 요청 간격 GOOGLE_GAP초. 차단(429)되면 그날은 중단한다.
  - 정비사업 정보몽땅 고시/공고(cleanup.seoul.go.kr, 공개 페이지): 서울시·자치구가 등록한 정비사업 고시·공고 목록을
    하루 한 번 최근 기간만 읽고(초당 1건 이하) 서울 사업장 이름과 대조한다. 등록기관(자치구)을 지역 이름으로 쓴다.

판정 (제목만 본다 — 본문은 받지 않는다)
  1) 제목에 사업장 이름(별칭)이 있어야 한다. 흔한 이름(현대·주공N단지…)이나 여러 사업장이 같이 쓰는 별칭은
     제목·요약에 동 이름이나 시군구 이름도 함께 있어야 한다.
  2) 제목의 단계 표현을 collect_reconstruction.STAGES 체계로 바꾼다(STAGE_PATTERNS). 무산·반려·보류 같은 말이 있으면 버린다.
     '앞두고·신청·예정·총회' 같은 말이 있으면 confidence='예정'(아직 그 단계가 아님), 아니면 '확정'.
  3) 기사 단계가 현재 단계보다 앞설 때만 후보로 올린다. 현재 단계가 따라오면(출처 갱신 또는 manual_stage.csv) 후보는 자연히 빠진다.

상태·출력
  state/news.jsonl                 사업장별 마지막 조회일, 본 기사 해시(제목 기준), 후보 목록 — 매일 새 기사만 본다
  output/review_queue_latest.csv   검토 대기열(사람이 보는 표)
  docs/data/review.json            페이지의 '검토 대기' 보기
  manual_stage.csv                 확인된 후보를 사람이 적는 곳(project_id, stage, date, evidence_url, note) → update_daily.py가 반영
  review_dismissed.csv             후보가 아니라고 판단한 기사(article_id) → 대기열에서 뺀다
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

import collect_reconstruction as cr

STATE_FILE = cr.BASE / "state" / "news.jsonl"
QUEUE_CSV = cr.OUT / "review_queue_latest.csv"
SITE_REVIEW = cr.BASE / "docs" / "data" / "review.json"
DISMISS_FILE = cr.BASE / "review_dismissed.csv"
ENV_FILE = cr.BASE / ".env"

NAVER_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
NAVER_BUDGET = 2000          # 하루 호출 상한(무료 한도 25,000건의 일부만 쓴다)
NAVER_GAP = 0.2              # 요청 간격(초)
GOOGLE_URL = "https://news.google.com/rss/search"
GOOGLE_GAP = 0.6
SEOUL_LIST = "https://cleanup.seoul.go.kr/cleanup/bbs/lscr.do"   # 정보몽땅 > 알림마당 > 고시/공고 (서울시·자치구 등록)
SEOUL_PARAMS = {"bbsClCode": "100", "ctgryClCode": "100"}
SEOUL_SOURCE = "정보몽땅 고시·공고"
SEOUL_GAP = 1.1              # 공개 페이지는 초당 1건 이하
SEOUL_MAX_PAGES = 10         # 기간을 걸어 읽으므로(한 달 10여 건) 보통 1~2쪽이면 끝난다

WINDOW_DAYS = 60             # 이보다 오래된 기사는 보지 않는다
GOOGLE_FIRST_DAYS, GOOGLE_NEXT_DAYS = 60, 14   # 구글 검색 기간(when:Nd): 처음 / 그 뒤
EXPIRE_DAYS = 90             # 후보는 기사 날짜 기준 90일이 지나면 대기열에서 뺀다
SEEN_KEEP_DAYS = 120         # 본 기사 해시 보관 기간(기사 창보다 길게)
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (reconstruction-status-collector; +https://github.com/kyoungwhankim/Korea_Reconstruction)"}
TIMEOUT = (10, 30)
LAST_STAGE = cr.STAGES[-1][1]
SESSION = requests.Session()
SESSION.headers.update(UA)

# ---------------------------------------------------------------- 단계 표현 → 표준 단계
# (단계, 정규식, 그 자체로는 '아직 아님') — 제목에서 공백을 뺀 문자열에 적용한다.
# '아직 아님'(True) 패턴은 단계를 언급만 한 것이라 confidence가 '예정'이 되고, 시세·분양권 기사에서는 무시한다.
STAGE_PATTERNS = [
    ("안전진단", r"안전진단.{0,8}(통과|확정|판정|면제|완료|합격|조건부|D등급|E등급|최종)|재건축(확정|판정)|조건부재건축|"
               r"재건축진단.{0,6}(통과|면제|완료)", False),
    ("안전진단", r"안전진단|재건축진단", True),
    ("정비계획수립", r"정비계획(안)?(수립|입안|열람|공람|공고)|신속통합기획(안)?.{0,4}(확정|완료|수립)|신통기획.{0,6}(확정|완료)", False),
    ("정비계획수립", r"신속통합기획|신통기획|정비계획", True),
    ("정비구역지정", r"정비구역(으로)?(지정|결정)|구역지정|정비구역.{0,3}고시|정비계획.{0,8}(통과|가결|결정)|"
                 r"도시계획위원회.{0,10}(통과|가결)|도계위.{0,10}(통과|가결)", False),
    ("정비구역지정", r"도시계획위원회|도계위|정비구역", True),
    ("추진위구성", r"추진위(원회)?(구성)?승인|추진위(원회)?(구성|설립|출범)", False),
    ("추진위구성", r"추진위", True),
    ("조합설립인가", r"조합설립(인가|승인)|조합설립.{0,3}인가|조합(이)?설립(됐|되|완료)|사업시행자(로)?지정|신탁.{0,10}사업시행자", False),
    ("조합설립인가", r"조합설립|창립총회|조합원모집", True),
    ("건축심의", r"건축심의.{0,6}(통과|가결|승인|완료)|건축위원회.{0,8}(통과|가결)|통합심의.{0,8}(통과|가결)", False),
    ("건축심의", r"건축심의|통합심의|건축위원회", True),
    ("사업시행인가", r"사업시행(계획)?(인가|승인)|사업시행.{0,3}인가", False),
    ("사업시행인가", r"사업시행계획|사업시행", True),
    ("관리처분인가", r"관리처분(계획)?(인가|승인)|관리처분.{0,3}인가", False),
    ("관리처분인가", r"관리처분", True),
    ("이주·철거", r"이주(개시|시작|돌입|착수|완료|마무리|절차)|이주율|철거(개시|시작|돌입|착수|완료|공사)|이주.{0,3}(개시|시작|돌입)|"
               r"이주및철거|이주·철거", False),
    ("이주·철거", r"이주|철거", True),
    ("착공", r"착공|첫삽|기공식", False),
    ("분양", r"일반분양|분양(개시|시작|돌입|일정|공고)|청약(접수|돌입|시작|일정|경쟁률|결과)|견본주택|모델하우스|입주자모집|"
           r"분양가.{0,8}(확정|승인|심사|통과)|후분양|선분양|특별공급|1순위", False),
    ("분양", r"분양", True),
    ("준공", r"준공|입주(시작|개시|돌입)|집들이|사용검사|사용승인", False),
    ("준공", r"입주", True),
    ("이전고시·청산", r"이전고시|조합(해산|청산)|해산총회|청산총회|청산(절차|개시)", False),
]
STAGE_PATTERNS = [(s, re.compile(rx), hedged) for s, rx, hedged in STAGE_PATTERNS]
NEGATIVE = re.compile(r"무산|반려|부결|보류|취소|제동|철회|불허|무효|백지화|좌초|불발|기각|중단|제외|탈락")
SKIP_TITLE = re.compile(r"리모델링|가로주택|소규모재건축|모아타운|재개발구역")       # 다른 사업 유형
MARKET = re.compile(r"분양권|입주권|경매|공매|낙찰|신고가|호가|시세|집값|매매가|전셋값|전세가|월세|임대료|갭투자|대출|청약통장")
HEDGE = re.compile(r"앞두|앞둔|예정|목표|추진|신청|준비|임박|가시화|눈앞|채비|시동|박차|나선|나섰|동의서|동의율|설명회|공청회|"
                   r"입찰|시공사|시공권|수주|총회|촉구|요구|요청|기대|전망|가능성|검토|논의|협의|갈등|반발|소송|난항|지연|연기|"
                   r"초읽기|코앞|청신호|가속|탄력|순항|급물살|물꼬|첫발|첫걸음|방침|용역|공모|도전|노린|노려|겨냥|위해|위한|"
                   r"관건|변수|기로|촉각|주목|예고|시기|언제|가능할까|되나|될까|향해|향한|남았|남은|예상|앞으로|수순|순서")
POSITIVE = re.compile(r"^.{0,2}(받|통과|획득|취득|따냈|따내|확정|고시|승인|완료|마쳤|마무리|됐|돼|했다|결정|인가|나왔|떨어졌|났다|확보)")
BREAK = re.compile(r"…|\.{2,}|·+|,|—|–|~|\||/|\]|\)")                 # 절이 바뀌는 자리: 그 뒤의 말은 앞 사건을 꾸미지 않는다
BEFORE_HEDGE = re.compile(r"연내|내년|내달|다음달|상반기|하반기|조만간|이르면|늦어도|이달중|향후|목표")   # '연내 사업시행인가'


def detect_stage(title: str, allow_hedged: bool = True) -> tuple[str, str] | None:
    """기사 제목 → (표준 단계, '확정'|'예정') 또는 None. 가장 진전된 단계를 고른다."""
    t = re.sub(r"\s+", "", title)
    if SKIP_TITLE.search(t) or NEGATIVE.search(t):
        return None
    events, rest = [], t
    for stage, rx, hedged in STAGE_PATTERNS:
        if hedged:
            continue
        for m in rx.finditer(t):
            events.append((m.start(), m.end(), stage))
        rest = rx.sub(" ", rest)
    # 단계를 언급만 한 표현(아직 아님): 제목 앞쪽에 나온 것이 보통 기사의 주제다
    mentioned = sorted((m.start(), stage) for stage, rx, hedged in STAGE_PATTERNS if hedged
                       for m in [rx.search(rest)] if m) if allow_hedged and not MARKET.search(t) else []
    if events:
        events.sort()
        masked = list(t)                       # 사건 표현 자체는 지우고 그 앞뒤 말만 본다('청약 접수'의 '접수'가 머뭇거림이 아니게)
        for s, e, _ in events:
            masked[s:e] = " " * (e - s)
        masked = "".join(masked)
        judged = []
        for s, e, stage in events:
            raw = masked[e:e + 14].lstrip()
            if POSITIVE.match(raw):                               # '…인가 받았다', '…통과했다'
                judged.append((stage, "확정", s))
                break
            # 바로 뒤가 '…'·','·'·'이면 다른 절(다음 계획)이라 보지 않는다; 따옴표는 벗기고 같은 절만 본다
            clause = "" if BREAK.match(raw) else BREAK.split(raw.lstrip("‘’'\"“”[(「『"), 1)[0]
            hedged = bool(HEDGE.search(clause) or BEFORE_HEDGE.search(masked[max(0, s - 6):s]))
            judged.append((stage, "예정" if hedged else "확정", s))
        stage, conf, start = judged[0] if judged[0][1] == "확정" or len(judged) == 1 else \
            next((j for j in judged if j[1] == "확정"), judged[0])
        if conf == "예정":
            # '사업시행계획안 통과…내년 관리처분인가 목표': 앞에 나온 더 가까운 단계를 후보로 쓴다
            earlier = [s for pos, s in mentioned if pos < start and cr.STAGE_ORDER[s] < cr.STAGE_ORDER[stage]]
            if earlier:
                stage = earlier[0]
        return stage, conf
    if mentioned:
        return mentioned[0][1], "예정"
    return None


# ---------------------------------------------------------------- 사업장 프로필(이름 키·지역·질의)
# 이름만으로는 어느 단지인지 알 수 없는 줄기(지역 이름 없이 쓰이면 제목에 동·시군구가 함께 있어야 한다)
GENERIC = {
    "주공", "현대", "신현대", "우성", "삼익", "한양", "대림", "시범", "한신", "삼성", "신동아", "극동", "라이프", "미성", "장미",
    "목련", "동아", "경남", "두산", "코오롱", "진흥", "청구", "건영", "삼호", "한보", "미도", "선경", "대우", "쌍용", "럭키", "롯데",
    "태영", "벽산", "성원", "동부", "부영", "성지", "무지개", "개나리", "진달래", "백합", "은하", "성보", "한진", "동신", "대동",
    "서해", "중앙", "제일", "신성", "영남", "미주", "삼환", "금호", "효성", "동성", "신안", "풍림", "상록", "한라", "보람", "신한",
    "한국", "국제", "수정", "우남", "태양", "화성", "세경", "경동", "삼부", "계룡", "연립", "빌라", "맨션", "주택", "시영", "공무원",
    "교수", "상아", "현대빌라", "삼환", "경향", "삼풍", "한성", "유원", "동원", "협성", "영풍", "뉴서울", "새서울", "서울", "우정",
    "현대홈타운", "대림e편한세상", "청솔", "샛별", "목화", "한신공영", "동양", "신라", "진주", "미림", "신일", "범양", "삼우",
    "동보", "보성", "일신", "일성", "대성", "태성", "성원", "경성", "고려", "신원", "해태", "우방", "동문", "우림", "현진", "장안",
    "한일", "한도", "청담", "대교", "신성", "삼도", "삼안", "한진", "대호", "대신", "동일", "명지", "성도", "세종", "주은", "한신",
    "단독", "아주", "동백", "천천", "인계", "송림", "동남", "월드", "반도", "서강", "대흥", "한솔", "가람",   # 일반 낱말·지명과 같은 이름
}
PAREN_SKIP = re.compile(r"(재촉|통합재건축|\d+~\d+동.*|.*동 제외)")


def generic_key(k: str) -> bool:
    stem = re.sub(r"\d+(-\d+)?(_\d+)*$", "", k)
    stem = re.sub(r"(연립|빌라|맨션|빌라트|타운)$", "", stem)
    return len(stem) <= 1 or stem in GENERIC


def display_name(zone: str) -> str:
    """'압구정아파트지구특별계획구역3' → '압구정3구역', '은마아파트' → '은마', '관산Ⅱ-1구역(새서울아파트)' → '관산2-1구역'."""
    z = re.sub(r"\(.*?\)", "", str(zone))
    z = re.sub(r"아파트지구특별계획구역|특별계획구역", "", z)
    c = cr._core(z)
    c = re.sub(r"신도시|특별정비(예정)?(구역)?", " ", c)
    c = re.sub(r"\s+", " ", c).strip(" ,·-")
    if re.search(r"\d$", c) and "구역" in zone:
        c += "구역"
    return c


def region_tokens(rec: dict) -> tuple[set[str], set[str]]:
    """(제목에서 찾을 짧은 지역 이름, 요약에서 찾을 온전한 지역 이름).
    '은행'(은행동)·'중동'처럼 흔한 낱말과 같은 줄기는 제목에서만 믿고, 요약에서는 '은행동'·'강남구'처럼 온전한 이름만 본다."""
    short, full = set(), set()
    addr = rec.get("address") or ""
    for d in cr._dongs(addr):
        full.add(d)                                   # 대치동
        stem = re.sub(r"\d*(동|가)$", "", d)
        if len(stem) >= 2:
            short.add(stem)                           # 대치
    for m in re.finditer(r"([가-힣]{2,4})(시|구|군)(?=\s|$)", addr):
        short.add(m.group(1))                         # 고양, 덕양
        full.add(m.group(0))
    sgg = rec.get("region_sigungu") or ""
    s = re.sub(r"(특별시|광역시|시|군|구)$", "", sgg)
    if len(s) >= 2:
        short.add(s)                                  # 강남, 부천
        full.add(sgg)                                 # 강남구, 부천시
    return short, full


def profile(pid: str, rec: dict, shared: set[str]) -> dict:
    """후보 판정과 검색에 쓰는 사업장 정보. shared: 다른 사업장도 쓰거나 지역 이름과 같은 별칭 키(이름만으로는 못 가림)."""
    strong, medium, weak = {}, {}, {}
    for alias, kind in rec.get("_aliases") or []:
        k = cr.zone_key(alias)
        if len(k) < 2 or re.fullmatch(r"[\d\-_]+", k) or k in strong or k in medium or k in weak:
            continue
        if generic_key(k) or k in shared:
            weak[k] = alias                 # 흔한 이름·여러 사업장이 쓰는 별칭: 지역 이름이 함께 있어야 한다
        elif len(k) >= 3:
            strong[k] = alias
        elif kind != "동 이름 제거":
            medium[k] = alias               # '은마'처럼 짧지만 고유한 이름: 재건축·아파트 같은 문맥어가 함께 있어야 한다
        else:
            weak[k] = alias
    sgg_short = re.sub(r"(특별시|광역시|시|군|구)$", "", rec.get("region_sigungu") or "")
    zone = rec.get("zone_name") or ""
    core = display_name(zone)
    zone_like = bool(re.search(r"구역$|지구$|\d-\d|^[A-Z]-?\d", core)) or "특별정비" in zone
    parens = [cr._core(p) for p in re.findall(r"\(([^)]*)\)", zone) if not PAREN_SKIP.fullmatch(p)]
    manual = [a for a, kind in rec.get("_aliases") or [] if kind == "수동 별칭"]
    members = [a for a, kind in rec.get("_aliases") or [] if kind == "구성 단지" and not a.endswith("아파트")]
    names = [*manual, *(parens if zone_like else []), core, *(parens if not zone_like else []), *members[:1]]
    queries: list[str] = []
    for n in names:
        n = n.strip()
        if not n:
            continue
        k = cr.zone_key(n)
        prefix = sgg_short and (generic_key(k) or len(k) < 3 or (zone_like and n == core)) and not n.startswith(sgg_short)
        q = f"{sgg_short} {n} 재건축" if prefix else f"{n} 재건축"
        if q not in queries:
            queries.append(q)
    region, region_full = region_tokens(rec)
    return {"pid": pid, "name": zone, "sido": rec.get("region_sido"), "sgg": rec.get("region_sigungu"),
            "stage": rec.get("stage"), "order": cr.STAGE_ORDER.get(rec.get("stage"), -1),
            "strong": strong, "medium": medium, "weak": weak, "region": region, "region_full": region_full,
            "queries": queries[:2] or [f"{zone} 재건축"], "google_query": (queries or [f"{zone} 재건축"])[0]}


def contains(text_key: str, k: str) -> bool:
    """정규화한 제목에 별칭 키가 있는가. 숫자 경계는 지킨다(신반포2 ⊄ 신반포22, 창전1 ⊄ 창전12)."""
    i = text_key.find(k)
    while i >= 0:
        after = text_key[i + len(k): i + len(k) + 1]
        before = text_key[i - 1: i] if i else ""
        if not (k[-1].isdigit() and after.isdigit()) and not (k[0].isdigit() and before.isdigit()):
            return True
        i = text_key.find(k, i + 1)
    return False


def match_name(prof: dict, title: str, desc: str = "") -> str | None:
    """제목이 이 사업장을 가리키면 맞은 별칭을 돌려준다."""
    tk = cr.zone_key(title)
    for k, alias in prof["strong"].items():
        if contains(tk, k):
            return alias
    if CONTEXT.search(title):
        for k, alias in prof["medium"].items():
            if contains(tk, k):
                return alias
    if any(tok in title for tok in prof["region"]) or any(tok in desc for tok in prof["region_full"]):
        for k, alias in {**prof["medium"], **prof["weak"]}.items():
            if contains(tk, k):
                return alias
    return None


CONTEXT = re.compile(r"재건축|아파트|정비|조합|단지|주공|재개발|시공")


def load_projects() -> dict[str, dict]:
    """state/projects.jsonl의 활성 아파트 재건축 사업장(현재 단계에 manual_stage.csv가 이미 반영돼 있다)."""
    out = {}
    for line in (cr.BASE / "state" / "projects.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        rec = r.get("record") or {}
        if rec.get("status") == "활성" and rec.get("_apartment"):
            out[r["project_id"]] = rec
    return out


def build_profiles(projects: dict[str, dict]) -> dict[str, dict]:
    owners: dict[str, set[str]] = {}
    places: set[str] = set()          # 동·시군구 이름('산본', '중동')은 단지 이름이어도 이름만으로는 못 가린다
    for pid, rec in projects.items():
        for alias, _ in rec.get("_aliases") or []:
            owners.setdefault(cr.zone_key(alias), set()).add(pid)
        places |= region_tokens(rec)[0]
    shared = {k for k, pids in owners.items() if len(pids) > 1}
    return {pid: profile(pid, rec, shared | places) for pid, rec in projects.items()}


# ---------------------------------------------------------------- 출처
class SourceDown(Exception):
    """이 출처는 오늘 더 쓸 수 없다(한도 초과·차단·인증 실패)."""


def clean(s: str | None) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", "", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def to_kst_date(rfc822: str | None) -> str | None:
    try:
        return parsedate_to_datetime(rfc822).astimezone(KST).date().isoformat()
    except (TypeError, ValueError):
        return None


def naver_auth() -> tuple[str, str] | None:
    """환경변수(GitHub secret) → 없으면 .env. 값은 절대 출력하지 않는다."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    cid = os.environ.get("NAVER_CLIENT_ID") or env.get("NAVER_CLIENT_ID")
    sec = os.environ.get("NAVER_CLIENT_SECRET") or env.get("NAVER_CLIENT_SECRET")
    return (cid, sec) if cid and sec else None


def naver_search(q: str, auth: tuple[str, str]) -> list[dict]:
    r = SESSION.get(NAVER_URL, params={"query": q, "display": 100, "sort": "date", "format": "json"},
                    headers={"X-NCP-APIGW-API-KEY-ID": auth[0], "X-NCP-APIGW-API-KEY": auth[1]}, timeout=TIMEOUT)
    if r.status_code in (401, 403, 429):
        raise SourceDown(f"네이버 HTTP {r.status_code}" + (" (한도 초과)" if r.status_code == 429 else " (인증 실패)"))
    r.raise_for_status()
    out = []
    for it in r.json().get("items", []):
        d = to_kst_date(it.get("pubDate"))
        title = clean(it.get("title"))
        if d and title:
            out.append({"title": title, "link": it.get("link") or it.get("originallink"), "published": d,
                        "desc": clean(it.get("description")), "source": "네이버"})
    return out


def google_search(q: str, days: int) -> list[dict]:
    r = SESSION.get(GOOGLE_URL, params={"q": f"{q} when:{days}d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"}, timeout=TIMEOUT)
    if r.status_code in (429, 503):
        raise SourceDown(f"구글 HTTP {r.status_code} (요청 제한)")
    r.raise_for_status()
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        raise SourceDown(f"구글 응답이 RSS가 아님({e})") from e
    out = []
    for it in root.iter("item"):
        title, src = clean(it.findtext("title")), clean(it.findtext("source"))
        if src and title.endswith(src):                     # '제목 - 매체명' 꼬리를 뗀다(대시 종류가 제각각)
            title = title[: -len(src)].rstrip(" -–—‐|·:")
        else:
            title = re.sub(r"\s+[-–—‐]\s+[^-–—‐]{1,30}$", "", title)
        d = to_kst_date(it.findtext("pubDate"))
        if d and title:
            out.append({"title": title, "link": it.findtext("link"), "published": d, "desc": "", "source": "구글"})
    return out


SEOUL_ROW = re.compile(r'<a href="(/cleanup/bbs/vscr\.do\?[^"]*?bbs\.bbsSn=(\d+))">\s*<h3 class="b-tit">(.*?)</h3>'
                       r'.*?등록일 : (\d{4}-\d{2}-\d{2}).*?등록기관 : ([^<]*)<', re.S)


def seoul_notices(since: date, until: date) -> list[dict]:
    """정보몽땅 고시/공고 목록(서울시·자치구가 등록한 정비사업 고시·공고)을 since~until 기간으로 읽는다(초당 1건 이하)."""
    out, ids = [], set()
    for page in range(1, SEOUL_MAX_PAGES + 1):
        r = SESSION.get(SEOUL_LIST, params={**SEOUL_PARAMS, "cpage": page, "pageSize": 50, "registStDt": since.isoformat(),
                                            "registEdDt": until.isoformat()}, timeout=TIMEOUT)
        r.raise_for_status()
        rows = [row for row in SEOUL_ROW.findall(r.text) if row[1] not in ids]
        if not rows:
            if page == 1 and "board-list" not in r.text:
                raise RuntimeError("정보몽땅 고시/공고 목록을 읽지 못함(페이지 형식 변경?)")
            break
        for href, sn, title, d, org in rows:
            ids.add(sn)
            out.append({"id": sn, "title": clean(title), "dept": clean(org), "published": d,
                        "link": "https://cleanup.seoul.go.kr" + html.unescape(href)})
        if len(rows) < 10 or date.fromisoformat(rows[-1][3]) < since:
            break
        time.sleep(SEOUL_GAP)
    return out


# ---------------------------------------------------------------- 상태
def load_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    out = {}
    for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["project_id"]] = r
    return out


def blank(pid: str) -> dict:
    return {"project_id": pid, "checked_at": None, "seen": {}, "candidates": []}


def serialize(state: dict[str, dict]) -> str:
    """정렬된 고정 형식 — 내용이 같으면 파일도 같다(git 변경 없음)."""
    lines = []
    for pid in sorted(state):
        r = state[pid]
        row = {"project_id": pid, "checked_at": r.get("checked_at"),
               "seen": dict(sorted((r.get("seen") or {}).items())),
               "candidates": sorted(r.get("candidates") or [], key=lambda c: (c["published"], c["id"]), reverse=True)}
        lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return "\n".join(lines) + ("\n" if lines else "")


def load_dismissed() -> set[str]:
    if not DISMISS_FILE.exists():
        return set()
    with open(DISMISS_FILE, encoding="utf-8-sig", newline="") as f:
        return {(r.get("article_id") or "").strip() for r in csv.DictReader(f)} - {""}


def article_id(pid: str, title: str) -> str:
    norm = re.sub(r"[\W_]+", "", title.lower())
    return hashlib.sha1(f"{pid}|{norm}".encode()).hexdigest()[:12]


def prune(state: dict[str, dict], profiles: dict[str, dict], dismissed: set[str], today: date) -> None:
    """해결된(현재 단계가 따라온)·기각된·오래된 후보와 오래된 기사 해시를 지운다."""
    expire, keep_seen = (today - timedelta(days=EXPIRE_DAYS)).isoformat(), (today - timedelta(days=SEEN_KEEP_DAYS)).isoformat()
    for pid in list(state):
        st = state[pid]
        st["seen"] = {k: d for k, d in (st.get("seen") or {}).items() if d >= keep_seen}
        if pid == "_seoul":
            continue
        if pid not in profiles:                    # 사라졌거나 더 볼 단계가 없는 사업장
            del state[pid]
            continue
        order, kept = profiles[pid]["order"], []
        for c in st.get("candidates") or []:
            # 판정 규칙이 나아지면 저장된 후보도 같은 규칙으로 다시 매긴다(제목 기준이라 결정적)
            det = detect_stage(c["title"], allow_hedged=c.get("source") != SEOUL_SOURCE)
            if not det:
                continue
            c["stage"], c["confidence"] = det
            if c["id"] not in dismissed and cr.STAGE_ORDER[c["stage"]] > order and c["published"] >= expire:
                kept.append(c)
        st["candidates"] = kept
        if not st["candidates"] and not st["seen"] and not st.get("checked_at"):
            del state[pid]


# ---------------------------------------------------------------- 조회
class Sources:
    def __init__(self, naver: bool, google: bool, seoul: bool):
        self.naver_auth = naver_auth() if naver else None
        self.naver = bool(self.naver_auth)
        self.google, self.seoul = google, seoul
        self.notes: list[str] = []
        if naver and not self.naver:
            self.notes.append("네이버: NAVER_CLIENT_ID/NAVER_CLIENT_SECRET 없음 — 건너뜀")
        self.google_fails = 0

    def down(self, name: str, why: str) -> None:
        setattr(self, name, False)
        self.notes.append(f"{'네이버' if name == 'naver' else '구글'}: {why} — 오늘은 더 조회하지 않음")
        print(f"   ! {self.notes[-1]}", file=sys.stderr)


def classify(pid: str, prof: dict, items: list[dict], st: dict, today: date, stats: dict) -> list[dict]:
    """가져온 기사들 중 새 기사만 판정해 후보를 추가한다."""
    cutoff, today_s = (today - timedelta(days=WINDOW_DAYS)).isoformat(), today.isoformat()
    have = {c["id"] for c in st["candidates"]}
    new = []
    for it in items:
        if it["published"] < cutoff or not it.get("link"):
            continue
        aid = article_id(pid, it["title"])
        if aid in st["seen"] or aid in have:
            continue
        st["seen"][aid] = today_s
        stats["articles"] += 1
        alias = match_name(prof, it["title"], it.get("desc", ""))
        if not alias:
            continue
        det = detect_stage(it["title"])
        if not det or cr.STAGE_ORDER[det[0]] <= prof["order"]:
            continue
        cand = {"id": aid, "stage": det[0], "confidence": det[1], "title": it["title"], "link": it["link"],
                "published": it["published"], "source": it["source"], "matched": alias, "first_seen": today_s}
        st["candidates"].append(cand)
        have.add(aid)
        new.append(cand)
    return new


def fetch_google(prof: dict, st: dict, srcs: Sources, stats: dict) -> tuple[list[dict], bool]:
    """구글 RSS 1건 — 네이버 조회와 겹쳐 돌릴 수 있게 따로 뗀다(요청 간격은 이 안에서 지킨다)."""
    days = GOOGLE_NEXT_DAYS if st.get("checked_at") else GOOGLE_FIRST_DAYS
    try:
        stats["google_calls"] += 1
        items = google_search(prof["google_query"], days)
        srcs.google_fails = 0
        return items, True
    except SourceDown as e:
        srcs.down("google", str(e))
    except requests.RequestException as e:
        stats["errors"] += 1
        srcs.google_fails += 1
        print(f"   ! 구글 '{prof['google_query']}': {e}", file=sys.stderr)
        if srcs.google_fails >= 5:
            srcs.down("google", "연속 5회 실패")
    finally:
        time.sleep(GOOGLE_GAP)
    return [], False


POOL = ThreadPoolExecutor(max_workers=1)   # 구글 1건을 네이버 조회와 나란히


def check_project(prof: dict, st: dict, srcs: Sources, today: date, stats: dict) -> list[dict]:
    items, ok = [], False
    future = POOL.submit(fetch_google, prof, st, srcs, stats) if srcs.google else None
    if srcs.naver:
        for q in prof["queries"]:
            if stats["naver_calls"] >= NAVER_BUDGET:
                srcs.down("naver", f"오늘 호출 {NAVER_BUDGET}건 도달")
                break
            try:
                stats["naver_calls"] += 1
                items += naver_search(q, srcs.naver_auth)
                ok = True
            except SourceDown as e:
                srcs.down("naver", str(e))
                break
            except requests.RequestException as e:
                stats["errors"] += 1
                print(f"   ! 네이버 '{q}': {e}", file=sys.stderr)
            time.sleep(NAVER_GAP)
    if future:
        g_items, g_ok = future.result()
        items += g_items
        ok = ok or g_ok
    new = classify(prof["pid"], prof, items, st, today, stats)
    if ok:
        st["checked_at"] = today.isoformat()
    return new


def check_seoul(profiles: dict[str, dict], state: dict[str, dict], today: date, stats: dict) -> list[dict]:
    st = state.setdefault("_seoul", blank("_seoul"))
    since = date.fromisoformat(st["checked_at"]) - timedelta(days=3) if st.get("checked_at") else today - timedelta(days=WINDOW_DAYS)
    notices = seoul_notices(since, today)
    stats["seoul_notices"] = len(notices)
    seoul = [p for p in profiles.values() if p["sido"] == "서울특별시" and p["order"] < cr.STAGE_ORDER[LAST_STAGE]]
    new, today_s = [], today.isoformat()
    for n in notices:
        if n["id"] in st["seen"] or n["published"] < since.isoformat():
            continue
        st["seen"][n["id"]] = today_s
        det = detect_stage(n["title"], allow_hedged=False)
        if not det:
            continue
        for prof in seoul:
            if cr.STAGE_ORDER[det[0]] <= prof["order"]:
                continue
            alias = match_name(prof, n["title"], n["dept"])     # 등록기관(자치구)이 지역 이름 구실을 한다
            if not alias:
                continue
            pst = state.setdefault(prof["pid"], blank(prof["pid"]))
            aid = article_id(prof["pid"], n["title"])
            if any(c["id"] == aid for c in pst["candidates"]):
                continue
            cand = {"id": aid, "stage": det[0], "confidence": det[1], "title": n["title"], "link": n["link"],
                    "published": n["published"], "source": SEOUL_SOURCE, "matched": alias, "first_seen": today_s}
            pst["candidates"].append(cand)
            new.append({**cand, "_pid": prof["pid"]})
    st["checked_at"] = today_s
    return new


# ---------------------------------------------------------------- 출력
QUEUE_COLS = ["project_id", "region_sido", "region_sigungu", "zone_name", "current_stage", "article_stage", "confidence",
              "published", "title", "link", "source", "matched_alias", "first_seen", "article_id"]


def queue_rows(state: dict[str, dict], profiles: dict[str, dict]) -> list[dict]:
    rows = []
    for pid, st in state.items():
        p = profiles.get(pid)
        if not p:
            continue
        for c in st.get("candidates") or []:
            rows.append({"project_id": pid, "region_sido": p["sido"], "region_sigungu": p["sgg"], "zone_name": p["name"],
                         "current_stage": p["stage"], "article_stage": c["stage"], "confidence": c["confidence"],
                         "published": c["published"], "title": c["title"], "link": c["link"], "source": c["source"],
                         "matched_alias": c["matched"], "first_seen": c["first_seen"], "article_id": c["id"]})
    rows.sort(key=lambda r: (r["published"], r["project_id"], r["article_id"]), reverse=True)
    return rows


def write_outputs(rows: list[dict], today: date) -> None:
    QUEUE_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=QUEUE_COLS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    items = [{"id": r["article_id"], "project_id": r["project_id"], "zone_name": r["zone_name"], "region_sido": r["region_sido"],
              "region_sigungu": r["region_sigungu"], "stage": r["current_stage"], "article_stage": r["article_stage"],
              "confidence": r["confidence"], "published": r["published"], "title": r["title"], "link": r["link"],
              "source": r["source"], "matched": r["matched_alias"], "first_seen": r["first_seen"]} for r in rows]
    data = {"generated_at": today.isoformat(), "count": len(items), "items": items}
    SITE_REVIEW.parent.mkdir(parents=True, exist_ok=True)
    SITE_REVIEW.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                           encoding="utf-8", newline="\n")


def write_summary(today: date, stats: dict, new: list[dict], rows: list[dict], srcs: Sources | None, profiles: dict) -> None:
    sure = sum(r["confidence"] == "확정" for r in rows)
    lines = [f"## 뉴스·고시 감시 {today.isoformat()}", "",
             f"- 조회 사업장 {stats['checked']}곳 (네이버 {stats['naver_calls']}건 · 구글 {stats['google_calls']}건 호출, "
             f"새 기사 {stats['articles']}건 판정) · 정보몽땅 고시·공고 {stats.get('seoul_notices', 0)}건 확인",
             f"- 새 후보 {len(new)}건 · 검토 대기열 {len(rows)}건 (확정 {sure} · 예정 {len(rows) - sure})"]
    for n in (srcs.notes if srcs else []):
        lines.append(f"- ⚠️ {n}")
    if new:
        lines += ["", "| 사업장 | 현재 → 기사 단계 | 기사 | 날짜 |", "|---|---|---|---|"]
        for c in new[:40]:
            p = profiles.get(c["_pid"], {})
            lines.append(f"| {p.get('name', c['_pid'])} ({p.get('sgg', '')}) | {p.get('stage')} → {c['stage']} ({c['confidence']}) "
                         f"| [{c['title'][:60]}]({c['link']}) | {c['published']} |")
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="저장하지 않고 결과만 출력")
    ap.add_argument("--only", help="이름에 이 말이 든 사업장만(쉼표로 여러 개)")
    ap.add_argument("--limit", type=int, help="최대 이만큼의 사업장만 조회(오래 안 본 순)")
    ap.add_argument("--force", action="store_true", help="오늘 이미 조회한 사업장도 다시 조회")
    ap.add_argument("--no-naver", action="store_true")
    ap.add_argument("--no-google", action="store_true")
    ap.add_argument("--no-seoul", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="조회 없이 저장된 후보로 출력 파일만 다시 만든다")
    ap.add_argument("--date", default=date.today().isoformat(), help="기록용 날짜(테스트용)")
    args = ap.parse_args()
    today = date.fromisoformat(args.date)

    projects = load_projects()
    profiles = build_profiles(projects)
    state = load_state()
    before = serialize(state)
    dismissed = load_dismissed()
    stats = {"checked": 0, "naver_calls": 0, "google_calls": 0, "articles": 0, "errors": 0}
    new: list[dict] = []
    srcs, todo = None, []
    if not args.rebuild:
        srcs = Sources(not args.no_naver, not args.no_google, not args.no_seoul)
        todo = [p for p in profiles.values() if p["order"] < cr.STAGE_ORDER[LAST_STAGE]]
        if args.only:
            words = [w.strip() for w in args.only.split(",") if w.strip()]
            todo = [p for p in todo if any(w in p["name"] or w in " ".join(p["strong"].values()) for w in words)]
        todo.sort(key=lambda p: ((state.get(p["pid"]) or {}).get("checked_at") or "", p["pid"]))  # 오래 안 본 곳부터
        if not args.force:
            todo = [p for p in todo if (state.get(p["pid"]) or {}).get("checked_at") != today.isoformat()]
        if args.limit:
            todo = todo[: args.limit]
        print(f"[뉴스] 사업장 {len(todo)}곳 조회 (네이버 {'O' if srcs.naver else 'X'} · 구글 {'O' if srcs.google else 'X'})")
        for i, prof in enumerate(todo, 1):
            if not srcs.naver and not srcs.google:
                print("   ! 쓸 수 있는 뉴스 출처가 없어 중단 — 남은 사업장은 다음 실행에서", file=sys.stderr)
                break
            st = state.setdefault(prof["pid"], blank(prof["pid"]))
            try:
                for c in check_project(prof, st, srcs, today, stats):
                    new.append({**c, "_pid": prof["pid"]})
                    print(f"   + {prof['name']}: {prof['stage']} → {c['stage']}({c['confidence']}) · {c['title'][:70]} · {c['published']}")
            except Exception as e:  # 한 사업장의 예외로 전체 실행을 잃지 않는다
                stats["errors"] += 1
                print(f"   ! {prof['name']}: {type(e).__name__}: {e}", file=sys.stderr)
            stats["checked"] += 1
            if i % 50 == 0:
                print(f"   … {i}/{len(todo)} (네이버 {stats['naver_calls']}건 · 구글 {stats['google_calls']}건)")
        if srcs.seoul and (args.force or (state.get("_seoul") or {}).get("checked_at") != today.isoformat()) and not args.only:
            try:
                for c in check_seoul(profiles, state, today, stats):
                    new.append(c)
                    print(f"   + [고시] {profiles[c['_pid']]['name']}: {c['title'][:80]} → {c['stage']} · {c['published']}")
            except (requests.RequestException, RuntimeError) as e:
                srcs.notes.append(f"{SEOUL_SOURCE}: {e}")
                print(f"   ! {SEOUL_SOURCE}: {e}", file=sys.stderr)

    prune(state, profiles, dismissed, today)
    rows = queue_rows(state, profiles)
    changed = serialize(state) != before
    if args.dry_run:
        print(f"[dry-run] 새 후보 {len(new)}건 · 대기열 {len(rows)}건 (저장 안 함)")
    else:
        if changed:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(serialize(state), encoding="utf-8", newline="\n")
        write_outputs(rows, today)
        print(f"[저장] 대기열 {len(rows)}건 → {QUEUE_CSV.relative_to(cr.BASE)}, {SITE_REVIEW.relative_to(cr.BASE)}"
              + ("" if changed else " (상태 변경 0건)"))
    write_summary(today, stats, new, rows, srcs, profiles)
    if srcs and todo and stats["checked"] == 0 and "seoul_notices" not in stats:
        sys.exit(1)   # 뉴스 출처도 고시 목록도 하나도 못 읽었다(인증·차단) — Actions 로그에 알린다


if __name__ == "__main__":
    main()
