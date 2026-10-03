"""관리자 초기 설정. AI/배포 구현은 app이 콜백으로 연결한다."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ddak.core.config import AdapterMode, Settings
from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.core.defaults import load_aws_defaults, load_defaults, project_values
from ddak.core.env_keys import check_runtime_keys
from ddak.core.private_values import SecretVault, private_directory, read_private, write_private
from ddak.core.project_settings import ProjectSettings

_CHOICE_FIELDS = {
    "generation_provider",
    "generation_model",
    "judgment_provider",
    "judgment_model",
    "llm_effort",
    "ai_timeout_s",
    "build_backend",
    "image_repository",
    "buildx_builder",
    "git_author_name",
    "git_author_email",
    "aws_profile",
}


class SetupService:
    def __init__(
        self,
        service: Any,
        *,
        catalog: Callable,
        effective: Callable,
        test_provider: Callable,
        validate_selection: Callable,
        inventory_writer: Callable,
        inventory_reader: Callable,
        build_factory: Callable,
        probes: Mapping[str, Callable] | None = None,
        provider_status: Callable | None = None,
        display_effective: Callable | None = None,
    ) -> None:
        self.service = service
        self.root = service.root / "setup"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.vault = SecretVault(service.root / "private")
        self.catalog, self.effective = catalog, effective
        self._display_effective = display_effective or effective
        self._test_provider, self._validate = test_provider, validate_selection
        self._write_inventory, self._read_inventory = inventory_writer, inventory_reader
        self._build_factory = build_factory
        self._probes = dict(probes or {})
        self._provider_status = provider_status
        self._lock = threading.RLock()

    def _project(self, project: str) -> str:
        project = self.service.resolve_project(project)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", project):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "프로젝트 이름 형식 오류")
        return project

    def _path(self, project: str) -> Path:
        path = self.root / self._project(project)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "설정 디렉토리 형식 오류")
        return path

    def _saved(self, project: str) -> dict[str, Any]:
        return self.service.get_project_settings(self._project(project)) or {}

    def _spec(self, ident: str) -> dict[str, Any]:
        return next((s for s in self.catalog() if s["id"] == ident), {})

    def _signature(self, project: str, ident: str) -> str:
        saved = self._saved(project)
        # 설정·키 교체 뒤 오래된 연결 확인을 재사용하지 않는다. 값은 해시에만 사용한다.
        private = {}
        for spec in self.catalog():
            key = spec.get("key_name")
            if key:
                value = self.vault.get(project, key)
                private[key] = hashlib.sha256((value or "").encode()).hexdigest()
        private["git_push_token"] = hashlib.sha256(
            (self.vault.get(project, "git_push_token") or "").encode()
        ).hexdigest()
        cfg = self.effective(project, saved, self.vault)
        effective_hash = hashlib.sha256(
            json.dumps(vars(cfg), sort_keys=True, default=str).encode()
        ).hexdigest()
        payload = {
            "settings": saved,
            "keys": private,
            "effective": effective_hash,
            "component": ident,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def _selected(self, project: str, saved: dict, *, cfg=None) -> dict:
        cfg = cfg or self.effective(project, saved, self.vault)
        return {
            "generation_provider": cfg.selected_provider("generation"),
            "judgment_provider": cfg.selected_provider("judgment"),
            "generation_model": cfg.llm_model,
            "judgment_model": cfg.judgment_model,
            "llm_effort": cfg.llm_effort,
            "ai_timeout_s": cfg.ai_timeout_s,
            "build_backend": cfg.build_backend,
            "image_repository": cfg.image_repository,
            "aws_profile": cfg.aws_profile or project_values(saved)["aws_profile"],
        }

    def _records(self, project: str) -> dict[str, Any]:
        path = self._path(project) / "checks.json"
        if not path.exists():
            return {}
        if path.is_symlink() or path.stat().st_size > 131072:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "연결 기록 파일 형식 오류")
        return json.loads(path.read_text())

    def _record(self, project: str, ident: str, result: Mapping[str, Any]) -> dict[str, Any]:
        status = result.get("status", "gray")
        status = {
            "ready": "green",
            "blocked": "red",
            "verified": "green",
            "configured": "gray",
        }.get(status, status)
        if status not in ("green", "gray", "red"):
            status = "red"
        # 콜백도 원문 오류를 반환하지 않는다. 최종 출력에서는 한 번 더 가린다.
        from ddak.core.redact import redact

        public = {
            "status": status,
            "detail": redact(str(result.get("detail", "")))[:240],
            "verified_at": time.time() if status == "green" else None,
        }
        if ident == "build":
            from ddak.core.redact import redact_obj

            public["checks"] = redact_obj(result.get("checks", []))
            public["installation"] = redact_obj(result.get("installation"))
        with self._lock:
            records = self._records(project)
            records[ident] = {**public, "signature": self._signature(project, ident)}
            self._write_json(self._path(project) / "checks.json", records)
        return public

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        fd, name = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _state(self, project: str, ident: str) -> dict[str, Any]:
        record = self._records(project).get(ident, {})
        try:
            signature = self._signature(project, ident)
        except DdakToolError as error:
            from ddak.core.redact import redact

            return {"status": "red", "detail": redact(str(error))[:240], "verified_at": None}
        if record.get("signature") == signature:
            return {k: v for k, v in record.items() if k != "signature"}
        return {"status": "gray", "detail": "연결 확인 필요", "verified_at": None}

    def view(self, project: str) -> dict[str, Any]:
        project = self._project(project)
        saved = self._saved(project)
        display_error = None
        try:
            cfg = self._display_effective(project, saved, self.vault)
        except (DdakToolError, ValueError):
            cfg = Settings()
            display_error = "설정 오류: 연결 설정을 수정하세요"
        settings = {
            **project_values(saved),
            **saved,
            **self._selected(project, saved, cfg=cfg),
        }
        defaults = load_defaults()
        sources = {
            key: "기본 파일" if key in defaults else "기본 설정"
            for key in ProjectSettings.model_fields
        }
        sources.update({key: "관리 페이지" for key, value in saved.items() if value is not None})
        sources.update(cfg.setting_sources)
        if cfg.aws_profile is None:
            sources["aws_profile"] = "관리 페이지" if saved.get("aws_profile") else "기본 파일"
        identity_error = None
        if cfg.adapter_mode is AdapterMode.REAL:
            from ddak.core.git_credentials import configured_identity

            repo_path = (
                self.service.root
                / "repositories"
                / project
                / hashlib.sha256((saved.get("repo_url") or "").encode()).hexdigest()
            )
            try:
                name, email = configured_identity(saved, repo_path)
                for key, value in (("git_author_name", name), ("git_author_email", email)):
                    settings[key] = value
                    sources[key] = "관리 페이지" if saved.get(key) is not None else "머신 git 신원"
            except DdakToolError:
                identity_error = "설정 필요: 앱 커밋 작성자 이름·이메일"
                for key in ("git_author_name", "git_author_email"):
                    if saved.get(key) is None:
                        sources[key] = "설정 필요"
        builder = self._builder(project)
        settings["buildx_builder"] = builder.builder_name
        sources["buildx_builder"] = "관리 페이지" if saved.get("buildx_builder") else "기본 파일"
        remember = getattr(getattr(self.service, "store", None), "remember_settings_view", None)
        view_token = None
        if remember is not None:
            view_token = remember(
                project,
                saved.get("version", 0),
                {key: settings[key] for key in _CHOICE_FIELDS},
            )
        providers = self.catalog()
        states = {p["id"]: self._state(project, p["id"]) for p in providers}
        keys = {
            p["id"]: bool(p.get("key_name") and self.vault.configured(project, p["key_name"]))
            for p in providers
        }
        inventory = None
        inventory_path = saved.get("inventory_path") or os.environ.get("DDAK_ONPREM_INVENTORY")
        if inventory_path:
            inventory = self._read_inventory(Path(inventory_path))
        selected = [settings.get("generation_provider"), settings.get("judgment_provider")]
        ai_states = [states[p] for p in selected if p in states]
        ai_status = (
            "red"
            if any(s["status"] == "red" for s in ai_states)
            else (
                "green" if ai_states and all(s["status"] == "green" for s in ai_states) else "gray"
            )
        )
        checklist = [
            {
                "id": "ai",
                "label": "AI 연결",
                "status": ai_status,
                "detail": "선택한 역할의 연결 테스트 결과",
            }
        ]
        for ident, label in (
            ("docker", "Docker Hub"),
            ("inventory", "온프레미스 환경"),
            ("build", "빌드 환경 준비"),
            ("repository", "앱 저장소 push 권한"),
        ):
            checklist.append({"id": ident, "label": label, **self._state(project, ident)})
        if identity_error:
            next(item for item in checklist if item["id"] == "repository").update(
                status="red", detail=identity_error
            )
        names_path = self._path(project) / "runtime-keys.json"
        names = json.loads(names_path.read_text()) if names_path.exists() else []
        return {
            "settings": settings,
            "settings_view": view_token,
            "setting_sources": sources,
            "providers": providers,
            "states": states,
            "keys": keys,
            "checklist": checklist,
            "inventory": inventory,
            "env_keys": names,
            "build_plan": builder.plan(),
            "build_state": self._state(project, "build"),
            "git_token_configured": self.vault.configured(project, "git_push_token"),
            "aws_expected_account_id": load_aws_defaults()["expected_account_id"],
            "configuration_notes": self._configuration_notes(project, saved, inventory)
            + [error for error in (identity_error, display_error) if error],
        }

    def save_choices(
        self,
        project: str,
        data: Mapping[str, Any],
        *,
        expected_version: int,
        view_token: str | None = None,
    ) -> dict:
        project = self._project(project)
        data = dict(data)
        if set(data) - _CHOICE_FIELDS:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "연결 설정 필드 오류")
        for role, field in (
            ("generation", "generation_provider"),
            ("judgment", "judgment_provider"),
        ):
            if data.get(field):
                self._validate(data[field], role)
        if data.get("buildx_builder") == self._build_factory(self._path(project)).builder_name:
            data["buildx_builder"] = None
        # service/Store가 3-way 최종 병합과 CLI 검사를 같은 트랜잭션에서 수행한다.
        return self.service.save_project_settings(
            project,
            dict(data),
            updated_by="local-operator",
            expected_version=expected_version,
            **({"view_token": view_token} if view_token else {}),
        )

    def _configuration_notes(self, project: str, saved: dict, inventory: dict | None) -> list[str]:
        notes = []
        for name in (
            "DDAK_BUILD_BACKEND",
            "DDAK_IMAGE_REPOSITORY",
            "DDAK_ONPREM_INVENTORY",
            "DDAK_WATCH_PROJECT",
            "DDAK_WATCH_TARGETS",
            "DDAK_WATCH_BRANCH",
        ):
            if os.environ.get(name):
                notes.append(name + " 실행환경 설정 있음; 관리 페이지 저장값이 우선합니다")
        if inventory:
            was = inventory.get("tiers", {}).get("was", {})
            path = self._path(project) / "runtime-keys.json"
            names = set(json.loads(path.read_text())) if path.exists() else set()
            duplicates = names & set(was.get("public_env", {}))
            if duplicates:
                notes.append(
                    "중복 키는 인벤토리 public_env가 우선: " + ", ".join(sorted(duplicates))
                )
            configured = was.get("env_file")
            expected = str(self.service.root / "private" / project / "runtime.env")
            if configured and configured != expected:
                notes.append("외부 env_file 사용 중: 웹의 실행 환경 값은 적용되지 않을 수 있습니다")
        return notes

    def save_git_token(self, project: str, value: str) -> None:
        from ddak.core.git_credentials import save_token

        project = self._project(project)
        save_token(self.vault.path, project, self._saved(project).get("repo_url") or "", value)
        self._record(
            project, "repository", {"status": "gray", "detail": "토큰 저장됨; 권한 검사 필요"}
        )

    def delete_git_token(self, project: str) -> None:
        self.vault.delete(self._project(project), "git_push_token")

    def save_key(self, project: str, provider_id: str, value: str) -> None:
        spec = self._spec(provider_id)
        if not spec.get("key_name"):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "키를 저장할 수 없는 provider")
        self.vault.put(self._project(project), spec["key_name"], value)

    def delete_key(self, project: str, provider_id: str) -> None:
        spec = self._spec(provider_id)
        if not spec.get("key_name"):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "키를 삭제할 수 없는 provider")
        self.vault.delete(self._project(project), spec["key_name"])

    def test_provider(self, project: str, provider_id: str) -> dict:
        spec = self._spec(provider_id)
        saved = self._saved(project)
        selected = self._selected(project, saved)
        roles = [
            role
            for role in ("generation", "judgment")
            if selected.get(role + "_provider") == provider_id
        ]
        roles = roles or list(spec.get("roles", ()))[:1]
        if not roles:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "미연결 provider")
        cfg = self.effective(project, saved, self.vault)
        try:
            results = [self._test_provider(provider_id, cfg, role=role) for role in roles]
            result = next((r for r in results if r.get("status") == "red"), None)
            if result is None:
                result = next((r for r in results if r.get("status") != "green"), results[0])
        except Exception:
            result = {"status": "red", "detail": "연결 검사 실패. 인증·모델·네트워크를 확인하세요"}
        return self._record(project, provider_id, result)

    def register_inventory(self, project: str, data: dict) -> str:
        saved = self._saved(project)
        path = self._write_inventory(self.service.root, self._project(project), data)
        self.service.save_project_settings(
            project,
            {"inventory_path": str(path)},
            updated_by="local-operator",
            expected_version=saved.get("version", 0),
        )
        self._record(
            project, "inventory", {"status": "gray", "detail": "등록됨. VM 연결 확인 필요"}
        )
        return str(path)

    def check_provider_status(self, project: str, provider_id: str) -> dict:
        """인증 상태 확인만 수행한다. AI 비용 호출은 연결 테스트에서만 한다."""
        spec = self._spec(provider_id)
        if not spec or self._provider_status is None:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "상태 확인 미지원")
        cfg = self.effective(project, self._saved(project), self.vault)
        try:
            result = self._provider_status(provider_id, cfg)
        except Exception:
            result = {"status": "red", "detail": "설치·로그인 상태 확인 실패"}
        return self._record(project, provider_id, result)

    def runtime_env(self, project: str) -> Path:
        path = self.service.root / "private" / self._project(project) / "runtime.env"
        with private_directory(self.vault.path, create=True):
            pass
        with private_directory(path.parent, create=True):
            pass
        if path.is_symlink():
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "환경 값 파일 형식 오류")
        return path

    def save_env(self, project: str, key: str, value: str) -> None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key) or key in {"RELEASE_ID", "SOURCE_SHA"}:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "제품 키 또는 잘못된 키 이름")
        check_runtime_keys([key])
        if not value or any(c in value for c in ("\n", "\r", "\x00")) or len(value) > 8192:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "환경 값 형식 오류")
        with self._lock:
            self.vault.put(project, "runtime_" + key, value)
            names_path = self._path(project) / "runtime-keys.json"
            names = set(json.loads(names_path.read_text())) if names_path.exists() else set()
            names.add(key)
            path = self.runtime_env(project)
            # 기존 생성 SECRET_KEY/등록값을 보존한다. 승인 데이터에는 이름만 나간다.
            with private_directory(path.parent) as folder:
                try:
                    raw = read_private(folder, path.name).decode()
                except FileNotFoundError:
                    raw = ""
                existing = dict(
                    line.split("=", 1)
                    for line in raw.splitlines()
                    if line and not line.startswith("#")
                )
                existing[key] = value
                write_private(
                    folder, path.name, "".join(f"{k}={v}\n" for k, v in existing.items()).encode()
                )
            self._write_json(names_path, sorted(names))

    def _builder(self, project: str):
        name = self._saved(project).get("buildx_builder")
        return self._build_factory(self._path(project), **({"builder_name": name} if name else {}))

    def save_migration_url(self, project: str, value: str) -> None:
        """기존 DB 등록용. migrator URL은 런타임 env와 완전히 분리한다."""
        if (
            not isinstance(value, str)
            or len(value) > 8192
            or any(c in value for c in ("\n", "\r", "\x00"))
        ):
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "마이그레이션 URL 형식 오류")
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"mysql", "mysql+pymysql"}
                or not all(
                    (parsed.hostname, parsed.username, parsed.password, parsed.path.strip("/"))
                )
                or parsed.fragment
            ):
                raise ValueError
            _ = parsed.port
        except ValueError:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "마이그레이션 URL 형식 오류") from None
        saved = self._saved(project)
        inventory_path = saved.get("inventory_path") or os.environ.get("DDAK_ONPREM_INVENTORY")
        inventory = self._read_inventory(Path(inventory_path)) if inventory_path else {}
        configured = inventory.get("tiers", {}).get("was", {}).get("migration_env_file")
        path = (
            Path(configured) if configured else self.runtime_env(project).with_name("migration.env")
        )
        if not path.is_absolute():
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "마이그레이션 파일 절대 경로 필요")
        with self._lock, private_directory(path.parent, create=True) as folder:
            write_private(
                folder,
                path.name,
                f"DATABASE_URL_MIGRATOR={value}\nDATABASE_URL={value}\n".encode(),
            )
        self.vault.put(project, "database_migrator_url", value)

    def build_context(self, project: str) -> dict:
        builder = self._builder(project)
        return {
            "docker_config": str(builder.docker_config),
            "builder": builder.builder_name,
            "tool_dir": str(builder.tool_dir),
        }

    def build_plan(self, project: str) -> dict:
        return self._builder(project).plan()

    def apply_build(self, project: str, plan_hash: str) -> dict:
        builder = self._builder(project)
        builder.approve(plan_hash)
        try:
            result = builder.apply(plan_hash)
        except DdakToolError as error:
            detail = (
                "Gitleaks 공식 릴리스 SHA-256 검증 실패"
                if error.message == "Gitleaks 공식 릴리스 SHA-256 검증 실패"
                else "필요 도구 설치 실패; 다시 검사하세요"
            )
            self._record(project, "build", {"status": "red", "detail": detail})
            raise
        self._record(project, "build", result)
        return result

    def login_docker(self, project: str, username: str, token: str) -> dict:
        try:
            result = self._builder(project).login(username, token)
            if result.get("status") != "green":
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, "설정 필요: Docker Hub 로그인")
            self._write_json(self._path(project) / "docker-user.json", {"username": username})
            result = self.probe(project, "docker")
            if result["status"] != "green":
                raise DdakToolError(ErrorCode.PRECONDITION_FAILED, result["detail"])
            return result
        except DdakToolError as error:
            self._record(project, "docker", {"status": "red", "detail": error.message})
            raise

    def probe(self, project: str, kind: str) -> dict:
        if kind not in {"ai", "inventory", "repository", "build", "docker"}:
            raise DdakToolError(ErrorCode.CONFIG_INVALID, "지원하지 않는 연결 검사")
        if kind == "ai":
            saved = self._selected(project, self._saved(project))
            for ident in {saved.get("generation_provider"), saved.get("judgment_provider")} - {
                None
            }:
                if isinstance(ident, str):
                    self.test_provider(project, ident)
            return {"status": "gray", "detail": "역할별 검사 결과를 확인하세요"}
        try:
            if kind == "build":
                result = self._builder(project).probe()
            elif kind in self._probes:
                result = self._probes[kind](project, self._saved(project))
            elif kind == "docker":
                settings = self._selected(project, self._saved(project))
                result = self._builder(project).probe_repository(settings.get("image_repository"))
            else:
                result = {"status": "gray", "detail": "실제 연결 확인 전"}
        except DdakToolError as error:
            result = {"status": "red", "detail": error.message}
        except Exception:
            result = {"status": "red", "detail": "검사 실패. 연결 설정을 확인하세요"}
        return self._record(project, kind, result)

    def require_ready(self, project: str, targets: str | None = None) -> None:
        view = self.view(project)
        if (
            any(s["id"] == "repository" and s["status"] == "red" for s in view["checklist"])
            and self.effective(project, self._saved(project), self.vault).adapter_mode
            is AdapterMode.REAL
        ):
            # 머신 helper 로그인 변경은 제품 설정 버전에 반영되지 않는다.
            self.probe(project, "repository")
            view = self.view(project)
        skipped = {"inventory"} if targets == "cloud" else set()
        if view["settings"].get("build_backend") != "local":
            skipped |= {"build", "docker"}
        failed = [
            s["label"] for s in view["checklist"] if s["status"] == "red" and s["id"] not in skipped
        ]
        if failed:
            raise DdakToolError(
                ErrorCode.PRECONDITION_FAILED, "연결 확인 필요: " + ", ".join(failed)
            )
