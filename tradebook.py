"""거래 장부 — 매매(최근 12개월 창 + 2006년~ 이력)·전월세·분양권/입주권을 단지(부동산원 COMPLEX_PK)와 사업장에 묶는다.

  - 요약 필드(12개월 매매 중위·전세가율·이력 건수 등)는 apartments.json / projects.json 행에 붙인다(숫자 몇 개뿐).
  - 건별 목록과 면적대별 표는 단지별 파일 docs/data/trades/<id>.json 에 쓰고, 페이지가 상세를 열 때만 읽는다.
    파일은 한 거래가 한 줄이라 git diff에 새 거래만 보인다. 내용이 같으면 다시 쓰지 않는다.

창(state/trades.jsonl)의 거래는 갱신 때 연결한 단지·사업장을 그대로 쓰고, 이력(state/trades_history/)은 연결을 저장하지 않으므로
여기서 같은 규칙(identity.Index.match_trade, enrich.ProjectLinker)으로 다시 연결한다.
"""
from __future__ import annotations

import json

import collect_reconstruction as cr
import enrich as en
import identity

TRADES_DIR = cr.BASE / "docs" / "data" / "trades"
SALE_COLS = ["date", "area", "floor", "price", "dong", "type", "built"]      # 가격 만원, dong = 동 번호, type = 중개/직거래
RENT_COLS = ["date", "area", "floor", "deposit", "monthly", "contract", "term", "renewal"]  # 보증금·월세 만원
SILV_COLS = ["date", "area", "floor", "price", "kind", "type"]              # kind = 입주권/분양권


def file_id(complex_id: str) -> str:
    """단지 파일 이름: 부동산원 PK는 그대로, 'lot:…'(부동산원에 없는 지번)는 'lot_…'."""
    return complex_id.replace(":", "_")


def project_file_id(project_id: str) -> str:
    return "p-" + project_id


def _sale(t: dict) -> tuple:
    return (t["date"], t["area"], t["floor"], t["price"], t.get("dong_no"), t.get("deal_type"), t.get("built"))


def _rent(r: dict) -> tuple:
    return (r["date"], r["area"], r["floor"], r["deposit"], r["monthly"], r.get("contract"), r.get("term"),
            True if r.get("renewal_right") else None)


def _silv(r: dict) -> tuple:
    return (r["date"], r["area"], r["floor"], r["price"], r["kind"], r.get("deal_type"))


def _sale_dicts(rows: list[tuple]) -> list[dict]:
    return [{"date": d, "area": a, "floor": f, "price": p, "dong_no": dn, "deal_type": dt, "built": b}
            for d, a, f, p, dn, dt, b in rows]


def _rent_dicts(rows: list[tuple]) -> list[dict]:
    return [{"date": d, "area": a, "floor": f, "deposit": dep, "monthly": mo, "kind": "전세" if mo == 0 else "월세",
             "contract": c, "term": t, "renewal_right": rr} for d, a, f, dep, mo, c, t, rr in rows]


def _sort_key(r: tuple) -> tuple:
    """None은 값보다 앞에. 열마다 형이 하나(문자열·숫자·True)라 (있음 여부, 값) 쌍이면 비교할 수 있다."""
    return tuple((0, "") if x is None else (1, x) for x in r)


class Book:
    def __init__(self, today: str, lots: dict[str, dict], names: dict[str, set[str]], records: dict[str, dict]):
        self.today = today
        self.window = set(en.months_back(today, en.TRADE_WINDOW))
        self.idx = identity.Index()
        self.linker = en.ProjectLinker(lots, names)
        self.records = records
        self.cx_sales: dict[str, list[tuple]] = {}
        self.lot_sales: dict[str, list[tuple]] = {}   # 단지를 못 가린 거래(지번 공통) 또는 부동산원에 없는 지번
        self.pid_sales: dict[str, list[tuple]] = {}
        self.cx_rents: dict[str, list[tuple]] = {}
        self.lot_rents: dict[str, list[tuple]] = {}
        self.pid_rents: dict[str, list[tuple]] = {}
        self.pid_silv: dict[str, list[tuple]] = {}
        self._cx_cache: dict[tuple, tuple] = {}
        self.history_rows = 0
        self._load()

    # ------------------------------------------------------------ 읽기
    def _complex(self, t: dict) -> tuple[str | None, str | None]:
        key = (t["sgg"], t["dong"], t["jibun"], t["apt"], t.get("dong_no"))
        if key not in self._cx_cache:
            lot = identity.jibun_key(t["sgg"], t["dong"], t["jibun"])
            self._cx_cache[key] = self.idx.match_trade(lot, t["apt"], t.get("dong_no"))
        return self._cx_cache[key]

    def _put(self, by_cx: dict, by_lot: dict, by_pid: dict, t: dict, row: tuple, cid: str | None, pid: str | None) -> None:
        if cid:
            by_cx.setdefault(cid, []).append(row)
        else:
            lot = identity.jibun_key(t["sgg"], t["dong"], t["jibun"])
            if lot:
                by_lot.setdefault(lot, []).append(row)
        if pid:
            by_pid.setdefault(pid, []).append(row)

    def _load(self) -> None:
        for t in en._read_jsonl(en.TRADES_FILE):
            self._put(self.cx_sales, self.lot_sales, self.pid_sales, t, _sale(t), t.get("complex_id"), t.get("project_id"))
        if en.HISTORY_DIR.exists():
            for path in sorted(en.HISTORY_DIR.glob("*.jsonl")):
                sgg = path.stem
                for t in en._read_jsonl(path):
                    t["sgg"] = sgg
                    cid, _ = self._complex(t)
                    pid, _ = self.linker.link(sgg, t["dong"], t["jibun"], t["apt"])
                    self._put(self.cx_sales, self.lot_sales, self.pid_sales, t, _sale(t), cid, pid)
                    self.history_rows += 1
        for r in en._read_jsonl(en.RENTS_FILE):
            self._put(self.cx_rents, self.lot_rents, self.pid_rents, r, _rent(r), r.get("complex_id"), r.get("project_id"))
        for r in en._read_jsonl(en.SILV_FILE):
            if r.get("project_id"):
                self.pid_silv.setdefault(r["project_id"], []).append(_silv(r))
        for d in (self.cx_sales, self.lot_sales, self.pid_sales, self.cx_rents, self.lot_rents, self.pid_rents, self.pid_silv):
            for rows in d.values():
                rows.sort(key=_sort_key)

    # ------------------------------------------------------------ 단지
    def complex_sales(self, cid: str, lot: str | None, is_reb: bool) -> tuple[list[tuple], str | None]:
        """단지의 매매: 부동산원 단지면 자기 거래, 없으면 같은 지번의 동 미상 거래. 지번 항목(부동산원에 없음)은 지번 거래 전부."""
        own, shared = self.cx_sales.get(cid, []), self.lot_sales.get(lot or "", [])
        if not is_reb:
            return shared, "지번"
        if own:
            return own, "단지"
        return shared, ("지번 공통(동 미상)" if shared else None)

    def complex_rents(self, cid: str, lot: str | None, is_reb: bool) -> list[tuple]:
        own, shared = self.cx_rents.get(cid, []), self.lot_rents.get(lot or "", [])
        if not is_reb:
            return shared
        return own or shared

    def _in_window(self, rows: list[tuple]) -> list[tuple]:
        return [r for r in rows if r[0][:7].replace("-", "") in self.window]

    def complex_fields(self, cid: str, lot: str | None, is_reb: bool) -> dict:
        sales, basis = self.complex_sales(cid, lot, is_reb)
        rents = self.complex_rents(cid, lot, is_reb)
        return self._fields(sales, basis, rents, [])

    def _fields(self, sales: list[tuple], basis: str | None, rents: list[tuple], silv: list[tuple]) -> dict:
        out: dict = {}
        recent = _sale_dicts(self._in_window(sales))
        if recent:
            out.update(en.trade_summary(recent), trade_basis=basis)
        if sales:
            out["trade_count_all"] = len(sales)
            out["trade_since"] = int(sales[0][0][:4])
        if rents:
            out.update(en.rent_summary(_rent_dicts(rents), out.get("trades_by_area")))
        if silv:
            latest = max(silv, key=lambda r: (r[0], r[3]))
            out.update(silv_count=len(silv), silv_latest_date=latest[0], silv_latest_price=latest[3],
                       silv_latest_area=round(latest[1]), silv_entry_count=sum(1 for r in silv if r[4] == "입주권"))
        if sales or rents or silv:
            out["trades"] = True  # 상세 파일(docs/data/trades/<id>.json) 있음
        return out

    # ------------------------------------------------------------ 사업장
    def project_rows(self, pid: str, rec: dict, bld: dict) -> tuple[list[tuple], str, list[tuple], list[tuple]]:
        """(매매, 거래 기준, 전월세, 분양권·입주권) — 기존 단지 준공연도보다 훨씬 늦은 건물의 매매는 뺀다(enrich.project_trade_filter).
        전월세 행에는 준공연도가 없어 거르지 않는다."""
        use, basis = en.project_trade_filter(_sale_dicts(self.pid_sales.get(pid, [])), rec, bld)
        keep = {(t["date"], t["area"], t["floor"], t["price"], t["built"]) for t in use}
        sales = [r for r in self.pid_sales.get(pid, []) if (r[0], r[1], r[2], r[3], r[6]) in keep]
        return sales, basis, self.pid_rents.get(pid, []), self.pid_silv.get(pid, [])

    def project_fields(self, pid: str, rec: dict, bld: dict) -> dict:
        sales, basis, rents, silv = self.project_rows(pid, rec, bld)
        return self._fields(sales, basis, rents, silv)

    # ------------------------------------------------------------ 단지별 파일
    def write_files(self, apartments: list[dict], projects: list[dict]) -> tuple[int, int]:
        """docs/data/trades/<id>.json — 바뀐 파일만 쓰고, 더는 없는 단지의 파일은 지운다. → (쓴 수, 지운 수)"""
        TRADES_DIR.mkdir(parents=True, exist_ok=True)
        expected: dict[str, str] = {}
        for a in apartments:
            if not a.get("trades"):
                continue
            is_reb = bool(a.get("reb_pk"))
            sales, basis = self.complex_sales(a["complex_id"], a.get("lot"), is_reb)
            rents = self.complex_rents(a["complex_id"], a.get("lot"), is_reb)
            expected[file_id(a["complex_id"])] = _render(a["complex_id"], a.get("name"), basis, sales, rents, [],
                                                         a.get("trades_by_area"), a.get("rents_by_area"))
        for p in projects:
            if not p.get("trades"):
                continue
            sales, basis, rents, silv = self.project_rows(p["project_id"], p, p)
            expected[project_file_id(p["project_id"])] = _render(p["project_id"], p.get("zone_name"), basis, sales, rents,
                                                                  silv, p.get("trades_by_area"), p.get("rents_by_area"))
        written = deleted = 0
        for fid, text in expected.items():
            path = TRADES_DIR / f"{fid}.json"
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8", newline="\n")
                written += 1
        for path in TRADES_DIR.glob("*.json"):
            if path.stem not in expected:
                path.unlink()
                deleted += 1
        print(f"[거래] 단지·사업장 상세 파일 {len(expected)}개 (새로 씀 {written}, 지움 {deleted}) → docs/data/trades/")
        return written, deleted


def _dump(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def _rows(rows: list[tuple]) -> str:
    return "[\n" + ",\n".join(_dump(list(r)) for r in rows) + ("\n" if rows else "") + "]"


def _render(cid: str, name: str | None, basis: str | None, sales: list[tuple], rents: list[tuple], silv: list[tuple],
            sales_by_area, rents_by_area) -> str:
    """한 거래가 한 줄인 JSON. 면적대별 표(by_area)는 12개월 매매·전세 요약을 면적으로 합친 것."""
    by_area: dict[int, dict] = {}
    for a in sales_by_area or []:
        by_area.setdefault(a["area"], {}).update(a)
    for a in rents_by_area or []:
        by_area.setdefault(a["area"], {}).update(a)
    parts = [f'"id":{_dump(cid)}', f'"name":{_dump(name)}', f'"basis":{_dump(basis)}',
             f'"by_area":{_dump([by_area[k] for k in sorted(by_area)])}',
             f'"sales":{{"cols":{_dump(SALE_COLS)},"rows":{_rows(sales)}}}',
             f'"rents":{{"cols":{_dump(RENT_COLS)},"rows":{_rows(rents)}}}']
    if silv:
        parts.append(f'"silv":{{"cols":{_dump(SILV_COLS)},"rows":{_rows(silv)}}}')
    return "{" + ",\n".join(parts) + "}\n"
