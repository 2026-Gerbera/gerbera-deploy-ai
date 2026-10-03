"""plan/intake/watch: 가짜 resolve·시계·sleep으로 감시 루프를 증명한다(실제 대기·네트워크 없음)."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from ddak.core.contracts.errors import DdakToolError, ErrorCode
from ddak.plan.intake import FetchPolicy, Watcher, WatchTarget, load_watch_targets, resolve_head
from ddak.plan.intake import watch as w
from ddak.plan.intake.fetch import warm_cache

A, B, C, D = ("a" * 40, "b" * 40, "c" * 40, "d" * 40)
T = WatchTarget("demo", "https://github.com/o/r")
T2 = WatchTarget("other", "https://github.com/o/r2")


class Sim:
    """heads를 tick마다 하나씩 소비하는 가짜 원격. sleep은 tick을 세고 끝나면 stop한다."""

    def __init__(
        self,
        tmp: Path,
        heads: list,
        handler=None,
        targets=(T,),
        max_ticks=20,
        initial_trigger=False,
        get_state=None,
    ):
        self.heads, self.i, self.calls, self.sleeps, self.warms = heads, 0, [], [], 0
        self.max, self.watcher = max_ticks, None
        self.policy = FetchPolicy(root=tmp)
        self.handler = handler or self._ok
        self.targets = list(targets)
        self.initial_trigger, self.get_state = initial_trigger, get_state

    async def _ok(self, t, sha):
        self.calls.append((t.project, sha))

    def resolve(self, url, ref, policy):
        h = self.heads[min(self.i, len(self.heads) - 1)]
        if isinstance(h, Exception):
            raise h
        return h

    def warm(self, t, policy):
        self.warms += 1
        return A

    async def sleep(self, s):
        self.sleeps.append(s)
        self.i += 1
        await asyncio.sleep(0)
        if self.i >= self.max:
            await self.watcher.stop()

    def run(self):
        self.watcher = Watcher(
            self.targets, self.handler, policy=self.policy, sleep=self.sleep,
            resolve=self.resolve, warm=self.warm, clock=lambda: 1.0,
            initial_trigger=self.initial_trigger, get_state=self.get_state,
        )  # fmt: skip
        asyncio.run(asyncio.wait_for(self.watcher.run(), 5))
        return self


def test_state_roundtrip_missing_corrupt_atomic(tmp_path: Path):
    p = FetchPolicy(root=tmp_path)
    assert w.read_last_commit(p, T) is None
    w.write_last_commit(p, T, A)
    assert w.read_last_commit(p, T) == A
    raw = (tmp_path / "watch" / "demo.json").read_text()
    assert str(tmp_path) not in raw and "token" not in raw.lower()
    assert json.loads(raw)["ref"] == "prod"
    assert w.read_last_commit(p, WatchTarget("demo", "https://github.com/o/other")) is None
    (tmp_path / "watch" / "demo.json").write_text("{broken")
    assert w.read_last_commit(p, T) is None
    w.write_last_commit(p, T, B)
    # 쓰기 도중 중단(rename 직전)이면 기존 파일이 유지된다
    orig = Path.replace

    def boom(self, target):
        raise OSError("interrupted")

    Path.replace = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(OSError):
            w.write_last_commit(p, T, C)
    finally:
        Path.replace = orig  # type: ignore[method-assign]
    assert w.read_last_commit(p, T) == B


def test_baseline_no_trigger_then_push_triggers(tmp_path: Path):
    s = Sim(tmp_path, [A, A, B, B], max_ticks=5).run()
    assert s.warms == 1 and s.calls == [("demo", B)]
    assert w.read_last_commit(s.policy, T) == B


def test_restart_same_sha_no_trigger_and_missed_push(tmp_path: Path):
    p = FetchPolicy(root=tmp_path)
    w.write_last_commit(p, T, A)
    s = Sim(tmp_path, [A], max_ticks=3).run()
    assert s.calls == [] and s.warms == 0
    s = Sim(tmp_path, [B], max_ticks=3).run()  # 꺼져 있던 동안 push
    assert s.calls == [("demo", B)]


def test_coalesce_latest_only_one_extra_run(tmp_path: Path):
    gate = asyncio.Event()
    started: list[str] = []

    async def slow(t, sha):
        started.append(sha)
        if sha == B:
            await gate.wait()

    p = FetchPolicy(root=tmp_path)
    w.write_last_commit(p, T, A)
    s = Sim(tmp_path, [B, B, C, D, D, D, D, D, D, D], handler=slow, max_ticks=9)
    orig = s.sleep

    async def sleep(sec):
        if s.i == 6:
            gate.set()
        await orig(sec)

    s.sleep = sleep  # type: ignore[method-assign]
    s.run()
    assert started == [B, D]  # C는 버려지고 최신 D만 한 번 더


def test_handler_failure_retries_3_then_done_and_loop_survives(tmp_path: Path):
    tries: list[str] = []

    async def bad(t, sha):
        tries.append(sha)
        if sha == B:
            raise RuntimeError("boom")

    p = FetchPolicy(root=tmp_path)
    w.write_last_commit(p, T, A)
    s = Sim(tmp_path, [B, B, B, B, B, C], handler=bad, max_ticks=12).run()
    assert tries == [B, B, B, C]
    assert w.read_last_commit(s.policy, T) == C


def test_poll_errors_backoff_and_recover(tmp_path: Path):
    p = FetchPolicy(root=tmp_path)
    w.write_last_commit(p, T, A)
    heads = [RuntimeError("net")] * 5 + [A, B]
    s = Sim(tmp_path, heads, max_ticks=8).run()
    assert s.sleeps[:5] == [20, 40, 60, 60, 60]
    assert s.sleeps[5] == 10 and s.calls == [("demo", B)]


def test_stop_is_immediate_and_projects_independent(tmp_path: Path):
    async def go():
        wt = Watcher(
            [T, T2], lambda t, s: asyncio.sleep(0), policy=FetchPolicy(root=tmp_path),
            resolve=lambda u, r, p: A, warm=lambda t, p: A, initial_trigger=False,
        )  # fmt: skip
        task = asyncio.create_task(wt.run())
        await asyncio.sleep(0.05)
        await wt.stop()  # 진짜 asyncio.sleep(10) 중에도 즉시 종료
        await asyncio.wait_for(task, 1)

    asyncio.run(go())
    assert w.read_last_commit(FetchPolicy(root=tmp_path), T) == A
    assert w.read_last_commit(FetchPolicy(root=tmp_path), T2) == A


def test_projects_independent_slow_one_does_not_block(tmp_path: Path):
    never = asyncio.Event()
    seen: list[str] = []

    async def h(t, sha):
        seen.append(t.project)
        if t.project == "demo":
            await never.wait()

    p = FetchPolicy(root=tmp_path)
    w.write_last_commit(p, T, A)
    w.write_last_commit(p, T2, A)
    Sim(tmp_path, [B] * 6, handler=h, targets=(T, T2), max_ticks=6).run()
    assert sorted(seen) == ["demo", "other"]


def test_load_targets(monkeypatch):
    with pytest.raises(DdakToolError):
        load_watch_targets({})
    ts = load_watch_targets({"DDAK_WATCH_REPO_URL": "https://github.com/x/y"})
    assert ts[0].repo_url == "https://github.com/x/y" and ts[0].ref == "prod"
    assert (ts[0].project, ts[0].target) == ("flaskr", "local")
    assert w.interval_from_env({}) == 10.0
    assert w.interval_from_env({"DDAK_WATCH_INTERVAL_S": "2.5"}) == 2.5
    with pytest.raises(DdakToolError):
        w.interval_from_env({"DDAK_WATCH_INTERVAL_S": "0"})


def test_resolve_head_and_warm_cache_local_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    for cmd in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run([*g, *cmd], cwd=repo, check=True, timeout=30, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        timeout=30,
        capture_output=True,
        text=True,
    ).stdout.strip()
    p = FetchPolicy(allowed_schemes=("file",), allowed_hosts=None, root=tmp_path / "src")
    url = f"file://{repo}"
    assert resolve_head(url, "main", policy=p) == sha
    assert warm_cache(url, "main", project="demo", policy=p) == sha
    with pytest.raises(DdakToolError):
        resolve_head("https://evil.example/o/r", "main", policy=p)


def test_load_targets_explicit_configuration():
    target = load_watch_targets(
        {
            "DDAK_WATCH_REPO_URL": "https://github.com/x/y",
            "DDAK_WATCH_PROJECT": "custom",
            "DDAK_WATCH_TARGETS": "onprem",
            "DDAK_WATCH_BRANCH": "staging",
        }
    )[0]
    assert (target.project, target.target, target.ref) == ("custom", "local", "staging")
    with pytest.raises(DdakToolError):
        load_watch_targets({"DDAK_WATCH_TARGETS": "unknown"})


def test_initial_head_triggers_by_default_without_success(tmp_path):
    s = Sim(tmp_path, [A] * 6, max_ticks=6, initial_trigger=True).run()
    assert s.calls == [("demo", A)]
    assert w.read_last_commit(s.policy, T) == A


@pytest.mark.parametrize("successful,expected", [(False, [("demo", A)]), (True, [])])
def test_initial_trigger_uses_service_success_even_with_processed_baseline(
    tmp_path, successful, expected
):
    w.write_last_commit(FetchPolicy(root=tmp_path), T, A)

    def state(_):
        return {"last_success": {"local": successful}}

    s = Sim(tmp_path, [A] * 6, max_ticks=6, initial_trigger=True, get_state=state).run()
    assert s.calls == expected


def test_initial_trigger_can_be_disabled(tmp_path):
    s = Sim(tmp_path, [A] * 6, max_ticks=6, initial_trigger=False).run()
    assert s.calls == []
    assert w.initial_trigger_from_env({})
    assert not w.initial_trigger_from_env({"DDAK_WATCH_INITIAL_TRIGGER": "false"})
    with pytest.raises(DdakToolError):
        w.initial_trigger_from_env({"DDAK_WATCH_INITIAL_TRIGGER": "not-a-bool"})


@pytest.mark.parametrize(
    "code,attempts",
    [
        (ErrorCode.CONFIG_INVALID, 1),
        (ErrorCode.PRECONDITION_FAILED, 1),
        (ErrorCode.ADAPTER_TIMEOUT, 3),
    ],
)
def test_deterministic_handler_once_transient_handler_retries(tmp_path, code, attempts):
    tries = []

    async def fail(t, sha):
        tries.append(sha)
        raise DdakToolError(code, "sensitive-placeholder")

    Sim(tmp_path, [A] * 10, handler=fail, max_ticks=10, initial_trigger=True).run()
    assert tries == [A] * attempts


def test_format_poll_error_stops_once(tmp_path):
    s = Sim(tmp_path, ["not-a-sha"], initial_trigger=True).run()
    assert s.calls == [] and s.sleeps == []
    assert w.read_last_commit(s.policy, T) is None


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_nonfinite_watch_interval_rejected(value):
    with pytest.raises(DdakToolError):
        w.interval_from_env({"DDAK_WATCH_INTERVAL_S": value})
