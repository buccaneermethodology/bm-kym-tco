"use strict";
(function () {
  const search = document.getElementById("search");
  const tp = document.getElementById("tp-filter");
  const m = document.getElementById("m-filter");
  const cards = Array.from(document.querySelectorAll(".capability"));
  function filter() {
    const query = search.value.toLocaleLowerCase();
    const visible = new Set();
    cards.forEach(function (card) {
      const matches = (!tp.value || JSON.parse(card.dataset.tps).includes(tp.value)) &&
        (!m.value || card.dataset.m === m.value) && card.textContent.toLocaleLowerCase().includes(query);
      card.hidden = !matches;
      if (matches) visible.add(card.dataset.m);
    });
    document.querySelectorAll(".profile").forEach(function (card) {
      card.hidden = Boolean(tp.value && card.dataset.profile !== tp.value);
    });
    document.getElementById("filter-count").textContent = "当前显示 " + visible.size + " / " + new Set(cards.map(function (card) { return card.dataset.m; })).size + " 个唯一能力/规则";
  }
  [search, tp, m].forEach(function (control) { control.addEventListener("input", filter); });
  document.getElementById("expand").addEventListener("click", function () {
    cards.forEach(function (card) { if (!card.hidden) card.open = true; });
  });
  function revealHash() {
    const target = document.getElementById(decodeURIComponent(location.hash.slice(1)));
    if (!target) return;
    let node = target;
    while (node) {
      if (node.tagName === "DETAILS") { node.open = true; node.hidden = false; }
      node = node.parentElement;
    }
    target.scrollIntoView({block: "center"});
  }
  window.addEventListener("hashchange", revealHash);
  filter();
  revealHash();
})();
