from datetime import datetime, timezone

import pytest

from search_router.contracts import SearchRequest, SearchResult
from search_router.search.policy import AcceptancePolicy, evaluate


def result(url="https://example.org/a", engines=None, **kwargs):
    return SearchResult(
        url=url,
        title="Market results",
        snippet="Market revenue growth",
        provider="searxng",
        engines=engines or [],
        **kwargs,
    )


def test_quorum_before_truncation_and_duplicate_attribution():
    decision = evaluate(
        "searxng",
        [result(engines=["Google News"]), result(engines=["bing"])],
        SearchRequest(query="market", limit=1),
        AcceptancePolicy(),
    )
    assert decision.accepted
    assert len(decision.results) == 1
    assert decision.results[0].engines == ["bing", "google"]


def test_unknown_attribution_and_aliases_do_not_fake_quorum():
    for engines in [["unknown", ""], ["google", "Google News"]]:
        decision = evaluate(
            "searxng", [result(engines=engines)], SearchRequest(query="market"), AcceptancePolicy()
        )
        assert decision.reason == "insufficient_engine_quorum"


def test_filters_apply_before_quorum():
    decision = evaluate(
        "searxng",
        [result(engines=["google"]), result("https://other.org/a", ["bing"])],
        SearchRequest(query="market", include_domains=["example.org"]),
        AcceptancePolicy(),
    )
    assert decision.engines == 1
    assert not decision.accepted


def test_dates_unknown_are_excluded():
    decision = evaluate(
        "tavily",
        [
            result(),
            result("https://example.org/b", published_at=datetime(2025, 1, 2, tzinfo=timezone.utc)),
        ],
        SearchRequest(query="market", start_date="2025-01-01", end_date="2025-01-03"),
        AcceptancePolicy(),
    )
    assert decision.accepted
    assert len(decision.results) == 1
    assert "unknown_date_excluded" in decision.warnings


def test_empty_invalid_and_irrelevant_are_distinct():
    request = SearchRequest(query="volcano")
    policy = AcceptancePolicy()
    assert evaluate("exa", [], request, policy).reason == "empty"
    assert (
        evaluate("exa", [result("javascript:alert(1)")], request, policy).reason
        == "no_valid_results"
    )
    assert evaluate("exa", [result()], request, policy).reason == "low_quality"


def test_multilingual_relevance():
    candidate = SearchResult(
        url="https://example.org", title="台積電營收", snippet="季度成長", provider="exa"
    )
    assert evaluate(
        "exa", [candidate], SearchRequest(query="台積電 最新"), AcceptancePolicy()
    ).accepted


def test_explicit_date_filter_uses_utc_day():
    candidate = result(published_at=datetime.fromisoformat("2025-01-02T01:00:00+08:00"))
    decision = evaluate(
        "exa",
        [candidate],
        SearchRequest(query="market", start_date="2025-01-02"),
        AcceptancePolicy(),
    )
    assert not decision.results


def test_financial_boilerplate_is_not_entity_relevance():
    candidate = SearchResult(
        url="https://example.org/africa",
        title="Top 10 Most Profitable Companies in Africa",
        snippet="Annual financial results and revenue growth for leading companies.",
        provider="tavily",
    )
    decision = evaluate(
        "tavily", [candidate], SearchRequest(query="Tencent annual results"), AcceptancePolicy()
    )
    assert not decision.accepted
    assert not decision.results
    assert decision.reason == "low_quality"


def test_site_operator_is_enforced_before_acceptance():
    candidate = SearchResult(
        url="https://en.wikipedia.org/wiki/Tencent",
        title="Tencent annual results",
        snippet="Tencent annual results financial statements",
        provider="serpapi",
    )
    decision = evaluate(
        "serpapi",
        [candidate],
        SearchRequest(query="Tencent annual results site:hkexnews.hk"),
        AcceptancePolicy(),
    )
    assert not decision.accepted
    assert not decision.results


def test_generic_entity_page_does_not_match_detailed_query():
    candidate = SearchResult(
        url="https://www.tencent.com/",
        title="Tencent",
        snippet="Tencent is an internet company.",
        provider="serpapi",
    )
    decision = evaluate(
        "serpapi",
        [candidate],
        SearchRequest(query="Tencent deferred tax reconciliation segment liabilities"),
        AcceptancePolicy(),
    )
    assert not decision.accepted
    assert not decision.results


@pytest.mark.parametrize(
    "query",
    [
        "Tencent site:hkexnews.hk",
        'Tencent site:"hkexnews.hk"',
        "Tencent -site:wikipedia.org",
        "site:hkexnews.hk",
    ],
)
def test_site_operators_keep_only_matching_hosts(query):
    good = SearchResult(
        url="https://www.hkexnews.hk/results.pdf",
        title="Tencent annual results",
        snippet="Tencent revenue",
        provider="serpapi",
    )
    bad = good.model_copy(update={"url": "https://en.wikipedia.org/wiki/Tencent"})
    decision = evaluate("serpapi", [good, bad], SearchRequest(query=query), AcceptancePolicy())
    assert decision.accepted
    assert decision.results == [good]


@pytest.mark.parametrize("site", ["site:", "site:[", "-site:example.org?query=private"])
def test_malformed_site_constraints_fail_closed(site):
    decision = evaluate(
        "serpapi", [result()], SearchRequest(query="market " + site), AcceptancePolicy()
    )
    assert not decision.accepted
    assert not decision.results
    assert decision.reason == "unsupported_query_constraint"


def test_aliases_are_alternatives_not_extra_required_terms():
    candidate = SearchResult(
        url="https://example.org/tencent",
        title="Tencent annual results",
        snippet="Tencent revenue",
        provider="exa",
    )
    assert evaluate(
        "exa",
        [candidate],
        SearchRequest(query="0700 annual results"),
        AcceptancePolicy(query_aliases={"0700": ["Tencent"]}),
    ).accepted


def test_irrelevant_results_cannot_contribute_engine_quorum():
    good = SearchResult(
        url="https://example.org/tencent",
        title="Tencent annual results",
        snippet="Tencent revenue",
        provider="searxng",
        engines=["google"],
    )
    bad = SearchResult(
        url="https://example.org/africa",
        title="Companies in Africa",
        snippet="Annual financial results",
        provider="searxng",
        engines=["bing"],
    )
    decision = evaluate(
        "searxng", [good, bad], SearchRequest(query="Tencent annual results"), AcceptancePolicy()
    )
    assert decision.reason == "insufficient_engine_quorum"
    assert decision.engines == 1
    assert decision.results == [good]
