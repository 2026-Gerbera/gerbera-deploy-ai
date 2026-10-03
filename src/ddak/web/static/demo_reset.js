/* 운영 화면에서만 실행. 배포·PR 생성은 POST 버튼에서만 요청한다. */
(function () {
  if (typeof document === "undefined" || typeof fetch === "undefined") return;
  var box = document.getElementById("demo-state");
  if (!box) return;
  async function refresh() {
    try {
      var response = await fetch(box.dataset.url, {credentials: "same-origin"});
      if (!response.ok) throw new Error("status");
      box.innerHTML = await response.text();
    } catch (_) {
      box.textContent = "⚠ 시연 상태 확인 실패. 프로젝트 연결 설정을 확인하세요.";
    }
    if (typeof setTimeout === "function") setTimeout(refresh, 15000);
  }
  refresh();
}());
