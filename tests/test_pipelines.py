"""Acceptance tests for governed pipelines, agent authoring, and dbt export."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import duckdb
import yaml
from mcp.server.mcpserver.exceptions import ToolError

from ai_data_platform import load_registry
from ai_data_platform.engineering import EngineeringService
from ai_data_platform.mcp import create_mcp_server
from ai_data_platform.mcp.server import create_pipeline_authoring_service
from ai_data_platform.pipelines import (
    PipelineRunner,
    PipelineSqlError,
    PipelineValidationError,
    RunStatus,
    load_pipeline_project,
    parse_pipeline_spec,
    validate_pipeline_project,
)
from ai_data_platform.pipelines.dbt import export_dbt_project
from ai_data_platform.pipelines.factory import open_runtime_repository
from ai_data_platform.pipelines.models import RowCountCheck
from ai_data_platform.pipelines.sql import render, sandboxed_connection, validate_select
from ai_data_platform.runtime import FreshnessStatus, OperationalHealth, QualityStatus
from ai_data_platform.sample_data import SampleDataError, download_nyc_taxi, generate_tpch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REGISTRY = PROJECT_ROOT / "registry"
PIPELINES = PROJECT_ROOT / "pipelines"
PROPOSALS = PROJECT_ROOT / "demo" / "agent_proposals"

# Four orders: two fulfilled (F), one open (O), one partially fulfilled (P).
# Shipped lines (line_status F) are recognized; returned ones (R) are reversed.
FIXTURE = {
    "region": "SELECT * FROM (VALUES (0::BIGINT, 'AMERICA', ''), (1::BIGINT, 'EUROPE', '')) t(r_regionkey, r_name, r_comment)",
    "nation": "SELECT * FROM (VALUES (0::BIGINT, 'CANADA', 0::BIGINT, ''), (1::BIGINT, 'FRANCE', 1::BIGINT, '')) t(n_nationkey, n_name, n_regionkey, n_comment)",
    "customer": "SELECT * FROM (VALUES (1::BIGINT, 0::BIGINT, 'BUILDING'), (2::BIGINT, 1::BIGINT, 'MACHINERY')) t(c_custkey, c_nationkey, c_mktsegment)",
    "part": "SELECT * FROM (VALUES (10::BIGINT, 'Brand#11'), (20::BIGINT, 'Brand#22')) t(p_partkey, p_brand)",
    "orders": """SELECT * FROM (VALUES
        (100::BIGINT, 1::BIGINT, 'F', DATE '1994-01-10'),
        (200::BIGINT, 2::BIGINT, 'F', DATE '1994-02-10'),
        (300::BIGINT, 1::BIGINT, 'O', DATE '1995-06-01'),
        (400::BIGINT, 2::BIGINT, 'P', DATE '1995-05-01')) t(o_orderkey, o_custkey, o_orderstatus, o_orderdate)""",
    "lineitem": """SELECT l_orderkey::BIGINT AS l_orderkey, l_partkey::BIGINT AS l_partkey,
            l_linenumber::INTEGER AS l_linenumber, l_quantity::DECIMAL(15,2) AS l_quantity,
            l_extendedprice::DECIMAL(15,2) AS l_extendedprice, l_discount::DECIMAL(15,2) AS l_discount,
            l_returnflag, l_linestatus, l_shipdate::DATE AS l_shipdate, l_receiptdate::DATE AS l_receiptdate
        FROM (VALUES
            (100, 10, 1, 1, 1000.00, 0.10, 'A', 'F', '1994-01-15', '1994-01-20'),
            (100, 20, 2, 2, 500.00, 0.00, 'R', 'F', '1994-01-16', '1994-01-25'),
            (200, 10, 1, 3, 2000.00, 0.05, 'A', 'F', '1994-02-15', '1994-02-20'),
            (300, 20, 1, 1, 300.00, 0.00, 'N', 'O', '1995-07-01', '1995-07-05'),
            (400, 10, 1, 1, 100.00, 0.00, 'N', 'F', '1995-05-05', '1995-05-09'),
            (400, 20, 2, 1, 50.00, 0.00, 'N', 'O', '1995-07-10', '1995-07-15'))
        t(l_orderkey, l_partkey, l_linenumber, l_quantity, l_extendedprice, l_discount,
          l_returnflag, l_linestatus, l_shipdate, l_receiptdate)""",
}


def write_fixture(data_directory: Path) -> None:
    tpch = data_directory / "tpch"
    tpch.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as connection:
        for table, sql in FIXTURE.items():
            connection.execute(f"COPY ({sql}) TO '{tpch / (table + '.parquet')}' (FORMAT parquet)")


class PipelineHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.data = root / "data"
        self.state = root / "state"
        write_fixture(self.data)
        self.registry = load_registry(REGISTRY)
        self.project = load_pipeline_project(PIPELINES)
        self.engineering = EngineeringService(self.registry, PROJECT_ROOT, self.state)
        self.runtime, current, history = open_runtime_repository(self.state)
        self.addCleanup(current.close)
        self.addCleanup(history.close)
        self.runner = PipelineRunner(
            self.registry, self.project, self.engineering, self.data, runtime_repository=self.runtime
        )

    def query(self, sql: str):
        with duckdb.connect(str(self.engineering.database_path), read_only=True) as connection:
            return connection.execute(sql).fetchall()


class PipelineProjectTests(unittest.TestCase):
    def test_repository_project_validates_and_orders_dependencies(self) -> None:
        registry = load_registry(REGISTRY)
        project = load_pipeline_project(PIPELINES)
        validate_pipeline_project(project, registry)
        order = [spec.output for spec in project.execution_order()]
        self.assertLess(order.index("silver.order_events"), order.index("silver.revenue_events"))
        self.assertLess(order.index("silver.revenue_events"), order.index("gold.finance_revenue"))
        self.assertLess(order.index("gold.order_revenue"), order.index("gold.product_performance"))
        self.assertEqual(len(project.pipelines), 7)

    def test_undeclared_and_unused_inputs_are_rejected(self) -> None:
        registry = load_registry(REGISTRY)
        project = load_pipeline_project(PIPELINES)
        spec = project.pipelines["pipeline.gold_order_revenue"]
        project.pipelines[spec.id] = spec.model_copy(
            update={"sql": spec.sql.replace("ref('silver.order_events')", "ref('silver.revenue_events')")}
        )
        with self.assertRaises(PipelineValidationError) as raised:
            validate_pipeline_project(project, registry)
        text = "\n".join(raised.exception.messages)
        self.assertIn("ref('silver.revenue_events') is not a declared dataset input", text)

    def test_cycles_are_rejected(self) -> None:
        project = load_pipeline_project(PIPELINES)
        silver = project.pipelines["pipeline.silver_order_events"]
        project.pipelines[silver.id] = silver.model_copy(
            update={"inputs": silver.inputs.model_copy(update={"datasets": ["gold.order_revenue"]})}
        )
        with self.assertRaises(PipelineValidationError):
            project.execution_order()


class SqlSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect()
        self.addCleanup(self.connection.close)
        self.allowed = {'wh.main."raw__tpch__orders"'}

    def test_render_maps_templates_and_rejects_undeclared_references(self) -> None:
        rendered = render("SELECT * FROM {{ source('tpch', 'orders') }}", sources=["tpch.orders"], datasets=[])
        self.assertIn('wh.main."raw__tpch__orders"', rendered.sql)
        with self.assertRaises(PipelineSqlError):
            render("SELECT * FROM {{ ref('gold.order_revenue') }}", sources=[], datasets=[])
        with self.assertRaises(PipelineSqlError):
            render("SELECT {{ env_var('HOME') }}", sources=[], datasets=[])

    def test_only_one_select_without_table_functions_or_undeclared_tables(self) -> None:
        validate_select(self.connection, 'SELECT * FROM wh.main."raw__tpch__orders" AS o', self.allowed)
        validate_select(
            self.connection,
            'WITH o AS (SELECT * FROM wh.main."raw__tpch__orders") SELECT * FROM o',
            self.allowed,
        )
        unsafe = {
            "DROP TABLE wh.main.raw__tpch__orders": "only SELECT",
            "SELECT 1; SELECT 2": "exactly one statement",
            "SELECT * FROM read_csv('/etc/passwd')": "table functions",
            "SELECT * FROM query_table('raw__tpch__orders')": "table functions",
            "SELECT * FROM wh.main.raw__tpch__lineitem": "not declared inputs",
            "SELECT (SELECT max(1) FROM wh.main._platform_pipeline_runs)": "not declared inputs",
            "SELECT * FROM information_schema.tables": "not declared inputs",
            "SELECT * FROM pg_catalog.pg_class": "not declared inputs",
            "SELECT * FROM sqlite_master": "not declared inputs",
            "SELECT * FROM memory.main.raw__tpch__orders": "not declared inputs",
        }
        for sql, message in unsafe.items():
            with self.subTest(sql=sql), self.assertRaisesRegex(PipelineSqlError, message):
                validate_select(self.connection, sql, self.allowed)

    def test_sandbox_blocks_file_access_even_without_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            warehouse = Path(directory) / "wh.duckdb"
            duckdb.connect(str(warehouse)).close()
            connection = sandboxed_connection(warehouse, read_only=True, temp_directory=Path(directory) / "tmp")
            try:
                with self.assertRaises(duckdb.Error):
                    connection.execute(f"SELECT * FROM read_csv('{warehouse}')")
                with self.assertRaises(duckdb.Error):
                    connection.execute("SET enable_external_access = true")
                with self.assertRaises(duckdb.Error):
                    connection.execute("CREATE TABLE wh.main.x AS SELECT 1")
            finally:
                connection.close()


class PipelineRunnerTests(PipelineHarness):
    def test_full_build_publishes_reconciled_tables_runtime_state_and_lineage(self) -> None:
        self.runner.ingest()
        results = self.runner.run()
        self.assertEqual({result.status for result in results}, {RunStatus.SUCCEEDED}, [r.message for r in results])
        self.assertEqual(self.query("SELECT COUNT(*) FROM silver__order_events")[0][0], 6)
        # Completed orders 100 and 200: 900 + 500 + 1900.
        self.assertEqual(self.query("SELECT SUM(net_revenue) FROM gold__order_revenue")[0][0], Decimal("3300.00"))
        # Shipped lines 900 + 500 + 1900 + 100, minus the returned 500.
        self.assertEqual(
            self.query("SELECT SUM(recognized_net_revenue) FROM gold__finance_revenue")[0][0], Decimal("2900.00")
        )
        self.assertEqual(self.query("SELECT SUM(bookings_amount) FROM gold__monthly_sales")[0][0], Decimal("3750.00"))
        latest = self.runtime.get_latest("gold.finance_revenue")
        self.assertEqual(latest.freshness_status, FreshnessStatus.FRESH)
        self.assertEqual(latest.quality_status, QualityStatus.PASSING)
        self.assertEqual(latest.operational_health, OperationalHealth.HEALTHY)
        self.assertGreater(latest.quality_checks_passed, 0)
        edges = {(row["input_id"], row["output"]) for row in self.runner.verified_lineage()}
        self.assertIn(("silver.revenue_events", "gold.finance_revenue"), edges)
        self.assertIn(("tpch.lineitem", "silver.order_events"), edges)

    def test_rerun_keeps_one_backup_and_leaves_no_staging_tables(self) -> None:
        self.runner.ingest()
        self.runner.run()
        self.runner.run(["pipeline.gold_order_revenue"], include_downstream=False)
        self.runner.run(["pipeline.gold_order_revenue"], include_downstream=False)
        tables = {row[0] for row in self.query("SELECT table_name FROM information_schema.tables")}
        backups = [name for name in tables if name.startswith("gold__order_revenue__backup__")]
        self.assertEqual(len(backups), 1)
        self.assertFalse([name for name in tables if "__staging_" in name])

    def test_bad_batch_is_quarantined_and_downstream_is_skipped(self) -> None:
        self.runner.ingest()
        self.runner.run()
        published = self.query("SELECT COUNT(*) FROM silver__order_events")[0][0]
        first_success = self.runtime.get_latest("silver.order_events").last_successful_run_at
        with duckdb.connect() as connection:
            connection.execute(
                f"COPY (SELECT * REPLACE (999::BIGINT AS l_orderkey) FROM read_parquet("
                f"'{self.data / 'tpch' / 'lineitem.parquet'}') LIMIT 1) "
                f"TO '{self.data / 'tpch' / 'lineitem_late.parquet'}' (FORMAT parquet)"
            )
        self.runner.ingest(["tpch.lineitem"])
        results = {result.pipeline_id: result for result in self.runner.run(["pipeline.silver_order_events"])}
        silver = results["pipeline.silver_order_events"]
        self.assertEqual(silver.status, RunStatus.FAILED_QUALITY)
        self.assertIn("orphans", next(check.name for check in silver.checks if not check.passed))
        self.assertEqual(results["pipeline.gold_finance_revenue"].status, RunStatus.SKIPPED)
        self.assertEqual(self.query("SELECT COUNT(*) FROM silver__order_events")[0][0], published)
        latest = self.runtime.get_latest("silver.order_events")
        self.assertEqual(latest.quality_status, QualityStatus.FAILING)
        self.assertEqual(latest.freshness_status, FreshnessStatus.STALE)
        self.assertEqual(latest.last_successful_run_at, first_success)

    def test_skipped_downstream_is_marked_stale_but_keeps_validated_quality(self) -> None:
        self.runner.ingest()
        self.runner.run()
        before = self.runtime.get_latest("gold.finance_revenue")
        silver = self.project.pipelines["pipeline.silver_order_events"]
        self.project.pipelines[silver.id] = silver.model_copy(
            update={"quality_checks": [*silver.quality_checks, RowCountCheck(check="row_count", min=10_000)]}
        )
        results = {result.pipeline_id: result for result in self.runner.run([silver.id])}
        self.assertEqual(results["pipeline.gold_finance_revenue"].status, RunStatus.SKIPPED)
        after = self.runtime.get_latest("gold.finance_revenue")
        self.assertEqual(after.freshness_status, FreshnessStatus.STALE)
        self.assertEqual(after.quality_status, before.quality_status)
        self.assertEqual(after.operational_health, before.operational_health)
        self.assertEqual(after.last_successful_run_at, before.last_successful_run_at)
        self.assertGreater(after.observed_at, before.observed_at)

    def test_renamed_pipeline_replaces_lineage_for_its_output(self) -> None:
        self.runner.ingest()
        self.runner.run()
        spec = self.project.pipelines.pop("pipeline.gold_product_performance")
        renamed = spec.model_copy(update={"id": "pipeline.gold_brand_performance"})
        self.project.pipelines[renamed.id] = renamed
        self.runner.run([renamed.id])
        producers = {
            row["pipeline_id"]
            for row in self.runner.verified_lineage()
            if row["output"] == "gold.product_performance"
        }
        self.assertEqual(producers, {"pipeline.gold_brand_performance"})

    def test_contract_type_drift_blocks_publication(self) -> None:
        self.runner.ingest()
        spec = self.project.pipelines["pipeline.silver_order_events"]
        drifted = spec.sql.replace("AS DECIMAL(18, 2)) AS net_amount", "AS DOUBLE) AS net_amount")
        self.project.pipelines[spec.id] = spec.model_copy(update={"sql": drifted})
        result = self.runner.run([spec.id], include_downstream=False)[0]
        self.assertEqual(result.status, RunStatus.FAILED_CONTRACT)
        self.assertIn("net_amount' has type DOUBLE, contract requires DECIMAL(18,2)", result.message)
        self.assertEqual(self.query("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'silver__order_events'")[0][0], 0)

    def test_missing_source_files_are_reported(self) -> None:
        shutil.rmtree(self.data / "tpch")
        with self.assertRaisesRegex(PipelineValidationError, "generate sample data first"):
            self.runner.ingest()


class AuthoringTests(PipelineHarness):
    def setUp(self) -> None:
        super().setUp()
        self.runner.ingest()
        self.runner.run()
        self.service = create_pipeline_authoring_service(self.registry, PIPELINES, self.state)
        self.spec = (PROPOSALS / "customer_segment_revenue.yaml").read_text()
        self.contract = (PROPOSALS / "customer_segment_revenue.contract.yaml").read_text()

    def test_unsafe_spec_reports_every_violation(self) -> None:
        validation = self.service.validate_proposal((PROPOSALS / "unsafe_spec.yaml").read_text())
        self.assertFalse(validation.valid)
        text = "\n".join(validation.errors)
        self.assertIn("read_csv", text)
        self.assertIn("raw__tpch__orders", text)

    def test_agents_cannot_certify_or_override_governed_contracts(self) -> None:
        certified = self.service.validate_proposal(
            self.spec, self.contract.replace("certification: uncertified", "certification: certified")
        )
        self.assertIn("agents cannot certify a dataset; propose it as uncertified", certified.errors)
        existing = yaml.safe_load(self.spec)
        existing["output"] = "gold.finance_revenue"
        override = self.service.validate_proposal(yaml.safe_dump(existing), self.contract)
        self.assertTrue(any("already has a governed contract" in error for error in override.errors))

    def test_dry_run_is_read_only_and_submission_needs_a_passing_run(self) -> None:
        before = self.query("SELECT table_name FROM information_schema.tables ORDER BY 1")
        dry_run = self.service.dry_run(self.spec, self.contract)
        self.assertTrue(dry_run.passed, dry_run.message)
        self.assertEqual(self.query("SELECT table_name FROM information_schema.tables ORDER BY 1"), before)
        self.assertTrue(dry_run.preview)

        broken = self.contract.replace("customer_count, type: BIGINT", "customer_count, type: INTEGER")
        rejected = self.service.submit_proposal(self.spec, broken)
        self.assertEqual(rejected.status, "rejected")
        self.assertFalse((self.state / "pipeline_proposals").exists())

        submitted = self.service.submit_proposal(self.spec, self.contract, "segment view")
        self.assertEqual(submitted.status, "approval_required")
        package = Path(submitted.proposal_path)
        self.assertEqual(sorted(path.name for path in package.iterdir()), ["contract.yaml", "pipeline.yaml", "review.json"])
        self.assertIn("new dataset contracts require approval", submitted.approval_reasons)

    def test_proposals_that_create_dependency_cycles_are_rejected(self) -> None:
        cyclic = yaml.safe_dump(
            {
                "id": "pipeline.silver_order_events",
                "description": "Rebuild order events from a downstream gold table.",
                "owner": "agent",
                "output": "silver.order_events",
                "inputs": {"datasets": ["gold.product_performance"]},
                "sql": "SELECT * FROM {{ ref('gold.product_performance') }}",
            }
        )
        validation = self.service.validate_proposal(cyclic)
        self.assertFalse(validation.valid)
        self.assertTrue(any("cycle" in error for error in validation.errors), validation.errors)
        self.assertFalse(self.service.dry_run(cyclic).passed)

    def test_lineage_lists_upstream_and_downstream(self) -> None:
        node = self.service.lineage("silver.revenue_events")
        self.assertEqual(node.upstream_datasets, ["silver.order_events"])
        self.assertEqual(node.downstream_datasets, ["gold.customer_financial_summary", "gold.finance_revenue"])


class McpPipelineToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_tools_are_registered_with_honest_annotations(self) -> None:
        from ai_data_platform.agent_tools import AgentToolService
        from ai_data_platform.context import ContextService

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            registry = load_registry(REGISTRY)
            runtime, current, history = open_runtime_repository(state)
            try:
                server = create_mcp_server(
                    AgentToolService(ContextService(registry, runtime), REGISTRY),
                    pipeline_service=create_pipeline_authoring_service(registry, PIPELINES, state),
                )
                tools = {tool.name: tool for tool in await server.list_tools()}
                for name in ("list_pipeline_inputs", "get_dataset_lineage", "validate_pipeline_proposal", "dry_run_pipeline_proposal"):
                    self.assertTrue(tools[name].annotations.read_only_hint, name)
                submit = tools["submit_pipeline_proposal"].annotations
                self.assertFalse(submit.read_only_hint)
                self.assertFalse(submit.destructive_hint)
                response = await server.call_tool(
                    "validate_pipeline_proposal", {"spec_yaml": (PROPOSALS / "unsafe_spec.yaml").read_text()}
                )
                self.assertFalse(response.structured_content["valid"])
                with self.assertRaisesRegex(ToolError, "CAPABILITY_UNAVAILABLE"):
                    await server.call_tool(
                        "dry_run_pipeline_proposal",
                        {
                            "spec_yaml": (PROPOSALS / "customer_segment_revenue.yaml").read_text(),
                            "contract_yaml": (PROPOSALS / "customer_segment_revenue.contract.yaml").read_text(),
                        },
                    )
            finally:
                current.close()
                history.close()


class McpRegistryCouplingTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_registry_starts_without_bundled_pipeline_tools(self) -> None:
        from ai_data_platform.mcp.server import create_mcp_server_from_paths, resolve_pipelines_path

        with tempfile.TemporaryDirectory() as directory:
            custom = Path(directory) / "registry"
            shutil.copytree(REGISTRY, custom)
            (custom / "datasets" / "gold_product_performance.yaml").unlink()
            self.assertIsNone(resolve_pipelines_path(custom, None))
            self.assertEqual(resolve_pipelines_path(REGISTRY, None), PIPELINES)
            self.assertEqual(resolve_pipelines_path(custom, PIPELINES), PIPELINES)
            server = create_mcp_server_from_paths(
                custom, Path(directory) / "state", resolve_pipelines_path(custom, None)
            )
            async with server.settings.lifespan(server):
                names = {tool.name for tool in await server.list_tools()}
            self.assertIn("discover_datasets", names)
            self.assertNotIn("dry_run_pipeline_proposal", names)


class DbtExportTests(unittest.TestCase):
    def test_export_maps_refs_contracts_and_gates(self) -> None:
        registry = load_registry(REGISTRY)
        project = load_pipeline_project(PIPELINES)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dbt"
            files = export_dbt_project(project, registry, output, warehouse_path=Path(directory) / "wh.duckdb")
            self.assertEqual(len({path.name for path in files}), len(files))
            model_sql = (output / "models" / "gold" / "gold__finance_revenue.sql").read_text()
            self.assertIn("{{ ref('silver__revenue_events') }}", model_sql)
            schema = yaml.safe_load((output / "models" / "gold" / "gold__finance_revenue.yml").read_text())
            model = schema["models"][0]
            self.assertTrue(model["config"]["contract"]["enforced"])
            self.assertEqual(
                {column["name"]: column["data_type"] for column in model["columns"]}["recognized_net_revenue"],
                "decimal(18,2)",
            )
            reconcile = (output / "tests" / "gold__finance_revenue__reconcile_1.sql").read_text()
            self.assertIn("{{ ref('gold__finance_revenue') }}", reconcile)
            self.assertNotIn("{{ this }}", reconcile)
            sources = yaml.safe_load((output / "models" / "sources.yml").read_text())
            self.assertIn("raw__tpch__lineitem", str(sources))
            profile = yaml.safe_load((output / "profiles.yml").read_text())
            self.assertTrue(profile["ai_native_platform"]["outputs"]["dev"]["attach"][0]["read_only"])


    def test_reexport_removes_artifacts_of_deleted_pipelines(self) -> None:
        registry = load_registry(REGISTRY)
        project = load_pipeline_project(PIPELINES)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dbt"
            warehouse = Path(directory) / "wh.duckdb"
            export_dbt_project(project, registry, output, warehouse_path=warehouse)
            (output / "notes.md").write_text("kept: not generated by the exporter")
            del project.pipelines["pipeline.gold_product_performance"]
            export_dbt_project(project, registry, output, warehouse_path=warehouse)
            leftovers = [path.name for path in output.rglob("gold__product_performance*")]
            self.assertEqual(leftovers, [])
            self.assertTrue((output / "notes.md").is_file())
            self.assertTrue((output / "models" / "gold" / "gold__order_revenue.sql").is_file())


class SampleDataTests(unittest.TestCase):
    def test_arguments_are_validated_before_any_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SampleDataError, "scale_factor"):
                generate_tpch(directory, 0)
            with self.assertRaisesRegex(SampleDataError, "YYYY-MM"):
                download_nyc_taxi(directory, ["2024-13"])

    @unittest.skipUnless(shutil.which("tpchgen-cli"), "tpchgen-cli is not installed")
    def test_tpch_generation_feeds_the_real_pipelines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = generate_tpch(root / "data", 0.01)
            self.assertEqual(manifest["row_counts"]["orders"], 15_000)
            registry = load_registry(REGISTRY)
            engineering = EngineeringService(registry, PROJECT_ROOT, root / "state")
            runner = PipelineRunner(registry, load_pipeline_project(PIPELINES), engineering, root / "data")
            runner.ingest()
            results = runner.run()
            self.assertEqual({result.status for result in results}, {RunStatus.SUCCEEDED}, [r.message for r in results])
            with self.assertRaisesRegex(SampleDataError, "--force"):
                generate_tpch(root / "data", 0.01)


class SpecParsingTests(unittest.TestCase):
    def test_range_checks_need_a_bound_and_paths_stay_relative(self) -> None:
        spec = yaml.safe_load((PIPELINES / "specs" / "gold_order_revenue.yaml").read_text())
        spec["quality_checks"] = [{"check": "range", "column": "net_revenue"}]
        with self.assertRaises(PipelineValidationError):
            parse_pipeline_spec(spec)
        from ai_data_platform.pipelines.models import SourceSpec

        with self.assertRaises(ValueError):
            SourceSpec(id="tpch.orders", description="x", format="parquet", path="../../etc/passwd", owner="x")


class ProjectConfigurationTests(unittest.TestCase):
    def test_malformed_sources_manifest_is_a_validation_error(self) -> None:
        for content in ("sources: [unclosed", "- a\n- b\n", "sources:\n", "sources: 5\n"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "specs").mkdir()
                (root / "sources.yaml").write_text(content)
                with self.assertRaises(PipelineValidationError):
                    load_pipeline_project(root)

    def test_ids_that_could_collide_as_physical_tables_are_rejected(self) -> None:
        from ai_data_platform.pipelines.models import SourceSpec

        for identifier in ("foo.bar__baz", "foo__bar.baz", "raw.tpch__lineitem", "gold.x_"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                SourceSpec(id=identifier, description="x", format="parquet", path="a.parquet", owner="o")
        spec = yaml.safe_load((PIPELINES / "specs" / "gold_order_revenue.yaml").read_text())
        spec["output"] = "gold.order__revenue"
        with self.assertRaises(PipelineValidationError):
            parse_pipeline_spec(spec)


if __name__ == "__main__":
    unittest.main()
