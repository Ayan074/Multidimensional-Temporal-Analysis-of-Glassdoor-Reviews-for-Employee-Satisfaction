"""
Configuration for Glassdoor Review Scraper
==========================================
Add new companies to COMPANIES dict. Each needs:
  - name: Display name
  - slug: URL slug on Glassdoor (from the reviews page URL)
  - employer_id: The E-code number (visible in Glassdoor review URLs)

To find a company's employer_id:
  1. Go to Glassdoor and search for the company
  2. Click on "Reviews"
  3. Look at the URL: .../CompanyName-Reviews-E{ID}.htm
  4. The number after 'E' is the employer_id
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ─── Load environment variables ───────────────────────────────────────────────
load_dotenv(Path(__file__).parent.parent / ".env")

GLASSDOOR_EMAIL = os.getenv("GLASSDOOR_EMAIL", "")
GLASSDOOR_PASSWORD = os.getenv("GLASSDOOR_PASSWORD", "")

# ─── Company Configurations ──────────────────────────────────────────────────
# Add more companies here as needed
COMPANIES = {
    "hilton": {
        "name": "Hilton",
        "slug": "Hilton",
        "employer_id": "330",
    },

    "synchrony": {
        "name": "Synchrony",
        "slug": "Synchrony",          
        "employer_id": "852160",      
    },


    "cisco": {
    "name": "Cisco",
    "slug": "Cisco",
    "employer_id": "1425",
    },
    

    "american_express": {
    "name": "American Express",
    "slug": "American-Express",
    "employer_id": "35",
},

    "wegmans_food_markets": {
    "name": "Wegmans Food Markets",
    "slug": "Wegmans-Food-Markets",
    "employer_id": "3042",
},

    "nvidia": {
    "name": "NVIDIA",
    "slug": "NVIDIA",
    "employer_id": "7633",
},

    "marriott_international": {
    "name": "Marriott International",
    "slug": "Marriott-International",
    "employer_id": "7790",
},



    "delta_air_lines": {
    "name": "Delta Air Lines",
    "slug": "Delta-Air-Lines",
    "employer_id": "197",
},

    "world_wide_technology": {
    "name": "World Wide Technology",
    "slug": "World-Wide-Technology",
    "employer_id": "9553",
},

    "pinnacle_financial_partners": {
    "name": "Pinnacle Financial Partners",
    "slug": "Pinnacle-Financial-Partners",
    "employer_id": "12049",
},

    "camden_property_trust": {
    "name": "Camden Property Trust",
    "slug": "Camden-Property-Trust",
    "employer_id": "2418",
},

    "david_weekley_homes": {
    "name": "David Weekley Homes",
    "slug": "David-Weekley-Homes",
    "employer_id": "6633",
},


    "plante_moran": {
    "name": "Plante Moran",
    "slug": "Plante-Moran",
    "employer_id": "11396",
},

    "baird": {
    "name": "Baird",
    "slug": "Baird",
    "employer_id": "19350",
},

    "kimley_horn": {
    "name": "Kimley-Horn",
    "slug": "Kimley-Horn",
    "employer_id": "15177",
},

    "credit_acceptance": {
    "name": "Credit Acceptance",
    "slug": "Credit-Acceptance",
    "employer_id": "2194",
},


    "progressive_insurance": {
    "name": "Progressive Insurance",
    "slug": "Progressive-Insurance",
    "employer_id": "546",
},


    "power_home_remodeling": {
    "name": "Power Home Remodeling",
    "slug": "Power-Home-Remodeling",
    "employer_id": "405781",
},

    "edward_jones": {
    "name": "Edward Jones",
    "slug": "Edward-Jones",
    "employer_id": "3161",
},
    "brightview_senior_living": {
    "name": "Brightview Senior Living",
    "slug": "Brightview-Senior-Living",
    "employer_id": "486610",
},
    "rocket": {
    "name": "Rocket",
    "slug": "Rocket",
    "employer_id": "7856",
},

    "the_cheesecake_factory": {
    "name": "The Cheesecake Factory",
    "slug": "The-Cheesecake-Factory",
    "employer_id": "2229",
},

    "abbvie": {
    "name": "AbbVie",
    "slug": "AbbVie",
    "employer_id": "649837",
},
    # Examples for future use:
    # "marriott": {
    #     "name": "Marriott International",
    #     "slug": "Marriott-International",
    #     "employer_id": "2565",
    # },
    # "hyatt": {
    #     "name": "Hyatt",
    #     "slug": "Hyatt",
    #     "employer_id": "1992",
    # },
}

# ─── Glassdoor URL Templates ─────────────────────────────────────────────────
BASE_URL = "https://www.glassdoor.com"
LOGIN_URL = f"{BASE_URL}/profile/login_input.htm"

def get_reviews_url(company_key: str, page: int = 1, sort_ascending: bool = False) -> str:
    """Build the Glassdoor reviews URL for a given company and page number.

    Parameters
    ----------
    sort_ascending : bool
        If True, sort reviews oldest-first (useful for resume — unscraped
        reviews appear on early pages instead of page 780+).
    """
    company = COMPANIES[company_key]
    slug = company["slug"]
    eid = company["employer_id"]
    if page == 1:
        url = f"{BASE_URL}/Reviews/{slug}-Reviews-E{eid}.htm"
    else:
        url = f"{BASE_URL}/Reviews/{slug}-Reviews-E{eid}_P{page}.htm"
    if sort_ascending:
        url += "?sort.sortType=RD&sort.ascending=true"
    return url

# ─── Scraper Settings ────────────────────────────────────────────────────────
# Delay between page loads (seconds) — randomized between MIN and MAX
MIN_DELAY = 5
MAX_DELAY = 10

# Number of reviews per page (Glassdoor default)
REVIEWS_PER_PAGE = 5

# Max retries per page on failure before skipping
MAX_RETRIES = 3

# Run browser without GUI (set False to watch it work — useful for debugging)
HEADLESS = False

# Save checkpoint every N pages (for resume capability)
CHECKPOINT_INTERVAL = 5

# Page load timeout in seconds
PAGE_LOAD_TIMEOUT = 45

# ─── Output Settings ─────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = Path(__file__).parent / "data"
CHECKPOINT_DIR = Path(__file__).parent / "data" / "checkpoints"

# Create directories if they don't exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
