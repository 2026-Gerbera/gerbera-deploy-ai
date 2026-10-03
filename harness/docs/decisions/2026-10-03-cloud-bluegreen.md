# 2026-10-03 클라우드 ECS 블루그린 결정

- 결정자: 사용자(정준우, O1)
- 상태: 확정
- 기준: 2026-10-02 팀 현황과 결정의 결정 4 및 2026-10-03 클라우드 방향 검토를 이 기록으로 갱신한다.

## 결정

1. **클라우드는 처음부터 ECS 네이티브 블루그린으로 배포한다.** ECS deployment controller와 `BLUE_GREEN` 전략을 사용하고 ALB를 유지한다. 테스트 리스너와 lifecycle hook은 두지 않는다. 트래픽은 100% 한 번에 전환한다. CodeDeploy는 사용하지 않는다.

2. **배포 완료는 운영 트래픽 전환 완료로 판정한다.** `ServiceDeployment.lifecycleStage`가 `BAKE_TIME` 또는 `CLEAN_UP`에 들어가거나 상태가 `SUCCESSFUL`이면 성공이다. Bake는 1분이다. Bake 중 실패하면 `StopServiceDeployment(ROLLBACK)`로 즉시 원복한다. Bake와 blue 정리는 시연 시간 측정에서 제외한다.

3. **시연 v2에는 스키마 변경과 시크릿 키 추가가 없다.** 새 마이그레이션도 없다. 클라우드 3분 목표는 이 제약을 전제로 한 목표이며, 아직 측정·달성된 성능으로 간주하지 않는다.

4. **시연 최종 배포의 시간 기준은 3분 이내다.** 시계는 앱 저장소 PR merge 감지부터 온프렘·클라우드 두 트랙의 health와 smoke 통과까지다. 승인 화면 클릭 한 번을 포함한다. 실제 3분 달성 여부는 별도 리허설로 측정해야 한다.

5. **빌드 분리는 이번 결정·구현 범위가 아니다.** 현재 합쳐진 빌드 단계를 온프렘 local과 클라우드 CodeBuild로 나누는 일은 별도 설계·작업으로 진행한다. 온프렘은 local, 클라우드는 CodeBuild를 사용한다. 감시 주기와 배포 대상 설정은 정하지 않는다.

## 인프라와 공유 계약

- 블루그린 전용 인프라 출력 키는 추가하지 않는다. 실행 시 `DescribeServices`의 `advancedConfiguration`에서 필요한 값을 읽는다.
- ECS 인프라 역할은 foundation 코드가 멱등하게 생성한다. IAM path는 `/ddak/infra/`, 이름은 `ddak-ecs-infra-elb`, 신뢰 주체는 `ecs.amazonaws.com`이며 AWS 관리형 `AmazonECSInfrastructureRolePolicyForLoadBalancers`를 연결한다. 이 역할과 설정을 foundation 승인 해시 및 승인 화면에 포함한다. AI HCL은 `var.account_id` 기반 고정 역할 ARN만 참조한다. AI 게이트의 역할 신뢰 주체·경로·관리형 정책 금지 조건은 바꾸지 않는다.
- 플랫폼 층도 `app_secret_arn_<KEY>` 형식의 시크릿 ARN 출력을 허용한다. 기존 `IMAGE_REPOSITORY_PATTERN` 검사와 `checked_cloud_outputs`의 재검증 경로는 유지한다.
- `generate_infra` 툴 제한 시간은 600초, `prepare_db`는 300초다. AI 전체 호출 제한은 20초로 유지하며, `generate_infra`에만 툴 내부 확장 설정을 둔다. 타임아웃 변경은 툴 실행 상한이며 시연 시간 보장이 아니다.
- Anthropic SDK 의존성은 추가하지 않는다. 수정 11의 표준 라이브러리 provider 경로로 통일한다.

## 대회 뒤 최소 권한 자동 공급

- 시연은 현행 자격증명을 사용한다. 테스트 계정 사용자가 AdministratorAccess를 가진다는 사용자 확인을 전제로 하며, 이 작업에서 실제 계정 권한을 조회하지 않았다. 제품이 caller에 정책을 붙이는 코드는 추가하지 않는다.
- 아래 표는 이번 블루그린 변경에 필요한 권한이다. 기존 S3 기반 버킷·Terraform·앱 배포 권한 전체를 대체하는 정책은 아니다. 대회 뒤 caller 주체와 리소스 범위를 확정해 최소 권한을 코드·승인 경로로 자동 공급한다. 수동 콘솔·CLI 단계를 전제로 하지 않는다.

| caller | 필요한 권한 | 리소스·조건 |
|---|---|---|
| foundation | iam:CreateRole, iam:GetRole, iam:AttachRolePolicy, iam:ListAttachedRolePolicies, iam:ListRolePolicies | /ddak/infra/ddak-ecs-infra-elb 역할. 연결할 관리형 정책은 AmazonECSInfrastructureRolePolicyForLoadBalancers로 제한 |
| apply | iam:PassRole | 같은 고정 역할, iam:PassedToService=ecs.amazonaws.com |
| deploy | ecs:ListServiceDeployments, ecs:DescribeServiceDeployments, ecs:StopServiceDeployment | 대상 ECS 서비스·배포 리소스 범위는 자동 공급 구현 시 확정 |
| ddak-readonly | ecs:DescribeServices, elasticloadbalancing:DescribeRules, elasticloadbalancing:DescribeTargetHealth | 대상 서비스·리스너 규칙·target group의 조회 |

현재 제품의 preflight_check는 카탈로그만 있고 구현이 없다. 로컬 빌드 사전검사와 하네스 운영 스크립트는 제품 cloud preflight가 아니다. 요청에 따라 새 사전검사를 구현하지 않으며, 인계서에 구현 위치만 제안한다.

## 신규 RDS bootstrap dbinit 예외

- bootstrap 준비가 완료된 platform plan(update=False)에서만 `aws_iam_role.dbinit_execution` 주소와 `ddak-<project>-dbinit-exec` 이름의 앱 역할에 `arn:aws:secretsmanager:<region>:<account_id>:secret:rds!db-*` 읽기를 허용한다. 현재 지원 리전은 ap-northeast-2다.
- 허용 액션은 secretsmanager:GetSecretValue·secretsmanager:DescribeSecret이며, 다른 역할·접두사·계정·리전·액션과 app/update 경로는 거부한다. 역할 ID가 미확정이면 Terraform의 단일 직접 참조로 역할 주소를 확인한다. 변경 없는 와일드카드 읽기 정책도 검사한다. 기존 정확 ARN의 허용 경로는 유지한다.
- 앱 권한 경계의 기존 GetSecretValue 범위는 보존하고, DescribeSecret만 위 접두사와 dbinit 역할 PrincipalArn 조건의 별도 문장으로 추가한다. 이 경계는 권한 자체를 부여하지 않으며, 실제 역할의 정책은 위 게이트를 통과해야 한다. 기존 경계가 템플릿과 다르면 자동 교체하지 않고 중단한다.
- 예외의 역할 주소·실행 모드·액션·리소스와 경계 조건을 foundation 템플릿에 넣어 foundation 해시와 결합 infra 승인 해시에 포함한다. 화면에는 `RDS bootstrap 범위 rds!db-* (ap-northeast-2, 계정 ************)`로 표시한다. 개별 실제 RDS 시크릿 ARN은 기존 해시 표시를 유지한다.
- **대회 뒤 정확한 ARN으로 좁힘.** bootstrap 제한은 정책의 승인·생성 경로 제한이며, 생성된 정책이 시간이 지나면 자동 만료된다는 뜻이 아니다. 생성 후 실제 ARN으로 축소하는 제품 경로는 후속 작업이다.
- error.json 직접 redact와 큰 교정 입력 처리는 main 스레드로 이관한다. 공유 .venv 연결은 추가 변경하지 않고 모든 검사는 UV_NO_SYNC=1과 cloud-int의 PYTHONPATH를 사용한다.

## C2·C3 후속 요청

- **C2 승환:** ECS 배포 완료 판정과 롤백을 블루그린 lifecycle에 맞추고, 이미지·환경변수·시크릿이 같을 때 불필요한 서비스 전환을 막는다. 이월 tier 관측값을 이전 release에서 구성하고 REAL 경로 회귀를 추가한다. DB 계정·앱 시크릿·web 환경변수 계약을 맞추고, plain/secret 주입을 provider 경로로 구현한다. DB 마이그레이션은 단일 태스크로 처리하고, 롤백은 `StopServiceDeployment(ROLLBACK)` 우선으로 중복 원복을 막는다. 폴링 간격 제안은 확정 설정이 아니다.
- **C3 서윤:** 생성 프롬프트를 ECS 블루그린, 두 target group, 운영 listener rule, TLS 정책, 앱 DB 시크릿, 최소 egress 계약과 맞춘다. Health는 운영 target group과 새 리비전 태스크를 기준으로 이월 artifact까지 검증하고 제한적으로 재시도한다. 생성기 캐시·교정 피드백을 안전하게 보완하고, O1·O2 공유 파일 변경은 각 담당 통합으로 넘긴다. 이 결정은 health 구현 완료를 뜻하지 않으며 `cloud/health/tls.py`는 서윤 소유로 유지한다.
- **C2/C3 공통 경계:** 빌드 단계 분리는 별도 작업이며, 결정된 backend는 온프렘 local·클라우드 CodeBuild다. 감시 주기와 실행 대상을 정한 것으로 기록하지 않는다. 관리 웹 디자인은 별도 작업이다.

## 10월 2일 기록 정정

2026-10-02 팀 현황과 결정 표의 결정 4에 있는 “온프렘·클라우드 모두 롤링 교체” 및 “클라우드는 ECS Fargate 롤링” 문구는 이 결정으로 대체한다. 블루그린을 1차 이후 검토 후보로 둔다는 관련 반대 의견과 되돌리는 조건도 더 이상 현재 배포 방식을 설명하지 않는다.
