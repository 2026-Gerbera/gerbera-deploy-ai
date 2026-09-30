"""`python -m ddak`: 앱 1개(관리 웹 + 실행기 + 레지스트리)를 띄운다.

주소: 127.0.0.1:${DDAK_ADMIN_PORT:-8765}
"""

from ddak.app import main

if __name__ == "__main__":
    main()
