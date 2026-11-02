(function () {
  // Market watch moved to its own page; keep links to the old section working.
  if (location.hash === "#markets") { location.replace("/markets/"); return; }
  var root = document.documentElement;
  function set(l) {
    root.dataset.lang = l;
    root.lang = l === "zh" ? "zh-CN" : "en";
    try { localStorage.setItem("lang", l); } catch (e) {}
  }
  var saved = null;
  try { saved = localStorage.getItem("lang"); } catch (e) {}
  set(saved || (/^zh/i.test(navigator.language || "") ? "zh" : "en"));
  document.getElementById("lang").addEventListener("click", function () {
    set(root.dataset.lang === "zh" ? "en" : "zh");
  });
})();
