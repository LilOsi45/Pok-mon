/* Row actions through ONE delegated listener instead of hx-* per button.
 *
 * Why: with 60 watches the table carried ~300 hx-* elements. Every 30 s poll
 * made htmx tear all of them down and initialise 300 fresh ones, which is what
 * made the dashboard stutter on a phone and turned "add a watch" into a long
 * blank frame. With delegation a table refresh is plain HTML replacement and
 * htmx has nothing to re-scan.
 *
 * Buttons declare what they do in data-attributes:
 *   data-url      request target (required — this is what marks a row button)
 *   data-method   GET | POST (default POST)
 *   data-target   CSS selector to swap into
 *   data-swap     htmx swap style (default innerHTML)
 *   data-confirm  ask first; abort when the user declines
 */
(function () {
  "use strict";

  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest("button[data-url]");
    if (!btn) return;
    evt.preventDefault();

    if (btn.dataset.confirm && !window.confirm(btn.dataset.confirm)) return;

    // reuse the existing dimming style so the button still feels pressed
    btn.classList.add("htmx-request");
    var release = function () {
      btn.classList.remove("htmx-request");
    };

    htmx
      .ajax(btn.dataset.method || "POST", btn.dataset.url, {
        target: btn.dataset.target,
        swap: btn.dataset.swap || "innerHTML"
      })
      .then(release, release);
  });
})();
