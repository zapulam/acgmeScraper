"""
Convert an ACGME scraper checkpoint JSON file into a final CSV file.

Written by: zapulam
"""

from __future__ import annotations

import argparse
import csv
import json

from pathlib import Path
from typing import Any

from logger import print_banner, print_run_config, print_success
python scrape_acgme_contacts import CONTACT_COLUMNS, clean_email, clean_text, ensure_parent_dir


# --- Helper functions ---
def csv_row_key(
            row: dict[str, str],
    ) -> str:
    """Build the export-time de-duplication key for a normalized CSV row."""
    email_key = row["Email"].lower()
    if email_key:
        return f"email|{email_key}"
    return "|".join(
        [
            "blank",
            row["Program Code"].lower(),
            row["Role"].lower(),
            row["Name"].lower(),
        ]
    )


def dedupe_rows_for_csv(
            rows: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
    """Return export rows de-duplicated by email with merged multi-value fields."""
    deduped: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized = normalize_csv_row(row)
        key = csv_row_key(normalized)
        if key in deduped:
            merge_csv_rows(deduped[key], normalized)
            continue
        deduped[key] = normalized
    return list(deduped.values())


def load_existing_checkpoint(
            checkpoint_path: Path,
    ) -> dict[str, Any]:
    """Load an existing ACGME scraper checkpoint from disk."""
    if not checkpoint_path.exists():
        raise FileNotFoundError(f"Checkpoint file does not exist: {checkpoint_path}")
    with checkpoint_path.open("r", encoding="utf-8") as handle:
        checkpoint = json.load(handle)
    if not isinstance(checkpoint, dict):
        raise ValueError("Checkpoint JSON must contain an object at the top level.")
    checkpoint.setdefault("rows", [])
    return checkpoint


def raw_checkpoint_row_count(
            checkpoint: dict[str, Any],
    ) -> int:
    """Return the number of raw contact rows in a checkpoint."""
    rows = checkpoint.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError("Checkpoint field `rows` must be a list.")
    return len(rows)


def merge_csv_rows(
            existing_row: dict[str, str],
            incoming_row: dict[str, str],
    ) -> dict[str, str]:
    """Merge two normalized CSV rows that represent the same contact identity."""
    for column in CONTACT_COLUMNS:
        if column == "Email":
            if not existing_row[column]:
                existing_row[column] = incoming_row[column]
            continue
        existing_row[column] = merge_distinct_values(
            existing_row[column],
            incoming_row[column],
        )
    return existing_row


def merge_distinct_values(
            existing_value: Any,
            incoming_value: Any,
    ) -> str:
    """Merge two semicolon-delimited values while preserving first-seen order."""
    values: list[str] = []
    seen: set[str] = set()
    for value in split_merged_values(existing_value) + split_merged_values(incoming_value):
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return "; ".join(values)


def normalize_csv_row(
            row: dict[str, Any],
    ) -> dict[str, str]:
    """Normalize one raw checkpoint row to the final CSV schema."""
    normalized = {
        column: clean_text(str(row.get(column, "") or ""))
        for column in CONTACT_COLUMNS
    }
    normalized["Email"] = clean_email(normalized["Email"])
    return normalized


def split_merged_values(
            value: Any,
    ) -> list[str]:
    """Split a semicolon-merged export value into normalized parts."""
    return [
        clean_text(part)
        for part in str(value or "").split(";")
        if clean_text(part)
    ]


# --- Core functions ---
def write_checkpoint_csv(
        checkpoint: dict[str, Any],
        output_path: Path,
    ) -> int:
    """Write checkpoint contact rows to a CSV file and return the row count."""
    rows = checkpoint.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError("Checkpoint field `rows` must be a list.")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Each checkpoint row must be a JSON object.")
    deduped_rows = dedupe_rows_for_csv(rows)

    ensure_parent_dir(output_path)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CONTACT_COLUMNS)
        writer.writeheader()
        for row in deduped_rows:
            writer.writerow(row)
    return len(deduped_rows)


def convert_checkpoint_to_csv(
        checkpoint_path: Path,
        output_path: Path,
    ) -> int:
    """Convert one ACGME checkpoint JSON file into a final CSV file."""
    checkpoint = load_existing_checkpoint(checkpoint_path)
    return write_checkpoint_csv(checkpoint, output_path)


# --- CLI functions ---
def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for checkpoint CSV conversion."""
    parser = argparse.ArgumentParser(
        description="Convert an ACGME scraper checkpoint JSON file to CSV.",
    )
    parser.add_argument(
        "--checkpoint",
        default="data/acgme_checkpoint.json",
        help="Path to the scraper checkpoint JSON file.",
    )
    parser.add_argument(
        "--output",
        default="data/acgme_contacts.csv",
        help="Path to the CSV file to write.",
    )
    return parser


def main(
        argv: list[str] | None = None,
    ) -> int:
    """Parse CLI arguments and convert the checkpoint to CSV."""
    parser = build_parser()
    args = parser.parse_args(argv)
    checkpoint_path = Path(args.checkpoint)
    output_path = Path(args.output)

    print_banner("ACGME EXPORT")
    checkpoint = load_existing_checkpoint(checkpoint_path)
    raw_row_count = raw_checkpoint_row_count(checkpoint)
    print_run_config(
        "Export configuration",
        [
            ("Checkpoint", checkpoint_path),
            ("Output", output_path),
            ("Raw rows", raw_row_count),
        ],
    )
    row_count = write_checkpoint_csv(checkpoint, output_path)
    print_success(f"Wrote {row_count} deduped rows to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
