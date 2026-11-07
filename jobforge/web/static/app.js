// Small progressive enhancements. No frameworks, no build step.
(function () {
  "use strict";

  // Submit status forms without inline handlers (the dashboard CSP blocks
  // inline script). A noscript button remains as the non-JS fallback.
  document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
    select.addEventListener("change", function () {
      if (select.form) select.form.submit();
    });
  });

  // Confirm destructive/irreversible actions. Never use inline handlers: the
  // dashboard CSP blocks them.
  document.querySelectorAll("form[data-confirm], form[action$='/hide'], form[action$='/disconnect'], form[action$='/remove'], form[action$='/skip']").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var message = form.getAttribute("data-confirm");
      if (!message) {
        if (form.getAttribute("action").indexOf("/hide") !== -1) message = "Hide this job from the feed?";
        else if (form.getAttribute("action").indexOf("/disconnect") !== -1) message = "Disconnect GitHub? The token and cached repository data will be removed. Imported resume projects stay.";
        else if (form.getAttribute("action").indexOf("/remove") !== -1) message = "Remove this project from your resume bank?";
        else message = "Skip this queue item?";
      }
      if (message && !window.confirm(message)) event.preventDefault();
    });
  });

  // Drawer navigation on narrow screens.
  var body = document.body;
  var openButton = document.querySelector("[data-drawer-open]");
  var closeButtons = document.querySelectorAll("[data-drawer-close]");
  function setDrawer(open) {
    body.classList.toggle("drawer-open", !!open);
    if (openButton) openButton.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (openButton) {
    openButton.addEventListener("click", function () { setDrawer(!body.classList.contains("drawer-open")); });
  }
  closeButtons.forEach(function (button) {
    button.addEventListener("click", function () { setDrawer(false); });
  });
  document.querySelectorAll(".side-nav a").forEach(function (link) {
    link.addEventListener("click", function () { setDrawer(false); });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") setDrawer(false);
  });

  // Show a busy state for POST actions that may take seconds.
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function () {
      if (!form.checkValidity || form.checkValidity()) {
        var button = form.querySelector("button[type='submit'], button:not([type])");
        if (button) {
          button.setAttribute("aria-busy", "true");
          button.dataset.originalText = button.textContent;
          button.textContent = "Working…";
          window.setTimeout(function () { button.disabled = true; }, 0);
        }
      }
    });
  });

  // Allow server redirects to surface a short toast without a full alert.
  var flash = document.querySelector(".flash");
  if (flash) {
    var host = document.createElement("div");
    host.className = "toast-host";
    var toast = document.createElement("div");
    toast.className = "toast";
    toast.setAttribute("role", "status");
    toast.textContent = flash.textContent.trim();
    flash.remove();
    host.appendChild(toast);
    document.body.appendChild(host);
    window.setTimeout(function () { toast.remove(); host.remove(); }, 6500);
  }

  // Filter provider cards client-side; no provider is selected by this action.
  var providerSearch = document.getElementById("provider-search");
  if (providerSearch) {
    providerSearch.addEventListener("input", function () {
      var query = providerSearch.value.trim().toLowerCase();
      var visible = 0;
      document.querySelectorAll("#provider-grid .provider-card").forEach(function (card) {
        var match = !query || card.textContent.toLowerCase().indexOf(query) !== -1;
        card.style.display = match ? "" : "none";
        if (match) visible += 1;
      });
      var count = document.getElementById("provider-count");
      if (count) count.textContent = visible + " provider" + (visible === 1 ? "" : "s");
    });
  }

  // Copy short install/config commands without a clipboard framework.
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var value = button.getAttribute("data-copy") || "";
      if (!value) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(value).then(function () {
          button.textContent = "Copied";
          window.setTimeout(function () { button.textContent = "Copy"; }, 1200);
        }).catch(function () {});
      }
    });
  });

  // "/" focuses the search box.
  document.addEventListener("keydown", function (event) {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    var target = event.target;
    if (target && ["INPUT", "TEXTAREA", "SELECT"].indexOf(target.tagName) !== -1) return;
    var search = document.querySelector('input[type="search"]');
    if (search) {
      event.preventDefault();
      search.focus();
    }
  });
})();
