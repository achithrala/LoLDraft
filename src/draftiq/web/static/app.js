// Vanilla JS, no build step. The server (draftiq.web.app) is always the source of
// truth: every mutating action re-fetches /api/draft/state and re-renders from
// scratch rather than maintaining separate client-side draft logic.

const API = "/api";
const ROLES = ["top", "jungle", "mid", "bottom", "support"];
const RANKS = [
  "all", "iron", "bronze", "silver", "gold", "gold_plus", "platinum", "platinum_plus",
  "emerald", "emerald_plus", "diamond", "diamond_plus", "master", "master_plus",
  "grandmaster", "challenger",
];

let state = null; // DraftStateResponse | null
let champions = []; // Champion[]

async function apiGet(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(await errorDetail(res));
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await errorDetail(res));
  return res.json();
}

async function errorDetail(res) {
  try {
    const body = await res.json();
    return body.detail || res.statusText;
  } catch {
    return res.statusText;
  }
}

function showError(message) {
  const el = document.getElementById("error-banner");
  el.textContent = message;
  el.hidden = false;
}

function clearError() {
  document.getElementById("error-banner").hidden = true;
}

function filledRoles(side) {
  if (!state) return new Set();
  return new Set(
    state.actions
      .filter((a) => a.action_type === "pick" && a.side === side)
      .map((a) => a.role)
  );
}

function takenChampionIds() {
  return new Set(state ? state.actions.map((a) => a.champion_id) : []);
}

// ---- status bar + board ----

function renderStatusBar() {
  const el = document.getElementById("status-bar");
  if (!state) {
    el.textContent = "No draft in progress.";
    return;
  }
  const base = `Mode: ${state.mode} | Rank: ${state.rank} | Provider: ${state.provider} | Patch: ${state.patch}`;
  el.textContent = state.is_complete
    ? `${base} | Draft complete.`
    : `${base} | Next: ${state.next_side} ${state.next_action}`;
}

function renderBoard() {
  const bansEl = document.getElementById("bans");
  const blueEl = document.getElementById("blue-picks");
  const redEl = document.getElementById("red-picks");
  bansEl.innerHTML = "";
  blueEl.innerHTML = "";
  redEl.innerHTML = "";
  if (!state) return;

  const bans = state.actions.filter((a) => a.action_type === "ban");
  const bansTitle = document.createElement("p");
  bansTitle.textContent = `Bans (${bans.length}):`;
  bansEl.appendChild(bansTitle);
  const bansList = document.createElement("ul");
  for (const a of bans) {
    const li = document.createElement("li");
    li.textContent = `${a.side}: ${a.champion_name}`;
    bansList.appendChild(li);
  }
  bansEl.appendChild(bansList);

  // Pick order (1-indexed, across both sides) is lost once picks are grouped by
  // role instead of the order they happened in -- look it up by identity so the
  // role-sorted view can still show it.
  const allPicks = state.actions.filter((a) => a.action_type === "pick");
  const pickNumber = new Map(allPicks.map((a, i) => [a, i + 1]));

  for (const [side, container] of [["blue", blueEl], ["red", redEl]]) {
    const picksByRole = Object.fromEntries(
      allPicks.filter((a) => a.side === side).map((a) => [a.role, a])
    );
    const ul = document.createElement("ul");
    for (const role of ROLES) {
      const li = document.createElement("li");
      const pick = picksByRole[role];
      li.textContent = pick
        ? `${role}: ${pick.champion_name} (pick ${pickNumber.get(pick)})`
        : `${role}: -`;
      const isNextSlot =
        !state.is_complete &&
        state.next_action === "pick" &&
        state.next_side === side &&
        !pick;
      if (isNextSlot) li.classList.add("next-turn");
      ul.appendChild(li);
    }
    container.appendChild(ul);
  }
}

// ---- champion picker ----

function renderChampionList() {
  const listEl = document.getElementById("champion-list");
  const query = document.getElementById("champion-search").value.trim().toLowerCase();
  listEl.innerHTML = "";
  const taken = takenChampionIds();
  const canAct = state && !state.is_complete;

  for (const c of champions) {
    if (query && !c.name.toLowerCase().includes(query) && !c.ddragon_id.toLowerCase().includes(query)) {
      continue;
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = c.name;
    btn.disabled = !canAct || taken.has(c.champion_id);
    btn.addEventListener("click", () => onChampionClick(c));
    listEl.appendChild(btn);
  }
}

async function onChampionClick(champ) {
  if (!state || state.is_complete) return;
  clearError();
  if (state.next_action === "ban") {
    try {
      state = await apiPost(`${API}/draft/ban`, { champion_id: champ.champion_id });
      afterMutation();
    } catch (e) {
      showError(e.message);
    }
  } else {
    openRoleSelector(champ);
  }
}

function openRoleSelector(champ) {
  const el = document.getElementById("role-selector");
  const buttonsEl = document.getElementById("role-buttons");
  buttonsEl.innerHTML = "";
  const open = ROLES.filter((r) => !filledRoles(state.next_side).has(r));
  for (const role of open) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = role;
    btn.addEventListener("click", async () => {
      clearError();
      try {
        state = await apiPost(`${API}/draft/pick`, { champion_id: champ.champion_id, role });
        el.hidden = true;
        afterMutation();
      } catch (e) {
        showError(e.message);
      }
    });
    buttonsEl.appendChild(btn);
  }
  el.hidden = false;
}

// ---- suggestions ----

function renderSuggestControls() {
  const roleSelect = document.getElementById("suggest-role");
  const lookaheadToggle = document.getElementById("lookahead-toggle");
  const anyRoleToggle = document.getElementById("any-role-toggle");

  const disableAll = !state || state.is_complete || state.next_action === "ban";
  lookaheadToggle.disabled = disableAll;
  anyRoleToggle.disabled = disableAll;

  const open = disableAll ? [] : ROLES.filter((r) => !filledRoles(state.next_side).has(r));
  const previous = roleSelect.value;
  roleSelect.innerHTML = open.map((r) => `<option value="${r}">${r}</option>`).join("");
  if (open.includes(previous)) roleSelect.value = previous;
  roleSelect.disabled = disableAll || anyRoleToggle.checked;
}

async function refreshSuggestions() {
  const tbody = document.querySelector("#suggestions-table tbody");
  const thead = document.querySelector("#suggestions-table thead");
  tbody.innerHTML = "";
  thead.innerHTML = "";
  if (!state || state.is_complete) return;

  const lookahead = document.getElementById("lookahead-toggle").checked;
  const anyRole = document.getElementById("any-role-toggle").checked;
  const roleSelect = document.getElementById("suggest-role");

  const params = new URLSearchParams({ top: "10" });
  if (lookahead) params.set("lookahead", "true");
  if (anyRole) params.set("any_role", "true");
  if (state.next_action === "pick" && !anyRole) {
    if (!roleSelect.value) return; // no unfilled role selected yet
    params.set("role", roleSelect.value);
  }

  try {
    const recs = await apiGet(`${API}/draft/suggest?${params.toString()}`);
    renderSuggestions(recs, anyRole);
  } catch (e) {
    showError(e.message);
  }
}

function renderSuggestions(recs, showRole) {
  const thead = document.querySelector("#suggestions-table thead");
  const tbody = document.querySelector("#suggestions-table tbody");
  thead.innerHTML = `<tr><th>#</th><th>Champion</th>${showRole ? "<th>Role</th>" : ""}<th>Score</th><th>90% CI</th><th>n games</th><th>Breakdown</th></tr>`;
  tbody.innerHTML = "";
  recs.forEach((rec, i) => {
    const tr = document.createElement("tr");
    const breakdown = rec.terms
      .map((t) => `${t.label}: ${t.value >= 0 ? "+" : ""}${t.value.toFixed(4)}`)
      .join(", ");
    tr.innerHTML =
      `<td>${i + 1}</td><td>${rec.champion_name}</td>` +
      (showRole ? `<td>${rec.role}</td>` : "") +
      `<td>${rec.total_score.toFixed(4)}</td>` +
      `<td>[${rec.ci_low.toFixed(3)}, ${rec.ci_high.toFixed(3)}]</td>` +
      `<td>${rec.n_games}</td><td>${breakdown}</td>`;
    tbody.appendChild(tr);
  });
}

// ---- build panel ----

function renderBuildSelectors() {
  const champSelect = document.getElementById("build-champion");
  const oppSelect = document.getElementById("build-opponent");
  const roleSelect = document.getElementById("build-role");

  const champOptions = champions
    .map((c) => `<option value="${c.champion_id}">${c.name}</option>`)
    .join("");
  champSelect.innerHTML = `<option value="">-- champion --</option>${champOptions}`;
  oppSelect.innerHTML = `<option value="">(no opponent)</option>${champOptions}`;
  roleSelect.innerHTML = ROLES.map((r) => `<option value="${r}">${r}</option>`).join("");
}

async function onShowBuild() {
  clearError();
  const champId = document.getElementById("build-champion").value;
  const role = document.getElementById("build-role").value;
  const oppId = document.getElementById("build-opponent").value;
  if (!champId || !role) {
    showError("Choose a champion and role first.");
    return;
  }
  const params = new URLSearchParams({ champion_id: champId, role });
  if (oppId) params.set("opponent_id", oppId);
  try {
    const build = await apiGet(`${API}/draft/build?${params.toString()}`);
    renderBuild(build);
  } catch (e) {
    showError(e.message);
    document.getElementById("build-result").innerHTML = "";
  }
}

function renderBuild(build) {
  const el = document.getElementById("build-result");
  const rows = [
    ["Starting", build.starting_items],
    ["Items", build.items],
    ["Primary runes", build.runes_primary],
    ["Secondary runes", build.runes_secondary],
    ["Shards", build.rune_shards],
    ["Summoners", build.summoner_spells],
  ];
  const parts = [`<p><strong>${build.role}</strong> (patch ${build.patch})</p>`];
  for (const [label, values] of rows) {
    if (values && values.length) parts.push(`<p>${label}: ${values.join(", ")}</p>`);
  }
  if (build.skill_order && build.skill_order.length) {
    parts.push(`<p>Skill order: ${build.skill_order.join(" &gt; ")}</p>`);
  }
  el.innerHTML = parts.join("");
}

// ---- refresh / init ----

function afterMutation() {
  renderStatusBar();
  renderBoard();
  renderChampionList();
  renderSuggestControls();
  refreshSuggestions();
}

async function refreshChampions() {
  try {
    champions = await apiGet(`${API}/champions`);
  } catch {
    champions = [];
  }
  renderChampionList();
  renderBuildSelectors();
}

async function refreshState() {
  try {
    state = await apiGet(`${API}/draft/state`);
  } catch {
    state = null;
  }
  renderStatusBar();
  renderBoard();
  renderSuggestControls();
  if (state) {
    await refreshChampions();
    await refreshSuggestions();
  } else {
    renderChampionList();
  }
}

function init() {
  document.getElementById("rank").innerHTML = RANKS.map(
    (r) => `<option value="${r}"${r === "all" ? " selected" : ""}>${r}</option>`
  ).join("");

  document.getElementById("new-draft-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clearError();
    if (state && !state.is_complete && !confirm("A draft is already in progress. Start a new one anyway?")) {
      return;
    }
    const mode = document.getElementById("mode").value;
    const rank = document.getElementById("rank").value;
    const provider = document.getElementById("provider").value;
    try {
      state = await apiPost(`${API}/draft/new`, { mode, rank, provider });
      await refreshChampions();
      afterMutation();
    } catch (err) {
      showError(err.message);
    }
  });

  document.getElementById("champion-search").addEventListener("input", renderChampionList);
  document.getElementById("lookahead-toggle").addEventListener("change", (e) => {
    if (e.target.checked) document.getElementById("any-role-toggle").checked = false;
    renderSuggestControls();
    refreshSuggestions();
  });
  document.getElementById("any-role-toggle").addEventListener("change", (e) => {
    if (e.target.checked) document.getElementById("lookahead-toggle").checked = false;
    renderSuggestControls();
    refreshSuggestions();
  });
  document.getElementById("suggest-role").addEventListener("change", refreshSuggestions);
  document.getElementById("show-build").addEventListener("click", onShowBuild);

  refreshState();
}

document.addEventListener("DOMContentLoaded", init);
