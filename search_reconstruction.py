"""재건축 사업장 이름 검색. 어떤 표기로 입력해도 같은 사업장을 찾는다.

    python search_reconstruction.py 은마
    python search_reconstruction.py "압구정 3구역"
    python search_reconstruction.py 목동아파트7단지 --all     # 상세 일자까지 출력

정규화(공백·'아파트'·'재건축'·'제N구역' 등 표기 차이 제거) 후
  1) 별칭과 정확히 일치 → 2) 부분 일치 → 3) 오타 허용 유사도 순으로 찾는다.
"""
from __future__ import annotations

import argparse
import sys
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

from collect_reconstruction import search_key

OUT = Path(__file__).resolve().parent / "output"


def latest(pattern: str) -> Path:
    """update_daily.py가 만드는 *_latest.csv를 우선, 없으면 가장 최근 날짜 파일."""
    files = sorted(OUT.glob(pattern), key=lambda p: (p.stem.endswith("_latest"), p.stem))
    if not files:
        sys.exit(f"{OUT}에 {pattern} 파일이 없습니다. 먼저 collect_reconstruction.py를 실행하세요.")
    return files[-1]


def search(query: str, aliases: pd.DataFrame, limit: int = 10) -> pd.DataFrame:
    q = search_key(query)
    if not q:
        return aliases.iloc[0:0]
    key = aliases["alias_key"].fillna("")
    exact = aliases[key.eq(q)].assign(score=1.0)
    part = aliases[key.str.contains(q, regex=False) | key.map(lambda k: len(k) >= 2 and k in q)]
    part = part.assign(score=0.9 * part["alias_key"].map(lambda k: min(len(k), len(q)) / max(len(k), len(q))))
    fuzzy = aliases.assign(score=key.map(lambda k: SequenceMatcher(None, q, k).ratio() * 0.8))
    fuzzy = fuzzy[fuzzy["score"] >= 0.55]
    hits = pd.concat([exact, part, fuzzy]).sort_values("score", ascending=False)
    best = hits.groupby("project_id", sort=False).agg(score=("score", "max"), matched_alias=("alias", "first"))
    return best.sort_values("score", ascending=False).head(limit)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--all", action="store_true", help="모든 단계 일자 출력")
    ap.add_argument("-n", type=int, default=10)
    args = ap.parse_args()

    aliases = pd.read_csv(latest("reconstruction_aliases_*.csv"), dtype=str)
    data = pd.read_csv(latest("capital_area_apartment_reconstruction_*.csv"), dtype=str).set_index("project_id")
    res = search(args.query, aliases, args.n)
    if res.empty:
        print("검색 결과 없음")
        return
    for pid, r in res.iterrows():
        d = data.loc[pid]
        print(f"[{r['score']:.2f}] {d['region_sido']} {d['region_sigungu']} · {d['zone_name']}"
              f"  — 현재 단계: {d['stage']}  (일치: '{r['matched_alias']}')")
        print(f"       다른 이름: {d['name_variants']}")
        if args.all:
            for c in ["designated_at", "committee_at", "union_approved_at", "arch_review_at", "plan_approved_at",
                      "mgmt_approved_at", "started_at", "completed_at", "planned_units", "sources"]:
                if isinstance(d[c], str):
                    print(f"       {c}: {d[c]}")


if __name__ == "__main__":
    main()
