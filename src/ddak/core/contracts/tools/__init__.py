"""툴별 입출력 모델. 1툴 1파일: <tool_name>.py 안에 <ToolName>Input / <ToolName>Output.

규칙(docs/harness/02 C-2):
- ToolInput(run_id 필수)을 상속한다. target이 있는 툴은 target: Target, ④ 배포 툴은 lock_token.
- 자유 문자열 명령, 빌드 명령 재정의, IAM 역할 재정의, 도메인·호스트·URL 입력은 열지 않는다.
- 비밀값 필드는 SecretStr. AI 툴 출력에는 ai_usage(AIUsage)와 source(Source)를 넣는다.
"""

from ddak.core.contracts.tools.post_report import PostReportInput, PostReportOutput, ReportIssue
from ddak.core.contracts.tools.verify_tls import TlsCheck, VerifyTlsInput, VerifyTlsOutput

__all__ = [
    "HealthCheckInput",
    "HealthCheckOutput",
    "PostReportInput",
    "PostReportOutput",
    "ReportIssue",
    "TlsCheck",
    "VerifyTlsInput",
    "VerifyTlsOutput",
]
from ddak.core.contracts.tools.health_check import HealthCheckInput, HealthCheckOutput
