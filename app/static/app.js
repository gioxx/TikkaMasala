// Shared behaviour for every page: smooth scrolling, back-to-top button,
// floating section nav and the System/Dark/Light theme switcher.
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
const floatingNav = document.querySelector("[data-floating-nav]");
const floatingNavLabel = document.querySelector("[data-hero-nav-label]");

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

if (floatingNav) {
  let isFloating = false;
  const toggleFloatingNav = () => {
    const shouldFloat = isFloating ? window.scrollY > 132 : window.scrollY > 180;
    isFloating = shouldFloat;
    floatingNav.classList.toggle("is-floating", shouldFloat);
    document.body.classList.toggle("has-floating-nav", shouldFloat);
    if (floatingNavLabel) {
      floatingNavLabel.classList.toggle("is-hidden", shouldFloat);
    }
  };

  window.addEventListener("scroll", toggleFloatingNav, { passive: true });
  toggleFloatingNav();
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
