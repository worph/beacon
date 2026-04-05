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
        if (data.hostname && data.port) {
            updateConnectionInfo(data.hostname, data.port);
        }
    } catch (e) {
        document.getElementById("status-text").textContent = "Disconnected";
    }
}

function updateConnectionInfo(hostname, port) {
    const mcpUrl = `http://${hostname}:${port}/mcp`;
    document.getElementById("mcp-url").textContent = mcpUrl;
    document.getElementById("setup-cli").textContent =
        `claude mcp add beacon -s user --transport http ${mcpUrl}`;
    document.getElementById("setup-json").textContent = JSON.stringify({
        mcpServers: {
            beacon: {
                type: "streamableHttp",
                url: mcpUrl,
            },
        },
    }, null, 2);
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
        <details class="server-card">
            <summary class="server-header">
                <span class="server-name">${esc(s.name)}</span>
                ${s.description ? `<span class="server-desc">${esc(s.description)}</span>` : ""}
                <span class="server-tool-count">${s.tools.length} tool${s.tools.length !== 1 ? "s" : ""}</span>
            </summary>
            <div class="server-body">
                <div class="tools-list">
                    ${s.tools.map(t => renderTool(t, s.name)).join("")}
                </div>
                <details class="server-details">
                    <summary>Technical Details</summary>
                    <div class="details-content">
                        <table class="details-table">
                            <tr><td class="details-label">IP</td><td><code>${esc(s.ip)}</code></td></tr>
                            <tr><td class="details-label">Endpoint</td><td><code>http://${esc(s.ip)}:${s.port}${esc(s.path || "/mcp")}</code></td></tr>
                            <tr><td class="details-label">Namespaced as</td><td><code>${esc(s.name)}__*</code></td></tr>
                            <tr><td class="details-label">Last seen</td><td>${formatTimestamp(s.last_seen)}</td></tr>
                        </table>
                    </div>
                </details>
            </div>
        </details>
    `).join("");
}

function renderTool(tool, serverName) {
    const schema = tool.inputSchema || {};
    const props = schema.properties || {};
    const required = schema.required || [];
    const paramNames = Object.keys(props);
    const hasDetails = tool.description || paramNames.length > 0;

    if (!hasDetails) {
        return `<span class="tool-tag">${esc(tool.name)}</span>`;
    }

    return `
        <details class="tool-detail">
            <summary class="tool-tag tool-tag-expandable">${esc(tool.name)}</summary>
            <div class="tool-info">
                ${tool.description ? `<p class="tool-desc">${esc(tool.description)}</p>` : ""}
                ${paramNames.length > 0 ? `
                    <div class="tool-params">
                        <span class="params-label">Parameters</span>
                        <table class="params-table">
                            <thead><tr><th>Name</th><th>Type</th><th>Description</th></tr></thead>
                            <tbody>
                                ${paramNames.map(name => {
                                    const p = props[name];
                                    const isReq = required.includes(name);
                                    return `<tr>
                                        <td><code>${esc(name)}</code>${isReq ? '<span class="param-required">*</span>' : ""}</td>
                                        <td class="param-type">${esc(p.type || "any")}</td>
                                        <td class="param-desc">${p.description ? esc(p.description) : '<span class="no-desc">—</span>'}</td>
                                    </tr>`;
                                }).join("")}
                            </tbody>
                        </table>
                    </div>
                ` : ""}
                <div class="tool-namespace">
                    <code>${esc(serverName)}__${esc(tool.name)}</code>
                </div>
            </div>
        </details>
    `;
}

function formatTimestamp(ts) {
    if (!ts) return "unknown";
    const d = new Date(ts * 1000);
    const now = new Date();
    const diffS = Math.round((now - d) / 1000);
    if (diffS < 60) return `${diffS}s ago`;
    if (diffS < 3600) return `${Math.round(diffS / 60)}m ago`;
    return d.toLocaleTimeString();
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

function copyText(id) {
    const text = document.getElementById(id).textContent;
    const btn = event.currentTarget;
    navigator.clipboard.writeText(text).then(() => {
        const original = btn.innerHTML;
        btn.textContent = "Copied!";
        btn.classList.add("copied");
        setTimeout(() => {
            btn.innerHTML = original;
            btn.classList.remove("copied");
        }, 1500);
    });
}

function esc(str) {
    const d = document.createElement("div");
    d.textContent = str;
    return d.innerHTML;
}

// Initial load
fetchServers();
fetchStatus();
