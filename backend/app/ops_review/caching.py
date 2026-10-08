"""Prompt caching in Ops Review: reviewed model capabilities plus measured use.

Caching advice is built here, deterministically, and never by the model that
writes the rest of the report. Three rules keep it honest:

* Support comes only from prompt_caching_catalog.json, a reviewed list of exact
  model IDs. A model that is not listed, or whose documentation does not describe
  prompt caching, gets no enablement advice. Support is never inferred from a
  provider name, and open-weight models are not excluded as a class.
* Usage comes only from CloudWatch cache metrics. Positive cache reads mean
  caching is in use, however small their share. Zero reads do not prove caching
  is off or that prompts repeat. Missing metrics (bedrock-mantle publishes none)
  leave usage unknown. A cached share is computed only where the input-token
  counter excludes cached tokens and the denominator is valid.
* Input-heavy request shape alone never produces a caching recommendation. The
  strongest advice is to evaluate repeated prefixes; savings are not promised.

The catalog is loaded from disk, so a report needs no documentation lookup and
no extra model call. Update the file when AWS documentation changes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

# Bump POLICY_VERSION when classification or advice rules change, and
# RENDER_VERSION when the rendered section changes. Both are part of the report
# cache key, so a release never serves a report built under the old rules.
POLICY_VERSION = "2026-10-07.2"
RENDER_VERSION = "3"

CATALOG_PATH = Path(__file__).with_name("prompt_caching_catalog.json")

# Below this many prompt tokens in the window, a model gets no recommendation.
MIN_PROMPT_TOKENS_FOR_ADVICE = 10_000_000
MAX_ROWS = 15

# Cross-Region inference profile prefixes. Prompt caching works with
# cross-Region inference, so a prefixed ID has its base model's support.
GEO_PREFIXES = ("us-gov.", "us.", "eu.", "apac.", "global.", "jp.", "au.", "ca.", "amer.")


@dataclass(frozen=True)
class Capability:
    name: str
    model_ids: tuple[str, ...]
    support: str                       # "documented" | "not_documented"
    implicit: bool
    explicit: bool
    doc_url: str
    min_tokens_per_checkpoint: int | None = None
    min_tokens_display: str = ""        # as the documentation states it, e.g. "1K"
    max_checkpoints: int | None = None
    max_cached_tokens: int | None = None
    max_cached_tokens_display: str = ""
    checkpoint_fields: tuple[str, ...] = ()
    ttl: tuple[str, ...] = ()
    api_guidance: tuple[tuple[str, str], ...] = ()
    input_tokens_exclude_cache: bool = False
    release: str = "Generally Available"
    notes: str = ""


@dataclass(frozen=True)
class Catalog:
    version: str
    reviewed_on: str
    guide_url: str
    digest: str
    by_id: dict = field(default_factory=dict)

    def lookup(self, model_id: str | None) -> Capability | None:
        if not model_id:
            return None
        return self.by_id.get(model_id) or self.by_id.get(base_model_id(model_id))


def base_model_id(model_id: str) -> str:
    for prefix in GEO_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


_FIELDS = {"name", "family", "model_ids", "support", "implicit", "explicit", "doc_url",
           "min_tokens_per_checkpoint", "min_tokens_display", "max_checkpoints",
           "max_cached_tokens", "max_cached_tokens_display", "checkpoint_fields", "ttl",
           "api_guidance", "input_tokens_exclude_cache", "release", "notes"}


def parse_catalog(raw: dict) -> Catalog:
    """Validate and flatten the catalog. Raises ValueError on any defect, so a
    bad edit fails the test suite instead of producing quiet advice."""
    guide = raw.get("guide_url") or ""
    families = raw.get("families") or {}
    by_id: dict[str, Capability] = {}
    for entry in raw.get("models") or []:
        unknown = set(entry) - _FIELDS
        if unknown:
            raise ValueError(f"unknown catalog fields {sorted(unknown)} in {entry.get('name')}")
        merged = {**families.get(entry.get("family"), {}), **entry}
        if entry.get("family") not in families:
            raise ValueError(f"unknown family for {entry.get('name')}")
        support = merged.get("support")
        if support not in ("documented", "not_documented"):
            raise ValueError(f"{entry.get('name')}: support must be documented or not_documented")
        doc_url = merged.get("doc_url") or guide
        if not doc_url.startswith("https://docs.aws.amazon.com/"):
            raise ValueError(f"{entry.get('name')}: documentation must be a public AWS doc URL")
        if support == "documented" and merged.get("explicit") and not merged.get(
                "min_tokens_per_checkpoint"):
            raise ValueError(f"{entry.get('name')}: explicit support needs a checkpoint minimum")
        ids = tuple(merged.get("model_ids") or ())
        if not ids or any(not i or "*" in i or i != i.strip() for i in ids):
            raise ValueError(f"{entry.get('name')}: list exact model IDs only")
        cap = Capability(
            name=merged["name"], model_ids=ids, support=support,
            implicit=bool(merged.get("implicit")), explicit=bool(merged.get("explicit")),
            doc_url=doc_url,
            min_tokens_per_checkpoint=merged.get("min_tokens_per_checkpoint"),
            min_tokens_display=merged.get("min_tokens_display") or "",
            max_checkpoints=merged.get("max_checkpoints"),
            max_cached_tokens=merged.get("max_cached_tokens"),
            max_cached_tokens_display=merged.get("max_cached_tokens_display") or "",
            checkpoint_fields=tuple(merged.get("checkpoint_fields") or ()),
            ttl=tuple(merged.get("ttl") or ()),
            api_guidance=tuple((merged.get("api_guidance") or {}).items()),
            input_tokens_exclude_cache=bool(merged.get("input_tokens_exclude_cache")),
            release=merged.get("release") or "Generally Available",
            notes=merged.get("notes") or "")
        for model_id in ids:
            if model_id in by_id:
                raise ValueError(f"duplicate catalog model ID {model_id}")
            by_id[model_id] = cap
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:12]
    return Catalog(version=str(raw.get("catalog_version") or ""),
                   reviewed_on=str(raw.get("reviewed_on") or ""),
                   guide_url=guide, digest=digest, by_id=by_id)


@lru_cache(maxsize=1)
def load_catalog(path: str = str(CATALOG_PATH)) -> Catalog:
    return parse_catalog(json.loads(Path(path).read_text()))


# --------------------------------------------------------------------------- #
# Measured use
# --------------------------------------------------------------------------- #
@dataclass
class ModelCacheMetrics:
    """One model's window totals, split by endpoint.

    The runtime fields come only from bedrock-runtime rows, the only endpoint
    that publishes CloudWatch cache metrics. A row whose cache counters are NULL
    is a missing observation, never a zero.
    """
    model_id: str
    requests: int = 0
    accounts: int = 0
    runtime_requests: int = 0
    runtime_rows: int = 0
    rows_missing_cache: int = 0
    rows_missing_input: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    mantle_requests: int = 0
    invalid_rows: int = 0


def usage_status(m: ModelCacheMetrics) -> str:
    """in_use | writes_only | none_reported | unknown | unavailable."""
    if m.runtime_rows == 0:
        return "unavailable"
    if m.invalid_rows or min(m.cache_read_tokens, m.cache_write_tokens, m.input_tokens) < 0:
        return "unknown"
    if m.cache_read_tokens > 0:
        return "in_use"            # positive evidence, even from partial rows
    if m.rows_missing_cache >= m.runtime_rows:
        return "unavailable"
    if m.rows_missing_cache:
        return "unknown"           # a missing row may have held the reads
    if m.cache_write_tokens > 0:
        return "writes_only"
    return "none_reported"


def cached_share_pct(m: ModelCacheMetrics, cap: Capability | None) -> float | None:
    """Share of prompt tokens read from cache, or None when it cannot be stated.

    Valid only where the input counter excludes cached tokens (Converse-style
    accounting: total input = input + cache read + cache write), with every row
    observed and a positive denominator.
    """
    if cap is None or not cap.input_tokens_exclude_cache:
        return None
    if usage_status(m) not in ("in_use", "writes_only", "none_reported"):
        return None
    if m.rows_missing_cache or m.rows_missing_input:
        return None
    denominator = m.input_tokens + m.cache_read_tokens + m.cache_write_tokens
    if denominator <= 0:
        return None
    return round(100.0 * m.cache_read_tokens / denominator, 2)


def prompt_tokens(m: ModelCacheMetrics) -> int:
    return max(0, m.input_tokens) + max(0, m.cache_read_tokens) + max(0, m.cache_write_tokens)


# --------------------------------------------------------------------------- #
# Advice
# --------------------------------------------------------------------------- #
def _fmt_int(n: int | float) -> str:
    return f"{int(round(n)):,}"


def _ttl(cap: Capability) -> str:
    return " or ".join(cap.ttl) if cap.ttl else "the model's TTL"


def _minimum(cap: Capability) -> str:
    return cap.min_tokens_display or _fmt_int(cap.min_tokens_per_checkpoint or 0)


def _guidance(cap: Capability) -> str:
    """Per-API checkpoint syntax, with APIs that share a syntax grouped."""
    grouped: dict[str, list[str]] = {}
    for api, how in cap.api_guidance:
        grouped.setdefault(how, []).append(api)
    return "; ".join(f"{' and '.join(apis)}: {how}" for how, apis in grouped.items())


def support_text(cap: Capability | None) -> str:
    if cap is None:
        return "Not reviewed"
    if cap.support != "documented":
        return "Not documented"
    if cap.explicit:
        kinds = "implicit and explicit" if cap.implicit else "explicit"
        text = (f"Documented: {kinds}, checkpoint minimum {_minimum(cap)} tokens, "
                f"TTL {_ttl(cap)}")
        if cap.max_cached_tokens:
            cached = cap.max_cached_tokens_display or _fmt_int(cap.max_cached_tokens)
            text += f", up to {cached} cached tokens"
    else:
        text = "Documented: implicit (automatic)"
    if cap.release != "Generally Available":
        text += f" ({cap.release})"
    if cap.notes:
        text += f". {cap.notes}"
    return text


def usage_text(m: ModelCacheMetrics, status: str, share: float | None) -> str:
    if status == "unavailable":
        text = ("Unknown: bedrock-mantle publishes no cache metrics"
                if m.runtime_rows == 0
                else "Unknown: CloudWatch published no cache-token datapoints for this traffic")
    elif status == "unknown":
        text = "Unknown: cache-token datapoints exist for only part of this window"
    elif status == "in_use":
        text = f"In use: {_fmt_int(m.cache_read_tokens)} tokens read from cache"
        if share is not None:
            text += f" ({share:g}% of prompt tokens)"
    elif status == "writes_only":
        text = f"Cache writes only: {_fmt_int(m.cache_write_tokens)} tokens, no reads"
    else:
        text = "No cache reads or writes reported"
    if m.mantle_requests and m.runtime_rows:
        text += "; bedrock-mantle requests not covered"
    return text


def advise(m: ModelCacheMetrics, cap: Capability | None) -> tuple[str, str]:
    """(code, recommendation) for one model. Deterministic and documented."""
    status = usage_status(m)
    share = cached_share_pct(m, cap)
    if cap is None:
        return "unknown_model", "Not in the reviewed catalog, so no recommendation."
    if cap.support != "documented":
        return ("not_documented",
                "Bedrock documentation does not describe prompt caching for this model. "
                "No enablement recommendation.")
    if status in ("unavailable", "unknown"):
        return "metrics_unavailable", "Usage cannot be determined, so no recommendation."
    if status == "in_use":
        if share is not None and share < 10:
            return ("in_use",
                    "In use. Reads are a small share of prompt tokens; check that the cached "
                    f"prefix stays identical between requests and repeats within {_ttl(cap)}.")
        return "in_use", "In use. Nothing to enable."
    if status == "writes_only":
        return ("writes_without_reads",
                "Cache writes without reads: prefixes may change between requests or expire "
                f"before reuse ({_ttl(cap)}). Check prefix stability; cache writes can be "
                "billed above the standard input rate.")
    # none_reported
    if m.rows_missing_input:
        return "metrics_unavailable", "Prompt-token measurements are incomplete, so no recommendation."
    if not cap.explicit:
        return ("automatic",
                "Caching is automatic for this model; there is nothing to enable. Placing "
                "static content first and variable content last improves the chance of a "
                "cache hit.")
    avg_prompt = prompt_tokens(m) / m.runtime_requests if m.runtime_requests else 0
    minimum = cap.min_tokens_per_checkpoint or 0
    if avg_prompt < minimum:
        return ("below_minimum",
                f"Average prompt is {_fmt_int(avg_prompt)} tokens, below the "
                f"{_minimum(cap)}-token checkpoint minimum, so caching is unlikely to "
                "apply to typical requests. No recommendation.")
    if prompt_tokens(m) < MIN_PROMPT_TOKENS_FOR_ADVICE:
        return ("low_volume",
                f"Under {_fmt_int(MIN_PROMPT_TOKENS_FOR_ADVICE)} prompt tokens in this window. "
                "No recommendation.")
    fields = ", ".join(f"`{f}`" for f in cap.checkpoint_fields)
    where = f" (checkpoints go in {fields})" if fields else ""
    how = f" {_guidance(cap)}." if cap.api_guidance else ""
    return ("evaluate",
            f"Evaluate: if requests repeat a stable prefix of at least {_minimum(cap)} tokens"
            f"{where}, add a cache checkpoint after it.{how} Savings depend on how often that "
            f"prefix is reused within {_ttl(cap)} and are not guaranteed.")


def evaluate_models(metrics: list[ModelCacheMetrics], catalog: Catalog | None = None) -> dict:
    """The prompt_caching findings block: one row per model, plus references."""
    catalog = catalog or load_catalog()
    rows = []
    for m in metrics:
        if m.requests <= 0:
            continue
        cap = catalog.lookup(m.model_id)
        status = usage_status(m)
        share = cached_share_pct(m, cap)
        code, recommendation = advise(m, cap)
        rows.append({
            "modelId": m.model_id,
            "model_name": cap.name if cap else None,
            "support": cap.support if cap else "not_reviewed",
            "implicit": cap.implicit if cap else None,
            "explicit": cap.explicit if cap else None,
            "min_tokens_per_checkpoint": cap.min_tokens_per_checkpoint if cap else None,
            "ttl": list(cap.ttl) if cap else [],
            "support_text": support_text(cap),
            "usage": status,
            "usage_text": usage_text(m, status, share),
            "cached_share_pct": share,
            "requests": m.requests,
            "accounts": m.accounts,
            "prompt_tokens": prompt_tokens(m),
            "cache_read_tokens": m.cache_read_tokens if m.runtime_rows else None,
            "cache_write_tokens": m.cache_write_tokens if m.runtime_rows else None,
            "mantle_requests": m.mantle_requests,
            "advice": code,
            "recommendation": recommendation,
            "doc_url": cap.doc_url if cap else None,
        })
    # Models outside the catalog with no cache activity carry no information
    # beyond "not reviewed", so they are listed once instead of as table rows.
    unreviewed = sorted((r for r in rows if r["advice"] == "unknown_model"
                         and r["usage"] not in ("in_use", "writes_only")),
                        key=lambda r: (-r["prompt_tokens"], r["modelId"]))
    rows = [r for r in rows if r not in unreviewed]
    actionable = {"evaluate": 0, "writes_without_reads": 1}
    rows.sort(key=lambda r: (actionable.get(r["advice"], 2), -r["prompt_tokens"], r["modelId"]))
    rows = rows[:MAX_ROWS]
    return {
        "policy_version": POLICY_VERSION,
        "render_version": RENDER_VERSION,
        "catalog_version": catalog.version,
        "reviewed_on": catalog.reviewed_on,
        "models": rows,
        "unreviewed_models": [r["modelId"] for r in unreviewed],
        "references": references(rows, catalog),
    }


def references(rows: list[dict], catalog: Catalog) -> list[dict]:
    """Each documentation URL once: the guide first, then model-specific pages."""
    out = [{"label": "Prompt caching for faster model inference", "url": catalog.guide_url}]
    seen = {catalog.guide_url}
    for r in rows:
        url = r.get("doc_url")
        if url and url not in seen:
            seen.add(url)
            out.append({"label": r.get("model_name") or r["modelId"], "url": url})
    return out


def recommended_actions(block: dict) -> list[dict]:
    rows = block.get("models") or []
    evaluate = [r for r in rows if r["advice"] == "evaluate"]
    writes = [r for r in rows if r["advice"] == "writes_without_reads"]
    actions = []
    if evaluate:
        names = ", ".join(f"`{r['modelId']}`" for r in evaluate[:4])
        actions.append({
            "priority": "info", "topic": "prompt_caching",
            "title": "Evaluate prompt caching for repeated prompt prefixes",
            "detail": (f"{len(evaluate)} model(s) with documented prompt-caching support report "
                       f"no cache activity ({names}). Where requests share a stable prefix above "
                       "the model's checkpoint minimum, add cache checkpoints and compare cost "
                       "and latency. Savings depend on reuse and are not guaranteed."),
        })
    if writes:
        names = ", ".join(f"`{r['modelId']}`" for r in writes[:4])
        actions.append({
            "priority": "info", "topic": "prompt_caching",
            "title": "Cache writes without cache reads",
            "detail": (f"{names} wrote prompt cache entries that were never read in this window. "
                       "Check that the cached prefix is identical between requests and that "
                       "requests repeat within the TTL."),
        })
    return actions


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #
def _cell(text) -> str:
    return str(text if text is not None else "-").replace("|", "\\|").replace("\n", " ")


def render_markdown(block: dict) -> str:
    rows = block.get("models") or []
    lines = ["## Prompt caching", ""]
    lines.append(
        f"Built from AWS documentation reviewed on {block.get('reviewed_on')} and this "
        "window's CloudWatch cache metrics; not written by the AI agent. Prompt caching "
        "support does not guarantee cache hits.")
    lines.append("")
    unreviewed = block.get("unreviewed_models") or []
    if not rows and not unreviewed:
        lines.append("No model traffic with prompt tokens in this window.")
    elif rows:
        lines.append("| Model | Documented support | Observed in this window | Recommendation |")
        lines.append("|---|---|---|---|")
        for r in rows:
            model = f"`{r['modelId']}`"
            if r.get("model_name"):
                model += f" ({r['model_name']})"
            lines.append(f"| {_cell(model)} | {_cell(r['support_text'])} | "
                         f"{_cell(r['usage_text'])} | {_cell(r['recommendation'])} |")
    if unreviewed:
        lines.append("")
        lines.append("Not in the reviewed catalog, so no recommendation: "
                     + ", ".join(f"`{m}`" for m in unreviewed) + ".")
    lines.append("")
    lines.append("AWS Doc:")
    for ref in block.get("references") or []:
        lines.append(f"- [{ref['label']}]({ref['url']})")
    return "\n".join(lines) + "\n"


_CACHING = re.compile(
    r"prompt[ -]?cach|\bcach(?:e|ed|es|ing)\b|cachePoint|cache_control|"
    r"cache[ _-]?(?:read|write|hit|point)",
    re.IGNORECASE)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def strip_caching_content(md: str) -> str:
    """Remove the report writer's prompt-caching text before the deterministic
    section goes in: whole sections under a caching heading, list items and
    table rows whose own text is about caching (with their nested items), and
    caching sentences in paragraphs. Caching code blocks are removed too. Ordered
    lists are renumbered afterwards."""
    lines = md.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    skip_level = 0            # inside a dropped section of this heading level
    drop_indent = None        # inside a dropped list item at this indent
    drop_table = False        # inside a table whose header is about caching
    while i < n:
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            marker = fence.group(1)
            end = i + 1
            while end < n:
                close = re.fullmatch(r"\s*" + re.escape(marker[0]) +
                                     "{" + str(len(marker)) + r",}\s*", lines[end])
                end += 1
                if close:
                    break
            block = lines[i:end]
            indent = len(line) - len(line.lstrip())
            dropped_item = drop_indent is not None and indent > drop_indent
            if not skip_level and not dropped_item and not _CACHING.search("\n".join(block)):
                out.extend(block)
            i = end
            continue
        # Markdown also allows a title followed by === or ---.
        if (line.strip() and i + 1 < n
                and re.fullmatch(r"\s{0,3}(={3,}|-{3,})\s*", lines[i + 1])
                and not _ITEM.match(line)):
            line = ("# " if lines[i + 1].lstrip().startswith("=") else "## ") + line.strip()
            i += 1
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if skip_level and level > skip_level:
                i += 1
                continue
            skip_level = 0
            drop_indent = None
            if _CACHING.search(heading.group(2)):
                skip_level = level
                i += 1
                continue
            out.append(line)
            i += 1
            continue
        if skip_level:
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if drop_indent is not None:
            if not line.strip():
                out.append(line)      # keep paragraph breaks; collapsed later
                i += 1
                continue
            if indent > drop_indent:
                i += 1
                continue
            drop_indent = None
        if drop_table:
            if line.lstrip().startswith("|"):
                i += 1
                continue
            drop_table = False
        item = _ITEM.match(line)
        if item:
            if _CACHING.search(_item_own_text(lines, i)):
                drop_indent = len(item.group(1))
                i += 1
                continue
            out.append(line)
            i += 1
            continue
        if line.lstrip().startswith("|"):
            if _CACHING.search(line):
                drop_table = _is_table_header(lines, i)   # a caching table goes whole
                i += 1
                continue
            out.append(line)
            i += 1
            continue
        if line.strip():
            # A wrapped sentence is one paragraph. Filtering each line alone
            # left "saves 90%" behind when "prompt caching" was on the prior line.
            paragraph = [line]
            end = i + 1
            while end < n and lines[end].strip():
                if (_HEADING.match(lines[end]) or _ITEM.match(lines[end])
                        or _FENCE.match(lines[end]) or lines[end].lstrip().startswith("|")
                        or re.fullmatch(r"\s{0,3}(={3,}|-{3,})\s*", lines[end])):
                    break
                paragraph.append(lines[end])
                end += 1
            text = " ".join(s.strip() for s in paragraph)
            if not _CACHING.search(text):
                out.extend(paragraph)
                i = end
                continue
            kept = [s for s in re.split(r"(?<=[.!?])\s+", text) if not _CACHING.search(s)]
            if kept:
                out.append(" " * indent + " ".join(kept))
            i = end
            continue
        out.append(line)
        i += 1
    return _collapse_blank_lines(_renumber(out))


def _item_own_text(lines: list[str], start: int) -> str:
    """The item's first line plus continuation lines, excluding nested items."""
    first = _ITEM.match(lines[start])
    indent = len(first.group(1))
    text = [lines[start]]
    for line in lines[start + 1:]:
        if not line.strip() or _ITEM.match(line) or _HEADING.match(line) or _FENCE.match(line):
            break
        if len(line) - len(line.lstrip()) <= indent:
            break
        text.append(line)
    return " ".join(text)


def _is_table_header(lines: list[str], i: int) -> bool:
    nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
    return bool(re.match(r"^\|?\s*:?-{3,}", nxt))


def _renumber(lines: list[str]) -> list[str]:
    counters: dict[int, int] = {}
    out = []
    fence = None
    for line in lines:
        marker = _FENCE.match(line)
        if fence:
            out.append(line)
            if re.fullmatch(r"\s*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                fence = None
            continue
        if marker:
            fence = marker.group(1)
            out.append(line)
            continue
        if _HEADING.match(line):
            counters.clear()
        m = re.match(r"^(\s*)(\d+)([.)])(\s+)", line)
        if m:
            indent = len(m.group(1))
            for deeper in [k for k in counters if k > indent]:
                del counters[deeper]
            counters[indent] = counters.get(indent, 0) + 1
            line = f"{m.group(1)}{counters[indent]}{m.group(3)}{m.group(4)}{line[m.end():]}"
        elif line.strip() and not _ITEM.match(line):
            indent = len(line) - len(line.lstrip())
            for k in [k for k in counters if k >= indent]:
                del counters[k]
        out.append(line)
    return out


def _collapse_blank_lines(lines: list[str]) -> str:
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def insert_section(md: str, section: str) -> str:
    """Place the caching section before the recommendations (or the priority
    matrix), or at the end when the report has neither."""
    lines = md.rstrip("\n").split("\n")
    for pattern in (r"^##\s+Recommendations", r"^##\s+Priority matrix"):
        fence = None
        for idx, line in enumerate(lines):
            marker = _FENCE.match(line)
            if fence:
                if re.fullmatch(r"\s*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                    fence = None
                continue
            if marker:
                fence = marker.group(1)
                continue
            if re.match(pattern, line, re.IGNORECASE):
                head = "\n".join(lines[:idx]).rstrip("\n")
                tail = "\n".join(lines[idx:])
                return f"{head}\n\n{section.rstrip()}\n\n{tail}\n"
    return f"{md.rstrip()}\n\n{section.rstrip()}\n"
