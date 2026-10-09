"""The URL that decides which credential leaves this process (#167).

CodeQL flagged the original substring form high as
`py/incomplete-url-substring-sanitization`, and the suite at the time could not
see it: the only coverage asserted the canonical gateway URL and the direct URL,
and BOTH pass under the vulnerable implementation. Reverting the fix left 1182
tests green.

That matters more than usual here. `CodeQL` was removed as a required status
context org-wide on 2026-10-09 because it reported `neutral` for whole classes of
change and made "wait for green" non-terminating under a strict up-to-date
policy. So for this defect the suite is the only gate left, and it was blind to
exactly the shape it needed to see.

These cases assert the HEADER the real `Advisor` sends, not the predicate, so a
correct helper wired up wrongly also reds. The bypass shapes come from a review
that drove them against the live code; `userinfo` was in nobody's first table and
is the one a human skimming a config file is most likely to read as the gateway.
"""

from __future__ import annotations

import pytest

from straightedge.config import AdviceConfig
from straightedge.llm import Advisor

GATEWAY = "https://gateway.ai.cloudflare.com/v1/acct/gw/anthropic/v1/messages"

# (url, expect_gateway_auth, why)
CASES = [
    (GATEWAY, True, "the canonical gateway URL"),
    (GATEWAY.replace("gateway.ai", "GATEWAY.AI").upper().replace("HTTPS", "https"),
     True, "hosts are case-insensitive, so an uppercase host is still the gateway"),
    ("https://api.anthropic.com/v1/messages", False, "the direct BYOK endpoint"),
    ("https://evil.example.com/gateway.ai.cloudflare.com/v1/messages",
     False, "the host appears in the PATH"),
    ("https://evil.example.com/v1/messages?upstream=gateway.ai.cloudflare.com",
     False, "the host appears in the QUERY"),
    ("https://gateway.ai.cloudflare.com.evil.example/v1/messages",
     False, "a lookalike anyone can register, the likeliest typo shape"),
    ("https://gateway.ai.cloudflare.com@evil.example/v1/messages",
     False, "USERINFO: reads as the gateway to a human, resolves to evil.example"),
    ("https://user:pw@gateway.ai.cloudflare.com/v1/a/anthropic/v1/messages",
     True, "userinfo on the REAL gateway is still the gateway"),
    ("https://sub.gateway.ai.cloudflare.com/v1/a/anthropic/v1/messages",
     True, "a true subdomain of the gateway"),
    ("https://notgateway.ai.cloudflare.com/v1/messages",
     False, "a hyphen-free lookalike that is not a subdomain"),
]


class _Spy:
    """Captures the headers instead of making a request."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    def post_json(self, url, body, timeout=None, headers=None):  # noqa: ANN001
        self.headers = dict(headers or {})
        return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}


@pytest.mark.parametrize("url,expect_gateway,why", CASES, ids=[c[2][:38] for c in CASES])
def test_the_credential_header_follows_the_parsed_host(url, expect_gateway, why) -> None:
    spy = _Spy()
    cfg = AdviceConfig(provider="claude", claude_url=url, claude_key="THE-TOKEN")
    Advisor(cfg=cfg, transport=spy).ask("q", "ctx")

    sent_to_gateway = "cf-aig-authorization" in spy.headers
    assert sent_to_gateway is expect_gateway, (
        f"{why}: expected gateway_auth={expect_gateway}, got {sent_to_gateway} for {url}"
    )
    # And the other header must NOT also be present: a credential that goes out
    # under both names is the same leak wearing a second label.
    if expect_gateway:
        assert "x-api-key" not in spy.headers
    else:
        assert "x-api-key" in spy.headers
