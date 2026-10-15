"""CLI adapter for sample data and governed pipelines."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_data_platform.registry.loader import load_registry

from .factory import PROJECT_ROOT, PlatformPaths, open_pipeline_environment
from .loader import load_pipeline_project, validate_pipeline_project
from .models import PipelineRunResult


def _paths(args: argparse.Namespace) -> PlatformPaths:
    return PlatformPaths(
        registry=args.registry,
        pipelines=args.pipelines,
        state=args.state_dir,
        data=args.data_dir,
    )


def add_commands(subparsers: argparse._SubParsersAction) -> None:
    sample = subparsers.add_parser("sample-data", help="Generate or download sample datasets.")
    sample_commands = sample.add_subparsers(dest="sample_command", required=True)
    tpch = sample_commands.add_parser("tpch", help="Generate TPC-H Parquet (scale 1 = ~6M order lines).")
    tpch.add_argument("--scale", type=float, default=1.0)
    taxi = sample_commands.add_parser("nyc-taxi", help="Download NYC TLC yellow-taxi trip Parquet.")
    taxi.add_argument("--months", nargs="+", default=["2024-01"], help="YYYY-MM months.")
    for parser in (tpch, taxi):
        parser.add_argument("--data-dir", type=Path, default=Path("var/data"))
        parser.add_argument("--force", action="store_true", help="Replace existing data.")

    pipelines = subparsers.add_parser("pipelines", help="Validate, run, and inspect governed pipelines.")
    pipelines.add_argument("--pipelines", type=Path, default=PROJECT_ROOT / "pipelines")
    pipelines.add_argument("--state-dir", type=Path, default=Path("var"))
    pipelines.add_argument("--data-dir", type=Path, default=Path("var/data"))
    pipelines.add_argument("--memory-limit", default=None, help="DuckDB memory limit, e.g. 2GB.")
    commands = pipelines.add_subparsers(dest="pipeline_command", required=True)
    commands.add_parser("list", help="List pipelines in dependency order.")
    commands.add_parser("validate", help="Validate specs against the registry.")
    ingest = commands.add_parser("ingest", help="Land source files as raw tables.")
    ingest.add_argument("--source", action="append", dest="sources")
    run = commands.add_parser("run", help="Run pipelines in dependency order.")
    run.add_argument("--select", action="append", help="Pipeline id (repeatable); includes downstream.")
    run.add_argument("--ingest", action="store_true", help="Land sources before running.")
    run.add_argument("--json", action="store_true")
    history = commands.add_parser("history", help="Show recent runs.")
    history.add_argument("--limit", type=int, default=20)
    commands.add_parser("lineage", help="Show lineage verified by successful runs.")
    export = commands.add_parser("export-dbt", help="Compile specs into a runnable dbt-duckdb project.")
    export.add_argument("--output", type=Path, default=Path("var/dbt_project"))


def print_run_results(results: list[PipelineRunResult]) -> None:
    print(f"{'pipeline':42} {'status':15} {'rows':>10} {'checks':>7} {'seconds':>8}")
    for result in results:
        rows = "-" if result.row_count is None else f"{result.row_count:,}"
        checks = f"{result.checks_passed}/{len(result.checks)}" if result.checks else "-"
        print(f"{result.pipeline_id:42} {result.status.value:15} {rows:>10} {checks:>7} {result.duration_seconds:>8.2f}")
        for check in result.checks:
            if not check.passed:
                print(f"    FAILED {check.name}: {check.detail}")
        if result.status.value != "succeeded" and result.message:
            print(f"    {result.message}")


def handle(args: argparse.Namespace) -> int | None:
    if args.command == "sample-data":
        from ai_data_platform.sample_data import download_nyc_taxi, generate_tpch

        if args.sample_command == "tpch":
            manifest = generate_tpch(args.data_dir, args.scale, force=args.force)
        else:
            manifest = download_nyc_taxi(args.data_dir, args.months, force=args.force)
        print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0
    if args.command != "pipelines":
        return None
    if args.pipeline_command in {"list", "validate"}:
        registry = load_registry(args.registry)
        project = load_pipeline_project(args.pipelines)
        validate_pipeline_project(project, registry)
        if args.pipeline_command == "validate":
            print(f"{len(project.pipelines)} pipelines and {len(project.sources)} sources are valid.")
            return 0
        for spec in project.execution_order():
            inputs = ", ".join(spec.inputs.sources + spec.inputs.datasets)
            print(f"{spec.id:42} -> {spec.output:34} <- {inputs}")
        return 0
    if args.pipeline_command == "export-dbt":
        from .dbt import export_dbt_project

        registry = load_registry(args.registry)
        project = load_pipeline_project(args.pipelines)
        validate_pipeline_project(project, registry)
        warehouse = (args.state_dir / "engineering.duckdb").resolve()
        files = export_dbt_project(project, registry, args.output, warehouse_path=warehouse)
        print(f"Wrote {len(files)} files to {args.output}; run: dbt build --project-dir {args.output} --profiles-dir {args.output}")
        return 0
    with open_pipeline_environment(_paths(args), memory_limit=args.memory_limit) as environment:
        runner = environment.runner
        if args.pipeline_command == "ingest" or (args.pipeline_command == "run" and args.ingest):
            for item in runner.ingest(getattr(args, "sources", None)):
                print(f"landed {item.source_id:16} -> {item.raw_table:24} {item.row_count:>12,} rows {item.duration_seconds:>6.2f}s")
        if args.pipeline_command == "run":
            results = runner.run(args.select)
            if args.json:
                print(json.dumps([result.model_dump(mode="json") for result in results], indent=2))
            else:
                print_run_results(results)
            return 0 if all(result.status.value == "succeeded" for result in results) else 1
        if args.pipeline_command == "history":
            for row in runner.run_history(args.limit):
                print(f"{row['finished_at']:%Y-%m-%d %H:%M:%S} {row['pipeline_id']:42} {row['status']:15} rows={row['row_count']}")
        if args.pipeline_command == "lineage":
            for row in runner.verified_lineage():
                print(f"{row['input_kind']:7} {row['input_id']:28} -> {row['output']:34} (run {row['verified_run_id']})")
    return 0
