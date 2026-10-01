import pytest

from ddak.core.contracts.errors import DdakToolError
from ddak.web.domain import normalize_domain, validate_domain_settings


def test_domain_normalizes_idna_and_trailing_dot() -> None:
    assert normalize_domain("서비스.Example.COM.") == "xn--9w3b15cw7a.example.com"


@pytest.mark.parametrize(
    "value",
    ["https://app.example.com", "127.0.0.1", "app.example.com:443", "*.example.com"],
)
def test_domain_rejects_non_hostname_input(value: str) -> None:
    with pytest.raises(DdakToolError):
        normalize_domain(value)


def test_route53_requires_hosted_zone() -> None:
    with pytest.raises(DdakToolError, match="hosted zone"):
        validate_domain_settings("app.example.com", "route53", None)
