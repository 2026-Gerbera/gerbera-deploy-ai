# 원본과 수정 범위

이 앱은 [Pallets Flask 3.1.2 공식 tutorial](https://github.com/pallets/flask/tree/3.1.2/examples/tutorial)의 flaskr를 기반으로 한다.
원본의 BSD-3-Clause 고지는 LICENSE.txt에 그대로 보존했다.

- flaskr/templates/**, flaskr/static/**: 3.1.2 원본 그대로.
- flaskr/auth.py, flaskr/blog.py: 원본을 가져온 뒤 SQLAlchemy Core로 DB 접근을 변경했다.
  로그인·등록·로그아웃·게시글 CRUD, 암호 해시, 작성자 검사는 원본 흐름을 유지한다.
- upstream/*.txt: 가져온 auth/blog/db/app factory/schema 원본(실행하지 않음).
- upstream/provenance.json: 공식 태그, 다운로드 아카이브 해시와 원본 파일 SHA-256.
- 새 앱 설정·DB 연결·마이그레이션·준비 확인·배포 파일은 이 배포용 추가 구현이다.

이 앱의 실행 Python 의존성은 pyproject.toml 및 uv.lock에 독립적으로 고정하며,
Docker용 requirements.lock은 해당 잠금 파일에서 생성한다.
images.lock.json은 harness/var/validation/onprem-public-images.json 승인 목록의 사본이다.
