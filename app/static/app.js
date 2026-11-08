// Shared behaviour for every page: smooth scrolling, back-to-top button,
// the collapsible top navigation menu and the System/Dark/Light theme switcher.
// Page scripts run after this file and may use smoothScrollToY / smoothScrollToElement.

const smoothScrollToY = (targetY, duration = 560) => {
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

const smoothScrollToElement = (element, offset = 0, duration = 560) => {
  if (!element) return;
  const targetY = Math.max(0, window.scrollY + element.getBoundingClientRect().top - offset);
  smoothScrollToY(targetY, duration);
};

const backToTopButton = document.getElementById("back-to-top");

if (backToTopButton) {
  const toggleBackToTop = () => {
    backToTopButton.classList.toggle("is-visible", window.scrollY > 240);
  };

  backToTopButton.addEventListener("click", () => {
    smoothScrollToY(0, 620);
  });

  window.addEventListener("scroll", toggleBackToTop, { passive: true });
  toggleBackToTop();
}

// Top navigation: on narrow screens the sections collapse behind a menu button.
const topbar = document.querySelector("[data-topbar]");
const topnavToggle = document.querySelector("[data-topnav-toggle]");

if (topbar && topnavToggle) {
  const setMenuOpen = (open) => {
    topbar.classList.toggle("is-open", open);
    topnavToggle.setAttribute("aria-expanded", open ? "true" : "false");
    topnavToggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
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

  topbar.querySelectorAll(".topnav-link").forEach((link) => {
    link.addEventListener("click", () => setMenuOpen(false));
  });

  // Close the menu when the viewport grows past the collapse width (--topnav-collapse in styles.css).
  const collapseWidth = getComputedStyle(document.documentElement).getPropertyValue("--topnav-collapse").trim() || "960px";
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

document.querySelectorAll('a[href^="#"]').forEach((anchor) => {
  anchor.addEventListener("click", (event) => {
    const href = anchor.getAttribute("href") || "";
    if (!href.startsWith("#")) return;
    const target = document.querySelector(href);
    if (!target) return;
    event.preventDefault();
    smoothScrollToElement(target, 68, href === "#page-top" ? 620 : 560);
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
