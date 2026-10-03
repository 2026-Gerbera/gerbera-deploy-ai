"""제품 Docker 설정의 Hub 인증 항목 판정. 빈 자동 helper 방지 항목은 인증이 아니다."""

from collections.abc import Mapping


def docker_hub_auth(metadata: Mapping) -> Mapping:
    auths = metadata.get("auths", {})
    if isinstance(auths, Mapping):
        for host in (
            "https://index.docker.io/v1/",
            "docker.io",
            "index.docker.io",
            "registry-1.docker.io",
        ):
            entry = auths.get(host)
            if isinstance(entry, Mapping) and any(
                isinstance(entry.get(key), str) and entry[key].strip()
                for key in ("auth", "identitytoken")
            ):
                return entry
    return {}
