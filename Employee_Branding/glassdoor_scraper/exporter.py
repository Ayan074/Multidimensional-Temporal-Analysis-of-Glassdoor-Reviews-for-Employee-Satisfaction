"""
Glassdoor Data Exporter
=======================
Handles saving scraped review data to CSV and Excel formats.
Supports incremental saves and deduplication.
"""

import hashlib
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import config

logger = logging.getLogger(__name__)

# Column order for output files
COLUMN_ORDER = [
    "company_name",
    "review_date",
    "overall_rating",
    "review_title",
    "pros",
    "cons",
    "advice_to_management",
    "ceo_approval",
    "business_outlook",
    "recommend_to_friend",
    "career_opportunities",
    "compensation_benefits",
    "culture_values",
    "senior_management",
    "work_life_balance",
    "diversity_inclusion",
    "reviewer_job_title",
    "reviewer_location",
    "employment_status",
    "page_number",
    "scrape_timestamp",
]


def _generate_review_hash(review: dict) -> str:
    """Generate a hash for deduplication based on review content."""
    key_parts = [
        str(review.get("overall_rating", "")),
        review.get("pros", ""),
        review.get("cons", ""),
        review.get("review_date", ""),
        review.get("reviewer_job_title", ""),
    ]
    combined = "|".join(key_parts)
    return hashlib.md5(combined.encode("utf-8")).hexdigest()


def clean_reviews(reviews: list[dict]) -> list[dict]:
    """
    Clean and normalize review data.

    - Strip whitespace from text fields
    - Normalize ratings to float
    - Remove reviews that are clearly duplicates
    """
    cleaned = []
    seen_hashes = set()

    for review in reviews:
        # Strip whitespace from all string fields
        for key, value in review.items():
            if isinstance(value, str):
                review[key] = value.strip()

        # Normalize rating fields to float
        rating_fields = [
            "overall_rating", "career_opportunities", "compensation_benefits",
            "culture_values", "senior_management", "work_life_balance",
            "diversity_inclusion",
        ]
        for field in rating_fields:
            val = review.get(field)
            if val is not None:
                try:
                    review[field] = float(val)
                except (ValueError, TypeError):
                    review[field] = None

        # Deduplication
        review_hash = _generate_review_hash(review)
        if review_hash not in seen_hashes:
            seen_hashes.add(review_hash)
            cleaned.append(review)

    return cleaned


def save_reviews_incremental(
    reviews: list[dict],
    company_name: str,
    page_number: int,
    output_dir: Path = None,
) -> Path:
    """
    Append reviews to the incremental CSV file.
    Used during scraping to save progress continuously.

    Returns the path to the CSV file.
    """
    if output_dir is None:
        output_dir = config.DATA_DIR

    csv_path = output_dir / f"{company_name.lower()}_reviews_raw.csv"

    # Add metadata to each review
    timestamp = datetime.now().isoformat()
    for review in reviews:
        review["company_name"] = company_name
        review["page_number"] = page_number
        review["scrape_timestamp"] = timestamp

    df = pd.DataFrame(reviews)

    # Reorder columns (only include columns that exist)
    cols = [c for c in COLUMN_ORDER if c in df.columns]
    extra_cols = [c for c in df.columns if c not in COLUMN_ORDER]
    df = df[cols + extra_cols]

    # Append to existing file or create new one
    if csv_path.exists():
        df.to_csv(csv_path, mode="a", header=False, index=False, encoding="utf-8-sig")
    else:
        df.to_csv(csv_path, mode="w", header=True, index=False, encoding="utf-8-sig")

    return csv_path


def export_final(
    company_name: str,
    output_format: str = "both",
    output_dir: Path = None,
) -> dict:
    """
    Produce the final cleaned output files from the raw incremental CSV.

    Parameters
    ----------
    company_name : str
        Company name (used for filenames).
    output_format : str
        'csv', 'excel', or 'both'.
    output_dir : Path
        Output directory. Defaults to config.DATA_DIR.

    Returns
    -------
    dict with keys 'csv' and/or 'excel' containing output file paths.
    """
    if output_dir is None:
        output_dir = config.DATA_DIR

    raw_csv = output_dir / f"{company_name.lower()}_reviews_raw.csv"

    if not raw_csv.exists():
        logger.error(f"Raw CSV not found: {raw_csv}")
        return {}

    logger.info(f"Reading raw data from {raw_csv}")
    df = pd.read_csv(raw_csv, encoding="utf-8-sig")

    # Deduplicate
    initial_count = len(df)
    key_cols = ["overall_rating", "pros", "cons", "review_date", "reviewer_job_title"]
    existing_cols = [c for c in key_cols if c in df.columns]
    if existing_cols:
        df = df.drop_duplicates(subset=existing_cols, keep="first")
    dedup_count = initial_count - len(df)
    if dedup_count > 0:
        logger.info(f"Removed {dedup_count} duplicate reviews")

    # Reorder columns
    cols = [c for c in COLUMN_ORDER if c in df.columns]
    extra_cols = [c for c in df.columns if c not in COLUMN_ORDER]
    df = df[cols + extra_cols]

    output_paths = {}
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if output_format in ("csv", "both"):
        csv_path = output_dir / f"{company_name.lower()}_reviews_final_{timestamp}.csv"
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        output_paths["csv"] = csv_path
        logger.info(f"Exported {len(df)} reviews to CSV: {csv_path}")

    if output_format in ("excel", "both"):
        excel_path = output_dir / f"{company_name.lower()}_reviews_final_{timestamp}.xlsx"
        df.to_excel(excel_path, index=False, engine="openpyxl", sheet_name="Reviews")
        output_paths["excel"] = excel_path
        logger.info(f"Exported {len(df)} reviews to Excel: {excel_path}")

    # Print summary
    print(f"\n{'='*60}")
    print(f"  EXPORT SUMMARY -- {company_name}")
    print(f"{'='*60}")
    print(f"  Total reviews exported : {len(df)}")
    print(f"  Duplicates removed     : {dedup_count}")
    for fmt, path in output_paths.items():
        print(f"  {fmt.upper()} file            : {path}")
    print(f"{'='*60}\n")

    return output_paths
