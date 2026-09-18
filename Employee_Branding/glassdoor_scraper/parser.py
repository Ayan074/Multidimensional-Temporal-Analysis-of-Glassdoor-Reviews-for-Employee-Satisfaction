"""
Glassdoor Review Parser (v4 - all fields verified)
===================================================
Based on actual Glassdoor HTML analysis (July 2026):

Selectors:
  - Reviews:     article[data-test="review-detail"]
  - Rating:      span[data-test="review-rating-label"]  
  - Title:       a.ContentTitle_link  OR  h3
  - Pros:        span[data-test="review-text-PROS"]
  - Cons:        span[data-test="review-text-CONS"]
  - Advice:      span[data-test="review-text-undefined"]
  - Job:         a[data-test="content-avatar-label"]  
  - Status:      div[data-test="content-avatar-tag"]
  - Date:        span.Timestamp_reviewDate
  - Sentiment:   div.ExperienceRating_container (class has positive_/negative_/neutral_/noData_)
  - Subratings:  Need to click caret to expand (JS click in scraper)
"""

import re
import logging
from bs4 import BeautifulSoup, Tag
from typing import Optional

logger = logging.getLogger(__name__)


def _safe_text(element: Optional[Tag], default: str = "") -> str:
    if element is None:
        return default
    return element.get_text(strip=True)


def parse_review(review_el: Tag) -> dict:
    """Parse a single review <article> element."""
    review = {}

    # ---- Review ID (from data-brandviews attribute) ----
    # Format: "MODULE:n=employee-reviews:eid=330:review_id=104724687"
    brandviews = review_el.get("data-brandviews", "")
    rid_match = re.search(r"review_id=(\d+)", brandviews)
    review["review_id"] = rid_match.group(1) if rid_match else ""

    # ---- Overall Rating ----
    rating_el = review_el.select_one('[data-test="review-rating-label"]')
    if rating_el:
        try:
            review["overall_rating"] = float(_safe_text(rating_el))
        except ValueError:
            review["overall_rating"] = None
    else:
        review["overall_rating"] = None

    # ---- Review Title ----
    title_el = (
        review_el.select_one('a[class*="ContentTitle"]')
        or review_el.select_one("h3")
        or review_el.select_one('a[href*="reviewDetail"]')
        or review_el.select_one('a[href*="Review"]')
    )
    review["review_title"] = _safe_text(title_el)

    # ---- Pros ----
    pros_el = review_el.select_one('[data-test="review-text-PROS"]')
    review["pros"] = _safe_text(pros_el)

    # ---- Cons ----
    cons_el = review_el.select_one('[data-test="review-text-CONS"]')
    review["cons"] = _safe_text(cons_el)

    # ---- Advice to Management ----
    advice_el = review_el.select_one('[data-test="review-text-undefined"]')
    review["advice_to_management"] = _safe_text(advice_el)

    # ---- Sentiment: Recommend / CEO / Outlook ----
    # Each is a div with class "ExperienceRating_container__..."
    # The class also contains positive_/negative_/neutral_/noData_
    review["ceo_approval"] = ""
    review["business_outlook"] = ""
    review["recommend_to_friend"] = ""

    sentiment_container = review_el.select_one('[class*="ExperienceRatings_experienceContainer"]')
    if sentiment_container:
        sentiment_items = sentiment_container.select('[class*="ExperienceRating_container"]')
        for item in sentiment_items:
            label = _safe_text(item.select_one('[class*="ExperienceRating_label"]')).lower()
            classes = " ".join(item.get("class", []))

            if "positive" in classes:
                sentiment = "Positive"
            elif "negative" in classes:
                sentiment = "Negative"
            elif "neutral" in classes:
                sentiment = "Neutral"
            elif "noData" in classes:
                sentiment = "No Opinion"
            else:
                sentiment = ""

            if "recommend" in label:
                if sentiment == "Positive":
                    review["recommend_to_friend"] = "Yes"
                elif sentiment == "Negative":
                    review["recommend_to_friend"] = "No"
                else:
                    review["recommend_to_friend"] = sentiment
            elif "ceo" in label:
                if sentiment == "Positive":
                    review["ceo_approval"] = "Approve"
                elif sentiment == "Negative":
                    review["ceo_approval"] = "Disapprove"
                else:
                    review["ceo_approval"] = sentiment
            elif "outlook" in label:
                review["business_outlook"] = sentiment

    # ---- Sub-Ratings ----
    # NOTE: Sub-ratings are NOT in the static HTML. They appear in a dynamic
    # tooltip when hovering over the caret element. Extraction is handled by
    # the scraper (Selenium ActionChains hover) and merged into the review
    # dict after parsing. We just set defaults here.
    review["career_opportunities"] = None
    review["compensation_benefits"] = None
    review["culture_values"] = None
    review["senior_management"] = None
    review["work_life_balance"] = None
    review["diversity_inclusion"] = None

    # ---- Job Title ----
    job_el = (
        review_el.select_one('[data-test="content-avatar-label"]')
        or review_el.select_one('a[class*="avatarLabel"]')
    )
    review["reviewer_job_title"] = _safe_text(job_el)

    # ---- Location & Employment Status ----
    review["reviewer_location"] = ""
    review["employment_status"] = ""

    tag_els = review_el.select('[data-test="content-avatar-tag"]')
    for tag_el in tag_els:
        text = _safe_text(tag_el)
        if "employee" in text.lower() or "current" in text.lower() or "former" in text.lower():
            if "current" in text.lower():
                review["employment_status"] = "Current Employee"
            elif "former" in text.lower():
                review["employment_status"] = "Former Employee"
            else:
                review["employment_status"] = text
        else:
            # Likely a location
            if text and not review["reviewer_location"]:
                review["reviewer_location"] = text

    # Also check for location links
    if not review["reviewer_location"]:
        loc_el = review_el.select_one('a[class*="LinkWrapper"]')
        if loc_el:
            review["reviewer_location"] = _safe_text(loc_el)

    # ---- Review Date ----
    review["review_date"] = ""
    # Primary: span with Timestamp_reviewDate class
    date_el = review_el.select_one('[class*="Timestamp_reviewDate"]')
    if date_el:
        review["review_date"] = _safe_text(date_el)
    else:
        # Fallback: look in ReviewDetail_metrics
        metrics_el = review_el.select_one('[class*="ReviewDetail_metrics"]')
        if metrics_el:
            text = _safe_text(metrics_el)
            # Extract date like "9 Jul 2026"
            match = re.search(
                r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4})",
                text
            )
            if match:
                review["review_date"] = match.group(1)

    return review


def parse_reviews_page(html: str) -> list[dict]:
    """
    Parse all reviews from a Glassdoor page.
    Uses data-test="review-detail" as primary selector.
    """
    soup = BeautifulSoup(html, "lxml")
    reviews = []

    # Primary selector
    review_elements = soup.select('[data-test="review-detail"]')
    logger.info(f"Found {len(review_elements)} review-detail elements")

    if not review_elements:
        # Fallback
        logger.debug("No review-detail found, trying fallback...")
        for article in soup.find_all("article"):
            if article.select_one('[data-test="review-text-PROS"]'):
                review_elements.append(article)
        logger.info(f"Fallback found {len(review_elements)} articles with PROS")

    for idx, review_el in enumerate(review_elements):
        try:
            review_data = parse_review(review_el)
            if review_data.get("pros") or review_data.get("cons"):
                reviews.append(review_data)
            else:
                logger.debug(f"Review {idx} skipped: no pros/cons")
        except Exception as e:
            logger.warning(f"Failed to parse review {idx}: {e}")

    return reviews


def get_total_review_count(html: str) -> Optional[int]:
    """Extract total number of reviews from page."""
    soup = BeautifulSoup(html, "lxml")

    # Pattern: "(11,535 total reviews)" or "11,535 Reviews"
    for el in soup.find_all(["span", "div", "p", "h2", "h3"]):
        text = _safe_text(el)
        match = re.search(r"([\d,]+)\s*total\s*reviews?", text, re.IGNORECASE)
        if match:
            return int(match.group(1).replace(",", ""))
        match = re.search(r"([\d,]+)\s*Reviews?", text, re.IGNORECASE)
        if match:
            count = int(match.group(1).replace(",", ""))
            if count > 10:
                return count

    # data-test="result-count"
    count_el = soup.select_one('[data-test="result-count"]')
    if count_el:
        text = _safe_text(count_el)
        match = re.search(r"([\d,]+)", text)
        if match:
            return int(match.group(1).replace(",", ""))

    # Page title
    title = soup.find("title")
    if title:
        match = re.search(r"([\d,]+)\s*Reviews?", _safe_text(title), re.IGNORECASE)
        if match:
            return int(match.group(1).replace(",", ""))

    return None
