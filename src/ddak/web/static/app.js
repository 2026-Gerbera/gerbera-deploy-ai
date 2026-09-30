(() => {
  const progress = document.querySelector("[data-run-id]");
  if (!progress) return;
  const runId = progress.dataset.runId;
  const source = new EventSource(`/runs/${encodeURIComponent(runId)}/events`);
  const append = (event) => {
    const data = JSON.parse(event.data);
    const id = data.target === "local" ? "local-events" : data.target === "cloud" ? "cloud-events" : "common-events";
    const item = document.createElement("li");
    item.className = "event";
    item.textContent = `${data.type} · ${data.step || data.status || data.detail || ""}`;
    document.getElementById(id).appendChild(item);
    if (data.type === "run.state" && ["SUCCEEDED","FAILED_BEFORE_DEPLOY","FAILED_LOCAL","FAILED_CLOUD","PARITY_FAILED","CANCELLED","NEEDS_HUMAN"].includes(data.status)) {
      source.close();
      document.getElementById("result-link").classList.remove("hidden");
    }
  };
  ["run.state","step.started","step.finished","step.skipped","gate.waiting","gate.opened","gate.failed","rollback.started","rollback.finished","report.ready"].forEach(name => source.addEventListener(name, append));
  source.onerror = () => document.getElementById("common-events").insertAdjacentHTML("beforeend", "<li>연결을 재시도합니다.</li>");
})();
