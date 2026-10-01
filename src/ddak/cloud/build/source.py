"""빌드 소스 업로드(승인 뒤 수정본 → zip → S3 버전 ID). 담당 C2. AI 없음.

- 소스는 실행기가 승인 뒤 만든 빌드 사본(ctx.build_source)이다. 승인 전에는 올리지 않는다
  (decisions/2026-09-30-o1-start-contracts).
- zip에 넣는 파일 목록은 core.snapshots.file_manifest와 같다(제외 목록·심볼릭 링크 거부 포함).
  승인 해시를 계산한 파일과 CodeBuild가 받는 파일이 어긋나지 않게 한다.
  zip 생성 규칙은 O2와 맞출 구현 계약이다(💭).
- 같은 내용이면 같은 zip이 나온다: 이름순, 고정 시각, 실행 비트만 남긴다.
- S3 응답의 VersionId로 소스를 고정한다. 버킷 버전 관리가 꺼져 있으면 실패한다.
- 버킷 이름은 환경 정보(C1 Terraform 출력)에서 받는다. 키 이름 규칙은 💭.
- 오류 메시지에 버킷 ARN·계정 ID·응답 원문을 넣지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
import zipfile
from pathlib import Path
from typing import Any, Protocol

from ddak.cloud.build.codebuild import BuildSource
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.snapshots import digest_bytes, file_manifest

_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_KEY_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)  # zip 형식의 최소 시각. 파일 수정 시각은 넣지 않는다
_MODE_EXEC = 0o100755
_MODE_FILE = 0o100644
_UNIX = 3  # ZipInfo.create_system: 실행 비트를 external_attr로 읽게 한다


class S3Client(Protocol):
    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...


def source_key(project: str, run_id: str) -> str:
    """S3 객체 키(💭 규칙): `sources/<project>/<run_id>.zip`."""
    for value in (project, run_id):
        if not _KEY_PART.fullmatch(value):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "소스 키에 쓸 수 없는 이름이다")
    return f"sources/{project}/{run_id}.zip"


def zip_source(root: Path) -> bytes:
    """빌드 사본을 결정적 zip으로 묶는다. 읽는 중 파일이 바뀌면 실패한다."""
    try:
        manifest = file_manifest(root)
    except ValueError as exc:
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, f"빌드 사본을 읽지 못했다: {exc}"
        ) from exc
    if not manifest:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "빌드 사본에 파일이 없다")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in sorted(manifest):
            entry = manifest[name]
            data = (root / name).read_bytes()
            if digest_bytes(data) != entry["sha256"]:
                raise DdakToolError(
                    ErrorCode.PRECONDITION_FAILED, "zip을 만드는 중 빌드 사본이 바뀌었다"
                )
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.create_system = _UNIX
            info.external_attr = (_MODE_EXEC if entry["executable"] else _MODE_FILE) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buffer.getvalue()


def upload_source(client: S3Client, bucket: str, key: str, root: Path) -> BuildSource:
    """빌드 사본 zip을 S3에 올리고 버전 ID로 고정한 위치를 돌려준다."""
    if not _BUCKET.fullmatch(bucket):
        raise DdakToolError(ErrorCode.CONFIG_INVALID, "S3 버킷 이름 형식이 아니다")
    body = zip_source(root)
    checksum = base64.b64encode(hashlib.sha256(body).digest()).decode()
    try:
        response = client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/zip",
            ChecksumSHA256=checksum,
        )
    except Exception as exc:  # botocore ClientError 등. 원문에는 ARN·계정 ID가 섞인다
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "빌드 소스를 S3에 올리지 못했다") from exc
    version_id = response.get("VersionId")
    # 버전 관리가 없으면 VersionId가 없고, 일시 중지면 "null"이다. 둘 다 소스를 고정하지 못한다
    if not version_id or version_id == "null":
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, "S3 소스 버킷의 버전 관리가 켜져 있지 않다"
        )
    return BuildSource(bucket=bucket, key=key, version_id=str(version_id))


__all__ = ["S3Client", "source_key", "upload_source", "zip_source"]
