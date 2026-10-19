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

신규 사업장 후보 (kind='신규 사업장 후보')
  사업장이 없는 노후 단지(docs/data/apartments.json에서 project_id가 없고 COMPLEX_MIN_UNITS세대 이상)도 하루 COMPLEX_DAILY곳씩
  돌아가며 조회한다(약 1,300곳 → 일주일에 한 바퀴; 구글 창 14일 안). 단지 이름은 흔해서(현대·주공…) 제목에 동·시군구 이름이
  함께 있어야 하고, 어떤 단계 표현이든(안전진단 통과·추진위 승인·동의서·정비구역 지정…) 보이면 후보가 된다.
  상태 키는 'cx:<complex_id>'. 페이지의 '등록 행' 버튼이 manual_projects.csv 한 줄을 복사해 준다 → update_daily.py가 사업장을 만든다.

상태·출력
  state/news.jsonl                 사업장별 마지막 조회일, 본 기사 해시(제목 기준), 후보 목록 — 매일 새 기사만 본다
  output/review_queue_latest.csv   검토 대기열(사람이 보는 표)
  docs/data/review.json            페이지의 '검토 대기' 보기
  manual_stage.csv                 확인된 후보를 사람이 적는 곳(project_id, stage, date, evidence_url, note) → update_daily.py가 반영
  manual_projects.csv              신규 사업장 후보를 사람이 등록하는 곳(complex_id, name, stage, date, evidence_url, note)
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

COMPLEX_MIN_UNITS = 300      # 신규 사업장 후보로 지켜볼 노후 단지의 최소 세대수(사업장이 없는 단지 약 1,300곳)
COMPLEX_DAILY = 200          # 하루에 조회하는 사업장 없는 노후 단지 수(오래 안 본 순 → 약 일주일에 한 바퀴)
COMPLEX_PREFIX = "cx:"       # 상태·대기열에서 단지 후보의 키: 'cx:<complex_id>'
STAGE_KIND, NEW_KIND = "단계 변경", "신규 사업장 후보"
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
    ("추진위구성", r"추진위|추진준비위|준비위원회|동의서", True),   # 동의서 징구 = 추진위·조합 설립 준비(아직 아님)
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
                   r"관건|변수|기로|촉각|주목|예고|시기|언제|가능할까|되나|될까|향해|향한|남았|남은|예상|앞으로|수순|순서|절차|공람|열람")
DRAFT = re.compile(r"\(안\)|안\)|공람|열람|고시안")       # 계획안·공람 단계: 지정·인가 자체가 아직 아니다('정비구역 지정(안) 재공람공고')
DRAFT_STAGES = {"정비구역지정", "건축심의", "사업시행인가", "관리처분인가"}   # 정비계획수립은 열람공고 자체가 그 단계라 제외
POSITIVE = re.compile(r"^.{0,2}(받|통과|획득|취득|따냈|따내|확정|고시|승인|완료|마쳤|마무리|됐|돼|했다|결정|인가|나왔|떨어졌|났다|확보)")
BREAK = re.compile(r"…|\.{2,}|·+|,|—|–|~|\||/|\]|\)")                 # 절이 바뀌는 자리: 그 뒤의 말은 앞 사건을 꾸미지 않는다
BREAK_BEFORE = re.compile(r"…|\.{2,}|—|–|~|\||/|\]|\)")                # 사건 앞쪽 절의 시작(쉼표·따옴표는 끊지 않는다: '11월, 1800가구 일반분양')
QUOTES = re.compile(r"[‘’'\"“”「」『』\[(]")
BEFORE_HEDGE = re.compile(r"연내|내년|내달|다음달|상반기|하반기|조만간|이르면|늦어도|이달중|향후|목표|\d{1,2}월")   # '연내 사업시행인가', '12월 이주 시작'
AGE = re.compile(r"(준공|입주|완공|지은지)\d+년(차)?|\d+년(차|된|넘은|지난|묵은|만에)")   # '준공 46년 신반포4차'의 '준공'은 사건이 아니다


def detect_stage(title: str, allow_hedged: bool = True) -> tuple[str, str] | None:
    """기사 제목 → (표준 단계, '확정'|'예정') 또는 None. 가장 진전된 단계를 고른다."""
    t = AGE.sub(" ", re.sub(r"\s+", "", title))
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
        draft = bool(DRAFT.search(masked))     # 사건 표현 밖에 '(안)'·'공람'이 있으면 지정·인가는 아직이다(같은 단계 사건이 둘이어도)
        judged = []
        for s, e, stage in events:
            raw = masked[e:e + 14].lstrip()
            if POSITIVE.match(raw) and not (draft and stage in DRAFT_STAGES):   # '…인가 받았다', '…통과했다'
                judged.append((stage, "확정", s))
                break
            # 바로 뒤가 '…'·','·'·'이면 다른 절(다음 계획)이라 보지 않는다; 따옴표는 벗기고 같은 절만 본다
            clause = "" if BREAK.match(raw) else BREAK.split(raw.lstrip("‘’'\"“”[(「『"), 1)[0]
            before = BREAK_BEFORE.split(QUOTES.sub("", masked[:s]))[-1][-20:]   # 같은 절의 앞말: '내년 ○○ 첫삽', '12월 이주 시작'
            hedged = bool(HEDGE.search(clause) or BEFORE_HEDGE.search(before) or (draft and stage in DRAFT_STAGES))
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
DROP_KEYS = {"서울", "경기", "인천", "수도권", "한국", "대한"}   # 어떤 기사에나 나오는 말: 별칭으로 쓰지 않는다('서울아파트'의 '서울')
COMPANY = re.compile(r"신탁|건설|주택공사|LH|SH공사|공사$|개발$|산업$|자산|시행자")   # '신길10(한국토지신탁)'의 괄호 안은 사업시행자지 단지 이름이 아니다


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
        elif len(d) >= 2:
            short.add(d)                              # 한 글자 동('창동', '목동')은 온전한 이름으로
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
        if len(k) < 2 or re.fullmatch(r"[\d\-_]+", k) or k in DROP_KEYS or k in strong or k in medium or k in weak:
            continue
        if kind == "괄호 안 이름" and COMPANY.search(alias):   # 시행자·신탁사 이름은 사업장 이름이 아니다
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
            "stage": rec.get("stage"), "order": cr.STAGE_ORDER.get(rec.get("stage"), -1), "kind": STAGE_KIND,
            "strong": strong, "medium": medium, "weak": weak, "region": region, "region_full": region_full,
            "queries": queries[:2] or [f"{zone} 재건축"], "google_query": (queries or [f"{zone} 재건축"])[0]}


PAREN_DESC = re.compile(r"고층|저층|임대|상가|제외|\d+\s*동")   # '상계주공15(고층,공무원임대)'의 괄호 안은 이름이 아니다


def complex_profile(key: str, apt: dict, shared: set[str]) -> dict:
    """사업장이 없는 노후 단지(apartments.json 한 줄)의 프로필 — 신규 사업장 후보 감지용.

    단지 이름은 사업장 이름보다 흔하고(현대·주공…) 다른 시·도에 같은 이름이 많아 사업장보다 엄격하게 본다.
    - 5자 이상의 고유한 이름만 이름만으로 믿고, 나머지는 제목에 동·시군구 이름이 함께 있어야 한다.
    - 번호가 있는 이름의 줄기('상계주공9'의 '상계주공', '한신2차'의 '한신')는 버린다 — 제목의 '상계주공7단지'가 9단지에 붙지 않게.
    - 흔한 이름뿐인 단지('대림', '현대')는 동 이름이 제목에 있어야 한다 — 같은 구의 '남산대림'이 이촌동 대림에 붙지 않게.
    - '주공19단지(창동리버타운)'은 괄호 밖·안을 따로 이름으로 쓴다. 질의는 시군구 이름을 앞에 붙인 1건.
    """
    names = list(dict.fromkeys(n for n in [apt.get("name"), *(apt.get("aliases") or [])] if n))
    for n in list(names):
        m = re.fullmatch(r"(.+?)\((.+)\)", n)
        if m:
            names += [m.group(1)] + ([m.group(2)] if not PAREN_DESC.search(m.group(2)) else [])
    names = list(dict.fromkeys(names))
    rec = {"zone_name": apt.get("name") or "?", "_aliases": [(n, "단지 이름") for n in names],
           "address": " ".join(str(apt.get(k) or "") for k in ("region_sigungu", "dong", "jibun")),   # 지역 이름: 시군구·동
           "region_sido": apt.get("region_sido"), "region_sigungu": apt.get("region_sigungu"), "stage": None}
    p = profile(key, rec, shared)
    keys = {**p["strong"], **p["medium"], **p["weak"]}
    numbered = [k for k in keys if re.search(r"\d", k)]
    keys = {k: a for k, a in keys.items()
            if re.search(r"\d", k) or not any(n.startswith(k) and n[len(k)].isdigit() for n in numbered)}
    p["strong"] = {k: a for k, a in keys.items() if k in p["strong"] and len(k) >= 5}
    p["medium"], p["weak"] = {}, {k: a for k, a in keys.items() if k not in p["strong"]}
    # 보여주는 이름이 흔한 이름이면('대림아파트') 다른 별칭이 있어도 동 이름이 제목에 있어야 한다 — '동작구 대림삼거리역 …'이 대방동 대림에 붙지 않게
    if keys and (generic_key(cr.zone_key(apt.get("name") or "")) or all(generic_key(k) for k in keys)):
        dong = apt.get("dong") or ""
        stem = re.sub(r"\d*(동|가)$", "", dong)
        p["region"], p["region_full"] = ({stem} if len(stem) >= 2 else set()), ({dong} if dong else set())
    sgg_short = re.sub(r"(특별시|광역시|시|군|구)$", "", apt.get("region_sigungu") or "")
    core = cr._core(re.sub(r"\(.*?\)", "", rec["zone_name"]))
    q = f"{sgg_short} {core} 재건축" if sgg_short and not core.startswith(sgg_short) else f"{core} 재건축"
    p.update({"kind": NEW_KIND, "complex_id": apt.get("complex_id"), "units": apt.get("households"),
              "dong": apt.get("dong"), "queries": [q], "google_query": q})
    return p


PLACE_AFTER = re.compile(r"^(역|동|구|시|군|삼거리|사거리|로|길|시장|공원|대교|터널|천|산|초등|중학|고등|"          # '대림삼거리역'·'남동구'는 지명
                         r"자산|신탁|건설|산업|개발|물산|엔지니어링|그룹|증권|은행|보험|카드|생명|홀딩스)")   # '하나자산신탁'·'현대건설'은 회사


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


def place_like(title: str, k: str) -> bool:
    """흔한 이름(약한 키)이 원래 제목에서 지명·회사 이름의 일부로만 쓰였는가: '대림삼거리역', '남동구', '하나자산신탁'.
    공백만 뺀 제목에서 키가 나온 자리마다 바로 뒤를 보고, 모두 그런 꼬리면 참(한 번이라도 '대림아파트'처럼 쓰였으면 거짓)."""
    raw = re.sub(r"\s+", "", title)
    ends = [m.end() for m in re.finditer(re.escape(k), raw)]
    return bool(ends) and all(PLACE_AFTER.match(raw[e:]) for e in ends)


def match_name(prof: dict, title: str, desc: str = "") -> str | None:
    """제목이 이 사업장을 가리키면 맞은 별칭을 돌려준다."""
    tk = cr.zone_key(title)
    for k, alias in prof["strong"].items():
        if contains(tk, k):
            return alias
    if CONTEXT.search(title):
        for k, alias in prof["medium"].items():
            if contains(tk, k) and not place_like(title, k):
                return alias
    if any(tok in title for tok in prof["region"]) or any(tok in desc for tok in prof["region_full"]):
        for k, alias in {**prof["medium"], **prof["weak"]}.items():
            if contains(tk, k) and not place_like(title, k):
                return alias
    return None


CONTEXT = re.compile(r"재건축|아파트|정비|조합|단지|주공|재개발|시공")


def stage_for(prof: dict, title: str, desc: str = "", allow_hedged: bool = True) -> tuple[str, str] | None:
    """이 사업장 기준의 단계 판정: 제목이 말줄임표로 나뉘면 사업장 이름이 처음 나온 토막부터만 읽는다 —
    '도봉구 첫 ○○ 입주…△△ 시공자 선정'처럼 앞 토막의 다른 단지 이야기에 속지 않게.
    이름이 어느 토막에도 따로 안 보이면 제목 전체를 쓴다."""
    segs = re.split(r"…|\.{3}", title)
    if len(segs) > 1:   # 지역 이름은 제목 어디에 있어도 된다('강남구, 개포6·7단지 관리처분 인가…가람아파트도 조합 승인'의 '가람')
        idx = next((i for i, s in enumerate(segs) if match_name(prof, s, f"{desc} {title}")), None)
        if idx is not None:
            title = "…".join(segs[idx:])
    return detect_stage(title, allow_hedged)


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


def load_complexes() -> dict[str, dict]:
    """사업장이 없는 노후 단지(docs/data/apartments.json — update_daily.py가 같은 실행에서 먼저 쓴다) 중
    COMPLEX_MIN_UNITS세대 이상. 키 'cx:<complex_id>' → 단지 한 줄."""
    path = cr.BASE / "docs" / "data" / "apartments.json"   # complexes.SITE_FILE (모듈을 불러오지 않고 경로만 쓴다)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {COMPLEX_PREFIX + a["complex_id"]: a for a in data.get("apartments", [])
            if not a.get("project_id") and (a.get("households") or 0) >= COMPLEX_MIN_UNITS and a.get("name")}


def build_profiles(projects: dict[str, dict], apartments: dict[str, dict] | None = None) -> dict[str, dict]:
    """사업장 프로필 + (있으면) 사업장 없는 노후 단지 프로필. 여러 곳이 같이 쓰는 별칭·지역 이름은 이름만으로 못 가린다."""
    apartments = apartments or {}
    owners: dict[str, set[str]] = {}
    places: set[str] = set()          # 동·시군구 이름('산본', '중동')은 단지 이름이어도 이름만으로는 못 가린다
    for pid, rec in projects.items():
        for alias, _ in rec.get("_aliases") or []:
            owners.setdefault(cr.zone_key(alias), set()).add(pid)
        places |= region_tokens(rec)[0]
    for key, apt in apartments.items():
        for alias in [apt.get("name"), *(apt.get("aliases") or [])]:
            owners.setdefault(cr.zone_key(alias or ""), set()).add(key)
        places |= region_tokens({"address": f"{apt.get('dong') or ''} 1", "region_sigungu": apt.get("region_sigungu")})[0]
    shared = {k for k, pids in owners.items() if len(pids) > 1}
    out = {pid: profile(pid, rec, shared | places) for pid, rec in projects.items()}
    out.update({key: complex_profile(key, apt, shared | places) for key, apt in apartments.items()})
    return out


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
        for _ in range(2):                                  # '제목 - 매체명 - 매체명' 꼬리를 뗀다(매체명이 두 번 붙기도 한다)
            if src and title.endswith(src):
                title = title[: -len(src)].rstrip(" -–—‐|·:")
            else:
                stripped = re.sub(r"\s+[-–—‐]\s+[^-–—‐]{1,30}$", "", title)
                if stripped == title:
                    break
                title = stripped
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
        prof = profiles[pid]
        order, kept = prof["order"], {}
        keys = set(prof["strong"]) | set(prof["medium"]) | set(prof["weak"])
        # 같은 기사가 네이버·구글에 다른 제목 꼬리로 들어오면 하나만 남긴다(네이버 링크 우선)
        cands = sorted(st.get("candidates") or [], key=lambda c: (c.get("source") != "네이버", c.get("first_seen") or "", c["id"]))
        for c in cands:
            if prof["kind"] == NEW_KIND:               # 단지 후보는 이름 대조 규칙도 소급한다(제목만) — 맞은 이름을 다시 적는다
                alias = match_name(prof, c["title"])
                if not alias:
                    continue
                c["matched"] = alias
            elif cr.zone_key(c.get("matched") or "") not in keys:   # 더는 별칭으로 안 쓰는 이름으로 맞은 후보는 뺀다
                continue
            # 판정 규칙이 나아지면 저장된 후보도 같은 규칙으로 다시 매긴다(제목 기준이라 결정적)
            det = stage_for(prof, c["title"], "", allow_hedged=c.get("source") != SEOUL_SOURCE)
            if not det:
                continue
            c["stage"], c["confidence"] = det
            key = title_key(c["title"])
            if c["id"] not in dismissed and cr.STAGE_ORDER[c["stage"]] > order and c["published"] >= expire and key not in kept:
                kept[key] = c
        st["candidates"] = list(kept.values())
        if not st["candidates"] and not st["seen"] and not st.get("checked_at"):
            del state[pid]


def title_key(title: str) -> str:
    """중복 판별용 제목 키: '… - 매체명' 꼬리를 떼고 기호·공백을 뺀다."""
    t = re.sub(r"(\s+[-–—‐]\s+[^-–—‐]{1,30})+$", "", title)
    return re.sub(r"[\W_]+", "", t.lower())


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
        det = stage_for(prof, it["title"], it.get("desc", ""))
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
    if not srcs.google:                 # 미리 걸어 둔 조회라도 그 사이 차단됐으면 보내지 않는다
        return [], False
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


POOL = ThreadPoolExecutor(max_workers=2)   # 구글 조회(응답이 2초쯤 걸린다)를 네이버 조회와 나란히, 다음 사업장 것은 미리


def check_project(prof: dict, st: dict, srcs: Sources, today: date, stats: dict, future=None) -> list[dict]:
    """한 사업장을 조회해 새 후보를 돌려준다. future: 미리 걸어 둔 구글 조회(없으면 여기서 건다)."""
    items, ok = [], False
    if future is None and srcs.google:
        future = POOL.submit(fetch_google, prof, st, srcs, stats)
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
    """정보몽땅 고시·공고를 서울 사업장(과 사업장 없는 서울 노후 단지 — 신규 사업장 후보)의 이름과 대조한다."""
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
        if not detect_stage(n["title"], allow_hedged=False):   # 단계 표현이 아예 없는 공고는 건너뛴다
            continue
        for prof in seoul:
            alias = match_name(prof, n["title"], n["dept"])     # 등록기관(자치구)이 지역 이름 구실을 한다
            if not alias:
                continue
            det = stage_for(prof, n["title"], n["dept"], allow_hedged=False)
            if not det or cr.STAGE_ORDER[det[0]] <= prof["order"]:
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


# ---------------------------------------------------------------- 자동 반영 (auto_stage.csv · auto_projects.csv)
# 사람이 검토하지 않아도 되게, 확신할 수 있는 후보만 update_daily.py가 읽는 파일에 적는다.
#   - 같은 사업장(또는 단지)·같은 단계를 '확정'으로 전한 서로 다른 제목의 기사가 AUTO_MIN_ARTICLES건 이상이거나, 정보몽땅 고시가 1건이라도 있을 때
#   - 사업장 단계는 현재보다 AUTO_MAX_JUMP단계 넘게 건너뛰지 않을 때(조합설립 → 준공 같은 오탐 차단)
#   - 신규 사업장(단지)은 AUTO_NEW_MAX_STAGE 이하의 단계일 때(그 뒤 단계면 공공 자료에 있어야 정상)
#   - 회사 이름(신탁사·시행자)으로 맞은 기사는 세지 않는다
# 같은 사업장에 이미 적힌 행보다 앞선 단계만 다시 적고, 수동 파일(manual_*.csv)의 행이 같은 단계면 수동이 앞선다.
AUTO_MIN_ARTICLES = 2
AUTO_MAX_JUMP = 4
AUTO_NEW_MAX_STAGE = "조합설립인가"
AUTO_STAGE_COLS = ["project_id", "stage", "date", "evidence_url", "note"]
AUTO_PROJECT_COLS = ["complex_id", "name", "stage", "date", "evidence_url", "note"]


def read_auto(path, cols: list[str]) -> dict[str, dict]:
    """자동 반영 파일 → {첫 열 값: 행}. 없으면 빈 dict."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {r[cols[0]]: {c: (r.get(c) or "") for c in cols} for r in csv.DictReader(f) if r.get(cols[0])}


def write_auto(path, cols: list[str], rows: dict[str, dict]) -> bool:
    """정렬된 고정 형식으로 쓴다(내용이 같으면 파일도 같다). → 바뀌었는지"""
    out = []
    w = csv.writer(_Buf(out), lineterminator="\n")
    w.writerow(cols)
    for k in sorted(rows):
        w.writerow([rows[k].get(c, "") for c in cols])
    text = "".join(out)
    old = path.read_text(encoding="utf-8-sig") if path.exists() else None
    if old == text:
        return False
    path.write_text(text, encoding="utf-8-sig", newline="\n")
    return True


class _Buf:
    def __init__(self, out: list):
        self.out = out

    def write(self, s: str) -> None:
        self.out.append(s)


def auto_apply(rows: list[dict], profiles: dict[str, dict], today: date, write: bool = True) -> list[dict]:
    """대기열에서 확신할 수 있는 후보를 골라 auto_stage.csv(사업장 단계) / auto_projects.csv(신규 사업장)에 적는다.
    → [{key, name, kind, stage, n, new(이번에 새로 적음), article_ids}] — 적용된(또는 이미 적혀 있는) 후보는 대기열에서 뺀다."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if r["confidence"] != "확정" or COMPANY.search(r.get("matched_alias") or ""):
            continue
        key = r["project_id"] or (COMPLEX_PREFIX + (r["complex_id"] or ""))
        groups.setdefault((key, r["article_stage"]), []).append(r)
    stages = read_auto(cr.AUTO_STAGE_FILE, AUTO_STAGE_COLS)
    projects = read_auto(cr.AUTO_PROJECTS_FILE, AUTO_PROJECT_COLS)
    manual_stage = {k: v for k, v in cr.load_manual_stage().items() if v.get("source") == cr.MANUAL_STAGE_SOURCE}
    applied: list[dict] = []
    for (key, stage), rs in groups.items():
        prof = profiles.get(key)
        if not prof:
            continue
        titles: dict[str, dict] = {}
        for r in sorted(rs, key=lambda r: (r["published"], r["article_id"])):
            titles.setdefault(title_key(r["title"]), r)
        notice = any(r["source"] == SEOUL_SOURCE for r in rs)
        if len(titles) < AUTO_MIN_ARTICLES and not notice:
            continue
        order = cr.STAGE_ORDER[stage]
        first = sorted(titles.values(), key=lambda r: (r["source"] != SEOUL_SOURCE, r["published"]))[0]
        note = f"자동 반영 {today.isoformat()}: 확정 기사 {len(titles)}건 — " + " | ".join(r["title"][:60] for r in list(titles.values())[:3])
        item = {"key": key, "name": prof["name"], "kind": prof["kind"], "stage": stage, "n": len(titles), "new": False,
                "article_ids": [r["article_id"] for r in rs], "link": first["link"]}
        if prof["kind"] == STAGE_KIND:
            if order - prof["order"] > AUTO_MAX_JUMP:
                continue
            if cr.STAGE_ORDER.get((manual_stage.get(key) or {}).get("stage"), -1) >= order:
                continue                                   # 사람이 적은 단계가 이미 같거나 앞선다
            have = stages.get(key)
            if have and cr.STAGE_ORDER.get(cr.norm_stage(have["stage"]), -1) >= order:
                applied.append(item)                       # 이미 적혀 있다(update_daily가 아직 반영 전이거나 오늘 반영됨)
                continue
            stages[key] = {"project_id": key, "stage": stage, "date": first["published"], "evidence_url": first["link"], "note": note}
        else:
            if order > cr.STAGE_ORDER[AUTO_NEW_MAX_STAGE]:
                continue
            cid = prof.get("complex_id") or ""
            have = projects.get(cid)
            if have and cr.STAGE_ORDER.get(cr.norm_stage(have["stage"]), -1) >= order:
                applied.append(item)
                continue
            projects[cid] = {"complex_id": cid, "name": (have or {}).get("name") or prof["name"], "stage": stage,
                             "date": first["published"], "evidence_url": first["link"], "note": note}
        item["new"] = True
        applied.append(item)
    if write:
        changed = write_auto(cr.AUTO_STAGE_FILE, AUTO_STAGE_COLS, stages) if any(a["new"] and a["kind"] == STAGE_KIND for a in applied) else False
        changed |= write_auto(cr.AUTO_PROJECTS_FILE, AUTO_PROJECT_COLS, projects) if any(a["new"] and a["kind"] == NEW_KIND for a in applied) else False
        if changed:
            print(f"[자동 반영] {cr.AUTO_STAGE_FILE.name} / {cr.AUTO_PROJECTS_FILE.name} 갱신 — update_daily.py가 다음 실행 때 반영")
    return applied


# ---------------------------------------------------------------- 출력
QUEUE_COLS = ["kind", "project_id", "complex_id", "region_sido", "region_sigungu", "dong", "zone_name", "current_stage",
              "article_stage", "confidence", "published", "title", "link", "source", "matched_alias", "first_seen", "article_id"]


def queue_rows(state: dict[str, dict], profiles: dict[str, dict]) -> list[dict]:
    """kind='단계 변경'(사업장, project_id) 또는 '신규 사업장 후보'(사업장 없는 노후 단지, complex_id)."""
    rows = []
    for key, st in state.items():
        p = profiles.get(key)
        if not p:
            continue
        new = p["kind"] == NEW_KIND
        for c in st.get("candidates") or []:
            rows.append({"kind": p["kind"], "project_id": None if new else key, "complex_id": p.get("complex_id") if new else None,
                         "region_sido": p["sido"], "region_sigungu": p["sgg"], "zone_name": p["name"], "dong": p.get("dong"),
                         "current_stage": p["stage"], "article_stage": c["stage"], "confidence": c["confidence"],
                         "published": c["published"], "title": c["title"], "link": c["link"], "source": c["source"],
                         "matched_alias": c["matched"], "first_seen": c["first_seen"], "article_id": c["id"]})
    rows.sort(key=lambda r: (r["published"], r["project_id"] or r["complex_id"] or "", r["article_id"]), reverse=True)
    return rows


def write_outputs(rows: list[dict], today: date) -> None:
    QUEUE_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=QUEUE_COLS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    items = [{"id": r["article_id"], "kind": r["kind"], "project_id": r["project_id"], "complex_id": r["complex_id"],
              "zone_name": r["zone_name"], "region_sido": r["region_sido"], "dong": r.get("dong"),
              "region_sigungu": r["region_sigungu"], "stage": r["current_stage"], "article_stage": r["article_stage"],
              "confidence": r["confidence"], "published": r["published"], "title": r["title"], "link": r["link"],
              "source": r["source"], "matched": r["matched_alias"], "first_seen": r["first_seen"]} for r in rows]
    items = [{k: v for k, v in it.items() if v is not None} for it in items]
    data = {"generated_at": today.isoformat(), "count": len(items), "items": items}
    SITE_REVIEW.parent.mkdir(parents=True, exist_ok=True)
    SITE_REVIEW.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                           encoding="utf-8", newline="\n")


def write_summary(today: date, stats: dict, new: list[dict], rows: list[dict], srcs: Sources | None, profiles: dict,
                  applied: list[dict] | None = None) -> None:
    sure = sum(r["confidence"] == "확정" for r in rows)
    newcx = sum(r["kind"] == NEW_KIND for r in rows)
    applied = applied or []
    lines = [f"## 뉴스·고시 감시 {today.isoformat()}", "",
             f"- 조회 사업장 {stats['checked']}곳 · 사업장 없는 노후 단지 {stats.get('checked_cx', 0)}곳 "
             f"(네이버 {stats['naver_calls']}건 · 구글 {stats['google_calls']}건 호출, "
             f"새 기사 {stats['articles']}건 판정) · 정비사업 정보몽땅 고시·공고 {stats.get('seoul_notices', 0)}건 확인",
             f"- 새 후보 {len(new)}건 · 검토 대기열 {len(rows)}건 (확정 {sure} · 예정 {len(rows) - sure} · 그중 신규 사업장 후보 {newcx})",
             f"- 자동 반영 {sum(a['new'] for a in applied)}건 새로 적음 (auto_stage.csv·auto_projects.csv, 적힌 채 대기 {sum(not a['new'] for a in applied)}건)"]
    for a in applied:
        if a["new"]:
            lines.append(f"  - {'신규 사업장 ' if a['kind'] == NEW_KIND else ''}{a['name']} → {a['stage']} (확정 기사 {a['n']}건, [근거]({a['link']}))")
    for n in (srcs.notes if srcs else []):
        lines.append(f"- ⚠️ {n}")
    if new:
        lines += ["", "| 사업장·단지 | 현재 → 기사 단계 | 기사 | 날짜 |", "|---|---|---|---|"]
        for c in new[:40]:
            p = profiles.get(c["_pid"], {})
            tag = " (신규 사업장 후보)" if p.get("kind") == NEW_KIND else ""
            lines.append(f"| {p.get('name', c['_pid'])} ({p.get('sgg', '')}){tag} | {p.get('stage') or '사업 미지정'} → {c['stage']} ({c['confidence']}) "
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
    ap.add_argument("--only", help="이름에 이 말이 든 사업장·단지만(쉼표로 여러 개)")
    ap.add_argument("--limit", type=int, help="최대 이만큼의 사업장만 조회(오래 안 본 순)")
    ap.add_argument("--complex-limit", type=int, default=COMPLEX_DAILY, metavar="N",
                    help=f"사업장 없는 노후 단지(신규 사업장 후보)는 이만큼만 조회(기본 {COMPLEX_DAILY}, 0이면 안 함)")
    ap.add_argument("--force", action="store_true", help="오늘 이미 조회한 사업장도 다시 조회")
    ap.add_argument("--no-naver", action="store_true")
    ap.add_argument("--no-google", action="store_true")
    ap.add_argument("--no-seoul", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="조회 없이 저장된 후보로 출력 파일만 다시 만든다")
    ap.add_argument("--no-auto", action="store_true", help="확신 후보를 auto_stage.csv·auto_projects.csv에 자동 반영하지 않는다(판정만 출력)")
    ap.add_argument("--date", default=date.today().isoformat(), help="기록용 날짜(테스트용)")
    args = ap.parse_args()
    today = date.fromisoformat(args.date)

    projects = load_projects()
    profiles = build_profiles(projects, load_complexes())
    state = load_state()
    before = serialize(state)
    dismissed = load_dismissed()
    stats = {"checked": 0, "checked_cx": 0, "naver_calls": 0, "google_calls": 0, "articles": 0, "errors": 0}
    new: list[dict] = []
    srcs, todo = None, []
    if not args.rebuild:
        srcs = Sources(not args.no_naver, not args.no_google, not args.no_seoul)
        todo = [p for p in profiles.values() if p["kind"] == STAGE_KIND and p["order"] < cr.STAGE_ORDER[LAST_STAGE]]
        todo_cx = [p for p in profiles.values() if p["kind"] == NEW_KIND]
        if args.only:
            words = [w.strip() for w in args.only.split(",") if w.strip()]
            pick = lambda p: any(w in p["name"] or w in " ".join({**p["strong"], **p["weak"]}.values()) for w in words)  # noqa: E731
            todo, todo_cx = [p for p in todo if pick(p)], [p for p in todo_cx if pick(p)]
        for lst in (todo, todo_cx):
            lst.sort(key=lambda p: ((state.get(p["pid"]) or {}).get("checked_at") or "", p["pid"]))  # 오래 안 본 곳부터
        if not args.force:
            todo = [p for p in todo if (state.get(p["pid"]) or {}).get("checked_at") != today.isoformat()]
            todo_cx = [p for p in todo_cx if (state.get(p["pid"]) or {}).get("checked_at") != today.isoformat()]
        if args.limit:
            todo = todo[: args.limit]
        todo_cx = todo_cx[: max(args.complex_limit, 0)]
        n_proj = len(todo)
        todo += todo_cx   # 사업장을 먼저(한도·시간이 모자라면 단지 후보가 밀린다), 그 뒤 사업장 없는 노후 단지
        print(f"[뉴스] 사업장 {n_proj}곳 · 사업장 없는 노후 단지 {len(todo_cx)}곳 조회 "
              f"(네이버 {'O' if srcs.naver else 'X'} · 구글 {'O' if srcs.google else 'X'})")
        ahead: dict[str, object] = {}       # 다음 사업장의 구글 조회를 미리 걸어 둔다(구글 응답 대기가 전체 시간을 좌우한다)

        def prefetch(j: int) -> None:
            if j < len(todo) and srcs.google:
                p = todo[j]
                ahead[p["pid"]] = POOL.submit(fetch_google, p, state.setdefault(p["pid"], blank(p["pid"])), srcs, stats)

        prefetch(0)
        for i, prof in enumerate(todo, 1):
            if not srcs.naver and not srcs.google:
                print("   ! 쓸 수 있는 뉴스 출처가 없어 중단 — 남은 사업장은 다음 실행에서", file=sys.stderr)
                break
            prefetch(i)
            st = state.setdefault(prof["pid"], blank(prof["pid"]))
            try:
                for c in check_project(prof, st, srcs, today, stats, ahead.pop(prof["pid"], None)):
                    new.append({**c, "_pid": prof["pid"]})
                    print(f"   + {prof['name']}{' [신규 후보]' if prof['kind'] == NEW_KIND else ''}: {prof['stage'] or '사업 미지정'} → "
                          f"{c['stage']}({c['confidence']}) · {c['title'][:70]} · {c['published']}")
            except Exception as e:  # 한 사업장의 예외로 전체 실행을 잃지 않는다
                stats["errors"] += 1
                print(f"   ! {prof['name']}: {type(e).__name__}: {e}", file=sys.stderr)
            stats["checked_cx" if prof["kind"] == NEW_KIND else "checked"] += 1
            if i % 50 == 0:
                print(f"   … {i}/{len(todo)} (네이버 {stats['naver_calls']}건 · 구글 {stats['google_calls']}건)")
        if srcs.seoul and (args.force or (state.get("_seoul") or {}).get("checked_at") != today.isoformat()) and not args.only:
            try:
                for c in check_seoul(profiles, state, today, stats):
                    new.append(c)
                    p = profiles[c["_pid"]]
                    print(f"   + [고시] {p['name']}{' [신규 후보]' if p['kind'] == NEW_KIND else ''}: {c['title'][:80]} → {c['stage']} · {c['published']}")
            except (requests.RequestException, RuntimeError) as e:
                srcs.notes.append(f"{SEOUL_SOURCE}: {e}")
                print(f"   ! {SEOUL_SOURCE}: {e}", file=sys.stderr)

    prune(state, profiles, dismissed, today)
    rows = queue_rows(state, profiles)
    applied = auto_apply(rows, profiles, today, write=not (args.dry_run or args.no_auto))
    done = {aid for a in applied for aid in a["article_ids"]}
    rows = [r for r in rows if r["article_id"] not in done]   # 자동 반영된(또는 반영 대기 중인) 후보는 대기열에서 뺀다
    for a in applied:
        if a["new"]:
            print(f"   = 자동 반영: {a['name']}{' [신규 사업장]' if a['kind'] == NEW_KIND else ''} → {a['stage']} (확정 기사 {a['n']}건)")
    changed = serialize(state) != before
    if args.dry_run:
        print(f"[dry-run] 새 후보 {len(new)}건 · 대기열 {len(rows)}건 · 자동 반영 {sum(a['new'] for a in applied)}건 (저장 안 함)")
    else:
        if changed:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(serialize(state), encoding="utf-8", newline="\n")
        write_outputs(rows, today)
        print(f"[저장] 대기열 {len(rows)}건 → {QUEUE_CSV.relative_to(cr.BASE)}, {SITE_REVIEW.relative_to(cr.BASE)}"
              + ("" if changed else " (상태 변경 0건)"))
    write_summary(today, stats, new, rows, srcs, profiles, applied)
    if srcs and todo and stats["checked"] + stats["checked_cx"] == 0 and "seoul_notices" not in stats:
        sys.exit(1)   # 뉴스 출처도 고시 목록도 하나도 못 읽었다(인증·차단) — Actions 로그에 알린다


if __name__ == "__main__":
    main()
