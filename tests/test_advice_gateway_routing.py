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
        # BOTH reply shapes from one spy, so one harness serves both providers:
        # `_claude` reads `content`, `_grok` reads `choices`. A second spy would
        # be a second thing to keep in step for no gain.
        return {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "choices": [{"message": {"content": "ok"}}],
        }


#: The header each provider sends when it is NOT routed through the gateway.
#: `claude` is BYOK with `x-api-key`; `grok` is BYOK with `Authorization`.
DIRECT_HEADER = {"claude": "x-api-key", "grok": "Authorization"}


def _cfg_for(provider: str, url: str) -> AdviceConfig:
    if provider == "claude":
        return AdviceConfig(provider="claude", claude_url=url, claude_key="THE-TOKEN")
    return AdviceConfig(provider="grok", grok_url=url, grok_key="THE-TOKEN")


@pytest.mark.parametrize("provider", sorted(DIRECT_HEADER))
@pytest.mark.parametrize("url,expect_gateway,why", CASES, ids=[c[2][:38] for c in CASES])
def test_the_credential_header_follows_the_parsed_host(
    provider, url, expect_gateway, why
) -> None:
    """ONE body, BOTH providers, the SAME bypass table (#155).

    `grok` was added to this decision and the table was not copied, because a
    second table is a second thing that can be fixed on one side only: every
    bypass shape below now has to hold for both providers, and a shape fixed
    for one is fixed for both. The cases are anthropic-shaped URLs, which is
    fine and is the point: `_is_cf_gateway` parses the HOST, so the
    provider-specific path after the gateway id cannot change the answer.
    """
    spy = _Spy()
    Advisor(cfg=_cfg_for(provider, url), transport=spy).ask("q", "ctx")

    direct = DIRECT_HEADER[provider]
    sent_to_gateway = "cf-aig-authorization" in spy.headers
    assert sent_to_gateway is expect_gateway, (
        f"{provider}: {why}: expected gateway_auth={expect_gateway}, "
        f"got {sent_to_gateway} for {url}"
    )
    # And the other header must NOT also be present: a credential that goes out
    # under both names is the same leak wearing a second label. For the gateway
    # case this is also the assertion that NO provider key is sent at all,
    # which is what Unified Billing requires and what a self-hoster must not
    # get by accident.
    if expect_gateway:
        assert direct not in spy.headers, (
            f"{provider} sent its direct credential to the gateway as well"
        )
    else:
        assert direct in spy.headers, (
            f"{provider} sent no direct credential to a non-gateway host"
        )


def test_a_self_hoster_changing_nothing_keeps_byok_on_both_providers() -> None:
    """The DEFAULT config must stay direct BYOK for both providers.

    The ruling routes BYOK through the gateway as an option the operator takes
    by setting a URL, not as a new default. So the shipped defaults are pinned:
    a self-hoster who changes nothing keeps their own key going straight to the
    provider, and neither provider acquires a Cloudflare dependency.
    """
    for provider in sorted(DIRECT_HEADER):
        spy = _Spy()
        # A KEY, AND THE SHIPPED URL. That is the self-hoster being pinned: own
        # credential, default endpoint. My first version of this fixture set no
        # key, the advisor short-circuited before calling the transport, and
        # `spy.headers` was `{}`.
        #
        # Which is worth a line, because an empty dict satisfies the
        # cf-aig-absence assertion VACUOUSLY: had this test asserted only that
        # the gateway header is absent, it would have passed while measuring
        # nothing at all. The positive half is what caught it, and the explicit
        # called-at-all check below is what stops either half passing on an
        # unmade request.
        cfg = AdviceConfig(provider=provider, claude_key="K", grok_key="K")
        Advisor(cfg=cfg, transport=spy).ask("q", "ctx")
        assert spy.headers, f"{provider} never reached the transport, so nothing was measured"
        assert "cf-aig-authorization" not in spy.headers, (
            f"{provider}'s default config routes through the gateway"
        )
        assert DIRECT_HEADER[provider] in spy.headers, (
            f"{provider}'s default config sends no direct credential"
        )
