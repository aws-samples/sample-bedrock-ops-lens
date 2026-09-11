"""Canonical model identity, for matching Service Quotas names to model ids.

Audit follow-up #2 then round 3. The first matcher reduced both sides to an
alnum-only string and required every distinctive token of the quota name to
appear as a SUBSTRING, so "Claude Sonnet 4" matched a Sonnet 4.5 id (because "4"
is a substring of "4-5") and 80,000 TPM was scored against another model's
1,000,000 ceiling: 8% reported where the truth was 160%.

Fixing that by comparing an exact version tuple introduced two NEW regressions,
both demonstrated against the live AWS catalog (round 3, R3-02):

  * `mistral.mistral-large-2402-v1:0` and `mistral.mistral-large-2407-v1:0` are
    both ACTIVE and have separate quotas. Treating any numeric token longer than
    two digits as a build number collapsed both to `(("large",), ())`, so the
    2402 model borrowed the 2407 limit: 160% became 8%.
  * `cohere.embed-v4:0` is ACTIVE with a real "Cohere Embed V4" quota. Stripping
    a trailing `-vN:M` as a disposable transport revision erased the model
    GENERATION, so the id became `(("embed",), ())` while the quota name became
    `(("embed","v"), (4,))` - no match at all, and a 300K limit went missing.

The distinction that actually holds in Bedrock's naming: a trailing `-vN[:M]` is
a revision when the name already carries a version (Claude
`…-sonnet-4-5-20250929-v1:0`), and IS the version when nothing else does
(`cohere.embed-v4:0`, `amazon.titan-embed-text-v2:0`). So identity keeps three
parts and the revision is compared only when the quota name states one:

    identity = (words, version, revision)

    "Claude Sonnet 4.5"                -> (('claude','sonnet'), (4,5), ())
    "…claude-sonnet-4-5-20250929-v1:0" -> (('claude','sonnet'), (4,5), (1,))
    "Claude Sonnet 4"                  -> (('claude','sonnet'), (4,),  ())
    "Mistral Large 2407"               -> (('large',),          (2407,), ())
    "…mistral-large-2402-v1:0"         -> (('large',),          (2402,), (1,))
    "Cohere Embed V4"                  -> (('embed',),          (4,),  ())
    "cohere.embed-v4:0"                -> (('embed',),          (4,),  ())

Matching is RANKED rather than boolean, because AWS publishes both
version-specific quota names ("Mistral Large 2407") and generic ones ("Mistral AI
Mistral Large"):

    2 = exact  - words agree and the versions are equal
    1 = generic - words agree and the quota name states NO version
    0 = no match

`resolve_quota` prefers rank 2, and when only generic candidates remain and they
disagree on value it reports AMBIGUOUS rather than silently taking the largest -
which is what produced the Mistral error.
"""
from __future__ import annotations

import re

# Vendor / marketing words that appear on one side but not the other, plus words
# that never distinguish one SKU from another.
_NOISE_WORDS = {
    "anthropic", "amazon", "meta", "mistral", "mistralai", "ai21", "cohere",
    "stability", "stabilityai", "deepseek", "openai", "writer", "luma", "qwen",
    "twelvelabs", "ai", "labs", "inc", "the", "for", "model", "models",
    "version", "tokens", "per", "minute", "day", "requests", "and",
    # A bare "v" is left over from splitting "V4" into ("v", "4").
    "v",
    # Endpoint/family words: the family is carried separately (traffic_type), so
    # it must not participate in model identity.
    "on", "demand", "ondemand", "cross", "region", "crossregion", "global",
    "inference", "profile", "provisioned", "throughput", "batch",
}

# Word spellings AWS uses inconsistently between a quota name and a model id.
# Explicit and checked, per the round-3 recommendation - not stemming, which
# would happily conflate unrelated SKUs.
_WORD_ALIASES = {
    "embeddings": "embed",
    "embedding": "embed",
    "texts": "text",
    "instruct": "instruct",
    "haiku": "haiku",
}

# A trailing revision/generation marker: -v1, -v1:0, :0, " V4".
_REV_SUFFIX = re.compile(r"[\s\-_:]v(\d+)(?::(\d+))?\s*$", re.I)
# A bare trailing ":0" with no v, e.g. "cohere.embed-english-v3:0" once the v-part
# is consumed, or "amazon.titan-tg1-large:0".
_TRAILING_COLON_NUM = re.compile(r":(\d+)\s*$")
# An 8-digit snapshot date: -20250929. Never a version.
_DATE_TOKEN = re.compile(r"^\d{8}$")
# Context-window markers some names carry: 200k, 1m, 18k.
_CTX_TOKEN = re.compile(r"^\d+[km]$", re.I)
# CRIS geography prefixes; the family, not the model.
_GEO_PREFIX = re.compile(r"^(us|eu|apac|jp|au|ca|amer|global|us-gov)\.", re.I)


def _split_tokens(s: str) -> list[str]:
    s = re.sub(r"[^a-z0-9]+", " ", (s or "").lower())
    parts: list[str] = []
    for tok in s.split():
        # "gpt5" / "v4" -> ("gpt", "5") / ("v", "4")
        m = re.match(r"^([a-z]+)(\d+)$", tok)
        if m:
            parts.extend([m.group(1), m.group(2)])
        else:
            parts.append(tok)
    return [p for p in parts if p]


def _version_of(tokens: list[str]) -> tuple[tuple[int, ...], list[str]]:
    """Split numeric version components out of a token list.

    Kept as a version: 1-2 digits (4, 5, 3, 7) and 4 digits (Mistral's YYMM
    2402 / 2407, which round 3 showed are real, distinct SKUs).
    Dropped: 8-digit snapshot dates, context markers, and anything else numeric
    (5-7 or 9+ digits) which is a build number.
    """
    version: list[int] = []
    words: list[str] = []
    for tok in tokens:
        if _DATE_TOKEN.match(tok) or _CTX_TOKEN.match(tok):
            continue
        if tok.isdigit():
            if len(tok) <= 2 or len(tok) == 4:
                version.append(int(tok))
            continue
        words.append(tok)
    while version and version[-1] == 0:
        version.pop()          # 4.0 == 4
    return tuple(version), words


def identity(s: str) -> tuple[tuple[str, ...], tuple[int, ...], tuple[int, ...]]:
    """Canonical (words, version, revision) for a model id or a quota name."""
    raw = (s or "").strip()
    raw = _GEO_PREFIX.sub("", raw)

    revision: tuple[int, ...] = ()
    m = _REV_SUFFIX.search(raw)
    if m:
        rev = [int(m.group(1))]
        if m.group(2) is not None:
            rev.append(int(m.group(2)))
        while len(rev) > 1 and rev[-1] == 0:
            rev.pop()
        revision = tuple(rev)
        raw = raw[:m.start()]
    else:
        m2 = _TRAILING_COLON_NUM.search(raw)
        if m2:
            raw = raw[:m2.start()]

    version, words = _version_of(_split_tokens(raw))
    words = [_WORD_ALIASES.get(w, w) for w in words]
    words = [w for w in words if w not in _NOISE_WORDS]

    # When nothing else carries a version, the trailing marker IS the version -
    # `cohere.embed-v4:0` is Embed *generation 4*, not Embed revision 4.
    if not version and revision:
        version, revision = revision, ()
    return tuple(words), version, revision


def match_rank(model_name: str, model_id: str) -> int:
    """0 = no match, 1 = generic (quota name states no version), 2 = exact."""
    if not model_name or not model_id:
        return 0
    n_words, n_ver, n_rev = identity(model_name)
    i_words, i_ver, i_rev = identity(model_id)
    if not n_words or not i_words:
        return 0
    if not set(n_words) <= set(i_words):
        return 0
    # A revision stated by the quota name must agree: "Claude 3.5 Sonnet V2" must
    # not match the v1 snapshot of the same version.
    if n_rev and n_rev != i_rev:
        return 0
    if n_ver:
        return 2 if n_ver == i_ver else 0
    # No version on the quota name: a legitimate generic entry ("Mistral AI
    # Mistral Large"), usable only when nothing exact matched.
    return 1


def matches(model_name: str, model_id: str) -> bool:
    """True when a quota name denotes the same model as a model id (any rank)."""
    return match_rank(model_name, model_id) > 0
