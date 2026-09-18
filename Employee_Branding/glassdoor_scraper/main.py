"""
Glassdoor Review Scraper -- Main Entry Point
============================================
CLI tool to scrape Glassdoor reviews for employee branding research.

Usage:
    python main.py                                    # Scrape all Hilton reviews
    python main.py --company hilton --pages 10        # Scrape first 10 pages
    python main.py --company hilton --start-page 50   # Start from page 50
    python main.py --company hilton --no-headless     # Watch the browser
    python main.py --export-only                      # Just export existing raw data
"""

import argparse
import logging
import sys
import os
from pathlib import Path

# Force UTF-8 output on Windows terminals
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# Add parent directory to path so we can run from the glassdoor_scraper folder
sys.path.insert(0, str(Path(__file__).parent.parent))

from glassdoor_scraper.scraper import GlassdoorScraper
from glassdoor_scraper.exporter import export_final
from glassdoor_scraper import config


def setup_logging(verbose: bool = False):
    """Configure logging for the scraper."""
    level = logging.DEBUG if verbose else logging.INFO
    
    # Create log directory
    log_dir = config.DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # File handler
    file_handler = logging.FileHandler(
        log_dir / "scraper.log",
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    
    # Formatter
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    # Suppress noisy loggers
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("undetected_chromedriver").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(
        description="Glassdoor Review Scraper - Employee Branding Data Collection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                    Scrape all Hilton reviews
  %(prog)s --company hilton --pages 10        Scrape first 10 pages only
  %(prog)s --company hilton --start-page 50   Resume/start from page 50
  %(prog)s --company hilton --no-headless     Watch the browser work
  %(prog)s --export-only --company hilton     Export existing raw data to final CSV/Excel
  %(prog)s --clear-checkpoint                 Clear saved progress and start fresh
        """,
    )

    parser.add_argument(
        "--company", "-c",
        type=str,
        default="hilton",
        choices=list(config.COMPANIES.keys()),
        help=f"Company to scrape (default: hilton). Available: {list(config.COMPANIES.keys())}",
    )
    parser.add_argument(
        "--pages", "-p",
        type=int,
        default=0,
        help="Maximum number of pages to scrape (default: 0 = all pages)",
    )
    parser.add_argument(
        "--start-page", "-s",
        type=int,
        default=0,
        help="Start scraping from this page (default: 0 = auto-resume from checkpoint)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="Run browser with visible GUI (useful for debugging / CAPTCHA solving)",
    )
    parser.add_argument(
        "--output-format", "-o",
        type=str,
        default="both",
        choices=["csv", "excel", "both"],
        help="Output format (default: both)",
    )
    parser.add_argument(
        "--export-only",
        action="store_true",
        help="Skip scraping; just export existing raw data to final output",
    )
    parser.add_argument(
        "--clear-checkpoint",
        action="store_true",
        help="Clear the checkpoint file and start fresh",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save page HTML and screenshots to data/debug/ for troubleshooting",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose (debug) logging",
    )

    args = parser.parse_args()
    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    company_name = config.COMPANIES[args.company]["name"]

    print(r"""
   ____  _                     _                   ____                                 
  / ___|| | __ _ ___ ___  __ _| | ___   ___  _ __ / ___|  ___ _ __ __ _ _ __   ___ _ __ 
 | |  _ | |/ _` / __/ __|/ _` | |/ _ \ / _ \| '__\___ \ / __| '__/ _` | '_ \ / _ \ '__|
 | |_| || | (_| \__ \__ \ (_| | | (_) | (_) | |   ___) | (__| | | (_| | |_) |  __/ |   
  \____||_|\__,_|___/___/\__,_|_|\___/ \___/|_|  |____/ \___|_|  \__,_| .__/ \___|_|   
                                                                       |_|              
    Employee Branding -- Data Collection Tool
    """)

    print(f"  Company  : {company_name}")
    print(f"  Output   : {config.DATA_DIR}")
    print(f"  Format   : {args.output_format}")
    print()

    # Handle clear checkpoint
    if args.clear_checkpoint:
        scraper = GlassdoorScraper(args.company)
        scraper.clear_checkpoint()
        print("[OK] Checkpoint cleared. Will start from page 1.")
        if args.export_only:
            pass  # Continue to export
        else:
            # If only clearing checkpoint, don't exit - continue to scrape
            pass

    # Export only mode
    if args.export_only:
        print("[EXPORT] Export-only mode: generating final output from raw data...\n")
        paths = export_final(
            company_name=company_name,
            output_format=args.output_format,
        )
        if paths:
            print("[OK] Export complete!")
            for fmt, path in paths.items():
                print(f"   {fmt.upper()}: {path}")
        else:
            print("[ERROR] No raw data found. Run the scraper first.")
        return

    # Scrape mode
    headless = not args.no_headless  # --no-headless means headless=False

    try:
        scraper = GlassdoorScraper(
            company_key=args.company,
            headless=headless,
            debug=args.debug,
        )

        raw_csv = scraper.run(
            max_pages=args.pages,
            start_page=args.start_page,
            output_format=args.output_format,
        )

        if raw_csv:
            print("\n[EXPORT] Generating final deduplicated output...\n")
            paths = export_final(
                company_name=company_name,
                output_format=args.output_format,
            )
            if paths:
                print("[OK] All done! Your data is ready:")
                for fmt, path in paths.items():
                    print(f"   {fmt.upper()}: {path}")
            print(f"\n[TIP] Open the Excel file in your spreadsheet app for analysis.")

    except KeyboardInterrupt:
        print("\n\n[STOP] Interrupted. Your progress has been saved.")
        print(f"   Run again to resume: python main.py --company {args.company}")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        print(f"\n[ERROR] {e}")
        print(f"   Check logs at: {config.DATA_DIR / 'logs' / 'scraper.log'}")
        sys.exit(1)


if __name__ == "__main__":
    main()
