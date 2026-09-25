from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import duckdb
import yaml

from ai_data_platform.engineering.crawler import SchemaCrawler
from ai_data_platform.engineering.crawler import validate_crawler_drafts
from ai_data_platform.validation import RegistryValidationError


class SchemaCrawlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = duckdb.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE gold_finance_revenue (fiscal_period VARCHAR, net_revenue_usd DECIMAL)"
        )
        self.connection.execute(
            "CREATE TABLE silver_customer_360 (customer_id INTEGER, churn_risk_score FLOAT)"
        )
        self.connection.execute("CREATE TABLE raw_stripe_events (event_id VARCHAR, payload JSON)")
        self.connection.execute(
            "CREATE TABLE tbl_sales_2023_final_v2 (txn_id VARCHAR, amt DECIMAL)"
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_crawl_creates_stable_drafts_for_messy_and_layered_tables(self) -> None:
        datasets = SchemaCrawler(self.connection).crawl_schema()

        self.assertEqual(
            [dataset.id for dataset in datasets],
            [
                "gold.finance_revenue",
                "raw.stripe_events",
                "silver.customer_360",
                "raw.tbl_sales_2023_final_v2",
            ],
        )
        finance = datasets[0]
        self.assertEqual(finance.domain, "finance")
        self.assertEqual(finance.certification.value, "uncertified")
        self.assertIn("fiscal_period (VARCHAR)", finance.description)
        self.assertIn("net_revenue_usd (DECIMAL", finance.description)

    def test_generated_yaml_is_portable_and_safe_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            files = SchemaCrawler(self.connection).generate_registry(Path(temp_dir))
            contents = Path(files[0]).read_text(encoding="utf-8")
            loaded = yaml.safe_load(contents)

        self.assertEqual(len(files), 4)
        self.assertEqual(loaded["certification"], "uncertified")
        self.assertNotIn("!!python", contents)

    def test_schema_name_is_bound_as_catalog_data(self) -> None:
        crawler = SchemaCrawler(self.connection)

        self.assertEqual(crawler.crawl_schema("main' OR 1=1 --"), [])

    def test_generated_file_name_cannot_escape_the_dataset_directory(self) -> None:
        self.connection.execute('CREATE TABLE "raw_event/2026" (event_id VARCHAR)')
        with tempfile.TemporaryDirectory() as temp_dir:
            files = SchemaCrawler(self.connection).generate_registry(Path(temp_dir))

            self.assertTrue(all(Path(file).parent == Path(temp_dir) / "datasets" for file in files))

    def test_profile_reports_duplicate_identifiers_and_column_types(self) -> None:
        self.connection.execute(
            "INSERT INTO raw_stripe_events VALUES ('evt_1', '{}'), ('evt_1', '{}')"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            SchemaCrawler(self.connection).generate_registry(Path(temp_dir))
            report = yaml.safe_load((Path(temp_dir) / "crawl_report.yaml").read_text())

        stripe = next(
            item
            for item in report["table_observations"]
            if item["dataset_id"] == "raw.stripe_events"
        )
        event_id = next(
            item
            for item in stripe["observed_facts"]["profile"]["columns"]
            if item["column"] == "event_id"
        )
        payload = next(
            item for item in stripe["observed_facts"]["columns"] if item["name"] == "payload"
        )
        self.assertEqual(event_id["profiled_duplicate_value_count"], 1)
        self.assertEqual(payload["data_type"], "JSON")
        self.assertEqual(stripe["observed_facts"]["profile"]["total_rows"], 2)
        self.assertIn("heuristic_assessments", stripe)

    def test_large_table_uses_repeatable_reservoir_sample(self) -> None:
        self.connection.execute(
            "CREATE TABLE raw_profile_rows AS SELECT i AS row_id FROM range(1000) t(i)"
        )
        first = SchemaCrawler(self.connection, max_profile_rows=25, profile_seed=7)
        with (
            tempfile.TemporaryDirectory() as first_dir,
            tempfile.TemporaryDirectory() as second_dir,
        ):
            first.generate_registry(Path(first_dir))
            first.generate_registry(Path(second_dir))
            report_a = yaml.safe_load((Path(first_dir) / "crawl_report.yaml").read_text())
            report_b = yaml.safe_load((Path(second_dir) / "crawl_report.yaml").read_text())

        profile_a = next(
            item["observed_facts"]["profile"]
            for item in report_a["table_observations"]
            if item["dataset_id"] == "raw.profile_rows"
        )
        profile_b = next(
            item["observed_facts"]["profile"]
            for item in report_b["table_observations"]
            if item["dataset_id"] == "raw.profile_rows"
        )
        self.assertEqual(profile_a["profile_method"], "reservoir_sample_without_replacement")
        self.assertEqual(profile_a["profiled_rows"], 25)
        self.assertFalse(profile_a["profile_complete"])
        self.assertEqual(profile_a["sampling_seed"], 7)
        self.assertEqual(profile_a["columns"], profile_b["columns"])

    def test_edited_drafts_validate_semantic_references_against_base_registry(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            draft_dir = Path(temp_dir) / "review"
            datasets = draft_dir / "datasets"
            datasets.mkdir(parents=True)
            (datasets / "customer.yaml").write_text(
                """id: silver.demo_customer
name: Demo customer
domain: customer
layer: silver
description: Synthetic demo contract.
grain: customer
certification: uncertified
owner: curator input required
entity_ids: [entity.customer]
dimension_concept_ids: [business_concept.customer]
freshness_sla: unknown
refresh_cadence: unknown
""",
                encoding="utf-8",
            )
            valid_ids = validate_crawler_drafts(draft_dir, root / "registry")
            self.assertEqual(valid_ids, ["silver.demo_customer"])

            draft = datasets / "customer.yaml"
            draft.write_text(
                draft.read_text(encoding="utf-8").replace(
                    "entity.customer", "entity.does_not_exist"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RegistryValidationError, "entity.does_not_exist"):
                validate_crawler_drafts(draft_dir, root / "registry")

    def test_existing_governed_contract_is_preserved_and_differences_reported(self) -> None:
        root = Path(__file__).resolve().parents[1]
        expected = yaml.safe_load(
            (root / "registry" / "datasets" / "gold_finance_revenue.yaml").read_text()
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            SchemaCrawler(self.connection).generate_registry(
                Path(temp_dir), existing_registry_directory=root / "registry"
            )
            preserved = yaml.safe_load(
                (Path(temp_dir) / "datasets" / "gold_finance_revenue.yaml").read_text()
            )
            report = yaml.safe_load((Path(temp_dir) / "crawl_report.yaml").read_text())

        self.assertEqual(preserved, expected)
        finance = next(
            item
            for item in report["dataset_reviews"]
            if item["dataset_id"] == "gold.finance_revenue"
        )
        self.assertEqual(finance["status"], "existing_contract_preserved")
        self.assertIn("product", finance["grain_terms_without_matching_columns"])
        self.assertEqual(len(report["existing_registry_only_dataset_ids"]), 6)

    def test_output_cannot_target_existing_registry_tree(self) -> None:
        root = Path(__file__).resolve().parents[1]

        with self.assertRaisesRegex(ValueError, "inside it"):
            SchemaCrawler(self.connection).generate_registry(
                root / "registry", existing_registry_directory=root / "registry"
            )


if __name__ == "__main__":
    unittest.main()
