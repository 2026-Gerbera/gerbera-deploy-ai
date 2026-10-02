# 사람이 적용하는 C1 기반 Terraform

state 버킷과 앱·파이프라인(CodeBuild) 권한 경계 2개만 만든다. AI 생성 번들에 넣지 않는다.
배포자(deployer) 역할, 사용자 신뢰 정책·MFA·역할 인수 권한은 관리자가 별도로 준비한다.
권한 경계는 권한 부여 정책이 아니며 앱/빌드 역할에는 별도 최소 권한 정책이 필요하다.

## 수동 적용 순서

1. 계정·리전과 기존 버킷/동명 경계 존재 여부를 사람이 확인한다. 기존 SDK `apply_foundation`
   경로로 이미 만든 경우 중복 생성하지 말고 사람이 import·plan을 검토한다.
2. 이 디렉터리를 Git 밖 권한 0700 작업 디렉터리에 복사한다. 초기 local state·백업·plan도
   Git/AI 입력에 넣지 않는다. 팀의 영속 비공개 보관 위치를 먼저 정한다.
3. 관리자 본인의 AWS 세션으로 다음을 실행한다. 아래 값은 각자 확인해 입력한다.
   자격증명은 tfvars에 넣지 않는다. 이 문서 작성 중에는 아래 init/plan/apply를 실행하지 않았다.

```bash
terraform init
terraform fmt -check
terraform validate
terraform plan -out=foundation.tfplan \
  -var='account_id=YOUR_12_DIGIT_ACCOUNT' \
  -var='project=flaskr' \
  -var='state_bucket=YOUR_GLOBALLY_UNIQUE_STATE_BUCKET'
# 사람이 계정·생성 대상·정책을 확인한 뒤에만:
terraform apply foundation.tfplan
```

4. `state_bucket`, `app_boundary_arn`, `build_boundary_arn` 출력만 조립 설정으로 전달한다.
   플랫폼/앱 state는 이 버킷의 구분된 key를 사용한다. 기반 state와 혼합하지 않는다.
5. 버킷과 경계에는 `prevent_destroy`가 있다. 코드가 없어진 경우까지 보호하는 장치는 아니므로
   state/코드 보관과 변경 검토를 함께 유지한다. 버킷 버전 관리·암호화·공개 차단·TLS 강제를 설정한다.

현재 정적 HCL·정책/보호장치 테스트와 `terraform fmt`만 통과했다. 실제 provider 초기화·AWS
적용 결과는 아니다. 생성 번들의 CodeBuild Git 소스 권한/리소스가 이 경계와 맞는지 C2/O2 취합 시
확인한다. S3 대체 소스의 읽기 권한은 현재 파이프라인 경계에 없으므로 별도 검토가 필요하다.
