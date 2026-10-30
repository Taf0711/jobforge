// Live autopilot trace. Polls the local status endpoint; no framework.
(function () {
  "use strict";

  var panel = document.getElementById("autopilot-live");
  if (!panel) return;
  var endpoint = panel.getAttribute("data-endpoint") || "/api/autopilot/status";
  var pill = document.getElementById("ap-status-pill");
  var stage = document.getElementById("ap-stage");
  var job = document.getElementById("ap-job");
  var trace = document.getElementById("ap-trace");
  var updated = document.getElementById("ap-updated");
  var counters = {
    prepared: document.getElementById("ap-prepared"),
    submitted: document.getElementById("ap-submitted"),
    skipped: document.getElementById("ap-skipped"),
    failed: document.getElementById("ap-failed")
  };
  var timer = null;

  function setText(node, value) {
    if (node) node.textContent = value === undefined || value === null ? "" : String(value);
  }

  function eventDetail(event) {
    var parts = [];
    if (event.company || event.title) parts.push([event.company, event.title].filter(Boolean).join(" — "));
    if (event.status) parts.push("status " + event.status);
    if (event.reason) parts.push(String(event.reason));
    if (event.error) parts.push(String(event.error));
    if (event.required_missing && event.required_missing.length) {
      parts.push("missing: " + event.required_missing.join("; "));
    }
    if (event.submitted !== undefined) parts.push("submitted " + String(event.submitted));
    if (event.pdf) parts.push(String(event.pdf).split("/").pop());
    return parts.join(" · ").slice(0, 220);
  }

  function renderTrace(events) {
    if (!trace) return;
    trace.textContent = "";
    var items = (events || []).slice(-60);
    if (!items.length) {
      var empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = "No events yet.";
      trace.appendChild(empty);
      return;
    }
    items.forEach(function (event) {
      var row = document.createElement("li");
      var time = document.createElement("time");
      var stamp = String(event.ts || "").split("T")[1] || "";
      time.textContent = stamp.replace("+00:00", "").slice(0, 8);
      var name = document.createElement("strong");
      name.textContent = String(event.event || "event").replace(/_/g, " ");
      var detail = document.createElement("span");
      detail.textContent = eventDetail(event);
      row.appendChild(time);
      row.appendChild(name);
      row.appendChild(detail);
      trace.appendChild(row);
    });
    trace.scrollTop = trace.scrollHeight;
  }

  function render(payload) {
    var state = payload.state || {};
    var running = !!payload.running;
    if (pill) {
      if (running) {
        pill.className = "pill";
        pill.textContent = "running · " + (state.stage || "starting");
      } else if (state.status) {
        pill.className = "pill";
        pill.textContent = "last run · " + state.status + (state.stopped_reason ? " · " + state.stopped_reason : "");
      } else {
        pill.className = "pill";
        pill.textContent = "idle";
      }
    }
    if (stage) {
      if (running) {
        stage.textContent = "Stage: " + (state.stage || "starting") + (state.dry_run ? " · dry run" : " · live");
      } else if (state.finished_at) {
        stage.textContent = "Finished " + state.finished_at + (state.dry_run ? " · dry run" : " · live");
      } else if (state.status === "queued") {
        stage.textContent = "Queued; waiting for the worker to start.";
      } else {
        stage.textContent = "No autopilot state yet.";
      }
    }
    if (job) {
      var current = state.current_job || {};
      job.textContent = current.job_id
        ? "Current: " + [current.company, current.title].filter(Boolean).join(" — ") + (current.ats ? " · " + current.ats : "")
        : "";
    }
    var values = state.counters || {};
    Object.keys(counters).forEach(function (key) {
      setText(counters[key], values[key] === undefined ? 0 : values[key]);
    });
    renderTrace(payload.trace);
    if (updated) {
      updated.textContent = state.updated_at
        ? "State updated " + state.updated_at + (state.pid ? " · pid " + state.pid : "")
        : "";
    }
    if (running) {
      window.clearTimeout(timer);
      timer = window.setTimeout(poll, 2000);
    }
  }

  function poll() {
    fetch(endpoint, { headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("status " + response.status);
        return response.json();
      })
      .then(render)
      .catch(function (error) {
        if (stage) stage.textContent = "Monitor unavailable: " + error.message;
      });
  }

  poll();
})();
