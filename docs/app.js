"use strict";

// 단계(표준) — 순서·색 구분. '사업 미지정'은 노후 단지 보기에서 아직 재건축 사업이 없는 단지
const STAGES = [
  ["예정구역·후보지", "s-early"], ["정비계획수립", "s-early"], ["정비구역지정", "s-early"], ["추진위구성", "s-early"],
  ["조합설립인가", "s-mid"], ["건축심의", "s-mid"], ["사업시행인가", "s-mid"],
  ["관리처분인가", "s-late"], ["착공", "s-late"], ["준공", "s-done"], ["사업 미지정", "s-none"],
];
const STAGE_CLASS = Object.fromEntries(STAGES);
const NO_PROJECT = "사업 미지정";

const DATE_FIELDS = [
  ["designated_at", "정비구역지정"], ["committee_at", "추진위구성"], ["union_approved_at", "조합설립인가"],
  ["arch_review_at", "건축심의"], ["plan_approved_at", "사업시행인가"], ["mgmt_approved_at", "관리처분인가"],
  ["relocation_started_at", "이주 시작"], ["started_at", "착공"], ["completed_at", "준공"],
];
const TRADE_RANGES = [
  ["trade_latest_price", "최근 거래가", "만원"],
  ["trade_median_ppp", "평당가(전용, 1년 중위)", "만원"],
  ["trade_count_12m", "1년 거래 건수", "건"],
];

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
      ["existing_built_year", "준공연도", true],
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
      "site_area_per_unit_m2", "land_share_m2", "existing_built_year", "trade_count_12m", "trade_latest_date",
      "trade_latest_price", "trade_median_ppp", "address", "zoning_before", "zoning_after", "name_variants"],
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
      ["trade_count_12m", "1년 거래", true],
    ],
    ranges: [
      ["built_year", "준공연도", "년"], ["age", "경과 연수", "년"], ["households", "세대수", "세대"],
      ["buildings", "동수", "동"], ["far", "용적률(건축물대장)", "%"], ["bcr", "건폐율(건축물대장)", "%"],
      ["land_area_m2", "대지면적", "㎡"], ["land_share_m2", "대지지분", "㎡"], ...TRADE_RANGES,
    ],
    detail: apartmentDetail,
    csv: ["complex_id", "region_sido", "region_sigungu", "dong", "jibun", "name", "stage", "project_name",
      "built_year", "age", "households", "buildings", "land_area_m2", "land_share_m2", "far", "bcr",
      "trade_count_12m", "trade_latest_date", "trade_latest_price", "trade_median_ppp", "name_variants", "sources"],
  },
};
const MONEY = new Set(["trade_latest_price"]);  // 만원 → '34.0억'
const YEAR = new Set(["existing_built_year", "built_year"]);

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
    const tr = e.target.closest("tr.item");
    if (!tr || e.target.closest("a")) return;
    openId = openId === tr.dataset.id ? null : tr.dataset.id;
    render(false);
  });
  document.querySelectorAll("#viewTabs button").forEach(b =>
    b.addEventListener("click", () => switchView(b.dataset.view, true)));
  const u = new URLSearchParams(location.search);
  await switchView(VIEWS[u.get("view")] ? u.get("view") : "projects", false);
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
    _names: [p[v.name], ...(p.aliases || []), p.name_variants || ""].join(" ").toLowerCase(),
  }));
  return (CACHE[name] = data);
}

async function switchView(name, keepQuery) {
  const q = $("#filters").q.value;
  viewName = name;
  V = VIEWS[name];
  document.querySelectorAll("#viewTabs button").forEach(b => b.setAttribute("aria-pressed", b.dataset.view === name));
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
  for (const k of p._keys) {
    if (k === nq) return 3;
    if (k.startsWith(nq)) best = Math.max(best, 2);
    else if (k.includes(nq)) best = Math.max(best, 1);
    else if (k.length >= 3 && k.length >= nq.length * 0.6 && nq.includes(k)) best = Math.max(best, 0.5);
  }
  return best;
}

const PAGE = 300;  // 노후 단지는 수천 곳이라 화면에는 앞에서부터 나눠서 그린다
let shown = PAGE;

function render(updateUrl = true) {
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
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

function place(p) {
  return [(p.region_sido || "").replace(/특별시|광역시|도$/, ""), p.region_sigungu, viewName === "apartments" ? p.dong : ""]
    .filter(Boolean).join(" ");
}

function cell(p, k, isNum) {
  if (k === V.name) return `<td class="name">${esc(p[k] || "")}<small>${esc(place(p))}</small></td>`;
  if (k === "stage_order") {
    return p.stage ? `<td><span class="stage ${STAGE_CLASS[p.stage] || ""}">${esc(p.stage)}</span></td>` : `<td class="na">–</td>`;
  }
  const v = p[k];
  if (v == null) return `<td class="num na">–</td>`;
  if (MONEY.has(k)) return `<td class="num" title="${esc(p.trade_latest_date || "")} · 전용 ${esc(p.trade_latest_area || "")}㎡">${eok(v)}</td>`;
  return `<td class="num">${YEAR.has(k) ? v : fmt.format(v)}</td>`;
}

// 만원 → '34.0억' / '9,500만'
function eok(v) {
  return v >= 10000 ? `${(v / 10000).toFixed(1)}억` : `${fmt.format(v)}만`;
}

function tradeTable(p) {
  if (!p.trades_by_area?.length) return "";
  const rows = p.trades_by_area.map(a => `<tr><td>${a.area}㎡ <small>(${a.pyeong}평)</small></td><td class="num">${a.n}</td>
    <td class="num">${eok(a.median)}</td><td class="num">${eok(a.min)} ~ ${eok(a.max)}</td>
    <td class="num">${eok(a.latest_price)} <small>${esc(a.latest_date)} · ${a.latest_floor}층</small></td></tr>`).join("");
  const note = p.trade_basis === "준공 후 신축" ? " — 준공 후 새 아파트의 거래" : "";
  return `<h3 class="sub-h">전용면적별 실거래 (최근 12개월${note})</h3>
    <div class="mini-wrap"><table class="mini"><thead><tr><th>전용면적</th><th class="num">건수</th><th class="num">중위가</th>
    <th class="num">최저 ~ 최고</th><th class="num">최근 거래</th></tr></thead><tbody>${rows}</tbody></table></div>`;
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
      ["zoning_before", "용도지역(기존)"], ["zoning_after", "용도지역(변경)"],
      ["existing_buildings", "기존 동수"], ["buildings_planned", "계획 동수"],
      ["gfa_planned_m2", "계획 연면적(㎡)"], ["height_planned_m", "계획 높이(m)"],
      ["units_sale", "분양 세대"], ["units_rent", "임대 세대"],
      ["owners_count", "토지등소유자(명)"], ["members_count", "조합원(명)"],
      ["bld_name", "건축물대장 단지명"], ["land_area_m2", "대지면적(㎡)"], ["bld_households", "건축물대장 세대수"],
      ["land_share_m2", "평균 대지지분(㎡)"], ["bld_far", "기존 용적률(대장, %)"], ["bld_bcr", "기존 건폐율(대장, %)"],
      ["bld_approved", "사용승인일"], ["bld_buildings", "주거동 수"],
      ["stage_by_source", "출처별 단계"], ["source_updated_at", "출처 기준일"],
    ])}
    ${tradeTable(p)}
    <p class="aliases">다른 이름: ${esc((p.aliases || []).slice(0, 30).join(" · "))}</p>
    ${home}
    <p class="aliases">출처: ${esc(p.sources || "")}</p>`);
}

function apartmentDetail(p) {
  const project = p.project_id
    ? `<p>재건축 사업: <a href="?view=projects&q=${encodeURIComponent(p.project_name || "")}">${esc(p.project_name || p.project_id)}</a>
       <span class="stage ${STAGE_CLASS[p.stage] || ""}">${esc(p.stage)}</span></p>`
    : `<p class="aliases">공공데이터에서 확인된 재건축 사업이 없습니다(추진 준비 단계이거나 아직 사업이 없음).</p>`;
  return wrapDetail(`
    ${project}
    ${grid(p, [
      ["jibun", "지번"], ["built_year", "준공연도"], ["age", "경과 연수"], ["households", "세대수"],
      ["buildings", "동수"], ["land_area_m2", "대지면적(㎡)"], ["land_share_m2", "평균 대지지분(㎡)"],
      ["far", "용적률(%)"], ["bcr", "건폐율(%)"], ["sources", "출처"],
    ])}
    ${tradeTable(p)}
    <p class="aliases">다른 이름: ${esc(p.name_variants || "")}</p>`);
}

function writeUrl(c) {
  const u = new URLSearchParams();
  if (viewName !== "projects") u.set("view", viewName);
  if (c.q) u.set("q", c.q);
  if (c.sido) u.set("sido", c.sido);
  if (c.sgg) u.set("sgg", c.sgg);
  if (c.addr) u.set("addr", c.addr);
  if (c.stages.length) u.set("stage", c.stages.join(","));
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
