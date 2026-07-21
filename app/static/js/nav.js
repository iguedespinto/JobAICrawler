/* In-place navigation.
 *
 * Progressive enhancement for the whole app: a form submit or a link click
 * inside the page's content is sent with fetch, and the response's content
 * region is swapped in without a full reload — so an action (save, close,
 * import, a filter change) never throws away the scroll position or blanks the
 * page. Every form and link still works with this script absent or broken:
 * anything we don't recognise, or any failure, falls back to the browser's own
 * navigation.
 *
 * The listeners are delegated and bound once on document, so they never stack.
 * The swap re-executes the scripts inside the new content — DOM-inserted
 * <script> tags don't run on their own — which is why each page's script binds
 * to elements within that content: the old nodes are discarded with their
 * listeners, the fresh ones are wired up again. This script lives outside the
 * content region and is loaded once, so it is never re-run.
 */
(function () {
  "use strict";

  if (!window.fetch || !window.history.pushState || !window.DOMParser) return;

  var CONTENT = ".main__inner";
  var busy = false;

  // We place the scroll ourselves; stop the browser second-guessing it on
  // back/forward.
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  function content() {
    return document.querySelector(CONTENT);
  }

  function samePath(a, b) {
    return new URL(a, location.href).pathname === new URL(b, location.href).pathname;
  }

  function sameOrigin(url) {
    return new URL(url, location.href).origin === location.origin;
  }

  // A DOM-inserted <script> does not execute. Swap each for a fresh element with
  // the same attributes and body so the page's own scripts run against the new
  // content, in document order.
  function runScripts(root) {
    var scripts = root.querySelectorAll("script");
    for (var i = 0; i < scripts.length; i++) {
      var old = scripts[i];
      var fresh = document.createElement("script");
      for (var j = 0; j < old.attributes.length; j++) {
        fresh.setAttribute(old.attributes[j].name, old.attributes[j].value);
      }
      fresh.textContent = old.textContent;
      old.replaceWith(fresh);
    }
  }

  // Put a fetched page's content region in place of the current one. Returns
  // false when the response has no region we recognise, so the caller can hand
  // Each page's own CSS lives in <head> (its {% block head %}), so it has to
  // travel with the swap as well — otherwise the destination renders wearing the
  // previous page's styles, which is invisible on a plain page and disfiguring on
  // a form. The shared stylesheet is a <link>, so every <style> here is a page's
  // own and can be replaced wholesale.
  function syncPageStyles(doc) {
    var current = document.head.querySelectorAll("style");
    for (var i = 0; i < current.length; i++) current[i].remove();
    var incoming = doc.head.querySelectorAll("style");
    for (var j = 0; j < incoming.length; j++) {
      document.head.appendChild(incoming[j].cloneNode(true));
    }
  }

  // back to a real navigation instead of blanking the page.
  function swap(html) {
    var doc = new DOMParser().parseFromString(html, "text/html");
    var next = doc.querySelector(CONTENT);
    var current = content();
    if (!next || !current) return false;
    syncPageStyles(doc);
    current.replaceWith(next);
    if (doc.title) document.title = doc.title;
    runScripts(next);
    return true;
  }

  // Fetch a destination and swap it in. `opts.method`/`opts.body` carry a form;
  // `opts.form` is the element to re-submit natively if the network itself fails
  // (only safe when the request never reached the server).
  function visit(url, opts) {
    opts = opts || {};
    if (busy) return;
    busy = true;

    var fromUrl = location.href;
    var fromScroll = window.scrollY;
    var init = { credentials: "same-origin" };
    if (opts.method) init.method = opts.method;
    if (opts.body) init.body = opts.body;

    document.documentElement.classList.add("is-navigating");

    var finalUrl = url;
    fetch(url, init)
      .then(function (response) {
        finalUrl = response.url || url;
        return response.text();
      })
      .then(function (html) {
        // The action ran but we got something we can't swap (a JSON error, a
        // login page): let the browser show it, by GET, so a POST is never
        // silently repeated.
        if (!swap(html)) {
          window.location.href = finalUrl;
          return;
        }

        // Record where we were so Back can restore this scroll, then decide the
        // new one: stay put when the action kept us on the same page (a save, a
        // filter on the same list), go to the top when the path changed and it
        // is genuinely a different page.
        history.replaceState({ y: fromScroll }, "");
        var stay = samePath(finalUrl, fromUrl);
        var y = stay ? fromScroll : 0;
        if (finalUrl !== fromUrl) history.pushState({ y: y }, "", finalUrl);
        else history.replaceState({ y: y }, "", finalUrl);
        window.scrollTo(0, y);
      })
      .catch(function () {
        // Network failure: the server never answered, so re-submitting a form is
        // safe; a link just navigates.
        if (opts.form && opts.method) opts.form.submit();
        else window.location.href = url;
      })
      .then(function () {
        busy = false;
        document.documentElement.classList.remove("is-navigating");
      });
  }

  function handledForm(form) {
    return (
      form instanceof HTMLFormElement &&
      !form.hasAttribute("data-native") &&
      !form.getAttribute("target") &&
      sameOrigin(form.action) &&
      content() &&
      content().contains(form)
    );
  }

  function handledLink(a) {
    if (!a || !a.getAttribute("href")) return false;
    var href = a.getAttribute("href");
    if (href.charAt(0) === "#") return false; // in-page anchor
    return (
      !a.hasAttribute("data-native") &&
      !a.hasAttribute("download") &&
      !a.getAttribute("target") && // external "open in new tab" links
      a.id !== "load-more" && // owns its own fetch-and-append
      sameOrigin(a.href) &&
      content() &&
      content().contains(a)
    );
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!handledForm(form)) return;
    e.preventDefault();

    var method = (form.getAttribute("method") || "get").toUpperCase();
    var data = new FormData(form);
    // FormData omits the submit button that triggered the submit; put it back so
    // a form whose action depends on which button was pressed still works.
    if (e.submitter && e.submitter.name) data.append(e.submitter.name, e.submitter.value);

    if (method === "GET") {
      var url = new URL(form.action, location.href);
      url.search = new URLSearchParams(data).toString();
      visit(url.toString(), {});
    } else {
      visit(form.action, { method: method, body: data, form: form });
    }
  });

  document.addEventListener("click", function (e) {
    if (e.defaultPrevented) return; // a page script already claimed it
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest("a");
    if (!handledLink(a)) return;
    e.preventDefault();
    visit(a.href, {});
  });

  // Back/forward: re-fetch the entry and restore the scroll we saved for it.
  window.addEventListener("popstate", function (e) {
    var y = e.state && typeof e.state.y === "number" ? e.state.y : 0;
    fetch(location.href, { credentials: "same-origin" })
      .then(function (response) {
        return response.text();
      })
      .then(function (html) {
        if (swap(html)) window.scrollTo(0, y);
        else window.location.reload();
      })
      .catch(function () {
        window.location.reload();
      });
  });
})();
