# Glassdoor Review Scraper — Employee Branding

Automated tool to scrape Glassdoor company reviews for employer branding research.

## 📋 Fields Collected

| Field | Type |
|---|---|
| Overall Rating | 1–5 stars |
| Review Title | Text |
| Pros | Free text |
| Cons | Free text |
| Advice to Management | Free text |
| CEO Approval | Approve / Disapprove / No Opinion |
| Business Outlook | Positive / Neutral / Negative |
| Recommend to a Friend | Yes / No |
| Career Opportunities | 1–5 stars |
| Compensation & Benefits | 1–5 stars |
| Culture & Values | 1–5 stars |
| Senior Management | 1–5 stars |
| Work–Life Balance | 1–5 stars |
| Diversity & Inclusion | 1–5 stars |
| Reviewer Job Title | Text |
| Reviewer Location | Text |
| Employment Status | Current / Former Employee |
| Review Date | Date |

## 🚀 Quick Start

### 1. Prerequisites
- **Python 3.10+** installed
- **Google Chrome** browser installed
- A **Glassdoor account** (free signup)

### 2. Setup

```bash
# Navigate to project folder
cd D:\Employee_Branding

# Activate virtual environment
.\venv\Scripts\activate

# Packages are already installed. If you need to reinstall:
pip install -r requirements.txt
```

### 3. Configure Credentials

Copy the example env file and add your Glassdoor login:

```bash
copy .env.example .env
```

Edit `.env` with your credentials:
```
GLASSDOOR_EMAIL=your_email@example.com
GLASSDOOR_PASSWORD=your_password
```

### 4. Run the Scraper

```bash
# Scrape ALL Hilton reviews (~12,000 reviews, ~1,200 pages)
python -m glassdoor_scraper.main

# Scrape only the first 10 pages (for testing)
python -m glassdoor_scraper.main --pages 10

# Watch the browser work (non-headless mode — useful for CAPTCHA)
python -m glassdoor_scraper.main --pages 10 --no-headless

# Resume a previously interrupted scrape
python -m glassdoor_scraper.main

# Start from a specific page
python -m glassdoor_scraper.main --start-page 50

# Export existing raw data to final CSV/Excel
python -m glassdoor_scraper.main --export-only

# Clear checkpoint and start fresh
python -m glassdoor_scraper.main --clear-checkpoint
```

## 📁 Output

Files are saved in `glassdoor_scraper/data/`:

| File | Description |
|---|---|
| `hilton_reviews_raw.csv` | Incremental raw data (appended during scraping) |
| `hilton_reviews_final_YYYYMMDD_HHMMSS.csv` | Final cleaned & deduplicated CSV |
| `hilton_reviews_final_YYYYMMDD_HHMMSS.xlsx` | Final cleaned & deduplicated Excel |

## 🔄 Adding New Companies

Edit `glassdoor_scraper/config.py` and add to the `COMPANIES` dict:

```python
COMPANIES = {
    "hilton": {
        "name": "Hilton",
        "slug": "Hilton",
        "employer_id": "330",
    },
    "marriott": {
        "name": "Marriott International",
        "slug": "Marriott-International",
        "employer_id": "2565",
    },
}
```

To find a company's `employer_id`:
1. Go to Glassdoor and search for the company
2. Click "Reviews"
3. Look at the URL: `.../CompanyName-Reviews-E{ID}.htm`
4. The number after `E` is the `employer_id`

Then run:
```bash
python -m glassdoor_scraper.main --company marriott
```

## ⚠️ Troubleshooting

### CAPTCHA / Blocked
- Use `--no-headless` mode to solve CAPTCHAs manually
- Add longer delays in `config.py` (`MIN_DELAY=8`, `MAX_DELAY=15`)
- Use a VPN or different network
- Run in shorter batches: `--pages 100`

### Login Failed
- Double-check your `.env` credentials
- Try logging in manually in Chrome first
- Use `--no-headless` to watch the login process

### No Reviews Found
- Glassdoor may have changed their HTML structure
- Check `glassdoor_scraper/data/logs/scraper.log` for details
- Try with `--verbose` flag for debug output

### Resume After Interruption
- Just run the same command again — it auto-resumes from checkpoint
- Use `--clear-checkpoint` to start over

## 📜 Disclaimer

This tool is for **academic research purposes only** (employee branding analysis). Always:
- Respect Glassdoor's Terms of Service
- Add reasonable delays between requests
- Do not redistribute scraped data commercially
- Comply with local data protection regulations
