let autoRefreshTimer = null;

async function fetchServers() {
    try {
        const res = await fetch("/api/servers");
        const servers = await res.json();
        renderServers(servers);
    } catch (e) {
        console.error("Failed to fetch servers:", e);
    }
}

async function fetchStatus() {
    try {
        const res = await fetch("/api/status");
        const data = await res.json();
        document.getElementById("status-text").textContent =
            `${data.servers} server(s) | ${data.tools} tool(s) | uptime ${formatUptime(data.uptime_seconds)}`;
    } catch (e) {
        document.getElementById("status-text").textContent = "Disconnected";
    }
}

function formatUptime(seconds) {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    return `${Math.round(seconds / 3600)}h`;
}

function renderServers(servers) {
    const container = document.getElementById("servers-container");
    if (servers.length === 0) {
        container.innerHTML = '<p class="empty">No servers discovered yet.</p>';
        return;
    }
    container.innerHTML = servers.map(s => `
        <div class="server-card">
            <div class="server-header">
                <span class="server-name">${esc(s.name)}</span>
                <span class="server-addr">${esc(s.ip)}:${s.port}</span>
            </div>
            ${s.description ? `<div class="server-desc">${esc(s.description)}</div>` : ""}
            <div class="tools-list">
                ${s.tools.map(t => `<span class="tool-tag">${esc(t.name)}</span>`).join("")}
            </div>
        </div>
    `).join("");
}

async function triggerDiscovery() {
    const btn = document.getElementById("discover-btn");
    btn.disabled = true;
    btn.textContent = "Discovering...";
    try {
        await fetch("/api/discover", { method: "POST" });
        await fetchServers();
        await fetchStatus();
    } finally {
        btn.disabled = false;
        btn.textContent = "Refresh Discovery";
    }
}

function toggleAutoRefresh() {
    const checked = document.getElementById("auto-refresh").checked;
    if (checked) {
        autoRefreshTimer = setInterval(() => {
            fetchServers();
            fetchStatus();
        }, 10000);
    } else {
        clearInterval(autoRefreshTimer);
        autoRefreshTimer = null;
    }
}

function copyText(id) {
    const text = document.getElementById(id).textContent;
    navigator.clipboard.writeText(text);
}

function esc(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
}

// Initial load
fetchServers();
fetchStatus();
