// Live operation state for approval pages. No framework, no build step.
(function () {
  "use strict";
  var root = document.querySelector("[data-operation-id]");
  if (!root) return;
  var id = root.getAttribute("data-operation-id");
  var state = root.getAttribute("data-operation-state") || "";
  if (!id || ["succeeded", "failed", "denied", "expired"].indexOf(state) !== -1) return;

  function check() {
    fetch("/api/operations/" + encodeURIComponent(id), {
      headers: { Accept: "application/json" },
      cache: "no-store"
    })
      .then(function (response) {
        if (!response.ok) throw new Error("state unavailable");
        return response.json();
      })
      .then(function (data) {
        if (data.state && data.state !== state) {
          window.location.reload();
        }
      })
      .catch(function () {
        // A transient local error should not disrupt the decision page.
      });
  }

  window.setInterval(check, 3000);
})();
