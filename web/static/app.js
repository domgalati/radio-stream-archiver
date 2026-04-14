(function () {
  "use strict";

  function initSchedulePage() {
    var page = document.getElementById("schedule-page");
    if (!page) return;
    var dlg = document.getElementById("schedule-dialog");
    var form = document.getElementById("schedule-form");
    var actionInput = document.getElementById("form-action");
    var indexInput = document.getElementById("form-index");

    function openDialog() {
      if (dlg && typeof dlg.show === "function") dlg.show();
    }
    function closeDialog() {
      if (dlg && typeof dlg.hide === "function") dlg.hide();
    }

    function setField(id, value) {
      var el = document.getElementById(id);
      if (!el) return;
      if (el.tagName && el.tagName.toLowerCase() === "sl-checkbox") {
        el.checked = !!value;
      } else {
        el.value = value != null ? String(value) : "";
      }
    }

    document.getElementById("btn-add-show") &&
      document.getElementById("btn-add-show").addEventListener("click", function () {
        if (dlg) dlg.setAttribute("label", "Add show");
        if (actionInput) actionInput.value = "add";
        if (indexInput) indexInput.value = "-1";
        form && form.reset();
        setField("f-enabled", true);
        setField("f-day", "Monday");
        setField("f-format", "192mp3");
        openDialog();
      });

    page.querySelectorAll(".btn-edit-show").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (dlg) dlg.setAttribute("label", "Edit show");
        if (actionInput) actionInput.value = "edit";
        if (indexInput) indexInput.value = btn.getAttribute("data-index") || "0";
        setField("f-title", btn.getAttribute("data-title") || "");
        setField("f-day", btn.getAttribute("data-day") || "Monday");
        setField("f-start", btn.getAttribute("data-start") || "");
        setField("f-end", btn.getAttribute("data-end") || "");
        setField("f-format", btn.getAttribute("data-format") || "192mp3");
        setField("f-enabled", btn.getAttribute("data-enabled") === "1");
        setField("f-end-date", btn.getAttribute("data-end-date") || "");
        openDialog();
      });
    });

    if (dlg) {
      dlg.querySelectorAll("[data-close-modal]").forEach(function (el) {
        el.addEventListener("click", closeDialog);
      });
    }

  }

  function initGlobalClock() {
    document.querySelectorAll("[data-global-clock]").forEach(function (root) {
      var tzEl = root.querySelector(".app-global-clock-tz");
      var timeEl = root.querySelector(".app-global-clock-time");
      try {
        var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
        if (tzEl) tzEl.textContent = tz;
      } catch (e) {
        if (tzEl) tzEl.textContent = "";
      }
      function tick() {
        var d = new Date();
        if (!timeEl) return;
        timeEl.setAttribute("datetime", d.toISOString());
        var dateStr = d.toLocaleDateString(undefined, {
          weekday: "short",
          year: "numeric",
          month: "short",
          day: "numeric",
        });
        var timeStr = d.toLocaleTimeString(undefined, {
          hour: "numeric",
          minute: "2-digit",
          second: "2-digit",
        });
        timeEl.textContent = dateStr + " · " + timeStr;
      }
      tick();
      setInterval(tick, 1000);
    });
  }

  function initLogsScrollPreserve() {
    var state = { scrollTop: 0, clientHeight: 0, scrollHeight: 0, wasAtBottom: false };

    function logsSwapTarget(detail) {
      if (!detail) return null;
      var el = detail.target || detail.elt;
      if (el && el.id === "logs-fragment") return el;
      if (el && el.id === "log-box" && el.parentElement && el.parentElement.id === "logs-fragment") {
        return el.parentElement;
      }
      if (el && typeof el.closest === "function") {
        var byClosest = el.closest("#logs-fragment");
        if (byClosest) return byClosest;
      }
      return null;
    }

    document.body.addEventListener("htmx:beforeSwap", function (e) {
      var frag = logsSwapTarget(e.detail);
      if (!frag) return;
      var box = frag.querySelector("#log-box");
      if (!box) return;
      state.scrollTop = box.scrollTop;
      state.clientHeight = box.clientHeight;
      state.scrollHeight = box.scrollHeight;
      state.wasAtBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 12;
    });
    document.body.addEventListener("htmx:afterSwap", function (e) {
      var frag = logsSwapTarget(e.detail);
      if (!frag) return;
      var box = frag.querySelector("#log-box");
      if (!box) return;
      requestAnimationFrame(function () {
        if (state.wasAtBottom) {
          box.scrollTop = box.scrollHeight;
          return;
        }
        var newSh = box.scrollHeight;
        var oldSh = state.scrollHeight;
        if (oldSh > 0 && newSh > 0) {
          var fromBottom = oldSh - state.scrollTop - state.clientHeight;
          box.scrollTop = Math.max(0, newSh - state.clientHeight - fromBottom);
        } else {
          box.scrollTop = state.scrollTop;
        }
      });
    });
  }

  document.body.addEventListener("click", function (ev) {
    var t = ev.target;
    if (!t || typeof t.closest !== "function") return;
    var btn = t.closest(".copy-start-cmd-btn");
    if (!btn) return;
    var cmd = btn.getAttribute("data-cmd") || "python main.py";
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(cmd).then(
        function () {
          var orig = btn.textContent;
          btn.textContent = "Copied!";
          setTimeout(function () {
            btn.textContent = orig;
          }, 1600);
        },
        function () {}
      );
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    initGlobalClock();
    initSchedulePage();
    initLogsScrollPreserve();
  });
})();
