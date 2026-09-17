"""Centralized, deterministic business and runtime ranking rules."""

RANKING_WEIGHTS = {
    "keyword_match_per_term": 2,
    "keyword_match_cap": 16,
    "metric_match_multiplier": 3,
    "metric_match_cap": 30,
    "domain_match": 12,
    "use_case_match_per_term": 2,
    "use_case_match_cap": 8,
    "audience_match_per_term": 1,
    "audience_match_cap": 4,
    "freshness_match_per_term": 2,
    "freshness_match_cap": 6,
    "certified": 8,
    "gold_layer": 5,
    "ambiguity_similarity_ratio": 0.70,
}


HYBRID_RANKING_WEIGHTS = {
    # A smaller constant than the web-search default keeps rank differences visible
    # in this intentionally small registry while retaining RRF's scale independence.
    "rrf_k": 10,
    "rrf_scale": 1_000,
    "deterministic_rrf_weight": 2,
    "vector_rrf_weight": 1,
}


RUNTIME_RANKING_WEIGHTS = {
    "freshness": {"fresh": 8, "stale": -12, "unknown": 0},
    "quality": {"passing": 10, "warning": -5, "failing": -20, "unknown": 0},
    "health": {"healthy": 8, "degraded": -8, "failed": -20, "unknown": 0},
    "usage_low": 1,
    "usage_medium": 3,
    "usage_high": 5,
}
