"""Live, end-to-end showcase of the governed AI-native data platform.

Run from the repository root:

    python demo/showcase.py --pause            # presenter mode: Enter advances each step
    python demo/showcase.py --only 6           # jump to one step (earlier state must exist)
    python demo/showcase.py --scale 10         # ~60M order lines instead of ~6M

Every number printed is computed live from generated TPC-H data with DuckDB.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import duckdb

from ai_data_platform import build_vector_index
from ai_data_platform.context import ContextService, DiscoveryMode
from ai_data_platform.embeddings import HashingEmbeddingProvider
from ai_data_platform.mcp.server import create_pipeline_authoring_service
from ai_data_platform.pipelines.cli import print_run_results
from ai_data_platform.pipelines.dbt import export_dbt_project
from ai_data_platform.pipelines.factory import (
    PROJECT_ROOT,
    PlatformPaths,
    open_pipeline_environment,
)
from ai_data_platform.sample_data import SampleDataError, generate_tpch

DEMO = PROJECT_ROOT / "demo"
BAD_BATCH = "lineitem_late_batch.parquet"


class Presenter:
    def __init__(self, pause: bool) -> None:
        self.pause = pause

    def step(self, number: int, title: str, talking_point: str) -> None:
        print("\n" + "=" * 100)
        print(f" STEP {number}: {title}")
        print("=" * 100)
        print(textwrap.fill(talking_point, 98, initial_indent="  > ", subsequent_indent="    "))
        print()
        if self.pause:
            input("  [Enter] to run ")

    @staticmethod
    def note(text: str) -> None:
        print(textwrap.fill(text, 98, initial_indent="  * ", subsequent_indent="    "))


def timed_query(connection: duckdb.DuckDBPyConnection, title: str, sql: str) -> None:
    started = time.perf_counter()
    cursor = connection.execute(sql)
    rows = cursor.fetchall()
    elapsed = time.perf_counter() - started
    names = [item[0] for item in cursor.description]
    print(f"\n  -- {title}  ({elapsed * 1000:.0f} ms)")
    widths = [max(len(str(name)), *(len(f"{row[i]:,}" if isinstance(row[i], int | float) else str(row[i])) for row in rows)) for i, name in enumerate(names)]
    print("     " + "  ".join(str(name).ljust(width) for name, width in zip(names, widths, strict=True)))
    for row in rows:
        cells = [f"{value:,}" if isinstance(value, int | float) else str(value) for value in row]
        print("     " + "  ".join(cell.rjust(width) if isinstance(value, int | float) or cell[:1].isdigit() else cell.ljust(width) for cell, width, value in zip(cells, widths, row, strict=True)))


def show_discovery(context: ContextService, question: str) -> None:
    result = context.discover(question, mode=DiscoveryMode.HYBRID, limit=4)
    discovery = result.discovery
    print(f'\n  Q: "{question}"')
    print(f"  status: {discovery.status.value}")
    if discovery.ambiguity:
        conflicting = ", ".join(metric.metric_id for metric in discovery.ambiguity.conflicting_metrics)
        print(f"  ambiguity: {conflicting}")
    for item in result.candidates:
        runtime = [reason for reason in item.candidate.reasons if reason.signal.startswith("runtime_")]
        signals = ", ".join(f"{reason.signal.removeprefix('runtime_')} {reason.points:+d}" for reason in runtime) or "no runtime data"
        print(f"    {item.candidate.score:>4}  {item.candidate.dataset_id:34} [{signals}]")


def step_data(presenter: Presenter, paths: PlatformPaths, scale: float) -> None:
    presenter.step(
        1,
        "Real volume, reproducible data",
        "We generate TPC-H, the industry-standard order/customer/product benchmark, with the "
        "tpchgen-rs reference generator. It lands as Parquet in a local data directory, "
        "never in Git.",
    )
    manifest_path = paths.data / "tpch" / "manifest.json"
    if manifest_path.is_file():
        presenter.note(f"Reusing existing data in {manifest_path.parent} (delete it to regenerate).")
        import json

        manifest = json.loads(manifest_path.read_text())
    else:
        started = time.perf_counter()
        manifest = generate_tpch(paths.data, scale)
        presenter.note(f"Generated scale factor {scale:g} in {time.perf_counter() - started:.1f}s.")
    for table in ("lineitem", "orders", "customer", "part"):
        size = manifest["bytes"][table] / 1_000_000
        print(f"    {table:10} {manifest['row_counts'][table]:>12,} rows  {size:>8.1f} MB parquet")


def step_semantics(presenter: Presenter, registry) -> None:
    presenter.step(
        2,
        "Semantics before pipelines",
        "Three teams say 'revenue' and mean three different things. The registry records each "
        "definition once, with its owner and permitted uses, and marks them as non-equivalent. "
        "Every pipeline publishes into one of these contracts.",
    )
    for metric_id in ("metric.finance_net_revenue", "metric.operations_net_revenue", "metric.sales_bookings"):
        metric = registry.metrics[metric_id]
        print(f"    {metric.id:32} owner={metric.owner}")
        print(textwrap.fill(metric.definition, 94, initial_indent="        ", subsequent_indent="        "))


def step_pipelines(presenter: Presenter, environment) -> None:
    presenter.step(
        3,
        "Declarative pipelines with governed gates",
        "Each pipeline is a YAML spec with one SELECT, declared inputs, and quality and "
        "reconciliation gates. The runner builds a candidate table in a sandbox with file "
        "access disabled, checks it against the dataset contract, and publishes atomically "
        "with a backup only if every gate passes.",
    )
    for item in environment.runner.ingest():
        print(f"    landed {item.source_id:15} {item.row_count:>12,} rows  {item.duration_seconds:5.2f}s")
    print()
    started = time.perf_counter()
    print_run_results(environment.runner.run())
    presenter.note(f"Medallion build (raw -> silver -> gold) finished in {time.perf_counter() - started:.1f}s.")


def step_analytics(presenter: Presenter, paths: PlatformPaths) -> None:
    presenter.step(
        4,
        "DuckDB analytics on governed gold tables",
        "Same question, three governed answers. TPC-H has a fixed as-of date (1995-06-17): "
        "orders placed near it are booked but not yet completed or recognized, so the three "
        "'net revenue' definitions diverge by billions. This is why the platform refuses to guess.",
    )
    with duckdb.connect(str(paths.state / "engineering.duckdb"), read_only=True) as connection:
        connection.execute("SET enable_progress_bar = false")
        timed_query(
            connection,
            "'Net revenue' by quarter, three governed definitions (USD millions)",
            """
            WITH quarters(quarter) AS (VALUES ('1994-Q4'), ('1995-Q1'), ('1995-Q2')),
            finance AS (
                SELECT fiscal_quarter AS quarter, SUM(recognized_net_revenue) AS usd
                FROM gold__finance_revenue GROUP BY 1
            ),
            operations AS (
                SELECT strftime(order_date, '%Y') || '-Q' || quarter(order_date) AS quarter, SUM(net_revenue) AS usd
                FROM gold__order_revenue GROUP BY 1
            ),
            bookings AS (
                SELECT strftime(booking_month, '%Y') || '-Q' || quarter(booking_month) AS quarter,
                       SUM(bookings_amount) AS usd
                FROM gold__monthly_sales GROUP BY 1
            )
            SELECT quarters.quarter,
                   ROUND(finance.usd / 1e6, 1) AS finance_recognized,
                   ROUND(operations.usd / 1e6, 1) AS operations_completed,
                   ROUND(bookings.usd / 1e6, 1) AS sales_bookings
            FROM quarters
            LEFT JOIN finance USING (quarter)
            LEFT JOIN operations USING (quarter)
            LEFT JOIN bookings USING (quarter)
            ORDER BY quarters.quarter
            """,
        )
        timed_query(
            connection,
            "Recognized revenue by region, 1994 vs 1993",
            """
            WITH yearly AS (
                SELECT sales_region, LEFT(fiscal_quarter, 4) AS fiscal_year, SUM(recognized_net_revenue) AS revenue
                FROM gold__finance_revenue GROUP BY ALL
            )
            SELECT sales_region,
                   ROUND(SUM(revenue) FILTER (WHERE fiscal_year = '1994') / 1e6, 1) AS usd_m_1994,
                   ROUND(100 * (SUM(revenue) FILTER (WHERE fiscal_year = '1994')
                         / SUM(revenue) FILTER (WHERE fiscal_year = '1993') - 1), 2) AS yoy_pct
            FROM yearly GROUP BY sales_region ORDER BY usd_m_1994 DESC
            """,
        )
        timed_query(
            connection,
            "Return reversals vs recognized revenue by region (full scan of the silver event table)",
            """
            SELECT sales_region,
                   COUNT(*) AS revenue_events,
                   ROUND(SUM(amount) FILTER (WHERE event_type = 'return_reversal') / 1e6, 1) AS reversals_usd_m,
                   ROUND(100 * -SUM(amount) FILTER (WHERE event_type = 'return_reversal')
                         / SUM(amount) FILTER (WHERE event_type = 'recognition'), 2) AS reversal_pct
            FROM silver__revenue_events GROUP BY ALL ORDER BY sales_region
            """,
        )
    presenter.note(
        "TPC-H flags about half of received lines as returned by design, so reversal rates "
        "near 50% reflect the synthetic generator, not a pipeline defect."
    )


def build_context(environment) -> ContextService:
    return ContextService(
        environment.registry,
        environment.runtime,
        build_vector_index(environment.registry, HashingEmbeddingProvider()),
    )


def step_discovery(presenter: Presenter, environment, context: ContextService) -> None:
    presenter.step(
        5,
        "Governed discovery now ranks on real pipeline signals",
        "Pipeline runs feed freshness, quality, and health back into the context layer. "
        "Discovery still returns CLARIFICATION_REQUIRED for the ambiguous question; runtime "
        "signals rerank candidates but never choose between business definitions.",
    )
    show_discovery(context, "What was net revenue last quarter?")
    show_discovery(context, "Finance recognized net revenue for quarter-end reporting")
    print("\n  Lineage verified by the last successful runs:")
    print_lineage(environment.runner.verified_lineage(), "gold.finance_revenue")


def print_lineage(rows: list[dict], dataset_id: str, depth: int = 0) -> None:
    print(f"    {'    ' * depth}{'<- ' if depth else ''}{dataset_id}")
    inputs = [row for row in rows if row["output"] == dataset_id]
    sources = sorted(row["input_id"] for row in inputs if row["input_kind"] == "source")
    for row in sorted(inputs, key=lambda item: item["input_id"]):
        if row["input_kind"] == "dataset":
            print_lineage(rows, row["input_id"], depth + 1)
    if sources:
        print(f"    {'    ' * (depth + 1)}<- raw: {', '.join(sources)}")


def write_bad_batch(paths: PlatformPaths) -> Path:
    target = paths.data / "tpch" / BAD_BATCH
    with duckdb.connect() as connection:
        connection.execute(
            f"""
            COPY (
                (SELECT * REPLACE (9999999901::BIGINT AS l_orderkey,
                                   'late batch: order never arrived' AS l_comment)
                 FROM read_parquet('{paths.data / "tpch" / "lineitem.parquet"}') LIMIT 1)
                UNION ALL
                (SELECT * REPLACE (8::INTEGER AS l_linenumber,
                                   -l_extendedprice AS l_extendedprice,
                                   'late batch: sign-flipped price' AS l_comment)
                 FROM read_parquet('{paths.data / "tpch" / "lineitem.parquet"}') LIMIT 1)
            ) TO '{target}' (FORMAT parquet)
            """
        )
    return target


def step_bad_batch(presenter: Presenter, paths: PlatformPaths, environment, context: ContextService) -> None:
    presenter.step(
        6,
        "A bad batch arrives",
        "A late file lands with two broken order lines: one for an order that never arrived "
        "and one with a sign-flipped price. Joins would silently drop the first; the "
        "reconciliation gate notices. The candidate is discarded, the published table is "
        "untouched, downstream pipelines are skipped, and discovery sees the degraded signal.",
    )
    question = "order lifecycle event records for pipeline troubleshooting"
    show_discovery(context, question)
    target = write_bad_batch(paths)
    presenter.note(f"Wrote {target.relative_to(PROJECT_ROOT) if target.is_relative_to(PROJECT_ROOT) else target}")
    environment.runner.ingest(["tpch.lineitem"])
    print_run_results(environment.runner.run(["pipeline.silver_order_events"]))
    presenter.note("Same question after the failed run: silver.order_events loses 40 points and drops in rank.")
    show_discovery(context, question)
    print()
    presenter.note("Remove the bad file, re-land, and re-run: the platform recovers on its own.")
    target.unlink()
    environment.runner.ingest(["tpch.lineitem"])
    results = environment.runner.run(["pipeline.silver_order_events"])
    print_run_results(results)


def step_agent(presenter: Presenter, paths: PlatformPaths, registry) -> None:
    presenter.step(
        7,
        "An agent authors a new pipeline",
        "Through MCP, an agent can inspect inputs, validate a spec, dry-run it in a read-only "
        "sandbox, and submit a proposal. It cannot publish, read files, or certify data. "
        "These are the same calls the MCP tools make.",
    )
    service = create_pipeline_authoring_service(registry, paths.pipelines, paths.state)
    proposals = DEMO / "agent_proposals"
    unsafe = service.validate_proposal((proposals / "unsafe_spec.yaml").read_text())
    print("  1) unsafe spec -> rejected:")
    for error in unsafe.errors:
        print(f"       - {error}")
    spec = (proposals / "customer_segment_revenue.yaml").read_text()
    contract = (proposals / "customer_segment_revenue.contract.yaml").read_text()
    certified = service.validate_proposal(spec, contract.replace("certification: uncertified", "certification: certified"))
    print("  2) self-certified contract -> rejected:")
    for error in certified.errors:
        print(f"       - {error}")
    dry_run = service.dry_run(spec, contract)
    print(f"  3) dry run -> passed={dry_run.passed}, rows={dry_run.row_count}")
    for check in dry_run.checks:
        print(f"       {'PASS' if check.passed else 'FAIL'} {check.name}")
    for row in dry_run.preview[:3]:
        print(f"       {row}")
    submission = service.submit_proposal(spec, contract, "Customer strategy asked for segment-level recognized revenue.")
    print(f"  4) submit -> {submission.status}: {', '.join(submission.approval_reasons)}")
    print(f"       review package: {Path(submission.proposal_path).relative_to(PROJECT_ROOT) if Path(submission.proposal_path).is_relative_to(PROJECT_ROOT) else submission.proposal_path}")
    presenter.note(
        "Live variant: start Claude Code (or any MCP client) with demo/mcp.json and ask it to "
        "propose a pipeline for revenue by customer market segment."
    )


def step_dbt(presenter: Presenter, paths: PlatformPaths, environment, dbt_bin: str | None) -> None:
    presenter.step(
        8,
        "Same specs, your stack: compile to dbt",
        "The spec SQL already uses dbt's ref()/source() syntax. Contracts become enforced dbt "
        "model contracts and gates become dbt tests, so teams on dbt + a warehouse keep this "
        "governance model. See docs/pipeline-portability.md for the Databricks mapping.",
    )
    output = paths.state / "dbt_project"
    if output.exists():
        shutil.rmtree(output)
    files = export_dbt_project(
        environment.project, environment.registry, output, warehouse_path=paths.state / "engineering.duckdb"
    )
    presenter.note(f"Wrote {len(files)} files to {output}.")
    executable = dbt_bin or shutil.which("dbt")
    if executable is None:
        presenter.note(f"dbt is not installed; run: pip install dbt-duckdb && dbt build --project-dir {output} --profiles-dir {output}")
        return
    completed = subprocess.run(
        [executable, "build", "--project-dir", str(output), "--profiles-dir", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    summary = [line for line in completed.stdout.splitlines() if "Done." in line or "Finished running" in line]
    for line in summary:
        print("    " + line.split("  ", 1)[-1].strip())
    if completed.returncode != 0:
        print(completed.stdout[-3_000:])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pause", action="store_true", help="Wait for Enter before each step.")
    parser.add_argument("--only", type=int, action="append", help="Run only these step numbers.")
    parser.add_argument("--scale", type=float, default=1.0, help="TPC-H scale factor (1 = ~6M order lines).")
    parser.add_argument("--state-dir", type=Path, default=Path("var"))
    parser.add_argument("--data-dir", type=Path, default=Path("var/data"))
    parser.add_argument("--dbt-bin", default=None, help="Path to a dbt executable (default: dbt on PATH).")
    args = parser.parse_args()
    paths = PlatformPaths(state=args.state_dir.resolve(), data=args.data_dir.resolve())
    steps = set(args.only or range(1, 9))
    presenter = Presenter(args.pause)
    if 1 in steps:
        try:
            step_data(presenter, paths, args.scale)
        except SampleDataError as error:
            print(f"  ! {error}")
            return 1
    with open_pipeline_environment(paths) as environment:
        if 2 in steps:
            step_semantics(presenter, environment.registry)
        if 3 in steps:
            step_pipelines(presenter, environment)
        if 4 in steps:
            step_analytics(presenter, paths)
        context = build_context(environment)
        if 5 in steps:
            step_discovery(presenter, environment, context)
        if 6 in steps:
            step_bad_batch(presenter, paths, environment, context)
        if 7 in steps:
            step_agent(presenter, paths, environment.registry)
        if 8 in steps:
            step_dbt(presenter, paths, environment, args.dbt_bin)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
