const api = "/api";

function td(txt) { const e = document.createElement("td"); e.textContent = txt; return e; }

async function loadLeaderboard() {
  const res = await fetch(`${api}/v1/leaderboard?limit=25`);
  const data = await res.json();
  const tbody = document.querySelector("#lb tbody");
  tbody.textContent = "";
  data.entries.forEach((e, i) => {
    const tr = document.createElement("tr");
    const name = e.steam_name_safe ?? e.steam_name ?? "Unknown";
    tr.appendChild(td(i + 1));
    tr.appendChild(td(name));
    tr.appendChild(td(e.steam_id));
    tr.appendChild(td(e.total ?? 0));
    tr.appendChild(td(e.shinies ?? 0));
    tbody.appendChild(tr);
  });
}

async function lookupSpecies(name) {
  const res = await fetch(`${api}/v1/species/${encodeURIComponent(name)}/caught`);
  const data = await res.json();
  const ul = document.getElementById("who");
  ul.textContent = "";
  data.players.forEach(p => {
    const li = document.createElement("li");
    const nameSafe = p.steam_name_safe ?? p.steam_name ?? "Unknown";
    const shinyTag = p.shiny ? " [shiny]" : "";
    li.textContent = `${nameSafe} (${p.steam_id})${shinyTag} at ${p.captured_at}`;
    ul.appendChild(li);
  });
}

document.getElementById("lookup").addEventListener("click", () => {
  const n = document.getElementById("speciesName").value.trim();
  if (n) lookupSpecies(n);
});

loadLeaderboard();