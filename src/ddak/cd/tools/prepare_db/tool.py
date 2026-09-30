"""O1 prepare_db 공통 dispatch."""

from ddak.cd.dispatch import provider_for
from ddak.core.contracts.context import RunContext
from ddak.core.contracts.tools.prepare_db import PrepareDbInput, PrepareDbOutput
from ddak.core.registry import tool


@tool("prepare_db")
def prepare_db(inp: PrepareDbInput, ctx: RunContext) -> PrepareDbOutput:
    result = provider_for(inp, ctx).migrate_db(inp.migrations, ctx)
    return PrepareDbOutput.model_validate(result.model_dump(mode="json"))
