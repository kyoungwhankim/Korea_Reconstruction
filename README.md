# Korea_Reconstruction

수도권(서울·경기·인천) **아파트 재건축 사업 진행 현황** 데이터. 정부·지자체가 공개한 자료만 모아 사업장 단위로 합치고, GitHub Actions가 매일 바뀐 부분만 갱신한다.

## 검색 페이지

**https://kyoungwhankim.github.io/Korea_Reconstruction/** — 단지명·별칭, 시도·시군구, 주소, 진행 단계, 용적률·건폐율·세대수·구역면적·준공연도 등 필드별 조건으로 검색. 검색 조건은 주소(URL)에 남아 그대로 공유할 수 있다. 페이지는 `docs/`에 있고 `docs/data/projects.json`을 읽는다(매일 갱신 때 함께 커밋).

## 30년 이상 노후 단지 (`complexes.py`)

재건축 사업장과 별도로, 수도권에서 준공 30년이 넘은 아파트 단지 전체(사업이 아직 없는 단지 포함)를 모은다. 페이지의 **30년 이상 노후 단지** 보기, `output/old_apartments_latest.csv`, `docs/data/apartments.json`.

- **건축물대장 총괄표제부**를 법정동(동·리 약 2,900곳)마다 나열 → 단지명·세대수(20세대 이상 공동주택)·대지면적·용적률·건폐율·사용승인일. 분기마다 한 바퀴, 매일 100곳씩(`state/complexes/registry.jsonl`, `registry_dongs.json`).
- **실거래가**를 수도권 83개 시군구 전체에서 받아, 1996년(=올해−30) 이전 준공 단지의 거래를 남긴다 → 준공연도·전용면적별 가격.
- 사용승인일이 어디에도 없는 100세대 이상 단지는 동별 표제부로 채운다(매일 150곳).
- 단지 구분은 `법정동코드-산여부-본번-부번`. 재건축 사업장 대표 지번과 같으면 사업 단계를 붙인다.
- **K-apt**(공동주택관리정보시스템, `AptListService4` 단지 목록·`AptBasisInfoServiceV5` 기본정보): 분기마다 단지 목록, 단지별 기본정보는 한 번 받아 캐시(`state/complexes/kapt*.jsonl`) → 공식 단지명·사용승인일·세대수·동수·최고층·난방·시공사. 의무관리대상(대략 150세대 이상) 단지만 있다. 인증키가 K-apt에 등록되지 않았으면 건너뛴다.
- 준공연도는 실거래 건축년도·K-apt 사용승인일을 우선하고, 없을 때만 건축물대장 사용승인일(오기가 있어 1960년 이전 값은 버림)을 쓴다.
- 한계: 한 단지가 여러 지번에 걸쳐 있으면 지번별로 나뉠 수 있고, 거래도 없고 K-apt·대장에 사용승인일도 없는 작은 단지는 빠질 수 있다.

## 데이터 (`output/`)

| 파일 | 내용 |
|---|---|
| `capital_area_apartment_reconstruction_latest.csv` | 사업장당 1행. 현재 단계, 단계별 일자(구역지정·추진위·조합설립·건축심의·사업시행·관리처분·착공·준공), 세대수, 단지·사업 지표(아래), 출처별 단계 |
| `reconstruction_aliases_latest.csv` | 별칭 1개당 1행(`alias` → `project_id`). 같은 단지의 여러 이름(출처별 표기·띄어쓰기·동 이름·구성 단지 등) |
| `excluded_single_house_latest.csv` | 단독주택 재건축이라 제외한 사업장 |
| `changes/changes_YYYYMMDD.csv` | 그날 바뀐 내용(사업장·항목·이전 값·이후 값) |

단지·사업 지표 열 — `planned`는 정비계획상 신축 계획값, `existing`은 현재 단지 값(출처가 제공하는 경우만):
`site_area_m2`(구역면적), `zoning_before/after`(용도지역), `far_existing`·`far_planned`(용적률), `bcr_planned`(건폐율), `gfa_planned_m2`(연면적), `floors_planned`(지상 최고층), `height_planned_m`, `buildings_planned`·`existing_buildings`(동수), `units_sale`·`units_rent`(분양·임대), `existing_built_year`(기존 준공연도), `owners_count`(토지등소유자), `members_count`(조합원).

`project_id`는 한 번 정해지면 바뀌지 않는다. 이름이 바뀌거나 출처가 추가돼도 같은 사업장은 같은 ID로 남는다.

### 출처

| 지역 | 출처 | 갱신 주기 | 단계별 일자 |
|---|---|---|---|
| 서울 | 서울시 정비사업 추진현황 (열린데이터광장 OA-22856) | 분기 | O |
| 서울 | 서울시 도시계획사업(서울플랜+) 정비사업 현황 (OA-23112) — 신속통합기획·준공 포함 | 수시 | 현재 단계 일자 |
| 경기 | 경기도 일반 정비 사업 추진 현황 (경기데이터드림) | 분기 | O |
| 경기 | 부천시 정비사업 추진현황 | 분기 | O |
| 인천 | 인천시 도시 및 주거환경 정비사업 추진현황 (data.go.kr 15055212) | 월 | X |
| 인천 | 인천시 정비사업개요(추정분담금정보시스템, data.go.kr 15080866) — 면적·동수·건폐율·용적률 | 수시 | X |
| 인천 | 미추홀구·연수구 정비사업 추진현황 (data.go.kr 15150904, 15039862) | 수시 | O |
| 전국 | 국토부 전국 도시정비사업 통합 데이터 (data.go.kr 15160169) | 수시 | X |

현재 단계는 출처들 중 가장 진전된 단계를 쓴다. 출처별 값은 `stage_by_source` 열에 있다.

### 오픈API 보강 (`enrich.py`, 인증키 필요)

사업장 주소를 법정동코드·지번으로 바꿔(국토교통부 법정동코드 파일) 아래 두 API로 조회한다. 결과는 `state/buildings.jsonl`, `state/trades.jsonl`에 따로 저장하고 내보낼 때만 사업장에 붙인다.

| API | 쓰는 값 | 갱신 |
|---|---|---|
| 국토교통부 건축HUB 건축물대장정보 (총괄표제부·표제부) | 대지면적·세대수 → 평균 대지지분(`land_share_m2`), 기존 용적률·건폐율(`bld_far`, `bld_bcr`), 사용승인일 | 지번이 새로 생기거나 바뀐 사업장만 |
| 국토교통부 아파트 매매 실거래가 | 최근 12개월 거래(해제 제외)를 지번·단지명으로 연결 → 전용면적별 건수·중위·최저·최고·최근가, 전용 평당가 | 매일 최근 3개월만 다시 받음(신고 기한 30일) |

- 오래된 건축물대장은 대지면적이 비어 있는 경우가 많아 대지지분은 일부 사업장만 나온다.
- 준공된 사업장의 지번에는 새 아파트가 있어 `trade_basis = 준공 후 신축`으로 구분한다. 진행 중인 사업장에서 기존 단지보다 훨씬 늦게 지어진 건물의 거래는 뺀다.
- 인증키: GitHub Actions는 secret `DATA_GO_KR_KEY`(Decoding 키), 로컬은 `.env`(커밋 안 됨)에 `DATA_GO_KR_KEY=...`. 키가 없으면 보강만 건너뛴다. 요청은 초당 5건으로 제한한다(넘으면 429).

## 매일 갱신 (`.github/workflows/daily-update.yml`)

매일 07:30 KST에 `update_daily.py`가 실행되고, 바뀐 것이 있을 때만 커밋한다.

1. **출처 단위**: 파일 ID·파일명(probe)이 그대로면 내려받지 않는다. probe가 없는 출처는 받아서 내용 해시를 비교한다.
2. **행 단위**: 바뀐 출처만 이전 행과 비교해 추가·수정·삭제된 행을 고른다.
3. **사업장 단위**: 그 행에 연결된 사업장만 다시 계산하고 필드별 변경 이력을 남긴다.

상태(`state/`)는 정렬된 JSONL 텍스트라 커밋 diff에 바뀐 행만 보인다. 일부 출처가 실패하면 성공한 부분은 커밋하고 실행은 실패로 표시된다.
Actions 탭 → *Daily reconstruction update* → *Run workflow*로 수동 실행할 수 있다(`force`: 모든 출처를 강제로 비교).

## 로컬 사용

```bash
pip install -r requirements.txt
python search_reconstruction.py 은마          # 어떤 이름으로든 검색 (--all: 단계별 일자)
python update_daily.py --dry-run              # 지금 갱신하면 무엇이 바뀌는지만 확인
python collect_reconstruction.py              # (검증용) 상태와 무관하게 전체를 새로 구축 → output/*_full_*.csv
python update_daily.py --migrate              # 수집 열을 늘린 뒤 1회: 새 열 때문에 생긴 변경은 이력에서 제외
python -m http.server 8000 --directory docs   # 검색 페이지 로컬 확인 → http://localhost:8000
python update_daily.py --skip-enrich          # 오픈API 보강 없이 출처만 갱신
```

`manual_aliases.csv`에 행을 추가하면 자동으로 못 묶은 같은 사업장을 합치거나, 공식 자료에 없는 통칭(예: `압구정 현대`)을 별칭으로 붙일 수 있다. 다음 실행 때 반영된다.

## 한계

- 경기 광주·양주·여주·포천·가평·양평·연천, 인천 영종·검단·옹진은 공개 데이터셋이 없다.
- 소규모재건축·리모델링, 정비계획 절차 전(안전진단 단계) 단지는 포함하지 않는다.
- 출처가 대부분 월·분기 단위로 갱신되므로 매일 실행해도 변경이 없는 날이 많다.
- `manual_aliases.csv`의 압구정 통칭 행은 공식 자료가 아닌 수기 입력이다.
