import re
import unicodedata
from datetime import timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

from ..contracts import SearchRequest, SearchResult, utcnow


class AcceptancePolicy(BaseModel):
    min_engines: int = Field(default=2, ge=1)
    min_results: int = Field(default=1, ge=1)
    min_relevance: float = Field(default=0.1, ge=0, le=1)
    min_query_coverage: float = Field(default=0.5, ge=0, le=1)
    engine_aliases: dict[str, str] = Field(default_factory=lambda: {"google news": "google"})
    query_aliases: dict[str, list[str]] = Field(default_factory=dict)


class Decision(BaseModel):
    accepted: bool
    reason: str
    results: list[SearchResult]
    engines: int
    relevance: float
    warnings: list[str]


def canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("invalid result URL")
    if parsed.username or parsed.password or any(ord(char) < 33 for char in value):
        raise ValueError("unsafe result URL")
    parsed.port
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
    )


def tokens(value: str) -> set[str]:
    value = unicodedata.normalize("NFKC", value).casefold()
    words = set(re.findall(r"[^\W_]+", value, re.UNICODE))
    for run in re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+", value):
        words.update(run[index : index + 2] for index in range(len(run) - 1))
    return words


def query_sites(query: str) -> tuple[str, list[tuple[bool, str, str]]]:
    sites = []

    def extract(match):
        value = match.group(2) or match.group(3)
        parsed = urlsplit("https://" + value)
        if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
            raise ValueError("unsupported site constraint")
        hostname = SearchRequest.domain_names([parsed.hostname or ""])[0]
        sites.append((bool(match.group(1)), hostname, parsed.path))
        return " "

    text = re.sub(r'(?<![\w-])(-?)site:(?:"([^"]+)"|([^\s()]+))', extract, query)
    if re.search(r"(?<![\w-])-?site:", text):
        raise ValueError("unsupported site constraint")
    return text, sites


def query_terms(query: str, policy: AcceptancePolicy) -> tuple[list[set[str]], set[str]]:
    ignored = tokens("a an and are as at be by for from how in is it latest of on or the to with")
    words = tokens(query) - ignored
    generic = tokens(
        "annual financial results report reports company companies earnings revenue profit profits"
    )
    anchors = {word for word in words - generic if not word.isdecimal()}
    groups = [{word} | tokens(" ".join(policy.query_aliases.get(word, []))) for word in words]
    return groups, anchors


def evaluate(
    provider: str,
    candidates: list[SearchResult],
    request: SearchRequest,
    policy: AcceptancePolicy,
    upstream_count: int | None = None,
) -> Decision:
    warnings = []
    results: dict[str, SearchResult] = {}
    try:
        query, sites = query_sites(request.query)
    except (ValueError, UnicodeError):
        return Decision(
            accepted=False,
            reason="unsupported_query_constraint",
            results=[],
            engines=0,
            relevance=0,
            warnings=["unsupported_site_constraint"],
        )
    groups, anchors = query_terms(query, policy)
    anchor_aliases = anchors | tokens(
        " ".join(alias for word in anchors for alias in policy.query_aliases.get(word, []))
    )
    relevant_count = 0
    eligible_count = 0
    now = utcnow()
    start = request.start_date
    end = request.end_date
    if request.time_range:
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[request.time_range]
        earliest = now - timedelta(days=days)
    else:
        earliest = None
    for candidate in candidates:
        try:
            url = canonical_url(candidate.url)
        except ValueError:
            warnings.append("invalid_result_url")
            continue
        if not candidate.title.strip() or not candidate.snippet.strip():
            warnings.append("incomplete_result")
            continue
        hostname = urlsplit(url).hostname or ""

        def matches(domain: str) -> bool:
            return hostname == domain or hostname.endswith("." + domain)

        if request.include_domains and not any(
            matches(domain) for domain in request.include_domains
        ):
            continue
        if any(matches(domain) for domain in request.exclude_domains):
            continue
        site_matches = [
            (excluded, matches(domain) and urlsplit(url).path.startswith(path))
            for excluded, domain, path in sites
        ]
        if any(excluded and matched for excluded, matched in site_matches) or (
            any(not excluded for excluded, matched in site_matches)
            and not any(not excluded and matched for excluded, matched in site_matches)
        ):
            warnings.append("query_site_excluded")
            continue
        publication = candidate.published_at
        if start or end or earliest:
            if publication is None:
                warnings.append("unknown_date_excluded")
                continue
            if publication.tzinfo is None:
                warnings.append("ambiguous_date_excluded")
                continue
            publication = publication.astimezone(timezone.utc)
            if (start and publication.date() < start) or (end and publication.date() > end):
                continue
            if earliest and not earliest <= publication <= now:
                continue
        eligible_count += 1
        content = tokens(candidate.title + " " + candidate.snippet)
        coverage = (
            sum(bool(group & content) for group in groups) / len(groups)
            if groups
            else float(bool(sites))
        )
        relevant = coverage >= policy.min_query_coverage and (
            not anchors or bool(anchor_aliases & content)
        )
        if relevant:
            relevant_count += 1
        elif policy.min_relevance > 0:
            warnings.append("irrelevant_result_excluded")
            continue
        engines = set()
        for engine in candidate.engines:
            name = " ".join(engine.casefold().split())
            name = policy.engine_aliases.get(name, name)
            if name and name not in {"unknown", "none", "n/a"}:
                engines.add(name)
        if url in results:
            results[url].engines = sorted(set(results[url].engines) | engines)
        else:
            results[url] = candidate.model_copy(
                update={"url": url, "engines": sorted(engines), "provider": provider}
            )
    valid = list(results.values())
    contributors = len({engine for result in valid for engine in result.engines})
    relevance = relevant_count / eligible_count if eligible_count else 0
    reason = "accepted"
    if not valid and eligible_count and policy.min_relevance > 0:
        reason = "low_quality"
    elif not valid:
        reason = (
            "empty"
            if (upstream_count if upstream_count is not None else len(candidates)) == 0
            else "no_valid_results"
        )
    elif relevance < policy.min_relevance:
        reason = "low_quality"
    elif len(valid) < policy.min_results:
        reason = "insufficient_results"
    elif provider == "searxng" and contributors < policy.min_engines:
        reason = "insufficient_engine_quorum"
    return Decision(
        accepted=reason == "accepted",
        reason=reason,
        results=valid,
        engines=contributors,
        relevance=relevance,
        warnings=sorted(set(warnings)),
    )
