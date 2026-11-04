// Proofie - the only client-side behaviour. Without it every page still works (the pages are server-rendered).
// This script is a convenience: one click, one request, and a visible "Sending..." state. It is not the guard.
// Every server keeps its own guards (approval fingerprint, attempt registry, write guard, PayPal's own links),
// so nothing here may be relied on to stop a double send.
(function () {
  "use strict";
  var SENDING_MESSAGE = "Sending to PayPal. Keep this page open.";

  function submitterOf(event, form) {
    if (event.submitter) { return event.submitter; }
    return form.querySelector("button[type=submit], button:not([type]), input[type=submit]");
  }

  // Guards every POST form unless it opts out with data-guard-submit="off" (an older bare data-guard-submit
  // attribute is harmless). The submitter is marked aria-disabled, never disabled: a disabled button would leave
  // out its own name/value and could not be read as busy by assistive technology.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) { return; }
    if ((form.getAttribute("method") || "").toLowerCase() !== "post") { return; }
    if (form.getAttribute("data-guard-submit") === "off") { return; }
    if (form.getAttribute("data-submitting") === "1") { event.preventDefault(); return; }
    form.setAttribute("data-submitting", "1");
    form.setAttribute("aria-busy", "true");
    var submitter = submitterOf(event, form);
    if (submitter) {
      submitter.setAttribute("aria-disabled", "true");
      var text = submitter.getAttribute("data-sending-text");
      if (text && submitter.tagName === "BUTTON") {
        submitter.setAttribute("data-original-text", submitter.textContent);
        submitter.textContent = text;
      }
    }
    // A form that writes to PayPal says so to everyone, including screen readers (polite live region in the page frame).
    if (form.hasAttribute("data-sends")) {
      var region = document.getElementById("inflight-status");
      if (region) { region.textContent = SENDING_MESSAGE; }
    }
  });

  // Coming back with the browser's Back button can restore this page from its cache with the controls still
  // marked busy. A page that holds a form that writes to PayPal is reloaded so it shows the current state from
  // the server (never a stale "ready to send"); on any other page the forms are simply unlocked.
  window.addEventListener("pageshow", function (event) {
    if (!event.persisted) { return; }
    if (document.querySelector("form[data-sends]")) {
      window.location.reload();
      return;
    }
    Array.prototype.forEach.call(document.querySelectorAll("form[data-submitting]"), function (form) {
      form.removeAttribute("data-submitting");
      form.removeAttribute("aria-busy");
      Array.prototype.forEach.call(form.querySelectorAll("[aria-disabled]"), function (control) {
        control.removeAttribute("aria-disabled");
        var original = control.getAttribute("data-original-text");
        if (original !== null) { control.textContent = original; control.removeAttribute("data-original-text"); }
      });
    });
    var region = document.getElementById("inflight-status");
    if (region) { region.textContent = ""; }
  });
  // Toggletips: <span class="tip" data-tip><button class="tip-btn" aria-expanded>...</button><span class="tip-body">..</span></span>.
  // Open on hover, on keyboard focus and on click or tap (a click pins it); close on Escape, on an outside click and when
  // the pointer or focus leaves an unpinned one. Without this script the explanation is plain text in the page.
  (function () {
    var tips = Array.prototype.slice.call(document.querySelectorAll("[data-tip]"));
    if (!tips.length) { return; }
    document.documentElement.classList.add("js-tips");
    function place(tip) {
      var body = tip.querySelector(".tip-body");
      body.style.left = "0px";
      var rect = body.getBoundingClientRect();
      var room = document.documentElement.clientWidth - 8;
      if (rect.right > room) { body.style.left = (room - rect.right) + "px"; }
      rect = body.getBoundingClientRect();
      if (rect.left < 8) { body.style.left = (parseFloat(body.style.left) + 8 - rect.left) + "px"; }
    }
    function open(tip) {
      tip.classList.add("tip-open");
      tip.querySelector(".tip-btn").setAttribute("aria-expanded", "true");
      place(tip);
    }
    function close(tip) {
      tip.classList.remove("tip-open", "tip-pinned");
      tip.querySelector(".tip-btn").setAttribute("aria-expanded", "false");
    }
    tips.forEach(function (tip, index) {
      var button = tip.querySelector(".tip-btn");
      var body = tip.querySelector(".tip-body");
      if (!button || !body) { return; }
      body.id = "tip-" + (index + 1);
      body.setAttribute("role", "status");
      button.setAttribute("aria-describedby", body.id);
      tip.addEventListener("mouseenter", function () { open(tip); });
      tip.addEventListener("mouseleave", function () { if (!tip.classList.contains("tip-pinned")) { close(tip); } });
      tip.addEventListener("focusin", function () { open(tip); });
      tip.addEventListener("focusout", function (event) {
        if (!tip.contains(event.relatedTarget) && !tip.classList.contains("tip-pinned")) { close(tip); }
      });
      button.addEventListener("click", function () {
        if (tip.classList.contains("tip-pinned")) { close(tip); } else { open(tip); tip.classList.add("tip-pinned"); }
      });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") { return; }
      tips.forEach(function (tip) {
        if (tip.classList.contains("tip-open")) {
          close(tip);
          if (tip.contains(document.activeElement)) { tip.querySelector(".tip-btn").blur(); }
        }
      });
    });
    document.addEventListener("click", function (event) {
      tips.forEach(function (tip) { if (!tip.contains(event.target)) { close(tip); } });
    });
  })();
  // Disclosures (data-disc): the markup is fully open, so nothing is lost without scripts. "closed" collapses at load;
  // "narrow" collapses on phones only (the site menu and the case task menu).
  (function () {
    var root = document.documentElement;
    var mq = window.matchMedia ? window.matchMedia("(max-width: 639px)") : null;
    var boxes = document.querySelectorAll("[data-disc]");
    for (var k = 0; k < boxes.length; k++) {
      (function (box) {
        var btn = box.querySelector(".disc-btn"), body = box.querySelector(".disc-body");
        if (!btn || !body) { return; }
        function set(open) { btn.setAttribute("aria-expanded", open ? "true" : "false"); body.hidden = !open; }
        var mode = box.getAttribute("data-disc");
        set(!(mode === "closed" || (mode === "narrow" && mq && mq.matches)));
        btn.addEventListener("click", function () { set(btn.getAttribute("aria-expanded") !== "true"); });
      })(boxes[k]);
    }
    // The site menu button is made here, so pages that must hold no button at all (the replay page) stay without one.
    var menu = document.querySelector(".site-menu"), menuBody = document.getElementById("site-menu-body");
    if (menu && menuBody) {
      var mb = document.createElement("button");
      mb.type = "button"; mb.className = "disc-btn btn-link"; mb.setAttribute("aria-controls", "site-menu-body"); mb.textContent = "Menu";
      function setMenu(open) { mb.setAttribute("aria-expanded", open ? "true" : "false"); menuBody.hidden = !open; }
      setMenu(!(mq && mq.matches));
      mb.addEventListener("click", function () { setMenu(mb.getAttribute("aria-expanded") !== "true"); });
      menu.insertBefore(mb, menuBody);
      // Escape closes the phone menu and gives the focus back to its button.
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && mb.getAttribute("aria-expanded") === "true" && mq && mq.matches) { setMenu(false); mb.focus(); }
      });
    }
    root.className += " js-disc";
  })();
  // A link that points inside a closed dossier section opens it (the heading stays visible above its fold).
  (function () {
    document.addEventListener("click", function (event) {
      var returnLink = event.target.closest && event.target.closest("[data-package-return]");
      if (returnLink) { var sections = document.getElementById("package-edit-sections"); if (sections) { sections.open = false; } return; }
      var link = event.target.closest && event.target.closest("a[href^='#']");
      if (!link) { return; }
      var id = decodeURIComponent((link.getAttribute("href") || "").replace(/^#/, ""));
      var target = id ? document.getElementById(id) : null;
      for (var n = target; n; n = n.parentElement) { if (n.tagName === "DETAILS") { n.open = true; } }
    });
    function openFor() {
      var id = decodeURIComponent((window.location.hash || "").replace(/^#/, ""));
      var el = id ? document.getElementById(id) : null;
      if (!el) { return; }
      var next = el.nextElementSibling;
      if (next && next.tagName === "DETAILS" && next.className.indexOf("dossier-fold") !== -1) { next.open = true; }
      for (var n = el; n; n = n.parentElement) { if (n.tagName === "DETAILS") { n.open = true; } }
      el.scrollIntoView();
    }
    window.addEventListener("hashchange", openFor);
    openFor();
  })();

  // Inbox watch (D-101 r2): while the box is ticked, ask the server every few seconds whether PayPal lists a case this page does not
  // show, and reload the inbox once when it does. The seller can untick the box at any time; it stops by itself after data-max checks.
  (function () {
    var bar = document.querySelector("[data-state='inbox-watch']");
    var toggle = document.getElementById("inbox-watch-toggle");
    var status = document.getElementById("inbox-watch-status");
    if (!bar || !toggle || !status || !window.fetch) { return; }
    var seen = bar.getAttribute("data-seen") || "";
    var every = (parseInt(bar.getAttribute("data-interval"), 10) || 15) * 1000;
    var max = parseInt(bar.getAttribute("data-max"), 10) || 40;
    var timer = null, checks = 0;
    function stop(message) {
      if (timer) { window.clearTimeout(timer); timer = null; }
      if (message) { status.textContent = message; }
    }
    function tick() {
      timer = null;
      if (!toggle.checked) { return; }
      checks += 1;
      fetch(bar.getAttribute("data-url") + "?seen=" + encodeURIComponent(seen), { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("status " + r.status)); })
        .then(function (data) {
          if (data.new && data.new.length) {
            status.textContent = "A new case arrived. Opening it in the list.";
            window.location.reload();
            return;
          }
          if (checks >= max) {
            toggle.checked = false;
            stop("Stopped after " + max + " checks. Tick the box to watch again.");
            return;
          }
          status.textContent = "Checked PayPal at " + new Date().toLocaleTimeString() + ". No new case yet.";
          if (toggle.checked) { timer = window.setTimeout(tick, every); }
        })
        .catch(function () {
          toggle.checked = false;
          stop("Could not check PayPal just now. Tick the box to try again.");
        });
    }
    toggle.addEventListener("change", function () {
      if (toggle.checked) { checks = 0; status.textContent = "Checking PayPal every " + (every / 1000) + " seconds for a new case. Untick the box to stop."; timer = window.setTimeout(tick, 1000); }
      else { stop("Stopped. Tick the box to watch again."); }
    });
    if (toggle.checked) { timer = window.setTimeout(tick, every); }
  })();
})();

// Copy buttons: data-copy-target names a textarea. A denied or missing clipboard is reported in words next to the button, never as
// a silent failure; the text is selected so the seller can copy it by hand.
(function () {
  "use strict";
  function say(button, text) {
    var note = button.nextElementSibling;
    if (!note || !note.hasAttribute("data-copy-status")) {
      note = document.createElement("span");
      note.setAttribute("data-copy-status", "");
      note.setAttribute("role", "status");
      note.setAttribute("aria-live", "polite");
      button.parentNode.insertBefore(note, button.nextSibling);
    }
    note.textContent = " " + text;
  }
  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("button[data-copy-target]") : null;
    if (!button) { return; }
    var area = document.getElementById(button.getAttribute("data-copy-target"));
    if (!area) { return; }
    var done = function () { say(button, "Copied. Nothing was sent."); };
    var failed = function () {
      try { area.focus(); area.select(); } catch (_) { /* nothing to select */ }
      say(button, "Copy failed: the text is selected, press Ctrl+C (or Cmd+C) to copy it.");
    };
    try {
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(area.value).then(done, failed);
      } else if (document.execCommand) {
        area.focus(); area.select();
        if (document.execCommand("copy")) { done(); } else { failed(); }
      } else { failed(); }
    } catch (_) { failed(); }
  });
})();
