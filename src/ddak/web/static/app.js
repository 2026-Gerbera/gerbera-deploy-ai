(() => {
  const progress = document.querySelector("[data-run-id]");
  if (!progress) return;
  const runId = progress.dataset.runId;
  const source = new EventSource(`/runs/${encodeURIComponent(runId)}/events`);
  const phases = ["plan", "infra", "build", "deploy", "verify"];
  const activatePhase = (step = "") => {
    const value = String(step).toLowerCase();
    let selected = value.includes("infra") || value.includes("terraform") ? "infra" :
      value.includes("build") || value.includes("image") ? "build" :
      value.includes("deploy") || value.includes("ecs") || value.includes("migrate") ? "deploy" :
      value.includes("verify") || value.includes("health") || value.includes("tls") || value.includes("smoke") || value.includes("report") ? "verify" : "plan";
    const activeIndex = phases.indexOf(selected);
    document.querySelectorAll("[data-phase]").forEach((node) => {
      const index = phases.indexOf(node.dataset.phase);
      node.classList.toggle("active", index === activeIndex);
      node.classList.toggle("complete", index < activeIndex);
    });
  };
  const append = (event) => {
    const data = JSON.parse(event.data);
    activatePhase(data.step || data.type);
    const id = data.target === "local" ? "local-events" : data.target === "cloud" ? "cloud-events" : "common-events";
    const list = document.getElementById(id);
    list.querySelector(".event-placeholder")?.remove();
    const item = document.createElement("li");
    item.className = "event";
    item.textContent = `${data.type} · ${data.step || data.status || data.detail || ""}`;
    list.appendChild(item);
    if (data.type === "run.state" && ["SUCCEEDED","FAILED_BEFORE_DEPLOY","FAILED_LOCAL","FAILED_CLOUD","PARITY_FAILED","CANCELLED","NEEDS_HUMAN"].includes(data.status)) {
      source.close();
      document.getElementById("result-link").classList.remove("hidden");
    }
  };
  ["run.state","step.started","step.finished","step.skipped","gate.waiting","gate.opened","gate.failed","rollback.started","rollback.finished","report.ready"].forEach(name => source.addEventListener(name, append));
  source.onerror = () => {
    const list = document.getElementById("common-events");
    list.querySelector(".event-placeholder")?.remove();
    const item = document.createElement("li");
    item.className = "event";
    item.textContent = "실시간 연결을 재시도하고 있습니다.";
    list.appendChild(item);
  };
})();
