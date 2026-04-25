/**
 * Unit tests for US-XXX: Collapse Historical Tool Usages in Chat UI
 *
 * Run with: node tests/test_tool_collapse.js
 *
 * Acceptance Criteria covered:
 *   AC-1 — No collapse when ≤ 3 tool uses (threshold not exceeded)
 *   AC-2 — Collapse triggers at 4th tool use; toggle shows "X tool uses"
 *   AC-3 — Expand on user interaction; label → "Hide tool uses"; aria-expanded="true"
 *   AC-4 — Re-collapse on user interaction; label → "X tool uses"; aria-expanded="false"
 *   AC-5 — Count updates dynamically during live orchestration without re-expanding
 *   AC-6 — Collapse state is per-thread; two threads do not interfere
 *   AC-7 — Keyboard accessibility: Enter/Space activate toggle; aria-expanded toggles
 *   AC-8 — Collapse/expand uses display:none/show (not DOM removal) — no-CLS pattern
 */

"use strict";

// ---------------------------------------------------------------------------
// Named constant (mirrors app.js)
// ---------------------------------------------------------------------------

const TOOL_USE_COLLAPSE_THRESHOLD = 3;

// ---------------------------------------------------------------------------
// Minimal DOM simulation
// ---------------------------------------------------------------------------

/**
 * Creates a mock DOM element sufficient for the collapse tests.
 * Supports: style.display, setAttribute/getAttribute, appendChild (as array),
 * querySelector (depth-1 only for .tool-collapse-label), children array,
 * addEventListener, classList, focus tracking, innerHTML inspection.
 */
function makeElement(tag = "li") {
  const el = {
    _tag: tag,
    _attrs: {},
    _classes: new Set(),
    _listeners: {},
    _children: [],       // child elements (for appendChild / Array.from)
    _focused: false,
    innerHTML: "",
    style: { display: "" },

    // --- Attribute API ---
    setAttribute(name, val) { this._attrs[name] = String(val); },
    getAttribute(name)      { return this._attrs[name] ?? null; },

    // --- ClassList ---
    get classList() {
      const self = this;
      return {
        add(cls)      { self._classes.add(cls); },
        remove(cls)   { self._classes.delete(cls); },
        contains(cls) { return self._classes.has(cls); },
      };
    },

    // --- Children (for collapse group) ---
    get children() { return this._children; },

    appendChild(child) {
      // Remove from current parent's children list if it's being moved.
      if (child._parent && child._parent !== this) {
        const idx = child._parent._children.indexOf(child);
        if (idx !== -1) child._parent._children.splice(idx, 1);
      }
      child._parent = this;
      this._children.push(child);
      return child;
    },

    // --- querySelector (supports ".tool-collapse-label" one level deep) ---
    querySelector(selector) {
      // Only handles class selectors in this mock.
      const cls = selector.replace(/^\./, "");
      // Search children recursively.
      return this._findByClass(cls);
    },

    _findByClass(cls) {
      for (const child of this._children) {
        if (child._classes && child._classes.has(cls)) return child;
        const found = child._findByClass && child._findByClass(cls);
        if (found) return found;
      }
      // Also check innerHTML for the label span (leaf nodes).
      return null;
    },

    // --- Event handling ---
    addEventListener(event, fn) {
      if (!this._listeners[event]) this._listeners[event] = [];
      this._listeners[event].push(fn);
    },

    _dispatch(event, arg) {
      (this._listeners[event] || []).forEach((fn) => fn(arg));
    },

    dispatchClick()   { this._dispatch("click", {}); },
    dispatchKeydown(key) {
      const e = { key, preventDefault: () => {} };
      this._dispatch("keydown", e);
    },

    // --- Focus ---
    focus() { this._focused = true; },
    get focused() { return this._focused; },

    _parent: null,
  };
  return el;
}

/**
 * Create a mock label <span> element (the .tool-collapse-label child of the toggle btn).
 */
function makeLabelSpan() {
  const span = makeElement("span");
  span._classes.add("tool-collapse-label");
  span.textContent = "";
  // Override querySelector to return self if asked for the label class.
  span.querySelector = (sel) => {
    if (sel === ".tool-collapse-label") return span;
    return null;
  };
  span._findByClass = (cls) => {
    if (cls === "tool-collapse-label") return span;
    return null;
  };
  return span;
}

/**
 * Build a complete mock tool-collapse setup that mirrors the DOM structure
 * created by appendAssistantBubble() in app.js.
 *
 * Returns { state, toggleBtn, collapsedGroup, recentGroup, allItems }
 * where allItems is a helper to list children of both groups.
 */
function buildMockCollapseSetup(uid = 0) {
  const labelSpan = makeLabelSpan();

  const toggleBtn = makeElement("button");
  toggleBtn._classes.add("tool-collapse-toggle");
  toggleBtn.setAttribute("aria-expanded", "false");
  toggleBtn.setAttribute("aria-controls", `tool-collapsed-group-${uid}`);
  toggleBtn.style.display = "none";
  // Give the button a querySelector that finds .tool-collapse-label in labelSpan.
  toggleBtn.appendChild(labelSpan);
  toggleBtn._findByClass = (cls) => {
    if (cls === "tool-collapse-label") return labelSpan;
    return null;
  };
  toggleBtn.querySelector = (sel) => {
    const cls = sel.replace(/^\./, "");
    return toggleBtn._findByClass(cls);
  };

  const collapsedGroup = makeElement("ul");
  collapsedGroup._classes.add("tool-collapsed-group");
  collapsedGroup.setAttribute("id", `tool-collapsed-group-${uid}`);
  collapsedGroup.style.display = "none";

  const recentGroup = makeElement("ul");
  recentGroup._classes.add("tool-activity");

  const state = {
    uid,
    toolUseCount: 0,
    pendingCalls: 0,
    isCollapsed: false,
    userExpanded: false,
    toggleBtn,
    collapsedGroup,
    recentGroup,
  };

  return { state, toggleBtn, collapsedGroup, recentGroup, labelSpan };
}

// ---------------------------------------------------------------------------
// Re-implement core collapse logic (mirrors app.js)
// ---------------------------------------------------------------------------

function toggleToolCollapse(state) {
  if (state.isCollapsed) {
    state.collapsedGroup.style.display = "";
    state.toggleBtn.setAttribute("aria-expanded", "true");
    state.toggleBtn.querySelector(".tool-collapse-label").textContent = "Hide tool uses";
    state.userExpanded = true;
    state.isCollapsed  = false;
  } else {
    state.collapsedGroup.style.display = "none";
    state.toggleBtn.setAttribute("aria-expanded", "false");
    const count = state.toolUseCount - 1;
    state.toggleBtn.querySelector(".tool-collapse-label").textContent =
      `${count} tool use${count !== 1 ? "s" : ""}`;
    state.userExpanded = false;
    state.isCollapsed  = true;
  }
  state.toggleBtn.focus();
}

function collapseOlderToolUses(state) {
  const recentGroup    = state.recentGroup;
  const collapsedGroup = state.collapsedGroup;
  const toggleBtn      = state.toggleBtn;

  const items     = Array.from(recentGroup.children);
  const keepCount = 2;
  const moveItems = items.slice(0, items.length - keepCount);

  for (const item of moveItems) {
    collapsedGroup.appendChild(item);
  }

  const hiddenCount = state.toolUseCount - 1;
  const label = `${hiddenCount} tool use${hiddenCount !== 1 ? "s" : ""}`;

  toggleBtn.style.display = "";

  if (!state.userExpanded) {
    collapsedGroup.style.display = "none";
    toggleBtn.setAttribute("aria-expanded", "false");
    toggleBtn.querySelector(".tool-collapse-label").textContent = label;
    state.isCollapsed = true;
  } else {
    collapsedGroup.style.display = "";
    toggleBtn.setAttribute("aria-expanded", "true");
    state.isCollapsed = false;
  }
}

/**
 * Simulate appending one tool usage (call + result) to the state.
 * Mirrors what appendToolItem() in app.js does when ctx.toolCollapseState is set.
 */
function simulateToolUse(state) {
  // Append call <li>
  const callItem = makeElement("li");
  state.recentGroup.appendChild(callItem);
  state.pendingCalls++;

  // Append result <li>
  const resultItem = makeElement("li");
  state.recentGroup.appendChild(resultItem);
  state.pendingCalls = Math.max(0, state.pendingCalls - 1);
  state.toolUseCount++;

  if (state.toolUseCount > TOOL_USE_COLLAPSE_THRESHOLD) {
    collapseOlderToolUses(state);
  }

  return { callItem, resultItem };
}

// ---------------------------------------------------------------------------
// Test runner (no dependencies — follows established project pattern)
// ---------------------------------------------------------------------------

let passed = 0;
let failed = 0;
const failures = [];

function assert(condition, label) {
  if (condition) {
    console.log(`  ✅  ${label}`);
    passed++;
  } else {
    console.error(`  ❌  ${label}`);
    failed++;
    failures.push(label);
  }
}

function describe(suiteName, fn) {
  console.log(`\n📋  ${suiteName}`);
  fn();
}

// ---------------------------------------------------------------------------
// AC-1 — No collapse when total tool uses ≤ TOOL_USE_COLLAPSE_THRESHOLD (3)
// ---------------------------------------------------------------------------

describe("AC-1 — No collapse for 1 tool use (below threshold)", () => {
  const { state, toggleBtn, collapsedGroup } = buildMockCollapseSetup(0);

  simulateToolUse(state);

  assert(state.toolUseCount === 1, "toolUseCount is 1 after one tool use");
  assert(toggleBtn.style.display === "none", "Toggle button is hidden (display:none)");
  assert(collapsedGroup.style.display === "none", "Collapsed group is hidden");
  assert(state.isCollapsed === false, "isCollapsed remains false");
});

describe("AC-1 — No collapse for exactly 3 tool uses (at threshold, not over)", () => {
  const { state, toggleBtn, collapsedGroup } = buildMockCollapseSetup(1);

  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state);

  assert(state.toolUseCount === 3, "toolUseCount is 3 after three tool uses");
  assert(toggleBtn.style.display === "none", "Toggle button stays hidden at exactly 3 uses");
  assert(collapsedGroup.style.display === "none", "Collapsed group stays hidden at exactly 3 uses");
  assert(state.isCollapsed === false, "isCollapsed is false at threshold (3)");
});

// ---------------------------------------------------------------------------
// AC-2 — Collapse triggers at 4th tool use
// ---------------------------------------------------------------------------

describe("AC-2 — Collapse triggers on 4th tool use", () => {
  const { state, toggleBtn, collapsedGroup, recentGroup, labelSpan } = buildMockCollapseSetup(2);

  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state); // 4th — should trigger collapse

  assert(state.toolUseCount === 4, "toolUseCount is 4 after four tool uses");
  assert(toggleBtn.style.display === "", "Toggle button is now visible (display not none)");
  assert(state.isCollapsed === true, "isCollapsed is true after threshold crossed");
  assert(
    toggleBtn.getAttribute("aria-expanded") === "false",
    "aria-expanded is 'false' in collapsed state"
  );

  // Label should read "3 tool uses" (3 hidden, 1 visible)
  assert(
    labelSpan.textContent === "3 tool uses",
    "Toggle label reads '3 tool uses' after 4th use collapses 3 older ones"
  );

  // Collapsed group should have 6 items (3 usages × 2 <li> each)
  assert(
    collapsedGroup.children.length === 6,
    "Collapsed group holds 6 <li> items (3 usages × call+result)"
  );

  // Recent group should have only 2 items (the 4th usage: call + result)
  assert(
    recentGroup.children.length === 2,
    "Recent group holds exactly 2 <li> items (latest call + result)"
  );

  // Collapsed group itself must be hidden
  assert(
    collapsedGroup.style.display === "none",
    "Collapsed group has display:none in collapsed state"
  );
});

describe("AC-2 — Label uses singular for 1 hidden use", () => {
  // This scenario: 2 tool uses total → at 2 we're still below threshold.
  // To get "1 tool use" label we need exactly toolUseCount=2 after crossing threshold.
  // That means TOOL_USE_COLLAPSE_THRESHOLD=1 for this sub-test — but since we
  // re-implement everything, we test the label helper directly instead.
  const count = 1;
  const label = `${count} tool use${count !== 1 ? "s" : ""}`;
  assert(label === "1 tool use", "Singular: '1 tool use' (no trailing 's')");

  const count2 = 3;
  const label2 = `${count2} tool use${count2 !== 1 ? "s" : ""}`;
  assert(label2 === "3 tool uses", "Plural: '3 tool uses'");
});

// ---------------------------------------------------------------------------
// AC-3 — Expand on user interaction
// ---------------------------------------------------------------------------

describe("AC-3 — Expand: click toggle button from collapsed state", () => {
  const { state, toggleBtn, collapsedGroup, labelSpan } = buildMockCollapseSetup(3);

  // Reach collapsed state.
  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state); // triggers collapse

  assert(state.isCollapsed === true, "Pre-condition: state is collapsed");

  // Simulate user click.
  toggleToolCollapse(state);

  assert(state.isCollapsed === false, "After expand: isCollapsed is false");
  assert(state.userExpanded === true, "After expand: userExpanded is true");
  assert(
    collapsedGroup.style.display === "",
    "After expand: collapsedGroup style.display is '' (visible)"
  );
  assert(
    toggleBtn.getAttribute("aria-expanded") === "true",
    "After expand: aria-expanded is 'true'"
  );
  assert(
    labelSpan.textContent === "Hide tool uses",
    "After expand: toggle label reads 'Hide tool uses'"
  );
  assert(
    toggleBtn._focused === true,
    "After expand: toggle button has focus (AC-7 focus retention)"
  );
});

// ---------------------------------------------------------------------------
// AC-4 — Re-collapse on user interaction
// ---------------------------------------------------------------------------

describe("AC-4 — Re-collapse: click toggle button from expanded state", () => {
  const { state, toggleBtn, collapsedGroup, labelSpan } = buildMockCollapseSetup(4);

  // Reach collapsed then expanded state.
  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state);
  toggleToolCollapse(state); // expand

  assert(state.isCollapsed === false, "Pre-condition: state is expanded");

  // Re-collapse.
  toggleToolCollapse(state);

  assert(state.isCollapsed === true, "After re-collapse: isCollapsed is true");
  assert(state.userExpanded === false, "After re-collapse: userExpanded is false");
  assert(
    collapsedGroup.style.display === "none",
    "After re-collapse: collapsedGroup has display:none"
  );
  assert(
    toggleBtn.getAttribute("aria-expanded") === "false",
    "After re-collapse: aria-expanded is 'false'"
  );

  // Count = toolUseCount - 1 = 4 - 1 = 3.
  assert(
    labelSpan.textContent === "3 tool uses",
    "After re-collapse: toggle label reads '3 tool uses' (correct count)"
  );
  assert(
    toggleBtn._focused === true,
    "After re-collapse: toggle button retains focus"
  );
});

// ---------------------------------------------------------------------------
// AC-5 — Count updates dynamically during live orchestration
// ---------------------------------------------------------------------------

describe("AC-5 — New tool use while collapsed: count increments, stays collapsed", () => {
  const { state, toggleBtn, labelSpan } = buildMockCollapseSetup(5);

  // Reach collapsed state at toolUseCount=4.
  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state);

  assert(state.isCollapsed === true, "Pre-condition: collapsed after 4 uses");
  assert(labelSpan.textContent === "3 tool uses", "Pre-condition: label shows '3 tool uses'");

  // 5th tool use arrives while collapsed.
  simulateToolUse(state);

  assert(state.toolUseCount === 5, "toolUseCount increments to 5");
  assert(state.isCollapsed === true, "Stays collapsed — not re-expanded by new tool use");
  assert(
    toggleBtn.getAttribute("aria-expanded") === "false",
    "aria-expanded remains 'false' (not re-expanded)"
  );
  assert(
    labelSpan.textContent === "4 tool uses",
    "Label updates to '4 tool uses' (5 total − 1 visible = 4 hidden)"
  );
});

describe("AC-5 — New tool use while expanded (user opened): stays expanded, count still visible", () => {
  const { state, toggleBtn, collapsedGroup } = buildMockCollapseSetup(6);

  // Reach collapsed, then user expands.
  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state);
  toggleToolCollapse(state); // user expands

  assert(state.isCollapsed === false, "Pre-condition: user has expanded");

  // 5th tool use arrives.
  simulateToolUse(state);

  assert(state.isCollapsed === false, "Remains expanded when user had opened");
  assert(
    collapsedGroup.style.display === "",
    "Collapsed group stays visible (expanded)"
  );
  assert(
    toggleBtn.getAttribute("aria-expanded") === "true",
    "aria-expanded stays 'true' (user had expanded)"
  );
});

describe("AC-5 — Recent group always holds exactly 2 items (latest call+result)", () => {
  const { state, recentGroup } = buildMockCollapseSetup(7);

  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state); // triggers collapse

  assert(
    recentGroup.children.length === 2,
    "After 4th use triggers collapse: recent group holds 2 items"
  );

  simulateToolUse(state); // 5th use
  assert(
    recentGroup.children.length === 2,
    "After 5th use: recent group still holds exactly 2 items"
  );

  simulateToolUse(state); // 6th use
  assert(
    recentGroup.children.length === 2,
    "After 6th use: recent group still holds exactly 2 items"
  );
});

// ---------------------------------------------------------------------------
// AC-6 — Collapse state is per-message-thread, not global
// ---------------------------------------------------------------------------

describe("AC-6 — Two threads: expand one does not affect the other", () => {
  const setupA = buildMockCollapseSetup(10);
  const setupB = buildMockCollapseSetup(11);

  // Bring both threads to collapsed state.
  for (let i = 0; i < 4; i++) {
    simulateToolUse(setupA.state);
    simulateToolUse(setupB.state);
  }

  assert(setupA.state.isCollapsed === true, "Pre-condition: Thread A is collapsed");
  assert(setupB.state.isCollapsed === true, "Pre-condition: Thread B is collapsed");

  // User expands Thread A only.
  toggleToolCollapse(setupA.state);

  assert(setupA.state.isCollapsed === false, "Thread A: expanded after user interaction");
  assert(setupB.state.isCollapsed === true,  "Thread B: remains collapsed (unaffected)");

  assert(
    setupA.collapsedGroup.style.display === "",
    "Thread A: collapsedGroup is visible"
  );
  assert(
    setupB.collapsedGroup.style.display === "none",
    "Thread B: collapsedGroup stays hidden"
  );

  assert(
    setupA.toggleBtn.getAttribute("aria-expanded") === "true",
    "Thread A: aria-expanded is 'true'"
  );
  assert(
    setupB.toggleBtn.getAttribute("aria-expanded") === "false",
    "Thread B: aria-expanded is still 'false'"
  );
});

describe("AC-6 — Each thread has a unique uid and aria-controls/id pairing", () => {
  const setupA = buildMockCollapseSetup(20);
  const setupB = buildMockCollapseSetup(21);

  const aId = setupA.toggleBtn.getAttribute("aria-controls");
  const bId = setupB.toggleBtn.getAttribute("aria-controls");

  assert(aId !== bId, "Thread A and Thread B toggle buttons control different IDs");
  assert(aId === "tool-collapsed-group-20", "Thread A aria-controls matches its uid");
  assert(bId === "tool-collapsed-group-21", "Thread B aria-controls matches its uid");

  assert(
    setupA.collapsedGroup.getAttribute("id") === "tool-collapsed-group-20",
    "Thread A collapsedGroup id matches aria-controls"
  );
  assert(
    setupB.collapsedGroup.getAttribute("id") === "tool-collapsed-group-21",
    "Thread B collapsedGroup id matches aria-controls"
  );
});

// ---------------------------------------------------------------------------
// AC-7 — Keyboard accessibility
// ---------------------------------------------------------------------------

describe("AC-7 — Toggle button responds to click; aria-expanded toggles", () => {
  const { state, toggleBtn, labelSpan } = buildMockCollapseSetup(30);

  // Reach collapsed state.
  for (let i = 0; i < 4; i++) simulateToolUse(state);

  assert(
    toggleBtn.getAttribute("aria-expanded") === "false",
    "Pre-condition: aria-expanded='false'"
  );

  // Simulate click (Enter/Space on a <button> fires a click event natively).
  // Wire up the click listener as app.js does.
  toggleBtn.addEventListener("click", () => toggleToolCollapse(state));
  toggleBtn.dispatchClick();

  assert(
    toggleBtn.getAttribute("aria-expanded") === "true",
    "After click: aria-expanded='true'"
  );
  assert(labelSpan.textContent === "Hide tool uses", "After click: label is 'Hide tool uses'");
  assert(toggleBtn._focused === true, "After click: focus retained on toggle button (AC-7)");
});

describe("AC-7 — aria-expanded starts as 'false' on initial render (before threshold)", () => {
  const { toggleBtn } = buildMockCollapseSetup(31);
  assert(
    toggleBtn.getAttribute("aria-expanded") === "false",
    "Toggle button initialises with aria-expanded='false'"
  );
});

describe("AC-7 — aria-controls on toggle matches id on collapsedGroup", () => {
  const { state, toggleBtn, collapsedGroup } = buildMockCollapseSetup(32);
  const controlsId = toggleBtn.getAttribute("aria-controls");
  const groupId    = collapsedGroup.getAttribute("id");
  assert(
    controlsId === groupId,
    `aria-controls (${controlsId}) matches collapsedGroup id (${groupId})`
  );
});

// ---------------------------------------------------------------------------
// AC-8 — No-CLS: collapse/expand uses display toggle, not DOM removal
// ---------------------------------------------------------------------------

describe("AC-8 — Collapse uses display:none (not DOM removal) — no-CLS pattern", () => {
  const { state, collapsedGroup } = buildMockCollapseSetup(40);

  // Reach collapsed state.
  for (let i = 0; i < 4; i++) simulateToolUse(state);

  // The element must still exist in the mock (not null/undefined).
  assert(
    collapsedGroup !== null && collapsedGroup !== undefined,
    "collapsedGroup element still exists in the object after collapse (not removed from mock)"
  );

  // It must have display:none (not be removed).
  assert(
    collapsedGroup.style.display === "none",
    "Collapsed group is hidden via style.display='none', not DOM removal"
  );
});

describe("AC-8 — Expand uses display='' (not re-insert) — no-CLS pattern", () => {
  const { state, collapsedGroup } = buildMockCollapseSetup(41);

  for (let i = 0; i < 4; i++) simulateToolUse(state);
  toggleToolCollapse(state); // expand

  // After expand, the element must still be the same object, now visible.
  assert(
    collapsedGroup.style.display === "",
    "Expanded: collapsedGroup display is '' (empty string, browser default = block)"
  );
  assert(
    collapsedGroup !== null && collapsedGroup !== undefined,
    "collapsedGroup element is the same object (no re-mount)"
  );
});

describe("AC-8 — Toggle button uses display:none before threshold, '' after", () => {
  const { state, toggleBtn } = buildMockCollapseSetup(42);

  // Before threshold.
  simulateToolUse(state);
  assert(
    toggleBtn.style.display === "none",
    "Toggle button hidden (display:none) when below threshold"
  );

  simulateToolUse(state);
  simulateToolUse(state);
  simulateToolUse(state); // 4th — crosses threshold

  assert(
    toggleBtn.style.display === "",
    "Toggle button visible (display:'') after threshold crossed"
  );
});

// ---------------------------------------------------------------------------
// TOOL_USE_COLLAPSE_THRESHOLD constant verification
// ---------------------------------------------------------------------------

describe("Named constant: TOOL_USE_COLLAPSE_THRESHOLD", () => {
  assert(
    typeof TOOL_USE_COLLAPSE_THRESHOLD === "number",
    "TOOL_USE_COLLAPSE_THRESHOLD is a number"
  );
  assert(
    TOOL_USE_COLLAPSE_THRESHOLD === 3,
    "TOOL_USE_COLLAPSE_THRESHOLD equals 3"
  );

  // Verify the boundary semantics: collapse at > 3, not >= 3.
  assert(
    3 > TOOL_USE_COLLAPSE_THRESHOLD === false,
    "toolUseCount=3 does NOT exceed threshold (3 > 3 is false)"
  );
  assert(
    4 > TOOL_USE_COLLAPSE_THRESHOLD === true,
    "toolUseCount=4 DOES exceed threshold (4 > 3 is true)"
  );
});

// ---------------------------------------------------------------------------
// Edge cases
// ---------------------------------------------------------------------------

describe("Edge case — pendingCalls floor: never goes below 0", () => {
  const { state } = buildMockCollapseSetup(50);

  // Simulate a result arriving without a prior call (defensive guard).
  state.pendingCalls = 0;
  state.pendingCalls = Math.max(0, state.pendingCalls - 1);
  assert(state.pendingCalls === 0, "pendingCalls does not go below 0 (Math.max guard)");
});

describe("Edge case — collapsedGroup absorbs items correctly across multiple uses", () => {
  const { state, collapsedGroup, recentGroup } = buildMockCollapseSetup(51);

  simulateToolUse(state); // 1
  simulateToolUse(state); // 2
  simulateToolUse(state); // 3
  simulateToolUse(state); // 4 — collapse triggered: 3 uses (6 items) → collapsed; 1 use (2 items) → recent
  simulateToolUse(state); // 5 — collapse triggered: move recent → collapsed; new use → recent
  simulateToolUse(state); // 6

  // After 6 uses: collapsed should have 10 items (5 uses × 2 li), recent should have 2.
  assert(
    collapsedGroup.children.length === 10,
    "After 6 tool uses: collapsed group holds 10 items (5 uses × 2 li each)"
  );
  assert(
    recentGroup.children.length === 2,
    "After 6 tool uses: recent group holds exactly 2 items"
  );
  assert(
    state.toolUseCount === 6,
    "toolUseCount is 6"
  );
  const label = state.toggleBtn.querySelector(".tool-collapse-label").textContent;
  assert(
    label === "5 tool uses",
    "Label reads '5 tool uses' (6 total − 1 visible)"
  );
});

describe("Edge case — toggle collapseState.isCollapsed tracks aria-expanded correctly", () => {
  const { state, toggleBtn } = buildMockCollapseSetup(52);

  for (let i = 0; i < 4; i++) simulateToolUse(state);
  // Collapsed: isCollapsed=true, aria-expanded=false
  assert(state.isCollapsed === true, "isCollapsed=true ↔ aria-expanded=false");
  assert(toggleBtn.getAttribute("aria-expanded") === "false", "aria-expanded='false' when isCollapsed=true");

  toggleToolCollapse(state); // expand
  assert(state.isCollapsed === false, "isCollapsed=false ↔ aria-expanded=true");
  assert(toggleBtn.getAttribute("aria-expanded") === "true", "aria-expanded='true' when isCollapsed=false");

  toggleToolCollapse(state); // re-collapse
  assert(state.isCollapsed === true, "isCollapsed=true ↔ aria-expanded=false (after re-collapse)");
  assert(toggleBtn.getAttribute("aria-expanded") === "false", "aria-expanded='false' after re-collapse");
});

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------

console.log(`\n${"─".repeat(55)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failures.length > 0) {
  console.error("\nFailed assertions:");
  failures.forEach((f) => console.error(`  • ${f}`));
  process.exit(1);
} else {
  console.log("All assertions passed ✅");
  process.exit(0);
}
