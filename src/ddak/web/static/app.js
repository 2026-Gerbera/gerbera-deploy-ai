(() => {
  const hasWindow = typeof window !== "undefined";
  const toasts = document.querySelectorAll("[data-toast]");
  toasts.forEach((toast) => {
    const dismiss = () => toast.remove();
    toast.querySelector("[data-toast-close]")?.addEventListener("click", dismiss);
    if (hasWindow) window.setTimeout(dismiss, 5000);
  });
  if (hasWindow && toasts.length && new URLSearchParams(window.location.search).get("saved") === "1") {
    const cleanUrl = new URL(window.location.href);
    cleanUrl.searchParams.delete("saved");
    window.history.replaceState({}, "", cleanUrl);
  }
  document.querySelectorAll("[data-deploy-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button[type='submit']");
      if (button) {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
        button.textContent = "요청 중";
      }
      const message = document.querySelector("[data-deploy-loading]");
      if (message) message.hidden = false;
    });
  });
  document.querySelectorAll("[data-confirm], [data-approval-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      const message = form.dataset.confirm || (event.submitter?.value === "denied"
        ? "이 배포를 거절할까요? 이 실행은 여기서 끝나고 서비스는 지금 버전을 유지합니다." : "");
      if (message && hasWindow && !window.confirm(message)) event.preventDefault();
    });
  });
  const targetSelect = document.querySelector("select[name='default_targets']");
  const cloudSettings = document.querySelector("[data-cloud-settings]");
  const dnsSelect = document.querySelector("select[name='dns_mode']");
  const zoneField = document.querySelector("[data-zone-field]");
  if (targetSelect && cloudSettings && dnsSelect && zoneField) {
    const domain = cloudSettings.querySelector("input[name='cloud_domain']");
    const zone = zoneField.querySelector("input");
    const updateCloudFields = (resetOpen = false) => {
      const cloud = targetSelect.value !== "onprem";
      if (resetOpen) cloudSettings.open = cloud;
      if (domain) domain.required = cloud;
      // 서버는 온프렘이라도 도메인을 저장하면 DNS 설정을 검증한다.
      const needed = (cloud || Boolean(domain?.value.trim())) && dnsSelect.value === "route53";
      zoneField.hidden = dnsSelect.value !== "route53";
      if (zone) {
        zone.required = needed;
        if (needed && !zone.value.trim()) cloudSettings.open = true;
      }
    };
    targetSelect.addEventListener("change", () => updateCloudFields(true));
    dnsSelect.addEventListener("change", () => updateCloudFields());
    domain?.addEventListener("input", () => updateCloudFields());
    updateCloudFields(true);
  }

  const progress = document.querySelector("[data-run-id]");
  if (!progress) return;
  const terminalStates = JSON.parse(progress.dataset.terminalStates || "[]");
  if (terminalStates.includes(progress.dataset.status)) return;
  const source = new EventSource(`/runs/${encodeURIComponent(progress.dataset.runId)}/events`);
  const phases = ["plan", "infra", "build", "deploy", "verify"];
  const maximum = { local: -1, cloud: -1 };
  const records = { local: {}, cloud: {} };
  const statusLabels = {
    APPROVED: "승인됨 · 곧 시작", RUNNING: "배포 중", DEPLOYING: "배포 중", FINALIZING: "마무리 중",
    SUCCEEDED: "배포 완료", FAILED_LOCAL: "온프렘 배포 실패", FAILED_CLOUD: "클라우드 배포 실패",
    FAILED_VERIFY: "배포 후 검증 실패", FAILED_BEFORE_DEPLOY: "배포 전에 중단",
    PARITY_FAILED: "두 환경 결과가 다름", NEEDS_HUMAN: "직접 확인 필요", SUPERSEDED: "새 커밋으로 대체됨", CANCELLED: "취소됨",
    succeeded: "성공", failed: "실패", check_failed: "검사 불합격", skipped: "건너뜀",
  };
  const typeLabels = { "run.state": "상태 바뀜", "step.started": "시작", "step.finished": "완료",
    "step.skipped": "건너뜀", "gate.waiting": "앞 단계 기다림", "gate.opened": "앞 단계 끝남",
    "gate.failed": "앞 단계 실패", "rollback.started": "되돌리는 중", "rollback.finished": "되돌림 끝", "report.ready": "보고서 준비됨" };
  const stepLabels = { "build.was": "앱 서버 이미지 빌드", "build.web": "웹 서버 이미지 빌드",
    "deploy.infra": "인프라 적용", "deploy.tls": "HTTPS 인증서 준비", "deploy.secrets": "비밀값 동기화",
    "deploy.config": "환경 설정 넣기", "deploy.migrate": "DB 마이그레이션", "deploy.dbinit": "DB 초기화",
    "deploy.was": "앱 서버 배포", "deploy.web": "웹 서버 배포", "deploy.app": "앱 배포",
    "verify.health": "응답 확인", "verify.smoke": "기본 동작 확인", "verify.tls": "HTTPS 확인",
    "verify.compare": "두 환경 결과 비교", "verify.report": "결과 보고", "verify.watch": "배포 후 지켜보기" };
  const setText = (id, text) => {
    const node = document.getElementById(id);
    if (node) node.textContent = text;
  };
  const activatePhase = (data) => {
    // 기존 VM 계약: step 이벤트당 정확히 한 번 조회한다.
    const nodes = document.querySelectorAll("[data-phase]");
    const step = String(data.step);
    const phase = step.startsWith("build.") ? "build"
      : /^deploy\.(infra|tls|secrets)(\.|$)/.test(step) ? "infra"
      : step.startsWith("deploy.") ? "deploy"
      : step.startsWith("verify.") && !["verify.compare", "verify.report"].includes(step) ? "verify" : null;
    if (!phase || !data.type.startsWith("step.")) return;
    const tracks = data.target ? [data.target] : step.endsWith(".local") ? ["local"]
      : step.endsWith(".cloud") ? ["cloud"]
      : phase === "build" && ["both", "cloud", "onprem", "local"].includes(progress.dataset.targets)
        ? ["local", "cloud"] : [];
    tracks.forEach((track) => {
      if (!records[track]) return;
      maximum[track] = Math.max(maximum[track], phases.indexOf(phase));
      records[track][phase] ||= {};
      records[track][phase][step] = data.type === "step.started" ? "running" : data.status || "skipped";
    });
    nodes.forEach((node) => {
      const track = node.dataset.track;
      if (node.dataset.excluded === "true" || !tracks.includes(track)) return;
      const values = Object.values(records[track]?.[node.dataset.phase] || {});
      if (!values.length) return;
      const failed = values.some((value) => ["failed", "check_failed"].includes(value));
      const running = values.includes("running");
      node.textContent = failed ? "실패" : running ? "진행 중" : values.includes("succeeded") ? "단계 성공" : "건너뜀";
      node.classList.toggle("failed", failed);
      node.classList.toggle("active", !failed && phases.indexOf(node.dataset.phase) === maximum[track]);
      node.classList.toggle("complete", !failed && !running && values.includes("succeeded"));
    });
  };
  const seen = new Set();
  const append = (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (data.seq !== undefined) {
      if (seen.has(data.seq)) return;
      seen.add(data.seq);
    }
    if (data.step) activatePhase(data);
    const track = data.target || (data.step?.endsWith(".local") ? "local" : data.step?.endsWith(".cloud") ? "cloud" : "common");
    const target = { local: "온프렘", cloud: "클라우드", common: "공통" }[track] || "공통";
    const step = data.step ? `${stepLabels[data.step.replace(/\.(local|cloud)$/, "")] || data.step} (${data.step})` : "";
    const date = data.ts ? new Date(data.ts) : null;
    const time = date && !Number.isNaN(date.getTime()) ? date.toLocaleTimeString("ko-KR", { hour12: false, timeZone: "Asia/Seoul" }) : "시각 없음";
    const description = [time, target, step, statusLabels[data.status] || typeLabels[data.type] || data.type,
      data.elapsed_s != null ? `${Number(data.elapsed_s).toFixed(1)}초` : "", data.detail || ""].filter(Boolean).join(" · ");
    const list = document.getElementById(track === "local" ? "local-events" : track === "cloud" ? "cloud-events" : "common-events");
    if (list) {
      list.querySelector(".event-placeholder")?.remove();
      const item = document.createElement("li");
      item.className = ["failed", "check_failed"].includes(data.status) || data.type === "gate.failed" ? "event failed" : "event";
      item.textContent = description;
      list.appendChild(item);
    }
    if (data.type === "step.started") setText("current-activity", `${target} · ${step} 시작`);
    if (data.type === "run.state") {
      progress.dataset.status = data.status;
      setText("progress-title", statusLabels[data.status] || data.status);
      if (terminalStates.includes(data.status)) {
        source.close();
        document.getElementById("result-link")?.classList.remove("hidden");
        setText("connection-state", "실행 종료");
        setText("current-activity", "실행이 끝났습니다. 환경별 최종 상태는 결과 화면에서 확인하세요.");
      }
    }
  };
  Object.keys(typeLabels).forEach((name) => source.addEventListener(name, append));
  source.onopen = () => setText("connection-state", "실시간 연결됨 · 이벤트 시각은 KST");
  source.onerror = () => setText("connection-state", "실시간 연결이 끊겨 다시 연결하는 중입니다.");
})();
