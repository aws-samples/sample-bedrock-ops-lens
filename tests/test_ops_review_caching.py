"""Ops Review prompt caching: catalog, measured use, advice and report assembly.

Run: python -m pytest tests/test_ops_review_caching.py -q

Covers documented, undocumented and unknown models; positive, zero and missing
cache metrics; the deterministic section replacing whatever the report writer
said about caching; and cache-key versioning.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]

from app.ops_review import caching  # noqa: E402
from app.ops_review.caching import ModelCacheMetrics  # noqa: E402

CATALOG = caching.load_catalog()
SONNET = "anthropic.claude-sonnet-4-5-20250929-v1:0"
NOVA = "amazon.nova-lite-v1:0"
OSS = "openai.gpt-oss-120b-1:0"
GPT55 = "openai.gpt-5.5"


def metrics(model_id=SONNET, *, requests=10_000, input_tokens=200_000_000, read=0, write=0,
            missing=0, rows=10, mantle=0, missing_input=0):
    return ModelCacheMetrics(
        model_id=model_id, requests=requests + mantle, accounts=2,
        runtime_requests=requests, runtime_rows=rows, rows_missing_cache=missing,
        rows_missing_input=missing_input, input_tokens=input_tokens,
        cache_read_tokens=read, cache_write_tokens=write, mantle_requests=mantle)


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
def test_catalog_entries_are_exact_reviewed_and_public():
    raw = json.loads(caching.CATALOG_PATH.read_text())
    assert raw["reviewed_on"] and raw["catalog_version"]
    for cap in set(CATALOG.by_id.values()):
        assert cap.doc_url.startswith("https://docs.aws.amazon.com/bedrock/")
        assert cap.support in ("documented", "not_documented")
        if cap.support == "documented" and cap.explicit:
            assert cap.min_tokens_per_checkpoint and cap.ttl and cap.api_guidance


@pytest.mark.parametrize("model_id,name", [
    (SONNET, "Claude Sonnet 4.5"),
    ("us." + SONNET, "Claude Sonnet 4.5"),
    ("global.anthropic.claude-opus-4-8", "Claude Opus 4.8"),
    ("eu.amazon.nova-lite-v1:0", "Nova Lite"),
    ("openai.gpt-oss-120b", "gpt-oss-120b"),      # the bedrock-mantle ID
])
def test_lookup_maps_exact_and_cross_region_ids(model_id, name):
    assert CATALOG.lookup(model_id).name == name


@pytest.mark.parametrize("model_id", [
    "anthropic.claude-3-haiku-20240307-v1:0",     # provider name is not support
    SONNET + ":200k",                             # a provisioned context variant
    "meta.llama3-1-8b-instruct-v1:0",
    "anthropic.claude-sonnet-4-5",                # near miss of an exact ID
    "",
])
def test_lookup_never_infers_support(model_id):
    assert CATALOG.lookup(model_id) is None


def test_gpt_oss_is_listed_as_undocumented_not_excluded_as_open_weight():
    cap = CATALOG.lookup(OSS)
    assert cap.support == "not_documented"
    assert "gpt-oss-120b" in cap.doc_url


@pytest.mark.parametrize("mutate,error", [
    (lambda r: r["models"].append(dict(r["models"][0])), "duplicate"),
    (lambda r: r["models"][0].update(model_ids=["anthropic.claude-*"]), "exact"),
    (lambda r: r["models"][0].update(doc_url="https://example.com/doc"), "public AWS doc"),
    (lambda r: r["models"][0].update(family="nope"), "family"),
    (lambda r: r["models"][0].update(guessed=True), "unknown catalog fields"),
])
def test_invalid_catalog_edits_fail_loudly(mutate, error):
    raw = json.loads(caching.CATALOG_PATH.read_text())
    mutate(raw)
    with pytest.raises(ValueError, match=error):
        caching.parse_catalog(raw)


# --------------------------------------------------------------------------- #
# Measured use
# --------------------------------------------------------------------------- #
def test_any_positive_read_means_in_use_even_at_a_tiny_share():
    m = metrics(read=1_000, input_tokens=999_000_000)
    assert caching.usage_status(m) == "in_use"
    assert caching.cached_share_pct(m, CATALOG.lookup(SONNET)) == 0.0
    code, text = caching.advise(m, CATALOG.lookup(SONNET))
    assert code == "in_use" and "enable" not in text.lower()


def test_zero_reads_are_none_reported_not_disabled():
    m = metrics()
    assert caching.usage_status(m) == "none_reported"
    assert "No cache reads or writes reported" == caching.usage_text(m, "none_reported", None)


@pytest.mark.parametrize("kwargs,status", [
    (dict(missing=10), "unavailable"),            # every row missing
    (dict(missing=3), "unknown"),                 # partly missing, no reads seen
    (dict(missing=3, read=5), "in_use"),          # reads are still evidence
    (dict(rows=0, requests=0, mantle=500), "unavailable"),  # mantle publishes none
    (dict(read=-1), "unknown"),
])
def test_missing_or_inconsistent_metrics_stay_unknown(kwargs, status):
    assert caching.usage_status(metrics(**kwargs)) == status


def test_share_needs_a_compatible_basis_and_a_valid_denominator():
    m = metrics(read=300, write=100, input_tokens=600)
    assert caching.cached_share_pct(m, CATALOG.lookup(SONNET)) == 30.0
    # The OpenAI input counter's relation to cached tokens is not established.
    assert caching.cached_share_pct(metrics(GPT55, read=300, input_tokens=600),
                                    CATALOG.lookup(GPT55)) is None
    assert caching.cached_share_pct(metrics(read=300, missing=1), CATALOG.lookup(SONNET)) is None
    assert caching.cached_share_pct(metrics(read=3, missing_input=1),
                                    CATALOG.lookup(SONNET)) is None
    assert caching.cached_share_pct(metrics(input_tokens=0), CATALOG.lookup(SONNET)) is None
    assert caching.cached_share_pct(metrics(), None) is None


# --------------------------------------------------------------------------- #
# Advice
# --------------------------------------------------------------------------- #
def test_documented_support_and_no_activity_is_evaluate_never_a_promise():
    code, text = caching.advise(metrics(), CATALOG.lookup(SONNET))
    assert code == "evaluate"
    assert text.startswith("Evaluate:") and "1,024" in text and "not guaranteed" in text
    assert "`cachePoint`" in text and "`cache_control" in text
    for promise in ("90%", "85%", "will save", "Enable prompt caching"):
        assert promise not in text


@pytest.mark.parametrize("model_id", [OSS, "openai.gpt-oss-20b"])
def test_undocumented_support_gets_no_enablement_advice(model_id):
    code, text = caching.advise(metrics(model_id), CATALOG.lookup(model_id))
    assert code == "not_documented"
    assert "No enablement recommendation" in text
    assert "checkpoint" not in text and "Evaluate" not in text


def test_unknown_model_gets_no_recommendation():
    code, text = caching.advise(metrics("meta.llama3-1-8b-instruct-v1:0"), None)
    assert code == "unknown_model" and "no recommendation" in text


def test_automatic_caching_has_no_switch_to_turn_on():
    code, text = caching.advise(metrics(GPT55), CATALOG.lookup(GPT55))
    assert code == "automatic" and "nothing to enable" in text


def test_prompts_below_the_checkpoint_minimum_are_not_recommended():
    # Haiku 4.5 needs 4,096 tokens per checkpoint; 3,000-token prompts cannot qualify.
    haiku = "anthropic.claude-haiku-4-5-20251001-v1:0"
    m = metrics(haiku, requests=100_000, input_tokens=300_000_000)
    assert caching.advise(m, CATALOG.lookup(haiku))[0] == "below_minimum"


def test_low_volume_is_not_recommended():
    m = metrics(requests=1_000, input_tokens=5_000_000)
    assert caching.advise(m, CATALOG.lookup(SONNET))[0] == "low_volume"


def test_writes_without_reads_point_at_reuse_not_enablement():
    code, text = caching.advise(metrics(write=50_000), CATALOG.lookup(SONNET))
    assert code == "writes_without_reads"
    assert "prefix" in text and "Evaluate" not in text


def test_missing_metrics_on_a_documented_model_recommend_nothing():
    code, _ = caching.advise(metrics(missing=10), CATALOG.lookup(SONNET))
    assert code == "metrics_unavailable"


# --------------------------------------------------------------------------- #
# Findings block and report section
# --------------------------------------------------------------------------- #
def block(*models):
    return caching.evaluate_models(list(models), CATALOG)


def test_block_orders_actionable_rows_first_and_lists_unreviewed_models_once():
    b = block(metrics(NOVA, read=10, input_tokens=900_000_000),
              metrics(SONNET),
              metrics(OSS, input_tokens=950_000_000),
              metrics("meta.llama3-1-8b-instruct-v1:0"),
              metrics("amazon.titan-embed-text-v2:0", input_tokens=10))
    assert [r["modelId"] for r in b["models"]] == [SONNET, OSS, NOVA]
    assert b["unreviewed_models"] == ["meta.llama3-1-8b-instruct-v1:0",
                                      "amazon.titan-embed-text-v2:0"]
    assert b["policy_version"] == caching.POLICY_VERSION
    urls = [ref["url"] for ref in b["references"]]
    assert len(urls) == len(set(urls))
    assert urls[0] == CATALOG.guide_url
    assert any("gpt-oss-120b" in u for u in urls) and any("nova-lite" in u for u in urls)


def test_actions_come_only_from_evaluate_and_writes_rows():
    actions = caching.recommended_actions(block(metrics(SONNET), metrics(OSS)))
    assert len(actions) == 1 and actions[0]["topic"] == "prompt_caching"
    assert SONNET in actions[0]["detail"] and OSS not in actions[0]["detail"]
    assert "not guaranteed" in actions[0]["detail"]
    assert caching.recommended_actions(block(metrics(OSS), metrics(GPT55))) == []


def test_rendered_section_lists_each_doc_url_once_with_the_aws_doc_label():
    md = caching.render_markdown(block(metrics(SONNET), metrics(OSS), metrics("x.unknown-v1")))
    assert md.startswith("## Prompt caching\n")
    assert "not written by the AI agent" in md
    assert md.count(CATALOG.guide_url) == 1
    assert md.count("model-card-openai-gpt-oss-120b.html") == 1
    assert "\nAWS Doc:\n" in md
    assert "Not in the reviewed catalog, so no recommendation: `x.unknown-v1`." in md
    assert "| Model | Documented support | Observed in this window | Recommendation |" in md


# --------------------------------------------------------------------------- #
# Replacing the report writer's caching text
# --------------------------------------------------------------------------- #
LLM_REPORT = """## Executive summary
The fleet is healthy. Prompt caching on gpt-oss-120b would cut cost by 90%. Throttling is the top issue.

## Key findings
- Throttling: 6.2% on `us.anthropic.claude-sonnet-5` in us-east-1.
- Prompt caching: gpt-oss-120b is input-heavy, so enable caching.
  - Cache reads are 0%.
- Growth: account 111111111111 grew 54%.

## Prompt caching opportunities
Enable cachePoint on everything.

### Detail
More caching detail.

## Traffic flow diagram
```mermaid
flowchart LR
  client["Client"] --> cache1["cache layer"]
```
The diagram shows one path.

## Recommendations (priority-ordered)
1. **Request a quota increase.**
   Throttle rate 6.2%.
   - Next steps: file the request.
2. **Enable prompt caching on gpt-oss-120b.**
   Input-heavy traffic.
3. **Tune max_tokens.**
   Burndown overhead 120%.

## Priority matrix
| Priority | Action | Effort | Impact | Cost direction |
|---|---|---|---|---|
| Critical | Request a quota increase | Small | Large | neutral |
| Low | Enable prompt caching | Small | Medium | ↓ cost |
| Medium | Tune max_tokens | Small | Medium | neutral |

| Cache metric | Value |
|---|---|
| Cache read | 0 |
"""


def test_strip_removes_caching_text_and_keeps_everything_else():
    out = caching.strip_caching_content(LLM_REPORT)
    assert not caching._CACHING.search(out), out
    assert "The fleet is healthy. Throttling is the top issue." in out
    assert "- Throttling: 6.2%" in out and "- Growth: account 111111111111 grew 54%." in out
    assert "## Prompt caching opportunities" not in out and "More caching detail" not in out
    assert '--> cache1["cache layer"]' not in out     # diagrams cannot bypass the policy
    assert "1. **Request a quota increase.**" in out
    assert "2. **Tune max_tokens.**" in out            # renumbered after the removal
    assert "   - Next steps: file the request." in out
    assert "| Critical | Request a quota increase |" in out
    assert "| Medium | Tune max_tokens |" in out
    assert "| Cache metric | Value |" not in out       # a caching table goes whole
    assert "## Traffic flow diagram" in out and "## Priority matrix" in out
    assert caching.strip_caching_content(out) == out   # idempotent


def test_the_section_goes_before_the_recommendations():
    stripped = caching.strip_caching_content(LLM_REPORT)
    report = caching.insert_section(stripped, caching.render_markdown(block(metrics(SONNET))))
    assert report.index("## Prompt caching") < report.index("## Recommendations")
    assert report.index("## Traffic flow diagram") < report.index("## Prompt caching")
    assert report.count("## Prompt caching") == 1


def test_the_section_is_appended_when_the_report_has_no_recommendations():
    report = caching.insert_section("## Executive summary\nAll good.\n",
                                    caching.render_markdown(block(metrics(SONNET))))
    assert report.rstrip().endswith(CATALOG.guide_url + ")")
