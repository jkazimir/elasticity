/**
 * Elasticity Web UI
 *
 * Hash-based routing:
 *   #/                → Home (orchestrations + conductors index)
 *   #/config/:id      → Config detail (orchestration list for one config)
 *   #/run/:id/:orch   → Batch-run view
 *   #/chat/:id/:orch  → Chat view
 *   #/conductor/:id        → Conductor detail
 *   #/conductor/:id/chat  → Conductor chat view
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Collapse threshold for tool usage entries in the chat view (US-XXX).
 * When the number of completed tool uses (call+result pairs) in a single
 * assistant message exceeds this value, all but the most recent are collapsed
 * behind a summary toggle.  "3 or fewer = all expanded" (AC-1).
 */
const TOOL_USE_COLLAPSE_THRESHOLD = 3;

/**
 * Monotonic counter used to generate unique IDs for aria-controls relationships
 * on the collapse toggle button and its controlled region.
 */
let _toolCollapseIdCounter = 0;

// ---------------------------------------------------------------------------
// Markdown + Mermaid setup
// ---------------------------------------------------------------------------

mermaid.initialize({ startOnLoad: false, theme: "default", securityLevel: "loose" });

// Custom marked renderer: intercept ```mermaid blocks.
const mermaidRenderer = {
  code({ text, lang }) {
    if (lang === "mermaid") {
      return `<div class="mermaid">${escHtml(text)}</div>`;
    }
    return false; // fall through to default
  },
};
marked.use({ renderer: mermaidRenderer });

async function renderMarkdown(text, container) {
  container.innerHTML = marked.parse(text || "");
  // Run mermaid on any newly added diagrams.
  const diagrams = container.querySelectorAll(".mermaid");
  if (diagrams.length > 0) {
    try {
      await mermaid.run({ nodes: diagrams });
    } catch (e) {
      // Mermaid parse errors are non-fatal; leave the raw text visible.
    }
  }
}

function escHtml(str) {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function displayConfigId(id) {
  return id.replace(/~/g, "/");
}

/** Format a token count as a compact human-readable string (e.g. "14.2K"). */
function formatTokenCount(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return String(n);
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

const app = document.getElementById("app");
const navBreadcrumb = document.getElementById("nav-breadcrumb");

function setNav(html) {
  navBreadcrumb.innerHTML = html;
}

window.addEventListener("hashchange", route);
window.addEventListener("load", route);

function route() {
  const hash = location.hash.slice(2) || ""; // strip '#/'
  const parts = hash.split("/");

  if (!hash || parts[0] === "") {
    renderHome();
  } else if (parts[0] === "config" && parts[1]) {
    renderConfig(parts[1]);
  } else if (parts[0] === "run" && parts[1] && parts[2]) {
    renderRun(parts[1], decodeURIComponent(parts[2]));
  } else if (parts[0] === "chat" && parts[1] && parts[2]) {
    renderChat(parts[1], decodeURIComponent(parts[2]));
  } else if (parts[0] === "conductor" && parts[1] && parts[2] === "chat") {
    renderConductorChat(parts[1]);
  } else if (parts[0] === "conductor" && parts[1]) {
    renderConductor(parts[1]);
  } else {
    renderHome();
  }
}

function nav(path) {
  location.hash = "/" + path;
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function apiFetch(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// SSE stream reader
// ---------------------------------------------------------------------------

async function* readSSE(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE events are separated by '\n\n'.
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop(); // keep incomplete trailing chunk

      for (const chunk of chunks) {
        for (const line of chunk.split("\n")) {
          if (line.startsWith("data: ")) {
            const raw = line.slice(6).trim();
            if (raw) {
              try {
                yield JSON.parse(raw);
              } catch (e) {
                console.warn("SSE parse error:", e, raw);
              }
            }
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

// ---------------------------------------------------------------------------
// Modal helpers
// ---------------------------------------------------------------------------

// Bootstrap modal instances (created lazily).
let _approvalModal = null;
let _humanApprovalModal = null;
let _askUserModal = null;

function getModal(id) {
  return bootstrap.Modal.getOrCreateInstance(document.getElementById(id));
}

/**
 * Show the tool-approval modal and resolve with the user's decision string.
 * @returns {Promise<"allow"|"deny"|"always_allow"|"always_deny">}
 */
function promptApproval(runId, agent, tool, args) {
  return new Promise((resolve) => {
    document.getElementById("approval-agent").textContent = agent;
    document.getElementById("approval-tool").textContent = tool;
    document.getElementById("approval-args").textContent = JSON.stringify(args, null, 2);

    const modal = getModal("approvalModal");

    const decide = async (decision) => {
      modal.hide();
      await fetch(`/api/runs/${runId}/approval`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      resolve(decision);
    };

    document.getElementById("btn-allow").onclick = () => decide("allow");
    document.getElementById("btn-always-allow").onclick = () => decide("always_allow");
    document.getElementById("btn-deny").onclick = () => decide("deny");
    document.getElementById("btn-always-deny").onclick = () => decide("always_deny");

    modal.show();
  });
}

/**
 * Show the ask-user modal and resolve with the user's answer string.
 * @returns {Promise<string>}
 */
function promptAskUser(runId, question) {
  return new Promise((resolve) => {
    document.getElementById("ask-user-question").textContent = question;
    const input = document.getElementById("ask-user-input");
    input.value = "";

    const modal = getModal("askUserModal");

    const submit = async () => {
      const answer = input.value.trim();
      modal.hide();
      await fetch(`/api/runs/${runId}/ask_user`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer }),
      });
      resolve(answer);
    };

    document.getElementById("btn-ask-user-submit").onclick = submit;
    input.onkeydown = (e) => { if (e.key === "Enter") submit(); };

    modal.show();
    setTimeout(() => input.focus(), 300);
  });
}

/**
 * Show the human-approval modal and resolve when the user submits.
 */
function promptHumanApproval(runId, message, content) {
  return new Promise((resolve) => {
    document.getElementById("human-approval-title").textContent = message || "Review Required";
    document.getElementById("human-approval-content").textContent = content;
    document.getElementById("human-approval-edit-input").value = content;
    document.getElementById("human-approval-feedback").value = "";

    // Reset UI to initial state.
    const editArea = document.getElementById("human-approval-edit-area");
    const rejectArea = document.getElementById("human-approval-reject-area");
    const btnSubmitEdit = document.getElementById("btn-human-submit-edit");
    const btnSubmitReject = document.getElementById("btn-human-submit-reject");
    editArea.classList.add("d-none");
    rejectArea.classList.add("d-none");
    btnSubmitEdit.classList.add("d-none");
    btnSubmitReject.classList.add("d-none");

    const modal = getModal("humanApprovalModal");

    const submit = async (payload) => {
      modal.hide();
      await fetch(`/api/runs/${runId}/human_approval`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      resolve(payload);
    };

    document.getElementById("btn-human-approve").onclick = () =>
      submit({ decision: "approve" });

    document.getElementById("btn-human-edit").onclick = () => {
      editArea.classList.remove("d-none");
      btnSubmitEdit.classList.remove("d-none");
    };
    btnSubmitEdit.onclick = () =>
      submit({
        decision: "edit",
        edited_content: document.getElementById("human-approval-edit-input").value,
      });

    document.getElementById("btn-human-reject").onclick = () => {
      rejectArea.classList.remove("d-none");
      btnSubmitReject.classList.remove("d-none");
    };
    btnSubmitReject.onclick = () =>
      submit({
        decision: "reject",
        feedback: document.getElementById("human-approval-feedback").value || null,
      });

    modal.show();
  });
}

// ---------------------------------------------------------------------------
// Tool collapse helpers (US-XXX — chat view only)
// ---------------------------------------------------------------------------

/**
 * Factory: creates a fresh per-message-thread tool-collapse state object.
 * Each assistant bubble in the chat view gets one of these via its DOM node's
 * `_toolCollapseState` property.
 *
 * @returns {{ uid: number, toolUseCount: number, pendingCalls: number,
 *             isCollapsed: boolean, userExpanded: boolean,
 *             toggleBtn: Element|null, collapsedGroup: Element|null,
 *             recentGroup: Element|null }}
 */
function createToolCollapseState() {
  const uid = _toolCollapseIdCounter++;
  return {
    uid,
    toolUseCount: 0,    // completed tool call+result/denied pairs
    pendingCalls: 0,    // tool_call events awaiting a matching result
    isCollapsed: false, // true once threshold is crossed and user hasn't expanded
    userExpanded: false,// true when the user has manually expanded the group
    // DOM element references — populated after the bubble is inserted into the DOM
    toggleBtn: null,
    collapsedGroup: null,
    recentGroup: null,
  };
}

/**
 * Toggle the collapsed ↔ expanded state of the tool-uses group for one
 * assistant message thread.
 *
 * AC-3 / AC-4 / AC-7: toggles aria-expanded, updates the label, and retains
 * keyboard focus on the toggle button.
 *
 * @param {ReturnType<createToolCollapseState>} state
 */
function toggleToolCollapse(state) {
  if (state.isCollapsed) {
    // ── Expand ──────────────────────────────────────────────────────────────
    state.collapsedGroup.style.display = "";
    state.toggleBtn.setAttribute("aria-expanded", "true");
    state.toggleBtn.querySelector(".tool-collapse-label").textContent = "Hide tool uses";
    state.userExpanded = true;
    state.isCollapsed  = false;
  } else {
    // ── Collapse ─────────────────────────────────────────────────────────────
    state.collapsedGroup.style.display = "none";
    state.toggleBtn.setAttribute("aria-expanded", "false");
    // Count = all completed uses minus the 1 that stays visible in recentGroup.
    const count = state.toolUseCount - 1;
    state.toggleBtn.querySelector(".tool-collapse-label").textContent =
      `${count} tool use${count !== 1 ? "s" : ""}`;
    state.userExpanded = false;
    state.isCollapsed  = true;
  }
  // AC-7: retain focus on the button so keyboard users don't lose their place.
  state.toggleBtn.focus();
}

/**
 * Called each time a tool usage completes (toolUseCount > TOOL_USE_COLLAPSE_THRESHOLD).
 *
 * Moves all items from `recentGroup` except the last 2 (one tool_call + one
 * tool_result/denied) into `collapsedGroup`, then shows/updates the toggle button.
 *
 * AC-2, AC-5: handles both the initial collapse and subsequent live updates
 * without re-expanding a manually-collapsed group.
 *
 * @param {ReturnType<createToolCollapseState>} state
 */
function collapseOlderToolUses(state) {
  const recentGroup   = state.recentGroup;
  const collapsedGroup = state.collapsedGroup;
  const toggleBtn     = state.toggleBtn;

  // Keep the last 2 <li> items (the current call + result/denied) visible.
  // Everything else is moved into the collapsed group.
  const items     = Array.from(recentGroup.children);
  const keepCount = 2; // tool_call li + tool_result/denied li for the latest usage
  const moveItems = items.slice(0, items.length - keepCount);

  for (const item of moveItems) {
    collapsedGroup.appendChild(item); // DOM move — no clone needed
  }

  // Update the toggle button label: X = all completed uses minus the visible one.
  const hiddenCount = state.toolUseCount - 1;
  const label = `${hiddenCount} tool use${hiddenCount !== 1 ? "s" : ""}`;

  // Show the toggle button (first time: was display:none).
  toggleBtn.style.display = "";

  if (!state.userExpanded) {
    // Default / AC-5: user has not manually expanded — keep collapsed.
    collapsedGroup.style.display = "none";
    toggleBtn.setAttribute("aria-expanded", "false");
    toggleBtn.querySelector(".tool-collapse-label").textContent = label;
    state.isCollapsed = true;
  } else {
    // AC-5 + AC-3: user had previously expanded — keep expanded, update re-collapse label.
    collapsedGroup.style.display = "";
    toggleBtn.setAttribute("aria-expanded", "true");
    // While expanded the button reads "Hide tool uses" (set by toggleToolCollapse).
    // We only update the label if we need to re-collapse.
    state.isCollapsed = false;
  }
}

// ---------------------------------------------------------------------------
// Shared SSE event dispatcher
// ---------------------------------------------------------------------------

/**
 * Process a single SSE event and mutate the provided `ctx` object.
 *
 * ctx shape (all fields optional, created as needed):
 *   runId, sessionId, agentDivs, toolList, onDone, onError
 */
async function handleSSEEvent(event, ctx) {
  switch (event.type) {
    case "run_start":
      ctx.runId = event.run_id;
      if (event.session_id) ctx.sessionId = event.session_id;
      break;

    case "token": {
      const div = ctx.agentDivs?.[event.agent];
      if (!div) break;
      div.dataset.raw = (div.dataset.raw || "") + event.text;
      // Cache the element reference to avoid repeated querySelector calls.
      const textEl = div._agentTextEl || (div._agentTextEl = div.querySelector(".agent-text"));
      // Append only the new token — avoids O(n²) full-string rewrite.
      textEl.appendChild(document.createTextNode(event.text));
      textEl.classList.add("typing-cursor");
      break;
    }

    case "agent_start": {
      createAgentDiv(ctx, event.agent);
      break;
    }

    case "agent_complete": {
      const div = ctx.agentDivs?.[event.agent];
      if (!div) break;
      const textEl = div.querySelector(".agent-text");
      textEl.classList.remove("typing-cursor");
      // Render final text as markdown.
      await renderMarkdown(div.dataset.raw || "", textEl);
      div.querySelector(".agent-spinner")?.remove();
      const ms = Math.round(event.duration_ms || 0);
      const badge = div.querySelector(".agent-duration");
      if (badge) badge.textContent = ms > 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
      // Update context usage display if token data is present
      if (ctx.onUsageUpdate && (event.input_tokens || event.output_tokens)) {
        ctx.onUsageUpdate(event);
      }
      break;
    }

    case "agent_error": {
      const div = ctx.agentDivs?.[event.agent];
      if (!div) break;
      div.querySelector(".agent-text").innerHTML =
        `<span class="text-danger"><i class="bi bi-exclamation-triangle"></i> ${escHtml(event.message)}</span>`;
      div.querySelector(".agent-spinner")?.remove();
      break;
    }

    case "tool_call":
      appendToolItem(ctx, event, "call");
      break;

    case "tool_result":
      appendToolItem(ctx, event, "result");
      break;

    case "tool_denied":
      appendToolItem(ctx, event, "denied");
      break;

    case "approval_requested":
      await promptApproval(event.run_id, event.agent, event.tool, event.args);
      break;

    case "ask_user":
      await promptAskUser(event.run_id, event.question);
      break;

    case "human_approval_requested":
      await promptHumanApproval(event.run_id, event.message, event.content);
      break;

    case "done":
      if (ctx.onDone) ctx.onDone(event);
      break;

    case "error":
      if (ctx.onError) ctx.onError(event.message);
      break;

    default:
      // Silently ignore unknown event types.
      break;
  }
}

/** Create a new agent output card/bubble inside ctx.agentContainer.
 *  Always creates a new bubble — each agent_start event gets its own sequential
 *  entry in the timeline, enabling group-chat-style multi-turn display.
 *  ctx.agentDivs[agentName] tracks the *current* active bubble for that agent.
 *  ctx.allAgentDivs is a flat ordered list of every bubble created (used by the
 *  done handler to find the matching final-response bubble across all invocations).
 */
function createAgentDiv(ctx, agentName) {
  if (!ctx.agentDivs) ctx.agentDivs = {};
  if (!ctx.allAgentDivs) ctx.allAgentDivs = [];

  const div = document.createElement("div");

  if (ctx.agentBubbleMode) {
    // Chat mode: each agent activation gets its own message-level response bubble.
    div.className = "msg-assistant msg-agent-bubble";
    div.innerHTML = `
      <div class="msg-agent-label">
        <span class="spinner-border spinner-sm text-primary agent-spinner" role="status"></span>
        <span class="agent-name-label">${escHtml(agentName)}</span>
        <span class="ms-auto badge bg-secondary agent-duration"></span>
      </div>
      <div class="msg-bubble agent-text"></div>
    `;
    ctx.agentContainer.appendChild(div);
    ctx._lastAgentDiv = div;
    ctx.scrollFn?.();
  } else {
    // Batch-run / nested view: card inside an agent-activity container.
    div.className = "card agent-card mb-2";
    div.innerHTML = `
      <div class="card-header d-flex align-items-center gap-2 py-1 px-3">
        <span class="spinner-border spinner-sm text-primary agent-spinner" role="status"></span>
        <strong class="small">${escHtml(agentName)}</strong>
        <span class="ms-auto badge bg-secondary agent-duration"></span>
      </div>
      <div class="card-body py-2 px-3">
        <div class="agent-text small"></div>
      </div>
    `;
    ctx.agentContainer.appendChild(div);
  }

  ctx.agentDivs[agentName] = div;  // current active bubble for this agent
  ctx.allAgentDivs.push(div);       // historical record in creation order
  return div;
}

/** Append a tool call / result / denied entry to ctx.toolList. */
function appendToolItem(ctx, event, kind) {
  if (!ctx.toolList) return;

  const item = document.createElement("li");
  item.className = "list-group-item tool-item py-1";

  const icons = { call: "bi-tools text-secondary", result: "bi-check-circle text-success", denied: "bi-slash-circle text-danger" };

  if (kind === "call") {
    item.innerHTML = `
      <i class="bi ${icons[kind]}"></i>
      <span class="text-secondary">${escHtml(event.agent || "")} →</span>
      <code>${escHtml(event.tool)}</code>
      <span class="text-muted">(${escHtml(JSON.stringify(event.args || {}).slice(0, 120))})</span>
    `;
  } else if (kind === "result") {
    const preview = (event.result || "").slice(0, 200);
    item.innerHTML = `
      <i class="bi ${icons[kind]}"></i>
      <code>${escHtml(event.tool)}</code> →
      <span class="text-muted">${escHtml(preview)}${preview.length === 200 ? "…" : ""}</span>
    `;
  } else {
    item.innerHTML = `
      <i class="bi ${icons[kind]}"></i>
      <code>${escHtml(event.tool)}</code>
      <span class="text-danger">denied: ${escHtml(event.reason || "")}</span>
    `;
  }
  ctx.toolList.appendChild(item);
  ctx.toolList.closest("details")?.setAttribute("open", "");

  // ── Collapse tracking (chat view only — AC-2, AC-5) ──────────────────────
  // ctx.toolCollapseState is only set in the chat sendMessage() path.
  // The batch-run view leaves it undefined, so this block is a no-op there.
  const state = ctx.toolCollapseState;
  if (state) {
    if (kind === "call") {
      state.pendingCalls++;
    } else {
      // kind === "result" or "denied" — one tool usage is now complete.
      state.pendingCalls = Math.max(0, state.pendingCalls - 1);
      state.toolUseCount++;

      if (state.toolUseCount > TOOL_USE_COLLAPSE_THRESHOLD) {
        collapseOlderToolUses(state);
      }
    }
  }
}

/**
 * Populate the tool-activity section of an assistant bubble from stored events.
 * Used when loading session history so past tool usage is shown as-is.
 */
function renderHistoricalActivity(bubble, events) {
  if (!events || events.length === 0) return;
  const toolList = bubble.querySelector(".tool-activity");
  if (!toolList) return;
  const ctx = { toolList };
  let hasActivity = false;
  for (const ev of events) {
    switch (ev.type) {
      case "ToolCalled":
        appendToolItem(ctx, { tool: ev.tool_name, agent: ev.agent_name, args: ev.arguments || {} }, "call");
        hasActivity = true;
        break;
      case "ToolResult":
        appendToolItem(ctx, { tool: ev.tool_name, result: ev.result || "" }, "result");
        hasActivity = true;
        break;
      case "ToolDenied":
        appendToolItem(ctx, { tool: ev.tool_name, reason: ev.reason || "" }, "denied");
        hasActivity = true;
        break;
    }
  }
  if (hasActivity) {
    bubble.querySelector("details")?.setAttribute("open", "");
  }
}

// ---------------------------------------------------------------------------
// View: Home — consolidated orchestration + conductor index
// ---------------------------------------------------------------------------

/**
 * Render the home page, listing all orchestrations and all conductor configs
 * in separate labelled sections.
 *
 * Both datasets are fetched in parallel (Promise.all) and the entire page is
 * written in a single app.innerHTML assignment to prevent cumulative layout
 * shift (AC-7 / No-CLS constraint).
 *
 * Empty-state behaviour:
 *   - Both empty  → full-page "No configurations found" message (AC-5).
 *   - Orchestrations empty, conductors exist → orchestrations empty row +
 *     conductors section.
 *   - Conductors empty (zero conductor configs) → conductors section omitted
 *     entirely (AC-5: hide section when no conductors configured).
 */
async function renderHome() {
  setNav("");
  app.innerHTML = `
    <div class="text-center text-secondary py-5">
      <div class="spinner-border" role="status">
        <span class="visually-hidden">Loading configurations…</span>
      </div>
    </div>
  `;

  // Fetch orchestrations and conductors in parallel (AC-7: single render pass).
  let orchestrations, conductors;
  try {
    [orchestrations, conductors] = await Promise.all([
      apiFetch("/api/orchestrations"),
      apiFetch("/api/conductors"),
    ]);
  } catch (e) {
    app.innerHTML = `<div class="alert alert-danger m-3" role="alert">Failed to load configurations: ${escHtml(e.message)}</div>`;
    return;
  }

  // ── Full empty state — no orchestrations AND no conductors ────────────────
  if (orchestrations.length === 0 && conductors.length === 0) {
    app.innerHTML = `
      <div class="container py-5 text-center" role="main">
        <i class="bi bi-diagram-3 display-4 text-secondary" aria-hidden="true"></i>
        <h5 class="mt-3 text-secondary">No orchestrations found</h5>
        <p class="text-secondary small mb-0">
          Add YAML configuration files to the config directory to get started.
        </p>
      </div>
    `;
    return;
  }

  // ── Build orchestrations section ──────────────────────────────────────────
  let orchestrationsHtml;
  if (orchestrations.length > 0) {
    const rows = orchestrations.map((o) => {
      const modeClass  = o.mode === "conversational" ? "bg-success"  : "bg-primary";
      const modeIcon   = o.mode === "conversational" ? "bi-chat-dots" : "bi-play-circle";
      const modeLabel  = o.mode === "conversational" ? "conversational" : "batch";

      const actionBtn  = o.mode === "conversational"
        ? `<a class="btn btn-sm btn-success"
              href="#/chat/${escHtml(o.config_id)}/${encodeURIComponent(o.name)}"
              aria-label="Chat: ${escHtml(o.name)}">
             <i class="bi bi-chat-dots" aria-hidden="true"></i>
             <span class="d-none d-sm-inline"> Chat</span>
           </a>`
        : `<a class="btn btn-sm btn-primary"
              href="#/run/${escHtml(o.config_id)}/${encodeURIComponent(o.name)}"
              aria-label="Run: ${escHtml(o.name)}">
             <i class="bi bi-play-fill" aria-hidden="true"></i>
             <span class="d-none d-sm-inline"> Run</span>
           </a>`;

      const inputKeys  = Object.keys(o.input || {});
      const inputBadges = inputKeys.length > 0
        ? inputKeys.map((k) => `<span class="badge bg-secondary me-1">${escHtml(k)}</span>`).join("")
        : `<span class="text-muted fst-italic small">none</span>`;

      return `
        <tr>
          <td class="align-middle">
            <a class="fw-semibold text-decoration-none link-body-emphasis stretched-link-cell"
               href="#/config/${escHtml(o.config_id)}"
               aria-label="Config: ${escHtml(displayConfigId(o.config_id))}">
              <i class="bi bi-file-earmark-code text-primary me-1" aria-hidden="true"></i>${escHtml(displayConfigId(o.config_id))}
            </a>
            <div class="text-muted small">${escHtml(o.config_filename)}</div>
          </td>
          <td class="align-middle">
            <span class="fw-semibold">${escHtml(o.name)}</span>
            ${o.description ? `<div class="text-secondary small text-truncate" style="max-width:28ch;" title="${escHtml(o.description)}">${escHtml(o.description)}</div>` : ""}
          </td>
          <td class="align-middle">
            <span class="badge ${modeClass}">
              <i class="bi ${modeIcon} me-1" aria-hidden="true"></i>${escHtml(modeLabel)}
            </span>
          </td>
          <td class="align-middle text-center">
            <span title="${o.agent_count} agent${o.agent_count !== 1 ? "s" : ""}">
              <i class="bi bi-people text-secondary" aria-hidden="true"></i>
              <span class="ms-1">${o.agent_count}</span>
            </span>
            &nbsp;
            <span title="${o.tool_count} tool${o.tool_count !== 1 ? "s" : ""}">
              <i class="bi bi-tools text-secondary" aria-hidden="true"></i>
              <span class="ms-1">${o.tool_count}</span>
            </span>
          </td>
          <td class="align-middle">${inputBadges}</td>
          <td class="align-middle text-end">${actionBtn}</td>
        </tr>
      `;
    }).join("");

    orchestrationsHtml = `
      <div class="d-flex align-items-center mb-3 gap-2">
        <h4 class="mb-0 fw-normal text-secondary">
          <i class="bi bi-diagram-3 me-1" aria-hidden="true"></i> Orchestrations
        </h4>
        <span class="badge bg-secondary rounded-pill ms-1"
              aria-label="${orchestrations.length} orchestration${orchestrations.length !== 1 ? "s" : ""}">
          ${orchestrations.length}
        </span>
      </div>
      <div class="table-responsive">
        <table class="table table-hover align-middle mb-0"
               aria-label="All orchestrations">
          <thead class="table-light">
            <tr>
              <th scope="col">Config</th>
              <th scope="col">Orchestration</th>
              <th scope="col">Mode</th>
              <th scope="col" class="text-center">Agents&nbsp;/&nbsp;Tools</th>
              <th scope="col">Inputs</th>
              <th scope="col" class="text-end">Action</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
  } else {
    // Orchestrations empty but conductors exist — show empty label row so the
    // section is still labelled (AC-4: orchestration section remains present).
    orchestrationsHtml = `
      <div class="d-flex align-items-center mb-3 gap-2">
        <h4 class="mb-0 fw-normal text-secondary">
          <i class="bi bi-diagram-3 me-1" aria-hidden="true"></i> Orchestrations
        </h4>
      </div>
      <p class="text-secondary small">No orchestrations found.</p>
    `;
  }

  // ── Build conductors section (omitted entirely when empty — AC-5) ─────────
  let conductorsHtml = "";
  if (conductors.length > 0) {
    const conductorRows = conductors.map((c) => `
      <tr>
        <td class="align-middle">
          <a class="fw-semibold text-decoration-none link-body-emphasis"
             href="#/conductor/${escHtml(c.config_id)}"
             aria-label="Conductor: ${escHtml(c.name)}">
            <i class="bi bi-broadcast me-1 text-primary" aria-hidden="true"></i>${escHtml(displayConfigId(c.name))}
          </a>
          <div class="text-muted small">${escHtml(c.config_filename)}</div>
        </td>
        <td class="align-middle">
          <code>${escHtml(c.agent)}</code>
        </td>
        <td class="align-middle text-center">
          <span title="${c.team_count} team${c.team_count !== 1 ? "s" : ""}">
            <i class="bi bi-people text-secondary" aria-hidden="true"></i>
            <span class="ms-1">${c.team_count}</span>
          </span>
        </td>
        <td class="align-middle text-end">
          <a class="btn btn-sm btn-success"
             href="#/conductor/${encodeURIComponent(c.config_id)}/chat"
             aria-label="Chat with conductor: ${escHtml(c.name)}">
            <i class="bi bi-chat-dots-fill" aria-hidden="true"></i>
            <span class="d-none d-sm-inline"> Chat</span>
          </a>
        </td>
      </tr>
    `).join("");

    conductorsHtml = `
      <div class="d-flex align-items-center mb-3 mt-4 gap-2">
        <h4 class="mb-0 fw-normal text-secondary">
          <i class="bi bi-broadcast me-1" aria-hidden="true"></i> Conductors
        </h4>
        <span class="badge bg-secondary rounded-pill ms-1"
              aria-label="${conductors.length} conductor${conductors.length !== 1 ? "s" : ""}">
          ${conductors.length}
        </span>
      </div>
      <div class="table-responsive">
        <table class="table table-hover align-middle mb-0"
               aria-label="All conductors">
          <thead class="table-light">
            <tr>
              <th scope="col">Conductor</th>
              <th scope="col">Agent</th>
              <th scope="col" class="text-center">Teams</th>
              <th scope="col" class="text-end">Action</th>
            </tr>
          </thead>
          <tbody>${conductorRows}</tbody>
        </table>
      </div>
    `;
  }

  // ── Single innerHTML write — prevents CLS (AC-7) ──────────────────────────
  app.innerHTML = `
    <div class="container-fluid py-3" role="main">
      ${orchestrationsHtml}
      ${conductorsHtml}
    </div>
  `;
}

// ---------------------------------------------------------------------------
// View: Conductor detail
// ---------------------------------------------------------------------------

/**
 * Render the conductor detail page for a given conductor config ID.
 *
 * Fetches GET /api/conductors/{configId} and displays a summary of the
 * conductor (agent name, team count) together with a card for every team
 * definition.  This satisfies AC-3 — the link from the home page navigates
 * here without a JavaScript error.
 *
 * @param {string} configId - The conductor config file stem (e.g. "conductor").
 */
async function renderConductor(configId) {
  setNav(
    `<a class="text-decoration-none text-secondary" href="#/">Home</a> / Conductor: ${escHtml(displayConfigId(configId))}`
  );
  app.innerHTML = `
    <div class="text-center text-secondary py-5">
      <div class="spinner-border" role="status">
        <span class="visually-hidden">Loading conductor…</span>
      </div>
    </div>
  `;

  let conductor;
  try {
    conductor = await apiFetch(`/api/conductors/${encodeURIComponent(configId)}`);
  } catch (e) {
    app.innerHTML = `<div class="alert alert-danger m-3" role="alert">${escHtml(e.message)}</div>`;
    return;
  }

  // Build a card for each team definition.
  const teamCards = conductor.teams.map((t) => {
    const inputBadges = Object.keys(t.input || {}).length > 0
      ? Object.keys(t.input).map((k) =>
          `<span class="badge bg-secondary me-1">${escHtml(k)}</span>`
        ).join("")
      : "";

    return `
      <div class="col-12">
        <div class="card">
          <div class="card-body">
            <h6 class="mb-1 fw-semibold">
              <i class="bi bi-people text-primary me-1" aria-hidden="true"></i>${escHtml(t.name)}
            </h6>
            <p class="text-secondary small mb-1">${escHtml(t.description)}</p>
            <div class="text-muted small">
              <i class="bi bi-file-earmark-code me-1" aria-hidden="true"></i>${escHtml(t.config)}
              →
              <code>${escHtml(t.orchestration)}</code>
            </div>
            ${inputBadges
              ? `<div class="mt-1">
                   <span class="text-muted small me-1">inputs:</span>
                   ${inputBadges}
                 </div>`
              : ""}
          </div>
        </div>
      </div>
    `;
  }).join("");

  const safeId = configId.replace(/[^A-Za-z0-9_-]/g, "_");

  const agentsHtml = conductor.agents && conductor.agents.length > 0 ? `
    <h5 class="mt-4 mb-2"><i class="bi bi-people me-2"></i>Agents</h5>
    <div class="accordion mb-3" id="conductor-agents-accordion-${safeId}">
      ${conductor.agents.map((a, i) => `
        <div class="accordion-item">
          <h2 class="accordion-header">
            <button class="accordion-button collapsed py-2" type="button"
                    data-bs-toggle="collapse"
                    data-bs-target="#cagent-${safeId}-${i}"
                    aria-expanded="false"
                    aria-controls="cagent-${safeId}-${i}">
              <span class="fw-semibold me-2">${escHtml(a.name)}</span>
              <span class="badge bg-secondary font-monospace fw-normal me-2">${escHtml(a.model)}</span>
              ${a.name === conductor.agent ? `<span class="badge bg-primary me-2">conductor</span>` : ""}
              ${a.tools.length ? `<span class="text-muted small">${a.tools.length} tool${a.tools.length !== 1 ? "s" : ""}</span>` : ""}
            </button>
          </h2>
          <div id="cagent-${safeId}-${i}" class="accordion-collapse collapse"
               data-bs-parent="#conductor-agents-accordion-${safeId}">
            <div class="accordion-body py-2">
              <div class="mb-2">
                <span class="text-muted small d-block mb-1">System prompt</span>
                <pre class="small bg-light p-2 rounded mb-0" style="white-space: pre-wrap; max-height: 300px; overflow-y: auto;">${escHtml(a.system_prompt)}</pre>
              </div>
              ${a.tools.length ? `
                <div class="mt-2">
                  <span class="text-muted small">Tools: </span>
                  ${a.tools.map((t) => `<span class="badge bg-secondary me-1">${escHtml(t)}</span>`).join("")}
                </div>` : ""}
              ${a.can_spawn.length ? `
                <div class="mt-1">
                  <span class="text-muted small">Spawnable: </span>
                  ${a.can_spawn.map((s) => `<span class="badge bg-info text-dark me-1">${escHtml(s)}</span>`).join("")}
                </div>` : ""}
            </div>
          </div>
        </div>
      `).join("")}
    </div>
  ` : "";

  const toolsHtml = conductor.tools && conductor.tools.length > 0 ? `
    <h5 class="mt-4 mb-2"><i class="bi bi-tools me-2"></i>Tools</h5>
    <div class="table-responsive mb-3">
      <table class="table table-sm table-bordered small mb-0">
        <thead class="table-light">
          <tr><th>Name</th><th>Kind</th><th>Description</th></tr>
        </thead>
        <tbody>
          ${conductor.tools.map((t) => `
            <tr>
              <td class="font-monospace">${escHtml(t.name)}</td>
              <td>${t.builtin
                ? `<span class="badge bg-primary">${escHtml(t.builtin)}</span>`
                : `<span class="badge bg-secondary">custom</span>`}</td>
              <td class="text-muted">${t.description ? escHtml(t.description) : ""}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  ` : "";

  app.innerHTML = `
    <div class="container py-3" role="main">
      <nav aria-label="breadcrumb">
        <ol class="breadcrumb">
          <li class="breadcrumb-item"><a href="#/">Home</a></li>
          <li class="breadcrumb-item active" aria-current="page">
            Conductor: ${escHtml(displayConfigId(configId))}
          </li>
        </ol>
      </nav>

      <div class="d-flex align-items-center gap-2 mb-1">
        <i class="bi bi-broadcast text-primary" aria-hidden="true"></i>
        <h4 class="mb-0">${escHtml(conductor.filename)}</h4>
        <a class="btn btn-sm btn-success ms-auto"
           href="#/conductor/${encodeURIComponent(configId)}/chat"
           aria-label="Chat with conductor ${escHtml(displayConfigId(configId))}">
          <i class="bi bi-chat-dots-fill me-1" aria-hidden="true"></i>Chat
        </a>
      </div>
      <p class="text-secondary small mb-3">
        Conductor agent: <code>${escHtml(conductor.agent)}</code>
        &nbsp;·&nbsp;
        ${conductor.teams.length} team${conductor.teams.length !== 1 ? "s" : ""}
      </p>

      <h5 class="mt-3 mb-2">Teams</h5>
      ${conductor.teams.length > 0
        ? `<div class="row g-3">${teamCards}</div>`
        : `<p class="text-secondary small">No teams configured.</p>`}
      ${agentsHtml}
      ${toolsHtml}
    </div>
  `;
}

// ---------------------------------------------------------------------------
// View: Conductor chat
// ---------------------------------------------------------------------------

async function renderConductorChat(configId) {
  setNav(
    `<a class="text-decoration-none text-secondary" href="#/">Home</a> /
     <a class="text-decoration-none text-secondary" href="#/conductor/${escHtml(configId)}">${escHtml(displayConfigId(configId))}</a> /
     Chat`
  );

  app.innerHTML = `
    <div class="container-fluid py-2" style="max-width: 1400px;">
      <div class="row g-3">

        <!-- Session sidebar -->
        <div class="col-12 col-md-3">
          <div class="card h-100">
            <div class="card-header d-flex align-items-center fw-semibold">
              <i class="bi bi-clock-history me-2"></i> Sessions
              <button class="btn btn-sm btn-outline-primary ms-auto" id="btn-new-chat">
                <i class="bi bi-plus-lg"></i> New
              </button>
            </div>
            <div class="card-body p-0">
              <div id="session-toast-slot"
                   role="status"
                   aria-live="polite"
                   aria-atomic="true"></div>
              <ul class="list-group list-group-flush" id="session-list">
                <li class="list-group-item text-secondary small py-1">Loading…</li>
              </ul>
            </div>
          </div>
        </div>

        <!-- Chat main -->
        <div class="col-12 col-md-9 d-flex flex-column" style="height: calc(100vh - 80px);">
          <div class="card flex-grow-1 d-flex flex-column">
            <div class="card-header fw-semibold d-flex align-items-center">
              <i class="bi bi-broadcast me-2 text-primary"></i>
              ${escHtml(displayConfigId(configId))}
              <span class="badge bg-dark text-light ms-auto me-2" id="context-usage-badge" style="display:none;">
                <i class="bi bi-speedometer2 me-1"></i><span id="context-usage-text"></span>
              </span>
              <span class="badge bg-secondary" id="session-id-badge">new session</span>
            </div>

            <!-- Messages -->
            <div class="card-body flex-grow-1 overflow-auto p-3 position-relative" id="chat-messages-wrapper">
              <div id="chat-messages"></div>
              <button
                id="scroll-to-bottom-btn"
                class="btn btn-primary btn-sm shadow"
                aria-label="New messages — scroll to bottom"
                aria-live="polite"
                tabindex="0"
              >
                <i class="bi bi-arrow-down-circle-fill me-1" aria-hidden="true"></i>New messages
              </button>
            </div>

            <!-- Input bar -->
            <div class="card-footer p-2">
              <div class="input-group">
                <textarea
                  class="form-control"
                  id="chat-input"
                  rows="2"
                  placeholder="Type a message… (Shift+Enter for newline)"
                  style="resize: none;"
                ></textarea>
                <button class="btn btn-success" id="btn-send" title="Send">
                  <i class="bi bi-send-fill"></i>
                </button>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>
  `;

  // --- State ---
  let currentSessionId   = null;
  let _toastDismissTimer = null;

  const chatMessages        = document.getElementById("chat-messages");
  const chatMessagesWrapper = document.getElementById("chat-messages-wrapper");
  const chatInput           = document.getElementById("chat-input");
  const btnSend             = document.getElementById("btn-send");
  const sessionIdBadge      = document.getElementById("session-id-badge");
  const sessionList         = document.getElementById("session-list");
  const sessionToastSlot    = document.getElementById("session-toast-slot");
  const scrollToBottomBtn   = document.getElementById("scroll-to-bottom-btn");

  // --- Session sidebar ---
  async function loadSessions() {
    try {
      const sessions = await apiFetch(`/api/sessions?config_id=${encodeURIComponent(configId)}`);
      const filtered = sessions.filter((s) => s.orchestration === "conductor");

      if (filtered.length === 0) {
        sessionList.innerHTML = `<li class="list-group-item text-secondary small py-1">No saved sessions</li>`;
        return;
      }

      sessionList.innerHTML = filtered.map((s) => {
        const isActive = s.id === currentSessionId;
        const shortId  = escHtml(s.id.slice(0, 8));
        const label    = `Session ${shortId}…, ${s.turn_count} turn${s.turn_count !== 1 ? "s" : ""}, updated ${escHtml(s.updated_at.slice(0, 16).replace("T", " "))}`;
        return `
          <li class="list-group-item list-group-item-action small py-2 d-flex align-items-start gap-1 ${isActive ? "active" : ""}"
              role="option"
              aria-selected="${isActive}"
              data-session-id="${escHtml(s.id)}">
            <div class="flex-grow-1" style="cursor:pointer; min-width:0;"
                 onclick="resumeSession('${escHtml(s.id)}')"
                 role="button"
                 aria-label="Resume ${label}">
              <div class="fw-semibold text-truncate">${shortId}…</div>
              <div class="${isActive ? "text-white-50" : "text-muted"}">${s.turn_count} turn${s.turn_count !== 1 ? "s" : ""} · ${escHtml(s.updated_at.slice(0, 16).replace("T", " "))}</div>
            </div>
            <button class="btn btn-sm session-delete-btn ${isActive ? "btn-outline-light" : "btn-outline-danger"} border-0 py-0 px-1 mt-1"
                    aria-label="Delete session ${shortId}"
                    title="Delete session"
                    onclick="promptDeleteSession('${escHtml(s.id)}', event)">
              <i class="bi bi-trash3" aria-hidden="true"></i>
            </button>
          </li>
        `;
      }).join("");
    } catch {}
  }

  // --- Delete session flow ---
  window.promptDeleteSession = function (sessionId, event) {
    if (event) event.stopPropagation();
    document.getElementById("delete-session-id-preview").textContent = sessionId.slice(0, 8) + "…";

    const modal = getModal("deleteSessionModal");

    const confirmBtn = document.getElementById("btn-confirm-delete-session");
    const newBtn = confirmBtn.cloneNode(true);
    confirmBtn.replaceWith(newBtn);

    newBtn.addEventListener("click", async () => {
      modal.hide();
      await executeDeleteSession(sessionId);
    });

    modal.show();
  };

  async function executeDeleteSession(sessionId) {
    try {
      await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });

      if (currentSessionId === sessionId) {
        currentSessionId = null;
        sessionIdBadge.textContent = "new session";
        chatMessages.innerHTML = "";
      }

      await loadSessions();
      showSessionToast("Session deleted.", "success");
    } catch (e) {
      showSessionToast(`Delete failed: ${e.message}`, "danger");
    }
  }

  function showSessionToast(message, variant = "success") {
    const slot = document.getElementById("session-toast-slot");
    if (!slot) return;

    if (_toastDismissTimer) {
      clearTimeout(_toastDismissTimer);
      _toastDismissTimer = null;
    }

    const icon = variant === "success"
      ? '<i class="bi bi-check-circle-fill me-1" aria-hidden="true"></i>'
      : variant === "danger"
        ? '<i class="bi bi-exclamation-triangle-fill me-1" aria-hidden="true"></i>'
        : "";

    slot.innerHTML = `
      <div class="alert alert-${escHtml(variant)} alert-dismissible py-1 px-2 mb-0 small"
           role="alert">
        ${icon}${escHtml(message)}
        <button type="button" class="btn-close btn-sm"
                aria-label="Dismiss"
                onclick="dismissSessionToast()"></button>
      </div>
    `;

    _toastDismissTimer = setTimeout(() => dismissSessionToast(), 3000);
  }

  function dismissSessionToast() {
    const slot = document.getElementById("session-toast-slot");
    if (slot) slot.innerHTML = "";
    if (_toastDismissTimer) {
      clearTimeout(_toastDismissTimer);
      _toastDismissTimer = null;
    }
  }

  window.dismissSessionToast = dismissSessionToast;

  window.resumeSession = async (sessionId) => {
    currentSessionId = sessionId;
    sessionIdBadge.textContent = sessionId.slice(0, 8) + "…";
    const ctxBadge = document.getElementById("context-usage-badge");
    if (ctxBadge) ctxBadge.style.display = "none";
    chatMessages.innerHTML = "";

    try {
      const history = await apiFetch(`/api/sessions/${sessionId}/history`);
      for (const turn of history.turns) {
        appendUserBubble(turn.user);
        const bubble = appendAssistantBubble();
        bubble.dataset.raw = turn.assistant;
        await renderMarkdown(turn.assistant, bubble.querySelector(".msg-bubble"));
        bubble.querySelector(".typing-cursor")?.classList.remove("typing-cursor");
        renderHistoricalActivity(bubble, turn.events || []);
        // Hide activity details if there are no tool items
        const details = bubble.querySelector("details");
        if (details) {
          const hasItems = details.querySelectorAll(".tool-activity li, .tool-collapsed-group li").length > 0;
          if (!hasItems) details.style.display = "none";
        }
      }
      if (history.pending_input) {
        const pendingPreview = history.pending_input.slice(0, 120) + (history.pending_input.length > 120 ? "…" : "");
        appendSystemMsg(`Last message didn't complete: "${pendingPreview}"`);
        chatInput.value = history.pending_input;
        chatInput.focus();
      }
      scrollToBottom();
    } catch (e) {
      appendSystemMsg(`Failed to load history: ${e.message}`);
    }

    await loadSessions();
  };

  document.getElementById("btn-new-chat").addEventListener("click", () => {
    currentSessionId = null;
    sessionIdBadge.textContent = "new session";
    const ctxBadge = document.getElementById("context-usage-badge");
    if (ctxBadge) ctxBadge.style.display = "none";
    chatMessages.innerHTML = "";
    loadSessions();
  });

  // --- Message rendering ---
  function appendUserBubble(text) {
    const div = document.createElement("div");
    div.className = "msg-user";
    div.innerHTML = `<div class="msg-bubble">${escHtml(text).replace(/\n/g, "<br>")}</div>`;
    chatMessages.appendChild(div);
    scrollToBottom();
    return div;
  }

  function appendAssistantBubble() {
    const div = document.createElement("div");
    div.className = "msg-assistant";
    div.dataset.raw = "";

    const collapseState = createToolCollapseState();
    const uid = collapseState.uid;

    div.innerHTML = `
      <div class="msg-bubble typing-cursor"></div>
      <details class="mt-1" style="max-width: 85%;">
        <summary class="text-secondary" style="font-size: 0.78rem; cursor: pointer;">
          <i class="bi bi-cpu"></i> Agent activity
        </summary>
        <div class="agent-activity mt-1"></div>
        <div class="tool-collapse-container mt-1">
          <button class="tool-collapse-toggle btn btn-sm btn-outline-secondary w-100 mb-1 text-start"
                  aria-expanded="false"
                  aria-controls="tool-collapsed-group-${uid}"
                  style="display: none;">
            <i class="bi bi-tools me-1" aria-hidden="true"></i>
            <span class="tool-collapse-label"></span>
          </button>
          <ul class="list-group list-group-flush tool-collapsed-group"
              id="tool-collapsed-group-${uid}"
              role="region"
              aria-label="Previous tool uses"
              style="display: none;">
          </ul>
          <ul class="list-group list-group-flush tool-activity"></ul>
        </div>
      </details>
    `;
    chatMessages.appendChild(div);

    collapseState.toggleBtn     = div.querySelector(".tool-collapse-toggle");
    collapseState.collapsedGroup = div.querySelector(".tool-collapsed-group");
    collapseState.recentGroup   = div.querySelector(".tool-activity");

    collapseState.toggleBtn.addEventListener("click", () => {
      toggleToolCollapse(collapseState);
    });

    div._toolCollapseState = collapseState;

    scrollToBottom();
    return div;
  }

  function appendSystemMsg(text) {
    const div = document.createElement("div");
    div.className = "text-center text-secondary small py-1";
    div.textContent = text;
    chatMessages.appendChild(div);
    scrollToBottom();
  }

  // --- Scroll management ---
  const SCROLL_THRESHOLD = 8;

  function isNearBottom() {
    const el = chatMessagesWrapper;
    return el.scrollTop + el.clientHeight >= el.scrollHeight - SCROLL_THRESHOLD;
  }

  function showNewMessagesBadge() {
    scrollToBottomBtn.classList.add("visible");
  }

  function hideNewMessagesBadge() {
    scrollToBottomBtn.classList.remove("visible");
  }

  let _scrollPending = false;
  function scrollToBottom() {
    hideNewMessagesBadge();
    if (_scrollPending) return;
    _scrollPending = true;
    requestAnimationFrame(() => {
      chatMessagesWrapper.scrollTop = chatMessagesWrapper.scrollHeight;
      _scrollPending = false;
    });
  }

  let _smartScrollPending = false;
  function maybeScrollToBottom() {
    if (isNearBottom()) {
      hideNewMessagesBadge();
      if (_smartScrollPending) return;
      _smartScrollPending = true;
      requestAnimationFrame(() => {
        chatMessagesWrapper.scrollTop = chatMessagesWrapper.scrollHeight;
        _smartScrollPending = false;
      });
    } else {
      showNewMessagesBadge();
    }
  }

  scrollToBottomBtn.addEventListener("click", () => {
    scrollToBottom();
  });
  scrollToBottomBtn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      scrollToBottom();
    }
  });

  chatMessagesWrapper.addEventListener("scroll", () => {
    if (isNearBottom()) {
      hideNewMessagesBadge();
    }
  }, { passive: true });

  // --- Send ---
  async function sendMessage() {
    const text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = "";
    btnSend.disabled = true;
    chatInput.disabled = true;

    appendUserBubble(text);
    const assistantDiv = appendAssistantBubble();
    assistantDiv.style.display = "none"; // hidden until done; agents stream as top-level bubbles

    const bubble = assistantDiv.querySelector(".msg-bubble");

    const ctx = {
      agentContainer: chatMessages,
      agentBubbleMode: true,
      scrollFn: maybeScrollToBottom,
      toolList: assistantDiv.querySelector(".tool-activity"),
      toolCollapseState: assistantDiv._toolCollapseState,
      onDone: async (event) => {
        const responseText = event.response || assistantDiv.dataset.raw || "";
        // If any agent bubble already holds this content, promote it as the
        // final answer and drop the now-empty assistantDiv.
        let matchingAgent = null;
        for (const div of (ctx.allAgentDivs || [])) {
          if ((div.dataset.raw || "").trim() === responseText.trim()) {
            matchingAgent = div;
          }
        }
        if (matchingAgent) {
          matchingAgent.classList.add("msg-agent-final");
          assistantDiv.remove();
        } else if (responseText) {
          // Distinct synthesised response — show in main bubble.
          assistantDiv.style.display = "";
          const details = assistantDiv.querySelector("details");
          if (details) {
            const toolItems = details.querySelectorAll(".tool-activity li, .tool-collapsed-group li");
            if (toolItems.length === 0) {
              details.style.display = "none";
            } else {
              details.querySelector("summary").innerHTML = '<i class="bi bi-tools"></i> Tool activity';
            }
          }
          bubble.classList.remove("typing-cursor");
          await renderMarkdown(responseText, bubble);
          assistantDiv.dataset.raw = responseText;
        } else {
          assistantDiv.remove();
        }
        if (event.session_id) {
          currentSessionId = event.session_id;
          sessionIdBadge.textContent = event.session_id.slice(0, 8) + "…";
        }
        btnSend.disabled = false;
        chatInput.disabled = false;
        chatInput.focus();
        scrollToBottom();
        await loadSessions();
      },
      onUsageUpdate: (event) => {
        const ctxBadge = document.getElementById("context-usage-badge");
        const ctxText = document.getElementById("context-usage-text");
        if (ctxBadge && ctxText) {
          const total = (event.input_tokens || 0) + (event.output_tokens || 0);
          if (total > 0) {
            ctxText.textContent = `${formatTokenCount(event.input_tokens || 0)} in / ${formatTokenCount(event.output_tokens || 0)} out`;
            ctxBadge.style.display = "";
          }
        }
      },
      onError: (msg) => {
        assistantDiv.style.display = "";
        bubble.innerHTML =
          `<span class="text-danger"><i class="bi bi-exclamation-triangle"></i> ${escHtml(msg)}</span>`;
        bubble.classList.remove("typing-cursor");
        btnSend.disabled = false;
        chatInput.disabled = false;
      },
    };

    try {
      const response = await fetch(
        `/api/conductor-chat/${encodeURIComponent(configId)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: currentSessionId, message: text }),
        }
      );

      for await (const event of readSSE(response)) {
        await handleSSEEvent(event, ctx);
      }
    } catch (e) {
      if (ctx.onError) ctx.onError(e.message);
    }
  }

  btnSend.addEventListener("click", sendMessage);
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // --- Init ---
  await loadSessions();
  chatInput.focus();
}

// ---------------------------------------------------------------------------
// View: Config detail
// ---------------------------------------------------------------------------

async function renderConfig(configId) {
  setNav(`<a class="text-decoration-none text-secondary" href="#/">Home</a> / ${escHtml(displayConfigId(configId))}`);
  app.innerHTML = `<div class="text-center text-secondary py-5"><div class="spinner-border" role="status"></div></div>`;

  let config;
  try {
    config = await apiFetch(`/api/configs/${encodeURIComponent(configId)}`);
  } catch (e) {
    app.innerHTML = `<div class="alert alert-danger m-3">${escHtml(e.message)}</div>`;
    return;
  }

  const safeId = configId.replace(/[^A-Za-z0-9_-]/g, "_");

  const rows = config.orchestrations.map((o, i) => {
    const modeClass = o.mode === "conversational" ? "bg-success" : "bg-primary";
    const modeIcon = o.mode === "conversational" ? "bi-chat-dots" : "bi-play-circle";
    const actionBtn = o.mode === "conversational"
      ? `<button class="btn btn-sm btn-success" onclick="nav('chat/${escHtml(configId)}/${encodeURIComponent(o.name)}')">
           <i class="bi bi-chat-dots"></i> Chat
         </button>`
      : `<button class="btn btn-sm btn-primary" onclick="nav('run/${escHtml(configId)}/${encodeURIComponent(o.name)}')">
           <i class="bi bi-play-fill"></i> Run
         </button>`;

    const inputBadges = Object.keys(o.input).map((k) =>
      `<span class="badge bg-secondary me-1">${escHtml(k)}</span>`
    ).join("");

    const diagramId = `diagram-${safeId}-${i}`;

    return `
      <div class="col-12">
        <div class="card">
          <div class="card-body">
            <div class="d-flex align-items-start gap-3">
              <div class="flex-grow-1">
                <div class="d-flex align-items-center gap-2 mb-1">
                  <h5 class="mb-0">${escHtml(o.name)}</h5>
                  <span class="badge ${modeClass}">
                    <i class="bi ${modeIcon}"></i> ${escHtml(o.mode)}
                  </span>
                </div>
                ${o.description ? `<p class="text-secondary mb-1 small">${escHtml(o.description)}</p>` : ""}
                <div class="text-secondary small">
                  <i class="bi bi-people"></i> ${o.agent_count} agent${o.agent_count !== 1 ? "s" : ""}
                  &nbsp;
                  <i class="bi bi-tools"></i> ${o.tool_count} tool${o.tool_count !== 1 ? "s" : ""}
                  ${inputBadges ? `&nbsp; <i class="bi bi-input-cursor"></i> inputs: ${inputBadges}` : ""}
                </div>
              </div>
              <div class="d-flex gap-2 flex-wrap justify-content-end">
                <button class="btn btn-sm btn-outline-secondary"
                        data-bs-toggle="collapse"
                        data-bs-target="#${diagramId}"
                        aria-expanded="false"
                        aria-controls="${diagramId}">
                  <i class="bi bi-diagram-3"></i> Flow Diagram
                </button>
                ${actionBtn}
              </div>
            </div>
          </div>
          <div id="${diagramId}" class="collapse">
            <div class="card-body border-top pt-3">
              <div class="mermaid-container text-center text-secondary small py-2"
                   data-config-id="${escHtml(configId)}"
                   data-orch-name="${escHtml(o.name)}">
                <div class="spinner-border spinner-border-sm me-1" role="status"></div> Loading diagram…
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
  }).join("");

  const agentsHtml = config.agents && config.agents.length > 0 ? `
    <h5 class="mt-4 mb-2"><i class="bi bi-people me-2"></i>Agents</h5>
    <div class="accordion mb-3" id="agents-accordion-${safeId}">
      ${config.agents.map((a, i) => `
        <div class="accordion-item">
          <h2 class="accordion-header">
            <button class="accordion-button collapsed py-2" type="button"
                    data-bs-toggle="collapse"
                    data-bs-target="#agent-${safeId}-${i}"
                    aria-expanded="false"
                    aria-controls="agent-${safeId}-${i}">
              <span class="fw-semibold me-2">${escHtml(a.name)}</span>
              <span class="badge bg-secondary font-monospace fw-normal me-2">${escHtml(a.model)}</span>
              ${a.tools.length ? `<span class="text-muted small">${a.tools.length} tool${a.tools.length !== 1 ? "s" : ""}</span>` : ""}
            </button>
          </h2>
          <div id="agent-${safeId}-${i}" class="accordion-collapse collapse"
               data-bs-parent="#agents-accordion-${safeId}">
            <div class="accordion-body py-2">
              <div class="mb-2">
                <span class="text-muted small d-block mb-1">System prompt</span>
                <pre class="small bg-light p-2 rounded mb-0" style="white-space: pre-wrap; max-height: 300px; overflow-y: auto;">${escHtml(a.system_prompt)}</pre>
              </div>
              ${a.tools.length ? `
                <div class="mt-2">
                  <span class="text-muted small">Tools: </span>
                  ${a.tools.map((t) => `<span class="badge bg-secondary me-1">${escHtml(t)}</span>`).join("")}
                </div>` : ""}
              ${a.can_spawn.length ? `
                <div class="mt-1">
                  <span class="text-muted small">Spawnable: </span>
                  ${a.can_spawn.map((s) => `<span class="badge bg-info text-dark me-1">${escHtml(s)}</span>`).join("")}
                </div>` : ""}
            </div>
          </div>
        </div>
      `).join("")}
    </div>
  ` : "";

  const toolsHtml = config.tools && config.tools.length > 0 ? `
    <h5 class="mt-4 mb-2"><i class="bi bi-tools me-2"></i>Tools</h5>
    <div class="table-responsive mb-3">
      <table class="table table-sm table-bordered small mb-0">
        <thead class="table-light">
          <tr><th>Name</th><th>Kind</th><th>Description</th></tr>
        </thead>
        <tbody>
          ${config.tools.map((t) => `
            <tr>
              <td class="font-monospace">${escHtml(t.name)}</td>
              <td>${t.builtin
                ? `<span class="badge bg-primary">${escHtml(t.builtin)}</span>`
                : `<span class="badge bg-secondary">custom</span>`}</td>
              <td class="text-muted">${t.description ? escHtml(t.description) : ""}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  ` : "";

  app.innerHTML = `
    <div class="container py-3">
      <nav aria-label="breadcrumb">
        <ol class="breadcrumb">
          <li class="breadcrumb-item"><a href="#/">Home</a></li>
          <li class="breadcrumb-item active">${escHtml(displayConfigId(configId))}</li>
        </ol>
      </nav>
      <h4 class="mb-3">${escHtml(config.filename)}</h4>
      <div class="row g-3">${rows}</div>
      ${agentsHtml}
      ${toolsHtml}
    </div>
  `;

  // Wire up lazy diagram loading: fetch + render mermaid on first collapse open.
  app.querySelectorAll('[id^="diagram-"]').forEach((collapseEl) => {
    collapseEl.addEventListener("show.bs.collapse", async function () {
      const container = this.querySelector(".mermaid-container");
      if (!container || container.dataset.loaded) return;
      container.dataset.loaded = "true";

      const cid = container.dataset.configId;
      const orchName = container.dataset.orchName;
      try {
        const data = await apiFetch(
          `/api/configs/${encodeURIComponent(cid)}/diagram/${encodeURIComponent(orchName)}`
        );
        const mermaidDiv = document.createElement("div");
        mermaidDiv.className = "mermaid";
        mermaidDiv.textContent = data.mermaid;
        container.innerHTML = "";
        container.style.textAlign = "";
        container.appendChild(mermaidDiv);
        await mermaid.run({ nodes: [mermaidDiv] });
      } catch (e) {
        container.innerHTML = `<div class="alert alert-warning small mb-0 py-1">Could not generate diagram: ${escHtml(e.message)}</div>`;
        container.style.textAlign = "";
      }
    });
  });
}

// ---------------------------------------------------------------------------
// View: Batch run
// ---------------------------------------------------------------------------

async function renderRun(configId, orchName) {
  setNav(
    `<a class="text-decoration-none text-secondary" href="#/">Home</a> /
     <a class="text-decoration-none text-secondary" href="#/config/${escHtml(configId)}">${escHtml(displayConfigId(configId))}</a> /
     ${escHtml(orchName)}`
  );

  let config;
  try {
    config = await apiFetch(`/api/configs/${encodeURIComponent(configId)}`);
  } catch (e) {
    app.innerHTML = `<div class="alert alert-danger m-3">${escHtml(e.message)}</div>`;
    return;
  }

  const orch = config.orchestrations.find((o) => o.name === orchName);
  if (!orch) {
    app.innerHTML = `<div class="alert alert-danger m-3">Orchestration '${escHtml(orchName)}' not found.</div>`;
    return;
  }

  // Build input form fields.
  const inputFields = Object.entries(orch.input).map(([key, type]) => `
    <div class="mb-3">
      <label class="form-label fw-semibold">${escHtml(key)} <span class="badge bg-secondary">${escHtml(type)}</span></label>
      <textarea class="form-control font-monospace" id="input-${escHtml(key)}" rows="3" placeholder="${escHtml(key)}…"></textarea>
    </div>
  `).join("");

  app.innerHTML = `
    <div class="container py-3">
      <nav aria-label="breadcrumb">
        <ol class="breadcrumb">
          <li class="breadcrumb-item"><a href="#/">Home</a></li>
          <li class="breadcrumb-item"><a href="#/config/${escHtml(configId)}">${escHtml(displayConfigId(configId))}</a></li>
          <li class="breadcrumb-item active">${escHtml(orchName)}</li>
        </ol>
      </nav>

      <div class="row g-3">
        <!-- Input form -->
        <div class="col-12 col-lg-4">
          <div class="card">
            <div class="card-header fw-semibold">
              <i class="bi bi-input-cursor-text"></i> Inputs
            </div>
            <div class="card-body">
              ${inputFields || `<p class="text-secondary mb-0 small">No inputs required.</p>`}
              <button class="btn btn-primary w-100" id="btn-run">
                <i class="bi bi-play-fill"></i> Run
              </button>
            </div>
          </div>
        </div>

        <!-- Output -->
        <div class="col-12 col-lg-8">
          <div class="card">
            <div class="card-header d-flex align-items-center gap-2 fw-semibold">
              <i class="bi bi-terminal"></i> Output
              <div class="ms-auto" id="run-status"></div>
            </div>
            <div class="card-body" id="run-output">
              <p class="text-secondary small">Press <strong>Run</strong> to start.</p>
            </div>
          </div>

          <!-- Tool log (hidden until first tool call) -->
          <details class="mt-2 d-none" id="tool-log-details">
            <summary class="text-secondary small mb-1">
              <i class="bi bi-tools"></i> Tool calls
            </summary>
            <ul class="list-group list-group-flush" id="tool-log"></ul>
          </details>
        </div>
      </div>
    </div>
  `;

  const outputDiv = document.getElementById("run-output");
  const statusDiv = document.getElementById("run-status");
  const toolLogDetails = document.getElementById("tool-log-details");
  const toolLog = document.getElementById("tool-log");

  document.getElementById("btn-run").addEventListener("click", async () => {
    // Collect input values.
    const inputData = {};
    for (const key of Object.keys(orch.input)) {
      inputData[key] = document.getElementById(`input-${key}`)?.value || "";
    }

    // Reset output.
    outputDiv.innerHTML = "";
    toolLog.innerHTML = "";
    toolLogDetails.classList.add("d-none");
    statusDiv.innerHTML = `<div class="spinner-border spinner-sm text-primary" role="status"></div>`;
    document.getElementById("btn-run").disabled = true;

    // Wire up SSE context.
    const ctx = {
      agentContainer: outputDiv,
      toolList: toolLog,
      onDone: async (event) => {
        statusDiv.innerHTML = `<span class="badge bg-success"><i class="bi bi-check-lg"></i> Done</span>`;
        document.getElementById("btn-run").disabled = false;

        // If there's a plain result (non-agent output), show it.
        if (event.result && Object.keys(ctx.agentDivs || {}).length === 0) {
          const resultEl = document.createElement("div");
          resultEl.className = "mt-2";
          await renderMarkdown(JSON.stringify(event.result, null, 2), resultEl);
          outputDiv.appendChild(resultEl);
        }
      },
      onError: (msg) => {
        statusDiv.innerHTML = `<span class="badge bg-danger"><i class="bi bi-x-lg"></i> Error</span>`;
        document.getElementById("btn-run").disabled = false;
        const errEl = document.createElement("div");
        errEl.className = "alert alert-danger mt-2 small";
        errEl.textContent = msg;
        outputDiv.appendChild(errEl);
      },
    };

    // Monitor tool-log visibility.
    const origAppendToolItem = ctx.toolList;
    if (origAppendToolItem) {
      const observer = new MutationObserver(() => {
        if (toolLog.children.length > 0) toolLogDetails.classList.remove("d-none");
      });
      observer.observe(toolLog, { childList: true });
    }

    try {
      const response = await fetch(
        `/api/run/${encodeURIComponent(configId)}/${encodeURIComponent(orchName)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ input: inputData }),
        }
      );

      for await (const event of readSSE(response)) {
        await handleSSEEvent(event, ctx);
      }
    } catch (e) {
      if (ctx.onError) ctx.onError(e.message);
    }
  });
}

// ---------------------------------------------------------------------------
// View: Chat
// ---------------------------------------------------------------------------

async function renderChat(configId, orchName) {
  setNav(
    `<a class="text-decoration-none text-secondary" href="#/">Home</a> /
     <a class="text-decoration-none text-secondary" href="#/config/${escHtml(configId)}">${escHtml(displayConfigId(configId))}</a> /
     ${escHtml(orchName)}`
  );

  app.innerHTML = `
    <div class="container-fluid py-2" style="max-width: 1400px;">
      <div class="row g-3">

        <!-- Session sidebar -->
        <div class="col-12 col-md-3">
          <div class="card h-100">
            <div class="card-header d-flex align-items-center fw-semibold">
              <i class="bi bi-clock-history me-2"></i> Sessions
              <button class="btn btn-sm btn-outline-primary ms-auto" id="btn-new-chat">
                <i class="bi bi-plus-lg"></i> New
              </button>
            </div>
            <div class="card-body p-0">
              <div id="session-toast-slot"
                   role="status"
                   aria-live="polite"
                   aria-atomic="true"></div>
              <ul class="list-group list-group-flush" id="session-list">
                <li class="list-group-item text-secondary small py-1">Loading…</li>
              </ul>
            </div>
          </div>
        </div>

        <!-- Chat main -->
        <div class="col-12 col-md-9 d-flex flex-column" style="height: calc(100vh - 80px);">
          <div class="card flex-grow-1 d-flex flex-column">
            <div class="card-header fw-semibold d-flex align-items-center">
              <i class="bi bi-chat-dots me-2 text-success"></i>
              ${escHtml(orchName)}
              <span class="badge bg-dark text-light ms-auto me-2" id="context-usage-badge" style="display:none;">
                <i class="bi bi-speedometer2 me-1"></i><span id="context-usage-text"></span>
              </span>
              <span class="badge bg-secondary" id="session-id-badge">new session</span>
            </div>

            <!-- Messages -->
            <div class="card-body flex-grow-1 overflow-auto p-3 position-relative" id="chat-messages-wrapper">
              <div id="chat-messages"></div>
              <!-- US-005: "New messages" scroll-to-bottom affordance -->
              <button
                id="scroll-to-bottom-btn"
                class="btn btn-primary btn-sm shadow"
                aria-label="New messages — scroll to bottom"
                aria-live="polite"
                tabindex="0"
              >
                <i class="bi bi-arrow-down-circle-fill me-1" aria-hidden="true"></i>New messages
              </button>
            </div>

            <!-- Input bar -->
            <div class="card-footer p-2">
              <div class="input-group">
                <textarea
                  class="form-control"
                  id="chat-input"
                  rows="2"
                  placeholder="Type a message… (Shift+Enter for newline)"
                  style="resize: none;"
                ></textarea>
                <button class="btn btn-success" id="btn-send" title="Send">
                  <i class="bi bi-send-fill"></i>
                </button>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>
  `;

  // --- State ---
  let currentSessionId   = null;
  let _toastDismissTimer = null;  // US-006: tracks auto-dismiss timer for session toast

  const chatMessages        = document.getElementById("chat-messages");
  const chatMessagesWrapper = document.getElementById("chat-messages-wrapper");
  const chatInput           = document.getElementById("chat-input");
  const btnSend             = document.getElementById("btn-send");
  const sessionIdBadge      = document.getElementById("session-id-badge");
  const sessionList         = document.getElementById("session-list");
  const sessionToastSlot    = document.getElementById("session-toast-slot");  // US-006
  const scrollToBottomBtn   = document.getElementById("scroll-to-bottom-btn");
  const contextUsageBadge   = document.getElementById("context-usage-badge");
  const contextUsageText    = document.getElementById("context-usage-text");

  // --- Session sidebar ---
  async function loadSessions() {
    try {
      const sessions = await apiFetch(`/api/sessions?config_id=${encodeURIComponent(configId)}`);
      const filtered = sessions.filter((s) => s.orchestration === orchName);

      if (filtered.length === 0) {
        sessionList.innerHTML = `<li class="list-group-item text-secondary small py-1">No saved sessions</li>`;
        return;
      }

      sessionList.innerHTML = filtered.map((s) => {
        const isActive = s.id === currentSessionId;
        const shortId  = escHtml(s.id.slice(0, 8));
        const label    = `Session ${shortId}…, ${s.turn_count} turn${s.turn_count !== 1 ? "s" : ""}, updated ${escHtml(s.updated_at.slice(0, 16).replace("T", " "))}`;
        return `
          <li class="list-group-item list-group-item-action small py-2 d-flex align-items-start gap-1 ${isActive ? "active" : ""}"
              role="option"
              aria-selected="${isActive}"
              data-session-id="${escHtml(s.id)}">
            <div class="flex-grow-1" style="cursor:pointer; min-width:0;"
                 onclick="resumeSession('${escHtml(s.id)}')"
                 role="button"
                 aria-label="Resume ${label}">
              <div class="fw-semibold text-truncate">${shortId}…</div>
              <div class="${isActive ? "text-white-50" : "text-muted"}">${s.turn_count} turn${s.turn_count !== 1 ? "s" : ""} · ${escHtml(s.updated_at.slice(0, 16).replace("T", " "))}</div>
            </div>
            <button class="btn btn-sm session-delete-btn ${isActive ? "btn-outline-light" : "btn-outline-danger"} border-0 py-0 px-1 mt-1"
                    aria-label="Delete session ${shortId}"
                    title="Delete session"
                    onclick="promptDeleteSession('${escHtml(s.id)}', event)">
              <i class="bi bi-trash3" aria-hidden="true"></i>
            </button>
          </li>
        `;
      }).join("");
    } catch {}
  }

  // --- Delete session flow ---

  /**
   * Open the confirmation modal for a session deletion.
   * `event` is passed to stop click-propagation (don't also trigger resumeSession).
   */
  window.promptDeleteSession = function (sessionId, event) {
    if (event) event.stopPropagation();
    document.getElementById("delete-session-id-preview").textContent = sessionId.slice(0, 8) + "…";

    const modal = getModal("deleteSessionModal");

    // Wire up the confirm button (replace any previous listener).
    const confirmBtn = document.getElementById("btn-confirm-delete-session");
    const newBtn = confirmBtn.cloneNode(true); // remove stale listeners
    confirmBtn.replaceWith(newBtn);

    newBtn.addEventListener("click", async () => {
      modal.hide();
      await executeDeleteSession(sessionId);
    });

    modal.show();
  };

  /**
   * Call the API, update UI state, and surface errors if the request fails.
   * AC-4: if the deleted session is the active one, revert to "new session".
   * AC-3: show a brief success toast after deletion.
   * AC-6: show an error alert (non-destructive) on API failure.
   */
  async function executeDeleteSession(sessionId) {
    try {
      await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });

      // AC-4 — reset to new session if the active one was just deleted.
      if (currentSessionId === sessionId) {
        currentSessionId = null;
        sessionIdBadge.textContent = "new session";
        const ctxBadge = document.getElementById("context-usage-badge");
        if (ctxBadge) ctxBadge.style.display = "none";
        chatMessages.innerHTML = "";
      }

      // AC-3 — refresh the list and show a transient success notification.
      await loadSessions();
      showSessionToast("Session deleted.", "success");

    } catch (e) {
      // AC-6 — surface the error without wiping the session list.
      showSessionToast(`Delete failed: ${e.message}`, "danger");
    }
  }

  /**
   * Display a small, auto-dismissing toast inside the reserved #session-toast-slot.
   * Replaces any existing toast in-place — no layout shift, no stacking (AC-1/2/3).
   * US-006: toast renders within the pre-reserved fixed-height area.
   *
   * @param {string} message                    - Human-readable text to display.
   * @param {"success"|"danger"|string} variant - Bootstrap alert variant.
   */
  function showSessionToast(message, variant = "success") {
    const slot = document.getElementById("session-toast-slot");
    if (!slot) return;

    // Cancel any pending auto-dismiss from a previous toast (AC-3: rapid deletions).
    if (_toastDismissTimer) {
      clearTimeout(_toastDismissTimer);
      _toastDismissTimer = null;
    }

    // Choose icon based on variant (AC-4: success uses green check icon).
    const icon = variant === "success"
      ? '<i class="bi bi-check-circle-fill me-1" aria-hidden="true"></i>'
      : variant === "danger"
        ? '<i class="bi bi-exclamation-triangle-fill me-1" aria-hidden="true"></i>'
        : "";

    // Replace slot content in-place — no DOM insertion/removal outside the slot
    // ensures zero Cumulative Layout Shift on appear or dismiss (AC-1, AC-2).
    slot.innerHTML = `
      <div class="alert alert-${escHtml(variant)} alert-dismissible py-1 px-2 mb-0 small"
           role="alert">
        ${icon}${escHtml(message)}
        <button type="button" class="btn-close btn-sm"
                aria-label="Dismiss"
                onclick="dismissSessionToast()"></button>
      </div>
    `;

    // Auto-dismiss after 3 s — preserving prior timing (AC-5).
    _toastDismissTimer = setTimeout(() => dismissSessionToast(), 3000);
  }

  /**
   * Clear the toast from the reserved slot without layout shift.
   * The slot element remains in the DOM, preserving its fixed min-height (AC-2).
   */
  function dismissSessionToast() {
    const slot = document.getElementById("session-toast-slot");
    if (slot) slot.innerHTML = "";
    if (_toastDismissTimer) {
      clearTimeout(_toastDismissTimer);
      _toastDismissTimer = null;
    }
  }

  // Expose on window so the inline onclick handler in the close button can reach it.
  // Follows the existing convention used by promptDeleteSession and resumeSession.
  window.dismissSessionToast = dismissSessionToast;

  window.resumeSession = async (sessionId) => {
    currentSessionId = sessionId;
    sessionIdBadge.textContent = sessionId.slice(0, 8) + "…";
    const ctxBadge = document.getElementById("context-usage-badge");
    if (ctxBadge) ctxBadge.style.display = "none";
    chatMessages.innerHTML = "";

    try {
      const history = await apiFetch(`/api/sessions/${sessionId}/history`);
      for (const turn of history.turns) {
        appendUserBubble(turn.user);
        const bubble = appendAssistantBubble();
        bubble.dataset.raw = turn.assistant;
        await renderMarkdown(turn.assistant, bubble.querySelector(".msg-bubble"));
        bubble.querySelector(".typing-cursor")?.classList.remove("typing-cursor");
        renderHistoricalActivity(bubble, turn.events || []);
        // Hide activity details if there are no tool items
        const details = bubble.querySelector("details");
        if (details) {
          const hasItems = details.querySelectorAll(".tool-activity li, .tool-collapsed-group li").length > 0;
          if (!hasItems) details.style.display = "none";
        }
      }
      if (history.pending_input) {
        const pendingPreview = history.pending_input.slice(0, 120) + (history.pending_input.length > 120 ? "…" : "");
        appendSystemMsg(`Last message didn't complete: "${pendingPreview}"`);
        chatInput.value = history.pending_input;
        chatInput.focus();
      }
      scrollToBottom();
    } catch (e) {
      appendSystemMsg(`Failed to load history: ${e.message}`);
    }

    await loadSessions();
  };

  document.getElementById("btn-new-chat").addEventListener("click", () => {
    currentSessionId = null;
    sessionIdBadge.textContent = "new session";
    const ctxBadge = document.getElementById("context-usage-badge");
    if (ctxBadge) ctxBadge.style.display = "none";
    chatMessages.innerHTML = "";
    loadSessions();
  });

  // --- Message rendering ---
  function appendUserBubble(text) {
    const div = document.createElement("div");
    div.className = "msg-user";
    div.innerHTML = `<div class="msg-bubble">${escHtml(text).replace(/\n/g, "<br>")}</div>`;
    chatMessages.appendChild(div);
    scrollToBottom();
    return div;
  }

  function appendAssistantBubble() {
    const div = document.createElement("div");
    div.className = "msg-assistant";
    div.dataset.raw = "";

    // Create a fresh per-thread collapse state (AC-6: independent per bubble).
    const collapseState = createToolCollapseState();
    const uid = collapseState.uid;

    div.innerHTML = `
      <div class="msg-bubble typing-cursor"></div>
      <details class="mt-1" style="max-width: 85%;">
        <summary class="text-secondary" style="font-size: 0.78rem; cursor: pointer;">
          <i class="bi bi-cpu"></i> Agent activity
        </summary>
        <div class="agent-activity mt-1"></div>
        <div class="tool-collapse-container mt-1">
          <button class="tool-collapse-toggle btn btn-sm btn-outline-secondary w-100 mb-1 text-start"
                  aria-expanded="false"
                  aria-controls="tool-collapsed-group-${uid}"
                  style="display: none;">
            <i class="bi bi-tools me-1" aria-hidden="true"></i>
            <span class="tool-collapse-label"></span>
          </button>
          <ul class="list-group list-group-flush tool-collapsed-group"
              id="tool-collapsed-group-${uid}"
              role="region"
              aria-label="Previous tool uses"
              style="display: none;">
          </ul>
          <ul class="list-group list-group-flush tool-activity"></ul>
        </div>
      </details>
    `;
    chatMessages.appendChild(div);

    // Cache DOM references on the state object for use in collapse helpers.
    collapseState.toggleBtn     = div.querySelector(".tool-collapse-toggle");
    collapseState.collapsedGroup = div.querySelector(".tool-collapsed-group");
    collapseState.recentGroup   = div.querySelector(".tool-activity");

    // Wire the click handler for mouse and touch (AC-3 / AC-4).
    collapseState.toggleBtn.addEventListener("click", () => {
      toggleToolCollapse(collapseState);
    });

    // Store on the DOM node for retrieval in sendMessage() (AC-6).
    div._toolCollapseState = collapseState;

    scrollToBottom();
    return div;
  }

  function appendSystemMsg(text) {
    const div = document.createElement("div");
    div.className = "text-center text-secondary small py-1";
    div.textContent = text;
    chatMessages.appendChild(div);
    scrollToBottom();
  }

  // ── US-005: Scroll position management ────────────────────────────────
  //
  // Threshold (px): the user is considered "at the bottom" when the
  // distance between the current scroll position and the maximum scroll
  // position is within this value.  ≤8 px satisfies AC-1.
  const SCROLL_THRESHOLD = 8;

  /** Returns true when the user is at (or within threshold of) the bottom. */
  function isNearBottom() {
    const el = chatMessagesWrapper;
    return el.scrollTop + el.clientHeight >= el.scrollHeight - SCROLL_THRESHOLD;
  }

  /** Show the "New messages" affordance badge. */
  function showNewMessagesBadge() {
    scrollToBottomBtn.classList.add("visible");
  }

  /** Hide the "New messages" affordance badge. */
  function hideNewMessagesBadge() {
    scrollToBottomBtn.classList.remove("visible");
  }

  /**
   * Scroll unconditionally to the bottom (used for user-initiated events:
   * sending a message, loading history, session reset).
   * AC-3: own sent messages always scroll into view.
   */
  let _scrollPending = false;
  function scrollToBottom() {
    hideNewMessagesBadge();
    if (_scrollPending) return;
    _scrollPending = true;
    requestAnimationFrame(() => {
      chatMessagesWrapper.scrollTop = chatMessagesWrapper.scrollHeight;
      _scrollPending = false;
    });
  }

  /**
   * Scroll to bottom only when the user is already near the bottom.
   * If they have scrolled up, show the "New messages" badge instead.
   * AC-1, AC-2: called on every incoming token during live streaming.
   */
  let _smartScrollPending = false;
  function maybeScrollToBottom() {
    if (isNearBottom()) {
      hideNewMessagesBadge();
      if (_smartScrollPending) return;
      _smartScrollPending = true;
      requestAnimationFrame(() => {
        chatMessagesWrapper.scrollTop = chatMessagesWrapper.scrollHeight;
        _smartScrollPending = false;
      });
    } else {
      // AC-4: user has scrolled up — surface the badge affordance.
      showNewMessagesBadge();
    }
  }

  // AC-4 / AC-5: clicking (or keyboard-activating) the badge scrolls to bottom.
  scrollToBottomBtn.addEventListener("click", () => {
    scrollToBottom();
  });
  scrollToBottomBtn.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      scrollToBottom();
    }
  });

  // Hide the badge as soon as the user manually scrolls back to the bottom.
  chatMessagesWrapper.addEventListener("scroll", () => {
    if (isNearBottom()) {
      hideNewMessagesBadge();
    }
  }, { passive: true });
  // ── End US-005 scroll management ──────────────────────────────────────

  // --- Send ---
  async function sendMessage() {
    const text = chatInput.value.trim();
    if (!text) return;
    chatInput.value = "";
    btnSend.disabled = true;
    chatInput.disabled = true;

    appendUserBubble(text);
    const assistantDiv = appendAssistantBubble();
    assistantDiv.style.display = "none"; // hidden until done; agents stream as top-level bubbles

    const bubble = assistantDiv.querySelector(".msg-bubble");

    const ctx = {
      agentContainer: chatMessages,
      agentBubbleMode: true,
      scrollFn: maybeScrollToBottom,
      toolList: assistantDiv.querySelector(".tool-activity"),
      toolCollapseState: assistantDiv._toolCollapseState,
      onDone: async (event) => {
        const responseText = event.response || assistantDiv.dataset.raw || "";
        // If any agent bubble already holds this content, promote it as the
        // final answer and drop the now-empty assistantDiv.
        let matchingAgent = null;
        for (const div of (ctx.allAgentDivs || [])) {
          if ((div.dataset.raw || "").trim() === responseText.trim()) {
            matchingAgent = div;
          }
        }
        if (matchingAgent) {
          matchingAgent.classList.add("msg-agent-final");
          assistantDiv.remove();
        } else if (responseText) {
          // Distinct synthesised response — show in main bubble.
          assistantDiv.style.display = "";
          const details = assistantDiv.querySelector("details");
          if (details) {
            const toolItems = details.querySelectorAll(".tool-activity li, .tool-collapsed-group li");
            if (toolItems.length === 0) {
              details.style.display = "none";
            } else {
              details.querySelector("summary").innerHTML = '<i class="bi bi-tools"></i> Tool activity';
            }
          }
          bubble.classList.remove("typing-cursor");
          await renderMarkdown(responseText, bubble);
          assistantDiv.dataset.raw = responseText;
        } else {
          assistantDiv.remove();
        }
        if (event.session_id) {
          currentSessionId = event.session_id;
          sessionIdBadge.textContent = event.session_id.slice(0, 8) + "…";
        }
        btnSend.disabled = false;
        chatInput.disabled = false;
        chatInput.focus();
        scrollToBottom();
        await loadSessions();
      },
      onUsageUpdate: (event) => {
        const total = (event.input_tokens || 0) + (event.output_tokens || 0);
        if (total > 0) {
          contextUsageText.textContent = `${formatTokenCount(event.input_tokens || 0)} in / ${formatTokenCount(event.output_tokens || 0)} out`;
          contextUsageBadge.style.display = "";
        }
      },
      onError: (msg) => {
        assistantDiv.style.display = "";
        bubble.innerHTML =
          `<span class="text-danger"><i class="bi bi-exclamation-triangle"></i> ${escHtml(msg)}</span>`;
        bubble.classList.remove("typing-cursor");
        btnSend.disabled = false;
        chatInput.disabled = false;
      },
    };

    try {
      const response = await fetch(
        `/api/chat/${encodeURIComponent(configId)}/${encodeURIComponent(orchName)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: currentSessionId, message: text }),
        }
      );

      for await (const event of readSSE(response)) {
        await handleSSEEvent(event, ctx);
      }
    } catch (e) {
      if (ctx.onError) ctx.onError(e.message);
    }
  }

  btnSend.addEventListener("click", sendMessage);
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // --- Init ---
  await loadSessions();
  chatInput.focus();
}
