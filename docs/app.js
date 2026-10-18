"use strict";

// 단계(표준) — 순서·색 구분
const STAGES = [
  ["예정구역·후보지", "s-early"], ["정비계획수립", "s-early"], ["정비구역지정", "s-early"], ["추진위구성", "s-early"],
  ["조합설립인가", "s-mid"], ["건축심의", "s-mid"], ["사업시행인가", "s-mid"],
  ["관리처분인가", "s-late"], ["착공", "s-late"], ["준공", "s-done"],
];
const STAGE_CLASS = Object.fromEntries(STAGES);

// 숫자 조건 필터: [필드, 라벨, 단위]
const RANGES = [
  ["far_existing", "기존 용적률", "%"],
  ["far_planned", "계획 용적률", "%"],
  ["bcr_planned", "계획 건폐율", "%"],
  ["existing_units", "기존 세대수", "세대"],
  ["planned_units", "계획 세대수", "세대"],
  ["site_area_m2", "구역면적", "㎡"],
  ["site_area_per_unit_m2", "세대당 구역면적", "㎡"],
  ["existing_built_year", "기존 준공연도", "년"],
  ["floors_planned", "계획 최고층", "층"],
  ["height_planned_m", "계획 높이", "m"],
  ["owners_count", "토지등소유자", "명"],
];

// 표 열: [필드, 제목, 숫자 여부]
const COLUMNS = [
  ["zone_name", "단지(구역)", false],
  ["stage_order", "현재 단계", false],
  ["existing_units", "기존 세대", true],
  ["planned_units", "계획 세대", true],
  ["far_existing", "기존 용적률", true],
  ["far_planned", "계획 용적률", true],
  ["bcr_planned", "계획 건폐율", true],
  ["site_area_m2", "구역면적(㎡)", true],
  ["site_area_per_unit_m2", "세대당 구역면적(㎡)", true],
  ["existing_built_year", "준공연도", true],
  ["floors_planned", "계획 층수", true],
];

const DATE_FIELDS = [
  ["designated_at", "정비구역지정"], ["committee_at", "추진위구성"], ["union_approved_at", "조합설립인가"],
  ["arch_review_at", "건축심의"], ["plan_approved_at", "사업시행인가"], ["mgmt_approved_at", "관리처분인가"],
  ["relocation_started_at", "이주 시작"], ["started_at", "착공"], ["completed_at", "준공"],
];
const DETAIL_FIELDS = [
  ["address", "위치"], ["project_type", "사업 유형"], ["operator_type", "시행 방식"],
  ["zoning_before", "용도지역(기존)"], ["zoning_after", "용도지역(변경)"],
  ["existing_buildings", "기존 동수"], ["buildings_planned", "계획 동수"],
  ["gfa_planned_m2", "계획 연면적(㎡)"], ["height_planned_m", "계획 높이(m)"],
  ["units_sale", "분양 세대"], ["units_rent", "임대 세대"],
  ["owners_count", "토지등소유자(명)"], ["members_count", "조합원(명)"],
  ["stage_by_source", "출처별 단계"], ["source_updated_at", "출처 기준일"],
];

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
let DATA = [];
let sort = { key: "stage_order", dir: -1 };
let openId = null;

init();

async function init() {
  const res = await fetch("data/projects.json", { cache: "no-cache" });
  const data = await res.json();
  DATA = data.projects.map(p => ({
    ...p,
    _keys: (p.search_keys || "").split(" ").filter(Boolean),
    _names: [p.zone_name, ...(p.aliases || [])].join(" ").toLowerCase(),
  }));
  $("#total").textContent = fmt.format(data.count);
  $("#updated").textContent = data.generated_at;
  buildControls();
  readUrl();
  render();
}

function buildControls() {
  const f = $("#filters");
  const sidos = [...new Set(DATA.map(p => p.region_sido))].sort();
  f.sido.append(...sidos.map(v => new Option(v, v)));
  fillSgg();

  const counts = {};
  DATA.forEach(p => { counts[p.stage] = (counts[p.stage] || 0) + 1; });
  $("#stageChips").append(...STAGES.filter(([s]) => counts[s]).map(([s]) => {
    const l = document.createElement("label");
    l.className = "chip";
    l.innerHTML = `<input type="checkbox" name="stage" value="${s}"> ${s} <small>${counts[s]}</small>`;
    return l;
  }));

  $("#rangeFilters").append(...RANGES.map(([key, label, unit]) => {
    const n = DATA.filter(p => p[key] != null).length;
    const d = document.createElement("div");
    d.className = "range";
    d.innerHTML = `<span>${label} (${unit}) <small>· 자료 ${n}곳</small></span>
      <span class="pair"><input type="number" name="min_${key}" placeholder="최소" step="any" aria-label="${label} 최소">
      ~ <input type="number" name="max_${key}" placeholder="최대" step="any" aria-label="${label} 최대"></span>`;
    return d;
  }));

  $("#headRow").append(...COLUMNS.map(([key, label, isNum]) => {
    const th = document.createElement("th");
    th.textContent = label;
    th.dataset.key = key;
    if (isNum) th.className = "num";
    th.tabIndex = 0;
    th.addEventListener("click", () => setSort(key));
    th.addEventListener("keydown", e => { if (e.key === "Enter") setSort(key); });
    return th;
  }));

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
}

function fillSgg() {
  const f = $("#filters");
  const prev = f.sgg.value;
  const list = [...new Set(DATA.filter(p => !f.sido.value || p.region_sido === f.sido.value)
    .map(p => p.region_sigungu))].sort();
  f.sgg.replaceChildren(new Option("전체", ""), ...list.map(v => new Option(v, v)));
  if (list.includes(prev)) f.sgg.value = prev;
}

function setSort(key) {
  sort = sort.key === key ? { key, dir: -sort.dir } : { key, dir: key === "zone_name" ? 1 : -1 };
  render();
}

function criteria() {
  const f = $("#filters");
  const c = {
    q: f.q.value.trim(), sido: f.sido.value, sgg: f.sgg.value, addr: f.addr.value.trim(),
    stages: [...f.querySelectorAll("input[name=stage]:checked")].map(i => i.value), ranges: [],
  };
  for (const [key] of RANGES) {
    const lo = f[`min_${key}`].value, hi = f[`max_${key}`].value;
    if (lo !== "" || hi !== "") c.ranges.push([key, lo === "" ? -Infinity : +lo, hi === "" ? Infinity : +hi]);
  }
  return c;
}

function matches(p, c) {
  if (c.sido && p.region_sido !== c.sido) return false;
  if (c.sgg && p.region_sigungu !== c.sgg) return false;
  if (c.stages.length && !c.stages.includes(p.stage)) return false;
  if (c.addr && !(p.address || "").replace(/\s/g, "").includes(c.addr.replace(/\s/g, ""))) return false;
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

function render(updateUrl = true) {
  const c = criteria();
  const rows = DATA.filter(p => matches(p, c));
  const { key, dir } = sort;
  rows.sort((a, b) => {
    if (c.q && a._score !== b._score) return b._score - a._score;  // 검색어가 있으면 잘 맞는 것부터
    const x = a[key], y = b[key];
    if (x == null && y == null) return a.zone_name.localeCompare(b.zone_name, "ko");
    if (x == null) return 1;
    if (y == null) return -1;
    return (typeof x === "number" ? x - y : String(x).localeCompare(String(y), "ko")) * dir
      || a.zone_name.localeCompare(b.zone_name, "ko");
  });
  document.querySelectorAll("#headRow th").forEach(th => {
    th.setAttribute("aria-sort", th.dataset.key === key ? (dir > 0 ? "ascending" : "descending") : "none");
  });
  $("#count").textContent = `${fmt.format(rows.length)}곳 / 전체 ${fmt.format(DATA.length)}곳`;
  $("#empty").hidden = rows.length > 0;
  $("#body").replaceChildren(...rows.flatMap(p => {
    const tr = document.createElement("tr");
    tr.className = "item";
    tr.dataset.id = p.project_id;
    tr.innerHTML = COLUMNS.map(([k, , isNum]) => cell(p, k, isNum)).join("");
    return p.project_id === openId ? [tr, detailRow(p)] : [tr];
  }));
  window.__lastRows = rows;
  if (updateUrl) writeUrl(c);
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

function cell(p, k, isNum) {
  if (k === "zone_name") {
    return `<td class="name">${esc(p.zone_name)}<small>${esc(p.region_sido.replace(/특별시|광역시/, ""))} ${esc(p.region_sigungu)}</small></td>`;
  }
  if (k === "stage_order") {
    return p.stage ? `<td><span class="stage ${STAGE_CLASS[p.stage] || ""}">${esc(p.stage)}</span></td>` : `<td class="na">–</td>`;
  }
  const v = p[k];
  if (v == null) return `<td class="num na">–</td>`;
  return `<td class="num">${k === "existing_built_year" ? v : fmt.format(v)}</td>`;
}

function detailRow(p) {
  const tr = document.createElement("tr");
  tr.className = "detail";
  const steps = DATE_FIELDS.map(([k, label]) =>
    `<span class="${p[k] ? "on" : ""}">${label} ${p[k] ? esc(p[k]) : "–"}</span>`).join("");
  const items = DETAIL_FIELDS.filter(([k]) => p[k] != null).map(([k, label]) =>
    `<div><dt>${label}</dt><dd>${esc(typeof p[k] === "number" ? fmt.format(p[k]) : p[k])}</dd></div>`).join("");
  const home = p.homepage ? `<p><a href="${esc(p.homepage)}" target="_blank" rel="noopener">사업장 홈페이지(정비사업 정보몽땅)</a></p>` : "";
  tr.innerHTML = `<td colspan="${COLUMNS.length}"><div class="detail-inner">
    <div class="timeline">${steps}</div>
    <dl class="detail-grid">${items}</dl>
    <p class="aliases">다른 이름: ${esc((p.aliases || []).slice(0, 30).join(" · "))}</p>
    ${home}
    <p class="aliases">출처: ${esc(p.sources || "")}</p>
  </div></td>`;
  return tr;
}

function writeUrl(c) {
  const u = new URLSearchParams();
  if (c.q) u.set("q", c.q);
  if (c.sido) u.set("sido", c.sido);
  if (c.sgg) u.set("sgg", c.sgg);
  if (c.addr) u.set("addr", c.addr);
  if (c.stages.length) u.set("stage", c.stages.join(","));
  for (const [key, lo, hi] of c.ranges) {
    if (lo !== -Infinity) u.set(`min_${key}`, lo);
    if (hi !== Infinity) u.set(`max_${key}`, hi);
  }
  if (sort.key !== "stage_order" || sort.dir !== -1) u.set("sort", `${sort.key}:${sort.dir}`);
  const qs = u.toString();
  history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

function readUrl() {
  const u = new URLSearchParams(location.search);
  const f = $("#filters");
  for (const name of ["q", "sido", "addr"]) if (u.has(name)) f[name].value = u.get(name);
  fillSgg();
  if (u.has("sgg")) f.sgg.value = u.get("sgg");
  const stages = (u.get("stage") || "").split(",");
  f.querySelectorAll("input[name=stage]").forEach(i => { i.checked = stages.includes(i.value); });
  let anyRange = false;
  for (const [key] of RANGES) {
    for (const side of ["min", "max"]) {
      if (u.has(`${side}_${key}`)) { f[`${side}_${key}`].value = u.get(`${side}_${key}`); anyRange = true; }
    }
  }
  if (anyRange) $("#moreFilters").open = true;
  if (u.has("sort")) {
    const [key, dir] = u.get("sort").split(":");
    if (COLUMNS.some(([k]) => k === key)) sort = { key, dir: +dir || -1 };
  }
}

function downloadCsv() {
  const rows = window.__lastRows || [];
  const fields = ["project_id", "region_sido", "region_sigungu", "zone_name", "stage",
    ...DATE_FIELDS.map(([k]) => k), "existing_units", "planned_units", ...RANGES.map(([k]) => k)
      .filter(k => !["existing_units", "planned_units"].includes(k)),
    "address", "zoning_before", "zoning_after", "name_variants"];
  const q = v => v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v);
  const csv = "﻿" + [fields.join(","), ...rows.map(p => fields.map(k => q(p[k])).join(","))].join("\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  a.download = `재건축_검색결과_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}
