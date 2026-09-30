"""onprem/deploy/config.py: 설정 주입(.env upsert, SECRET_KEY 생성·재사용) 자리. 담당 정준우(O1).

지금 구현은 provider.py(OnPremProvider.inject_config, _private_env)에 있다. 파일이 커지면
여기로 옮긴다. 다른 디렉토리는 ddak.onprem.deploy의 공개 이름만 쓴다.
"""
