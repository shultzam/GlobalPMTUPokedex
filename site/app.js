const api = "/api";
const lbBody = document.querySelector("#lb tbody");
const lbEmpty = document.getElementById("lbEmpty");
const playerResult = document.getElementById("playerResult");
const speciesResult = document.getElementById("speciesResult");
const speciesList = document.getElementById("speciesList");
const leaderboardCache = new Map();

function td(txt) { const e = document.createElement("td"); e.textContent = txt; return e; }
function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function setResult(el, html, isError = false) {
  el.innerHTML = html;
  el.classList.toggle("muted", isError);
}

function renderCaptureBadges(captures) {
  if (!captures || captures.length === 0) {
    return "<span class='muted'>No captures yet.</span>";
  }
  return captures
    .map((c) => {
      const classes = ["badge"];
      if (c.shiny) classes.push("shiny");
      return `<span class="${classes.join(" ")}">${escapeHtml(c.pokemon_name)}</span>`;
    })
    .join(" ");
}

async function fetchPlayerDex(steamId) {
  if (leaderboardCache.has(steamId)) {
    return leaderboardCache.get(steamId);
  }
  const res = await fetch(`${api}/v1/dex/${encodeURIComponent(steamId)}`);
  if (!res.ok) throw new Error("Failed to fetch dex");
  const data = await res.json();
  leaderboardCache.set(steamId, data);
  return data;
}

function buildDetailContent(data, rank, fallbackTotals) {
  const safeName = data.steam_name_safe ?? data.steam_name ?? data.steam_id;
  const total = data.count ?? fallbackTotals.total ?? 0;
  const shinies = data.shiny_count ?? fallbackTotals.shinies ?? 0;
  const capturesList = renderCaptureBadges(data.captures);

  return `
    <div class="lb-detail-head">
      <div>
        <div class="player">
          <strong>${escapeHtml(safeName)}</strong>
          <span class="muted">(${escapeHtml(data.steam_id)})</span>
        </div>
        <div class="muted">Rank #${rank} · Total ${total} · Shiny ${shinies}</div>
      </div>
      <div class="muted">Click row to collapse</div>
    </div>
    <div class="captures lb-captures">${capturesList}</div>
  `;
}

async function toggleLeaderboardRow(tr, entry) {
  const alreadyExpanded = tr.classList.contains("expanded");
  const next = tr.nextElementSibling;
  if (alreadyExpanded) {
    if (next && next.classList.contains("lb-details")) next.remove();
    tr.classList.remove("expanded");
    return;
  }

  const detailRow = document.createElement("tr");
  detailRow.classList.add("lb-details");
  const detailTd = document.createElement("td");
  detailTd.colSpan = 4;
  detailTd.innerHTML = "<span class='muted'>Loading captures…</span>";
  detailRow.appendChild(detailTd);

  if (next && next.classList.contains("lb-details")) next.remove();
  tr.after(detailRow);
  tr.classList.add("expanded");

  try {
    const data = await fetchPlayerDex(entry.steam_id);
    detailTd.innerHTML = buildDetailContent(data, entry.rank, {
      total: entry.total,
      shinies: entry.shinies,
    });
  } catch (err) {
    detailTd.innerHTML = "<span class='muted'>Could not load captures right now.</span>";
  }
}

async function loadLeaderboard() {
  if (!lbBody) return;
  const res = await fetch(`${api}/v1/leaderboard?limit=50`);
  if (!res.ok) {
    lbBody.textContent = "";
    lbEmpty && (lbEmpty.textContent = "Could not load leaderboard." + (res.status ? ` (${res.status})` : ""));
    lbEmpty && (lbEmpty.style.display = "block");
    return;
  }
  const data = await res.json();
  lbBody.textContent = "";
  if (!data.entries || data.entries.length === 0) {
    lbEmpty && (lbEmpty.style.display = "block");
    return;
  }
  lbEmpty && (lbEmpty.style.display = "none");
  data.entries.forEach((e, i) => {
    const tr = document.createElement("tr");
    tr.classList.add("lb-row");
    const name = e.steam_name_safe ?? e.steam_name ?? e.steam_id;
    tr.appendChild(td(i + 1));
    const nameCell = td(name);
    nameCell.classList.add("lb-name");
    nameCell.title = "Click to view captures";
    tr.appendChild(nameCell);
    tr.appendChild(td(e.total ?? 0));
    tr.appendChild(td(e.shinies ?? 0));
    const entry = { ...e, rank: i + 1 };
    tr.addEventListener("click", () => toggleLeaderboardRow(tr, entry));
    lbBody.appendChild(tr);
  });
}

async function searchPlayer() {
  if (!playerResult) return;
  const q = document.getElementById("playerQuery").value.trim();
  if (!q) {
    setResult(playerResult, "Enter a Steam name or Steam ID.", true);
    return;
  }
  setResult(playerResult, "Searching…");
  try {
    const res = await fetch(`${api}/v1/player/search?query=${encodeURIComponent(q)}`);
    if (!res.ok) {
      setResult(playerResult, "Player not found.", true);
      return;
    }
    const data = await res.json();
    const safeName = data.steam_name_safe ?? data.steam_name ?? "Unknown";

    const capturesList = renderCaptureBadges(data.captures);

    setResult(playerResult, `
      <div class="player">
        <strong>${safeName}</strong> <span class="muted">(${data.steam_id})</span>
      </div>
      <div>Rank #${data.rank} · Total ${data.total ?? 0} · Shiny ${data.shinies ?? 0}</div>
      <div class="captures">${capturesList || "<span class='muted'>No captures yet.</span>"}</div>
    `);
  } catch (err) {
    setResult(playerResult, "Player not found.", true);
  }
}

async function lookupSpecies(name) {
  if (!speciesResult) return;
  if (!name) {
    setResult(speciesResult, "Enter a Pokémon name.", true);
    return;
  }
  setResult(speciesResult, "Looking up…");
  try {
    const res = await fetch(`${api}/v1/species/${encodeURIComponent(name)}/caught`);
    if (!res.ok) {
      // Try to get suggestions.
      const fallback = await fetch(`${api}/v1/species/search?term=${encodeURIComponent(name)}&limit=5`);
      let suggestionText = "";
      if (fallback.ok) {
        const data = await fallback.json();
        if (data.names && data.names.length) {
          const escaped = data.names.map(escapeHtml);
          suggestionText = ` Did you mean: ${escaped.join(", ")}?`;
        }
      }
      setResult(speciesResult, `Could not find that Pokémon.${suggestionText}`, true);
      return;
    }
    const data = await res.json();
    const baseLine = `${escapeHtml(data.pokemon_name)}: ${data.total_players ?? 0} players · ${data.shiny_players ?? 0} shiny`;
    let firstLine = "";
    if (data.first_caught_by_id && data.first_caught_at) {
      const firstName = escapeHtml(
        data.first_caught_by_name_safe ?? data.first_caught_by_name ?? data.first_caught_by_id
      );
      let when = data.first_caught_at;
      try {
        when = new Date(data.first_caught_at).toLocaleDateString();
      } catch (_) {
        // Fall back to raw value if parsing fails.
        when = data.first_caught_at;
      }
      firstLine = `<div class="muted">First caught by ${firstName} on ${escapeHtml(when)}</div>`;
    }
    setResult(
      speciesResult,
      `<div>${baseLine}</div>${firstLine}`
    );
  } catch (err) {
    setResult(speciesResult, `${escapeHtml(name)} not found.`, true);
  }
}

let speciesTimer = null;
async function loadSpeciesOptions(term = "") {
  if (!speciesList) return;
  const res = await fetch(`${api}/v1/species/search?term=${encodeURIComponent(term)}`);
  if (!res.ok) return;
  const data = await res.json();
  speciesList.innerHTML = data.names.map(n => `<option value="${n}"></option>`).join("");
}

const lookupBtn = document.getElementById("lookup");
if (lookupBtn) {
  lookupBtn.addEventListener("click", () => {
    const n = document.getElementById("speciesName").value.trim();
    lookupSpecies(n);
  });
}

const playerLookupBtn = document.getElementById("playerLookup");
if (playerLookupBtn) playerLookupBtn.addEventListener("click", searchPlayer);

const playerQueryInput = document.getElementById("playerQuery");
if (playerQueryInput) {
  playerQueryInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") searchPlayer();
  });
}

const speciesNameInput = document.getElementById("speciesName");
if (speciesNameInput) {
  speciesNameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      lookupSpecies(e.target.value.trim());
    }
  });
  speciesNameInput.addEventListener("input", (e) => {
    clearTimeout(speciesTimer);
    const term = e.target.value.trim();
    speciesTimer = setTimeout(() => loadSpeciesOptions(term), 150);
  });
}

const jumpLeaderboardBtn = document.getElementById("jumpLeaderboard");
if (jumpLeaderboardBtn) {
  jumpLeaderboardBtn.addEventListener("click", () => {
    window.location.href = "/leaderboard.html";
  });
}

const homeBtn = document.getElementById("homeBtn");
if (homeBtn) {
  homeBtn.addEventListener("click", () => {
    window.location.href = "/index.html";
  });
}

const aboutModal = document.getElementById("aboutModal");
const aboutCloseBtn = document.getElementById("aboutClose");
const aboutOpenBtns = document.querySelectorAll("[data-about-open]");

function openAbout() {
  if (!aboutModal) return;
  aboutModal.classList.add("show");
  aboutModal.setAttribute("aria-hidden", "false");
}

function closeAbout() {
  if (!aboutModal) return;
  aboutModal.classList.remove("show");
  aboutModal.setAttribute("aria-hidden", "true");
}

aboutOpenBtns.forEach((btn) => btn.addEventListener("click", openAbout));
if (aboutCloseBtn) aboutCloseBtn.addEventListener("click", closeAbout);
if (aboutModal) {
  aboutModal.addEventListener("click", (e) => {
    if (e.target === aboutModal) closeAbout();
  });
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeAbout();
});

loadLeaderboard();
loadSpeciesOptions();
