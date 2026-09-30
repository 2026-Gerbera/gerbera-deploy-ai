# gerbera-on-premise

SoftBank 해커톤 배포 파이프라인 프로젝트입니다.

- [harness/](harness/README.md): 최신 하네스 코드·개발환경·규칙·테스트
- [single-app/dev-docs/](single-app/dev-docs/README.md): 최신 개발자 문서
- [single-app/docs/](single-app/docs/): 설계 문서
- [docs/](docs/README.md), [research/](research/): 기획·조사 기록

```sh
cd harness
make setup-local
make ci
make run-fake
```

GitHub Actions는 루트 `.github/workflows/ci.yml`에서 `harness/`를 검사합니다. main push와 PR 생성·수정 때 실행되며, 실제 배포는 하지 않습니다. 구형 MCP 하네스는 삭제했습니다.
