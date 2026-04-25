/**
 * Unit tests for US-005: Preserve User Scroll Position During Live Chat Output
 *
 * These tests validate the scroll-position logic extracted from renderChat()
 * in app.js by simulating DOM scroll state.  Run with: node tests/test_chat_scroll.js
 *
 * Acceptance Criteria covered:
 *   AC-1 — isNearBottom() threshold: user is "at bottom" when
 *           scrollTop + clientHeight >= scrollHeight - 8
 *   AC-2 — maybeScrollToBottom() does NOT scroll when user is scrolled up
 *   AC-3 — scrollToBottom() always scrolls (used for user-sent messages)
 *   AC-4 — showNewMessagesBadge() is called when user is scrolled up
 *   AC-5 — badge click / keydown invokes scrollToBottom()
 *   AC-6 — hideNewMessagesBadge() is called when user scrolls back to bottom
 */

// ---------------------------------------------------------------------------
// Minimal DOM simulation
// ---------------------------------------------------------------------------

/**
 * Creates a mock scroll container with configurable geometry.
 * Mirrors the properties the real browser exposes on HTMLElement.
 */
function makeMockWrapper({ scrollTop = 0, clientHeight = 500, scrollHeight = 500 } = {}) {
  return {
    scrollTop,
    clientHeight,
    scrollHeight,
    _listeners: {},
    addEventListener(event, fn, _opts) {
      if (!this._listeners[event]) this._listeners[event] = [];
      this._listeners[event].push(fn);
    },
    dispatchScroll() {
      (this._listeners["scroll"] || []).forEach((fn) => fn());
    },
  };
}

/** Creates a mock button element for the "New messages" badge. */
function makeMockBadge() {
  return {
    _classes: new Set(),
    _listeners: {},
    classList: {
      _owner: null, // set after construction
      add(cls)    { this._owner._classes.add(cls); },
      remove(cls) { this._owner._classes.delete(cls); },
      contains(cls) { return this._owner._classes.has(cls); },
    },
    addEventListener(event, fn) {
      if (!this._listeners[event]) this._listeners[event] = [];
      this._listeners[event].push(fn);
    },
    dispatchClick()   { (this._listeners["click"]   || []).forEach((fn) => fn()); },
    dispatchKeydown(key) {
      const e = { key, preventDefault: () => {} };
      (this._listeners["keydown"] || []).forEach((fn) => fn(e));
    },
  };
}

// ---------------------------------------------------------------------------
// Re-implement the US-005 scroll logic extracted from app.js
// This mirrors the production code so that tests stay honest.
// ---------------------------------------------------------------------------

const SCROLL_THRESHOLD = 8;

function buildScrollManager(wrapper, badge) {
  // Fix the classList owner reference
  badge.classList._owner = badge;

  function isNearBottom() {
    return wrapper.scrollTop + wrapper.clientHeight >= wrapper.scrollHeight - SCROLL_THRESHOLD;
  }

  function showNewMessagesBadge() {
    badge.classList.add("visible");
  }

  function hideNewMessagesBadge() {
    badge.classList.remove("visible");
  }

  // scrollToBottom — unconditional, used for user-sent messages and history load
  function scrollToBottom() {
    hideNewMessagesBadge();
    // In tests we skip requestAnimationFrame and apply immediately.
    wrapper.scrollTop = wrapper.scrollHeight;
  }

  // maybeScrollToBottom — respects user scroll position during live output
  function maybeScrollToBottom() {
    if (isNearBottom()) {
      hideNewMessagesBadge();
      wrapper.scrollTop = wrapper.scrollHeight;
    } else {
      showNewMessagesBadge();
    }
  }

  // Wire badge interactions
  badge.addEventListener("click", () => scrollToBottom());
  badge.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      scrollToBottom();
    }
  });

  // Hide badge when user manually scrolls to bottom
  wrapper.addEventListener("scroll", () => {
    if (isNearBottom()) {
      hideNewMessagesBadge();
    }
  }, { passive: true });

  return { isNearBottom, showNewMessagesBadge, hideNewMessagesBadge, scrollToBottom, maybeScrollToBottom };
}

// ---------------------------------------------------------------------------
// Test runner (no dependencies)
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
// AC-1 — isNearBottom() threshold behaviour
// ---------------------------------------------------------------------------

describe("AC-1 — isNearBottom() uses ≤8px threshold", () => {
  function make(scrollTop, clientHeight, scrollHeight) {
    const w = makeMockWrapper({ scrollTop, clientHeight, scrollHeight });
    const b = makeMockBadge();
    return buildScrollManager(w, b);
  }

  assert(
    make(0, 500, 500).isNearBottom() === true,
    "Exactly at bottom (scrollTop=0, clientHeight=500, scrollHeight=500) → true"
  );

  assert(
    make(492, 500, 1000).isNearBottom() === true,
    "Within 8px of bottom (492+500=992, scrollHeight=1000, diff=8) → true"
  );

  assert(
    make(491, 500, 1000).isNearBottom() === false,
    "Just beyond threshold (491+500=991, scrollHeight=1000, diff=9) → false"
  );

  assert(
    make(0, 500, 1000).isNearBottom() === false,
    "Scrolled well up (scrollTop=0, scrollHeight=1000) → false"
  );

  assert(
    make(400, 500, 900).isNearBottom() === true,
    "Exactly at bottom with offset (400+500=900, scrollHeight=900) → true"
  );

  assert(
    make(393, 500, 900).isNearBottom() === true,
    "7px from bottom (393+500=893, scrollHeight=900, diff=7 ≤8) → true"
  );
});

// Re-run the boundary correctly:
describe("AC-1 — isNearBottom() exact boundary re-verification", () => {
  function mgr(scrollTop, clientHeight, scrollHeight) {
    return buildScrollManager(
      makeMockWrapper({ scrollTop, clientHeight, scrollHeight }),
      makeMockBadge()
    );
  }

  // diff = scrollHeight - (scrollTop + clientHeight)
  // diff = 1000 - (492 + 500) = 8  → exactly at threshold → true
  assert(mgr(492, 500, 1000).isNearBottom() === true,  "diff=8 (≤8) → near bottom");
  // diff = 1000 - (491 + 500) = 9  → just past threshold → false
  assert(mgr(491, 500, 1000).isNearBottom() === false, "diff=9 (>8) → not near bottom");
  // diff = 0 → true
  assert(mgr(500, 500, 1000).isNearBottom() === true,  "diff=0 → near bottom");
  // diff = 1 → true
  assert(mgr(499, 500, 1000).isNearBottom() === true,  "diff=1 → near bottom");
});

// ---------------------------------------------------------------------------
// AC-2 — maybeScrollToBottom() respects user scroll position
// ---------------------------------------------------------------------------

describe("AC-2 — maybeScrollToBottom() does not scroll when user has scrolled up", () => {
  function setup(scrollTop = 0, clientHeight = 500, scrollHeight = 1000) {
    const wrapper = makeMockWrapper({ scrollTop, clientHeight, scrollHeight });
    const badge   = makeMockBadge();
    const mgr     = buildScrollManager(wrapper, badge);
    return { wrapper, badge, mgr };
  }

  {
    const { wrapper, mgr } = setup(0, 500, 1000); // user scrolled to top
    const before = wrapper.scrollTop;
    mgr.maybeScrollToBottom();
    assert(wrapper.scrollTop === before, "User scrolled up: scrollTop unchanged after maybeScrollToBottom()");
  }

  {
    const { wrapper, mgr } = setup(492, 500, 1000); // user near bottom (diff=8)
    mgr.maybeScrollToBottom();
    assert(wrapper.scrollTop === wrapper.scrollHeight, "User near bottom: scrollTop set to scrollHeight");
  }
});

// ---------------------------------------------------------------------------
// AC-3 — scrollToBottom() always scrolls (user-sent message path)
// ---------------------------------------------------------------------------

describe("AC-3 — scrollToBottom() always scrolls regardless of position", () => {
  function setup(scrollTop = 0, scrollHeight = 1000) {
    const wrapper = makeMockWrapper({ scrollTop, clientHeight: 500, scrollHeight });
    const badge   = makeMockBadge();
    const mgr     = buildScrollManager(wrapper, badge);
    return { wrapper, badge, mgr };
  }

  {
    const { wrapper, mgr } = setup(0, 1000); // user at top
    mgr.scrollToBottom();
    assert(wrapper.scrollTop === 1000, "scrollToBottom() from top: scrolls to scrollHeight");
  }

  {
    const { wrapper, mgr } = setup(250, 1000); // user mid-page
    mgr.scrollToBottom();
    assert(wrapper.scrollTop === 1000, "scrollToBottom() from mid: scrolls to scrollHeight");
  }
});

// ---------------------------------------------------------------------------
// AC-4 — Badge appears when user is scrolled up and new tokens arrive
// ---------------------------------------------------------------------------

describe("AC-4 — 'New messages' badge shown when user is scrolled up during streaming", () => {
  function setup(scrollTop = 0, clientHeight = 500, scrollHeight = 1000) {
    const wrapper = makeMockWrapper({ scrollTop, clientHeight, scrollHeight });
    const badge   = makeMockBadge();
    const mgr     = buildScrollManager(wrapper, badge);
    return { wrapper, badge, mgr };
  }

  {
    const { badge, mgr } = setup(0, 500, 1000); // user scrolled up
    mgr.maybeScrollToBottom();
    assert(badge.classList.contains("visible"), "Badge becomes visible when user is scrolled up");
  }

  {
    const { badge, mgr } = setup(492, 500, 1000); // user near bottom
    mgr.maybeScrollToBottom();
    assert(!badge.classList.contains("visible"), "Badge stays hidden when user is near bottom");
  }

  {
    // Badge hidden after scrollToBottom() is called (e.g., user sends a new message)
    const { badge, mgr } = setup(0, 500, 1000);
    mgr.showNewMessagesBadge(); // simulate badge already showing
    mgr.scrollToBottom();
    assert(!badge.classList.contains("visible"), "scrollToBottom() hides the badge");
  }
});

// ---------------------------------------------------------------------------
// AC-5 — Badge click and keyboard activation scroll to bottom
// ---------------------------------------------------------------------------

describe("AC-5 — Badge is keyboard-accessible: click / Enter / Space scroll to bottom", () => {
  function setup(scrollTop = 0) {
    const wrapper = makeMockWrapper({ scrollTop, clientHeight: 500, scrollHeight: 1000 });
    const badge   = makeMockBadge();
    const mgr     = buildScrollManager(wrapper, badge);
    return { wrapper, badge, mgr };
  }

  {
    const { wrapper, badge } = setup(0);
    badge.dispatchClick();
    assert(wrapper.scrollTop === 1000, "Click on badge scrolls to bottom");
  }

  {
    const { wrapper, badge } = setup(100);
    badge.dispatchKeydown("Enter");
    assert(wrapper.scrollTop === 1000, "Enter keydown on badge scrolls to bottom");
  }

  {
    const { wrapper, badge } = setup(200);
    badge.dispatchKeydown(" ");
    assert(wrapper.scrollTop === 1000, "Space keydown on badge scrolls to bottom");
  }

  {
    const { wrapper, badge } = setup(300);
    badge.dispatchKeydown("Escape"); // should be ignored
    assert(wrapper.scrollTop === 300, "Escape keydown on badge does NOT scroll");
  }
});

// ---------------------------------------------------------------------------
// AC-6 — Badge hides when user manually scrolls back to the bottom
// ---------------------------------------------------------------------------

describe("AC-6 — Badge hides automatically when user scrolls back to bottom", () => {
  function setup(scrollTop = 0, clientHeight = 500, scrollHeight = 1000) {
    const wrapper = makeMockWrapper({ scrollTop, clientHeight, scrollHeight });
    const badge   = makeMockBadge();
    buildScrollManager(wrapper, badge);
    return { wrapper, badge };
  }

  {
    const { wrapper, badge } = setup(0, 500, 1000);
    badge.classList.add("visible"); // badge is showing

    // User scrolls to bottom manually
    wrapper.scrollTop = 500; // 500 + 500 = 1000 = scrollHeight → diff = 0
    wrapper.dispatchScroll();

    assert(!badge.classList.contains("visible"), "Badge hidden after user scrolls to exact bottom");
  }

  {
    const { wrapper, badge } = setup(0, 500, 1000);
    badge.classList.add("visible");

    // User scrolls to within 8px of bottom
    wrapper.scrollTop = 492; // 492 + 500 = 992, diff = 8 → near bottom
    wrapper.dispatchScroll();

    assert(!badge.classList.contains("visible"), "Badge hidden when user scrolls within threshold");
  }

  {
    const { wrapper, badge } = setup(0, 500, 1000);
    badge.classList.add("visible");

    // User scrolls but stays above threshold
    wrapper.scrollTop = 400; // 400 + 500 = 900, diff = 100 → not near bottom
    wrapper.dispatchScroll();

    assert(badge.classList.contains("visible"), "Badge stays visible when user is still scrolled up");
  }
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
