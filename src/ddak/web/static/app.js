(() => {
  const toasts = document.querySelectorAll("[data-toast]");
  toasts.forEach((toast) => {
    const dismiss = () => {
      toast.classList.add("toast-leaving");
      window.setTimeout(() => toast.remove(), 260);
    };
    toast.querySelector("[data-toast-close]")?.addEventListener("click", dismiss);
    window.setTimeout(dismiss, 3600);
  });
  // 저장 알림을 띄운 뒤에만 주소의 saved 표시를 지워 새로고침 때 다시 뜨지 않게 한다.
  if (toasts.length && new URLSearchParams(window.location.search).get("saved") === "1") {
    const cleanUrl = new URL(window.location.href);
    cleanUrl.searchParams.delete("saved");
    window.history.replaceState({}, "", cleanUrl);
  }

  const deployForm = document.querySelector("[data-deploy-form]");
  const deployLoading = document.querySelector("[data-deploy-loading]");
  if (deployForm && deployLoading) {
    const messages = [
      "저장소의 최신 코드를 확인하고 있습니다.",
      "변경사항과 배포 조건을 분석하고 있습니다.",
      "배포 대상에 맞는 실행 계획을 만들고 있습니다.",
      "승인에 필요한 자료를 정리하고 있습니다.",
    ];
    deployForm.addEventListener("submit", () => {
      const button = deployForm.querySelector("button[type='submit']");
      if (button) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        button.innerHTML = "<span class=\"button-spinner\"></span> 준비 중";
      }
      deployLoading.hidden = false;
      document.body.classList.add("modal-open");
      const message = deployLoading.querySelector("[data-loading-message]");
      const stages = [...deployLoading.querySelectorAll(".loading-stages span")];
      let index = 0;
      window.setInterval(() => {
        index = Math.min(index + 1, messages.length - 1);
        if (message) message.textContent = messages[index];
        stages.forEach((stage, stageIndex) => stage.classList.toggle("active", stageIndex <= index));
      }, 3800);
    });
  }

  const progress = document.querySelector("[data-run-id]");
  if (!progress) return;
  const runId = progress.dataset.runId;
  const terminalStates = JSON.parse(progress.dataset.terminalStates || "[]");
  if (terminalStates.includes(progress.dataset.status)) return;
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
    if (data.step) activatePhase(data.step);
    const id = data.target === "local" ? "local-events" : data.target === "cloud" ? "cloud-events" : "common-events";
    const list = document.getElementById(id);
    list.querySelector(".event-placeholder")?.remove();
    const item = document.createElement("li");
    item.className = "event";
    item.textContent = `${data.type} · ${data.step || data.status || data.detail || ""}`;
    list.appendChild(item);
    if (data.type === "run.state" && terminalStates.includes(data.status)) {
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
