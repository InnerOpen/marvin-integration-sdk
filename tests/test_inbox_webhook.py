"""A provider may turn a webhook delivery into Social inbox items (0.18.0); by default it turns none."""

from marvin_integration_sdk import INBOX_KINDS, IntegrationProvider


class _Plain(IntegrationProvider):
    slug = "plain"
    name = "Plain"


def test_a_provider_returns_no_inbox_items_by_default():
    assert _Plain().inbox_items_from_webhook({"object": "page"}, None) == []


def test_the_inbox_kinds_start_with_what_a_comments_read_returns():
    assert INBOX_KINDS[:3] == ("comment", "reply", "mention")
    assert "message" in INBOX_KINDS
