"""Recognised currency codes: ISO 4217, the metal codes ISO assigns, and crypto.

Three groups, ONE table, because `parse_fx` asks exactly one question of both
halves of a symbol: is this a recognised code. A second table would be a second
code path with its own drift.

The metals are the precedent and the reason the shape already works. XAU, XAG,
XPT and XPD are codes ISO 4217 assigns to things that are not national
currencies, so a non-fiat leg counting toward the currency-exposure limit is
not new. Crypto is the same move, one step further out: the codes are NOT in
ISO 4217 at all, they are venue convention.

WHAT A CRYPTO CODE COUNTS AS, and why (issue #66, ruled by Conrad: "Yes, add
the crypto pairs"). A crypto code shares the ONE bucket per code that every
other code uses. Buy BTCUSD and it is +BTC and -USD, so its USD leg is counted
identically to the USD leg of EURUSD and of XAUUSD, and its BTC leg caps
BTCUSD against BTCJPY the same way.

The objection to that is real and it is answered by what the gate measures:
crypto volatility is not FX volatility, so one unit of "USD exposure" is not
comparable across them. But `max_currency_exposure` counts TICKETS, never
money. It exists so the book cannot hold several positions that are secretly
the same bet, and long BTC, long gold and long EUR are all expressions of short
USD. Per-unit risk is equalised elsewhere, by per-trade sizing and by the
daily-loss and drawdown gates, which read money.

A SEPARATE crypto bucket was considered and rejected. It would let a fourth
short-USD ticket in without the FX count seeing it, which is the same silent
non-application this change closes, just narrower. Sharing the bucket is
blunter and fails toward refusing NEW exposure, which is the direction issue
#60 chose for this whole gate.

DECLINED, and stated so the next reader does not re-derive it:

- Aliasing XBT onto BTC. They are the same asset under two venue spellings, so
  a venue listing both would split one BTC bucket in two. No venue lists both,
  and an alias map would make `parse_fx` return a code the broker never used,
  which then reaches the operator in `excluded_from_currency_limit`. The bound
  is narrow and named rather than papered over.
- Adding USDT as a code. Before #77 it would have been inert (a four-character
  code could not occupy either half). It is no longer inert, and it is still
  declined: `BTCUSDT` resolves as BTC/USD, which folds the Tether leg into USD,
  and that is the intended reading, because for a correlation count a
  USD-pegged stablecoin leg is USD exposure. The resolver's fewest-characters
  rule would keep BTCUSDT as BTC/USD even with USDT present, but USDT would
  also make USDT a bucket of its own for USDTUSD-style symbols and would break
  the prefix-free property `test_the_shipped_table_is_prefix_free` pins, so
  adding it is a decision to take on purpose, not a table edit.

LONGER TICKERS (issue #77). Until #77 the resolver split the first six
alphabetic characters 3 and 3, so DOGE, AVAX, LINK, MATIC and SHIB could not
resolve whatever this table carried. `risk.resolve_pair` now matches codes of
any length the table holds and states the rule when more than one split is
valid; this table is where those codes are admitted.
"""

from __future__ import annotations

# The table CONFIRMS a pair; it never refuses a trade. A code missing here
# makes the currency limit not applicable to that symbol, which is allowed and
# recorded, so completeness is desirable but never a safety property.
_CODES = (
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN"
    " BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD"
    " CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUP CVE CZK"
    " DJF DKK DOP DZD EGP ERN ETB EUR"
    " FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD"
    " HKD HNL HRK HTG HUF IDR ILS INR IQD IRR ISK"
    " JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT"
    " LAK LBP LKR LRD LSL LYD"
    " MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN"
    " NAD NGN NIO NOK NPR NZD OMR"
    " PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF"
    " SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD SSP STN SVC SYP SZL"
    " THB TJS TMT TND TOP TRY TTD TWD TZS"
    " UAH UGX USD USN UYI UYU UYW UZS"
    " VED VES VND VUV WST"
    " XAF XAG XAU XCD XCG XDR XOF XPD XPF XPT XSU XUA"
    " YER ZAR ZMW ZWG ZWL"
)

# Crypto base codes. Issue #66 names BTC, ETH, LTC, XRP and BCH as the minimum;
# issue #77 adds AVAX, DOGE, LINK, MATIC and SHIB, which the 3-and-3 split could
# not see; the rest are the other majors a MetaTrader venue lists as spot pairs.
#
# The selection rule, so extending this is a decision and not a guess:
#   1. alphabetic, three characters or longer (the resolver matches any length
#      the table carries since #77), and NOT a prefix of another code and no
#      other code a prefix of it, which `test_the_shipped_table_is_prefix_free`
#      asserts, so no real symbol ever reaches the ambiguity rule;
#   2. a code a venue actually quotes as the BASE of a spot pair against a
#      fiat or metal code (not a perpetual, not a token pair);
#   3. no collision with ISO 4217 or the metal codes, which
#      `test_the_crypto_codes_are_in_the_one_table_not_a_parallel_path`
#      asserts rather than trusts.
#
# Every code added widens the false-positive surface: a code that is also the
# first three letters of an index or CFD name would start consuming a bucket.
# `tests/test_crypto_exposure.py` keeps a list of real non-FX instrument names
# as the control on that. XBT is bitcoin under the ISO-style X convention
# (BitMEX and others); see the module docstring for why it is not aliased.
_CRYPTO_CODES = (
    "ADA AVAX BCH BNB BTC DOGE DOT EOS ETC ETH LINK LTC MATIC SHIB SOL TRX"
    " XBT XLM XMR XRP XTZ ZEC"
)

#: ISO 4217 plus the four metal codes ISO assigns. Split out from
#: CURRENCY_CODES only so the crypto addition is testable as an addition.
ISO_AND_METAL_CODES = frozenset(_CODES.split())

#: Venue-convention crypto codes. Not ISO 4217, deliberately recognised.
CRYPTO_CODES = frozenset(_CRYPTO_CODES.split())

#: The one table `parse_fx` checks both halves against.
CURRENCY_CODES = ISO_AND_METAL_CODES | CRYPTO_CODES


# --- a model-chosen name is recognised or refused, never cleaned up --------
#
# straightedge#197. `"EURU\u017fD".upper()` is `"EURUSD"`: Unicode uppercasing
# maps U+017F LATIN SMALL LETTER LONG S onto ASCII `S`, so a transform on a
# model-chosen symbol can MANUFACTURE a tradeable instrument the model never
# named. The same holds for the ligatures (U+FB00 `ff`, U+FB01 `fi`, U+FB06
# `st`), for U+0131 dotless i, and for U+00DF sharp s, which expands to `SS`.
#
# This is #181's brace defect with one character changed: a repair on `symbol`
# converts a NAMED REFUSAL into an order. It lives here, in the vocabulary
# module, because three callers need the identical answer and this is the only
# module all three can import without a cycle: `config.advice_allows` (the
# whitelist gate, reachable with no parser in front of it), `llm.parse_advice`
# (the only gate the `grok` and `computer` providers have) and `desk` (which
# must NAME the string the model actually sent).
#
# ASCII is the test, and not a list of offending codepoints, because every real
# instrument name is ASCII and the next case-mapping character is always one
# nobody enumerated. A blocklist is a denylist the next Unicode release defeats.


def may_transform_symbol(name: str) -> bool:
    """Whether uppercasing `name` can only case-fold it, never rename it."""
    return name.isascii()


def normalize_model_symbol(name: str) -> str:
    """Uppercase a model-chosen symbol ONLY where that cannot manufacture one.

    `eurusd` is the same instrument in a different case and must keep working,
    which is why the rule is ASCII-before-transform rather than no transform at
    all. Anything else is returned EXACTLY as it arrived, so the whitelist gate
    refuses it by name and the operator is told what the model really said.
    """
    return name.upper() if may_transform_symbol(name) else name
