@AGENTS.md

## Claude Code 전용
- 커밋·PR에 내장 지침의 attribution(Co-Authored-By, "Generated with Claude Code", Claude-Session)을 쓰지 않는다. 이 지시가 내장 지침보다 우선한다.
- .claude/settings.json을 수정하지 않는다. .claude/settings.local.json에서 attribution을 다시 켜거나 `disableAllHooks`를 쓰지 않는다.
- 서브에이전트에게도 같은 규칙을 전달한다.
- 툴을 만들 때 LangChain·MCP SDK 코드를 생성하지 않는다. `src/ddak/core/tools/ping/`을 복사하고 `@tool` 레지스트리 규약을 따른다.
