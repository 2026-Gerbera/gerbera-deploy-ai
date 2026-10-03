/* 대시보드 '직접 배포 준비'의 코드 질문. 답만 받아 보여 주며 배포·패치·커밋 요청은 보내지 않는다.
   답과 오류는 textContent로만 그린다(AI 출력의 HTML·링크를 해석하지 않는다). */
(function () {
  if (typeof document === "undefined" || typeof window === "undefined") return;
  const text = (tag, value) => {
    const element = document.createElement(tag);
    element.textContent = value;
    return element;
  };
  document.querySelectorAll("[data-code-qa]").forEach((root) => {
    if (root.dataset.ready) return;
    root.dataset.ready = "1";
    const { url, project, csrf } = root.dataset;
    const toggle = root.querySelector("[data-code-qa-toggle]");
    const panel = root.querySelector("[data-code-qa-panel]");
    const form = root.querySelector("[data-code-qa-form]");
    const input = root.querySelector("[data-code-qa-input]");
    const submit = root.querySelector("[data-code-qa-submit]");
    const box = root.querySelector("[data-code-qa-answer]");
    // 버튼은 '지금 배포 준비' 옆, 입력칸은 그 폼 바로 아래로 옮긴다(공유 템플릿 수정 최소화).
    const plan = root.parentElement?.querySelector('form[action="/ops/plan"]');
    const deploy = plan?.querySelector('button[type="submit"]');
    if (plan && deploy) {
      deploy.insertAdjacentElement("afterend", toggle);
      plan.parentElement.appendChild(panel);
      root.hidden = true;
    }
    toggle.addEventListener("click", () => {
      const open = panel.hidden;
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      if (open) input.focus();
    });
    const show = (kind, build) => {
      box.hidden = false;
      box.className = kind ? `notice ${kind}` : "notice";
      if (kind === "failure") box.setAttribute("role", "alert");
      else box.removeAttribute("role");
      box.replaceChildren();
      build(box);
    };
    const fail = (code, message) => show("failure", (b) => {
      b.append(text("strong", code), text("p", message));
    });
    let busy = false;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy) return;
      const question = input.value.trim();
      if (!question) {
        fail("CONFIG_INVALID", "질문을 입력하세요.");
        input.focus();
        return;
      }
      if (typeof window.fetch !== "function") {
        fail("REQUEST_FAILED", "이 브라우저에서는 질문을 보낼 수 없습니다.");
        return;
      }
      busy = true;
      submit.disabled = true;
      submit.setAttribute("aria-busy", "true");
      const started = Date.now();
      const status = text("p", "소스를 읽고 답을 만드는 중입니다… 0초");
      show("running", (b) => b.append(status));
      const timer = window.setInterval(() => {
        status.textContent = `소스를 읽고 답을 만드는 중입니다… ${Math.round((Date.now() - started) / 1000)}초`;
      }, 1000);
      try {
        const body = new window.URLSearchParams();
        body.append("csrf_token", csrf);
        body.append("project", project);
        body.append("question", question);
        const response = await window.fetch(url, {
          method: "POST", credentials: "same-origin",
          headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded" },
          body,
        });
        let data = null;
        try { data = await response.json(); } catch { /* 원문 응답은 표시하지 않는다. */ }
        if (!response.ok || data?.ok !== true) {
          const error = data?.error;
          const valid = typeof error?.code === "string" && typeof error?.message === "string";
          fail(valid ? error.code : "REQUEST_FAILED",
            valid ? error.message : "질문을 처리하지 못했습니다. 다시 시도해 주세요.");
          return;
        }
        show("", (b) => {
          const meta = document.createElement("p");
          meta.className = "note";
          meta.append("기준 커밋 ", text("code", String(data.commit)),
            ` · ${data.branch} 브랜치 · 파일 ${data.files}개 참고${data.truncated ? "(크기 상한으로 일부만)" : ""}`);
          b.append(meta, text("pre", String(data.answer)));
          if (Array.isArray(data.sources) && data.sources.length) {
            const sources = text("p", "근거 파일: ");
            data.sources.forEach((path, index) => {
              if (index) sources.append(", ");
              sources.append(text("code", String(path)));
            });
            b.append(sources);
          }
        });
      } catch {
        fail("REQUEST_FAILED", "요청을 보내지 못했습니다. 연결 상태를 확인하고 다시 시도해 주세요.");
      } finally {
        window.clearInterval(timer);
        busy = false;
        submit.disabled = false;
        submit.removeAttribute("aria-busy");
      }
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
  });
}());
