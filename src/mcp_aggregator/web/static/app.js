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
            updateConnectionInfo(data.hostname, data.port, data.public_url, data.auth_hash, data.oauth_admin_url);
        }
        updateOauthRedirect(data.oauth_redirect_uri);
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

function updateOauthRedirect(redirectUri) {
    const el = document.getElementById("oauth-redirect-uri");
    if (!el || !redirectUri) return;
    el.textContent = redirectUri;
    const warning = document.getElementById("oauth-redirect-warning");
    if (!warning) return;
    // The redirect URI is registered with the remote authorization server, so it
    // has to be the origin the browser actually comes back to. If you reached
    // this UI on a different one, the callback will land nowhere.
    let matches = true;
    try {
        matches = new URL(redirectUri).origin === window.location.origin;
    } catch (e) {
        matches = false;
    }
    if (matches) {
        warning.style.display = "none";
        return;
    }
    warning.style.display = "";
    warning.textContent = `— does not match this page's origin (${window.location.origin}); set OAUTH_REDIRECT_BASE_URL`;
}

function deriveMcpName(publicUrl) {
    try {
        return new URL(publicUrl).hostname.replace(/\./g, "-");
    } catch (e) {
        return "beacon";
    }
}

function updateConnectionInfo(hostname, port, publicUrl, authHash, oauthAdminUrl) {
    const remoteBlock = document.getElementById("remote-setup");
    const localBlock = document.getElementById("local-setup");

    // Optional "Manage remote access" link — shown only when an admin URL is configured.
    const oauthLink = document.getElementById("oauth-admin-link");
    if (oauthLink) {
        if (oauthAdminUrl) {
            oauthLink.href = oauthAdminUrl;
            oauthLink.style.display = "";
        } else {
            oauthLink.style.display = "none";
        }
    }

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
    container.innerHTML = servers.map((s, i) => {
        const endpoint = s.url || `http://${s.ip}:${s.port}${s.path || "/mcp"}`;
        const originTag = s.federated_via
            ? `<span class="badge badge-federated" title="Reached through the Beacon '${escAttr(s.federated_via)}' (known there as '${escAttr(s.remote_name || "")}')">federated</span>`
            : s.origin === "external"
                ? '<span class="badge badge-external" title="Added manually via /api/external">external</span>'
                : '<span class="badge badge-discovered" title="Discovered via UDP">discovered</span>';
        const override = s.description_override || "";
        const note = s.note || "";
        const isCustom = !!override || !!note;
        const effectiveDesc = override || s.description || "";
        const customTag = isCustom
            ? '<span class="badge badge-custom" title="Customized in the UI">custom</span>'
            : "";
        const errorBanner = s.error
            ? `<p class="server-error">⚠ ${esc(s.error)}</p>`
            : "";
        return `
        <details class="server-card">
            <summary class="server-header">
                <span class="server-name">${esc(s.name)}</span>
                ${originTag}
                ${customTag}
                ${effectiveDesc ? `<span class="server-desc">${esc(effectiveDesc)}</span>` : ""}
                <span class="server-tool-count">${s.tools.length} tool${s.tools.length !== 1 ? "s" : ""}</span>
            </summary>
            <div class="server-body">
                ${errorBanner}
                <div class="server-doc-preview">
                    <button class="info-btn" onclick="toggleServerDoc(${i})"
                        title="Show the exact server_doc the LLM receives for this server">ⓘ Show server_doc</button>
                    <pre id="server-doc-${i}" class="doc-render" data-name="${escAttr(s.name)}" style="display:none"></pre>
                </div>
                <div class="server-annotate">
                    <label class="annotate-label" for="annotate-text-${i}">Description shown to the LLM (replaces the discovered one)</label>
                    <textarea id="annotate-text-${i}" class="annotate-text" rows="3" spellcheck="false"
                        data-name="${escAttr(s.name)}" data-default="${escAttr(s.description || "")}">${esc(effectiveDesc)}</textarea>
                    <div class="annotate-actions">
                        <button onclick="saveAnnotation(${i})">Save</button>
                        <button id="annotate-restore-${i}" onclick="restoreAnnotation(${i})"${override ? "" : " disabled"}>Restore default</button>
                        <span id="annotate-fb-${i}" class="annotate-feedback"></span>
                    </div>
                </div>
                <div class="server-annotate">
                    <label class="annotate-label" for="note-text-${i}">Extra docs — added to <code>server_doc</code> (drill-down only)</label>
                    <textarea id="note-text-${i}" class="annotate-text" rows="3" spellcheck="false"
                        data-name="${escAttr(s.name)}" placeholder="Optional. e.g. read https://…/start-here first; page titles follow <area>/<topic>.">${esc(note)}</textarea>
                    <div class="annotate-actions">
                        <button onclick="saveNote(${i})">Save</button>
                        <button id="note-clear-${i}" onclick="clearNote(${i})"${note ? "" : " disabled"}>Clear</button>
                        <span id="note-fb-${i}" class="annotate-feedback"></span>
                    </div>
                </div>
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

function externalAuthBadge(c) {
    const status = (c.auth && c.auth.status) || "none";
    if (status === "connected") {
        const refresh = c.auth.has_refresh_token;
        const title = refresh
            ? "Connected. A refresh token is stored, so this survives token expiry."
            : "Connected, but the server issued no refresh token — you will have to reconnect when it expires.";
        return `<span class="badge badge-oauth-ok" title="${escAttr(title)}">oauth ✓${refresh ? "" : " (no refresh)"}</span>`;
    }
    if (status === "authorizing") {
        return '<span class="badge badge-oauth-wait" title="Waiting for the browser login to finish">authorizing…</span>';
    }
    if (status === "needs_auth") {
        return '<span class="badge badge-oauth-need" title="This server requires OAuth authorization">needs auth</span>';
    }
    return "";
}

function renderExternal(configs) {
    const list = document.getElementById("external-list");
    if (!configs.length) {
        list.innerHTML = '<p class="empty">No external servers configured.</p>';
        return;
    }
    list.innerHTML = configs.map(c => {
        const status = (c.auth && c.auth.status) || "none";
        const fedCount = (c.federated || []).length;
        const fedBadge = fedCount
            ? `<span class="badge badge-federated" title="${escAttr(c.federated.join(", "))}">federated: ${fedCount} server${fedCount === 1 ? "" : "s"}</span>`
            : "";
        const connectBtn = status === "connected"
            ? `<button class="external-auth" onclick="disconnectExternal('${escAttr(c.name)}')" title="Forget the stored tokens">Disconnect</button>`
            : (status === "needs_auth" || status === "authorizing" || c.oauth)
                ? `<button class="external-auth primary" onclick="connectExternal('${escAttr(c.name)}')">Connect</button>`
                : "";
        return `
        <div class="external-row">
            <div class="external-info">
                <span class="external-name">${esc(c.name)}</span>
                <code class="external-url">${esc(c.url)}</code>
                ${externalAuthBadge(c)}
                ${fedBadge}
                ${c.header_keys.length
                    ? `<span class="external-headers" title="${esc(c.header_keys.join(", "))}">${c.header_keys.length} header${c.header_keys.length === 1 ? "" : "s"}</span>`
                    : ""}
                ${c.error && status !== "needs_auth" ? `<span class="external-err" title="${escAttr(c.error)}">⚠</span>` : ""}
            </div>
            <div class="external-row-actions">
                ${connectBtn}
                <button class="external-delete" onclick="deleteExternal('${escAttr(c.name)}')" title="Remove">Remove</button>
            </div>
        </div>`;
    }).join("");
}

async function addExternalUrl() {
    const input = document.getElementById("external-url");
    const btn = document.getElementById("external-url-btn");
    const feedback = document.getElementById("external-feedback");
    feedback.textContent = "";
    feedback.className = "external-feedback";

    const url = input.value.trim();
    if (!url) {
        feedback.textContent = "Enter the MCP endpoint URL first.";
        feedback.classList.add("error");
        return;
    }

    btn.disabled = true;
    btn.textContent = "Adding...";
    try {
        const res = await fetch("/api/external", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url }),
        });
        const data = await res.json();
        if (!res.ok) {
            feedback.textContent = data.error || `HTTP ${res.status}`;
            feedback.classList.add("error");
            return;
        }
        const added = data.added[0] || {};
        input.value = "";
        await Promise.all([fetchExternal(), fetchServers(), fetchStatus()]);
        if (added.auth_required) {
            // The server wants OAuth — go straight into the flow rather than
            // making the user hunt for the Connect button.
            feedback.textContent = `${added.name} requires authorization — opening login…`;
            await connectExternal(added.name);
            return;
        }
        feedback.textContent = added.error
            ? `${added.name}: ${added.error}`
            : `Added ${added.name} (${added.tools} tool${added.tools === 1 ? "" : "s"}).`;
        feedback.classList.add(added.error ? "error" : "success");
    } catch (e) {
        feedback.textContent = `Error: ${e.message}`;
        feedback.classList.add("error");
    } finally {
        btn.disabled = false;
        btn.textContent = "Add";
    }
}

async function connectExternal(name) {
    const feedback = document.getElementById("external-feedback");
    feedback.className = "external-feedback";
    feedback.textContent = `Starting authorization for ${name}…`;
    // Open the popup before the await: browsers block window.open once the
    // gesture that triggered it has been consumed by a network round-trip.
    const popup = window.open("", "beacon-oauth", "width=520,height=720");
    try {
        const res = await fetch(`/api/external/${encodeURIComponent(name)}/authorize`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) {
            if (popup) popup.close();
            feedback.textContent = data.error || `HTTP ${res.status}`;
            feedback.classList.add("error");
            await fetchExternal();
            return;
        }
        if (popup) {
            popup.location = data.authorize_url;
        } else {
            feedback.innerHTML = `Popup blocked — <a href="${escAttr(data.authorize_url)}" target="_blank" rel="noopener">open the login manually</a>.`;
        }
        feedback.textContent = `Waiting for you to finish logging in to ${name}…`;
    } catch (e) {
        if (popup) popup.close();
        feedback.textContent = `Error: ${e.message}`;
        feedback.classList.add("error");
    }
}

async function disconnectExternal(name) {
    if (!confirm(`Forget the stored OAuth tokens for "${name}"?`)) return;
    try {
        const res = await fetch(`/api/external/${encodeURIComponent(name)}/authorize`, { method: "DELETE" });
        if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            alert(data.error || `HTTP ${res.status}`);
            return;
        }
        await Promise.all([fetchExternal(), fetchServers()]);
    } catch (e) {
        alert(`Error: ${e.message}`);
    }
}

// The OAuth popup posts back when the callback lands, so the UI updates as soon
// as the flow completes instead of waiting for the next poll.
window.addEventListener("message", async (event) => {
    const data = event.data;
    if (!data || data.source !== "beacon-oauth") return;
    const feedback = document.getElementById("external-feedback");
    if (feedback) {
        feedback.className = "external-feedback " + (data.ok ? "success" : "error");
        feedback.textContent = data.ok
            ? `Connected ${data.name || ""}.`.trim()
            : `Authorization failed${data.name ? ` for ${data.name}` : ""}.`;
    }
    // The background flow still has to exchange the code for a token and
    // refresh the tool list; give it a moment before reading the state back.
    setTimeout(() => Promise.all([fetchExternal(), fetchServers(), fetchStatus()]), 1500);
});

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

function setAnnotateFeedback(el, text, cls) {
    el.textContent = text;
    el.className = "annotate-feedback" + (cls ? " " + cls : "");
}

// Shared PUT helper for the per-server annotation endpoint. `body` is the
// JSON object to send ({description} or {note}); returns parsed data or throws.
async function putAnnotation(name, body) {
    const res = await fetch(`/api/annotations/${encodeURIComponent(name)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
}

async function saveAnnotation(i) {
    const ta = document.getElementById(`annotate-text-${i}`);
    const fb = document.getElementById(`annotate-fb-${i}`);
    const restoreBtn = document.getElementById(`annotate-restore-${i}`);
    setAnnotateFeedback(fb, "");
    try {
        const data = await putAnnotation(ta.dataset.name, { description: ta.value });
        if (!data.description) {
            ta.value = ta.dataset.default || "";
            if (restoreBtn) restoreBtn.disabled = true;
            setAnnotateFeedback(fb, "Empty — default restored", "success");
        } else {
            if (restoreBtn) restoreBtn.disabled = false;
            setAnnotateFeedback(fb, "Saved", "success");
        }
        fetchStatus();
    } catch (e) {
        setAnnotateFeedback(fb, e.message, "error");
    }
}

async function restoreAnnotation(i) {
    const ta = document.getElementById(`annotate-text-${i}`);
    const fb = document.getElementById(`annotate-fb-${i}`);
    const restoreBtn = document.getElementById(`annotate-restore-${i}`);
    try {
        // Clear only the description (empty string), leaving any server note intact.
        await putAnnotation(ta.dataset.name, { description: "" });
        ta.value = ta.dataset.default || "";
        if (restoreBtn) restoreBtn.disabled = true;
        setAnnotateFeedback(fb, "Default restored", "success");
        fetchStatus();
    } catch (e) {
        setAnnotateFeedback(fb, e.message, "error");
    }
}

async function saveNote(i) {
    const ta = document.getElementById(`note-text-${i}`);
    const fb = document.getElementById(`note-fb-${i}`);
    const clearBtn = document.getElementById(`note-clear-${i}`);
    setAnnotateFeedback(fb, "");
    try {
        const data = await putAnnotation(ta.dataset.name, { note: ta.value });
        if (!data.note) {
            ta.value = "";
            if (clearBtn) clearBtn.disabled = true;
            setAnnotateFeedback(fb, "Empty — cleared", "success");
        } else {
            if (clearBtn) clearBtn.disabled = false;
            setAnnotateFeedback(fb, "Saved", "success");
        }
        fetchStatus();
    } catch (e) {
        setAnnotateFeedback(fb, e.message, "error");
    }
}

async function clearNote(i) {
    const ta = document.getElementById(`note-text-${i}`);
    const fb = document.getElementById(`note-fb-${i}`);
    const clearBtn = document.getElementById(`note-clear-${i}`);
    try {
        await putAnnotation(ta.dataset.name, { note: "" });
        ta.value = "";
        if (clearBtn) clearBtn.disabled = true;
        setAnnotateFeedback(fb, "Cleared", "success");
        fetchStatus();
    } catch (e) {
        setAnnotateFeedback(fb, e.message, "error");
    }
}

async function fetchInstructionsNote() {
    try {
        const res = await fetch("/api/instructions-note");
        const data = await res.json();
        const ta = document.getElementById("instructions-note");
        if (ta) ta.value = data.note || "";
        const clearBtn = document.getElementById("instructions-note-clear");
        if (clearBtn) clearBtn.disabled = !data.note;
    } catch (e) {
        console.error("Failed to fetch instructions note:", e);
    }
}

async function saveInstructionsNote() {
    const ta = document.getElementById("instructions-note");
    const fb = document.getElementById("instructions-note-fb");
    const clearBtn = document.getElementById("instructions-note-clear");
    setAnnotateFeedback(fb, "");
    try {
        const res = await fetch("/api/instructions-note", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ note: ta.value }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            setAnnotateFeedback(fb, data.error || `HTTP ${res.status}`, "error");
            return;
        }
        ta.value = data.note || "";
        if (clearBtn) clearBtn.disabled = !data.note;
        setAnnotateFeedback(fb, data.cleared ? "Empty — cleared" : "Saved", "success");
    } catch (e) {
        setAnnotateFeedback(fb, `Error: ${e.message}`, "error");
    }
}

async function clearInstructionsNote() {
    const ta = document.getElementById("instructions-note");
    const fb = document.getElementById("instructions-note-fb");
    const clearBtn = document.getElementById("instructions-note-clear");
    try {
        await fetch("/api/instructions-note", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ note: "" }),
        });
        ta.value = "";
        if (clearBtn) clearBtn.disabled = true;
        setAnnotateFeedback(fb, "Cleared", "success");
    } catch (e) {
        setAnnotateFeedback(fb, `Error: ${e.message}`, "error");
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

// Like esc() but also safe inside double-quoted HTML attributes.
function escAttr(str) {
    return esc(str).replace(/"/g, "&quot;");
}

// Toggle the exact server_doc render the LLM receives for a server.
async function toggleServerDoc(i) {
    const pre = document.getElementById(`server-doc-${i}`);
    if (!pre) return;
    if (pre.style.display !== "none") { pre.style.display = "none"; return; }
    pre.textContent = "Loading…";
    pre.style.display = "";
    try {
        const res = await fetch(`/api/servers/${encodeURIComponent(pre.dataset.name)}/doc`);
        const data = await res.json();
        pre.textContent = JSON.stringify(data, null, 2);
    } catch (e) {
        pre.textContent = `Error: ${e.message}`;
    }
}

// Toggle a preview of the live top-level instructions next to the editor.
async function toggleInstructionsRender() {
    const pre = document.getElementById("instructions-render-preview");
    if (!pre) return;
    if (pre.style.display !== "none") { pre.style.display = "none"; return; }
    pre.textContent = "Loading…";
    pre.style.display = "";
    try {
        const res = await fetch("/api/beacon-info");
        const data = await res.json();
        pre.textContent = data.instructions || "(empty)";
    } catch (e) {
        pre.textContent = `Error: ${e.message}`;
    }
}

async function fetchBeaconInfo() {
    try {
        const res = await fetch("/api/beacon-info");
        const data = await res.json();
        const instr = document.getElementById("beacon-instructions-render");
        if (instr) instr.textContent = data.instructions || "(empty)";
        const tools = document.getElementById("beacon-tools-render");
        if (tools) tools.innerHTML = (data.tools || []).map(renderMetaTool).join("");
    } catch (e) {
        console.error("Failed to fetch beacon info:", e);
    }
}

// Like renderTool but for Beacon's own meta-tools (no server namespace).
function renderMetaTool(tool) {
    const schema = tool.inputSchema || {};
    const props = schema.properties || {};
    const required = schema.required || [];
    const paramNames = Object.keys(props);
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
                ` : '<p class="hint">No parameters.</p>'}
            </div>
        </details>
    `;
}

// Initial load
fetchServers();
fetchStatus();
fetchExternal();
fetchInstructionsNote();
fetchBeaconInfo();
