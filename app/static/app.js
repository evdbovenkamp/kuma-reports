// Small progressive enhancements; every page works without JavaScript too.
(function () {
  // Tabs
  document.querySelectorAll("[data-tabs]").forEach(function (bar) {
    function show(name) {
      bar.querySelectorAll("button").forEach(function (b) { b.classList.toggle("active", b.dataset.tab === name); });
      document.querySelectorAll(".tab").forEach(function (t) { t.classList.toggle("active", t.id === "tab-" + name); });
      try { history.replaceState(null, "", "#" + name); } catch (e) { /* ignore */ }
    }
    bar.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-tab]");
      if (b) show(b.dataset.tab);
    });
    var initial = location.hash.slice(1);
    if (initial && document.getElementById("tab-" + initial)) show(initial);
    // Jump to the tab of an invalid field instead of failing silently.
    document.addEventListener("invalid", function (e) {
      var tab = e.target.closest(".tab");
      if (tab && !tab.classList.contains("active")) show(tab.id.slice(4));
    }, true);
  });

  // Text filters for trees and tables
  document.querySelectorAll("[data-filter]").forEach(function (input) {
    var target = document.querySelector(input.dataset.filter);
    if (!target) return;
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      target.querySelectorAll("[data-text]").forEach(function (el) {
        var hit = !q || el.dataset.text.indexOf(q) !== -1;
        el.classList.toggle("hidden", !hit);
        if (el.tagName === "TR") el.style.display = hit ? "" : "none";
      });
    });
  });

  // Show fields depending on a select value: data-visible-if="name=value"
  function applyVisibility(form) {
    form.querySelectorAll("[data-visible-if]").forEach(function (el) {
      var parts = el.dataset.visibleIf.split("=");
      var ctrl = form.querySelector("[name='" + parts[0] + "']");
      var value = !ctrl ? null : ctrl.type === "checkbox" ? (ctrl.checked ? "on" : "") : ctrl.value;
      el.style.display = value === parts[1] ? "" : "none";
    });
  }
  document.querySelectorAll("form").forEach(function (form) {
    if (!form.querySelector("[data-visible-if]")) return;
    applyVisibility(form);
    form.addEventListener("change", function () { applyVisibility(form); });
  });
})();

// Report editor: live count of monitors in scope.
function initReportForm() {
  var form = document.getElementById("reportform");
  var out = document.getElementById("scope-count");
  if (!form || !out) return;
  var timer;
  function update() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      var data = new FormData(form);
      fetch("/api/scope-preview", { method: "POST", body: data, credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          out.textContent = j.count + " monitor" + (j.count === 1 ? "" : "s") + " in scope · " + j.period;
          out.title = j.names.join("\n");
        })
        .catch(function () { out.textContent = ""; });
    }, 250);
  }
  form.addEventListener("change", update);
  form.addEventListener("input", function (e) { if (e.target.type === "text" && e.target.name.indexOf("tagval_") === 0) update(); });
  update();
}

// Template editor: live preview of unsaved changes.
function initTemplateEditor(tid) {
  var form = document.getElementById("tplform");
  var frame = document.getElementById("tplpreview");
  var subj = document.getElementById("tplsubject");
  var err = document.getElementById("tplerror");
  var sel = document.getElementById("previewreport");
  var timer;
  function refresh() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      var data = new FormData(form);
      data.set("report_id", sel ? sel.value : "");
      fetch("/templates/" + tid + "/try", { method: "POST", body: data, credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (j.error) { err.textContent = j.error; err.style.display = ""; return; }
          err.style.display = "none";
          subj.textContent = j.subject;
          frame.srcdoc = j.html;
        });
    }, 600);
  }
  form.addEventListener("input", refresh);
  if (sel) sel.addEventListener("change", refresh);
  form.querySelectorAll("textarea.code").forEach(function (ta) {
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Tab") {
        e.preventDefault();
        var s = ta.selectionStart;
        ta.value = ta.value.slice(0, s) + "  " + ta.value.slice(ta.selectionEnd);
        ta.selectionStart = ta.selectionEnd = s + 2;
      }
    });
  });
  refresh();
}
