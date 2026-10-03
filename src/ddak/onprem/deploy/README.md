# onprem/deploy (담당: 정준우)
- 할 일: 온프렘 Docker 배포·롤백·설정 주입·마이그레이션. containers.py(옛 cd/docker_host.py), provider.py(옛 cd/providers/onprem.py), `__init__.py`가 OnPremProvider(CD 인터페이스 구현) 노출
- 입출력 계약: `src/ddak/core/contracts`, CD 인터페이스는 `src/ddak/cd/interface.py`(ProviderResult)
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 금지: import-linter 계약이 막는다).

## 수정11 등록·준비 API (main 연결용)

아래 이름은 모두 `from ddak.onprem.deploy import ...`로 가져온다.

```python
write_inventory(root: Path, project: str, data: dict[str, Any]) -> Path
read_inventory(path: Path) -> InventoryConfig
register_onprem_inventory(inventory_json: str, destination: Path) -> Path

OnPremPreparationManager(root: Path, ctx: RunContext, *,
    runner: Runner = subprocess_runner, stdin_runner: StdinRunner = ...)
manager.plan_database(*, database: str, backup_database: str,
    accounts: tuple[DatabaseAccount, ...]) -> DatabasePreparationPlan
manager.apply_database(plan, *, approved_sha: str,
    approval_check: ApprovalCheck) -> DatabasePreparationResult
manager.plan_ownership(tier: str, *, replica: int = 1) -> OwnerTransferPlan
manager.apply_ownership(plan, *, approved_sha: str,
    approval_check: ApprovalCheck, replica: int = 1) -> ContainerObservation
```

- 인벤토리는 `root/settings/<project>/inventory.json`에 0600 atomic replace로 저장한다.
  JSON 전체를 기존 InventoryConfig로 검증하며 입력 객체는 수정하지 않는다.
  WAS env 경로가 없으면 `root/private/<project>/runtime.env`, migration 경로가 없으면
  별도 `root/private/<project>/migration.env`를 넣는다. 기존 명시 경로는 보존한다.
  nginx ready 설정에서 빠진 값은 `8080`, `/nginx-health`로 채운다.
  SSH는 기존 `ssh.host/user/key_path/host_key_fingerprint` 형식을 유지한다.
  등록 시 키·env 내용 접근이나 VM 접속은 없다. read 결과는 `model_dump(mode="json")`으로
  API 응답에 쓸 수 있다. UI에서 받은 임의 파일 경로를 read에 넘기지 말고 위 제품 경로를 만든다.
- manager는 현재 요청의 project/mode/platform을 가진 RunContext로 만든다. main은 모델이나
  승인 계약 변경 없이 manager 메서드를 setup UI callback으로 주입한다.
  `ApprovalCheck = Callable[[str, str], bool]`: `(approval_sha, summary)`를 기존 승인 저장소에서
  검증하고 실제 사람 승인이 확인됐을 때만 정확히 `True`를 반환한다. plan의 summary와
  approval_sha를 한 쌍으로 표시·저장한다. HTTP 입력의 `approved=True`를 신뢰하지 않는다.
- B6는 BOOTSTRAP 전용이다. 기존 owned MySQL 컨테이너/영구볼륨/이미지를 보존하며
  `docker exec mysql`로 DB 전체 테이블·% host 계정·schema 권한을 새로 관측한다.
  이전 앱 소유를 추측하지 않고 기존 전체 테이블 목록을 승인 요약과 SHA에 묶는다.
  backup DB가 없을 때 생성 후 단일 RENAME TABLE로 이동한다. 기존 backup DB, view,
  trigger, FK 또는 InnoDB 외 테이블은 자동 이전하지 않는다. DROP은 실행하지 않는다.
  신규 DB/계정은 생성하고 앱은 DML, migrator는 CREATE/ALTER/INDEX/REFERENCES를 추가한다.
  `DatabaseAccount(name, role="app"|"migrator", password_env_ref="ENV_NAME")`를 공급한다.
  계정 이름은 name@'%'로 고정한다. 기존 계정 비밀번호는 변경하지 않는다.
  신규 계정 비밀번호는 승인 후 execution에서만 해당 환경변수를 읽어 stdin으로 전달한다.
  DB 관리자 인증은 컨테이너의 MYSQL_ROOT_PASSWORD를 사용한다. 값은 argv/로그/결과에 넣지 않는다.
- B7는 각 WEB/WAS replica를 별도로 승인한다. 기존 named volume은 인벤토리와 정확히 같고
  local driver여야 한다. 기존 컨테이너 이미지 ID·명령·healthcheck·설정·라벨·volume을 관측하고,
  소유 라벨이 바뀐 새 컨테이너를 만든 뒤 설정 동등성을 확인한다. 원본을 중지·백업 이름으로
  보존하고 새 컨테이너에 기존 이름을 준다. named volume 생성/삭제, 원본 컨테이너 삭제는 없다.
  임의 bind/privileged/다중 network/대화형 실행 등 보존 못 하는 설정은 중단한다.
  env 값은 inspect하지 않는다. 기존 실행에 필요한 값이 들어 있는 승인된 env_file과
  검증된 public_env를 공급해야 한다. 새로운 env가 원래 컨테이너 값과 같은지는 증명하지 않는다.
  running/설정 동등성은 확인하지만 애플리케이션 readiness 검증은 기존 preflight/health에 연결한다.
- 다른 소유자의 DB는 라벨이 immutable이고 DB 재생성은 금지이므로 `NEEDS_CONTEXT`이다.
  준비 오류 코드는 기존 PRECONDITION_FAILED를 사용한다. controller alias/가짜 claim으로
  정상화하지 않는다. 없는 DB 컨테이너를 이 API가 새로 만들거나 고아 볼륨에 연결하지 않는다.
- 프로젝트별 로컬 preparation.lock과 작업 전후 전체 관측으로 중복 실행·변경을 차단한다.
  다른 컨트롤러/외부 DBA까지 막는 MySQL 전역 잠금은 아니다. 부분 실행/전송 실패 후에는
  `DdakToolError.needs_human=True`다. main은 성공 기록·자동 재시도 대신 수동 확인 상태를 기록한다.
  자동 보상 DROP, volume 삭제, 승인 취소/소비 저장은 이 helper가 하지 않는다.
- 아직 main의 setup UI/승인 callback 연결과 실제 VM 검증이 남아 있다. fake 테스트 성공은
  실제 MySQL·Docker 실행 성공이나 제품 전체 B6/B7 완료를 의미하지 않는다.
