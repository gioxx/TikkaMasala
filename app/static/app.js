// Shared behaviour for every page: scrolling and focus helpers, back-to-top button, the
// collapsible top navigation menu, the System/Dark/Light theme switcher and the tooltips.
// Page scripts run after this file and may use the helpers defined here.

const reducedMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
const topbar = document.querySelector("[data-topbar]");

// Height of the top bar while it sticks to the top (0 when it scrolls away with the page).
// Kept in --topbar-h for scroll-padding-top in styles.css, and used as the offset below.
const stickyTopbarHeight = () =>
  topbar && getComputedStyle(topbar).position === "sticky" ? topbar.getBoundingClientRect().height : 0;
const syncTopbarHeight = () => {
  document.documentElement.style.setProperty("--topbar-h", `${Math.ceil(stickyTopbarHeight())}px`);
};
if (topbar) {
  syncTopbarHeight();
  if (typeof ResizeObserver === "function") new ResizeObserver(syncTopbarHeight).observe(topbar);
  window.addEventListener("resize", syncTopbarHeight, { passive: true });
}
const scrollOffset = () => stickyTopbarHeight() + 16;

// Animated scroll, or an instant jump when the user asked for reduced motion.
const smoothScrollToY = (targetY, duration = 560) => {
  if (reducedMotionQuery.matches) {
    window.scrollTo(0, targetY);
    return;
  }
  const startY = window.scrollY;
  const distance = targetY - startY;
  if (Math.abs(distance) < 2) return;

  const startTime = performance.now();
  const easeInOutCubic = (t) => (t < 0.5 ? 4 * t * t * t : 1 - ((-2 * t + 2) ** 3) / 2);

  const step = (currentTime) => {
    const elapsed = currentTime - startTime;
    const progress = Math.min(elapsed / duration, 1);
    const eased = easeInOutCubic(progress);
    window.scrollTo(0, startY + distance * eased);
    if (progress < 1) {
      window.requestAnimationFrame(step);
    }
  };

  window.requestAnimationFrame(step);
};

const smoothScrollToElement = (element, offset = scrollOffset(), duration = 560) => {
  if (!element) return;
  const targetY = Math.max(0, window.scrollY + element.getBoundingClientRect().top - offset);
  smoothScrollToY(targetY, duration);
};

// Move keyboard and screen-reader focus to an element that is not a control (a section, a
// notice, <main>): it becomes focusable from script only.
const focusElement = (element) => {
  if (!element) return;
  if (!element.matches("a[href], button, input, select, textarea, summary, [tabindex]")) {
    element.setAttribute("tabindex", "-1");
  }
  element.focus({ preventScroll: true });
};

// Scroll to an element and move focus to it.
const revealElement = (element, duration = 560) => {
  if (!element) return;
  focusElement(element);
  smoothScrollToElement(element, scrollOffset(), duration);
};

const mainContent = document.getElementById("main");
const backToTopButton = document.getElementById("back-to-top");

if (backToTopButton) {
  const toggleBackToTop = () => {
    backToTopButton.classList.toggle("is-visible", window.scrollY > 240);
  };

  // Focus follows the jump: the next Tab starts from the top of the content, not from the button.
  backToTopButton.addEventListener("click", () => {
    focusElement(mainContent);
    smoothScrollToY(0, 620);
  });

  window.addEventListener("scroll", toggleBackToTop, { passive: true });
  toggleBackToTop();
}

// Top navigation: on narrow screens the sections collapse behind a menu button. Its name stays
// "Menu"; aria-expanded tells whether it is open.
const topnavToggle = document.querySelector("[data-topnav-toggle]");

if (topbar && topnavToggle) {
  const setMenuOpen = (open) => {
    topbar.classList.toggle("is-open", open);
    topnavToggle.setAttribute("aria-expanded", open ? "true" : "false");
  };

  topnavToggle.addEventListener("click", () => {
    setMenuOpen(!topbar.classList.contains("is-open"));
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && topbar.classList.contains("is-open")) {
      setMenuOpen(false);
      topnavToggle.focus();
    }
  });

  // Focus leaving the top bar (Tab past the last link) closes the menu.
  topbar.addEventListener("focusout", (event) => {
    if (topbar.classList.contains("is-open") && event.relatedTarget && !topbar.contains(event.relatedTarget)) {
      setMenuOpen(false);
    }
  });

  topbar.querySelectorAll(".topnav-link").forEach((link) => {
    link.addEventListener("click", () => setMenuOpen(false));
  });

  // Close the menu when the viewport grows past the collapse width (--topnav-collapse in styles.css).
  const collapseWidth = getComputedStyle(document.documentElement).getPropertyValue("--topnav-collapse").trim() || "60em";
  const narrowQuery = window.matchMedia(`(max-width: ${collapseWidth})`);
  const closeWhenWide = (event) => {
    if (!event.matches) setMenuOpen(false);
  };
  if (typeof narrowQuery.addEventListener === "function") {
    narrowQuery.addEventListener("change", closeWhenWide);
  } else if (typeof narrowQuery.addListener === "function") {
    narrowQuery.addListener(closeWhenWide);
  }
}

// In-page links (skip link, "Restore", "Restore history"): scroll and move focus to the target.
document.querySelectorAll('a[href^="#"]').forEach((anchor) => {
  anchor.addEventListener("click", (event) => {
    const href = anchor.getAttribute("href") || "";
    if (!href.startsWith("#") || href.length < 2) return;
    const target = document.getElementById(href.slice(1));
    if (!target) return;
    event.preventDefault();
    revealElement(target, href === "#main" ? 620 : 560);
    history.replaceState(null, "", href);
  });
});

const root = document.documentElement;
const themeButtons = document.querySelectorAll("[data-theme-value]");
const themeStorageKey = "theme-preference";
const systemThemeQuery = window.matchMedia("(prefers-color-scheme: dark)");

const applyThemePreference = (value) => {
  if (value === "light" || value === "dark") {
    root.setAttribute("data-theme", value);
  } else {
    root.removeAttribute("data-theme");
  }

  root.dataset.themePreference = value;

  themeButtons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.themeValue === value);
    button.setAttribute("aria-pressed", button.dataset.themeValue === value ? "true" : "false");
  });
};

themeButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const value = button.dataset.themeValue || "system";
    window.localStorage.setItem(themeStorageKey, value);
    applyThemePreference(value);
  });
});

const syncSystemTheme = () => {
  const savedTheme = window.localStorage.getItem(themeStorageKey) || "system";
  if (savedTheme === "system") {
    applyThemePreference(savedTheme);
  }
};

if (typeof systemThemeQuery.addEventListener === "function") {
  systemThemeQuery.addEventListener("change", syncSystemTheme);
} else if (typeof systemThemeQuery.addListener === "function") {
  systemThemeQuery.addListener(syncSystemTheme);
}

applyThemePreference(root.dataset.themePreference || "system");

// Tooltips ([data-tooltip]): one shared element positioned in the viewport, so scrolling
// tables never clip it. Shown on hover and on keyboard focus; it stays open while the pointer
// is over it and Escape closes it (WCAG 1.4.13). aria-hidden: every host already carries the
// same text as its accessible name (icon actions) or its content (notes).
const tooltip = document.createElement("div");
tooltip.className = "tooltip";
tooltip.setAttribute("aria-hidden", "true");
tooltip.hidden = true;
const tooltipBubble = document.createElement("span");
tooltipBubble.className = "tooltip-bubble";
tooltip.appendChild(tooltipBubble);
document.body.appendChild(tooltip);
root.classList.add("js-tooltips");
let tooltipHost = null;
let dismissedTooltipHost = null;

const placeTooltip = () => {
  if (!tooltipHost) return;
  const margin = 8;
  const host = tooltipHost.getBoundingClientRect();
  tooltip.style.left = "0px";
  tooltip.style.top = "0px";
  tooltip.style.width = "";
  tooltip.dataset.side = "above";
  const { width, height } = tooltip.getBoundingClientRect();
  let top = host.top - height;
  if (top < margin) {
    tooltip.dataset.side = "below";
    top = host.bottom;
  }
  const viewportWidth = document.documentElement.clientWidth;
  const alignStart = tooltipHost.dataset.tooltipAlign === "start";
  let left = alignStart ? host.left : host.right - width;
  left = Math.max(margin, Math.min(left, viewportWidth - width - margin));
  tooltip.style.width = `${Math.ceil(width)}px`;
  tooltip.style.left = `${Math.round(left)}px`;
  tooltip.style.top = `${Math.round(top)}px`;
};

const showTooltip = (host) => {
  const text = host.dataset.tooltip;
  if (!text || host === dismissedTooltipHost) return;
  tooltipHost = host;
  tooltipBubble.textContent = text;
  tooltip.hidden = false;
  placeTooltip();
};

const hideTooltip = () => {
  tooltipHost = null;
  tooltip.hidden = true;
};

// Kept open while the pointer is over the host or the tooltip, or the host has keyboard focus.
const tooltipStillWanted = () =>
  tooltipHost && (tooltipHost.matches(":hover") || tooltip.matches(":hover") || tooltipHost.matches(":focus-visible"));

document.addEventListener("mouseover", (event) => {
  const host = event.target.closest && event.target.closest("[data-tooltip]");
  if (host && host !== tooltipHost) showTooltip(host);
});

document.addEventListener("mouseout", (event) => {
  const host = event.target.closest && event.target.closest("[data-tooltip]");
  if (host && host === dismissedTooltipHost && !host.contains(event.relatedTarget)) dismissedTooltipHost = null;
  if (!tooltipHost) return;
  const to = event.relatedTarget;
  if (to && (tooltipHost.contains(to) || tooltip.contains(to))) return;
  window.requestAnimationFrame(() => {
    if (!tooltipStillWanted()) hideTooltip();
  });
});

document.addEventListener("focusin", (event) => {
  const host = event.target.closest && event.target.closest("[data-tooltip]");
  if (host && host.matches(":focus-visible")) showTooltip(host);
});

document.addEventListener("focusout", (event) => {
  if (event.target === dismissedTooltipHost) dismissedTooltipHost = null;
  if (tooltipHost && event.target === tooltipHost && !tooltipHost.matches(":hover") && !tooltip.matches(":hover")) {
    hideTooltip();
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && tooltipHost) {
    dismissedTooltipHost = tooltipHost;
    hideTooltip();
  }
});

window.addEventListener("scroll", placeTooltip, { passive: true, capture: true });
window.addEventListener("resize", placeTooltip, { passive: true });

// Archive notes are cut short with an ellipsis: only a truncated note gets the tooltip and a
// tab stop (to reach it from the keyboard); a note shown in full gets neither.
const syncNoteTooltips = () => {
  document.querySelectorAll(".archive-notes-tip").forEach((host) => {
    const note = host.querySelector(".archive-notes");
    if (!note) return;
    if (note.scrollWidth > note.clientWidth + 1) {
      host.dataset.tooltip = note.textContent;
      host.tabIndex = 0;
    } else {
      delete host.dataset.tooltip;
      host.removeAttribute("tabindex");
      if (host === tooltipHost) hideTooltip();
    }
  });
};
// Tables that scroll sideways: while one actually overflows it becomes a named, focusable
// region, so keyboard users can scroll it too (WCAG 2.1.1); otherwise it adds no tab stop.
const syncScrollRegions = () => {
  document.querySelectorAll("[data-scroll-region]").forEach((region) => {
    if (region.scrollWidth > region.clientWidth + 1) {
      region.tabIndex = 0;
      region.setAttribute("role", "region");
      region.setAttribute("aria-label", region.dataset.scrollRegion);
    } else {
      region.removeAttribute("tabindex");
      region.removeAttribute("role");
      region.removeAttribute("aria-label");
    }
  });
};

const syncLayoutDependentAttributes = () => {
  syncNoteTooltips();
  syncScrollRegions();
};
let layoutSyncFrame = 0;
window.addEventListener("resize", () => {
  window.cancelAnimationFrame(layoutSyncFrame);
  layoutSyncFrame = window.requestAnimationFrame(syncLayoutDependentAttributes);
}, { passive: true });
document.addEventListener("htmx:afterSettle", syncLayoutDependentAttributes);
syncLayoutDependentAttributes();

// The result of a form, rendered by the server as a notice with data-focus-notice, receives
// focus: screen readers read it and keyboard users continue from there (WCAG 3.3.1, 4.1.3).
const formResultNotice = document.querySelector("[data-focus-notice]");
if (formResultNotice) {
  window.requestAnimationFrame(() => revealElement(formResultNotice));
}

// htmx swaps replace the control that started them (Back up, Back up all, the data source
// links): remember its id before the request (hx-disabled-elt drops focus right after) and
// focus its new copy once the swap settles, so keyboard focus is not lost to <body> (2.4.3).
let htmxFocusId = "";
document.addEventListener("htmx:beforeRequest", (event) => {
  const active = document.activeElement;
  const submitter = event.detail.requestConfig?.triggeringEvent?.submitter;
  htmxFocusId = (active && active !== document.body && active.id) || (submitter && submitter.id) || "";
});
document.addEventListener("htmx:afterSettle", () => {
  if (!htmxFocusId) return;
  const target = document.getElementById(htmxFocusId);
  htmxFocusId = "";
  const active = document.activeElement;
  if (target && (!active || active === document.body || !active.isConnected)) {
    target.focus({ preventScroll: true });
  }
});

// Render [data-utc-datetime] elements in the scheduler timezone. Pages that
// carry data-auto-backup-timezone follow the same rule as the Automation page
// (browser timezone unless the server one was set); others fall back to UTC.
const formatLocalDateTimes = () => {
  const container = document.querySelector("[data-auto-backup-timezone]");
  const browserTimezoneName = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  let fallbackTimezone = "UTC";
  if (container) {
    const source = container.dataset.autoBackupTimezoneSource || "default";
    fallbackTimezone = source === "default" ? browserTimezoneName : container.dataset.autoBackupTimezone || "UTC";
  }
  document.querySelectorAll("[data-utc-datetime]").forEach((element) => {
    const rawValue = element.dataset.utcDatetime || "";
    const displayTimezone = element.dataset.displayTimezone || fallbackTimezone;
    const parsed = new Date(rawValue);
    if (Number.isNaN(parsed.getTime())) return;
    const zonedFormatter = new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      timeZone: displayTimezone,
      timeZoneName: "short",
    });
    element.textContent = zonedFormatter.format(parsed);
    element.title = `${rawValue} UTC`;
  });
};

document.addEventListener("DOMContentLoaded", formatLocalDateTimes);
