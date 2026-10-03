(function () {
  var storageKey = "theme";
  var root = document.documentElement;

  function preferredTheme() {
    var stored = null;
    try {
      stored = window.localStorage.getItem(storageKey);
    } catch (_error) {
      stored = null;
    }
    if (stored === "light" || stored === "dark") {
      return stored;
    }
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }

  function applyTheme(theme) {
    if (theme === "dark") {
      root.setAttribute("data-theme", "dark");
    } else {
      root.removeAttribute("data-theme");
    }
  }

  applyTheme(preferredTheme());

  window.ewsTheme = {
    toggle: function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        window.localStorage.setItem(storageKey, next);
      } catch (_error) {
        /* 隐私模式下无法持久化时仅应用当前页 */
      }
      return next;
    },
  };
})();
