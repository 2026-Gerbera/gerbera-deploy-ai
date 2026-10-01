"""이전 AWS provider import 경로를 위한 호환 모듈.

새 코드는 ``ddak.cloud.deploy.providers.aws``를 사용한다.
"""

from ddak.cloud.deploy.providers.aws import AwsProvider

__all__ = ["AwsProvider"]
