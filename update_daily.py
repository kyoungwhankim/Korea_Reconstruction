"""재건축 데이터 증분 갱신기 — 매일 바뀐 부분만 반영한다.

    python update_daily.py --init     # 최초 1회: 전체 구축 후 상태 파일(state/) 생성
    python update_daily.py            # 매일 실행 (GitHub Actions: .github/workflows/daily-update.yml)
    python update_daily.py --dry-run  # 무엇이 바뀔지만 출력하고 저장하지 않음

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
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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

STATE = cr.BASE / "state"
CHANGES = cr.OUT / "changes"
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
    (STATE / "meta.json").write_text(json.dumps(dict(con.execute("SELECT key, value FROM meta ORDER BY key")),
                                                ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                     encoding="utf-8", newline="\n")


def manual_hash() -> str:
    """줄바꿈(CRLF/LF) 차이는 무시 — Windows와 GitHub Actions(Linux)에서 같은 값이 나오게."""
    if not cr.MANUAL_FILE.exists():
        return ""
    return hashlib.sha1(cr.MANUAL_FILE.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def content_hash(payloads: dict[str, dict]) -> str:
    return hashlib.sha1("\n".join(f"{k}\t{cr.row_hash(p)}" for k, p in sorted(payloads.items())).encode()).hexdigest()


NETWORK_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
RETRY_WAITS = (60, 180)  # 실패한 출처는 1분 뒤, 다시 실패하면 3분 뒤 한 번 더 시도


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
    con.commit()
    save_state(con)
    print(f"[초기화] 출처 {len(fetched)}개, 행 {len(links)}개, 사업장 {len(recs)}개")


# ---------------------------------------------------------------- 새 행 연결
def active_rows(con, project_id: str) -> list[tuple[str, str, dict]]:
    return [(s, k, json.loads(p)) for s, k, p in con.execute(
        "SELECT source, row_key, payload FROM source_rows WHERE project_id=? AND removed_at IS NULL", (project_id,))]


def link_row(con, source: str, payload: dict, manual: pd.DataFrame, today: str) -> tuple[str, str]:
    """새로 나타난 출처 행을 기존 사업장에 붙이거나 새 사업장을 만든다. → (project_id, 설명)"""
    area = f"{payload['region_sido']}|{cr.sgg_key(payload['region_sigungu'], payload['region_sido'])}"
    tag = f"{source}/{payload.get('project_type') or ''}"
    mine = cr.name_keys([payload["zone_name"]])
    my_lot = _lot_of(payload)
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
        same_lot = bool(my_lot) and any(_lot_of(p) == my_lot for _, _, p in rows)
        scored.append((max(cr._score(a, b) for a in mine for b in keys), same_lot, pid))
    scored.sort(reverse=True)
    if scored and scored[0][0] >= LINK_THRESHOLD:
        best, runner = scored[0], (scored[1] if len(scored) > 1 else None)
        clear = runner is None or runner[0] < best[0] - 0.03
        if clear or (best[1] and not runner[1]):  # 이름 점수가 비슷하면 대표지번이 같은 쪽
            how = f"유사도 {best[0]:.2f}" + (" · 지번 일치" if best[1] else "")
            return best[2], f"기존 사업장에 연결({how})"
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
def recompute(con, pid: str, manual: pd.DataFrame, today: str, log: list) -> None:
    old_json = con.execute("SELECT record FROM projects WHERE project_id=?", (pid,)).fetchone()[0]
    old = json.loads(old_json)
    rows = active_rows(con, pid)
    if not rows:  # 모든 출처에서 사라짐 — 지우지 않고 표시만
        if old and old.get("status") != "출처에서 사라짐":
            new = {**old, "status": "출처에서 사라짐"}
            log.append((today, pid, "모든 출처에서 사라짐", "status", old.get("status"), new["status"], ""))
            con.execute("UPDATE projects SET record=?, updated_at=? WHERE project_id=?", (dumps(new), today, pid))
        return
    new = cr.build_record(cr.rows_frame([(k, p) for _, k, p in rows]), manual)
    new["project_id"] = pid
    if dumps(new) == old_json:
        return
    if not old:
        log.append((today, pid, "신규 사업장", "stage", None, new.get("stage"), new.get("sources")))
    else:
        for f in sorted(set(old) | set(new)):
            if f in IGNORE_FIELDS or old.get(f) == new.get(f):
                continue
            kind = "변경"
            if f == "stage":
                o, n = cr.STAGE_ORDER.get(old.get(f), -1), cr.STAGE_ORDER.get(new.get(f), -1)
                kind = "단계 진전" if n > o else "단계 후퇴(확인 필요)"
            log.append((today, pid, kind, f, _s(old.get(f)), _s(new.get(f)), ""))
    con.execute("UPDATE projects SET record=?, updated_at=? WHERE project_id=?", (dumps(new), today, pid))


def _s(v):
    return None if v is None else str(v)


# ---------------------------------------------------------------- 매일 갱신
def update(con: sqlite3.Connection, today: str, force: bool = False,
           failed: list[str] | None = None) -> list[tuple]:
    if not con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]:
        sys.exit("상태가 비어 있습니다. 먼저 `python update_daily.py --init` 을 실행하세요.")
    manual = cr.load_manual()
    dirty: set[str] = set()
    log: list[tuple] = []
    pending, errors = list(cr.SOURCES), {}
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
                sync_source(con, src, today, force, manual, dirty, log)
                errors.pop(src.name, None)
            except Exception as e:
                errors[src.name] = str(e)
                print(f"[실패] {src.name}: {e}", file=sys.stderr)
                if isinstance(e, NETWORK_ERRORS) and src.host:
                    down_hosts.add(src.host)
                still.append(src)
        pending = still
    for src in pending:  # 끝까지 실패한 출처는 이전 데이터를 그대로 둔다
        print(f"[포기] {src.name} — 이전 데이터 유지", file=sys.stderr)
        if failed is not None:
            failed.append(f"{src.name}: {errors.get(src.name)}")

    # 수동 별칭표가 바뀌었으면: 지정된 병합을 적용하고 해당 지역 사업장을 다시 계산
    mh = manual_hash()
    if mh != (con.execute("SELECT value FROM meta WHERE key='manual_hash'").fetchone() or [""])[0]:
        print("[변경] manual_aliases.csv")
        dirty |= apply_manual(con, manual, today, log)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('manual_hash', ?)", (mh,))

    for pid in sorted(dirty):
        recompute(con, pid, manual, today, log)
    con.executemany("INSERT INTO changes VALUES (?,?,?,?,?,?,?)", log)
    return log


def sync_source(con, src: cr.Source, today: str, force: bool, manual: pd.DataFrame,
                dirty: set[str], log: list) -> None:
    """출처 하나를 확인해 바뀐 행만 반영한다. 내려받기 실패는 예외로 올린다(호출 쪽에서 재시도)."""
    stored = con.execute("SELECT fingerprint, content_hash FROM sources WHERE name=?", (src.name,)).fetchone()
    fp = safe_probe(src)
    if not force and fp and stored and stored[0] == fp:
        print(f"[그대로] {src.name} (probe 동일 — 다운로드 생략)")
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
    recs = with_enrichment(recs)
    recs = [r for r in recs if active_rows(con, r["project_id"]) or r.get("status") == "출처에서 사라짐"]
    res, excluded = cr.records_frame(recs)
    cr.write_outputs(res, excluded, "latest")
    if complexes.REGISTRY_FILE.exists() or enrich.TRADES_FILE.exists():  # 30년 이상 노후 단지 목록
        complexes.write(complexes.build(today, recs, enrich.project_lots(con)), today)
    if log:
        CHANGES.mkdir(parents=True, exist_ok=True)
        path = CHANGES / f"changes_{today.replace('-', '')}.csv"
        df = pd.DataFrame(log, columns=["run_date", "project_id", "kind", "field", "old", "new", "source"])
        names = {r["project_id"]: r.get("zone_name") for r in recs}
        df.insert(2, "zone_name", df["project_id"].map(names))
        new = not path.exists()  # 같은 날 두 번 실행되면 이어 붙인다(BOM은 처음 한 번만)
        df.to_csv(path, mode="a", header=new, index=False, encoding="utf-8-sig" if new else "utf-8", lineterminator="\n")
        print(f"[이력] {len(log)}건 → {path.relative_to(cr.BASE)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", action="store_true", help="최초 전체 구축")
    ap.add_argument("--force", action="store_true", help="probe를 무시하고 모든 출처를 내려받아 비교")
    ap.add_argument("--dry-run", action="store_true", help="변경 내용만 출력하고 저장하지 않음")
    ap.add_argument("--migrate", action="store_true",
                    help="수집 열을 늘린 뒤 1회 실행: 모든 출처를 다시 받아 반영하되, 새 열 때문에 생긴 변경은 이력에서 뺀다")
    ap.add_argument("--skip-enrich", action="store_true", help="건축물대장·실거래가 보강을 건너뜀")
    ap.add_argument("--date", default=date.today().isoformat(), help="기록용 날짜(테스트용)")
    args = ap.parse_args()

    con = connect()
    if args.init:
        init(con, args.date)
        export(con, [], args.date)
        return
    failed: list[str] = []
    log = update(con, args.date, force=args.force or args.migrate, failed=failed)
    if args.migrate:  # 계산 규칙·열이 바뀌었을 수 있으니 모든 사업장을 다시 계산한다(바뀐 것만 이력에 남는다)
        manual = cr.load_manual()
        for (pid,) in con.execute("SELECT project_id FROM projects").fetchall():
            recompute(con, pid, manual, args.date, log)
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
            project_lots = {enrich.lot_key(l) for l in enrich.project_lots(con).values()}
            enriched = complexes.update(args.date, failed, project_lots) or enriched
        except Exception as e:  # 보강이 실패해도 사업장 데이터 갱신은 그대로 반영한다
            failed.append(f"보강(건축물대장·실거래가): {e}")
    else:
        print("[보강] DATA_GO_KR_KEY 없음 — 건축물대장·실거래가 건너뜀")
    latest = cr.OUT / "capital_area_apartment_reconstruction_latest.csv"
    if log or enriched or args.migrate or not latest.exists():
        export(con, log, args.date)
    print(f"[완료] 변경 {len(log)}건" + ("" if log else " — 출력 파일은 그대로 둠"))
    write_summary(log, failed, args.date)
    if failed:  # 성공한 출처의 변경은 저장했지만, 실패가 있었음을 종료 코드로 알린다(Actions에서 빨간불)
        sys.exit(1)


def with_enrichment(recs: list[dict]) -> list[dict]:
    """건축물대장·실거래가(enrich.py가 state/에 저장)를 사업장 레코드에 붙인다.
    출처에 없는 기존 단지 값(세대수·용적률·준공연도)은 건축물대장 값으로 채운다."""
    extra = enrich.fields({r["project_id"]: r for r in recs})
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
    row_kinds = {"출처 행 추가", "출처에서 삭제", "신규 사업장", "모든 출처에서 사라짐", "수동 병합"}
    keep_fields = {"stage", "status", *cr.DATE_COLS}
    kept = [r for r in log if (r[2] in row_kinds or r[3] in keep_fields)
            and not (r[3] == "stage" and (r[4], r[5]) in cr.RENAMED_STAGES)]  # 단계 이름만 바뀐 것은 변경이 아니다
    note = (today, "*", "스키마 확장", "columns", None, ", ".join(cr.DETAIL_COLS), "")
    print(f"[migrate] 변경 {len(log)}건 중 {len(kept)}건만 이력에 남김 (나머지는 새 열 채움)")
    return [note, *kept]


def write_summary(log: list[tuple], failed: list[str], today: str) -> None:
    """GitHub Actions 실행 화면에 보일 요약(GITHUB_STEP_SUMMARY)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    kinds = pd.Series([r[2] for r in log]).value_counts() if log else pd.Series(dtype=int)
    lines = [f"## 재건축 데이터 갱신 {today}", "", f"- 변경 {len(log)}건"]
    lines += [f"  - {k}: {v}건" for k, v in kinds.items()]
    lines += [f"- ❌ 실패: {f}" for f in failed]
    stages = [r for r in log if r[3] == "stage"]
    if stages:
        lines += ["", "| 사업장 | 변경 | 이전 | 이후 |", "|---|---|---|---|"]
        lines += [f"| {r[1]} | {r[2]} | {r[4] or ''} | {r[5] or ''} |" for r in stages[:50]]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
