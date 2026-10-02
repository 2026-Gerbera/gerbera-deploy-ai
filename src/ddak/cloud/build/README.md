# cloud/build (담당: 안승환, 옛 src/ddak/ci)
- 할 일: CodeBuild 멀티 아키텍처 빌드·push(build_image). push_image는 build_image에 합쳐 등록하지 않는다. 툴은 tools/<이름>/tool.py, 저장소 어댑터는 registries/(dockerhub 기본, ecr 옵션)
- 입출력 계약: `src/ddak/core/contracts`
- 다른 디렉토리 안쪽 파일을 직접 import하지 말고 __init__.py의 공개 함수만 쓴다.
- AI 호출은 core/ai(call_ai)로만, 허용된 디렉토리에서만 한다(여기는 금지).
