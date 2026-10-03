"""코드 질문용 소스 문맥 고르기(결정적, AI 없음). 앱 저장소 커밋을 git 읽기 명령으로만 본다.

- 작업 트리를 바꾸지 않는다: `git ls-tree`, `git grep <sha>`, `git cat-file`만 쓴다.
- 비밀 경로(.env*, .secrets/, 키·인증서, tfstate, SSH 키 등)는 파일 목록과 내용 모두에서 뺀다.
- 내용은 redact를 거친 뒤 크기를 잰다. 총량은 계약의 MAX_CONTEXT_BYTES 안이다.
- 질문 단어와 경로·내용 일치로 점수를 매기고 README·진입 파일을 먼저 넣는다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from ddak.core.contracts.tools.answer_code_question import MAX_CONTEXT_BYTES, CodeFile
from ddak.core.redact import redact

GitBytes = Callable[..., bytes]  # AppRepository.git_bytes(*args, ok=...)

_SECRET_DIRS = frozenset({".secrets", ".aws", ".ssh", ".gnupg", ".docker", ".claude", ".codex"})
_SECRET_NAMES = frozenset(
    {".netrc", ".pgpass", ".npmrc", ".pypirc", ".htpasswd", ".git-credentials", "credentials"}
)
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk", ".kdbx", ".tfvars")
_SSH_KEYS = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519")
# 비밀은 아니지만 답에 쓸모없는 디렉토리(목록·내용 모두 제외)
_NOISE_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".terraform", "dist", "build",
     ".pytest_cache", ".ruff_cache", ".mypy_cache", ".cache", ".idea", ".vscode", "var", "instance"}
)  # fmt: skip
# 목록에는 남기되 내용은 넣지 않는 파일(이진·잠금 파일)
_NO_CONTENT_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".pdf", ".zip", ".gz", ".tgz",
    ".tar", ".whl", ".jar", ".so", ".dylib", ".dll", ".exe", ".pyc", ".woff", ".woff2", ".ttf",
    ".eot", ".mp4", ".mp3", ".sqlite", ".sqlite3", ".db", ".lock", ".min.js", ".map",
)  # fmt: skip
_NO_CONTENT_NAMES = frozenset({"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock"})
_ENTRY_NAMES = frozenset(
    {"app.py", "main.py", "wsgi.py", "asgi.py", "manage.py", "__init__.py", "config.py",
     "settings.py", "dockerfile", "pyproject.toml", "setup.py", "setup.cfg", "package.json",
     "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", "procfile",
     "deploy.yaml", "schema.sql", "makefile"}
)  # fmt: skip
_STOP = frozenset(
    {"the", "and", "for", "what", "how", "why", "where", "which", "does", "this", "that", "with",
     "from", "into", "are", "is", "do", "in", "of", "to", "it", "code", "file", "files",
     "코드", "파일", "어디", "어디서", "무엇", "어떻게", "설명", "알려줘", "있나요", "있나", "하는"}
)  # fmt: skip
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[./-][A-Za-z0-9_]+)*|[가-힣]{2,}")

MAX_FILE_BLOB = 512 * 1024  # 이보다 큰 파일은 읽지 않는다(목록에는 남김)
PER_FILE_BYTES = 16_000  # 파일 하나가 문맥을 다 차지하지 않게 자른다
LIST_BYTES = 6_000  # 파일 목록 몫
MAX_FILES = 80
MAX_TERMS = 12


def is_secret_path(path: str) -> bool:
    """비밀값이 들어 있을 수 있는 경로. 목록과 내용 모두에서 뺀다."""
    parts = [p.lower() for p in PurePosixPath(path).parts]
    if not parts:
        return True
    name = parts[-1]
    return (
        any(p in _SECRET_DIRS or p.startswith(".env") for p in parts)
        or name in _SECRET_NAMES
        or name.endswith(_SECRET_SUFFIXES)
        or name.endswith(".env")
        or ".tfstate" in name
        or name.startswith(_SSH_KEYS)
        or PurePosixPath(name).stem in {"secret", "secrets"}
    )


def _noise(path: str) -> bool:
    return any(p in _NOISE_DIRS for p in PurePosixPath(path).parts[:-1])


def _readable(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return not (name in _NO_CONTENT_NAMES or name.endswith(_NO_CONTENT_SUFFIXES))


def question_terms(question: str) -> list[str]:
    """질문에서 경로·내용 검색어를 뽑는다(소문자, 중복 제거, 최대 MAX_TERMS개)."""
    terms: list[str] = []
    for word in _WORD.findall(question):
        for term in (word, *re.split(r"[./-]", word)):
            term = term.lower()
            if len(term) >= 2 and term not in _STOP and term not in terms:
                terms.append(term)
    return terms[:MAX_TERMS]


def _priority(path: str) -> int:
    pure = PurePosixPath(path)
    name, depth = pure.name.lower(), len(pure.parts)
    if name.startswith("readme"):
        return 8 if depth == 1 else 3
    if (
        name in _ENTRY_NAMES
        or name.startswith("dockerfile")
        or (name.startswith("requirements") and name.endswith(".txt"))
    ):
        return 6 if depth <= 2 else 3
    return 0


@dataclass(frozen=True)
class _Blob:
    path: str
    oid: str
    size: int


@dataclass(frozen=True)
class CodeContext:
    files: tuple[CodeFile, ...]
    paths: tuple[str, ...]
    truncated: bool  # 잘린 파일이나 상한 때문에 빠진 파일이 있다
    skipped_secret: int  # 비밀 경로라 뺀 파일 수(경로 이름은 남기지 않는다)

    @property
    def total_bytes(self) -> int:
        return sum(len(f.content.encode()) for f in self.files) + sum(
            len(p.encode()) + 1 for p in self.paths
        )


def _tree(git: GitBytes, sha: str) -> list[_Blob]:
    blobs = []
    for entry in git("ls-tree", "-r", "-z", "--long", sha).split(b"\0"):
        if not entry:
            continue
        meta, _, raw = entry.partition(b"\t")
        mode, kind, oid, size = meta.decode().split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            continue  # 심볼릭 링크·서브모듈은 읽지 않는다
        blobs.append(_Blob(raw.decode("utf-8", errors="replace"), oid, int(size)))
    return blobs


def _content_hits(git: GitBytes, sha: str, terms: list[str]) -> dict[str, int]:
    hits: dict[str, int] = {}
    prefix = sha.encode() + b":"
    for term in terms:
        out = git("grep", "-I", "-i", "-F", "-c", "-z", "-e", term, sha, "--", ok=(0, 1))
        for line in out.split(b"\n"):
            head, _, count = line.partition(b"\0")
            if not head.startswith(prefix) or not count.strip().isdigit():
                continue
            path = head[len(prefix) :].decode("utf-8", errors="replace")
            # 내용에 질문 단어가 있으면 README 기본 가산보다 앞서게 한다.
            hits[path] = hits.get(path, 0) + 4 + min(int(count), 5)
    return hits


def _score(path: str, terms: list[str]) -> int:
    pure = PurePosixPath(path.lower())
    score = _priority(path)
    for term in terms:
        if term in (pure.name, pure.stem):
            score += 10
        elif term in str(pure):
            score += 5
    return score


def _cut(data: bytes, limit: int) -> str:
    return data[:limit].decode("utf-8", errors="ignore")


def collect_context(
    git: GitBytes, sha: str, question: str, *, limit: int = MAX_CONTEXT_BYTES
) -> CodeContext:
    """커밋 sha의 파일에서 질문과 관련 있는 것을 골라 limit 바이트 안의 문맥을 만든다."""
    blobs = _tree(git, sha)
    secret = [b for b in blobs if is_secret_path(b.path)]
    visible = [b for b in blobs if not is_secret_path(b.path) and not _noise(b.path)]
    truncated = False

    paths: list[str] = []
    used = 0
    for blob in sorted(visible, key=lambda b: (len(PurePosixPath(b.path).parts), b.path)):
        size = len(blob.path.encode()) + 1
        if used + size > min(LIST_BYTES, limit):
            truncated = True
            break
        paths.append(blob.path)
        used += size

    terms = question_terms(question)
    hits = _content_hits(git, sha, terms) if terms else {}
    candidates = [b for b in visible if _readable(b.path) and b.size <= MAX_FILE_BLOB]
    candidates.sort(
        key=lambda b: (
            -(_score(b.path, terms) + hits.get(b.path, 0)),
            len(PurePosixPath(b.path).parts),
            b.path,
        )
    )
    files: list[CodeFile] = []
    for blob in candidates:
        remaining = limit - used
        if remaining < 512 or len(files) >= MAX_FILES:
            truncated = True
            break
        data = git("cat-file", "blob", blob.oid)
        if b"\0" in data[:8000]:
            continue  # 이진 파일
        # 비밀값 모양을 먼저 가린 뒤 크기를 잰다(AI에는 가린 내용만 간다).
        encoded = redact(data.decode("utf-8", errors="replace"), max_len=None).encode()
        cap = min(PER_FILE_BYTES, remaining)
        cut = len(encoded) > cap
        content = _cut(encoded, cap) if cut else encoded.decode()
        truncated = truncated or cut
        files.append(CodeFile(path=blob.path, content=content, truncated=cut))
        used += len(content.encode())
    return CodeContext(
        files=tuple(files), paths=tuple(paths), truncated=truncated, skipped_secret=len(secret)
    )
