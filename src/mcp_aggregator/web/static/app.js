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
            updateConnectionInfo(data.hostname, data.port, data.public_url, data.auth_hash);
        }
    } catch (e) {
        document.getElementById("status-text").textContent = "Disconnected";
    }
}

async function fetchExternal() {
    try {
        const res = await fetch("/api/external");
        const configs = await res.json();
        renderExternal(configs);
    } catch (e) {
        console.error("Failed to fetch external servers:", e);
    }
}

function deriveMcpName(publicUrl) {
    try {
        return new URL(publicUrl).hostname.replace(/\./g, "-");
    } catch (e) {
        return "beacon";
    }
}

function updateConnectionInfo(hostname, port, publicUrl, authHash) {
    const remoteBlock = document.getElementById("remote-setup");
    const localBlock = document.getElementById("local-setup");

    if (publicUrl) {
        const remoteUrl = authHash
            ? `${publicUrl}?hash=${encodeURIComponent(authHash)}`
            : publicUrl;
        const name = deriveMcpName(publicUrl);
        document.getElementById("remote-mcp-url").textContent = remoteUrl;
        document.getElementById("remote-setup-cli").textContent =
            `claude mcp add ${name} -s user --transport http "${remoteUrl}"`;
        document.getElementById("remote-setup-json").textContent = JSON.stringify({
            mcpServers: {
                [name]: {
                    type: "streamableHttp",
                    url: remoteUrl,
                },
            },
        }, null, 2);
        remoteBlock.style.display = "";
        localBlock.style.display = "none";
        return;
    }

    remoteBlock.style.display = "none";
    localBlock.style.display = "";

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
    container.innerHTML = servers.map(s => {
        const endpoint = s.url || `http://${s.ip}:${s.port}${s.path || "/mcp"}`;
        const originTag = s.origin === "external"
            ? '<span class="badge badge-external" title="Added manually via /api/external">external</span>'
            : '<span class="badge badge-discovered" title="Discovered via UDP">discovered</span>';
        const errorBanner = s.error
            ? `<p class="server-error">⚠ ${esc(s.error)}</p>`
            : "";
        return `
        <details class="server-card">
            <summary class="server-header">
                <span class="server-name">${esc(s.name)}</span>
                ${originTag}
                ${s.description ? `<span class="server-desc">${esc(s.description)}</span>` : ""}
                <span class="server-tool-count">${s.tools.length} tool${s.tools.length !== 1 ? "s" : ""}</span>
            </summary>
            <div class="server-body">
                ${errorBanner}
                <div class="tools-list">
                    ${s.tools.map(t => renderTool(t, s.name)).join("")}
                </div>
                <details class="server-details">
                    <summary>Technical Details</summary>
                    <div class="details-content">
                        <table class="details-table">
                            <tr><td class="details-label">Origin</td><td>${esc(s.origin || "discovery")}</td></tr>
                            <tr><td class="details-label">Endpoint</td><td><code>${esc(endpoint)}</code></td></tr>
                            <tr><td class="details-label">Namespaced as</td><td><code>${esc(s.name)}__*</code></td></tr>
                            <tr><td class="details-label">Authenticated</td><td>${s.authenticated ? "yes" : "no"}</td></tr>
                            <tr><td class="details-label">Last seen</td><td>${formatTimestamp(s.last_seen)}</td></tr>
                        </table>
                    </div>
                </details>
            </div>
        </details>
    `;
    }).join("");
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

function renderExternal(configs) {
    const list = document.getElementById("external-list");
    if (!configs.length) {
        list.innerHTML = '<p class="empty">No external servers configured.</p>';
        return;
    }
    list.innerHTML = configs.map(c => `
        <div class="external-row">
            <div class="external-info">
                <span class="external-name">${esc(c.name)}</span>
                <code class="external-url">${esc(c.url)}</code>
                ${c.header_keys.length
                    ? `<span class="external-headers" title="${esc(c.header_keys.join(", "))}">${c.header_keys.length} header${c.header_keys.length === 1 ? "" : "s"}</span>`
                    : ""}
            </div>
            <button class="external-delete" onclick="deleteExternal('${esc(c.name)}')" title="Remove">Remove</button>
        </div>
    `).join("");
}

async function addExternal() {
    const btn = document.getElementById("external-add-btn");
    const feedback = document.getElementById("external-feedback");
    const textarea = document.getElementById("external-json");
    feedback.textContent = "";
    feedback.className = "external-feedback";

    const raw = textarea.value.trim();
    if (!raw) {
        feedback.textContent = "Paste a JSON config first.";
        feedback.classList.add("error");
        return;
    }

    let payload;
    try {
        payload = JSON.parse(raw);
    } catch (e) {
        feedback.textContent = `Invalid JSON: ${e.message}`;
        feedback.classList.add("error");
        return;
    }

    btn.disabled = true;
    btn.textContent = "Adding...";
    try {
        const res = await fetch("/api/external", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) {
            feedback.textContent = data.error || `HTTP ${res.status}`;
            feedback.classList.add("error");
            return;
        }
        const parts = data.added.map(a =>
            a.error ? `${a.name} (error: ${a.error})` : `${a.name} (${a.tools} tools)`
        );
        feedback.textContent = `Added: ${parts.join(", ")}`;
        feedback.classList.add(data.added.some(a => a.error) ? "error" : "success");
        textarea.value = "";
        await Promise.all([fetchExternal(), fetchServers(), fetchStatus()]);
    } catch (e) {
        feedback.textContent = `Error: ${e.message}`;
        feedback.classList.add("error");
    } finally {
        btn.disabled = false;
        btn.textContent = "Add";
    }
}

async function deleteExternal(name) {
    if (!confirm(`Remove external server "${name}"?`)) return;
    try {
        const res = await fetch(`/api/external/${encodeURIComponent(name)}`, { method: "DELETE" });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            alert(data.error || `HTTP ${res.status}`);
            return;
        }
        await Promise.all([fetchExternal(), fetchServers(), fetchStatus()]);
    } catch (e) {
        alert(`Error: ${e.message}`);
    }
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
        await fetchExternal();
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
fetchExternal();
