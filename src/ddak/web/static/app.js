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
  const pendingForms = new WeakSet();
  const showFormError = (form, code, message) => {
    let box = form.nextElementSibling;
    if (!box?.matches("[data-form-error]")) {
      box = document.getElementById("form-error-template").content.firstElementChild.cloneNode(true);
      form.insertAdjacentElement("afterend", box);
    }
    box.classList.add("notice", "failure");
    box.setAttribute("role", "alert");
    box.querySelector("[data-error-code]").textContent = code;
    box.querySelector("[data-error-message]").textContent = message;
    box.hidden = false;
  };
  // 확인 전용 최소 VM도 지원하며 실제 POST 처리는 브라우저 API가 있을 때만 켠다.
  const forms = hasWindow ? new Set([
    ...document.querySelectorAll("form"),
    ...document.querySelectorAll("[data-confirm], [data-approval-form]"),
  ]) : [];
  forms.forEach((form) => {
    form.addEventListener("submit", async (event) => {
      if (event.defaultPrevented) return;
      if (pendingForms.has(form)) {
        event.preventDefault();
        return;
      }
      const message = form.dataset.confirm || (event.submitter?.value === "denied"
        ? "이 배포를 거절할까요? 이 실행은 여기서 끝나고 서비스는 지금 버전을 유지합니다." : "");
      if (message && !window.confirm(message)) {
        event.preventDefault();
        return;
      }
      if (typeof window.fetch !== "function" || typeof window.FormData !== "function"
        || typeof window.URLSearchParams !== "function" || typeof window.URL !== "function"
        || form.method.toLowerCase() !== "post") return;
      event.preventDefault();
      pendingForms.add(form);
      const buttons = Array.from(form.elements).filter((element) =>
        ["submit", "image"].includes(element.type));
      const buttonStates = buttons.map((button) => ({
        button, disabled: button.disabled, busy: button.getAttribute("aria-busy"),
      }));
      const loading = form.hasAttribute("data-deploy-form")
        ? document.querySelector("[data-deploy-loading]") : null;
      const loadingHidden = loading?.hidden;
      try {
        const action = new window.URL(form.action, window.location.href);
        if (action.origin !== window.location.origin) {
          showFormError(form, "REQUEST_FAILED", "요청을 처리하지 못했습니다. 다시 시도해 주세요.");
          return;
        }
        // 버튼을 잠그기 전에 직렬화하여 기존 필드와 클릭한 제출 버튼 값을 보존한다.
        const body = new window.URLSearchParams(new window.FormData(form));
        if (event.submitter?.name && !event.submitter.disabled) {
          body.append(event.submitter.name, event.submitter.value);
        }
        buttons.forEach((button) => {
          button.disabled = true;
          button.setAttribute("aria-busy", "true");
        });
        if (loading) loading.hidden = false;
        const box = form.nextElementSibling;
        if (box?.matches("[data-form-error]")) box.hidden = true;
        const response = await window.fetch(action.href, {
          method: "POST", credentials: "same-origin",
          headers: { "X-Ddak-Form": "1", "Accept": "text/html",
            "Content-Type": "application/x-www-form-urlencoded" },
          body,
        });
        if (!response.ok) {
          let error;
          try { error = (await response.json())?.error; } catch { /* 원문 응답은 표시하지 않는다. */ }
          const valid = typeof error?.code === "string" && typeof error?.message === "string";
          showFormError(form, valid ? error.code : "REQUEST_FAILED",
            valid ? error.message : "요청을 처리하지 못했습니다. 다시 시도해 주세요.");
          if (action.pathname === "/setup/docker") {
            const check = document.querySelector('[data-check="docker"]');
            const badge = check?.querySelector("[data-status]");
            if (badge) {
              badge.dataset.status = "red";
              badge.className = "state failure";
              badge.querySelector("span").textContent = "확인 필요";
            }
            const detail = check?.querySelector(".section-description");
            if (detail) detail.textContent = valid ? error.message : "Docker Hub 연결 확인 실패";
          }
          return;
        }
        if (response.redirected) {
          const destination = new window.URL(response.url);
          if (destination.origin !== window.location.origin) {
            showFormError(form, "REQUEST_FAILED", "요청을 처리하지 못했습니다. 다시 시도해 주세요.");
            return;
          }
          window.location.assign(destination.href);
        } else {
          window.location.reload();
        }
      } catch {
        showFormError(form, "REQUEST_FAILED", "요청을 처리하지 못했습니다. 다시 시도해 주세요.");
      } finally {
        buttonStates.forEach(({ button, disabled, busy }) => {
          button.disabled = disabled;
          if (busy === null) button.removeAttribute("aria-busy");
          else button.setAttribute("aria-busy", busy);
        });
        if (loading) loading.hidden = loadingHidden;
        pendingForms.delete(form);
      }
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
  const activeWork = new Map();
  const runningCells = new Map();
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
      node.textContent = failed ? (running ? "일부 실패 · 진행 중" : "실패") : running ? "진행 중" : values.includes("succeeded") ? "수신 작업 성공" : "건너뜀";
      node.classList.toggle("failed", failed);
      node.classList.toggle("active", running);
      if (running) runningCells.set(node, failed);
      else runningCells.delete(node);
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
    progress.dataset.stream = "live";
    setText("connection-state", `실시간 연결됨 · 최근 이벤트 ${time} (KST)`);
    if (data.step && data.type === "step.started") activeWork.set(data.step, `${target} · ${step}`);
    if (data.step && ["step.finished", "step.skipped"].includes(data.type)) activeWork.delete(data.step);
    if (data.type === "rollback.started") activeWork.set(`rollback:${track}`, `${target} · 이전 배포로 복구 중`);
    if (data.type === "rollback.finished") activeWork.delete(`rollback:${track}`);
    setText("activity-title", activeWork.size ? "작업 진행 중" : "배포 처리 중");
    setText("current-activity", activeWork.size
      ? [...activeWork.values()].join(" / ")
      : "다음 실행 상태를 기다리고 있습니다. 상세 로그는 작업 단위로 갱신됩니다.");
    if (data.type === "run.state") {
      progress.dataset.status = data.status;
      setText("progress-title", statusLabels[data.status] || data.status);
      if (terminalStates.includes(data.status)) {
        source.close();
        progress.dataset.activity = "ended";
        activeWork.clear();
        runningCells.forEach((failed, node) => {
          node.classList.remove("active");
          node.textContent = failed ? "일부 실패 · 결과 확인" : "결과 확인";
        });
        runningCells.clear();
        setText("activity-title", statusLabels[data.status] || data.status);
        document.getElementById("result-link")?.classList.remove("hidden");
        setText("connection-state", "실행 종료");
        setText("current-activity", "실행이 끝났습니다. 환경별 최종 상태는 결과 화면에서 확인하세요.");
      }
    }
  };
  Object.keys(typeLabels).forEach((name) => source.addEventListener(name, append));
  source.onopen = () => {
    if (terminalStates.includes(progress.dataset.status)) return;
    progress.dataset.stream = "live";
    setText("activity-title", activeWork.size ? "작업 진행 중" : "배포 처리 중");
    setText("connection-state", "실시간 연결됨 · 이벤트 시각은 KST");
  };
  source.onerror = () => {
    if (terminalStates.includes(progress.dataset.status)) return;
    progress.dataset.stream = "reconnecting";
    setText("activity-title", "연결 재시도 중 · 실행 상태 확인 대기");
    setText("connection-state", "실시간 연결이 끊겼습니다. 배포 중단 여부는 아직 확인되지 않았으며 자동으로 다시 연결합니다.");
  };
})();
