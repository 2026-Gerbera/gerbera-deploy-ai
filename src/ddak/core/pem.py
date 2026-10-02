"""공개 인증서만 든 PEM 허용. 개인키·본문은 오류나 로그에 포함하지 않는다."""

from __future__ import annotations

import base64
import binascii
import json
import re
import ssl
from pathlib import Path

MAX_PEM_BYTES = 2 * 1024 * 1024
_BEGIN = "-----BEGIN CERTIFICATE-----"
_END = "-----END CERTIFICATE-----"


class UnsupportedPemError(ValueError):
    def __init__(self, path: Path | str) -> None:
        name = json.dumps(str(path), ensure_ascii=True)
        super().__init__(f"지원하지 않는 파일: {name}: 개인키/미지원 PEM")


def is_certificate_only_pem(data: bytes) -> bool:
    """2MiB 이하 UTF-8, 인증서 블록과 공백/#/; 주석 줄만 허용한다."""
    if not data or len(data) > MAX_PEM_BYTES:
        return False
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
        return False
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    body: list[str] | None = None
    count = 0
    for raw in text.splitlines():
        line = raw.strip()
        if body is None:
            if line == _BEGIN:
                body = []
            elif not line or line.startswith(("#", ";")):
                if "-----BEGIN " in line or "-----END " in line:
                    return False
            else:
                return False
        elif line == _END:
            if not body:
                return False
            try:
                der = base64.b64decode("".join(body), validate=True)
                if len(der) < 2 or der[0] != 0x30:
                    return False
                octets = der[1] & 0x7F if der[1] & 0x80 else 0
                if der[1] == 0x80 or octets > 4 or len(der) < 2 + octets:
                    return False
                size = int.from_bytes(der[2 : 2 + octets], "big") if octets else der[1]
                if 2 + octets + size != len(der):
                    return False  # 인증서 DER 뒤에 다른 내용을 덧붙일 수 없다.
                # Base64를 CERTIFICATE로 감싼 개인키/CSR도 X.509 파싱에서 거부한다.
                context.load_verify_locations(cadata="\n".join([_BEGIN, *body, _END]))
            except (ssl.SSLError, ValueError, binascii.Error):
                return False
            count += 1
            body = None
        elif line and re.fullmatch(r"[A-Za-z0-9+/=]+", line):
            body.append(line)
        elif line:
            return False
    return body is None and count > 0


def require_certificate_pem(data: bytes, path: Path | str) -> bytes:
    if not is_certificate_only_pem(data):
        raise UnsupportedPemError(path)
    return data


def read_source_bytes(path: Path, relative: Path) -> bytes:
    """PEM은 제한된 크기만 읽고 검사한 동일 bytes를 manifest에 사용한다."""
    if path.suffix.lower() == ".key":
        raise UnsupportedPemError(relative)
    if path.suffix.lower() != ".pem":
        return path.read_bytes()
    if path.stat().st_size > MAX_PEM_BYTES:
        raise UnsupportedPemError(relative)
    with path.open("rb") as stream:
        data = stream.read(MAX_PEM_BYTES + 1)
    return require_certificate_pem(data, relative)
