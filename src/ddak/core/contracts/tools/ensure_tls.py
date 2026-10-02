"""TLS는 플랫폼이 준비하고 이 툴은 상태만 확인한다."""

from typing import Literal

from ddak.core.contracts.enums import Source
from ddak.core.contracts.tools.cd import CdInput, CdOutput


class EnsureTlsInput(CdInput):
    mode: Literal["check", "apply"] = "check"


class EnsureTlsOutput(CdOutput):
    source: Source = Source.LIVE
