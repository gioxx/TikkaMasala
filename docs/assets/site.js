(function () {
  var HEADER_OFFSET = 84;
  var DURATION = 700;

  function easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }

  function smoothScrollTo(targetY) {
    var startY = window.scrollY;
    var distance = targetY - startY;
    var startTime = null;

    function step(timestamp) {
      if (startTime === null) startTime = timestamp;
      var elapsed = timestamp - startTime;
      var progress = Math.min(elapsed / DURATION, 1);
      window.scrollTo(0, startY + distance * easeInOutCubic(progress));
      if (progress < 1) requestAnimationFrame(step);
    }

    requestAnimationFrame(step);
  }

  document.addEventListener("click", function (e) {
    var link = e.target.closest('a[href^="#"]');
    if (!link) return;
    var id = link.getAttribute("href").slice(1);
    if (!id) return;
    var target = document.getElementById(id);
    if (!target) return;
    e.preventDefault();
    var targetY = target.getBoundingClientRect().top + window.scrollY - HEADER_OFFSET;
    smoothScrollTo(Math.max(targetY, 0));
    history.pushState(null, "", "#" + id);
  });

  var ICON_COPY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  var ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>';

  document.querySelectorAll("pre").forEach(function (pre) {
    var codeText = pre.textContent;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "copy-btn";
    button.innerHTML = ICON_COPY + '<span class="copy-label">Copy</span>';
    button.setAttribute("aria-label", "Copy to clipboard");
    pre.appendChild(button);

    button.addEventListener("click", function () {
      navigator.clipboard.writeText(codeText.trim()).then(function () {
        button.innerHTML = ICON_CHECK + '<span class="copy-label">Copied!</span>';
        button.classList.add("is-copied");
        setTimeout(function () {
          button.innerHTML = ICON_COPY + '<span class="copy-label">Copy</span>';
          button.classList.remove("is-copied");
        }, 1500);
      });
    });
  });

  var galleryLinks = document.querySelectorAll(".screenshot-gallery a");
  if (galleryLinks.length) {
    var lightbox = document.createElement("div");
    lightbox.className = "lightbox";
    lightbox.setAttribute("aria-hidden", "true");
    lightbox.innerHTML = '<button type="button" class="lightbox-close" aria-label="Close">&times;</button><img src="" alt="">';
    document.body.appendChild(lightbox);
    var lightboxImg = lightbox.querySelector("img");

    function openLightbox(src, alt) {
      lightboxImg.src = src;
      lightboxImg.alt = alt;
      lightbox.classList.add("is-open");
      lightbox.setAttribute("aria-hidden", "false");
    }

    function closeLightbox() {
      lightbox.classList.remove("is-open");
      lightbox.setAttribute("aria-hidden", "true");
    }

    galleryLinks.forEach(function (link) {
      link.addEventListener("click", function (e) {
        e.preventDefault();
        var img = link.querySelector("img");
        openLightbox(link.getAttribute("href"), img ? img.alt : "");
      });
    });

    lightbox.addEventListener("click", function (e) {
      if (e.target === lightbox || e.target.classList.contains("lightbox-close")) {
        closeLightbox();
      }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeLightbox();
    });
  }

  var navToggle = document.querySelector(".nav-toggle");
  var navMenu = document.querySelector(".nav ul");
  if (navToggle && navMenu) {
    navToggle.addEventListener("click", function () {
      var isOpen = navMenu.classList.toggle("is-open");
      navToggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
    });

    navMenu.addEventListener("click", function (e) {
      if (e.target.closest("a")) {
        navMenu.classList.remove("is-open");
        navToggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  var btn = document.querySelector(".back-to-top");
  if (btn) {
    btn.addEventListener("click", function () {
      smoothScrollTo(0);
    });

    var toggle = function () {
      btn.classList.toggle("visible", window.scrollY > 480);
    };
    window.addEventListener("scroll", toggle, { passive: true });
    toggle();
  }
})();
