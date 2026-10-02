"""A provider can contribute webhook signature schemes; they reach its catalog entry."""

from marvin_integration_sdk import IntegrationProvider


class _Shop(IntegrationProvider):
    slug = "shop"
    name = "Shop"
    signature_schemes = {"shop": {"encoding": "base64", "message": "{url}{body}", "header": "x-shop-sig"}}


def test_declared_signature_schemes_reach_info():
    assert _Shop().info()["signature_schemes"] == {"shop": {"encoding": "base64", "message": "{url}{body}", "header": "x-shop-sig"}}


def test_declaring_none_is_the_default():
    class _Plain(IntegrationProvider):
        slug = "plain"
        name = "Plain"

    assert _Plain().info()["signature_schemes"] == {}
