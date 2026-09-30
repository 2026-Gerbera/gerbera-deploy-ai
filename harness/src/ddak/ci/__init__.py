"""ci: ③ 빌드. 담당 C2(안승환), local(온프렘 pre-pull, 결정 필요)은 O1(정준우). AI 없음.

툴 2개: build_image, push_image(💭).
- CodeBuild가 모든 서비스를 이미지로 빌드한다(✅ 장부 9). 플랫폼 소유 인라인 buildspec.
- 이미지 저장소 기본 = Docker Hub(✅ 9/30). ECR은 옵션 어댑터(registries/ecr.py).
  CodeBuild는 Secrets Manager의 Docker Hub 읽기·쓰기 토큰으로 로그인해 push한다(빌드 역할은 그
  시크릿 하나만 읽음). 배포는 항상 digest 고정 `docker.io/<조직>/<이미지>@sha256:...`.
- push_image(💭): CodeBuild 경로에서는 push가 빌드 안에서 일어나므로 저장소 API로 digest를 확인·
  기록한다(별도 CodeBuild 호출 없음). 별도 툴로 둘지 build_image에 합칠지는 결정 필요.
- AWS 호출은 툴 호출마다 새 boto3 Session(자격증명 파일 교체 반영). 읽기 전용 검증 프로필은
  DDAK_AWS_READONLY_PROFILE(값이 없으면 ddak-readonly로 고정).
"""
