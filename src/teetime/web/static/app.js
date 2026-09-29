/* Progressive enhancement only (the CSP allows same-origin script, never inline). Every page
   works without this file: the ranked booking form's extra option rows sit inside a
   <details class="more-options"> disclosure, and the server ignores rows with no course.

   With script, the disclosure becomes "Add another time slot": extra rows are hidden until
   asked for, one at a time, and a row can be removed again (which blanks its course so the
   server skips it).

   Also with script: each <input type="date" class="datepick"> gets a month calendar paged
   left/right (Google Calendar style: ‹ › buttons, a swipe on touch screens, arrow keys and
   PageUp/PageDown on the days). The native input stays in the form and carries the value;
   without script it is the only date control. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  function isBlank(row) {
    var select = row.querySelector("select");
    return !select || select.value === "";
  }

  function enhance(form) {
    var more = form.querySelector("details.more-options");
    var add = form.querySelector("button.add-option");
    if (!more || !add) {
      return;
    }
    var extras = Array.prototype.slice.call(more.querySelectorAll(".option.extra"));
    more.open = true;

    function refresh() {
      var anyHidden = extras.some(function (row) { return row.hidden; });
      add.hidden = !anyHidden;
    }

    extras.forEach(function (row) {
      row.hidden = isBlank(row);
      var remove = row.querySelector("button.remove");
      if (remove) {
        remove.hidden = false;
        remove.addEventListener("click", function () {
          var select = row.querySelector("select");
          if (select) {
            select.value = "";
          }
          row.hidden = true;
          refresh();
          add.focus();
        });
      }
    });

    add.addEventListener("click", function () {
      var next = extras.filter(function (row) { return row.hidden; })[0];
      if (!next) {
        return;
      }
      next.hidden = false;
      var select = next.querySelector("select");
      if (select) {
        if (select.value === "" && select.options.length > 1) {
          select.selectedIndex = 1; // the first real course, like option 1
        }
        select.focus();
      }
      refresh();
    });
    refresh();
  }


  // ---- date: month calendar paged left/right -------------------------------------------

  var DOW = ["S", "M", "T", "W", "T", "F", "S"];

  function pad(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function iso(d) {
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function parseIso(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || "");
    return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) {
      node.className = cls;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  function calendar(input) {
    var label = input.closest("label");
    // The server renders min= as "today" in the course's timezone; the visitor's clock is only
    // the fallback (a page rendered without it).
    var local = new Date();
    var today = parseIso(input.min) || new Date(local.getFullYear(), local.getMonth(), local.getDate());
    var min = today;
    var selected = parseIso(input.value);
    var view = new Date((selected || min).getFullYear(), (selected || min).getMonth(), 1);

    var wrap = el("div", "date-field");
    var cal = el("div", "calendar");
    cal.setAttribute("role", "group");
    cal.setAttribute("aria-label", "Choose a date");
    var head = el("div", "cal-head");
    var prev = el("button", "cal-nav", "\u2039");
    prev.type = "button";
    prev.setAttribute("aria-label", "Previous month");
    var title = el("div", "cal-title");
    title.setAttribute("aria-live", "polite");
    var next = el("button", "cal-nav", "\u203A");
    next.type = "button";
    next.setAttribute("aria-label", "Next month");
    head.appendChild(prev);
    head.appendChild(title);
    head.appendChild(next);
    var grid = el("div", "cal-grid");
    var picked = el("p", "cal-picked");
    picked.setAttribute("aria-live", "polite");
    cal.appendChild(head);
    cal.appendChild(grid);
    cal.appendChild(picked);

    label.parentNode.insertBefore(wrap, label);
    wrap.appendChild(label);
    wrap.appendChild(cal);
    input.classList.add("enhanced");
    input.tabIndex = -1;

    function describe(d) {
      return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
    }

    function render(focusDate) {
      title.textContent = view.toLocaleDateString("en-US", { month: "long", year: "numeric" });
      prev.disabled = view <= new Date(min.getFullYear(), min.getMonth(), 1);
      grid.textContent = "";
      DOW.forEach(function (d) {
        grid.appendChild(el("div", "cal-dow", d));
      });
      for (var i = 0; i < view.getDay(); i += 1) {
        grid.appendChild(el("div"));
      }
      var days = new Date(view.getFullYear(), view.getMonth() + 1, 0).getDate();
      var focusBtn = null;
      for (var day = 1; day <= days; day += 1) {
        var d = new Date(view.getFullYear(), view.getMonth(), day);
        var btn = el("button", "cal-day", String(day));
        btn.type = "button";
        btn.dataset.date = iso(d);
        btn.setAttribute(
          "aria-label",
          d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric" })
        );
        var isSel = selected && iso(d) === iso(selected);
        btn.setAttribute("aria-pressed", isSel ? "true" : "false");
        btn.tabIndex = -1;
        if (iso(d) === iso(today)) {
          btn.classList.add("today");
        }
        if (d < min) {
          btn.disabled = true;
        }
        grid.appendChild(btn);
        if (focusDate && iso(d) === iso(focusDate)) {
          focusBtn = btn;
        }
      }
      // Exactly one tab stop in the grid (roving tabindex): the day being moved to, else the
      // selected day, else the first enabled one.
      var stop = (focusBtn && !focusBtn.disabled ? focusBtn : null) ||
        grid.querySelector('.cal-day[aria-pressed="true"]:not(:disabled)') ||
        grid.querySelector(".cal-day:not(:disabled)");
      if (stop) {
        stop.tabIndex = 0;
      }
      picked.textContent = selected ? "Selected: " + describe(selected) : "";
      if (stop && stop === focusBtn) {
        stop.focus();
      }
    }

    function page(delta, focusDate) {
      var target = new Date(view.getFullYear(), view.getMonth() + delta, 1);
      if (target < new Date(min.getFullYear(), min.getMonth(), 1)) {
        return;
      }
      view = target;
      render(focusDate);
    }

    function choose(d) {
      selected = d;
      input.value = iso(d);
      input.dispatchEvent(new Event("change", { bubbles: true }));
      render(d);
    }

    prev.addEventListener("click", function () { page(-1); });
    next.addEventListener("click", function () { page(1); });

    grid.addEventListener("click", function (e) {
      var btn = e.target.closest(".cal-day");
      if (btn && !btn.disabled) {
        choose(parseIso(btn.dataset.date));
      }
    });

    grid.addEventListener("keydown", function (e) {
      var btn = e.target.closest(".cal-day");
      if (!btn) {
        return;
      }
      var d = parseIso(btn.dataset.date);
      var step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key];
      var to = null;
      if (step) {
        to = new Date(d.getFullYear(), d.getMonth(), d.getDate() + step);
      } else if (e.key === "PageUp" || e.key === "PageDown") {
        // Same day next/previous month, clamped (Jan 31 -> Feb 28, never Mar 3).
        var first = new Date(d.getFullYear(), d.getMonth() + (e.key === "PageUp" ? -1 : 1), 1);
        var last = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
        to = new Date(first.getFullYear(), first.getMonth(), Math.min(d.getDate(), last));
      } else {
        return;
      }
      e.preventDefault();
      if (to < min) {
        return;
      }
      if (to.getMonth() !== view.getMonth() || to.getFullYear() !== view.getFullYear()) {
        view = new Date(to.getFullYear(), to.getMonth(), 1);
      }
      render(to);
    });

    // Side-to-side swipe on touch screens.
    var startX = null;
    var startY = null;
    cal.addEventListener("touchstart", function (e) {
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
    }, { passive: true });
    cal.addEventListener("touchend", function (e) {
      if (startX === null) {
        return;
      }
      var dx = e.changedTouches[0].clientX - startX;
      var dy = e.changedTouches[0].clientY - startY;
      startX = null;
      if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy)) {
        page(dx < 0 ? 1 : -1);
      }
    }, { passive: true });

    // The native input is hidden, so replace the browser's "fill in this field" bubble.
    input.addEventListener("invalid", function (e) {
      e.preventDefault();
      picked.textContent = "Pick a date.";
      var first = grid.querySelector(".cal-day:not(:disabled)");
      if (first) {
        first.focus();
      }
    });

    render();
  }

  function init() {
    var forms = document.querySelectorAll("form.ranked");
    for (var i = 0; i < forms.length; i += 1) {
      enhance(forms[i]);
    }
    var dates = document.querySelectorAll("input.datepick");
    for (var j = 0; j < dates.length; j += 1) {
      if (dates[j].closest("label")) {
        calendar(dates[j]);
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
