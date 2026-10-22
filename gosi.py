"""지자체 고시·공고 감시(국토이음 고시정보) — 수도권 아파트 재건축 고시를 읽어 진행 단계를 '고시' 출처로 반영한다.

    python gosi.py                      # 매일(워크플로): 최근 DAILY_DAYS일의 고시를 읽고 notice_stage.csv·notice_projects.csv·미연결 목록을 다시 쓴다
    python gosi.py --since 2025-10-22   # 그 날짜부터 읽는다(첫 실행·따라잡기). 상태가 비어 있으면 자동으로 BACKFILL_DAYS 전부터
    python gosi.py --dry-run            # 저장하지 않고 결과만 출력
    python gosi.py --rebuild            # 조회 없이 저장된 고시로 연결·출력 파일만 다시 만든다

출처 (공개 페이지, 키 없음, 요청 간격 GAP초)
  국토이음(토지이음) 고시정보 https://www.eum.go.kr/web/gs/gv/gvGosiList.jsp — 전국 지자체가 등록하는 도시계획 관련 고시.
  재건축에서는 정비구역 지정·변경(정비계획 결정), 사업시행계획(변경)인가, 관리처분계획(변경)인가, 노후계획도시 특별정비구역·
  특별정비계획, 사업시행자 지정, 준공인가·이전고시가 실린다. 도정법 재건축의 조합설립인가·추진위원회 승인은 지형도면이 없어
  대개 여기 없다(공공 출처·뉴스가 맡는다). 제목 검색(zonenm)·날짜 범위를 GET으로 보내며 EUC-KR로 인코딩해야 한다.
  고시일·고시번호(기관 포함)·제목·담당기관을 목록에서 읽고, 상세 페이지(gvGosiDet.jsp?seq=…)를 근거 링크로 쓴다.

판정 (제목만 본다 — 고시 제목은 정식 표현이라 결정적이다)
  - 재건축(노후계획도시 특별정비 포함)만: 재개발·가로주택·소규모·모아타운·도시정비형·주거환경·취소·해제·정정 고시는 뺀다.
  - 단계는 제목의 표현으로 정한다(STAGE_RX). '(변경)인가'·'변경 결정'은 그 단계를 이미 지났다는 근거다 — 날짜는 변경 고시일이므로
    note에 '변경 고시'를 남긴다(원래 인가일은 더 이르다).
연결
  - 같은 시도(가능하면 같은 시군)의 사업장 별칭과 대조(news.match_name) → 하나만 맞으면 연결, 여럿이면 제목의 구역명 키와 같은 것.
  - 맞는 사업장이 없고 사업장 없는 노후 단지가 하나 맞으면 notice_projects.csv로 등록(update_daily가 사업장을 만든다).
  - 못 붙인 고시는 output/notices_unlinked_latest.csv에 남고, 실행마다 다시 대조한다(사업장·별칭이 늘면 붙는다).
상태·출력
  state/notices.jsonl                  seq별 고시(날짜·기관·제목·단계·구역명·연결 결과)
  notice_stage.csv                     project_id, stage, date, evidence_url, note → update_daily가 stage_source='고시'로 반영
  notice_projects.csv                  complex_id, name, stage, date, evidence_url, note, project_id → 고시로 등록되는 사업장
  output/notices_unlinked_latest.csv   어느 사업장·단지에도 못 붙인 재건축 고시
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import date, timedelta

import requests

import collect_reconstruction as cr
import news

LIST_URL = "https://www.eum.go.kr/web/gs/gv/gvGosiList.jsp"
DETAIL_URL = "https://www.eum.go.kr/web/gs/gv/gvGosiDet.jsp?seq={seq}"
STATE_FILE = cr.BASE / "state" / "notices.jsonl"
UNLINKED_FILE = cr.OUT / "notices_unlinked_latest.csv"
KEYWORDS = ["재건축", "정비구역", "정비계획", "사업시행계획", "관리처분", "조합설립", "추진위원회", "특별정비", "준공인가", "이전고시"]
DAILY_DAYS = 14        # 매일 이만큼 되돌아본다(등록이 며칠 늦는 고시가 있다; seq로 중복을 거른다)
BACKFILL_DAYS = 365    # 상태가 비어 있을 때(첫 실행) 되돌아보는 기간
GAP = 1.0              # 요청 간격(초)
LIST_SIZE = 50
MAX_PAGES = 40
TIMEOUT = (10, 60)
SIDOS = ("서울특별시", "경기도", "인천광역시")

SESSION = requests.Session()
SESSION.headers.update(news.UA)

TAG = re.compile(r"^\s*\[(신규|변경|폐지|정정)\]\s*")
SKIP = re.compile(r"재개발|가로주택|소규모|모아타운|도시정비형|주거환경|자율주택|도시재생|지적재조사|주택건설사업|도시개발사업|리모델링|"
                  r"취소|해제|폐지|정정|실효|직권")
KIND = re.compile(r"재건축|특별정비|노후계획도시")
STAGE_RX = [   # 진전된 단계부터 — 제목에 여럿 있으면 가장 진전된 것
    ("이전고시·청산", re.compile(r"이전\s*고시")),
    ("준공", re.compile(r"준공\s*인가")),
    ("관리처분인가", re.compile(r"관리처분\s*(계획)?\s*(\(?변경\)?)?\s*인가")),
    ("사업시행인가", re.compile(r"사업시행\s*(계획)?\s*(\(?변경\)?)?\s*인가")),
    ("조합설립인가", re.compile(r"조합설립\s*(\(?변경\)?)?\s*인가|사업시행자\s*지정")),
    ("추진위구성", re.compile(r"추진위원회\s*(구성)?\s*승인")),
    ("정비구역지정", re.compile(r"정비구역\s*(의\s*)?(지정|변경)|정비계획\s*(결정|수립|변경)|특별정비구역\s*(지정|변경)|특별정비계획\s*(결정|변경)")),
    ("예정구역·후보지", re.compile(r"정비예정구역\s*(지정|변경)")),
]
CHANGE = re.compile(r"변경|경미")
ZONE_END = re.compile(r"\s*(주택)?재건축|\s*(노후계획도시\s*)?특별정비|\s*정비기본계획|\s*정비계획|\s*정비구역|\s*사업시행|\s*관리처분|"
                      r"\s*조합설립|\s*추진위원회|\s*준공|\s*이전\s*고시|\s*도시관리계획")
ORG_SGG = re.compile(r"^([가-힣]{1,6}(?:시|군|구))(?=\s|$)")


# ---------------------------------------------------------------- 조회
def fetch(keyword: str, since: date, until: date, stats: dict) -> list[dict]:
    """제목에 keyword가 든 고시를 since~until 기간으로, 최신 쪽부터 읽는다(목록 50행씩)."""
    out, seen = [], set()
    for page in range(1, MAX_PAGES + 1):
        params = {"pageNo": page, "zonenm": keyword, "startdt": since.isoformat(), "enddt": until.isoformat(), "listSize": LIST_SIZE}
        qs = "&".join(f"{k}={urllib.parse.quote(str(v).encode('euc-kr'))}" for k, v in params.items())
        r = SESSION.get(f"{LIST_URL}?{qs}", timeout=TIMEOUT)
        r.raise_for_status()
        r.encoding = "euc-kr"
        stats["calls"] += 1
        rows = parse_list(r.text)
        if not rows:
            if page == 1 and "gvGosiDet.jsp" not in r.text and "고시제목" not in r.text:
                raise RuntimeError("국토이음 고시 목록을 읽지 못함(페이지 형식 변경?)")
            break
        for row in rows:
            if row["seq"] not in seen:
                seen.add(row["seq"])
                out.append(row)
        if len(rows) < LIST_SIZE or rows[-1]["date"] < since.isoformat():
            break
        time.sleep(GAP)
    return out


def parse_list(text: str) -> list[dict]:
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 4:
            continue
        cells = [html.unescape(re.sub(r"<[^>]+>|\s+", " ", c)).strip() for c in tds]
        seq = re.search(r"seq=(\d+)", row)
        if not seq or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        out.append({"seq": seq.group(1), "date": cells[0], "number": cells[1], "title": cells[2], "org": cells[3]})
    return out


# ---------------------------------------------------------------- 판정
def region_of(number: str, org: str, regions: dict[str, str]) -> tuple[str | None, str | None]:
    """고시번호 칸('경기도 안성시 고시 제2026-1호')과 담당기관('광명시', '인천광역시 계양구', '서울특별시 도봉구 도시…')에서
    (시도, 시군구). 시도가 안 적힌 '광명시'는 사업장 지역 표(regions)로 시도를 찾는다. 시도 자체의 고시는 시군구가 없다."""
    for text in (number, org):
        t = re.sub(r"\s+", " ", text or "").strip()
        t = re.sub(r"\s*(고시|공고)\s*제?\s*\d{4}-\d+\s*호.*$", "", t)
        sido = next((s for s in SIDOS if t.startswith(s)), None)
        rest = t[len(sido):].strip() if sido else t
        m = ORG_SGG.match(rest)
        sgg = m.group(1) if m else None
        if sido and sgg and sgg in regions and regions[sgg] != sido:
            sgg = None                                  # '경기도 중구'처럼 안 맞는 조합은 시군구를 버린다
        if not sido and sgg and sgg in regions:
            sido = regions[sgg]
        if sido:
            return sido, sgg
    return None, None


def classify(title: str) -> tuple[str, str, str, bool] | None:
    """제목 → (단계, 구역명, [신규|변경|…] 꼬리표, 변경 여부). 재건축이 아니거나 단계가 없으면 None."""
    m = TAG.match(title)
    tag = m.group(1) if m else ""
    t = TAG.sub("", title).strip()
    if not KIND.search(t) or SKIP.search(t):
        return None
    for stage, rx in STAGE_RX:
        if rx.search(t):
            zone = ZONE_END.split(t, 1)[0].strip(" ,·-") or t
            return stage, zone, tag, bool(CHANGE.search(t)) or tag == "변경"
    return None


# ---------------------------------------------------------------- 연결
def link(n: dict, profiles: dict[str, dict]) -> tuple[str | None, str | None, str]:
    """고시 → (project_id, complex_id, 설명). 같은 시도(가능하면 같은 시군)의 사업장 별칭과 대조한다."""
    sido, sgg = n.get("sido"), n.get("sgg")
    pool = [p for p in profiles.values() if p.get("sido") == sido]
    if sgg:
        narrow = [p for p in pool if (p.get("sgg") or "").startswith(sgg) or sgg.startswith(p.get("sgg") or "\0")]
        pool = narrow or pool
    desc = f"{n.get('org') or ''} {n.get('number') or ''}"
    matched = {p["pid"]: news.match_name(p, n["title"], desc) for p in pool if p["kind"] == news.STAGE_KIND}
    hits = [p for p in pool if p["kind"] == news.STAGE_KIND and matched.get(p["pid"])]
    if len(hits) > 1:
        # 여럿이면 제목의 구역명과 같은 이름 → 가장 길게 맞은 별칭('호계럭키아파트' > '럭키', '신반포4지구' > '신반포4')
        zk = cr.zone_key(n.get("zone") or "")
        zks = {zk, re.sub(r"(지구|구역|단지)$", "", zk)}
        exact = [p for p in hits if cr.zone_key(p["name"]) in zks or any(k in zks for k in p["strong"])]
        if len(exact) == 1:
            hits = exact
        else:
            best = max(len(cr.zone_key(matched[p["pid"]])) for p in hits)
            longest = [p for p in hits if len(cr.zone_key(matched[p["pid"]])) == best]
            hits = longest if len(longest) == 1 else hits
    if len(hits) == 1:
        return hits[0]["pid"], None, f"사업장 연결({hits[0]['name']})"
    if len(hits) > 1:
        return None, None, "사업장 여럿에 맞음(모호): " + ", ".join(p["name"] for p in hits[:4])
    cx = [p for p in pool if p["kind"] == news.NEW_KIND and news.match_name(p, n["title"], desc)]
    if len(cx) == 1:
        return None, cx[0]["complex_id"], f"노후 단지 등록({cx[0]['name']})"
    if len(cx) > 1:
        return None, None, "노후 단지 여럿에 맞음(모호): " + ", ".join(p["name"] for p in cx[:4])
    return None, None, "맞는 사업장·단지 없음"


# ---------------------------------------------------------------- 상태·출력
def load_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    out = {}
    for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["seq"]] = r
    return out


def save_state(state: dict[str, dict]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8", newline="\n") as f:
        for seq in sorted(state, key=int):
            f.write(json.dumps(state[seq], ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path, cols: list[str], rows: list[dict]) -> bool:
    """정렬된 고정 형식으로 쓴다(내용이 같으면 파일도 같다). 바뀌었으면 True."""
    old = path.read_bytes() if path.exists() else None
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    if data == old:
        return False
    path.write_bytes(data)
    return True


def note_for(n: dict) -> str:
    base = f"고시: {n['number']} — {TAG.sub('', n['title'])}"
    return base + (" (변경 고시: 원래 인가·지정일은 이보다 이르다)" if n.get("change") else "")


def build_outputs(state: dict[str, dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """연결된 고시 → 사업장별 가장 진전된 단계(같은 단계면 가장 이른 고시일), 단지 등록 행, 미연결 목록."""
    by_pid: dict[str, dict] = {}
    by_cx: dict[str, dict] = {}
    unlinked = []
    for n in sorted(state.values(), key=lambda r: (r["date"], r["seq"])):
        if not n.get("stage"):
            continue
        row = {"stage": n["stage"], "date": n["date"], "evidence_url": DETAIL_URL.format(seq=n["seq"]), "note": note_for(n)}
        if n.get("project_id"):
            cur = by_pid.get(n["project_id"])
            if cur is None or cr.STAGE_ORDER[n["stage"]] > cr.STAGE_ORDER[cur["stage"]] or \
                    (cr.STAGE_ORDER[n["stage"]] == cr.STAGE_ORDER[cur["stage"]] and n["date"] < cur["date"]):
                by_pid[n["project_id"]] = {"project_id": n["project_id"], **row}
        elif n.get("complex_id"):
            cur = by_cx.get(n["complex_id"])
            if cur is None or cr.STAGE_ORDER[n["stage"]] > cr.STAGE_ORDER[cur["stage"]]:
                by_cx[n["complex_id"]] = {"complex_id": n["complex_id"], "name": n.get("zone") or "", **row, "project_id": ""}
        else:
            unlinked.append({"date": n["date"], "sido": n.get("sido"), "sgg": n.get("sgg"), "stage": n["stage"], "zone": n.get("zone"),
                             "title": TAG.sub("", n["title"]), "number": n["number"], "why": n.get("why"), "link": DETAIL_URL.format(seq=n["seq"])})
    return ([by_pid[k] for k in sorted(by_pid)], [by_cx[k] for k in sorted(by_cx)], sorted(unlinked, key=lambda r: (r["date"], r["title"]), reverse=True))


# ---------------------------------------------------------------- 실행
def regions_from_state() -> dict[str, str]:
    """시군구 이름 → 시도 (state/projects.jsonl의 사업장 지역). '성남시 분당구'는 '성남시'로도 넣는다."""
    out: dict[str, str] = {}
    path = cr.BASE / "state" / "projects.jsonl"
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line).get("record") or {}
        sido, sgg = rec.get("region_sido"), rec.get("region_sigungu")
        if sido in SIDOS and sgg:
            out[sgg] = sido
            out[sgg.split(" ")[0]] = sido
    return out


def write_summary(today: date, stats: dict, new: list[dict], stage_rows: list[dict], cx_rows: list[dict], unlinked: list[dict],
                  changed: list[str], error: str | None) -> None:
    lines = [f"## 고시 감시(국토이음) {today.isoformat()}", "",
             f"- 조회 {stats['calls']}건 · 새 고시 {len(new)}건(수도권 재건축) · 누적 {stats['total']}건",
             f"- 사업장 단계 근거 {len(stage_rows)}곳 · 고시로 등록되는 단지 {len(cx_rows)}곳 · 미연결 {len(unlinked)}건",
             ] + ([f"- ⚠️ {error}"] if error else []) + ([f"- 바뀐 파일: {', '.join(changed)}"] if changed else [])
    if new:
        lines += ["", "| 고시일 | 기관 | 단계 | 제목 | 연결 |", "|---|---|---|---|---|"]
        lines += [f"| {n['date']} | {n.get('org') or ''} | {n.get('stage') or '-'} | [{TAG.sub('', n['title'])[:60]}]({DETAIL_URL.format(seq=n['seq'])}) | "
                  f"{n.get('project_id') or n.get('complex_id') or n.get('why') or ''} |" for n in new[:40]]
    text = "\n".join(lines)
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="이 날짜부터 읽는다(YYYY-MM-DD). 기본: 최근 14일, 상태가 비어 있으면 1년")
    ap.add_argument("--dry-run", action="store_true", help="저장하지 않고 결과만 출력")
    ap.add_argument("--rebuild", action="store_true", help="조회 없이 저장된 고시로 연결·출력만 다시")
    ap.add_argument("--date", default=date.today().isoformat(), help="기록용 날짜(테스트용)")
    args = ap.parse_args()
    today = date.fromisoformat(args.date)

    state = load_state()
    regions = regions_from_state()
    stats = {"calls": 0, "total": 0}
    new: list[dict] = []
    error = None
    if not args.rebuild:
        since = date.fromisoformat(args.since) if args.since else today - timedelta(days=DAILY_DAYS if state else BACKFILL_DAYS)
        print(f"[고시] 국토이음 {since} ~ {today} · 검색어 {len(KEYWORDS)}개")
        try:
            for kw in KEYWORDS:
                for row in fetch(kw, since, today, stats):
                    if row["seq"] in state:
                        continue
                    sido, sgg = region_of(row["number"], row["org"], regions)
                    if not sido:
                        continue                                   # 수도권 밖
                    c = classify(row["title"])
                    if not c:
                        continue                                   # 재건축 단계 고시가 아님
                    stage, zone, tag, change = c
                    state[row["seq"]] = {**row, "sido": sido, "sgg": sgg, "stage": stage, "zone": zone, "tag": tag, "change": change,
                                         "first_seen": today.isoformat()}
                    new.append(state[row["seq"]])
                time.sleep(GAP)
        except Exception as e:  # 한 검색어가 실패해도 받은 것까지는 반영한다(실패는 요약에 남긴다)
            error = f"국토이음 조회 실패: {e}"
            print(f"   ! {error}", file=sys.stderr)

    # 연결은 매번 다시 한다 — 사업장·별칭이 늘면 전에 못 붙인 고시도 붙는다
    profiles = news.build_profiles(news.load_projects(), news.load_complexes())
    for n in state.values():
        if not n.get("stage"):
            continue
        pid, cx, why = link(n, profiles)
        n["project_id"], n["complex_id"], n["why"] = pid, cx, why
        n["last_linked"] = today.isoformat()
    stats["total"] = sum(1 for n in state.values() if n.get("stage"))
    stage_rows, cx_rows, unlinked = build_outputs(state)
    for n in new:
        print(f"   + {n['date']} {n.get('org') or ''} · {n['stage']} · {TAG.sub('', n['title'])[:70]} → {n.get('project_id') or n.get('complex_id') or n.get('why')}")
    changed: list[str] = []
    if args.dry_run:
        print(f"[dry-run] 새 고시 {len(new)}건 · 단계 근거 {len(stage_rows)}곳 · 단지 등록 {len(cx_rows)}곳 · 미연결 {len(unlinked)}건 (저장 안 함)")
    else:
        save_state(state)
        if write_csv(cr.NOTICE_STAGE_FILE, ["project_id", "stage", "date", "evidence_url", "note"], stage_rows):
            changed.append(cr.NOTICE_STAGE_FILE.name)
        if write_csv(cr.NOTICE_PROJECTS_FILE, ["complex_id", "name", "stage", "date", "evidence_url", "note", "project_id"], cx_rows):
            changed.append(cr.NOTICE_PROJECTS_FILE.name)
        cr.OUT.mkdir(parents=True, exist_ok=True)
        if write_csv(UNLINKED_FILE, ["date", "sido", "sgg", "stage", "zone", "title", "number", "why", "link"], unlinked):
            changed.append(UNLINKED_FILE.name)
        print(f"[저장] 고시 {stats['total']}건 → {STATE_FILE.name}; 단계 근거 {len(stage_rows)}곳, 단지 등록 {len(cx_rows)}곳, 미연결 {len(unlinked)}건"
              + (f" (바뀜: {', '.join(changed)})" if changed else " (바뀐 파일 없음)"))
    write_summary(today, stats, new, stage_rows, cx_rows, unlinked, changed, error)


if __name__ == "__main__":
    main()
