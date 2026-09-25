"""Seed an optional demo DuckDB source and crawl it into an editable review area."""

from __future__ import annotations

import json
import argparse
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from ai_data_platform.engineering.crawler import SchemaCrawler


def populate_demo_data(connection: duckdb.DuckDBPyConnection) -> None:
    """Create data with ordinary operational rough edges, deterministically."""
    for table in (
        "gold_finance_revenue",
        "tbl_sales_2023_final_v2",
        "raw_stripe_events",
        "silver_customer_360",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")

    connection.execute("""
        CREATE TABLE gold_finance_revenue (
            fiscal_period VARCHAR,
            legal_entity VARCHAR,
            region VARCHAR,
            product VARCHAR,
            net_revenue_usd DECIMAL(18, 2),
            loaded_at TIMESTAMP
        )
    """)
    connection.execute("""
        CREATE TABLE tbl_sales_2023_final_v2 (
            txn_id VARCHAR,
            cust_id INTEGER,
            amt DECIMAL(10, 2),
            currency_cd VARCHAR,
            ts TIMESTAMP,
            source_file VARCHAR
        )
    """)
    connection.execute("""
        CREATE TABLE raw_stripe_events (
            event_id VARCHAR,
            type VARCHAR,
            payload JSON,
            created BIGINT,
            ingested_at TIMESTAMP
        )
    """)
    connection.execute("""
        CREATE TABLE silver_customer_360 (
            customer_id INTEGER,
            first_name VARCHAR,
            last_name VARCHAR,
            lifetime_value DECIMAL(12, 2),
            churn_risk_score FLOAT,
            updated_at TIMESTAMP
        )
    """)

    randomizer = random.Random(20260923)
    base_time = datetime(2026, 9, 1, 9, tzinfo=UTC)
    finance_rows = []
    for period in ("2026-07", "2026-08"):
        for entity in ("US01", "UK01"):
            for region in ("NA", "EMEA"):
                for product in ("core", "addons"):
                    revenue = round(randomizer.uniform(85_000, 160_000), 2)
                    finance_rows.append((period, entity, region, product, revenue, base_time))
    # A credit memo and an as-yet-unexplained gap are both realistic outcomes.
    finance_rows[2] = (*finance_rows[2][:4], -4_250.00, base_time)
    finance_rows[12] = (*finance_rows[12][:4], None, base_time + timedelta(days=1))
    connection.executemany(
        "INSERT INTO gold_finance_revenue VALUES (?, ?, ?, ?, ?, ?)", finance_rows
    )

    sales_rows = []
    for index in range(18):
        timestamp = base_time + timedelta(hours=index * 7)
        sales_rows.append(
            (
                f"TXN-{1000 + index}",
                200 + index % 9,
                round(randomizer.uniform(15, 850), 2),
                "USD" if index % 4 else "EUR",
                timestamp,
                f"sales_extract_{timestamp:%Y%m%d}.csv",
            )
        )
    # Source corrections commonly retain a duplicate identifier; one record is incomplete.
    sales_rows.append(("TXN-1004", 204, 99.99, "USD", base_time, "sales_fixup.csv"))
    sales_rows.append(("TXN-1019", None, 47.50, "US$", None, "manual_upload.csv"))
    connection.executemany(
        "INSERT INTO tbl_sales_2023_final_v2 VALUES (?, ?, ?, ?, ?, ?)", sales_rows
    )

    stripe_rows = []
    for index in range(14):
        created_at = base_time + timedelta(minutes=index * 11)
        payload = {
            "id": f"pi_{5000 + index}",
            "amount": int(randomizer.uniform(500, 30_000)),
            "currency": "usd",
            "customer": f"cus_{200 + index % 7}",
        }
        stripe_rows.append(
            (
                f"evt_{index:03}",
                "payment_intent.succeeded",
                json.dumps(payload),
                int(created_at.timestamp()),
                created_at + timedelta(minutes=randomizer.randint(1, 45)),
            )
        )
    # A replayed webhook and a producer version that changes epoch units.
    stripe_rows.append((*stripe_rows[7][:4], stripe_rows[7][4] + timedelta(hours=3)))
    stripe_rows.append(
        (
            "evt_014",
            "charge.refunded",
            json.dumps({"id": "re_9001", "amount": 1299, "reason": "requested_by_customer"}),
            int((base_time + timedelta(hours=4)).timestamp() * 1000),
            base_time + timedelta(hours=4, minutes=6),
        )
    )
    connection.executemany("INSERT INTO raw_stripe_events VALUES (?, ?, ?, ?, ?)", stripe_rows)

    customer_rows = [
        (200, "Ava", "Patel", 1042.35, 0.12, base_time),
        (201, "Noah", "Kim", 0.00, 0.87, base_time),
        (202, "Mia", None, 351.10, 0.33, base_time),
        (203, "Liam", "Jones", None, None, base_time),
        (204, "Emma", "Garcia", 899.99, 1.08, base_time),
        (204, "Emma", "Garcia", 925.00, 0.71, base_time + timedelta(days=2)),
    ]
    connection.executemany(
        "INSERT INTO silver_customer_360 VALUES (?, ?, ?, ?, ?, ?)", customer_rows
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="DuckDB database file.")
    parser.add_argument("--schema", required=True, help="DuckDB schema to crawl.")
    parser.add_argument("--registry", type=Path, required=True, help="Canonical registry path.")
    parser.add_argument("--output", type=Path, required=True, help="New or empty review directory.")
    parser.add_argument(
        "--seed-demo", action="store_true", help="Create the synthetic demo source first."
    )
    parser.add_argument("--profile-rows", type=int, default=10_000)
    parser.add_argument("--sample-seed", type=int, default=42)
    args = parser.parse_args()

    source = args.source.resolve()
    output = args.output.resolve()
    if args.seed_demo:
        if source.exists():
            parser.error(f"refusing to overwrite existing demo source: {source}")
        source.parent.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect(str(source))
        try:
            populate_demo_data(connection)
        finally:
            connection.close()
    elif not source.is_file():
        parser.error(f"DuckDB source does not exist or is not a file: {source}")
    if output.exists() and any(output.iterdir()):
        parser.error(f"crawler output must be a new or empty directory: {output}")

    connection = duckdb.connect(str(source), read_only=True)
    try:
        generated_files = SchemaCrawler(
            connection,
            max_profile_rows=args.profile_rows,
            profile_seed=args.sample_seed,
        ).generate_registry(
            output,
            schema_name=args.schema,
            existing_registry_directory=args.registry,
        )
    finally:
        connection.close()

    print(f"Crawled {source} schema {args.schema} into review area {output}.")
    print("Generated editable contract drafts:")
    for path in generated_files:
        print(f"- {path}")
    print(f"Comparison and profiling report: {output / 'crawl_report.yaml'}")


if __name__ == "__main__":
    main()
