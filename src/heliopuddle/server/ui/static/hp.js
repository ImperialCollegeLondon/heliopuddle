/* heliopuddle UI enhancements (docs/design/ui.md, JavaScript).
 *
 * Every page works without this file. It adds:
 *   - typeahead under the header search box (/search/suggest fragments);
 *   - the in-page file inspector on the Files page (/files/{id}/inspector fragments);
 *   - the column chooser (rewrites columns= and reloads);
 *   - the file preview, fetched after the file page has loaded (/files/{id}/preview);
 *   - closing <details> menus on an outside click or Escape, and confirm prompts.
 * Vanilla JavaScript, no build step, no framework.
 */
(function () {
  "use strict";

  /* --- menus ----------------------------------------------------------------- */

  function openMenus() {
    return Array.prototype.slice.call(document.querySelectorAll("details.menu[open]"));
  }

  document.addEventListener("click", function (event) {
    openMenus().forEach(function (menu) {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });

  document.addEventListener("toggle", function (event) {
    var menu = event.target;
    if (!(menu instanceof HTMLDetailsElement) || !menu.classList.contains("menu") || !menu.open) return;
    openMenus().forEach(function (other) {
      if (other !== menu && !other.contains(menu)) other.removeAttribute("open");
    });
    var first = menu.querySelector(".menu-panel input:not([type=hidden]), .menu-panel select");
    if (first && menu.classList.contains("chip-add")) first.focus();
  }, true);

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    openMenus().forEach(function (menu) {
      menu.removeAttribute("open");
      var summary = menu.querySelector("summary");
      if (summary && menu.contains(document.activeElement)) summary.focus();
    });
  });

  /* --- confirm prompts and openers --------------------------------------------------- */

  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute && form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) event.preventDefault();
  });

  function openTarget(id) {
    var target = document.getElementById(id);
    if (target && target.tagName === "DETAILS") target.open = true;
    return target;
  }

  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-open]");
    if (!opener) return;
    var target = openTarget(opener.getAttribute("data-open"));
    if (target) {
      event.preventDefault();
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      history.replaceState(null, "", "#" + target.id);
    }
  });

  if (location.hash.length > 1) openTarget(decodeURIComponent(location.hash.slice(1)));

  /* --- typeahead ----------------------------------------------------------------------- */

  var input = document.getElementById("hp-search");
  var box = document.getElementById("hp-suggest");
  if (input && box) {
    var form = input.form;
    var timer = null;
    var controller = null;
    var active = -1;
    var lastQuery = "";

    var items = function () {
      return Array.prototype.slice.call(box.querySelectorAll("a.res"));
    };
    var show = function (visible) {
      box.hidden = !visible;
      input.setAttribute("aria-expanded", visible ? "true" : "false");
      if (!visible) {
        active = -1;
        input.removeAttribute("aria-activedescendant");
      }
    };
    var highlight = function (index) {
      var list = items();
      if (!list.length) return;
      active = (index + list.length) % list.length;
      list.forEach(function (item, i) {
        item.classList.toggle("active", i === active);
        item.setAttribute("aria-selected", i === active ? "true" : "false");
        if (!item.id) item.id = "hp-suggest-" + i;
      });
      input.setAttribute("aria-activedescendant", list[active].id);
      list[active].scrollIntoView({ block: "nearest" });
    };
    var fetchSuggestions = function () {
      var q = input.value.trim();
      if (q.length < 2) {
        lastQuery = "";
        show(false);
        return;
      }
      if (q === lastQuery && box.innerHTML) {
        show(true);
        return;
      }
      lastQuery = q;
      if (controller) controller.abort();
      controller = typeof AbortController === "function" ? new AbortController() : null;
      var params = new URLSearchParams({ q: q });
      var scope = form.querySelector("input[name=project]");
      if (scope && scope.value) params.set("project", scope.value);
      fetch(form.getAttribute("data-suggest") + "?" + params.toString(), {
        credentials: "same-origin",
        headers: { Accept: "text/html" },
        signal: controller ? controller.signal : undefined
      })
        .then(function (response) {
          if (!response.ok || response.redirected) throw new Error("suggest " + response.status);
          return response.text();
        })
        .then(function (html) {
          if (input.value.trim() !== q) return;
          box.innerHTML = html;
          active = -1;
          show(html.trim().length > 0 && document.activeElement === input);
        })
        .catch(function () { /* typeahead is optional: keep the plain form */ });
    };

    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(fetchSuggestions, 180);
    });
    input.addEventListener("focus", function () {
      if (input.value.trim().length >= 2) fetchSuggestions();
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (box.hidden) fetchSuggestions();
        else highlight(active + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        highlight(active - 1);
      } else if (event.key === "Enter" && !box.hidden && active >= 0) {
        event.preventDefault();
        window.location.assign(items()[active].href);
      } else if (event.key === "Escape") {
        show(false);
      }
    });
    document.addEventListener("click", function (event) {
      if (!form.contains(event.target)) show(false);
    });
    form.addEventListener("focusout", function () {
      setTimeout(function () {
        if (!form.contains(document.activeElement)) show(false);
      }, 120);
    });
  }

  /* --- inspector ----------------------------------------------------------------------- */

  var aside = document.getElementById("inspector");
  if (aside) {
    var layout = aside.closest(".files-layout");
    var pending = null;

    var markRow = function (fileId) {
      document.querySelectorAll(".ft tr.row").forEach(function (row) {
        var on = fileId !== null && row.getAttribute("data-file") === String(fileId);
        row.classList.toggle("sel", on);
        if (on) row.setAttribute("aria-selected", "true");
        else row.removeAttribute("aria-selected");
      });
    };
    var pageUrl = function (fileId) {
      var url = new URL(window.location.href);
      if (fileId === null) url.searchParams.delete("file");
      else url.searchParams.set("file", String(fileId));
      url.hash = "";
      return url;
    };
    var close = function () {
      aside.hidden = true;
      aside.innerHTML = "";
      if (layout) layout.classList.remove("has-inspector");
      markRow(null);
      history.replaceState(null, "", pageUrl(null).toString());
    };
    var open = function (link) {
      var fragment = link.getAttribute("data-inspect");
      var match = /\/files\/(\d+)\/inspector$/.exec(fragment);
      if (!match) return false;
      var fileId = match[1];
      var target = pageUrl(fileId);
      if (pending) pending.abort();
      pending = typeof AbortController === "function" ? new AbortController() : null;
      aside.classList.add("loading");
      fetch(fragment + "?back=" + encodeURIComponent(target.search.slice(1)), {
        credentials: "same-origin",
        headers: { Accept: "text/html" },
        signal: pending ? pending.signal : undefined
      })
        .then(function (response) {
          if (!response.ok || response.redirected) throw new Error("inspector " + response.status);
          return response.text();
        })
        .then(function (html) {
          aside.innerHTML = html;
          aside.hidden = false;
          aside.classList.remove("loading");
          if (layout) layout.classList.add("has-inspector");
          markRow(fileId);
          history.replaceState(null, "", target.toString());
          if (window.matchMedia("(max-width: 900px)").matches) {
            aside.scrollIntoView({ behavior: "smooth", block: "start" });
          }
        })
        .catch(function (error) {
          aside.classList.remove("loading");
          if (error && error.name === "AbortError") return;
          window.location.assign(link.href);
        });
      return true;
    };

    document.addEventListener("click", function (event) {
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      var closer = event.target.closest("[data-close]");
      if (closer && aside.contains(closer)) {
        event.preventDefault();
        close();
        return;
      }
      var link = event.target.closest("a[data-inspect]");
      if (!link) {
        var row = event.target.closest(".ft tr.row");
        if (!row || event.target.closest("a, button, input, select, label, form, summary")) return;
        link = row.querySelector("a[data-inspect]");
        if (!link) return;
      }
      if (open(link)) event.preventDefault();
    });
  }

  /* --- file preview ----------------------------------------------------------------------- */

  document.querySelectorAll("[data-preview]").forEach(function (box) {
    var src = box.getAttribute("data-preview");
    box.classList.add("loading");
    fetch(src, { credentials: "same-origin", headers: { Accept: "text/html" } })
      .then(function (response) {
        if (!response.ok || response.redirected) throw new Error("preview " + response.status);
        return response.text();
      })
      .then(function (html) {
        box.innerHTML = html;
        box.removeAttribute("data-preview");
        box.classList.remove("loading");
      })
      .catch(function () {
        box.classList.remove("loading"); /* keep the "Show preview" link */
      });
  });

  /* --- column chooser -------------------------------------------------------------------- */

  document.querySelectorAll("form[data-columns]").forEach(function (chooser) {
    var boxes = Array.prototype.slice.call(chooser.querySelectorAll("input[name=col]"));
    var limit = function () {
      var checked = boxes.filter(function (b) { return b.checked; }).length;
      boxes.forEach(function (b) { b.disabled = !b.checked && checked >= 6; });
    };
    var apply = function () {
      var url = new URL(chooser.action, window.location.href);
      chooser.querySelectorAll("input[type=hidden]").forEach(function (field) {
        url.searchParams.append(field.name, field.value);
      });
      var chosen = boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.value; });
      url.searchParams.set("columns", chosen.length ? chosen.join(",") : "-");
      window.location.assign(url.toString());
    };
    limit();
    boxes.forEach(function (b) {
      b.addEventListener("change", limit); /* applied only on "Apply" */
    });
    chooser.addEventListener("submit", function (event) {
      event.preventDefault();
      apply();
    });
  });
})();
