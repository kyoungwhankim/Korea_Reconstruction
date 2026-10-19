"use strict";

// 단계(표준) — 순서·색 구분. '사업 미지정'은 노후 단지 보기에서 아직 재건축 사업이 없는 단지
const STAGES = [
  ["예정구역·후보지", "s-early"], ["안전진단", "s-early"], ["정비계획수립", "s-early"], ["정비구역지정", "s-early"],
  ["추진위구성", "s-early"], ["조합설립인가", "s-mid"], ["건축심의", "s-mid"], ["사업시행인가", "s-mid"],
  ["관리처분인가", "s-late"], ["이주·철거", "s-late"], ["착공", "s-late"], ["분양", "s-late"],
  ["준공", "s-done"], ["이전고시·청산", "s-done"], ["사업 미지정", "s-none"],
];
const STAGE_CLASS = Object.fromEntries(STAGES);
const NO_PROJECT = "사업 미지정";
const NEW_KIND = "신규 사업장 후보";   // 검토 대기열의 종류: 사업장이 없는 노후 단지에서 단계 표현이 보인 기사 (news.py NEW_KIND)

const DATE_FIELDS = [
  ["designated_at", "정비구역지정"], ["committee_at", "추진위구성"], ["union_approved_at", "조합설립인가"],
  ["arch_review_at", "건축심의"], ["plan_approved_at", "사업시행인가"], ["mgmt_approved_at", "관리처분인가"],
  ["relocation_started_at", "이주 시작"], ["started_at", "착공"], ["completed_at", "준공"],
];
const TRADE_RANGES = [
  ["trade_latest_price", "최근 거래가", "만원"],
  ["trade_median_ppp", "평당가(전용, 1년 중위)", "만원"],
  ["trade_count_12m", "1년 거래 건수", "건"],
  ["trade_count_all", "매매 이력 건수(2006년~)", "건"],
  ["jeonse_median", "전세 중위(대표 면적, 1년)", "만원"],
  ["jeonse_ratio", "전세가율(전세 중위 ÷ 매매 중위)", "%"],
  ["rent_count_12m", "1년 전월세 건수", "건"],
];
const TRADE_CSV = ["trade_count_12m", "trade_latest_date", "trade_latest_price", "trade_median_ppp", "trade_count_all",
  "trade_since", "rent_count_12m", "jeonse_area", "jeonse_median", "jeonse_ratio"];

// 두 가지 보기: [필드, 제목, 숫자 여부] 열 / [필드, 라벨, 단위] 숫자 조건
const VIEWS = {
  projects: {
    label: "재건축 사업장", unit: "사업장", file: "data/projects.json", list: "projects",
    id: "project_id", name: "zone_name", sort: { key: "stage_order", dir: -1 },
    address: p => p.address || "",
    columns: [
      ["zone_name", "단지(구역)", false], ["stage_order", "현재 단계", false],
      ["existing_units", "기존 세대", true], ["planned_units", "계획 세대", true],
      ["far_existing", "기존 용적률", true], ["far_planned", "계획 용적률", true],
      ["land_share_m2", "대지지분(㎡)", true], ["site_area_per_unit_m2", "세대당 구역면적(㎡)", true],
      ["trade_latest_price", "최근 거래가", true], ["trade_median_ppp", "평당가(전용)", true],
      ["jeonse_ratio", "전세가율", true], ["existing_built_year", "준공연도", true],
    ],
    ranges: [
      ["far_existing", "기존 용적률", "%"], ["far_planned", "계획 용적률", "%"], ["bcr_planned", "계획 건폐율", "%"],
      ["existing_units", "기존 세대수", "세대"], ["planned_units", "계획 세대수", "세대"],
      ["site_area_m2", "구역면적", "㎡"], ["site_area_per_unit_m2", "세대당 구역면적", "㎡"],
      ["existing_built_year", "기존 준공연도", "년"], ["floors_planned", "계획 최고층", "층"],
      ["height_planned_m", "계획 높이", "m"], ["owners_count", "토지등소유자", "명"],
      ["land_share_m2", "대지지분(건축물대장)", "㎡"], ["bld_bcr", "기존 건폐율", "%"], ...TRADE_RANGES,
    ],
    detail: projectDetail,
    csv: ["project_id", "region_sido", "region_sigungu", "zone_name", "stage", ...DATE_FIELDS.map(([k]) => k),
      "existing_units", "planned_units", "far_existing", "far_planned", "bcr_planned", "site_area_m2",
      "site_area_per_unit_m2", "land_share_m2", "existing_built_year", ...TRADE_CSV, "silv_count", "silv_entry_count",
      "silv_latest_date", "silv_latest_price", "address", "zoning_before", "zoning_after", "name_variants"],
  },
  apartments: {
    label: "30년 이상 노후 단지", unit: "단지", file: "data/apartments.json", list: "apartments",
    id: "complex_id", name: "name", sort: { key: "built_year", dir: 1 },
    address: p => `${p.dong || ""} ${p.jibun || ""}`,
    columns: [
      ["name", "단지", false], ["stage_order", "재건축 단계", false],
      ["built_year", "준공연도", true], ["households", "세대수", true], ["far", "용적률", true],
      ["bcr", "건폐율", true], ["land_share_m2", "대지지분(㎡)", true],
      ["trade_latest_price", "최근 거래가", true], ["trade_median_ppp", "평당가(전용)", true],
      ["jeonse_ratio", "전세가율", true], ["trade_count_12m", "1년 거래", true],
    ],
    ranges: [
      ["built_year", "준공연도", "년"], ["age", "경과 연수", "년"], ["households", "세대수", "세대"],
      ["buildings", "동수", "동"], ["top_floor", "최고층", "층"], ["far", "용적률(건축물대장)", "%"], ["bcr", "건폐율(건축물대장)", "%"],
      ["land_area_m2", "대지면적", "㎡"], ["land_share_m2", "대지지분", "㎡"], ...TRADE_RANGES,
    ],
    detail: apartmentDetail,
    csv: ["complex_id", "region_sido", "region_sigungu", "dong", "jibun", "dong_range", "siblings", "name", "stage", "project_name",
      "built_year", "age", "households", "buildings", "top_floor", "heating", "builder", "land_area_m2", "land_share_m2", "far", "bcr",
      ...TRADE_CSV, "name_variants", "sources"],
  },
};
const MONEY = new Set(["trade_latest_price", "jeonse_median", "silv_latest_price"]);  // 만원 → '34.0억'
const PERCENT = new Set(["jeonse_ratio"]);
const YEAR = new Set(["existing_built_year", "built_year", "trade_since"]);

// 검색어 정규화 — collect_reconstruction.zone_key와 같은 규칙(표기 차이 무시)
const CIRCLED = Object.fromEntries([..."①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"].map((c, i) => [c, String(i + 1)]));
Object.assign(CIRCLED, { "Ⅰ": "1", "Ⅱ": "2", "Ⅲ": "3", "Ⅳ": "4", "Ⅴ": "5" });
function normalize(s) {
  s = [...String(s)].map(c => CIRCLED[c] ?? c).join("");
  s = s.replace(/\(재촉\)|\(통합재건축\)/g, "").replace(/\s+/g, "");
  s = s.replace(/(?<=[가-힣])A(?=$|\d|[(,·])/g, "");
  s = s.replace(/아파트지구특별계획구역|특별계획구역|재정비촉진|촉진/g, "");
  s = s.replace(/(주택)?재건축(정비)?(사업)?(조합)?|정비사업|정비구역|아파트|APT|주택|일대|번지/gi, "");
  s = s.replace(/제(?=\d)/g, "").replace(/(?<=\d)차/g, "").replace(/(?<=\d)단지/g, "").replace(/구역$/, "");
  s = s.replace(/(?<=\d)[·,.∙](?=\d)/g, "_").replace(/[·,.~()[\]&?]/g, "");
  return s;
}

const $ = sel => document.querySelector(sel);
const fmt = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 1 });
const CACHE = {};
const REVIEW = { items: [], byPid: {}, byCid: {}, generated_at: "" };  // 검토 대기열(docs/data/review.json): 사업장별 · 단지(후보)별
let viewName = "projects";
let V = VIEWS.projects;
let DATA = [];
let sort = V.sort;
let openId = null;

init();

async function init() {
  const f = $("#filters");
  f.addEventListener("input", e => {
    if (e.target.name === "sido") fillSgg();
    render();
  });
  f.addEventListener("submit", e => e.preventDefault());
  $("#resetBtn").addEventListener("click", () => { f.reset(); fillSgg(); render(); });
  $("#downloadBtn").addEventListener("click", downloadCsv);
  $("#body").addEventListener("click", e => {
    if (e.target.closest("button.copy")) return copyRow(e);   // 노후 단지 상세의 '등록 행' 등
    const tr = e.target.closest("tr.item");
    if (!tr || e.target.closest("a")) return;
    openId = openId === tr.dataset.id ? null : tr.dataset.id;
    render(false);
  });
  document.querySelectorAll("#viewTabs button").forEach(b =>
    b.addEventListener("click", () => switchView(b.dataset.view, true)));
  $("#rvQ").addEventListener("input", renderReview);
  $("#rvConf").addEventListener("input", renderReview);
  $("#rvKind").addEventListener("input", renderReview);
  $("#rvBody").addEventListener("click", copyRow);
  await loadReview();
  const u = new URLSearchParams(location.search);
  const first = u.get("view");
  await switchView(VIEWS[first] || first === "review" ? first : "projects", false);
  readUrl(u);
  render();
}

async function load(name) {
  if (CACHE[name]) return CACHE[name];
  const res = await fetch(VIEWS[name].file, { cache: "no-cache" });
  const data = await res.json();
  const v = VIEWS[name];
  data.rows = data[v.list].map(p => ({
    ...p,
    stage: p.stage || (name === "apartments" ? NO_PROJECT : p.stage),
    stage_order: p.stage_order ?? (p.stage ? STAGES.findIndex(([s]) => s === p.stage) : -1),
    _keys: (p.search_keys || "").split(" ").filter(Boolean),
    _names: [p[v.name], ...(p.aliases || []), p.name_variants || "", p.sibling_names || ""].join(" ").toLowerCase(),
  }));
  return (CACHE[name] = data);
}

async function switchView(name, keepQuery) {
  const q = $("#filters").q.value;
  viewName = name;
  document.querySelectorAll("#viewTabs button").forEach(b => b.setAttribute("aria-pressed", b.dataset.view === name));
  $("#reviewPanel").hidden = name !== "review";
  $("#mainPanel").hidden = name === "review";
  if (name === "review") {  // 검토 대기열: 검색 표 대신 후보 목록
    if (keepQuery) history.replaceState(null, "", "?view=review");
    renderReview();
    return;
  }
  V = VIEWS[name];
  $("#body").replaceChildren();
  $("#count").textContent = "불러오는 중…";
  const data = await load(name);
  DATA = data.rows;
  sort = { ...V.sort };
  openId = null;
  $("#total").textContent = `${V.label} ${fmt.format(data.count)}곳`;
  $("#updated").textContent = data.generated_at;
  $("#viewNote").textContent = name === "apartments"
    ? `${data.old_before}년 이전(포함)에 지어진 수도권 아파트 단지입니다. 재건축 사업이 확인된 단지는 단계가 표시됩니다.`
    : "공공데이터로 확인된 수도권 아파트 재건축 사업장입니다.";
  buildControls();
  if (keepQuery) $("#filters").q.value = q;
  if (keepQuery) render();
}

function buildControls() {
  const f = $("#filters");
  f.reset();
  const sidos = [...new Set(DATA.map(p => p.region_sido).filter(Boolean))].sort();
  f.sido.replaceChildren(new Option("전체", ""), ...sidos.map(v => new Option(v, v)));
  fillSgg();

  const hasRv = viewName === "projects" ? Object.keys(REVIEW.byPid).length : viewName === "apartments" ? Object.keys(REVIEW.byCid).length : 0;
  $("#reviewOnly").hidden = !hasRv;
  $("#reviewOnly").lastChild.textContent = viewName === "apartments" ? " 신규 사업장 후보 있는 곳만" : " 검토 대기 있는 곳만";
  const counts = {};
  DATA.forEach(p => { counts[p.stage] = (counts[p.stage] || 0) + 1; });
  $("#stageChips").replaceChildren(...STAGES.filter(([s]) => counts[s]).map(([s]) => {
    const l = document.createElement("label");
    l.className = "chip";
    l.innerHTML = `<input type="checkbox" name="stage" value="${s}"> ${s} <small>${fmt.format(counts[s])}</small>`;
    return l;
  }));

  $("#rangeFilters").replaceChildren(...V.ranges.map(([key, label, unit]) => {
    const n = DATA.filter(p => p[key] != null).length;
    const d = document.createElement("div");
    d.className = "range";
    d.innerHTML = `<span>${label} (${unit}) <small>· 자료 ${fmt.format(n)}곳</small></span>
      <span class="pair"><input type="number" name="min_${key}" placeholder="최소" step="any" aria-label="${label} 최소">
      ~ <input type="number" name="max_${key}" placeholder="최대" step="any" aria-label="${label} 최대"></span>`;
    return d;
  }));

  $("#headRow").replaceChildren(...V.columns.map(([key, label, isNum]) => {
    const th = document.createElement("th");
    th.textContent = label;
    th.dataset.key = key;
    if (isNum) th.className = "num";
    th.tabIndex = 0;
    th.addEventListener("click", () => setSort(key));
    th.addEventListener("keydown", e => { if (e.key === "Enter") setSort(key); });
    return th;
  }));
}

function fillSgg() {
  const f = $("#filters");
  const prev = f.sgg.value;
  const list = [...new Set(DATA.filter(p => !f.sido.value || p.region_sido === f.sido.value)
    .map(p => p.region_sigungu).filter(Boolean))].sort();
  f.sgg.replaceChildren(new Option("전체", ""), ...list.map(v => new Option(v, v)));
  if (list.includes(prev)) f.sgg.value = prev;
}

function setSort(key) {
  sort = sort.key === key ? { key, dir: -sort.dir } : { key, dir: key === V.name ? 1 : -1 };
  render();
}

function criteria() {
  const f = $("#filters");
  const c = {
    q: f.q.value.trim(), sido: f.sido.value, sgg: f.sgg.value, addr: f.addr.value.trim(),
    stages: [...f.querySelectorAll("input[name=stage]:checked")].map(i => i.value), ranges: [],
    reviewOnly: f.review_only.checked && !$("#reviewOnly").hidden,
  };
  for (const [key] of V.ranges) {
    const lo = f[`min_${key}`].value, hi = f[`max_${key}`].value;
    if (lo !== "" || hi !== "") c.ranges.push([key, lo === "" ? -Infinity : +lo, hi === "" ? Infinity : +hi]);
  }
  return c;
}

function matches(p, c) {
  if (c.sido && p.region_sido !== c.sido) return false;
  if (c.sgg && p.region_sigungu !== c.sgg) return false;
  if (c.stages.length && !c.stages.includes(p.stage)) return false;
  if (c.reviewOnly && !reviewItems(p).length) return false;
  if (c.addr && !V.address(p).replace(/\s/g, "").includes(c.addr.replace(/\s/g, ""))) return false;
  for (const [key, lo, hi] of c.ranges) {
    const v = p[key];
    if (v == null || v < lo || v > hi) return false;
  }
  if (c.q) {
    p._score = nameScore(p, c.q);
    if (!p._score) return false;
  }
  return true;
}

// 이름 일치 정도: 정확히 같음 3 > 앞부분 일치 2 > 포함 1 > 검색어가 별칭을 품음 0.5 (긴 별칭만)
function nameScore(p, q) {
  const nq = normalize(q);
  let best = p._names.includes(q.toLowerCase()) ? 1 : 0;
  if (!nq) return best;
  const sortedQ = [...nq].sort().join(""), digitsQ = nq.replace(/\D/g, "");
  for (const k of p._keys) {
    if (k === nq) return 3;
    if (k.length === nq.length && k.replace(/\D/g, "") === digitsQ && [...k].sort().join("") === sortedQ) {
      best = Math.max(best, 2.5);  // 어순만 다름: 세종주공6 ↔ 주공6세종 (숫자 순서는 같아야: 신반포12 ≠ 신반포21)
    }
    if (k.startsWith(nq)) best = Math.max(best, 2);
    else if (k.includes(nq)) best = Math.max(best, 1);
    else if (k.length >= 3 && k.length >= nq.length * 0.6 && nq.includes(k)) best = Math.max(best, 0.5);
  }
  return best;
}

const PAGE = 300;  // 노후 단지는 수천 곳이라 화면에는 앞에서부터 나눠서 그린다
let shown = PAGE;

function render(updateUrl = true) {
  if (viewName === "review") return renderReview();
  const c = criteria();
  const rows = DATA.filter(p => matches(p, c));
  const { key, dir } = sort;
  const nm = V.name;
  rows.sort((a, b) => {
    if (c.q && a._score !== b._score) return b._score - a._score;  // 검색어가 있으면 잘 맞는 것부터
    const x = a[key], y = b[key];
    if (x == null && y == null) return String(a[nm]).localeCompare(String(b[nm]), "ko");
    if (x == null) return 1;
    if (y == null) return -1;
    return (typeof x === "number" ? x - y : String(x).localeCompare(String(y), "ko")) * dir
      || String(a[nm]).localeCompare(String(b[nm]), "ko");
  });
  if (updateUrl) shown = PAGE;
  document.querySelectorAll("#headRow th").forEach(th => {
    th.setAttribute("aria-sort", th.dataset.key === key ? (dir > 0 ? "ascending" : "descending") : "none");
  });
  $("#count").textContent = `${fmt.format(rows.length)}곳 / 전체 ${fmt.format(DATA.length)}곳`;
  $("#empty").hidden = rows.length > 0;
  const visible = rows.slice(0, shown);
  $("#body").replaceChildren(...visible.flatMap(p => {
    const tr = document.createElement("tr");
    tr.className = "item";
    tr.dataset.id = p[V.id];
    tr.innerHTML = V.columns.map(([k, , isNum]) => cell(p, k, isNum)).join("");
    return p[V.id] === openId ? [tr, V.detail(p)] : [tr];
  }));
  const more = $("#moreBtn");
  more.hidden = rows.length <= shown;
  more.textContent = `${fmt.format(Math.min(PAGE, rows.length - shown))}곳 더 보기 (남은 ${fmt.format(rows.length - shown)}곳)`;
  more.onclick = () => { shown += PAGE; render(false); };
  window.__lastRows = rows;
  if (updateUrl) writeUrl(c);
  if (openId != null) hydrateTrades();
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

function place(p) {
  return [(p.region_sido || "").replace(/특별시|광역시|도$/, ""), p.region_sigungu, viewName === "apartments" ? p.dong : ""]
    .filter(Boolean).join(" ");
}

// 이 행의 검토 대기 후보: 사업장 보기는 사업장별, 노후 단지 보기는 단지별(신규 사업장 후보)
function reviewItems(p) {
  return (viewName === "projects" ? REVIEW.byPid[p.project_id] : viewName === "apartments" ? REVIEW.byCid[p.complex_id] : null) || [];
}

function cell(p, k, isNum) {
  if (k === V.name) {
    const n = reviewItems(p).length;
    const rv = !n ? "" : viewName === "projects"
      ? ` <span class="rv" title="뉴스·고시에서 더 앞선 단계가 보임 — 검토 대기">검토 ${n}</span>`
      : ` <span class="rv" title="뉴스·고시에서 이 단지의 재건축 단계 표현이 보임 — 신규 사업장 후보(사람 확인 전)">후보 ${n}</span>`;
    return `<td class="name">${esc(p[k] || "")}${rv}<small>${esc(place(p))}</small></td>`;
  }
  if (k === "stage_order") {
    return p.stage ? `<td><span class="stage ${STAGE_CLASS[p.stage] || ""}">${esc(p.stage)}</span></td>` : `<td class="na">–</td>`;
  }
  const v = p[k];
  if (v == null) return `<td class="num na">–</td>`;
  if (MONEY.has(k)) return `<td class="num" title="${esc(p.trade_latest_date || "")} · 전용 ${esc(p.trade_latest_area || "")}㎡">${eok(v)}</td>`;
  if (PERCENT.has(k)) return `<td class="num" title="전용 ${esc(p.jeonse_area || "")}㎡ 전세 중위 ${eok(p.jeonse_median || 0)} ÷ 매매 중위">${v}%</td>`;
  return `<td class="num">${YEAR.has(k) ? v : fmt.format(v)}</td>`;
}

// 만원 → '34.0억' / '9,500만'
function eok(v) {
  return v >= 10000 ? `${(v / 10000).toFixed(1)}억` : `${fmt.format(v)}만`;
}

// ---------------------------------------------------------------- 거래 상세 (단지별 파일을 상세를 열 때만 읽는다)
const TRADE_CACHE = {};   // 파일 id → 자료
const TRADE_UI = {};      // 파일 id → 패널 상태(거래 종류·면적·기간·펼친 줄 수)
const TRADE_PAGE = 30;
const KIND_LABEL = { sales: "매매", rents: "전월세", silv: "입주권·분양권" };

function tradeFileId(p) {
  return viewName === "projects" ? `p-${p.project_id}` : String(p.complex_id).replace(":", "_");
}

function tradeSection(p) {
  if (!p.trades) return `<p class="aliases">국토교통부 실거래가에 이 단지의 매매·전월세 신고가 없습니다.</p>`;
  const fid = tradeFileId(p);
  const cached = TRADE_CACHE[fid];
  return `<section class="trades" data-file="${esc(fid)}">${cached ? "" : `<p class="aliases">거래 자료 불러오는 중…</p>`}</section>`;
}

async function hydrateTrades() {
  for (const el of document.querySelectorAll("section.trades")) {
    const fid = el.dataset.file;
    if (!TRADE_CACHE[fid]) {
      try {
        const res = await fetch(`data/trades/${encodeURIComponent(fid)}.json`, { cache: "no-cache" });
        if (!res.ok) throw new Error(String(res.status));
        TRADE_CACHE[fid] = await res.json();
      } catch (e) {
        el.innerHTML = `<p class="aliases">거래 자료를 불러오지 못했습니다 (${esc(e.message)}).</p>`;
        continue;
      }
    }
    if (el.isConnected) renderTrades(el, TRADE_CACHE[fid]);
  }
}

function rowsOf(section) {
  if (!section?.rows?.length) return [];
  return section.rows.map(r => Object.fromEntries(section.cols.map((c, i) => [c, r[i]])));
}

function byAreaTable(data) {
  if (!data.by_area?.length) return "";
  const hasRent = data.by_area.some(a => a.jeonse_n || a.wolse_n);
  const rows = data.by_area.map(a => `<tr><td>${a.area}㎡ <small>(${a.pyeong}평)</small></td>
    <td class="num">${a.n ?? "–"}</td><td class="num">${a.median ? eok(a.median) : "–"}</td>
    <td class="num">${a.min ? `${eok(a.min)} ~ ${eok(a.max)}` : "–"}</td>
    <td class="num">${a.latest_price ? `${eok(a.latest_price)} <small>${esc(a.latest_date)} · ${a.latest_floor}층</small>` : "–"}</td>
    ${hasRent ? `<td class="num">${a.jeonse_n || "–"}</td><td class="num">${a.jeonse_median ? eok(a.jeonse_median) : "–"}</td>
    <td class="num">${a.jeonse_ratio ? `${a.jeonse_ratio}%` : "–"}</td>
    <td class="num">${a.wolse_n ? `${a.wolse_n} <small>${eok(a.wolse_deposit_median)}/${fmt.format(a.wolse_monthly_median)}만</small>` : "–"}</td>` : ""}
    </tr>`).join("");
  const note = data.basis === "준공 후 신축" ? " — 준공 후 새 아파트의 거래"
    : data.basis === "지번 공통(동 미상)" ? " — 같은 지번의 다른 단지 거래가 섞여 있을 수 있음" : "";
  return `<h3 class="sub-h">전용면적별 실거래 (최근 12개월${note})</h3>
    <div class="mini-wrap"><table class="mini"><thead><tr><th>전용면적</th><th class="num">매매</th><th class="num">매매 중위</th>
    <th class="num">최저 ~ 최고</th><th class="num">최근 매매</th>${hasRent ? `<th class="num">전세</th><th class="num">전세 중위</th>
    <th class="num">전세가율</th><th class="num">월세 <small>보증금/월세 중위</small></th>` : ""}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderTrades(el, data) {
  const fid = el.dataset.file;
  const ui = (TRADE_UI[fid] ||= { kind: "sales", area: null, range: "5y", shown: TRADE_PAGE });
  const kinds = ["sales", "rents", "silv"].filter(k => data[k]?.rows?.length);
  if (!kinds.includes(ui.kind)) ui.kind = kinds[0];
  const all = ui.kind ? rowsOf(data[ui.kind]) : [];
  const counts = {};
  all.forEach(r => { const a = Math.round(r.area); counts[a] = (counts[a] || 0) + 1; });
  const areas = Object.keys(counts).map(Number).sort((a, b) => a - b);
  if (ui.area !== "all" && !counts[ui.area]) ui.area = areas.reduce((b, a) => (counts[a] > (counts[b] ?? -1) ? a : b), null);
  const rows = all.filter(r => ui.area === "all" || Math.round(r.area) === ui.area);
  const chartRows = ui.kind === "rents" ? rows.filter(r => r.monthly === 0) : rows;
  const valueOf = ui.kind === "rents" ? r => r.deposit : r => r.price;
  const tabs = kinds.map(k => `<button type="button" data-kind="${k}" aria-pressed="${k === ui.kind}">${KIND_LABEL[k]} <small>${fmt.format(data[k].rows.length)}</small></button>`).join("");
  const areaOpts = [`<option value="all"${ui.area === "all" ? " selected" : ""}>전체 면적 (${fmt.format(all.length)}건)</option>`,
    ...areas.map(a => `<option value="${a}"${ui.area === a ? " selected" : ""}>전용 ${a}㎡ · ${(a / 3.3058).toFixed(1)}평 (${fmt.format(counts[a])}건)</option>`)].join("");
  const ranges = [["5y", "최근 5년"], ["all", "전체"]].map(([r, l]) =>
    `<button type="button" data-range="${r}" aria-pressed="${r === ui.range}">${l}</button>`).join("");
  const sorted = [...rows].sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0));
  const visible = sorted.slice(0, ui.shown);
  el.innerHTML = `${byAreaTable(data)}
    <h3 class="sub-h">거래 이력</h3>
    <div class="trade-controls">
      <div class="tabs small" role="tablist">${tabs}</div>
      <select class="area" aria-label="전용면적">${areaOpts}</select>
      <div class="tabs small">${ranges}</div>
    </div>
    ${priceChart(chartRows, valueOf, ui.range, ui.kind)}
    ${tradeRows(ui.kind, visible)}
    ${sorted.length > ui.shown ? `<p class="more-wrap"><button type="button" class="more-trades">${fmt.format(Math.min(100, sorted.length - ui.shown))}건 더 보기 (남은 ${fmt.format(sorted.length - ui.shown)}건)</button></p>` : ""}`;
  el.querySelectorAll("button[data-kind]").forEach(b => b.addEventListener("click", () => {
    ui.kind = b.dataset.kind; ui.shown = TRADE_PAGE; renderTrades(el, data);
  }));
  el.querySelectorAll("button[data-range]").forEach(b => b.addEventListener("click", () => {
    ui.range = b.dataset.range; renderTrades(el, data);
  }));
  el.querySelector("select.area").addEventListener("change", e => {
    ui.area = e.target.value === "all" ? "all" : +e.target.value; ui.shown = TRADE_PAGE; renderTrades(el, data);
  });
  el.querySelector("button.more-trades")?.addEventListener("click", () => { ui.shown += 100; renderTrades(el, data); });
}

function tradeRows(kind, rows) {
  if (!rows.length) return `<p class="aliases">표시할 거래가 없습니다.</p>`;
  const td = (v, cls = "") => `<td${cls ? ` class="${cls}"` : ""}>${v == null || v === "" ? "–" : v}</td>`;
  let head, body;
  if (kind === "rents") {
    head = `<th>계약일</th><th>구분</th><th class="num">전용(㎡)</th><th class="num">층</th><th class="num">보증금</th><th class="num">월세</th><th>계약</th><th>기간</th>`;
    body = rows.map(r => `<tr>${td(esc(r.date))}${td(r.monthly === 0 ? "전세" : "월세")}${td(r.area, "num")}${td(r.floor, "num")}
      ${td(eok(r.deposit), "num")}${td(r.monthly ? `${fmt.format(r.monthly)}만` : "–", "num")}
      ${td(r.contract ? esc(r.contract) + (r.renewal ? " <small>갱신요구권</small>" : "") : null)}${td(r.term ? esc(r.term) : null)}</tr>`).join("");
  } else if (kind === "silv") {
    head = `<th>계약일</th><th>구분</th><th class="num">전용(㎡)</th><th class="num">층</th><th class="num">가격</th><th>거래</th>`;
    body = rows.map(r => `<tr>${td(esc(r.date))}${td(esc(r.kind))}${td(r.area, "num")}${td(r.floor, "num")}${td(eok(r.price), "num")}${td(r.type ? esc(r.type) : null)}</tr>`).join("");
  } else {
    const showBuilt = viewName === "projects" && new Set(rows.map(r => r.built)).size > 1;
    head = `<th>계약일</th><th class="num">전용(㎡)</th><th class="num">층</th><th class="num">가격</th><th>동</th><th>거래</th>${showBuilt ? "<th class=\"num\">건축년도</th>" : ""}`;
    const dong = d => !d ? null : /^\d+$/.test(d) ? `${d}동` : /동$/.test(d) ? esc(d) : `${esc(d)}동`;  // '101' / '제101' → 101동 / 제101동
    body = rows.map(r => `<tr>${td(esc(r.date))}${td(r.area, "num")}${td(r.floor, "num")}${td(eok(r.price), "num")}
      ${td(dong(r.dong))}${td(r.type ? esc(r.type) : null)}${showBuilt ? td(r.built, "num") : ""}</tr>`).join("");
  }
  return `<div class="mini-wrap"><table class="mini"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// 가격 추이: 거래 1건 = 점, 분기(또는 연)별 중위가 = 선. 외부 라이브러리 없이 SVG로 그린다.
function priceChart(rows, valueOf, range, kind) {
  const pts = rows.map(r => ({ t: Date.parse(r.date), v: valueOf(r), r })).filter(p => p.v > 0 && !isNaN(p.t));
  const now = Date.now();
  const t0 = range === "5y" ? now - 5 * 365.25 * 864e5 : (pts.length ? Math.min(...pts.map(p => p.t)) : now - 365.25 * 864e5);
  const shown = pts.filter(p => p.t >= t0);
  if (!shown.length) return `<p class="aliases">이 기간에 ${KIND_LABEL[kind] || ""} 거래가 없습니다.</p>`;
  const W = 760, H = 260, L = 64, R = 16, T = 14, B = 30;
  const t1 = Math.max(now, ...shown.map(p => p.t));
  const span = (t1 - t0) / (365.25 * 864e5);
  const vmax = Math.max(...shown.map(p => p.v)), vmin = Math.min(...shown.map(p => p.v));
  const pad = Math.max((vmax - vmin) * 0.12, vmax * 0.05, 1);
  const step = niceStep((vmax + pad - Math.max(0, vmin - pad)) / 5);
  const yBot = Math.max(0, Math.floor((vmin - pad) / step) * step), yTop = Math.ceil((vmax + pad) / step) * step;
  const x = t => L + (t - t0) / (t1 - t0) * (W - L - R);
  const y = v => T + (yTop - v) / (yTop - yBot) * (H - T - B);
  let grid = "";
  for (let v = yBot; v <= yTop + 1e-9; v += step) {
    grid += `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" class="grid"/>
      <text x="${L - 6}" y="${(y(v) + 4).toFixed(1)}" class="lbl" text-anchor="end">${eok(Math.round(v))}</text>`;
  }
  const y0 = new Date(t0).getFullYear(), y1 = new Date(t1).getFullYear();
  const every = span > 14 ? 3 : span > 7 ? 2 : 1;
  for (let yr = y0 + 1; yr <= y1; yr++) {
    if ((yr - y1) % every) continue;
    const t = Date.UTC(yr, 0, 1);
    if (t < t0) continue;
    grid += `<line x1="${x(t).toFixed(1)}" x2="${x(t).toFixed(1)}" y1="${T}" y2="${H - B}" class="grid"/>
      <text x="${x(t).toFixed(1)}" y="${H - B + 16}" class="lbl" text-anchor="middle">${yr}</text>`;
  }
  // 중위가 선: 기간이 6년을 넘으면 연도별, 아니면 분기별
  const key = span > 6 ? p => new Date(p.t).getUTCFullYear() : p => { const d = new Date(p.t); return `${d.getUTCFullYear()}Q${Math.floor(d.getUTCMonth() / 3)}`; };
  const groups = {};
  shown.forEach(p => { (groups[key(p)] ||= []).push(p); });
  const line = Object.values(groups).map(g => {
    const vs = g.map(p => p.v).sort((a, b) => a - b);
    const med = vs.length % 2 ? vs[(vs.length - 1) / 2] : (vs[vs.length / 2 - 1] + vs[vs.length / 2]) / 2;
    const tm = g.reduce((s, p) => s + p.t, 0) / g.length;
    return { t: tm, v: med, n: g.length };
  }).sort((a, b) => a.t - b.t);
  const path = line.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const dots = shown.map(p => `<circle cx="${x(p.t).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="2.6" class="dot"><title>${esc(p.r.date)} · 전용 ${p.r.area}㎡ · ${p.r.floor}층 · ${eok(p.v)}${p.r.kind ? ` · ${esc(p.r.kind)}` : ""}</title></circle>`).join("");
  const medDots = line.map(p => `<circle cx="${x(p.t).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="3.2" class="med-dot"><title>중위 ${eok(Math.round(p.v))} (${p.n}건)</title></circle>`).join("");
  const unit = kind === "rents" ? "전세 보증금" : "거래가";
  return `<figure class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${unit} 추이 차트">
    ${grid}<path d="${path}" class="med"/>${dots}${medDots}</svg>
    <figcaption>점 = 거래 1건(${fmt.format(shown.length)}건), 선 = ${span > 6 ? "연도" : "분기"}별 중위 ${unit}. 해제된 거래는 뺐습니다.</figcaption></figure>`;
}

function niceStep(raw) {
  const pow = 10 ** Math.floor(Math.log10(Math.max(raw, 1)));
  const f = raw / pow;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * pow;
}

function grid(p, fields) {
  return `<dl class="detail-grid">${fields.filter(([k]) => p[k] != null).map(([k, label]) =>
    `<div><dt>${label}</dt><dd>${esc(typeof p[k] === "number" && !YEAR.has(k) ? fmt.format(p[k]) : p[k])}</dd></div>`).join("")}</dl>`;
}

function wrapDetail(inner) {
  const tr = document.createElement("tr");
  tr.className = "detail";
  tr.innerHTML = `<td colspan="${V.columns.length}"><div class="detail-inner">${inner}</div></td>`;
  return tr;
}

function projectDetail(p) {
  const steps = DATE_FIELDS.map(([k, label]) =>
    `<span class="${p[k] ? "on" : ""}">${label} ${p[k] ? esc(p[k]) : "–"}</span>`).join("");
  const home = p.homepage ? `<p><a href="${esc(p.homepage)}" target="_blank" rel="noopener">사업장 홈페이지(정비사업 정보몽땅)</a></p>` : "";
  return wrapDetail(`
    <div class="timeline">${steps}</div>
    ${grid(p, [
      ["address", "위치"], ["project_type", "사업 유형"], ["operator_type", "시행 방식"],
      ["trade_count_all", "매매 이력(2006년~, 건)"], ["jeonse_ratio", "전세가율(%)"],
      ["silv_count", "입주권·분양권 거래(3년, 건)"], ["silv_latest_price", "최근 입주권·분양권가(만원)"],
      ["zoning_before", "용도지역(기존)"], ["zoning_after", "용도지역(변경)"],
      ["existing_buildings", "기존 동수"], ["buildings_planned", "계획 동수"],
      ["gfa_planned_m2", "계획 연면적(㎡)"], ["height_planned_m", "계획 높이(m)"],
      ["units_sale", "분양 세대"], ["units_rent", "임대 세대"],
      ["owners_count", "토지등소유자(명)"], ["members_count", "조합원(명)"],
      ["bld_name", "건축물대장 단지명"], ["land_area_m2", "대지면적(㎡)"], ["bld_households", "건축물대장 세대수"],
      ["land_share_m2", "평균 대지지분(㎡)"], ["bld_far", "기존 용적률(대장, %)"], ["bld_bcr", "기존 건폐율(대장, %)"],
      ["bld_approved", "사용승인일"], ["bld_buildings", "주거동 수"],
      ["stage_date", "현재 단계 일자"], ["stage_by_source", "출처별 단계"], ["source_updated_at", "출처 기준일"],
    ])}
    ${reviewBox(p)}
    <p class="aliases">현재 단계 근거: ${esc(p.stage_source || "–")}${p.stage_evidence ? ` · <a href="${esc(p.stage_evidence)}" target="_blank" rel="noopener">${(p.stage_source || "").startsWith("수동") ? "근거 링크(뉴스·고시)" : "정보몽땅 사업장 페이지"}</a>` : ""}${(p.sources || "").includes("manual_projects.csv") ? " · 공공 출처에 아직 없는 사업장을 근거와 함께 수동 등록(manual_projects.csv)한 것입니다" : ""}</p>
    ${tradeSection(p)}
    <p class="aliases">다른 이름: ${esc((p.aliases || []).slice(0, 30).join(" · "))}</p>
    ${home}
    <p class="aliases">출처: ${esc(p.sources || "")}</p>`);
}

function apartmentDetail(p) {
  const project = p.project_id
    ? `<p>재건축 사업: <a href="?view=projects&q=${encodeURIComponent(p.project_name || "")}">${esc(p.project_name || p.project_id)}</a>
       <span class="stage ${STAGE_CLASS[p.stage] || ""}">${esc(p.stage)}</span></p>`
    : `<p class="aliases">공공데이터에서 확인된 재건축 사업이 없습니다(추진 준비 단계이거나 아직 사업이 없음).
       근거(기사·고시)가 있으면 <a href="https://github.com/kyoungwhankim/Korea_Reconstruction/edit/main/manual_projects.csv" target="_blank" rel="noopener">manual_projects.csv</a>에
       <code>${esc(p.complex_id)},${esc(p.name)},단계,날짜,근거링크,메모</code> 한 줄을 적으면 다음 갱신 때 사업장으로 올라옵니다.</p>`;
  const lotNote = p.siblings > 1
    ? `<p class="aliases">같은 지번(${esc(p.jibun)})에 단지 ${p.siblings}개: ${esc(p.sibling_names || "")} — 대지면적·용적률은 지번 전체 값이고, 동 번호가 없는 거래 ${fmt.format(p.shared_trades || 0)}건은 단지를 가리지 못했습니다.</p>`
    : "";
  return wrapDetail(`
    ${project}
    ${reviewBox(p)}
    ${lotNote}
    ${grid(p, [
      ["jibun", "지번"], ["dong_range", "동"], ["road_address", "도로명 주소"], ["built_year", "준공연도"], ["age", "경과 연수"],
      ["households", "세대수"], ["buildings", "동수"], ["top_floor", "최고층"], ["heating", "난방"],
      ["builder", "시공사"], ["land_area_m2", "대지면적(㎡)"], ["land_share_m2", "평균 대지지분(㎡)"],
      ["far", "용적률(%)"], ["bcr", "건폐율(%)"], ["trade_basis", "거래 기준"],
      ["trade_count_all", "매매 이력(2006년~, 건)"], ["jeonse_ratio", "전세가율(%)"], ["sources", "출처"],
    ])}
    ${tradeSection(p)}
    <p class="aliases">다른 이름: ${esc((p.aliases || []).slice(0, 20).join(" · ") || p.name_variants || "")}</p>`);
}

function writeUrl(c) {
  const u = new URLSearchParams();
  if (viewName !== "projects") u.set("view", viewName);
  if (c.q) u.set("q", c.q);
  if (c.sido) u.set("sido", c.sido);
  if (c.sgg) u.set("sgg", c.sgg);
  if (c.addr) u.set("addr", c.addr);
  if (c.stages.length) u.set("stage", c.stages.join(","));
  if (c.reviewOnly) u.set("rv", "1");
  for (const [key, lo, hi] of c.ranges) {
    if (lo !== -Infinity) u.set(`min_${key}`, lo);
    if (hi !== Infinity) u.set(`max_${key}`, hi);
  }
  if (sort.key !== V.sort.key || sort.dir !== V.sort.dir) u.set("sort", `${sort.key}:${sort.dir}`);
  const qs = u.toString();
  history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

function readUrl(u) {
  const f = $("#filters");
  for (const name of ["q", "sido", "addr"]) if (u.has(name)) f[name].value = u.get(name);
  fillSgg();
  if (u.has("sgg")) f.sgg.value = u.get("sgg");
  const stages = (u.get("stage") || "").split(",");
  f.querySelectorAll("input[name=stage]").forEach(i => { i.checked = stages.includes(i.value); });
  f.review_only.checked = u.get("rv") === "1";
  let anyRange = false;
  for (const [key] of V.ranges) {
    for (const side of ["min", "max"]) {
      if (u.has(`${side}_${key}`)) { f[`${side}_${key}`].value = u.get(`${side}_${key}`); anyRange = true; }
    }
  }
  if (anyRange) $("#moreFilters").open = true;
  if (u.has("sort")) {
    const [key, dir] = u.get("sort").split(":");
    if (V.columns.some(([k]) => k === key)) sort = { key, dir: +dir || -1 };
  }
}

function downloadCsv() {
  const rows = window.__lastRows || [];
  const q = v => v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v);
  const csv = "﻿" + [V.csv.join(","), ...rows.map(p => V.csv.map(k => q(p[k])).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  a.download = `${V.label}_검색결과_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

// ---- 검토 대기열(docs/data/review.json): 뉴스·고시 제목에서 현재 단계보다 앞선 단계가 보인 사업장. 자동 반영하지 않는다.
async function loadReview() {
  try {
    const res = await fetch("data/review.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    REVIEW.items = data.items || [];
    REVIEW.generated_at = data.generated_at || "";
  } catch { REVIEW.items = []; }
  REVIEW.byPid = {};
  REVIEW.byCid = {};
  for (const it of REVIEW.items) {
    it.kind ||= "단계 변경";
    if (it.project_id) (REVIEW.byPid[it.project_id] ||= []).push(it);
    if (it.complex_id) (REVIEW.byCid[it.complex_id] ||= []).push(it);
  }
  $("#reviewCount").textContent = fmt.format(REVIEW.items.length);
}

// CSV 한 칸: 쉼표·따옴표·줄바꿈이 있으면 따옴표로 감싼다
function csvField(v) {
  const s = v == null ? "" : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

// 후보 1건의 복사 버튼: 사업장 후보는 manual_stage.csv 행, 신규 사업장 후보는 manual_projects.csv 행 + 기각 행
function copyButtons(it) {
  const btn = (label, row, title) => `<button type="button" class="copy" data-text="${esc(row)}" title="${esc(title)}">${label}</button>`;
  if (it.kind === NEW_KIND) {
    return btn("등록 행", [it.complex_id, it.zone_name, it.article_stage, it.published, it.link, ""].map(csvField).join(","),
      "manual_projects.csv에 붙여 넣을 행 복사 — 다음 갱신 때 이 단지가 사업장으로 올라옵니다(단계·날짜·이름은 기사에 맞게 고쳐도 됨)")
      + " " + btn("기각 행", [it.id, `cx:${it.complex_id}`, ""].map(csvField).join(","), "review_dismissed.csv에 붙여 넣을 행 복사");
  }
  return btn("반영 행", [it.project_id, it.article_stage, it.published, it.link, ""].map(csvField).join(","), "manual_stage.csv에 붙여 넣을 행 복사")
    + " " + btn("기각 행", [it.id, it.project_id, ""].map(csvField).join(","), "review_dismissed.csv에 붙여 넣을 행 복사");
}

function confClass(c) {
  return c === "확정" ? "conf conf-sure" : "conf conf-maybe";
}

function renderReview() {
  const q = $("#rvQ").value.trim().toLowerCase();
  const confs = [...document.querySelectorAll("#rvConf input:checked")].map(i => i.value);
  const kinds = [...document.querySelectorAll("#rvKind input:checked")].map(i => i.value);
  const rows = REVIEW.items.filter(it => (!confs.length || confs.includes(it.confidence)) && (!kinds.length || kinds.includes(it.kind))
    && (!q || `${it.zone_name} ${it.region_sigungu} ${it.dong || ""} ${it.title} ${it.article_stage} ${it.kind}`.toLowerCase().includes(q)));
  const newN = REVIEW.items.filter(it => it.kind === NEW_KIND).length;
  $("#rvCount").textContent = `${fmt.format(rows.length)}건 / 전체 ${fmt.format(REVIEW.items.length)}건`
    + (newN ? ` (신규 사업장 후보 ${fmt.format(newN)}건)` : "") + ` · 갱신 ${REVIEW.generated_at || "–"}`;
  $("#rvBody").replaceChildren(...rows.map(it => {
    const tr = document.createElement("tr");
    tr.innerHTML = reviewRow(it);
    return tr;
  }));
  $("#rvEmpty").hidden = rows.length > 0;
}

function reviewRow(it) {
  const isNew = it.kind === NEW_KIND;
  const link = isNew ? `?view=apartments&q=${encodeURIComponent(it.zone_name)}` : `?q=${encodeURIComponent(it.zone_name)}`;
  const tag = isNew ? ` <span class="kind-new" title="사업장이 없는 노후 단지 — 맞으면 '등록 행'으로 manual_projects.csv에 등록">신규 사업장 후보</span>` : "";
  const where = [place(it), it.dong].filter(Boolean).join(" ");
  const cur = isNew ? NO_PROJECT : (it.stage || "–");
  return `<td class="name"><a href="${link}">${esc(it.zone_name)}</a>${tag}<small>${esc(where)}</small></td>
    <td><span class="stage ${STAGE_CLASS[cur] || ""}">${esc(cur)}</span> → <span class="stage ${STAGE_CLASS[it.article_stage] || ""}">${esc(it.article_stage)}</span></td>
    <td><span class="${confClass(it.confidence)}">${esc(it.confidence)}</span></td>
    <td class="art"><a href="${esc(it.link)}" target="_blank" rel="noopener">${esc(it.title)}</a><small>${esc(it.source)} · 일치한 이름: ${esc(it.matched || "")}</small></td>
    <td>${esc(it.published)}</td>
    <td class="ops">${copyButtons(it)}</td>`;
}

async function copyRow(e) {
  const b = e.target.closest("button.copy");
  if (!b) return;
  try {
    await navigator.clipboard.writeText(b.dataset.text);
    const t = b.textContent;
    b.textContent = "복사됨";
    b.classList.add("done");
    setTimeout(() => { b.textContent = t; b.classList.remove("done"); }, 1500);
  } catch { window.prompt("아래 행을 복사하세요", b.dataset.text); }
}

function reviewBox(p) {
  const rv = reviewItems(p);
  if (!rv.length) return "";
  const isNew = viewName === "apartments";
  const head = isNew
    ? `<b>신규 사업장 후보 ${rv.length}건</b> — 뉴스·고시 제목에 이 단지 이름과 재건축 단계 표현이 함께 보였습니다(사람 확인 전, 자동 등록 안 함).
       기사가 맞으면 <b>등록 행</b>을 복사해 <a href="https://github.com/kyoungwhankim/Korea_Reconstruction/edit/main/manual_projects.csv" target="_blank" rel="noopener">manual_projects.csv</a>에 붙여 넣으세요.`
    : `<b>검토 대기 ${rv.length}건</b> — 뉴스·고시 제목에 현재 단계보다 앞선 단계가 보였습니다(사람 확인 전, 자동 반영 안 함).`;
  return `<div class="rv-box">${head}
    <a href="?view=review">검토 대기열 보기</a><ul>${rv.map(it => `<li><span class="stage ${STAGE_CLASS[it.article_stage] || ""}">${esc(it.article_stage)}</span>
    <span class="${confClass(it.confidence)}">${esc(it.confidence)}</span> <a href="${esc(it.link)}" target="_blank" rel="noopener">${esc(it.title)}</a>
    <small>${esc(it.published)} · ${esc(it.source)}</small>${isNew ? ` <span class="ops">${copyButtons(it)}</span>` : ""}</li>`).join("")}</ul></div>`;
}
