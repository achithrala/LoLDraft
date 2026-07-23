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

async function apiGet(path, signal) {
  const res = await fetch(path, { signal });
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
  const poolToggle = document.getElementById("pool-toggle");

  const inactive = !state || state.is_complete;
  const isBan = !inactive && state.next_action === "ban";
  // Lookahead/any-role/role-select are picks-only. Pool is meaningful during a
  // ban too now (a bonus/highlight for enemy-roster champions, not a
  // restriction -- see search/ban.py), so it only disables on no-draft/complete.
  const disablePickOnly = inactive || isBan;
  lookaheadToggle.disabled = disablePickOnly;
  anyRoleToggle.disabled = disablePickOnly;
  poolToggle.disabled = inactive;

  const open = disablePickOnly ? [] : ROLES.filter((r) => !filledRoles(state.next_side).has(r));
  const previous = roleSelect.value;
  roleSelect.innerHTML = open.map((r) => `<option value="${r}">${r}</option>`).join("");
  if (open.includes(previous)) roleSelect.value = previous;
  roleSelect.disabled = disablePickOnly || anyRoleToggle.checked;
}

// A role switch (or any other control change) fires a brand new fetch without
// waiting for the previous one -- and against a network-bound provider like
// OP.GG, an older request can easily resolve *after* a newer one. Without
// ordering, that stale response silently overwrites the table with the wrong
// role's results (e.g. top-lane suggestions still showing after switching to
// jungle). `suggestRequestId` tags every call so a response only renders if
// it's still the most recent one in flight; aborting the previous fetch is a
// courtesy (frees up the connection for the new request) but isn't what makes
// this correct -- the id check is.
let suggestRequestId = 0;
let suggestAbortController = null;

async function refreshSuggestions() {
  const tbody = document.querySelector("#suggestions-table tbody");
  const thead = document.querySelector("#suggestions-table thead");
  if (!state || state.is_complete) {
    tbody.innerHTML = "";
    thead.innerHTML = "";
    return;
  }

  const lookahead = document.getElementById("lookahead-toggle").checked;
  const anyRole = document.getElementById("any-role-toggle").checked;
  const pool = document.getElementById("pool-toggle").checked;
  const roleSelect = document.getElementById("suggest-role");

  const params = new URLSearchParams({ top: "10" });
  if (lookahead) params.set("lookahead", "true");
  if (anyRole) params.set("any_role", "true");
  if (pool) params.set("pool", "true");
  if (state.next_action === "pick" && !anyRole) {
    if (!roleSelect.value) {
      tbody.innerHTML = "";
      thead.innerHTML = "";
      return; // no unfilled role selected yet
    }
    params.set("role", roleSelect.value);
  }

  if (suggestAbortController) suggestAbortController.abort();
  const controller = new AbortController();
  suggestAbortController = controller;
  const requestId = ++suggestRequestId;

  thead.innerHTML = "";
  tbody.innerHTML = `<tr><td colspan="7">Loading suggestions${state.provider === "opgg" ? " from OP.GG (can take up to a minute on a fresh role/rank)" : ""}...</td></tr>`;

  try {
    const recs = await apiGet(`${API}/draft/suggest?${params.toString()}`, controller.signal);
    if (requestId !== suggestRequestId) return; // a newer request has since superseded this one
    renderSuggestions(recs, anyRole);
  } catch (e) {
    if (e.name === "AbortError" || requestId !== suggestRequestId) return;
    tbody.innerHTML = "";
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
    // "in enemy pool" is a bonus/highlight term added by search/ban.py, not a
    // restriction (the full ban list is always shown) -- flag it visually so it
    // doesn't get lost in the Breakdown column text.
    const inEnemyPool = rec.terms.some((t) => t.label === "in enemy pool");
    const nameCell = inEnemyPool
      ? `${rec.champion_name}<span class="pool-badge">enemy pool</span>`
      : rec.champion_name;
    tr.innerHTML =
      `<td>${i + 1}</td><td>${nameCell}</td>` +
      (showRole ? `<td>${rec.role}</td>` : "") +
      `<td>${rec.total_score.toFixed(4)}</td>` +
      `<td>[${rec.ci_low.toFixed(3)}, ${rec.ci_high.toFixed(3)}]</td>` +
      `<td>${rec.n_games}</td><td>${breakdown}</td>`;
    tbody.appendChild(tr);
  });
}

// ---- roster panel ----

let roster = { ally: [], enemy: [] };

function renderRoster() {
  for (const side of ["ally", "enemy"]) {
    const listEl = document.getElementById(`roster-${side}-list`);
    listEl.innerHTML = "";
    for (const player of roster[side]) {
      const li = document.createElement("li");
      li.textContent = `${player} `;
      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.textContent = "×";
      removeBtn.addEventListener("click", async () => {
        clearError();
        try {
          roster = await apiPost(`${API}/roster/remove`, { side, player });
          renderRoster();
        } catch (e) {
          showError(e.message);
        }
      });
      li.appendChild(removeBtn);
      listEl.appendChild(li);
    }
  }
}

async function refreshRoster() {
  if (!state) {
    roster = { ally: [], enemy: [] };
    renderRoster();
    return;
  }
  try {
    roster = await apiGet(`${API}/roster`);
  } catch {
    roster = { ally: [], enemy: [] };
  }
  renderRoster();
}

function wireRosterForm(side) {
  document.getElementById(`roster-${side}-form`).addEventListener("submit", async (e) => {
    e.preventDefault();
    clearError();
    const input = e.target.querySelector("input");
    const player = input.value.trim();
    if (!player) return;
    try {
      roster = await apiPost(`${API}/roster/add`, { side, player });
      input.value = "";
      renderRoster();
    } catch (err) {
      showError(err.message);
    }
  });
}

// ---- champion pool panel ----

let poolRegistry = {}; // { player: { by_role: { role: [names] } } }
let poolChampions = []; // Champion[], from /api/pool/champions (no active-draft requirement)

function renderPoolSelectors() {
  const roleOptions = ROLES.map((r) => `<option value="${r}">${r}</option>`).join("");
  document.getElementById("pool-role").innerHTML = roleOptions;
  document.getElementById("import-role").innerHTML = roleOptions;
  document.getElementById("pool-champion").innerHTML = poolChampions
    .map((c) => `<option value="${c.champion_id}">${c.name}</option>`)
    .join("");
}

async function refreshPoolChampions() {
  try {
    poolChampions = await apiGet(`${API}/pool/champions`);
  } catch {
    poolChampions = [];
  }
  renderPoolSelectors();
}

function renderPoolList() {
  const el = document.getElementById("pool-list");
  el.innerHTML = "";
  const players = Object.keys(poolRegistry).sort();
  if (!players.length) {
    el.innerHTML = '<p class="hint">No pools defined yet.</p>';
    return;
  }
  for (const player of players) {
    const pool = poolRegistry[player];
    const div = document.createElement("div");
    div.className = "pool-entry";
    const title = document.createElement("strong");
    title.textContent = player;
    div.appendChild(title);
    for (const role of ROLES) {
      const names = (pool.by_role && pool.by_role[role]) || [];
      if (!names.length) continue;
      const row = document.createElement("div");
      row.className = "pool-role-row";
      const roleLabel = document.createElement("span");
      roleLabel.textContent = `${role}:`;
      row.appendChild(roleLabel);
      for (const name of names) {
        const chip = document.createElement("span");
        chip.className = "pool-chip";
        chip.textContent = `${name} `;
        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.textContent = "×";
        removeBtn.addEventListener("click", async () => {
          clearError();
          const champ = poolChampions.find((c) => c.name === name);
          if (!champ) return;
          try {
            const resp = await apiPost(`${API}/pool/remove`, {
              player,
              role,
              champion_id: champ.champion_id,
            });
            poolRegistry = resp.registry;
            renderPoolList();
          } catch (e) {
            showError(e.message);
          }
        });
        chip.appendChild(removeBtn);
        row.appendChild(chip);
      }
      div.appendChild(row);
    }
    el.appendChild(div);
  }
}

async function refreshPool() {
  try {
    const resp = await apiGet(`${API}/pool`);
    poolRegistry = resp.registry;
  } catch {
    poolRegistry = {};
  }
  renderPoolList();
}

function wirePoolForms() {
  document.getElementById("pool-add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clearError();
    const player = document.getElementById("pool-player").value.trim();
    const role = document.getElementById("pool-role").value;
    const championId = parseInt(document.getElementById("pool-champion").value, 10);
    if (!player || !role || !championId) return;
    try {
      const resp = await apiPost(`${API}/pool/add`, { player, role, champion_id: championId });
      poolRegistry = resp.registry;
      renderPoolList();
    } catch (err) {
      showError(err.message);
    }
  });

  document.getElementById("pool-import-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    clearError();
    const player = document.getElementById("import-player").value.trim();
    const role = document.getElementById("import-role").value;
    const riotId = document.getElementById("import-riot-id").value.trim();
    const region = document.getElementById("import-region").value.trim();
    const top = parseInt(document.getElementById("import-top").value, 10) || 10;
    const hashIdx = riotId.indexOf("#");
    if (!player || !role || hashIdx === -1 || !region) {
      showError('Riot ID must be in the form Name#Tag, e.g. "Faker#KR1".');
      return;
    }
    try {
      const resp = await apiPost(`${API}/pool/import-opgg`, {
        player,
        role,
        game_name: riotId.slice(0, hashIdx),
        tag_line: riotId.slice(hashIdx + 1),
        region,
        top,
      });
      poolRegistry = resp.registry;
      renderPoolList();
    } catch (err) {
      showError(err.message);
    }
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

// ---- matchup tips panel ----
// Always live OP.GG data, regardless of the active draft's provider -- uses its
// own champion list (/api/tips/champions) rather than the shared `champions`
// cache, since that could be the manual dataset's id space instead.

let tipsChampions = [];

function renderTipsSelectors() {
  const champSelect = document.getElementById("tips-champion");
  const oppSelect = document.getElementById("tips-opponent");
  const roleSelect = document.getElementById("tips-role");

  const options = tipsChampions
    .map((c) => `<option value="${c.champion_id}">${c.name}</option>`)
    .join("");
  champSelect.innerHTML = `<option value="">-- champion --</option>${options}`;
  oppSelect.innerHTML = `<option value="">-- opponent --</option>${options}`;
  roleSelect.innerHTML = ROLES.map((r) => `<option value="${r}">${r}</option>`).join("");
}

async function refreshTipsChampions() {
  try {
    tipsChampions = await apiGet(`${API}/tips/champions`);
  } catch {
    tipsChampions = [];
  }
  renderTipsSelectors();
}

async function onShowTips() {
  clearError();
  const champId = document.getElementById("tips-champion").value;
  const role = document.getElementById("tips-role").value;
  const oppId = document.getElementById("tips-opponent").value;
  if (!champId || !role || !oppId) {
    showError("Choose a champion, role, and opponent first.");
    return;
  }
  const params = new URLSearchParams({ champion_id: champId, role, opponent_id: oppId });
  try {
    const guide = await apiGet(`${API}/tips?${params.toString()}`);
    renderTips(guide);
  } catch (e) {
    showError(e.message);
    document.getElementById("tips-result").innerHTML = "";
  }
}

function renderTips(guide) {
  const el = document.getElementById("tips-result");
  const parts = [
    `<p><strong>${guide.my_champion}</strong> vs <strong>${guide.opponent_champion}</strong> (${guide.role})</p>`,
    `<p>Lane advantage: ${guide.lane_advantage}</p>`,
    `<p>Solo-kill advantage: ${guide.lane_solo_kill_advantage}</p>`,
    `<p>Recommended play style: ${guide.recommended_play_style}</p>`,
    `<p>Tip: ${guide.tip}</p>`,
  ];
  if (guide.win_rate_by_game_length && guide.win_rate_by_game_length.length) {
    const curve = guide.win_rate_by_game_length
      .map((g) => `${g.game_length}: ${(g.win_rate * 100).toFixed(1)}%`)
      .join(", ");
    parts.push(`<p>Win rate by game length: ${curve}</p>`);
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
  await refreshRoster();
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
      await refreshRoster(); // roster resets on every `new` (it lives in DraftState)
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
  document.getElementById("pool-toggle").addEventListener("change", () => {
    renderSuggestControls();
    refreshSuggestions();
  });
  document.getElementById("suggest-role").addEventListener("change", refreshSuggestions);
  document.getElementById("show-build").addEventListener("click", onShowBuild);
  document.getElementById("show-tips").addEventListener("click", onShowTips);

  wireRosterForm("ally");
  wireRosterForm("enemy");
  wirePoolForms();
  refreshPoolChampions();
  refreshPool();
  refreshTipsChampions();

  refreshState();
}

document.addEventListener("DOMContentLoaded", init);
