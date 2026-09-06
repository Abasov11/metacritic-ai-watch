// Refresh button on a game card. The status strip itself is swapped by HTMX; this
// only fires the POST and reports what the server said.
(() => {
  const MAX_WAIT_MS = 3 * 60 * 1000;
  let startedAt = 0;

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("#refresh-btn");
    if (!button) return;

    const message = document.querySelector("#refresh-msg");
    button.disabled = true;
    if (message) message.textContent = "";
    try {
      const response = await fetch(`/game/${button.dataset.slug}/refresh`, { method: "POST" });
      const body = await response.json();
      if (message) {
        message.textContent = body.detail;
        message.className = "refresh__msg " + (response.ok ? "refresh__msg--ok" : "refresh__msg--warn");
      }
      if (response.ok) {
        startedAt = Date.now();
        htmx.trigger("#game-status", "refresh-started");
      } else {
        button.disabled = false;
      }
    } catch (error) {
      if (message) {
        message.textContent = "не удалось отправить запрос: " + error;
        message.className = "refresh__msg refresh__msg--warn";
      }
      button.disabled = false;
    }
  });

  // Once the strip reports it is no longer busy, the data on the page is stale.
  document.body.addEventListener("htmx:afterSwap", (event) => {
    if (event.detail.target.id !== "game-status") return;
    const stillBusy = event.detail.target.querySelector(".refresh__state--busy");
    if (!stillBusy && startedAt) {
      startedAt = 0;
      location.reload();
    } else if (startedAt && Date.now() - startedAt > MAX_WAIT_MS) {
      startedAt = 0;
      const message = document.querySelector("#refresh-msg");
      if (message) {
        message.textContent = "обновление идёт дольше трёх минут, перезагрузите страницу";
        message.className = "refresh__msg refresh__msg--warn";
      }
    }
  });
})();
