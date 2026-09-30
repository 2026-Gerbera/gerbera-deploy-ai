# infra/terraform — AI Terraform의 코드 소유 틀·정책 검사·권한 경계

> 기준: 앱 1개 구조(✅ 9/30 최신 개발자 문서 기준). ✅ 장부 21~24(2026-09-30): Terraform도 AI가 짠다.
> 범위는 플랫폼까지 전부(VPC·서브넷·ALB·ECS 클러스터·공유 RDS·CodeBuild·IAM·시크릿·상태 버킷 등).
> 9/30: 이미지 저장소 기본은 Docker Hub라 ECR은 옵션 어댑터일 때만 만든다. state는 플랫폼 층 / 앱 층으로 나눈다.
> IAM은 AI가 설계하고 사람이 검증·승인한 뒤 생성한다. 앱 역할·DB 계정은 최소 권한. 테스트 때 권한은 넓게.
> 아래 배치·파일 이름·도구는 전부 💭 고려안이다. 설계 문서: single-app/docs/01 §5-5·5-8~5-10, 06 §8.

- 담당: 유상준(C1, 짝 안승환 C2). 리전은 서울(`ap-northeast-2`)만 씁니다.
- **이 디렉토리에 리소스 HCL을 사람이 쓰지 않습니다.** 리소스는 `generate_infra`(AI)가 만들고,
  이 디렉토리에는 AI가 건드릴 수 없는 코드 소유 틀과 검사 규칙만 둡니다(💭).

## 배치 (💭)

| 경로 | 내용 | 누가 |
|---|---|---|
| `skeleton/versions.tf` | `terraform { required_version = ">= 1.11, < 2.0" }`, AWS provider `~> 6.66`. `.terraform.lock.hcl` 커밋, `init -lockfile=readonly` | C1(사람, 고정) |
| `skeleton/providers.tf` | `region = "ap-northeast-2"`, `allowed_account_ids`, `default_tags`(Project·Owner·Env·ManagedBy) | C1(사람, 고정) |
| `skeleton/backend.hcl.tpl` | S3 백엔드 partial config(`use_lockfile = true`). backend 블록은 변수를 못 쓰므로 코드가 `-backend-config`로 주입 | 코드 |
| `skeleton/variables.tf` | 이름 규칙·계정 ID·리전 같은 입력. AI 파일은 하드코딩 대신 이 변수를 씀. 💭 이름 규칙: 앱 시크릿 `ddak/<앱>/<KEY>`(예 `ddak/flaskr/SECRET_KEY`), 앱 역할 경로 `/ddak/app/` + 이름 `ddak-<앱>-<용도>`, 플랫폼 시크릿 `ddak-platform/*`(Docker Hub 토큰) | C1(사람, 고정) |
| `layers/platform/`, `layers/app/`(💭) | state 두 개. 플랫폼 층(VPC·ALB·ECS 클러스터·공유 RDS·CodeBuild·플랫폼 역할·Docker Hub 토큰 시크릿 컨테이너) / 앱 층(앱 시크릿 컨테이너, 앱 실행·태스크 역할, ECS 서비스·대상 그룹 등). 개선 배포의 인프라 변경(예: v2 새 시크릿)은 앱 층 plan만 돌아 짧음. 앱 층 입력은 코드가 플랫폼 층 `terraform output -json`을 읽어 변수로 넘김(`terraform_remote_state` 안 씀: state 원문을 읽게 됨) | C1 |
| `policy/` | 리소스 타입 허용 목록, init 전 정적 게이트 규칙(금지 블록: `provisioner`, `data "external"`/`"http"`, 로컬 아닌 `module source`, AI 파일 안 `terraform`/`backend`/`provider` 블록, `aws_iam_user`/`aws_iam_access_key`, 상태에 비밀값을 남기는 `aws_secretsmanager_secret_version`·`aws_db_instance.password` / 금지 함수: `file*`·`templatefile`·`fileset`·`pathexpand`·`abspath` / IAM: `/ddak/app/` 역할 `permissions_boundary` 필수, 신뢰 주체 허용 목록, `/ddak/app/` 관리형 정책은 `/ddak/app/` 역할에만, `rds!` 참조는 DB 초기화 역할만, 정책 안 `.arn` 참조 금지), Checkov `--check` 허용 목록. 커스텀 YAML(3306 공개 금지, 필수 태그, 경로·경계)은 P1 | C1 |
| `foundation/` | 기반 준비 고정 템플릿(💭): 상태 버킷 설정, 권한 경계 정책 JSON(`ddak-app-boundary`, P1: `ddak-pipeline-boundary`), P1: 파이프라인 역할. 부트스트랩 run **전**에 `apply_infra(stage=foundation)`이 [기반 승인] 뒤 boto3로 만듦(Terraform 상태 밖). 이후 수정 금지(Deny). 장부 22·23과의 관계는 사용자 확인 필요(N27) | C1 |
| (생성물) `var/infra/<입력 해시>/main.tf` | AI가 쓴 리소스. gitignore(`var/`). 입력 해시가 같으면 재사용, 다르면 재생성하고 HCL diff·plan diff 표시 | `generate_infra` |

- AI 생성물은 **팀 저장소에 AI가 커밋하지 않습니다**(AI 컨트리뷰터 차단, ✅ 장부 19). 리뷰용으로 남기려면 사람이 자기 이름으로 커밋합니다.
- 💭 위 틀(버전 고정·provider·backend·이름 규칙·권한 경계·상태 버킷 템플릿)은 앱마다 사람이 쓰는 Terraform이 아니라 **제품 코드**이므로 "딸깍" 취지에 맞는다는 것이 우리 권고입니다(설계 문서 00 장부 34, 사용자 확인 필요). 코드 쪽 자리는 `src/ddak/infra/providers/aws.py`입니다.

## 흐름 (💭)

(계정당 한 번) 기반 준비 → `discover_existing`(읽기 전용, P1) → `generate_infra`(AI, HCL 초안) → `validate_infra`(init 전 정적 게이트 → `init -lockfile=readonly`(P1 provider 미러) → `validate -json` → Checkov) → `plan_infra`(`plan -out`, P0 테스트 프로필 / P1 읽기 세션 → `show -json` 요약 + IAM Access Analyzer) → **사람 승인**(plan 파일 sha256에 묶음, 승인 POST는 CSRF 검사) → `apply_infra`(`apply <run>.tfplan`, stale이면 다시 plan·승인) → 출력(`terraform output -json` → 환경 정보의 Terraform 출력 부분, 💭 파일 `platform.cloud.json`).

- 자동 apply는 없습니다. 수정 루프는 최대 3회, 매 회차 보안 검사 전체를 다시 돌립니다. 3회 안에 실패하면 사람에게 넘깁니다.
- AI가 만든 HCL은 `init`·`plan`만 해도 코드를 실행할 수 있습니다(`data "external"`은 plan 중 실행, init은 provider를 내려받음, `file()`은 로컬 파일을 읽음). 그래서 정적 게이트가 init보다 먼저이고, `init -lockfile=readonly`로 잠금 파일 밖 provider를 거부합니다(P1: `TF_CLI_CONFIG_FILE` 미러). terraform 자식 프로세스는 `HOME`을 빈 임시 디렉토리로, AWS 자격은 단기 키 env만(`AWS_PROFILE`·`TF_LOG` 없음) 받습니다.
- plan 파일에는 민감값이 평문으로 들어갑니다. `var/infra/` 아래 권한 제한 디렉토리에 두고 apply 뒤 지웁니다(`*.tfplan`은 gitignore).
- 443 리스너·SslPolicy(`ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09` 명시)·HSTS·80→301·DNS는 계속 `ensure_tls`가 만듭니다. 도메인은 AI Terraform 입력에 없습니다(✅ 장부 17).
- **Terraform state는 `terraform output -json`으로만 읽습니다.** state 원문(JSON)을 파싱하지 않고 LLM에 넣지 않습니다(비밀값이 평문일 수 있음). 출력은 코드 소유 이름 허용 목록만 받고, `sensitive` 출력은 환경 정보에 쓰지 않습니다(`output -json`은 sensitive 값도 평문으로 내보냄).
- 시크릿 **값**은 Terraform에 두지 않습니다. Terraform은 빈 시크릿 컨테이너와 실행 역할 읽기 권한만 만들고, 값은 파이프라인 코드가 난수로 만들어 `PutSecretValue`합니다(✅ 9/30 데모 시크릿 장면). 대안 write-only `secret_string_wo`는 Terraform·provider 버전 확인 뒤(💭).
- 상태는 S3 백엔드(Terraform 1.11+, `use_lockfile = true`, 버킷 버저닝, DynamoDB 잠금 없음). 첫 상태 버킷은 부트스트랩 run 전 기반 준비에서 **코드(boto3, AI 없음)**가 고정 템플릿으로 만듭니다(💭, N27. 장부 22는 상태 버킷도 AI 범위로 적었으므로 사용자 확인 필요). 로컬 tfstate는 커밋하지도 읽지도 않습니다(RDS 비밀번호 등 평문).

## 권한 (💭, 자세히 research/2026-09-30_IAM-최소권한-설계.md 13절)

- 테스트 때는 넓은 권한으로 돌립니다(✅ 장부 24). 데모 전에는 좁은 파이프라인 역할 + 권한 경계로 바꾸기를 권합니다.
- 부트스트랩 모드(기반 준비·플랫폼 첫 생성): 사람이 승인 시점에만 관리자 자격을 줍니다. 개선 배포: 좁은 파이프라인 역할(P1). 이 역할은 `role/ddak/app/*` 경로에 `ddak-app-boundary`를 붙일 때만 역할을 만들 수 있고, 경계 정책은 고칠 수 없습니다. 신뢰 주체·관리형 정책 부착 대상처럼 경계로 못 막는 것은 정적 게이트와 사람 승인이 막습니다.
- 개발 중 코딩 에이전트는 여전히 `terraform apply/destroy`를 하지 않습니다(`.claude` 훅·deny). 제품의 `apply_infra`는 사람 승인 뒤 코드가 실행하는 별개 경로입니다.
- 이미지 저장소 기본이 Docker Hub라 CodeBuild·배포 역할·앱 경계에서 ECR 권한을 뺐고, Docker Hub 토큰 시크릿 읽기(CodeBuild = push 토큰, 실행 역할 = pull 토큰)만 넣습니다(research/IAM 14절). Fargate가 Docker Hub에서 pull하려면 인터넷이 필요합니다(💭 퍼블릭 서브넷 + 퍼블릭 IP, NAT 비용 회피).
- 테스트는 로컬 우선, 개발 중 클라우드 사용 최소화(비용은 추후 주최측 환급, ✅ 장부 19). 상시 과금 리소스(ALB, RDS, (쓰면) NAT GW·인터페이스 엔드포인트) 목록과 10/4 이후 정리 일정을 여기에 적습니다. AWS Budgets 알림: ₩300,000의 50%, 80%.
