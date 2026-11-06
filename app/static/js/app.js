// KANAANI — tiny progressive enhancements (the app works without JavaScript).
(function () {
  "use strict";

  // Bar charts from data-series='[{"label":"11-01","value":3}, …]'
  function drawChart(el) {
    var data;
    try { data = JSON.parse(el.getAttribute("data-series") || "[]"); } catch (e) { return; }
    if (!data.length) return;
    var max = Math.max.apply(null, data.map(function (d) { return Math.abs(d.value); })) || 1;
    var total = 0;
    data.forEach(function (d, i) {
      total += d.value;
      var b = document.createElement("div");
      b.className = "bar" + (d.value === 0 ? " zero" : "");
      b.style.height = Math.max(3, Math.round(Math.abs(d.value) / max * 100)) + "%";
      b.style.animationDelay = (i * 12) + "ms";
      b.setAttribute("data-tip", d.label + ": " + d.value);
      b.setAttribute("title", d.label + ": " + d.value);
      el.appendChild(b);
    });
    var axis = document.createElement("div");
    axis.className = "axis";
    axis.innerHTML = "";
    [data[0].label, data[Math.floor(data.length / 2)].label, data[data.length - 1].label].forEach(function (t) {
      var s = document.createElement("span"); s.textContent = t; axis.appendChild(s);
    });
    el.appendChild(axis);
    var sum = document.createElement("div");
    sum.className = "chart-total";
    sum.textContent = "Total: " + total;
    el.parentNode.insertBefore(sum, el.nextSibling);
  }

  // Confirm dangerous actions: <form data-confirm="…">
  function guardForms() {
    document.querySelectorAll("form[data-confirm]").forEach(function (f) {
      f.addEventListener("submit", function (e) {
        if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault();
      });
    });
    // Block double submits (the server is also idempotent).
    document.querySelectorAll("form[method=post]").forEach(function (f) {
      f.addEventListener("submit", function (e) {
        if (e.defaultPrevented) return;
        if (f.dataset.sent) { e.preventDefault(); return; }
        f.dataset.sent = "1";
        f.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".chart[data-series]").forEach(drawChart);
    document.querySelectorAll(".bars i[data-w]").forEach(function (i) {
      requestAnimationFrame(function () { i.style.width = i.getAttribute("data-w") + "%"; });
    });
    guardForms();
  });
})();
