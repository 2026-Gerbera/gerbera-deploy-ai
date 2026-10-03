(() => {
  const hasWindow = typeof window !== "undefined";
  const sidebar = document.querySelector("#sidebar");
  const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
  const shell = document.querySelector(".app-shell");
  if (hasWindow && sidebar && sidebarToggle && shell) {
    const storageKey = "gerbera.sidebar";
    let saved;
    try { saved = window.localStorage.getItem(storageKey); } catch { /* 저장 없이도 탐색 가능 */ }
    let open = saved === "open" || (saved !== "closed" && !window.matchMedia?.("(max-width: 900px)").matches);
    const renderSidebar = () => {
      if (!open && sidebar.contains(document.activeElement)) sidebarToggle.focus();
      sidebar.hidden = !open;
      shell.dataset.sidebarOpen = String(open);
      sidebarToggle.setAttribute("aria-expanded", String(open));
      sidebarToggle.textContent = open ? "메뉴 접기" : "메뉴 펼치기";
    };
    const toggleSidebar = () => {
      open = !open;
      renderSidebar();
      try { window.localStorage.setItem(storageKey, open ? "open" : "closed"); } catch { /* 일시적 선택 유지 */ }
    };
    sidebarToggle.hidden = false;
    renderSidebar();
    sidebarToggle.addEventListener("click", toggleSidebar);
    sidebar.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && open) { event.preventDefault(); toggleSidebar(); }
    });
  }
  const toasts = document.querySelectorAll("[data-toast]");
  const patchSelection = document.querySelector("[data-patch-selection]");
  if (patchSelection) {
    const choices = patchSelection.querySelectorAll("input[type='checkbox']");
    const count = patchSelection.querySelector("[data-selection-count]");
    const updateSelection = () => {
      const selected = [...choices].filter((input) => input.checked).length;
      if (count) count.textContent = `${choices.length}개 중 ${selected}개 선택`;
    };
    choices.forEach((input) => input.addEventListener("change", updateSelection));
    updateSelection();
  }
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
  const bindForms = () => {
    document.querySelectorAll("[data-deploy-form], [data-confirm], [data-approval-form]").forEach((form) => {
      if (form.dataset.bound) return;
      form.dataset.bound = "true";
      form.addEventListener("submit", (event) => {
        if (form.dataset.submitting) { event.preventDefault(); return; }
        const confirmation = form.dataset.confirm || (event.submitter?.value === "denied"
          ? "이 배포를 거절할까요? 이 실행은 여기서 끝나고 서비스는 지금 버전을 유지합니다." : "");
        if (confirmation && hasWindow && !window.confirm(confirmation)) { event.preventDefault(); return; }
        form.dataset.submitting = "true";
        const button = event.submitter || form.querySelector("button[type='submit']");
        if (button) {
          // decision 값이 POST에서 빠지지 않도록 submitter는 disabled로 바꾸지 않는다.
          button.setAttribute("aria-busy", "true");
          button.setAttribute("aria-disabled", "true");
          button.textContent = button.value === "approved" ? "승인 보내는 중" : "요청 중";
        }
        const message = document.querySelector("[data-deploy-loading]");
        if (message) message.hidden = false;
      });
    });
  };
  bindForms();
  const secondsText = (value) => {
    const seconds = Math.max(0, Math.floor(value));
    return seconds >= 60 ? `${Math.floor(seconds / 60)}분 ${seconds % 60}초` : `${seconds}초`;
  };
  const waitingSince = new Map();
  const updateWaiting = () => document.querySelectorAll("[data-wait-clock]").forEach((node) => {
    const key = node.dataset.waitClock;
    if (!waitingSince.has(key)) waitingSince.set(key, Date.now());
    node.textContent = secondsText((Date.now() - waitingSince.get(key)) / 1000);
  });
  if (hasWindow && document.querySelector("[data-live-region]")) {
    let stopped = false, timer, controller;
    const clock = window.setInterval(updateWaiting, 1000);
    updateWaiting();
    const schedule = (delay = 3000) => { if (!stopped) timer = window.setTimeout(poll, delay); };
    const poll = async () => {
      if (stopped) return;
      if (document.hidden) { schedule(); return; }
      const message = document.querySelector("[data-refresh-state]");
      controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 8000);
      let delay = 3000;
      try {
        const response = await fetch(window.location.href, { signal: controller.signal, cache: "no-store" });
        if (!response.ok) throw new Error("refresh");
        const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
        const next = parsed.querySelector("[data-live-region]");
        const current = document.querySelector("[data-live-region]");
        if (!next || !current) throw new Error("refresh markup");
        const editing = current.contains(document.activeElement) &&
          ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
        if (!stopped && !editing && next.dataset.liveVersion !== current.dataset.liveVersion) {
          current.replaceWith(document.importNode(next, true));
          for (const selector of ["[data-ops-state]", "[data-unlock-note]"]) {
            const live = document.querySelector(selector), refreshed = parsed.querySelector(selector);
            if (live && refreshed) live.replaceWith(document.importNode(refreshed, true));
          }
          const unlock = document.querySelector("[data-unlock-button]");
          const nextUnlock = parsed.querySelector("[data-unlock-button]");
          if (unlock && nextUnlock) unlock.disabled = nextUnlock.disabled;
          bindForms();
          updateWaiting();
        }
        if (!stopped && message) message.textContent = `최신 상태 확인 · ${new Date().toLocaleTimeString("ko-KR", { hour12: false })}`;
      } catch {
        delay = 6000;
        if (!stopped && message) message.textContent = "자동 갱신 연결이 끊겼습니다. 표시된 기록을 유지하며 다시 확인합니다.";
      } finally {
        window.clearTimeout(timeout);
        schedule(delay);
      }
    };
    schedule();
    window.addEventListener?.("pagehide", () => {
      stopped = true;
      window.clearTimeout(timer);
      window.clearInterval(clock);
      controller?.abort();
    }, { once: true });
  }
  if (hasWindow) window.addEventListener?.("pageshow", (event) => {
    if (event.persisted) window.location.reload();
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
    PARITY_FAILED: "두 환경 결과가 다름", NEEDS_HUMAN: "직접 확인 필요", SUPERSEDED: "새 승인 자료로 대체됨", CANCELLED: "취소됨",
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
  const trackWork = { local: {}, cloud: {}, common: {} };
  let lastEventAt = null, finished = false, connected = false;
  const timestamp = (value) => {
    const parsed = value ? new Date(value).getTime() : NaN;
    return Number.isFinite(parsed) ? parsed : null;
  };
  const workHint = (step) => step.includes("infra") ? "Terraform 적용은 작업이 끝날 때까지 새 기록이 없을 수 있습니다."
    : step.startsWith("build.") ? "이미지를 만드는 중입니다. 빌드가 끝나면 해당 환경의 다음 작업으로 이어집니다."
    : step.includes("tls") ? "인증서와 DNS 응답을 확인하고 있습니다."
    : step.startsWith("verify.") ? "응답과 기본 동작을 검사하고 있습니다."
    : "이 작업이 끝나면 다음 기록을 표시합니다.";
  const updateWork = () => {
    for (const track of ["local", "cloud", "common"]) {
      const active = Object.values(trackWork[track]).filter((item) => item.running);
      if (active.length) {
        setText(`${track}-activity`, active.map((item) => item.label).join(" · "));
        setText(`${track}-work-note`, workHint(active[0].step));
        const dates = active.map((item) => item.started).filter((value) => value !== null);
        setText(`${track}-work-age`, dates.length ? `현재 작업 시작 후 ${secondsText((Date.now() - Math.min(...dates)) / 1000)}` : "작업 시작 시각 기록 없음");
      }
    }
    if (!finished && lastEventAt !== null) {
      const age = Math.max(0, (Date.now() - lastEventAt) / 1000);
      setText("event-age", age < 45 ? `마지막 이벤트 ${secondsText(age)} 전`
        : `새 이벤트 없이 ${secondsText(age)} 경과. ${connected ? "연결은 유지 중이며 작업 완료를 기다립니다." : "연결이 복구되면 기록을 다시 받습니다."}`);
    }
  };
  let workClock;
  if (hasWindow && typeof window.setInterval === "function") {
    workClock = window.setInterval(updateWork, 1000);
    window.addEventListener?.("pagehide", () => { source.close(); window.clearInterval(workClock); }, { once: true });
  }
  const seen = new Set();
  const append = (event) => {
    if (finished) return;
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (data.seq !== undefined) {
      if (seen.has(data.seq)) return;
      seen.add(data.seq);
    }
    if (data.step) activatePhase(data);
    lastEventAt = timestamp(data.ts) ?? Date.now();
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
    if (data.step && data.type.startsWith("step.")) {
      const key = data.step;
      const title = stepLabels[data.step.replace(/\.(local|cloud)$/, "")] || data.step;
      const bucket = trackWork[track] || trackWork.common;
      bucket[key] = { label: title, step: key, started: timestamp(data.ts), running: data.type === "step.started" };
      if (!Object.values(bucket).some((item) => item.running)) {
        setText(`${track}-activity`, `${title} · ${statusLabels[data.status] || typeLabels[data.type] || "기록 확인"}`);
        setText(`${track}-work-note`, data.detail || "다음 작업이나 최종 결과를 기다립니다.");
        setText(`${track}-work-age`, data.elapsed_s != null ? `이 작업 ${secondsText(data.elapsed_s)}` : "");
      }
    }
    if (data.type === "gate.waiting") {
      setText(`${track}-activity`, data.detail === "images_ready" ? "이미지 빌드를 기다립니다" : "앞 단계 완료를 기다립니다");
      setText(`${track}-work-note`, "준비된 환경은 독립적으로 다음 작업을 시작합니다.");
    }
    if (data.type.startsWith("rollback.")) {
      trackWork[track] = {};
      setText(`${track}-activity`, data.type === "rollback.started" ? "이전 버전으로 되돌리는 중" : "되돌리기 처리 결과 확인 중");
      setText(`${track}-work-note`, "복구 성공 여부는 최종 결과에서 확인하세요.");
    }
    updateWork();
    if (data.type === "run.state") {
      progress.dataset.status = data.status;
      setText("progress-title", statusLabels[data.status] || data.status);
      if (terminalStates.includes(data.status)) {
        finished = true;
        source.close();
        progress.dataset.activity = "ended";
        activeWork.clear();
        runningCells.forEach((failed, node) => {
          node.classList.remove("active");
          node.textContent = failed ? "일부 실패 · 결과 확인" : "결과 확인";
        });
        runningCells.clear();
        setText("activity-title", statusLabels[data.status] || data.status);
        if (hasWindow) window.clearInterval(workClock);
        document.querySelectorAll(".flow-step").forEach((node) => {
          if (["이벤트 대기", "진행 중"].includes(node.textContent)) node.textContent = "최종 기록 확인";
        });
        for (const name of ["local", "cloud", "common"]) {
          const excluded = (name === "local" && progress.dataset.targets === "cloud")
            || (name === "cloud" && ["local", "onprem"].includes(progress.dataset.targets));
          setText(`${name}-activity`, excluded ? "이번 배포 대상이 아닙니다." : "실행 종료");
          setText(`${name}-work-note`, excluded ? "" : "복구 여부와 환경별 판정은 결과에서 확인하세요.");
          setText(`${name}-work-age`, "");
        }
        setText("event-age", "최종 상태를 받았습니다.");
        document.getElementById("result-link")?.classList.remove("hidden");
        setText("connection-state", "실행 종료");
        setText("current-activity", "실행이 끝났습니다. 환경별 최종 상태는 결과 화면에서 확인하세요.");
      }
    }
  };
  Object.keys(typeLabels).forEach((name) => source.addEventListener(name, append));
  source.onopen = () => {
    if (terminalStates.includes(progress.dataset.status)) return;
    connected = true;
    progress.dataset.stream = "live";
    setText("activity-title", activeWork.size ? "작업 진행 중" : "배포 처리 중");
    setText("connection-state", "실시간 연결됨 · 이벤트 시각은 KST");
  };
  source.onerror = () => {
    if (terminalStates.includes(progress.dataset.status)) return;
    connected = false;
    progress.dataset.stream = "reconnecting";
    setText("activity-title", "연결 재시도 중 · 실행 상태 확인 대기");
    setText("connection-state", "실시간 연결이 끊겼습니다. 배포 중단 여부는 아직 확인되지 않았으며 자동으로 다시 연결합니다.");
  };
})();
