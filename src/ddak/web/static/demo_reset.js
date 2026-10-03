/* 운영 화면에서만 실행. 배포·PR 생성은 POST 버튼에서만 요청한다. */
(function () {
  if (typeof document === "undefined" || typeof fetch === "undefined") return;
  var box = document.getElementById("demo-state");
  if (!box) return;
  const fallbackWording = {
    "demo.ready": "v3 시연 PR 준비",
    "demo.missing": "v3 태그 없음",
    "demo.failed": "v3 태그 확인 실패",
    "demo.status_failed": "⚠ 시연 상태 확인 실패. 프로젝트 연결 설정을 확인하세요."
};
  let appWording = {};
  try { appWording = JSON.parse(document.getElementById("app-wording")?.textContent || "{}"); } catch { /* KO 기본값 유지 */ }
  const t = (key) => typeof appWording?.[key] === "string"
    ? appWording[key] : fallbackWording[key] ?? key;
  async function refresh() {
    try {
      var response = await fetch(box.dataset.url, {credentials: "same-origin"});
      if (!response.ok) throw new Error("status");
      box.innerHTML = await response.text();
      var button = document.getElementById("demo-prepare-v3");
      var tag = box.querySelector("[data-demo-v3]");
      if (button && tag) {
        button.disabled = tag.dataset.demoV3 !== "ready";
        button.textContent = tag.dataset.demoV3 === "ready" ? t("demo.ready") :
          tag.dataset.demoV3 === "missing" ? t("demo.missing") : t("demo.failed");
      }
    } catch (_) {
      box.textContent = t("demo.status_failed");
    }
    if (typeof setTimeout === "function") setTimeout(refresh, 15000);
  }
  refresh();
}());
