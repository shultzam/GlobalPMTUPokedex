const api = "/api";
const lbBody = document.querySelector("#lb tbody");
const lbEmpty = document.getElementById("lbEmpty");
const playerResult = document.getElementById("playerResult");
const speciesResult = document.getElementById("speciesResult");
const speciesList = document.getElementById("speciesList");

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
    const name = e.steam_name_safe ?? e.steam_name ?? e.steam_id;
    tr.appendChild(td(i + 1));
    tr.appendChild(td(name));
    tr.appendChild(td(e.total ?? 0));
    tr.appendChild(td(e.shinies ?? 0));
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

    const capturesList = data.captures.map(c => {
      const shiny = c.shiny ? "✨ " : "";
      return `<span class="badge">${shiny}${escapeHtml(c.pokemon_name)}</span>`;
    }).join(" ");

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
    setResult(
      speciesResult,
      `${escapeHtml(data.pokemon_name)}: ${data.total_players ?? 0} players · ${data.shiny_players ?? 0} shiny`
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

loadLeaderboard();
loadSpeciesOptions();
