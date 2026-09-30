"""inject_env_config 입출력."""

from pydantic import Field

from ddak.core.contracts.tools.cd import CdInput, CdOutput


class InjectEnvConfigInput(CdInput):
    keys: list[str] = Field(default_factory=list)


class InjectEnvConfigOutput(CdOutput):
    pass
