"""Reproducible sample datasets for local pipeline and analytics demonstrations.

Sample data is written below a caller-supplied data directory (``var/data`` by default),
never into the Git registry. Each loader records a ``manifest.json`` describing exactly
what was produced so demo results can be traced to their inputs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import duckdb

TPCH_TABLES = ("region", "nation", "supplier", "customer", "part", "partsupp", "orders", "lineitem")
NYC_TLC_BASE_URL = "https://d37ci6vzurychx.cloudfront.net"
_MONTH = re.compile(r"^(20[0-9]{2})-(0[1-9]|1[0-2])$")


class SampleDataError(RuntimeError):
    """Raised when sample data cannot be produced as requested."""


def _tpchgen_executable() -> str:
    candidates = [
        shutil.which("tpchgen-cli"),
        str(Path(sys.executable).with_name("tpchgen-cli")),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise SampleDataError(
        "tpchgen-cli is not installed; run: python -m pip install -e '.[data]'"
    )


def _parquet_row_counts(paths: dict[str, Path]) -> dict[str, int]:
    with duckdb.connect() as connection:
        return {
            name: connection.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(path)]).fetchone()[0]
            for name, path in paths.items()
        }


def _write_manifest(directory: Path, manifest: dict[str, object]) -> Path:
    path = directory / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _require_replaceable(directory: Path, force: bool) -> None:
    if directory.exists() and any(directory.iterdir()) and not force:
        raise SampleDataError(f"{directory} already contains data; pass --force to replace it")


def generate_tpch(data_directory: str | Path, scale_factor: float = 1.0, *, force: bool = False) -> dict:
    """Generate TPC-H tables as Parquet using the tpchgen-rs reference generator.

    Scale factor 1 produces about 6 million line items (~350 MB of Parquet).
    """
    if not 0.001 <= scale_factor <= 100:
        raise SampleDataError("scale_factor must be between 0.001 and 100")
    output = Path(data_directory).resolve() / "tpch"
    _require_replaceable(output, force)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    command = [
        _tpchgen_executable(),
        "parquet",
        "--scale-factor",
        f"{scale_factor:g}",
        "--output-dir",
        str(output),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=3_600)
    if completed.returncode != 0:
        raise SampleDataError(f"tpchgen-cli failed: {(completed.stderr or completed.stdout)[-2_000:]}")
    files = {table: output / f"{table}.parquet" for table in TPCH_TABLES}
    missing = [table for table, path in files.items() if not path.is_file()]
    if missing:
        raise SampleDataError(f"tpchgen-cli did not produce: {', '.join(missing)}")
    version = subprocess.run(
        [command[0], "--version"], check=False, capture_output=True, text=True
    ).stdout.strip()
    manifest = {
        "dataset": "tpch",
        "generator": version or "tpchgen-cli",
        "scale_factor": scale_factor,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "row_counts": _parquet_row_counts(files),
        "bytes": {table: path.stat().st_size for table, path in files.items()},
    }
    _write_manifest(output, manifest)
    return manifest


def _download(url: str, destination: Path, timeout: int) -> None:
    partial = destination.with_suffix(destination.suffix + ".partial")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response, partial.open("wb") as file:
            shutil.copyfileobj(response, file, length=1 << 20)
    except OSError as error:
        partial.unlink(missing_ok=True)
        raise SampleDataError(
            f"could not download {url}: {error}. If this runs in a restricted network, allow "
            f"the host {NYC_TLC_BASE_URL.removeprefix('https://')}."
        ) from error
    partial.replace(destination)


def download_nyc_taxi(
    data_directory: str | Path,
    months: list[str],
    *,
    force: bool = False,
    timeout: int = 300,
) -> dict:
    """Download NYC TLC yellow-taxi trip records (public Parquet) and the zone lookup."""
    if not months or len(months) > 24:
        raise SampleDataError("request between 1 and 24 months, formatted YYYY-MM")
    invalid = [month for month in months if not _MONTH.fullmatch(month)]
    if invalid:
        raise SampleDataError(f"months must be formatted YYYY-MM: {', '.join(invalid)}")
    output = Path(data_directory).resolve() / "nyc_taxi"
    _require_replaceable(output, force)
    if output.exists():
        shutil.rmtree(output)
    trips = output / "yellow_tripdata"
    trips.mkdir(parents=True)
    files: dict[str, Path] = {}
    for month in sorted(dict.fromkeys(months)):
        destination = trips / f"yellow_tripdata_{month}.parquet"
        _download(f"{NYC_TLC_BASE_URL}/trip-data/{destination.name}", destination, timeout)
        files[month] = destination
    _download(f"{NYC_TLC_BASE_URL}/misc/taxi_zone_lookup.csv", output / "taxi_zone_lookup.csv", timeout)
    manifest = {
        "dataset": "nyc_taxi",
        "source": f"{NYC_TLC_BASE_URL} (NYC Taxi & Limousine Commission trip record data)",
        "months": list(files),
        "downloaded_at": datetime.now(tz=UTC).isoformat(),
        "row_counts": _parquet_row_counts(files),
        "bytes": {month: path.stat().st_size for month, path in files.items()},
    }
    _write_manifest(output, manifest)
    return manifest
