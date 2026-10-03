# DB 관리 소유권 승계 원장 설계 — 2026-10-03

**정준우 결정:** DB 컨테이너를 재생성하거나 Docker 라벨을 바꾸지 않고, 제품의 별도 승계 원장을 설계한다. 이 문서는 설계이며 수정11의 실행 코드에는 아직 구현하지 않는다. 현재 DB 소유권 변경 요청은 계속 거부한다.

## 승계의 의미

승계하는 것은 **제품이 해당 DB 자원을 관리할 권한**이다. DB 계정 권한·데이터 소유권·Docker 라벨은 바꾸지 않는다. 기존 앱 연결을 끊거나 DB를 restart/recreate하지 않는다. WEB/WAS의 컨테이너 교체 방식과 별개다.

원장에 active claim이 있으면 그 claim의 프로젝트가 관리 주체다. Docker 라벨의 프로젝트는 최초 소유자 증거로 보존한다. 새 프로젝트를 허용하면서 이전 프로젝트도 계속 허용하는 예외 목록을 만들지 않는다.

## 관측과 승인

읽기 관측으로 다음을 모으고 전체를 canonical JSON 해시에 묶는다. 값·비밀번호·DB 데이터/볼륨 내용은 읽거나 기록하지 않는다.

- 관리 endpoint 식별과 SSH host key fingerprint(또는 로컬 Docker endpoint), Docker daemon ID
- DB container ID, 실제 image digest, 현재 owner/tier/managed 라벨
- `/var/lib/mysql`의 **named volume** 이름·생성 시각·driver/options의 해시와 mount 속성
- 기존 원장의 claim ID·revision·현재 관리 프로젝트. 이전 기록이 없으면 그 사실을 경고한다.
- 출발 프로젝트, 목적 프로젝트, 인벤토리 해시, 변경 목적, 제안 만료 시각

승인 화면에는 현재 라벨 소유자, 승계 후 관리 프로젝트, DB/볼륨 유지, 이전 프로젝트의 향후 DB 관리 차단을 명시한다. 사람이 이 해시를 승인한다. 테이블 이동/DB·계정 생성은 **다른 변경 목적**이며 승계 승인만으로 허용하지 않는다.

## 원장 모델 초안

`db_resource_claims`: resource_key(PK), revision, original_owner, owner_project, observation_hash, observed_resource(비밀 없는 위 식별자), state(ACTIVE/INVALID), approval_id, claimed_at.

`db_claim_audit`: 이전/다음 revision과 owner, 승인자·시각·사유, 해시, 승인 ID, 결과. append-only로 남긴다. 기존 release/current/previous 및 Docker 라벨을 수정하지 않는다.

resource_key는 endpoint 문자열 하나로 만들지 않는다. 동일 호스트 별칭 때문에 중복 승계가 생기지 않도록 **daemon ID + volume 관측 식별**을 기준으로 하고, container ID와 호스트 지문은 별도의 승인된 관측으로 고정한다.

## 승인 뒤 코드 실행

1. 컨트롤러 lease를 확인하고 자원 잠금 + 출발/목적 프로젝트 잠금을 정해진 순서로 획득한다. 어느 프로젝트든 DB 변경/배포가 실행 중이면 거부한다.
2. 새 관측을 읽어 승인 해시의 모든 식별자·인벤토리·원장 revision과 비교한다. 컨테이너 교체·볼륨 변경·호스트 키 변경·만료는 재계획 사유다.
3. SQLite 단일 트랜잭션에서 revision을 compare-and-swap하여 owner를 목적 프로젝트로 바꾸고 감사 기록을 함께 남긴다. 실패하면 이전 claim을 유지한다. 외부 DB/Docker 변경 명령은 실행하지 않는다.
4. 잠금을 해제한다. 목적 프로젝트는 이후 새 run에서 DB 재사용·마이그레이션 계획을 만들 수 있다. 이전 프로젝트의 과거 release는 읽기 전용으로 남으며 관리 요청은 거부된다.

같은 승인 ID 재전송은 동일 결과 조회만 허용하고 또 승계하지 않는다. 이전 AWAITING run도 start 시 최신 claim/revision을 재검사하므로, 승계 전에 승인한 옛 계획으로 DB를 관리할 수 없다.

## 모든 DB 실행 경로의 판정

`deploy_database`의 기존 자원 보존, DB 준비 manager, DB를 건드리는 마이그레이션 사전 점검·reset·운영 도구에서 **하나의 core 판정 결과**를 소비한다. 범용 `DockerHost.owned`를 느슨하게 바꾸지 않는다.

- claim이 없으면 기존 Docker owner 라벨 규칙을 따른다.
- ACTIVE claim이 있으면 실제 관측이 claim과 일치하고 호출 프로젝트가 owner_project인 경우에만 DB 관리 허용. 호출 프로젝트가 옛 Docker 라벨과 일치해도 claim을 우회할 수 없다.
- claim이 있되 INVALID/불일치이면 **라벨 규칙으로 fallback하지 않는다**. NEEDS_HUMAN과 재관측/재승인이 필요하다.
- 이 계약으로 새 프로젝트의 DB 관리는 허용되지만, DB 재생성·볼륨 삭제 금지는 계속 유지한다.

원장 참조와 승인된 revision은 RunContext에 선택 필드로 연결하는 안이다. 구현 시 core 계약·스키마·카탈로그 영향 확인 및 contracts-update가 필요하다. 기존 ApprovalKind를 임의로 재해석하지 않고, 준비 승인 모델에 `operation=db_ownership_succession` 목적과 자원 해시를 명시한다.

## 복구와 제한

- 제품 잠금 해제는 이 원장을 삭제/변경하지 않는다. DB에 대해 자동 재승계하지 않는다.
- 반대 방향 승계도 새로운 관측·승인·revision으로 한다. 감사 기록 삭제나 이전 승인 재사용은 금지한다.
- SQLite를 잃거나 다른 PC에 복사한 경우 기존 라벨만 믿어 자동 복구하지 않는다. 원장 백업·감사 일치 확인과 사람의 재승인이 필요하다.
- **한 컨트롤러가 이 VM 자원을 관리한다는 전제**다. 서로 다른 SQLite를 가진 두 컨트롤러의 동시 관리를 이 설계만으로 막을 수 없다. 다중 컨트롤러는 공유 lease/원장 또는 호스트 측 잠금이 별도로 필요하다. 이 전제를 배포 런북에 명시한다.

## 구현 시 필수 회귀

- 컨테이너 ID·라벨·image·볼륨·데이터가 그대로인 채 관리 owner만 변경된다.
- 새 프로젝트 허용, 이전 프로젝트 및 이전 승인 run 거부, Docker 라벨 fallback 우회 거부.
- 같은 DB의 동시 승계/동시 배포는 한 건만 획득한다. 승인 후 관측/revision 변경은 적용 전 중단한다.
- DB 계정/테이블 작업이 승계 승인만으로 호출되지 않는다. 일반 unlock은 claim을 유지한다.
- 트랜잭션 실패/재시작/승인 재전송에서 이중 소유자가 생기지 않는다. 감사·UI에 비밀값이 없다.

구현 순서는 원장·잠금 → 읽기 관측/승인 데이터 → 실행 경로의 공통 판정 → 관리 UI이다. 수정11의 DB 거부 경로는 이 회귀가 통과한 뒤에만 바꾼다.
