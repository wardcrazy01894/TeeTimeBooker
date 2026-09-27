/* Progressive enhancement only (the CSP allows same-origin script, never inline). Every page
   works without this file: the ranked booking form's extra option rows sit inside a
   <details class="more-options"> disclosure, and the server ignores rows with no course.

   With script, the disclosure becomes "Add another time slot": extra rows are hidden until
   asked for, one at a time, and a row can be removed again (which blanks its course so the
   server skips it). */
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

  function init() {
    var forms = document.querySelectorAll("form.ranked");
    for (var i = 0; i < forms.length; i += 1) {
      enhance(forms[i]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
