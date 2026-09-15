"""Small CLI adapter for the reusable capability layer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_data_platform import (
    build_vector_index,
    discover_datasets,
    discover_datasets_hybrid,
    load_registry,
)
from ai_data_platform.embeddings import HashingEmbeddingProvider
from ai_data_platform.evaluation import evaluate_retrieval, load_evaluation_cases


def _default_registry_path() -> Path:
    return Path(__file__).resolve().parents[2] / "registry"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and discover semantic dataset contracts.")
    parser.add_argument("--registry", type=Path, default=_default_registry_path())
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover_parser = subparsers.add_parser("discover", help="Discover ranked datasets.")
    discover_parser.add_argument("question")
    discover_parser.add_argument("--limit", type=int, default=5)
    discover_parser.add_argument(
        "--mode", choices=("deterministic", "hybrid"), default="deterministic"
    )
    discover_parser.add_argument("--min-similarity", type=float, default=0.25)
    evaluate_parser = subparsers.add_parser(
        "evaluate", help="Compare deterministic and hybrid retrieval."
    )
    evaluate_parser.add_argument(
        "--cases", type=Path, default=Path(__file__).resolve().parents[2] / "evaluation/cases.yaml"
    )
    evaluate_parser.add_argument("--limit", type=int, default=5)
    evaluate_parser.add_argument("--min-similarity", type=float, default=0.25)
    subparsers.add_parser("validate", help="Validate registry metadata and references.")
    args = parser.parse_args()

    registry = load_registry(args.registry)
    if args.command == "validate":
        print("Registry is valid.")
        return 0
    if args.command == "evaluate":
        index = build_vector_index(registry, HashingEmbeddingProvider())
        report = evaluate_retrieval(
            registry,
            load_evaluation_cases(args.cases),
            index,
            k=args.limit,
            min_similarity=args.min_similarity,
        )
        print(json.dumps(report.model_dump(mode="json"), indent=2))
        return 0
    if args.mode == "hybrid":
        index = build_vector_index(registry, HashingEmbeddingProvider())
        result = discover_datasets_hybrid(
            args.question,
            registry,
            index,
            limit=args.limit,
            min_similarity=args.min_similarity,
        )
    else:
        result = discover_datasets(args.question, registry, args.limit)
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
