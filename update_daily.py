"""재건축 데이터 증분 갱신기 — 매일 바뀐 부분만 반영한다.

    python update_daily.py --init     # 최초 1회: 전체 구축 후 상태 파일(state/) 생성
    python update_daily.py            # 매일 실행 (GitHub Actions: .github/workflows/daily-update.yml)
    python update_daily.py --dry-run  # 무엇이 바뀔지만 출력하고 저장하지 않음
    python update_daily.py --export-only  # 출처를 확인하지 않고 현재 상태(state/)로 출력 파일만 다시 만듦(이름·별칭 같은 계산 규칙을 바꾼 뒤)

상태 저장: state/ 아래 텍스트 파일(한 줄 = 한 행, 정렬·고정 형식)로 저장해 git diff에 바뀐 줄만 보이게 한다.
  state/sources.json               출처별 마지막 probe 값·내용 해시·변경일
  state/source_rows/<출처>.jsonl   출처 행 원본(payload)과 연결된 project_id
  state/projects.jsonl             사업장 레코드
실행 중에는 메모리 SQLite에 올려 쓰고, 끝나면 다시 텍스트로 쓴다.

동작 원리 (3단계로 걸러서, 바뀌지 않은 것은 손대지 않는다)
  1) 출처 단위: 파일을 받기 전에 probe(파일ID·파일명)로 바뀌었는지 확인 → 그대로면 다운로드도 안 함.
     probe가 없는 출처(서울플랜+, 경기도)는 받아서 내용 해시가 같으면 거기서 멈춘다.
  2) 행 단위: 바뀐 출처는 이전에 저장한 행과 row_key로 비교해 추가·수정·삭제된 행만 고른다.
  3) 사업장 단위: 그 행에 연결된 사업장만 다시 계산하고, 필드별로 이전 값과 비교해 변경 이력을 남긴다.

안정성 규칙
  - 출처 행 → 사업장(project_id) 연결은 한 번 정하면 바꾸지 않는다. 사업장을 매번 다시 묶지 않으므로
    이름 매칭 결과가 날마다 흔들리지 않는다. 새로 나타난 행만 기존 사업장에 붙이거나 새 사업장을 만든다.
  - 출처에서 행이 사라져도 사업장을 지우지 않는다(이력에 '출처에서 삭제', 모든 출처에서 사라지면 status 표시).
  - 현재 단계가 뒤로 가면 '단계 후퇴(확인 필요)'로 따로 표시한다.
  - 같은 입력이면 같은 출력: 사업장 레코드는 연결된 출처 행들만으로 계산된다(collect_reconstruction.build_record).
  - 사람이 뉴스·고시로 확인한 단계는 manual_stage.csv에 적는다(news.py의 검토 대기열 → 사람 확인 → 수동 반영).
    출처 단계보다 앞설 때만 현재 단계로 쓰고(stage_source='수동(근거)'), 출처가 따라오면 자연히 무시된다.
  - 공공 출처에 아직 없는 초기 단계(안전진단·추진위 준비) 단지는 manual_projects.csv에 근거와 함께 적는다.
    이 파일은 보통 출처처럼 다뤄진다(출처 이름 'manual_projects.csv', probe = 파일 해시): 행이 생기면 노후 단지 자료로
    사업장을 만들고(이력 '신규 사업장(수동 등록)'), 나중에 공공 출처에 같은 사업장이 실리면 이름·대표 지번으로 같은 project_id에 붙는다.
  - 사람이 검토하지 않아도 돌아가게, news.py가 확신할 수 있는 후보(서로 다른 기사 2건 이상 또는 고시가 같은 단계를 '확정'으로 전함)를
    auto_stage.csv·auto_projects.csv에 적는다. 같은 형식·같은 처리이며 stage_source='자동(뉴스)'로 구분된다. 수동 파일이 앞선다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

import collect_reconstruction as cr
import complexes
import enrich
import tradebook

STATE = cr.BASE / "state"
CHANGES = cr.OUT / "changes"
RUN_START = time.monotonic()
HISTORY_TIME_LIMIT = 32 * 60   # 매매 이력 수집은 실행 시작 32분까지만(워크플로 timeout 50분 — 내보내기·커밋 시간을 남긴다)
SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
    name TEXT PRIMARY KEY, fingerprint TEXT, content_hash TEXT, checked_at TEXT, changed_at TEXT);
CREATE TABLE IF NOT EXISTS source_rows(
    source TEXT, row_key TEXT, project_id TEXT, row_hash TEXT, payload TEXT,
    first_seen TEXT, last_changed TEXT, removed_at TEXT, PRIMARY KEY (source, row_key));
CREATE TABLE IF NOT EXISTS projects(
    project_id TEXT PRIMARY KEY, area TEXT, record TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS changes(
    run_date TEXT, project_id TEXT, kind TEXT, field TEXT, old TEXT, new TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""
IGNORE_FIELDS = {"search_keys", "_aliases", "source_updated_at"}  # 이력에 남길 필요 없는 파생 필드
LINK_THRESHOLD = 0.75
LOT_LINK_THRESHOLD = 0.5   # 대표 지번이 같은 기존 사업장이 하나뿐이면 이름이 이만큼만 닮아도 그 사업장으로(수동 등록 단지 ↔ 공공 출처 표기 차이)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA)
    load_state(con)
    return con


def _jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def load_state(con: sqlite3.Connection) -> None:
    if not (STATE / "sources.json").exists():
        return
    for r in json.loads((STATE / "sources.json").read_text(encoding="utf-8")):
        con.execute("INSERT INTO sources VALUES (?,?,?,NULL,?)",
                    (r["name"], r["fingerprint"], r["content_hash"], r["changed_at"]))
    by_key = {s.key: s.name for s in cr.SOURCES}
    for path in sorted((STATE / "source_rows").glob("*.jsonl")):
        con.executemany("INSERT INTO source_rows VALUES (?,?,?,?,?,?,?,?)", [
            (by_key[path.stem], r["row_key"], r["project_id"], r["row_hash"], dumps(r["payload"]),
             r["first_seen"], r["last_changed"], r["removed_at"]) for r in _jsonl(path)])
    con.executemany("INSERT INTO projects VALUES (?,?,?,?,?)", [
        (r["project_id"], r["area"], dumps(r["record"]), r["created_at"], r["updated_at"])
        for r in _jsonl(STATE / "projects.jsonl")])
    meta = json.loads((STATE / "meta.json").read_text(encoding="utf-8"))
    con.executemany("INSERT INTO meta VALUES (?,?)", meta.items())


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(dumps(r) + "\n")


def save_state(con: sqlite3.Connection) -> None:
    """정렬된 고정 형식으로 쓴다. 내용이 같으면 파일도 바이트 단위로 같다(→ git 변경 없음).
    매일 바뀌는 확인 시각(checked_at)은 저장하지 않는다."""
    sources = [{"name": n, "fingerprint": f, "content_hash": h, "changed_at": c} for n, f, h, c in con.execute(
        "SELECT name, fingerprint, content_hash, changed_at FROM sources ORDER BY name")]
    (STATE / "sources.json").parent.mkdir(parents=True, exist_ok=True)
    (STATE / "sources.json").write_text(json.dumps(sources, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                        encoding="utf-8", newline="\n")
    for s in cr.SOURCES:
        _write_jsonl(STATE / "source_rows" / f"{s.key}.jsonl", (
            {"row_key": k, "project_id": pid, "row_hash": h, "payload": json.loads(p),
             "first_seen": fs, "last_changed": lc, "removed_at": ra}
            for k, pid, h, p, fs, lc, ra in con.execute(
                "SELECT row_key, project_id, row_hash, payload, first_seen, last_changed, removed_at "
                "FROM source_rows WHERE source=? ORDER BY row_key", (s.name,))))
    _write_jsonl(STATE / "projects.jsonl", (
        {"project_id": pid, "area": a, "record": json.loads(r), "created_at": c, "updated_at": u}
        for pid, a, r, c, u in con.execute(
            "SELECT project_id, area, record, created_at, updated_at FROM projects ORDER BY project_id")))
    save_meta(con)


def save_meta(con: sqlite3.Connection) -> None:
    (STATE / "meta.json").write_text(json.dumps(dict(con.execute("SELECT key, value FROM meta ORDER BY key")),
                                                ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                     encoding="utf-8", newline="\n")


def failure_ages(con: sqlite3.Connection, failed: list[str], today: str) -> dict[str, int]:
    """실패한 출처마다 언제부터 실패 중인지(meta의 fail_since:<이름>)를 기록하고 오늘로 며칠째인지 돌려준다.
    성공한 출처의 기록은 지운다 → 실패가 시작되거나 끝날 때만 상태 파일이 바뀐다."""
    names = [fail_name(f) for f in failed]
    for (key,) in con.execute("SELECT key FROM meta WHERE key LIKE 'fail_since:%'").fetchall():
        if key[len("fail_since:"):] not in names:
            con.execute("DELETE FROM meta WHERE key=?", (key,))
    ages = {}
    for name in names:
        row = con.execute("SELECT value FROM meta WHERE key=?", (f"fail_since:{name}",)).fetchone()
        if not row:
            con.execute("INSERT INTO meta VALUES (?, ?)", (f"fail_since:{name}", today))
        since = row[0] if row else today
        ages[name] = (date.fromisoformat(today) - date.fromisoformat(since)).days + 1
    return ages


file_hash = cr.file_hash   # 줄바꿈(CRLF/LF) 차이를 무시하는 파일 해시


def fail_name(f: str) -> str:
    """실패 메시지 '출처: 이유' → 상태 키로 쓸 이름. '매매 이력 24건 실패 (예: …)'처럼 괄호 안 콜론에서 잘린 꼬리를 떼고
    건수는 N으로 바꿔, 날마다 숫자가 달라도 같은 실패로 센다."""
    name = re.sub(r"\s*\(예$", "", f.split(":", 1)[0].strip())
    return re.sub(r"\d+", "N", name)


def manual_hash() -> str:
    return file_hash(cr.MANUAL_FILE)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def content_hash(payloads: dict[str, dict]) -> str:
    return hashlib.sha1("\n".join(f"{k}\t{cr.row_hash(p)}" for k, p in sorted(payloads.items())).encode()).hexdigest()


NETWORK_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
RETRY_WAITS = (60, 180)  # 실패한 출처는 1분 뒤, 다시 실패하면 3분 뒤 한 번 더 시도
SHRINK_LIMIT = 0.3       # 살아 있는 행의 30%가 넘게 한꺼번에 사라지면 부분 응답으로 보고 반영하지 않는다(--accept-shrink로 허용)
GRACE_DAYS = 3           # 출처가 실패해도 3일째까지는 경고만(해외 IP 차단 같은 일시 장애), 그 뒤로 계속 실패하면 실행을 실패로 표시


def safe_probe(src: cr.Source) -> str | None:
    """probe 값. 접속 자체가 안 되면 예외를 그대로 올려 다운로드 시도도 하지 않는다(어차피 같은 서버)."""
    if not src.probe:
        return None
    try:
        return src.probe()
    except NETWORK_ERRORS:
        raise
    except Exception as e:
        print(f"   ! probe 실패({e}) → 내려받아 비교", file=sys.stderr)
        return None


# ---------------------------------------------------------------- 최초 구축
def init(con: sqlite3.Connection, today: str) -> None:
    if con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]:
        sys.exit("이미 초기화되어 있습니다. 처음부터 다시 만들려면 state/ 폴더를 지우고 실행하세요.")
    probes = {s.name: safe_probe(s) for s in cr.SOURCES}
    fetched, links, recs = cr.full_build()
    for name, payloads in fetched.items():
        con.execute("INSERT INTO sources VALUES (?,?,?,?,?)",
                    (name, probes[name], content_hash(payloads), today, today))
        con.executemany("INSERT INTO source_rows VALUES (?,?,?,?,?,?,?,NULL)",
                        [(name, k, links[(name, k)], cr.row_hash(p), dumps(p), today, today)
                         for k, p in payloads.items()])
    for rec in recs:
        area = f"{rec['region_sido']}|{cr.sgg_key(rec['region_sigungu'], rec['region_sido'])}"
        con.execute("INSERT INTO projects VALUES (?,?,?,?,?)", (rec["project_id"], area, dumps(rec), today, today))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('manual_hash', ?)", (manual_hash(),))
    con.execute("INSERT OR REPLACE INTO meta VALUES ('manual_stage_hash', ?)", (stage_files_hash(),))
    con.commit()
    save_state(con)
    print(f"[초기화] 출처 {len(fetched)}개, 행 {len(links)}개, 사업장 {len(recs)}개")


def stage_files_hash() -> str:
    """manual_stage.csv + auto_stage.csv(news.py 자동 반영)의 해시 — 어느 쪽이 바뀌어도 모든 사업장을 다시 계산한다."""
    return "+".join(file_hash(p) for p, _ in cr.STAGE_FILES)


# ---------------------------------------------------------------- 새 행 연결
def active_rows(con, project_id: str) -> list[tuple[str, str, dict]]:
    return [(s, k, json.loads(p)) for s, k, p in con.execute(
        "SELECT source, row_key, payload FROM source_rows WHERE project_id=? AND removed_at IS NULL", (project_id,))]


def _complex_keys(payload: dict) -> set[str]:
    """수동 등록 행에 붙은 단지 이름들(부동산원·K-apt 표기)의 키 — 공공 출처가 다른 표기로 실려도 같은 사업장으로 알아보게."""
    return {k for k in (cr.zone_key(a) for a in payload.get("complex_aliases") or []) if k}


def link_row(con, source: str, payload: dict, manual: pd.DataFrame, today: str) -> tuple[str, str]:
    """새로 나타난 출처 행을 기존 사업장에 붙이거나 새 사업장을 만든다. → (project_id, 설명)

    이름 유사도 LINK_THRESHOLD 이상이면 연결(비슷한 후보가 둘이면 대표 지번이 같은 쪽). 그에 못 미쳐도 대표 지번이 같은
    기존 사업장이 하나뿐이고 이름이 LOT_LINK_THRESHOLD 이상 닮았으면 그 사업장으로 — 수동 등록한 '가야주공5단지1차'가
    나중에 공공 출처에 '가야5단지1차'처럼 실려도 새 사업장을 만들지 않는다.
    """
    area = f"{payload['region_sido']}|{cr.sgg_key(payload['region_sigungu'], payload['region_sido'])}"
    tag = f"{source}/{payload.get('project_type') or ''}"
    mine = cr.name_keys([payload["zone_name"]]) | _complex_keys(payload)
    my_lot = _lot_of(payload)
    # 수동·자동 등록 행은 단지 이름이 흔할 수 있다('대림아파트'): 지번이 다르면 또렷한 이름(4자 이상, 유사도 0.9 이상)일 때만 기존 사업장에 붙인다
    registered = source in cr.PROJECT_FILE_SOURCES
    plain_name = len(cr.zone_key(payload["zone_name"])) >= 4
    scored = []
    for (pid,) in con.execute("SELECT project_id FROM projects WHERE area=? ORDER BY project_id", (area,)):
        rows = active_rows(con, pid)
        if not rows:
            continue
        names = [p["zone_name"] for _, _, p in rows]
        if any(f"{s}/{p.get('project_type') or ''}" == tag for s, _, p in rows) and not (mine & cr.name_keys(names)):
            continue  # 같은 출처에 이미 다른 행으로 실린 사업장 = 다른 사업장 (이름이 완전히 같으면 같은 사업장의 중복 등재로 본다)
        keys = cr.name_keys(names) | set(manual.loc[manual["_area"].eq(area) & manual["name_key"].isin(
            {cr.zone_key(n) for n in names}), "alias_key"])
        for _, _, p in rows:
            keys |= _complex_keys(p)
        same_lot = bool(my_lot) and any(_lot_of(p) == my_lot for _, _, p in rows)
        scored.append((max(cr._score(a, b) for a in mine for b in keys), same_lot, pid))
    scored.sort(reverse=True)
    if scored and scored[0][0] >= LINK_THRESHOLD:
        best, runner = scored[0], (scored[1] if len(scored) > 1 else None)
        clear = runner is None or runner[0] < best[0] - 0.03
        trusted = not registered or best[1] or (best[0] >= 0.9 and plain_name)
        if (clear or (best[1] and not runner[1])) and trusted:  # 이름 점수가 비슷하면 대표지번이 같은 쪽
            how = f"유사도 {best[0]:.2f}" + (" · 지번 일치" if best[1] else "")
            return best[2], f"기존 사업장에 연결({how})"
    lot_hits = [s for s in scored if s[1]]
    if len(lot_hits) == 1 and lot_hits[0][0] >= LOT_LINK_THRESHOLD:
        return lot_hits[0][2], f"기존 사업장에 연결(지번 일치 · 유사도 {lot_hits[0][0]:.2f})"
    used = {r[0] for r in con.execute("SELECT project_id FROM projects")}
    pid = cr.unique_project_id(payload, used)
    con.execute("INSERT INTO projects VALUES (?,?,?,?,?)", (pid, area, "{}", today, today))
    note = "새 사업장" if not scored or scored[0][0] < LINK_THRESHOLD else \
        f"새 사업장(후보 {scored[0][2]}·{scored[1][2]} 모호 — 확인 필요)"
    return pid, note


_LOT_CACHE: dict[tuple, tuple | None] = {}


def _lot_of(payload: dict) -> tuple | None:
    """출처 행의 주소 → (법정동코드, 산, 본번, 부번). 같은 사업장인지 가릴 때 쓴다."""
    key = (payload.get("region_sido"), payload.get("region_sigungu"), payload.get("address"))
    if key not in _LOT_CACHE:
        lot = enrich.parse_lot(*key) if key[2] else None
        _LOT_CACHE[key] = (lot["bjd"], lot["plat_gb"], lot["bun"], lot["ji"]) if lot else None
    return _LOT_CACHE[key]


# ---------------------------------------------------------------- 사업장 재계산
MANUAL_STAGE_FIELDS = {"stage", "stage_order", "stage_source", "stage_evidence", "stage_date", "stage_by_source"}


def recompute(con, pid: str, manual: pd.DataFrame, today: str, log: list, manual_stage: dict | None = None) -> None:
    old_json = con.execute("SELECT record FROM projects WHERE project_id=?", (pid,)).fetchone()[0]
    old = json.loads(old_json)
    rows = active_rows(con, pid)
    if not rows:  # 모든 출처에서 사라짐 — 지우지 않고 표시만
        if old and old.get("status") != "출처에서 사라짐":
            new = {**old, "status": "출처에서 사라짐"}
            log.append((today, pid, "모든 출처에서 사라짐", "status", old.get("status"), new["status"], ""))
            con.execute("UPDATE projects SET record=?, updated_at=? WHERE project_id=?", (dumps(new), today, pid))
        return
    new = cr.build_record(cr.rows_frame([(k, p) for _, k, p in rows]), manual, (manual_stage or {}).get(pid))
    new["project_id"] = pid
    if dumps(new) == old_json:
        return
    if not old:
        srcs = {s for s, _, _ in rows}
        kind = "신규 사업장" if srcs - set(cr.PROJECT_FILE_SOURCES) else \
            "신규 사업장(수동 등록)" if cr.MANUAL_PROJECTS_SOURCE in srcs else "신규 사업장(자동 등록)"
        log.append((today, pid, kind, "stage", None, new.get("stage"), new.get("sources")))
    else:
        for f in sorted(set(old) | set(new)):
            if f in IGNORE_FIELDS or old.get(f) == new.get(f):
                continue
            kind = "변경"
            if f == "stage":
                o, n = cr.STAGE_ORDER.get(old.get(f), -1), cr.STAGE_ORDER.get(new.get(f), -1)
                kind = "단계 진전" if n > o else "단계 후퇴(확인 필요)"
            # 수동·자동 단계(manual_stage.csv·auto_stage.csv) 또는 수동·자동 등록(manual_projects.csv·auto_projects.csv)이 만든 변경은
            # 출처 칸에 파일 이름을 남긴다
            by_manual = f in MANUAL_STAGE_FIELDS and new.get("stage_source") in (cr.MANUAL_STAGE_SOURCE, cr.AUTO_STAGE_SOURCE)
            log.append((today, pid, kind, f, _s(old.get(f)), _s(new.get(f)), _manual_file(new, rows) if by_manual else ""))
    con.execute("UPDATE projects SET record=?, updated_at=? WHERE project_id=?", (dumps(new), today, pid))


def _manual_file(rec: dict, rows: list[tuple[str, str, dict]]) -> str:
    """'수동(근거)'·'자동(뉴스)' 단계가 어느 파일에서 왔는지: 등록 행의 단계·근거가 레코드와 같으면 그 등록 파일, 아니면 단계 파일."""
    for s, _, p in rows:
        if s in cr.PROJECT_FILE_SOURCES and cr.norm_stage(p.get("stage_raw")) == rec.get("stage") \
                and p.get("evidence_url") == rec.get("stage_evidence"):
            return s
    return "auto_stage.csv" if rec.get("stage_source") == cr.AUTO_STAGE_SOURCE else "manual_stage.csv"


def _s(v):
    return None if v is None else str(v)


# ---------------------------------------------------------------- 매일 갱신
def update(con: sqlite3.Connection, today: str, force: bool = False,
           failed: list[str] | None = None, accept_shrink: bool = False, local_only: bool = False) -> list[tuple]:
    """local_only: 공공 출처는 확인하지 않고 저장소 안의 파일(manual_*.csv·auto_*.csv)만 반영한다."""
    if not con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]:
        sys.exit("상태가 비어 있습니다. 먼저 `python update_daily.py --init` 을 실행하세요.")
    manual = cr.load_manual()
    dirty: set[str] = set()
    log: list[tuple] = []
    pending, errors = [s for s in cr.SOURCES if s.local or not local_only], {}
    for attempt, wait in enumerate((0, *RETRY_WAITS)):
        if not pending:
            break
        if wait:
            print(f"[재시도 {attempt}/{len(RETRY_WAITS)}] {len(pending)}개 출처 — {wait}초 후")
            time.sleep(wait)
        down_hosts: set[str] = set()  # 이번 회차에 접속이 안 된 서버 → 같은 서버의 다른 출처는 건너뜀
        still = []
        for src in pending:
            if src.host and src.host in down_hosts:
                errors[src.name] = f"{src.host} 접속 실패로 건너뜀"
                still.append(src)
                continue
            try:
                sync_source(con, src, today, force, manual, dirty, log, accept_shrink)
                errors.pop(src.name, None)
            except Exception as e:
                errors[src.name] = str(e)
                print(f"[실패] {src.name}: {e}", file=sys.stderr)
                if isinstance(e, NETWORK_ERRORS) and src.host:
                    down_hosts.add(src.host)
                if not src.local:  # 저장소 안의 파일은 기다려도 달라지지 않는다 — 재시도 없이 이전 상태 유지
                    still.append(src)
        pending = still
    for src in cr.SOURCES:  # 끝까지 실패한 출처는 이전 데이터를 그대로 둔다
        if src.name in errors:
            print(f"[포기] {src.name} — 이전 데이터 유지", file=sys.stderr)
            if failed is not None:
                failed.append(f"{src.name}: {errors.get(src.name)}")

    # 수동 별칭표가 바뀌었으면: 지정된 병합을 적용하고 해당 지역 사업장을 다시 계산
    mh = manual_hash()
    if mh != (con.execute("SELECT value FROM meta WHERE key='manual_hash'").fetchone() or [""])[0]:
        print("[변경] manual_aliases.csv")
        dirty |= apply_manual(con, manual, today, log)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('manual_hash', ?)", (mh,))

    # 수동·자동 단계표가 바뀌었으면(추가·수정·삭제) 모든 사업장을 다시 계산한다 — 바뀐 레코드만 이력에 남는다
    manual_stage = cr.load_manual_stage()
    msh = stage_files_hash()
    if msh != (con.execute("SELECT value FROM meta WHERE key='manual_stage_hash'").fetchone() or [""])[0]:
        n_auto = sum(v.get("source") == cr.AUTO_STAGE_SOURCE for v in manual_stage.values())
        print(f"[변경] manual_stage.csv·auto_stage.csv (수동 {len(manual_stage) - n_auto}곳 · 자동 {n_auto}곳) — 모든 사업장 다시 계산")
        dirty |= {pid for (pid,) in con.execute("SELECT project_id FROM projects")}
        con.execute("INSERT OR REPLACE INTO meta VALUES ('manual_stage_hash', ?)", (msh,))

    for pid in sorted(dirty):
        recompute(con, pid, manual, today, log, manual_stage)
    con.executemany("INSERT INTO changes VALUES (?,?,?,?,?,?,?)", log)
    return log


def sync_source(con, src: cr.Source, today: str, force: bool, manual: pd.DataFrame,
                dirty: set[str], log: list, accept_shrink: bool = False) -> None:
    """출처 하나를 확인해 바뀐 행만 반영한다. 내려받기 실패는 예외로 올린다(호출 쪽에서 재시도).

    행이 한꺼번에 많이 사라진 응답(목록 일부만 온 경우 등)도 실패로 본다 — 지운 행을 되살리는 비용이 훨씬 크다.
    """
    stored = con.execute("SELECT fingerprint, content_hash FROM sources WHERE name=?", (src.name,)).fetchone()
    fp = safe_probe(src)
    if not force and fp and stored and stored[0] == fp:
        print(f"[그대로] {src.name} ({'파일 해시 동일' if src.local else 'probe 동일 — 다운로드 생략'})")
        con.execute("UPDATE sources SET checked_at=? WHERE name=?", (today, src.name))
        return
    payloads = cr.fetch_payloads(src)
    ch = content_hash(payloads)
    if stored and stored[1] == ch:
        print(f"[그대로] {src.name} (내용 동일)")
        con.execute("UPDATE sources SET fingerprint=?, checked_at=? WHERE name=?", (fp, today, src.name))
        return

    old = {k: (h, pid, removed) for k, h, pid, removed in con.execute(
        "SELECT row_key, row_hash, project_id, removed_at FROM source_rows WHERE source=?", (src.name,))}
    live = {k for k, v in old.items() if v[2] is None}
    removed = sorted(live - payloads.keys())
    added = sorted(k for k in payloads if k not in live)
    changed = sorted(k for k in payloads if k in live and old[k][0] != cr.row_hash(payloads[k]))
    if len(live) >= 20 and len(removed) > SHRINK_LIMIT * len(live) and not accept_shrink and not src.local:
        raise RuntimeError(f"행 수 급감: 살아 있는 {len(live)}행 중 {len(removed)}행이 한꺼번에 사라짐 — "
                           f"부분 응답으로 보고 반영하지 않음(실제 축소면 --accept-shrink)")
    print(f"[변경] {src.name}: 추가 {len(added)} · 수정 {len(changed)} · 삭제 {len(removed)}")

    for k in removed:  # 삭제를 먼저 반영해야 이름이 바뀐 행(삭제+추가)이 같은 사업장에 다시 붙는다
        con.execute("UPDATE source_rows SET removed_at=? WHERE source=? AND row_key=?", (today, src.name, k))
        dirty.add(old[k][1])
        log.append((today, old[k][1], "출처에서 삭제", "row", k, None, src.name))
    for k in changed:
        p = payloads[k]
        con.execute("UPDATE source_rows SET row_hash=?, payload=?, last_changed=? WHERE source=? AND row_key=?",
                    (cr.row_hash(p), dumps(p), today, src.name, k))
        dirty.add(old[k][1])
    for k in added:
        p = payloads[k]
        if k in old:  # 예전에 사라졌다가 다시 나타난 행 → 원래 사업장으로
            pid, note = old[k][1], "다시 나타남"
            con.execute("UPDATE source_rows SET row_hash=?, payload=?, last_changed=?, removed_at=NULL "
                        "WHERE source=? AND row_key=?", (cr.row_hash(p), dumps(p), today, src.name, k))
        else:
            pid, note = link_row(con, src.name, p, manual, today)
            con.execute("INSERT INTO source_rows VALUES (?,?,?,?,?,?,?,NULL)",
                        (src.name, k, pid, cr.row_hash(p), dumps(p), today, today))
        dirty.add(pid)
        log.append((today, pid, "출처 행 추가", "row", None, f"{k} ({note})", src.name))
    con.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?)", (src.name, fp, ch, today, today))


def apply_manual(con, manual: pd.DataFrame, today: str, log: list) -> set[str]:
    dirty = set()
    for _, m in manual.iterrows():
        pids = {"name": set(), "alias": set()}
        for (pid,) in con.execute("SELECT project_id FROM projects WHERE area=?", (m["_area"],)):
            keys = {cr.zone_key(p["zone_name"]) for _, _, p in active_rows(con, pid)}
            dirty.add(pid)
            if m["name_key"] in keys:
                pids["name"].add(pid)
            if m["alias_key"] in keys:
                pids["alias"].add(pid)
        if len(pids["name"]) == 1:
            keep = next(iter(pids["name"]))
            for other in pids["alias"] - {keep}:  # 수동 지정된 같은 사업장 → 하나로 합침(작은 쪽 행을 옮김)
                con.execute("UPDATE source_rows SET project_id=? WHERE project_id=?", (keep, other))
                con.execute("UPDATE projects SET record='{}', updated_at=? WHERE project_id=?", (today, other))
                log.append((today, keep, "수동 병합", "project_id", other, keep, "manual_aliases.csv"))
    return dirty


# ---------------------------------------------------------------- 내보내기
def export(con: sqlite3.Connection, log: list, today: str) -> None:
    recs = [json.loads(r) for (r,) in con.execute("SELECT record FROM projects WHERE record != '{}'")]
    lots, names = enrich.project_lots(con), enrich.project_names(con)
    book = tradebook.Book(today, lots, names, {r["project_id"]: r for r in recs})  # 매매 창+이력·전월세·분양권을 단지·사업장별로
    if book.history_rows:
        print(f"[거래] 매매 이력 {book.history_rows:,}건 읽음")
    recs = with_enrichment(recs, book)
    recs = [r for r in recs if active_rows(con, r["project_id"]) or r.get("status") == "출처에서 사라짐"]
    res, excluded = cr.records_frame(recs)
    cr.write_outputs(res, excluded, "latest")
    if complexes.REGISTRY_FILE.exists() or enrich.TRADES_FILE.exists():  # 30년 이상 노후 단지 목록
        rows = complexes.build(today, recs, lots, book, manual_complexes(con))
        complexes.write(rows, today)
        book.write_files(rows, [r for r in recs if r.get("_apartment")])
    if log:
        CHANGES.mkdir(parents=True, exist_ok=True)
        path = CHANGES / f"changes_{today.replace('-', '')}.csv"
        df = pd.DataFrame(log, columns=["run_date", "project_id", "kind", "field", "old", "new", "source"])
        names = {r["project_id"]: r.get("zone_name") for r in recs}
        df.insert(2, "zone_name", df["project_id"].map(names))
        new = not path.exists()  # 같은 날 두 번 실행되면 이어 붙인다(BOM은 처음 한 번만)
        df.to_csv(path, mode="a", header=new, index=False, encoding="utf-8-sig" if new else "utf-8", lineterminator="\n")
        print(f"[이력] {len(log)}건 → {path.relative_to(cr.BASE)}")


def manual_complexes(con) -> dict[str, str]:
    """수동·자동 등록(manual_projects.csv·auto_projects.csv) 행의 단지 → 사업장. 노후 단지 목록에서 그 단지에만 사업 단계를 붙이는 데 쓴다."""
    return {json.loads(p)["complex_id"]: pid for pid, p in con.execute(
        "SELECT project_id, payload FROM source_rows WHERE source IN (?, ?) AND removed_at IS NULL",
        (cr.MANUAL_PROJECTS_SOURCE, cr.AUTO_PROJECTS_SOURCE))}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", action="store_true", help="최초 전체 구축")
    ap.add_argument("--force", action="store_true", help="probe를 무시하고 모든 출처를 내려받아 비교")
    ap.add_argument("--dry-run", action="store_true", help="변경 내용만 출력하고 저장하지 않음")
    ap.add_argument("--migrate", action="store_true",
                    help="수집 열을 늘린 뒤 1회 실행: 모든 출처를 다시 받아 반영하되, 새 열 때문에 생긴 변경은 이력에서 뺀다")
    ap.add_argument("--skip-enrich", action="store_true", help="건축물대장·실거래가 보강을 건너뜀")
    ap.add_argument("--history-budget", type=int, default=None, metavar="N",
                    help=f"이번 실행에서 매매 이력(2006년~)을 받을 최대 요청 수 (기본: 하루 {enrich.HISTORY_DAILY_CAP}건 안에서 "
                         f"실행 {HISTORY_TIME_LIMIT // 60}분까지; 0이면 받지 않음)")
    ap.add_argument("--accept-shrink", action="store_true",
                    help="출처의 행이 30%% 넘게 사라져도 반영(기본은 부분 응답으로 보고 실패 처리)")
    ap.add_argument("--export-only", action="store_true",
                    help="출처를 확인하지 않고 현재 상태(state/)로 출력 파일만 다시 만든다(노후 단지 이름·별칭 같은 파생 계산 규칙을 바꾼 뒤; "
                         "사업장 레코드는 다시 계산하지 않으니 그때는 --migrate)")
    ap.add_argument("--local-only", action="store_true",
                    help="공공 출처는 확인하지 않고 저장소 안의 파일(manual_*.csv·auto_*.csv)만 반영한다(news.py 자동 반영 뒤 바로 적용할 때)")
    ap.add_argument("--date", default=date.today().isoformat(), help="기록용 날짜(테스트용)")
    args = ap.parse_args()

    con = connect()
    if args.init:
        init(con, args.date)
        export(con, [], args.date)
        return
    if args.export_only:   # 상태 파일·변경 이력은 그대로 두고, 저장된 상태에서 출력 파일만 다시 만든다
        export(con, [], args.date)
        print("[완료] --export-only: 출력 파일만 다시 만듦 (상태 파일은 그대로 둠)")
        return
    failed: list[str] = []
    log = update(con, args.date, force=args.force or args.migrate, failed=failed, accept_shrink=args.accept_shrink,
                 local_only=args.local_only)
    if args.migrate:  # 계산 규칙·열이 바뀌었을 수 있으니 모든 사업장을 다시 계산한다(바뀐 것만 이력에 남는다)
        manual, manual_stage = cr.load_manual(), cr.load_manual_stage()
        for (pid,) in con.execute("SELECT project_id FROM projects").fetchall():
            recompute(con, pid, manual, args.date, log, manual_stage)
        log = migration_log(log, args.date)
    if args.dry_run:
        for row in log:
            print("  ", row)
        print(f"[dry-run] 변경 {len(log)}건 (저장 안 함)")
        return
    con.commit()
    save_state(con)
    enriched = False
    if args.skip_enrich:
        print("[보강] --skip-enrich: 건축물대장·실거래가 건너뜀")
    elif enrich.api_key():
        try:
            enriched = enrich.update(con, args.date, log, failed)
            lots = enrich.project_lots(con)
            enriched = complexes.update(args.date, failed, {enrich.lot_key(l) for l in lots.values()}) or enriched
            # 매매 이력(2006년~)은 맨 마지막에: 다른 일 예산을 다 쓴 뒤 남은 실행 시간만 쓴다
            enriched = enrich.update_history(lots, enrich.project_names(con), args.date, failed,
                                             budget=args.history_budget,
                                             deadline=RUN_START + HISTORY_TIME_LIMIT) or enriched
        except Exception as e:  # 보강이 실패해도 사업장 데이터 갱신은 그대로 반영한다
            failed.append(f"보강(건축물대장·실거래가): {e}")
    else:
        print("[보강] DATA_GO_KR_KEY 없음 — 건축물대장·실거래가 건너뜀")
    latest = cr.OUT / "capital_area_apartment_reconstruction_latest.csv"
    if log or enriched or args.migrate or not latest.exists():
        export(con, log, args.date)
    print(f"[완료] 변경 {len(log)}건" + ("" if log else " — 출력 파일은 그대로 둠"))
    ages = failure_ages(con, failed, args.date)
    con.commit()
    save_meta(con)
    write_summary(log, failed, args.date, ages)
    stale = [n for n, d in ages.items() if d > GRACE_DAYS]
    for f in failed:
        n = fail_name(f)
        print(f"[{'계속 실패' if n in stale else '일시 실패'}] {n} — {ages[n]}일째, 이전 데이터 유지", file=sys.stderr)
    if stale:  # 성공한 출처의 변경은 저장했다. 며칠째 계속 실패하는 출처가 있을 때만 종료 코드로 알린다(Actions에서 빨간불)
        sys.exit(1)


def with_enrichment(recs: list[dict], book=None) -> list[dict]:
    """건축물대장·실거래가(enrich.py가 state/에 저장)를 사업장 레코드에 붙인다.
    출처에 없는 기존 단지 값(세대수·용적률·준공연도)은 건축물대장 값으로 채운다."""
    extra = enrich.fields({r["project_id"]: r for r in recs}, book)
    out = []
    for r in recs:
        e = extra.get(r["project_id"], {})
        r = {**r, **e}
        if r.get("stage") != "준공":  # 준공 후 지번의 건축물대장은 새 아파트 것이라 '기존 단지' 값으로 쓰지 않는다
            r["existing_units"] = r.get("existing_units") or e.get("bld_households")
            r["far_existing"] = r.get("far_existing") or e.get("bld_far")
            r["existing_built_year"] = r.get("existing_built_year") or (
                int(e["bld_approved"][:4]) if e.get("bld_approved") else None)
        out.append(r)
    return out


def migration_log(log: list[tuple], today: str) -> list[tuple]:
    """스키마 확장 실행의 이력: 행 추가·삭제와 단계·일자 변경만 남기고, 새 열이 채워지며 생긴 변경은 한 줄로 요약."""
    row_kinds = {"출처 행 추가", "출처에서 삭제", "신규 사업장", "신규 사업장(수동 등록)", "신규 사업장(자동 등록)", "모든 출처에서 사라짐",
                 "수동 병합"}
    keep_fields = {"stage", "status", *cr.DATE_COLS}
    kept = [r for r in log if (r[2] in row_kinds or r[3] in keep_fields)
            and not (r[3] == "stage" and (r[4], r[5]) in cr.RENAMED_STAGES)]  # 단계 이름만 바뀐 것은 변경이 아니다
    note = (today, "*", "스키마 확장", "columns", None, ", ".join(cr.DETAIL_COLS), "")
    print(f"[migrate] 변경 {len(log)}건 중 {len(kept)}건만 이력에 남김 (나머지는 새 열 채움)")
    return [note, *kept]


def write_summary(log: list[tuple], failed: list[str], today: str, ages: dict[str, int] | None = None) -> None:
    """GitHub Actions 실행 화면에 보일 요약(GITHUB_STEP_SUMMARY)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    ages = ages or {}
    kinds = pd.Series([r[2] for r in log]).value_counts() if log else pd.Series(dtype=int)
    lines = [f"## 재건축 데이터 갱신 {today}", "", f"- 변경 {len(log)}건"]
    lines += [f"  - {k}: {v}건" for k, v in kinds.items()]
    for f in failed:
        d = ages.get(fail_name(f), 1)
        lines.append((f"- ❌ 계속 실패({d}일째, 확인 필요): " if d > GRACE_DAYS else f"- ⚠️ 일시 실패({d}일째, 이전 데이터 유지): ") + f)
    stages = [r for r in log if r[3] == "stage"]
    if stages:
        lines += ["", "| 사업장 | 변경 | 이전 | 이후 |", "|---|---|---|---|"]
        lines += [f"| {r[1]} | {r[2]} | {r[4] or ''} | {r[5] or ''} |" for r in stages[:50]]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
