"""승인에 묶인 두 권한 경계의 관측·차이·기본 버전 생성.

승인은 부모 foundation이 검사한다. 부모는 account 공용 경계의 전체 작업을
동일 프로세스 RLock으로 묶어야 한다. IAM에는 원자적 CAS가 없으므로 재조회와
쓰기 사이의 외부 프로세스 변경은 막을 수 없다. 버전 삭제는 수행하지 않는다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from botocore.exceptions import ClientError

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.redact import redact

from .providers.aws import BOUNDARY_NAME, BOUNDARY_PATH, boundary_document, build_boundary_document

if TYPE_CHECKING:
    from .runtime import AwsSettings

_VERSION = re.compile(r"v[1-9][0-9]*(\.[A-Za-z0-9-]*)?")
_ACCOUNT = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")
_SNAPSHOT_FIELDS = {"policy_arn", "default_version_id", "document_sha256", "document"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DdakToolError(ErrorCode.PRECONDITION_FAILED, message)


def _version(value: Any) -> bool:
    return isinstance(value, str) and _VERSION.fullmatch(value) is not None


def _specs(settings: AwsSettings) -> list[tuple[str, str, dict[str, Any]]]:
    account = settings.account_id
    _require(
        isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account) is not None,
        "권한 경계 대상 계정 형식이 잘못되었다",
    )
    arns = [
        f"arn:aws:iam::{account}:policy{BOUNDARY_PATH}{name}"
        for name in (BOUNDARY_NAME, "ddak-build-boundary")
    ]
    _require(
        [settings.boundary_arn, settings.build_boundary_arn] == arns,
        "허용된 두 권한 경계 ARN과 설정이 다르다",
    )
    return [
        (BOUNDARY_NAME, arns[0], boundary_document(account, settings.project)),
        ("ddak-build-boundary", arns[1], build_boundary_document(account)),
    ]


def _document(value: Any) -> dict[str, Any]:
    from .runtime import canonical

    try:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = json.loads(unquote(value))
        _require(isinstance(value, dict), "권한 경계 Document가 JSON 객체가 아니다")
        # 응답/호출자 객체와 분리하고 runtime과 동일한 정규화를 적용한다.
        value = json.loads(canonical(value))
    except (ValueError, TypeError, RecursionError):
        raise DdakToolError(
            ErrorCode.PRECONDITION_FAILED, "권한 경계 Document 정규화에 실패했다"
        ) from None
    _statements(value)
    return value


def _hash(document: dict[str, Any]) -> str:
    from .runtime import canonical, digest

    return digest(canonical(document))


def _response_arn(response: dict[str, Any], arn: str) -> None:
    for field in ("Arn", "PolicyArn"):
        if field in response:
            _require(response[field] == arn, "권한 경계 응답 ARN이 요청과 다르다")


def _read(iam: Any, arn: str) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "policy_arn": arn,
        "default_version_id": None,
        "document_sha256": None,
        "document": None,
    }
    try:
        response = iam.get_policy(PolicyArn=arn)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
            return snapshot
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "권한 경계 정책 조회 실패") from None
    except Exception:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "권한 경계 정책 조회 실패") from None
    _require(isinstance(response, dict), "권한 경계 정책 응답이 누락되었다")
    _response_arn(response, arn)
    policy = response.get("Policy")
    _require(isinstance(policy, dict), "권한 경계 Policy 응답이 누락되었다")
    _response_arn(policy, arn)
    _require(policy.get("Arn") == arn, "권한 경계 정책 ARN이 누락되거나 다르다")
    for field, expected in (("Path", BOUNDARY_PATH), ("PolicyName", arn.rsplit("/", 1)[1])):
        if field in policy:
            _require(policy[field] == expected, "권한 경계 정책 이름 또는 경로가 다르다")
    version = policy.get("DefaultVersionId")
    _require(_version(version), "권한 경계 기본 버전 ID가 누락되거나 잘못되었다")
    try:
        response = iam.get_policy_version(PolicyArn=arn, VersionId=version)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "권한 경계 기본 버전이 조회 중 사라졌다"
            ) from None
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "권한 경계 기본 버전 조회 실패") from None
    except Exception:
        raise DdakToolError(ErrorCode.ADAPTER_FAILED, "권한 경계 기본 버전 조회 실패") from None
    _require(isinstance(response, dict), "권한 경계 버전 응답이 누락되었다")
    _response_arn(response, arn)
    current = response.get("PolicyVersion")
    _require(isinstance(current, dict), "권한 경계 PolicyVersion 응답이 누락되었다")
    _response_arn(current, arn)
    _require(
        current.get("VersionId") == version and current.get("IsDefaultVersion") is True,
        "권한 경계 기본 버전 응답 ID 또는 기본 버전 표시가 다르다",
    )
    document = _document(current.get("Document"))
    snapshot.update(default_version_id=version, document_sha256=_hash(document), document=document)
    return snapshot


def snapshot_boundaries(settings: AwsSettings, iam: Any) -> list[dict[str, Any]]:
    """정확한 app/build 경계만 조회한다. 정책 부재와 버전 부재를 구별한다."""
    specs = _specs(settings)  # 두 ARN 모두 검증한 뒤 첫 API를 호출한다.
    return [_read(iam, arn) for _, arn, _ in specs]


def _snapshots(
    specs: list[tuple[str, str, dict[str, Any]]], snapshots: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    _require(
        isinstance(snapshots, (list, tuple)) and len(snapshots) == 2,
        "권한 경계 스냅샷 두 개가 필요하다",
    )
    allowed = {arn for _, arn, _ in specs}
    indexed: dict[str, dict[str, Any]] = {}
    for item in snapshots:
        _require(
            isinstance(item, dict) and set(item) == _SNAPSHOT_FIELDS,
            "권한 경계 스냅샷 필드가 누락되거나 잘못되었다",
        )
        arn = item["policy_arn"]
        _require(
            isinstance(arn, str) and arn in allowed and arn not in indexed,
            "권한 경계 스냅샷 ARN이 허용 목록과 다르거나 중복되었다",
        )
        version, sha, document = (
            item["default_version_id"],
            item["document_sha256"],
            item["document"],
        )
        if version is None:
            _require(sha is None and document is None, "부재 스냅샷의 문서 또는 해시가 남아 있다")
        else:
            _require(
                _version(version) and isinstance(document, dict),
                "권한 경계 스냅샷 버전 또는 문서가 잘못되었다",
            )
            document = _document(document)
            _require(sha == _hash(document), "권한 경계 스냅샷 문서 해시가 다르다")
        indexed[arn] = {
            "policy_arn": arn,
            "default_version_id": version,
            "document_sha256": sha,
            "document": document,
        }
    return [indexed[arn] for _, arn, _ in specs]


def _masked(value: Any) -> Any:
    if isinstance(value, str):
        from .plan import _masked as mask_policy_value

        masked = _ACCOUNT.sub("*" * 12, mask_policy_value(value))
        # 기존 정책뷰와 같은 RDS/비밀값 처리를 쓰되 전체 Statement를 자르지 않는다.
        if re.fullmatch(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\*{12}:[\w/*?.:@+=-]+", masked):
            return masked
        return redact(masked, max_len=max(4096, len(masked) * 16))
    if isinstance(value, int) and not isinstance(value, bool) and _ACCOUNT.fullmatch(str(value)):
        return "*" * 12
    if isinstance(value, dict):
        return {_masked(key): _masked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_masked(item) for item in value]
    return value


def _statements(document: dict[str, Any] | None) -> dict[bytes, dict[str, Any]]:
    from .runtime import canonical

    if document is None:
        return {}
    statements = document.get("Statement")
    if isinstance(statements, dict):
        statements = [statements]
    _require(
        isinstance(statements, list) and all(isinstance(s, dict) for s in statements),
        "권한 경계 Statement가 누락되거나 잘못되었다",
    )
    return {canonical(statement): statement for statement in statements}


def _resources(statements: dict[bytes, dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for statement in statements.values():
        for field in ("Resource", "NotResource"):
            if field not in statement:
                continue
            values = statement[field]
            if isinstance(values, str):
                values = [values]
            _require(
                isinstance(values, list) and all(isinstance(value, str) for value in values),
                "권한 경계 Resource 형식이 잘못되었다",
            )
            result.update(values)
    return result


def boundary_changes(
    settings: AwsSettings, snapshots: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """승인표에는 완전한 Statement의 추가/제거만 넣고 계정을 재귀 마스킹한다."""
    specs = _specs(settings)
    expected = _snapshots(specs, snapshots)
    changes = []
    for (_, arn, document), previous in zip(specs, expected, strict=True):
        template_hash = _hash(document)
        before, after = _statements(previous["document"]), _statements(document)
        before_resources, after_resources = _resources(before), _resources(after)
        action = (
            "create"
            if previous["document"] is None
            else ("unchanged" if previous["document_sha256"] == template_hash else "update")
        )
        changes.append(
            {
                "policy_arn": _masked(arn),
                "action": action,
                "previous_version_id": previous["default_version_id"],
                "previous_document_sha256": previous["document_sha256"],
                "template_sha256": template_hash,
                "added_statements": [_masked(after[key]) for key in sorted(after.keys() - before)],
                "removed_statements": [
                    _masked(before[key]) for key in sorted(before.keys() - after)
                ],
                "added_resources": [
                    _masked(value) for value in sorted(after_resources - before_resources)
                ],
                "removed_resources": [
                    _masked(value) for value in sorted(before_resources - after_resources)
                ],
            }
        )
    return changes


def _version_capacity(iam: Any, arn: str, default: str) -> None:
    request = {"PolicyArn": arn}
    markers: set[str] = set()
    ids: set[str] = set()
    defaults: set[str] = set()
    while True:
        try:
            response = iam.list_policy_versions(**request)
        except Exception:
            raise DdakToolError(ErrorCode.ADAPTER_FAILED, "권한 경계 버전 목록 조회 실패") from None
        _require(isinstance(response, dict), "권한 경계 버전 목록 응답이 누락되었다")
        _response_arn(response, arn)
        entries = response.get("Versions")
        _require(isinstance(entries, list) and bool(entries), "권한 경계 버전 목록이 불완전하다")
        for entry in entries:
            _require(isinstance(entry, dict), "권한 경계 버전 목록 항목이 잘못되었다")
            _response_arn(entry, arn)
            version = entry.get("VersionId")
            flag = entry.get("IsDefaultVersion")
            _require(
                _version(version) and type(flag) is bool and version not in ids,
                "권한 경계 버전 목록 ID/기본 표시가 누락되거나 중복되었다",
            )
            ids.add(version)
            if flag:
                defaults.add(version)
        truncated = response.get("IsTruncated")
        _require(type(truncated) is bool, "권한 경계 버전 목록 페이지 표시가 누락되었다")
        marker = response.get("Marker")
        if not truncated:
            _require(marker is None, "권한 경계 마지막 페이지에 잘못된 marker가 있다")
            break
        _require(
            isinstance(marker, str) and bool(marker) and marker not in markers,
            "권한 경계 버전 목록 marker가 누락되거나 중복되었다",
        )
        markers.add(marker)
        request["Marker"] = marker
    _require(defaults == {default}, "권한 경계 버전 목록의 기본 버전이 스냅샷과 다르다")
    _require(len(ids) < 5, "권한 경계 버전이 5개 이상이다; 자동 삭제 없이 수동 정리가 필요하다")


def _check_one(iam: Any, previous: dict[str, Any], document: dict[str, Any]) -> bool:
    current = _read(iam, previous["policy_arn"])
    # 템플릿과 같아졌더라도 승인 당시 상태와 달라졌으면 먼저 거절한다.
    _require(current == previous, "승인 이후 권한 경계 기본 버전 또는 문서가 바뀌었다")
    unchanged = current["document_sha256"] == _hash(document)
    if current["document"] is not None and not unchanged:
        _version_capacity(iam, previous["policy_arn"], current["default_version_id"])
    return unchanged


def check_boundaries(settings: AwsSettings, iam: Any, snapshots: Sequence[dict[str, Any]]) -> None:
    """부모는 foundation의 모든 쓰기 전에 두 경계 전체를 검사한다."""
    specs = _specs(settings)
    expected = _snapshots(specs, snapshots)
    for (_, _, document), previous in zip(specs, expected, strict=True):
        _check_one(iam, previous, document)


def _record(
    record: Callable[[dict[str, Any]], None], row: dict[str, Any], *, attempted: bool
) -> None:
    try:
        record(dict(row))
    except Exception:
        raise DdakToolError(
            ErrorCode.ADAPTER_FAILED,
            "권한 경계 변경 결과 영속 기록 실패; 상태 확인이 필요하다",
            needs_human=attempted,
        ) from None


def apply_boundaries(
    settings: AwsSettings,
    iam: Any,
    snapshots: Sequence[dict[str, Any]],
    *,
    guard: Callable[[], None],
    record: Callable[[dict[str, Any]], None],
) -> list[dict[str, Any]]:
    """각 쓰기 직전 재검사한다. unknown 선기록은 부모의 영속 upsert를 요구한다."""
    from .runtime import canonical

    specs = _specs(settings)
    expected = _snapshots(specs, snapshots)
    result = []
    try:
        for (name, arn, document), previous in zip(specs, expected, strict=True):
            unchanged = _check_one(iam, previous, document)
            row = {
                "policy_arn": _masked(arn),
                "previous_version_id": previous["default_version_id"],
                "new_version_id": previous["default_version_id"] if unchanged else None,
                "status": "unchanged" if unchanged else "unknown",
            }
            if unchanged:
                _record(record, row, attempted=False)
                result.append(row)
                continue
            create = previous["document"] is None
            request: dict[str, Any] = {"PolicyDocument": canonical(document).decode()}
            if create:
                request.update(
                    PolicyName=name,
                    Path=BOUNDARY_PATH,
                    Tags=[
                        {"Key": "ManagedBy", "Value": "ddak"},
                        {"Key": "Project", "Value": settings.project},
                    ],
                )
            else:
                request.update(PolicyArn=arn, SetAsDefault=True)
            guard()
            _record(record, row, attempted=False)
            try:
                response = (
                    iam.create_policy(**request) if create else iam.create_policy_version(**request)
                )
                _require(isinstance(response, dict), "권한 경계 생성 응답이 누락되었다")
                _response_arn(response, arn)
                value = response.get("Policy" if create else "PolicyVersion")
                _require(isinstance(value, dict), "권한 경계 생성 결과 필드가 누락되었다")
                _response_arn(value, arn)
                if create:
                    _require(value.get("Arn") == arn, "생성된 권한 경계 ARN이 누락되거나 다르다")
                    for field, target in (("PolicyName", name), ("Path", BOUNDARY_PATH)):
                        if field in value:
                            _require(
                                value[field] == target, "생성된 권한 경계 이름 또는 경로가 다르다"
                            )
                    version = value.get("DefaultVersionId")
                else:
                    _require(
                        value.get("IsDefaultVersion") is True,
                        "새 권한 경계 버전의 기본 버전 표시가 누락되거나 다르다",
                    )
                    version = value.get("VersionId")
                _require(
                    _version(version) and version != previous["default_version_id"],
                    "새 권한 경계 버전 ID가 누락되거나 잘못되었다",
                )
            except Exception:
                raise DdakToolError(
                    ErrorCode.ADAPTER_FAILED,
                    "권한 경계 변경 API 또는 응답 검증 실패; "
                    "unknown 기록의 실제 상태 확인이 필요하다",
                    needs_human=True,
                ) from None
            row.update(new_version_id=version, status="created" if create else "updated")
            _record(record, row, attempted=True)
            result.append(row)
    except DdakToolError as exc:
        if any(row["status"] in ("created", "updated") for row in result):
            exc.needs_human = True
        raise
    except Exception:
        raise DdakToolError(
            ErrorCode.ADAPTER_FAILED,
            "권한 경계 적용 중단; 기록된 대상 상태 확인이 필요하다",
            needs_human=any(row["status"] in ("created", "updated") for row in result),
        ) from None
    return result
