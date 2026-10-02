# C1 기반 Terraform 참고본

이 디렉터리의 HCL은 제품이 직접 실행하지 않는 참고본이다. 실제 제품은 `foundation.py`의 SDK 코드로 생성한다.
state bucket과 앱·CodeBuild 권한 경계 2개는 코드 소유 foundation이다. AI 생성 번들에 넣지 않는다.
권한 경계는 권한 부여 정책이 아니며 앱/빌드 역할에는 별도 최소 권한 정책이 필요하다.
사람이 foundation/platform을 미리 apply하는 전제는 폐기한다. 사람이 할 사전 준비는 AWS 자격증명과 도메인 구매다.
AI 개발 에이전트는 Terraform을 직접 실행하지 않는다. 제품이 검증·승인을 거쳐 코드로 실행하는 경로와 구분한다.

## 제품 실행 순서

1. 제품 코드가 계정·리전과 기존 bucket/동명 경계 존재 여부를 확인하고 인프라를 validate → plan한다.
   foundation(bucket + app/build 권한 경계)과 platform 변경·IAM 내용을 한 화면의 infra 승인에 포함한다.
   승인 대상과 해시가 바뀌면 다시 plan·승인한다. 기존 SDK foundation 경로와 중복 생성하지 않는다.
2. 사용자 승인 뒤 제품 코드가 foundation과 platform apply를 수행한다. bucket이 없으면 local backend로
   작성한 plan을 승인한 뒤 SDK로 bucket을 생성하고 platform local apply를 실행한다.
   그 뒤 remote backend로 state를 이전한다. SDK가 만든 bucket을 HCL에서 중복 생성하지 않도록
   소유권·state 연결을 확인해야 한다. O1 조립은 fixture 생성기로 검증했다. 실제 생성기 툴 구현은 O2 몫이며 AWS 실환경은 미검증이다.
3. `state_bucket`, `app_boundary_arn`, `build_boundary_arn` 등 허용 출력만 조립 설정에 전달한다.
   foundation/platform/app state는 구분된 key를 사용하며 혼합하지 않는다.
4. runtime·local state·백업·plan은 Git 밖의 영속 비공개 위치에서 권한을 제한해 보관한다.
   자격증명을 tfvars에 넣지 않으며 state·plan 원문을 Git이나 AI 입력에 넣지 않는다.
5. bucket과 경계의 `prevent_destroy`는 코드가 사라진 경우까지 보호하지 않는다. 코드/state 보관과
   변경 검토를 함께 유지한다. bucket 버전 관리·암호화·공개 차단·TLS 강제를 설정한다.

## 검증 범위

기존 정적 HCL·정책/보호장치 테스트와 `terraform fmt` 통과는 과거 검증 기록이다.
이 문서 수정에서는 Terraform/AWS를 실행하지 않았다. 실제 provider 초기화·AWS 생성·remote backend
state 이전·전체 AWS 완료를 입증하지 않는다. 생성 번들의 CodeBuild Git 소스 권한/리소스가 경계와
맞는지 C2/O2 취합 때 확인해야 한다. S3 대체 소스 읽기 권한은 현재 build 경계에 없어 별도 검토가 필요하다.

local apply 실패는 영속 bootstrap-recovery 표식과 local_state_path를 남기고 새 run도 막는다. 원격 이전 성공 때만 표식을 지운다. HCL과 SDK를 중복 적용하지 않는다.
