(() => {
  // 서버 사전이 없는 기존 페이지와 최소 Node VM에서는 한국어를 유지한다.
  const fallbackWording = {
    "menu.close": "메뉴 접기",
    "menu.open": "메뉴 펼치기",
    "form.error": "작업을 완료하지 못했습니다. 기술 정보에서 원인을 확인하세요.",
    "form.deny": "이 배포를 거절할까요? 이 실행은 여기서 끝나고 서비스는 지금 버전을 유지합니다.",
    "form.request_failed": "요청을 처리하지 못했습니다. 다시 시도해 주세요.",
    "check.needed": "확인 필요",
    "check.docker_failed": "Docker Hub 연결 확인 실패",
    "refresh.disconnected": "자동 갱신 연결이 끊겼습니다. 표시된 기록을 유지하며 다시 확인합니다.",
    "run.running": "배포 중",
    "run.approved": "승인됨 · 곧 시작",
    "run.succeeded": "배포 완료",
    "run.failed_local": "온프레미스 배포 실패",
    "run.failed_cloud": "클라우드 배포 실패",
    "run.failed_verify": "배포 후 검증 실패",
    "run.failed_before_deploy": "배포 전에 중단",
    "run.parity_failed": "두 환경 결과가 다름",
    "run.needs_human": "직접 확인 필요",
    "run.cancelled": "취소됨",
    "run.superseded": "새 승인 자료로 대체됨",
    "state.running": "진행 중",
    "state.waiting": "대기",
    "state.succeeded": "완료",
    "state.failed": "실패",
    "state.check_failed": "검사 불합격",
    "state.skipped": "건너뜀",
    "state.unrecorded": "기록 없음",
    "event.state": "상태",
    "event.started": "시작",
    "event.gate_waiting": "앞 단계 대기",
    "event.gate_opened": "앞 단계 완료",
    "event.gate_failed": "앞 단계 실패",
    "event.rollback_started": "복구 시작",
    "event.rollback_finished": "복구 완료",
    "event.stage_finished": "준비 단계 기록",
    "event.report_ready": "보고 완료",
    "event.ai_call": "제안 기록",
    "task.name": "실행 작업",
    "task.running": "작업이 진행 중입니다.",
    "task.finished": "작업을 마쳤습니다.",
    "task.failed": "작업을 완료하지 못했습니다.",
    "task.waiting": "앞 단계 완료를 기다립니다.",
    "task.skipped": "이번 실행에서는 건너뛰었습니다.",
    "state.checking": "확인 중",
    "task.check_record": "실행 기록을 확인합니다.",
    "task.start": "작업 시작",
    "time.unrecorded": "시간 기록 없음",
    "time.start_unrecorded": "시작 시각 기록 없음",
    "connection.waiting": "연결 유지 · 완료 기록 대기",
    "connection.reconnecting": "다시 연결하는 중",
    "gate.previous": "앞 단계",
    "gate.failed": "앞 단계 실패로 대기를 해제했습니다. 이 작업은 실행하지 않습니다.",
    "compare.skipped": "한 환경이 실패해 두 환경을 비교하지 않았습니다.",
    "task.previous_unrecorded": "앞 작업의 실행 기록 없음",
    "state.check_record": "기록 확인",
    "task.unaffected": "이 환경은 영향 없이 계속 진행합니다.",
    "task.candidate_ready": "실행 단계 시작 · 승인본 소스 준비 완료로 추정합니다.",
    "time.missing": "시각 없음",
    "event.record": "기록",
    "connection.live": "실시간 연결됨 · 이벤트 시각은 KST",
    "task.next_preparing": "다음 작업 준비 중",
    "track.local": "온프레미스",
    "track.cloud": "클라우드",
    "track.common": "공통",
    "task.next_waiting": "다음 실행 상태를 기다립니다.",
    "run.check_state": "실행 상태 확인",
    "task.not_run": "실행 안 함 · 앞 단계 실패로 실행 기록이 없습니다.",
    "task.no_completion": "완료 기록이 없습니다. 결과를 확인하세요.",
    "run.ended": "실행 종료",
    "run.result": "실행이 끝났습니다. 환경별 최종 상태는 결과 화면에서 확인하세요.",
    "run.final_received": "최종 상태를 받았습니다.",
    "connection.lost": "실시간 연결이 끊겼습니다. 배포 중단 여부는 아직 확인되지 않았으며 자동으로 다시 연결합니다.",
    "selection.count": "{total}개 중 {selected}개 선택",
    "time.minutes": "{minutes}분 {seconds}초",
    "time.seconds": "{seconds}초",
    "refresh.checked": "최신 상태 확인 · {time}",
    "time.running": "진행 {elapsed}",
    "time.lane": "환경 시작 후 {elapsed}",
    "steps.remaining": "남은 단계 {count}개{estimate}",
    "steps.estimate": " · 이전 성공 기준 약 {elapsed}",
    "steps.processed": "{done}/{total} 작업 처리",
    "event.age": "마지막 이벤트 {elapsed} 전 · {connection}",
    "gate.waiting": "{gate} 완료를 기다립니다.",
    "locale": "ko-KR"
};
  let appWording = {};
  try { appWording = JSON.parse(document.getElementById("app-wording")?.textContent || "{}"); } catch { /* 한국어 기본값 유지 */ }
  const t = (key, params = {}) => {
    const value = appWording?.[key];
    const template = typeof value === "string" ? value : fallbackWording[key] ?? key;
    // 원문 언어 변환 없이 명시적인 매개변수 자리만 보간한다.
    return template.split(/(\{[a-z]+\})/).map((part) => {
      const name = part.slice(1, -1);
      return part.startsWith("{") && Object.prototype.hasOwnProperty.call(params, name)
        ? String(params[name]) : part;
    }).join("");
  };
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
      sidebarToggle.textContent = open ? t("menu.close") : t("menu.open");
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
      if (count) count.textContent = t("selection.count", { total: choices.length, selected });
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
  let errorDictionary = {};
  try { errorDictionary = JSON.parse(document.getElementById("narrative-errors")?.textContent || "{}"); } catch { /* 초기 표시를 유지한다. */ }
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
    box.querySelector("[data-error-message]").textContent = errorDictionary[code] || t("form.error");
    const detail = box.querySelector("[data-error-detail]");
    if (detail) detail.textContent = message;
    box.hidden = false;
    box.scrollIntoView?.({ block: "nearest", behavior: "smooth" });
  };
  const boundForms = new WeakSet();
  const bindForms = () => {
    // 확인 전용 최소 VM도 지원하며 실제 POST 처리는 브라우저 API가 있을 때만 켠다.
    const forms = hasWindow ? new Set([
      ...document.querySelectorAll("form"),
      ...document.querySelectorAll("[data-deploy-form], [data-confirm], [data-approval-form]"),
    ]) : [];
    forms.forEach((form) => {
      if (boundForms.has(form)) return;
      boundForms.add(form);
      const dirty = () => { form.dataset.dirty = "true"; };
      form.addEventListener("input", dirty);
      form.addEventListener("change", dirty);
      form.addEventListener("submit", async (event) => {
        if (event.defaultPrevented) return;
        if (pendingForms.has(form)) {
          event.preventDefault();
          return;
        }
        const message = form.dataset.confirm || (event.submitter?.value === "denied"
          ? t("form.deny") : "");
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
          // name="action" 제출 버튼은 form.action 속성을 가릴 수 있다.
          const action = new window.URL(
            form.getAttribute("action") || window.location.href, window.location.href);
          if (action.origin !== window.location.origin) {
            showFormError(form, "REQUEST_FAILED", t("form.request_failed"));
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
              valid ? error.message : t("form.request_failed"));
            if (action.pathname === "/setup/docker") {
              const check = document.querySelector('[data-check="docker"]');
              const badge = check?.querySelector("[data-status]");
              if (badge) {
                badge.dataset.status = "red";
                badge.className = "state failure";
                badge.querySelector("span").textContent = t("check.needed");
              }
              const detail = check?.querySelector(".section-description");
              if (detail) detail.textContent = valid ? error.message : t("check.docker_failed");
            }
            return;
          }
          if (response.redirected) {
            const destination = new window.URL(response.url);
            if (destination.origin !== window.location.origin) {
              showFormError(form, "REQUEST_FAILED", t("form.request_failed"));
              return;
            }
            window.location.assign(destination.href);
          } else {
            window.location.reload();
          }
        } catch {
          showFormError(form, "REQUEST_FAILED", t("form.request_failed"));
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
  };
  bindForms();
  const secondsText = (value) => {
    const seconds = Math.max(0, Math.floor(value));
    return seconds >= 60 ? t("time.minutes", { minutes: Math.floor(seconds / 60), seconds: seconds % 60 }) : t("time.seconds", { seconds });
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
      let delay = 3000, next = null;
      try {
        const response = await fetch(document.querySelector("[data-live-region]")?.dataset.liveUrl || window.location.href, { signal: controller.signal, cache: "no-store" });
        if (!response.ok) throw new Error("refresh");
        if (response.url && (response.redirected || new window.URL(response.url).pathname !== window.location.pathname)) {
          const destination = new window.URL(response.url);
          if (destination.origin === window.location.origin) { stopped = true; window.location.assign(destination.href); return; }
          throw new Error("refresh origin");
        }
        const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
        next = parsed.querySelector("[data-live-region]");
        const current = document.querySelector("[data-live-region]");
        if (!next || !current) throw new Error("refresh markup");
        const editing = (current.contains(document.activeElement) &&
          ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) ||
          [...current.querySelectorAll("form")].some((form) => pendingForms.has(form) || form.dataset.dirty === "true");
        if (!stopped && !editing && next.dataset.liveVersion !== current.dataset.liveVersion) {
          const replacement = document.importNode(next, true);
          // 폼이 사라지는 상태 전환에서도 오류는 남기고 실행 상태는 갱신한다.
          current.querySelectorAll("form").forEach((form) => {
            const box = form.nextElementSibling;
            if (!box?.matches("[data-form-error]") || box.hidden) return;
            const id = form.querySelector('[name="_form_id"]')?.value;
            const nextForm = [...replacement.querySelectorAll("form")].find((item) =>
              id && item.querySelector('[name="_form_id"]')?.value === id);
            if (nextForm) {
              const nextBox = nextForm.nextElementSibling;
              if (nextBox?.matches("[data-form-error]")) nextBox.replaceWith(box);
              else nextForm.insertAdjacentElement("afterend", box);
            } else document.querySelector("[data-refresh-errors]")?.append(box);
          });
          current.replaceWith(replacement);
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
        if (next.dataset.summaryState && next.dataset.summaryState !== "pending") {
          stopped = true;
          window.clearInterval(clock);
        }
        if (!stopped && message) message.textContent = t("refresh.checked", { time: new Date().toLocaleTimeString(t("locale"), { hour12: false, timeZone: "Asia/Seoul" }) });
      } catch {
        delay = 6000;
        // 응답을 받기 전 실패면 next가 없다. 받은 응답이 요약 완료를 알렸을 때만 멈춘다.
        if (next?.dataset.summaryState && next.dataset.summaryState !== "pending") {
          stopped = true;
          window.clearInterval(clock);
        }
        if (!stopped && message) message.textContent = t("refresh.disconnected");
      } finally {
        window.clearTimeout(timeout);
        schedule(delay);
      }
    };
    schedule();
    window.addEventListener?.("ddak-track-result", () => { window.clearTimeout(timer); poll(); });
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
      // 서버는 온프레미스여도 도메인을 저장하면 DNS 설정을 검증한다.
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
  let dictionary = { texts: {}, aliases: {}, gates: {}, errors: {} };
  try { dictionary = JSON.parse(document.getElementById("pipeline-wording")?.textContent || "{}"); } catch { /* 서버 기록을 유지한다. */ }
  const source = new EventSource(`/runs/${encodeURIComponent(progress.dataset.runId)}/events`);
  const labels = { RUNNING: t("run.running"), APPROVED: t("run.approved"), SUCCEEDED: t("run.succeeded"), FAILED_LOCAL: t("run.failed_local"), FAILED_CLOUD: t("run.failed_cloud"), FAILED_VERIFY: t("run.failed_verify"), FAILED_BEFORE_DEPLOY: t("run.failed_before_deploy"), PARITY_FAILED: t("run.parity_failed"), NEEDS_HUMAN: t("run.needs_human"), CANCELLED: t("run.cancelled"), SUPERSEDED: t("run.superseded") };
  const states = { running: t("state.running"), waiting: t("state.waiting"), succeeded: t("state.succeeded"), failed: t("state.failed"), check_failed: t("state.check_failed"), skipped: t("state.skipped"), unrecorded: t("state.unrecorded") };
  const typeLabels = { "run.state": t("event.state"), "step.started": t("event.started"), "step.finished": t("state.succeeded"), "step.skipped": t("state.skipped"), "gate.waiting": t("event.gate_waiting"), "gate.opened": t("event.gate_opened"), "gate.failed": t("event.gate_failed"), "rollback.started": t("event.rollback_started"), "rollback.finished": t("event.rollback_finished"), "stage.finished": t("event.stage_finished"), "report.ready": t("event.report_ready"), "ai.call": t("event.ai_call") };
  // 이벤트 처리와 경과 시계에서 조회하지 않도록 노드를 한 번만 찾는다.
  const ids = ["progress-title", "activity-title", "current-activity", "candidate-activity", "now-working", "current-step-clock", "total-clock", "remaining-steps", "result-link", "event-age", "connection-state", "pipeline-final"];
  for (const track of ["local", "cloud", "common"]) ids.push(`${track}-activity`, `${track}-work-note`, `${track}-work-age`, `${track}-events`, `pipeline-${track}`, `${track}-progress`, `${track}-progress-label`);
  const nodes = new Map(ids.map((id) => [id, document.getElementById(id)]));
  const rail = [...document.querySelectorAll("[data-rail-step]")];
  const lanes = new Map();
  document.querySelectorAll("[data-step-row]").forEach((node) => {
    const wrapper = node.querySelector("[data-step-status]");
    const status = wrapper?.querySelector?.(".state") || wrapper;
    const sentence = node.querySelector("[data-step-sentence]");
    lanes.set(node.dataset.stepRow, { node, status, statusLabel: status?.querySelector?.("span"), title: node.querySelector("[data-step-title]"), outcome: node.querySelector("[data-step-outcome]"), sentence, recordedOutcome: node.dataset.stepState === "succeeded" ? sentence?.textContent : null, recordedStart: node.dataset.started, age: node.querySelector("[data-step-age]"), detail: node.querySelector("[data-step-detail]"), summary: node.querySelector("[data-step-summary]") });
  });
  const active = new Map(), seen = new Set();
  let finished = false, connected = false, lastEventAt = null, clock;
  const timestamp = (value) => { const date = Date.parse(value || ""); return Number.isNaN(date) ? null : date; };
  const setText = (id, value) => { const node = nodes.get(id); if (node) node.textContent = value; };
  const textFor = (id, tool) => {
    const base = String(id || "").replace(/\.(local|cloud)$/, "");
    const key = dictionary.aliases?.[base] || (base.startsWith("build.") ? "build_image" : base.startsWith("deploy.") ? "deploy_tier" : base);
    return dictionary.texts?.[id] || dictionary.texts?.[tool] || dictionary.texts?.[key] || { name: t("task.name"), running: t("task.running"), finished: t("task.finished"), failed: t("task.failed"), waiting: t("task.waiting"), skipped: t("task.skipped") };
  };
  const makeRow = (id, track, text) => {
    const list = nodes.get(["verify.compare", "verify.report", "verify.diagnose"].includes(id) ? "pipeline-final" : `pipeline-${track}`);
    if (!list) return null;
    const node = document.createElement("li"); node.className = "pipeline-step"; node.dataset.stepRow = id; node.dataset.lane = track;
    const detail = document.createElement("details"), summary = document.createElement("summary"), title = document.createElement("strong"), outcome = document.createElement("span"), status = document.createElement("span"), sentence = document.createElement("p"), age = document.createElement("span");
    detail.dataset.stepDetail = ""; summary.dataset.stepSummary = ""; summary.className = "step-heading";
    title.dataset.stepTitle = ""; title.textContent = text.name; outcome.dataset.stepOutcome = "";
    status.dataset.stepStatus = ""; sentence.dataset.stepSentence = ""; age.dataset.stepAge = ""; age.className = "numeric";
    summary.appendChild(title); summary.appendChild(outcome); summary.appendChild(status);
    detail.appendChild(summary); detail.appendChild(sentence); detail.appendChild(age); node.appendChild(detail); list.appendChild(node);
    const row = { node, title, outcome, status, sentence, age, detail, summary }; lanes.set(id, row); return row;
  };
  const renderRow = (row, state, sentence, elapsed, ts, text) => {
    row.node.dataset.stepState = state;
    if (state === "running") row.node.dataset.started = ts || "";
    row.node.classList.remove("active", "complete", "failed", "is-running");
    if (state === "succeeded") row.node.classList.add("complete");
    if (["failed", "check_failed"].includes(state)) row.node.classList.add("failed");
    if (state === "running") row.node.classList.add("active", "is-running");
    const kind = state === "succeeded" ? "success" : ["failed", "check_failed"].includes(state) ? "failure" : state === "running" ? "running" : state === "waiting" ? "waiting" : "neutral";
    row.status.className = `state ${kind}`; row.status.dataset.code = state;
    if (!row.statusLabel) {
      const icon = document.createElement("i"); icon.setAttribute("aria-hidden", "true");
      row.statusLabel = document.createElement("span"); row.status.appendChild(icon); row.status.appendChild(row.statusLabel);
    }
    row.statusLabel.textContent = states[state] || t("state.checking");
    if (text && row.title) { row.title.textContent = text.name; row.title.classList.toggle("state", !!text.warning); row.title.classList.toggle("failure", !!text.warning); }
    row.sentence.textContent = sentence || t("task.check_record");
    if (row.detail) row.detail.open = state !== "succeeded";
    if (row.outcome) { row.outcome.textContent = sentence || ""; row.outcome.hidden = state !== "succeeded"; }
    row.age.textContent = elapsed != null ? secondsText(elapsed) : state === "running" ? t("task.start") : t("time.unrecorded");
  };
  const updateClock = () => {
    lanes.forEach((row) => { const start = timestamp(row.node.dataset.started); if (row.node.dataset.stepState === "running" && start !== null) row.age.textContent = t("time.running", { elapsed: secondsText((Date.now() - start) / 1000) }); });
    for (const track of ["local", "cloud", "common"]) {
      const work = [...active.values()].filter((item) => item.track === track);
      if (work.length) { setText(`${track}-activity`, work.map((item) => item.text.name).join(" · ")); setText(`${track}-work-note`, work[0].text.running); }
      const laneStart = timestamp(nodes.get(`${track}-work-age`)?.dataset.laneStarted);
      if (!finished) setText(`${track}-work-age`, laneStart !== null ? t("time.lane", { elapsed: secondsText((Date.now() - laneStart) / 1000) }) : t("time.start_unrecorded"));
    }
    const started = timestamp(progress.dataset.started);
    if (!finished && started !== null) setText("total-clock", secondsText((Date.now() - started) / 1000));
    const activeStarts = [...active.values()].map((item) => item.start).filter((value) => value !== null);
    setText("current-step-clock", activeStarts.length ? secondsText((Date.now() - Math.min(...activeStarts)) / 1000) : "—");
    const remaining = [...lanes.values()].filter((row) => ["waiting", "running"].includes(row.node.dataset.stepState));
    const known = remaining.every((row) => row.node.dataset.expected && Number.isFinite(Number(row.node.dataset.expected)));
    setText("remaining-steps", t("steps.remaining", { count: remaining.length, estimate: remaining.length && known ? t("steps.estimate", { elapsed: secondsText(remaining.reduce((sum, row) => sum + Number(row.node.dataset.expected), 0)) }) : "" }));
    for (const track of ["local", "cloud"]) {
      const rows = [...lanes.values()].filter((row) => row.node.dataset.lane === track);
      const done = rows.filter((row) => ["succeeded", "failed", "check_failed", "skipped"].includes(row.node.dataset.stepState)).length;
      const bar = nodes.get(`${track}-progress`); if (bar) { bar.max = rows.length || 1; bar.value = done; }
      setText(`${track}-progress-label`, t("steps.processed", { done, total: rows.length }));
    }
    if (!finished && lastEventAt !== null) setText("event-age", t("event.age", { elapsed: secondsText((Date.now() - lastEventAt) / 1000), connection: connected ? t("connection.waiting") : t("connection.reconnecting") }));
  };
  const syncActivity = () => {
    const working = connected && !finished;
    nodes.get("now-working")?.classList.toggle("is-working", working && active.size > 0);
    lanes.forEach((row) => row.node.classList.toggle("is-running", working && row.node.dataset.stepState === "running"));
  };
  const append = (event) => {
    if (finished) return;
    let data; try { data = JSON.parse(event.data); } catch { return; }
    if (data.seq !== undefined) { if (seen.has(data.seq)) return; seen.add(data.seq); }
    connected = true;
    const track = ["local", "cloud"].includes(data.target) ? data.target : data.step?.endsWith(".local") ? "local" : data.step?.endsWith(".cloud") ? "cloud" : "common";
    const id = data.type === "stage.finished" ? data.preparation_stage : data.type.startsWith("rollback.") ? `rollback.${track}` : data.step;
    const text = textFor(id, data.tool);
    lastEventAt = timestamp(data.ts) ?? Date.now();
    if (id && !["gate.opened", "ai.call", "report.ready"].includes(data.type)) {
      const state = data.type.endsWith(".started") ? "running" : data.type === "gate.waiting" ? "waiting" : data.type === "gate.failed" ? "failed" : data.type === "step.skipped" ? "skipped" : data.status || "waiting";
      let sentence = state === "running" ? text.running : state === "succeeded" ? text.finished : state === "waiting" ? text.waiting : state === "skipped" ? text.skipped : text.failed;
      if (data.type === "gate.waiting") sentence = t("gate.waiting", { gate: dictionary.gates?.[data.detail] || t("gate.previous") });
      if (data.type === "gate.failed") sentence = t("gate.failed");
      if (id === "verify.compare" && state === "skipped") sentence = t("compare.skipped");
      if (["failed", "check_failed"].includes(state)) { const code = String(data.detail || "").split(":", 1)[0]; sentence = dictionary.errors?.[code] || sentence; }
      if (data.type === "step.started") {
        const laneClock = nodes.get(`${track}-work-age`), first = timestamp(laneClock?.dataset.laneStarted), start = timestamp(data.ts);
        if (laneClock && start !== null && (first === null || start < first)) laneClock.dataset.laneStarted = data.ts;
        for (const [previous, entry] of lanes) {
          if (previous === id) break;
          if (entry.node.dataset.lane === track && entry.node.dataset.stepState === "waiting") renderRow(entry, "unrecorded", t("task.previous_unrecorded"));
        }
      }
      const row = lanes.get(id) || makeRow(id, track, text);
      // 재연결로 받은 완료 이벤트에 요약 숫자가 없으면 서버가 렌더한 완료 문장을 유지한다.
      if (row && state === "succeeded") {
        if (row.recordedOutcome && timestamp(row.node.dataset.started) === timestamp(row.recordedStart)) sentence = row.recordedOutcome;
        else if (row.node.dataset.stepState === "succeeded") sentence = row.sentence.textContent || sentence;
      }
      if (row) renderRow(row, state, sentence, data.elapsed_s, data.ts, text);
      if (state === "running") active.set(id, { track, text, start: timestamp(data.ts) }); else active.delete(id);
      setText(`${track}-activity`, `${text.name} · ${states[state] || t("state.check_record")}`); setText(`${track}-work-note`, sentence);
      if (["failed", "check_failed"].includes(state) && track !== "common") setText(`${track === "local" ? "cloud" : "local"}-work-note`, t("task.unaffected"));
      const phase = id.startsWith("build.") ? "build" : id.startsWith("deploy.") ? "deploy" : id === "verify.compare" ? "compare" : id === "verify.report" ? "report" : id.startsWith("verify.") ? "verify" : id;
      if (state === "running") rail.forEach((node) => { if (node.dataset.railStep === phase) node.setAttribute("aria-current", "step"); else node.removeAttribute("aria-current"); });
    }
    if (hasWindow && data.type === "gate.opened" && ["local_verified", "cloud_verified"].includes(data.detail)) window.dispatchEvent?.(new window.Event("ddak-track-result"));
    if (data.type === "step.started") setText("candidate-activity", t("task.candidate_ready"));
    const list = nodes.get(`${track}-events`);
    if (list) {
      const item = document.createElement("li"), technical = document.createElement("small");
      const date = new Date(data.ts || Date.now());
      const time = Number.isNaN(date.getTime()) ? t("time.missing") : date.toLocaleTimeString(t("locale"), { hourCycle: "h23", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Seoul" });
      item.textContent = `${time} · ${text.name} · ${states[data.status] || typeLabels[data.type] || t("event.record")}${data.elapsed_s != null ? ` · ${secondsText(data.elapsed_s)}` : ""}`;
      technical.className = "tool-label"; technical.textContent = [id, data.type.startsWith("gate.") ? data.detail : null].filter(Boolean).join(" · "); item.appendChild(technical); list.appendChild(item);
    }
    progress.dataset.stream = "live"; setText("connection-state", t("connection.live"));
    syncActivity();
    setText("activity-title", active.size ? [...active.values()].map((item) => item.text.name).join(" · ") : t("task.next_preparing"));
    const warning = [...active.values()].some((item) => item.text.warning);
    for (const name of ["state", "failure"]) nodes.get("activity-title")?.classList.toggle(name, warning);
    setText("current-activity", active.size ? [...active.values()].map((item) => `${{ local: t("track.local"), cloud: t("track.cloud"), common: t("track.common") }[item.track]} · ${item.text.running}`).join(" / ") : t("task.next_waiting"));
    if (data.type === "run.state") {
      progress.dataset.status = data.status; setText("progress-title", labels[data.status] || t("run.check_state"));
      if (terminalStates.includes(data.status)) {
        finished = true; source.close(); active.clear(); progress.dataset.activity = "ended"; nodes.get("now-working")?.classList.remove("is-working");
        lanes.forEach((row) => { if (["running", "waiting"].includes(row.node.dataset.stepState)) renderRow(row, "unrecorded", data.status.startsWith("FAILED") || data.status === "NEEDS_HUMAN" ? t("task.not_run") : t("task.no_completion")); });
        nodes.get("result-link")?.classList.remove("hidden"); setText("activity-title", labels[data.status] || t("run.ended")); setText("current-activity", t("run.result")); setText("connection-state", t("run.ended")); setText("event-age", t("run.final_received"));
        if (hasWindow) window.clearInterval(clock);
      }
    }
    updateClock();
  };
  lanes.forEach((row, id) => { if (row.node.dataset.stepState === "running") active.set(id, { track: row.node.dataset.lane, text: textFor(id), start: timestamp(row.node.dataset.started) }); });
  syncActivity();
  updateClock();
  if (hasWindow) { clock = window.setInterval(updateClock, 1000); window.addEventListener?.("pagehide", () => { finished = true; source.close(); window.clearInterval(clock); }, { once: true }); }
  Object.keys(typeLabels).forEach((type) => source.addEventListener(type, append));
  source.onopen = () => { if (finished) return; connected = true; progress.dataset.stream = "live"; syncActivity(); setText("connection-state", t("connection.live")); };
  source.onerror = () => { if (finished) return; connected = false; progress.dataset.stream = "reconnecting"; syncActivity(); setText("connection-state", t("connection.lost")); };
})();
