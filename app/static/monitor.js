(() => {
  const $ = (s) => document.querySelector(s);
  const conn = $("#conn"), log = $("#log"), runBtn = $("#run"), runMsg = $("#run-msg");
  const MAX_ROWS = 200;

  const setConn = (cls, text) => { conn.className = "conn conn--" + cls; conn.textContent = text; };

  function renderState(s) {
    for (const [name, w] of Object.entries(s.workers)) {
      const li = document.querySelector(`[data-worker="${name}"]`);
      if (!li) continue;
      li.querySelector(".worker__dot").className = "worker__dot worker__dot--" + w.status;
      li.querySelector(".worker__state").textContent = w.status + (w.detail ? " · " + w.detail : "");
    }
    for (const [k, v] of Object.entries(s.today)) {
      const el = document.querySelector(`#today [data-k="${k}"]`);
      if (el) el.textContent = k === "llm_cost" ? Number(v).toFixed(4) : v;
    }
    const box = $("#run-box"), r = s.run;
    if (r) {
      const done = r.processed + r.failed;
      box.innerHTML = `<p class="mon__line">#${r.id} · ${r.source} · ${r.reason} · начат ${r.started_at}</p>
        <progress class="bar" value="${done}" max="${r.planned || 1}"></progress>
        <p class="mon__line">${done} / ${r.planned} · ошибок ${r.failed}</p>`;
      runBtn.disabled = true;
    } else {
      box.innerHTML = '<p class="empty">Обход не идёт.</p>';
      runBtn.disabled = false;
    }
  }

  function addLog(e) {
    if (log.querySelector(".empty")) log.innerHTML = "";
    const li = document.createElement("li");
    li.className = "log__row log__row--" + e.type;
    li.innerHTML = `<span class="log__at"></span><span class="log__type"></span><span class="log__msg"></span>`;
    li.children[0].textContent = (e.at || "").slice(11, 19);
    li.children[1].textContent = e.type;
    li.children[2].textContent = e.message || e.detail || "";
    log.prepend(li);
    while (log.children.length > MAX_ROWS) log.lastElementChild.remove();
  }

  // EventSource reconnects on its own; we only surface the state to the user.
  let source;
  function connect() {
    source = new EventSource("/monitor/stream");
    source.addEventListener("open", () => setConn("live", "в эфире"));
    source.addEventListener("state", (m) => renderState(JSON.parse(m.data)));
    source.addEventListener("log", (m) => addLog(JSON.parse(m.data)));
    source.addEventListener("error", () => setConn("down", "переподключение…"));
  }
  connect();

  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true;                       // no double-submit while we wait
    runMsg.textContent = "";
    try {
      const res = await fetch("/monitor/run", { method: "POST" });
      const body = await res.json();
      runMsg.textContent = body.detail;
      runMsg.className = "mon__msg " + (res.ok ? "mon__msg--ok" : "mon__msg--warn");
      if (!res.ok) runBtn.disabled = false;
    } catch (err) {
      runMsg.textContent = "не удалось отправить запрос: " + err;
      runMsg.className = "mon__msg mon__msg--warn";
      runBtn.disabled = false;
    }
  });
})();
