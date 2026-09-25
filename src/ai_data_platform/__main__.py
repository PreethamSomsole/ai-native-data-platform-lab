"""Small CLI adapter for the reusable capability layer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import uvicorn

from ai_data_platform import (
    build_vector_index,
    discover_datasets,
    discover_datasets_hybrid,
    load_registry,
)
from ai_data_platform.embeddings import HashingEmbeddingProvider
from ai_data_platform.evaluation import evaluate_retrieval, load_evaluation_cases
from ai_data_platform.engineering.crawler import SchemaCrawler, validate_crawler_drafts


def _default_registry_path() -> Path:
    return Path(__file__).resolve().parents[2] / "registry"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and discover semantic dataset contracts."
    )
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
    serve_parser = subparsers.add_parser("serve", help="Run the local context REST service.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument(
        "--state-dir", type=Path, default=Path("var"), help="SQLite and DuckDB directory."
    )
    mcp_parser = subparsers.add_parser(
        "mcp", help="Run the read-only Model Context Protocol server over stdio."
    )
    mcp_parser.add_argument(
        "--state-dir", type=Path, default=Path("var"), help="SQLite and DuckDB directory."
    )
    subparsers.add_parser("validate", help="Validate registry metadata and references.")
    crawl_parser = subparsers.add_parser(
        "crawl", help="Crawl a DuckDB schema into an editable review area."
    )
    crawl_parser.add_argument("--source", type=Path, required=True, help="DuckDB database file.")
    crawl_parser.add_argument("--schema", required=True, help="DuckDB schema to crawl.")
    crawl_parser.add_argument(
        "--registry",
        dest="crawl_registry",
        type=Path,
        required=True,
        help="Existing semantic registry used for comparison.",
    )
    crawl_parser.add_argument(
        "--output", type=Path, required=True, help="New or empty crawler review directory."
    )
    crawl_parser.add_argument(
        "--profile-rows",
        type=int,
        default=10_000,
        help="Maximum rows profiled per table (default: 10000).",
    )
    crawl_parser.add_argument(
        "--sample-seed",
        type=int,
        default=42,
        help="Repeatable reservoir-sampling seed for tables above the profile limit.",
    )
    draft_validate_parser = subparsers.add_parser(
        "validate-drafts",
        help="Validate edited crawler contracts and semantic references without promotion.",
    )
    draft_validate_parser.add_argument(
        "--drafts", type=Path, required=True, help="Crawler review directory containing datasets/."
    )
    draft_validate_parser.add_argument(
        "--registry",
        dest="draft_registry",
        type=Path,
        required=True,
        help="Canonical registry supplying semantic references.",
    )
    args = parser.parse_args()

    if args.command == "crawl":
        source = args.source.resolve()
        if not source.is_file():
            parser.error(f"DuckDB source does not exist or is not a file: {source}")
        output = args.output.resolve()
        if output.exists() and any(output.iterdir()):
            parser.error(f"crawler output must be a new or empty directory: {output}")
        with duckdb.connect(str(source), read_only=True) as connection:
            crawler = SchemaCrawler(
                connection,
                max_profile_rows=args.profile_rows,
                profile_seed=args.sample_seed,
            )
            files = crawler.generate_registry(
                output,
                schema_name=args.schema,
                existing_registry_directory=args.crawl_registry,
            )
        print(f"Crawled {source} schema {args.schema} into review area {output}.")
        print(f"Generated {len(files)} contract drafts; review {output / 'crawl_report.yaml'}.")
        return 0
    if args.command == "validate-drafts":
        dataset_ids = validate_crawler_drafts(args.drafts, args.draft_registry)
        print(f"Validated {len(dataset_ids)} crawler dataset contracts and semantic references.")
        for dataset_id in dataset_ids:
            print(f"- {dataset_id}")
        print("No contracts were promoted.")
        return 0

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
    if args.command == "serve":
        from ai_data_platform.context import ContextService
        from ai_data_platform.http import create_app, create_reasoning_service_from_env
        from ai_data_platform.runtime import (
            DuckDBRuntimeHistoryStore,
            RuntimeMetadataRepository,
            SQLiteRuntimeMetadataStore,
        )

        args.state_dir.mkdir(parents=True, exist_ok=True)
        repository = RuntimeMetadataRepository(
            SQLiteRuntimeMetadataStore(args.state_dir / "runtime-metadata.sqlite"),
            DuckDBRuntimeHistoryStore(args.state_dir / "runtime-history.duckdb"),
        )
        index = build_vector_index(registry, HashingEmbeddingProvider())
        context_service = ContextService(registry, repository, index)
        app = create_app(
            context_service,
            create_reasoning_service_from_env(context_service),
        )
        uvicorn.run(app, host=args.host, port=args.port)
        return 0
    if args.command == "mcp":
        from ai_data_platform.mcp import create_mcp_server_from_paths

        server = create_mcp_server_from_paths(args.registry, args.state_dir)
        server.run(transport="stdio")
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
