"""prepare_db 입출력."""

from pydantic import Field

from ddak.core.contracts.tools.cd import CdInput, CdOutput


class PrepareDbInput(CdInput):
    migrations: list[str] = Field(default_factory=list)


class PrepareDbOutput(CdOutput):
    pass
