import asyncio
import re
import os
import logging
from apify import Actor
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

from scrapers.propertyfinder import PropertyFinderScraper

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Fallback search URL used whenever the user starts a run without a URL.
DEFAULT_SEARCH_URL = "https://www.propertyfinder.ae/en/search?c=1&fu=0&l=1&ob=mr"

# Regex that matches a single property detail page URL.
SINGLE_LISTING_PATTERN = re.compile(
    r'/en/(?:rent|buy|plp|property|commercial|new-projects)'
    r'/[a-zA-Z0-9\-\/]+-\d{6,15}\.html'
)

def is_single_listing_url(url: str) -> bool:
    """Return True when the URL points to one specific property page."""
    return bool(SINGLE_LISTING_PATTERN.search(url))


async def main():
    async with Actor:
        logger.info("PropertyFinder Scraper starting up...")

        # ── Load Runtime Inputs ──────────────────────────────────────────────
        actor_input = await Actor.get_input() or {}

        url           = actor_input.get("url")
        start_page    = actor_input.get("startPage", 1)
        end_page      = actor_input.get("endPage", 1)
        max_results   = actor_input.get("maxResults", 5)
        proxy_config  = actor_input.get("proxy")
        save_to_mongo = actor_input.get("saveToMongo", False)
        mongo_uri     = actor_input.get("mongoUri")

        # ── Fallback when no URL is supplied ─────────────────────────────────
        if not url or not str(url).strip():
            logger.warning(
                f"No 'url' provided in input — falling back to default search URL: "
                f"{DEFAULT_SEARCH_URL}"
            )
            url = DEFAULT_SEARCH_URL

        single_listing = is_single_listing_url(url)

        if single_listing:
            logger.info(f"Mode  : Single listing")
            logger.info(f"URL   : {url}")
        else:
            logger.info(f"Mode  : Search / multi-listing")
            logger.info(f"URL   : {url}")
            logger.info(f"Pages : {start_page} → {end_page}   |   Max results: {max_results}")

        # ── MongoDB setup ────────────────────────────────────────────────────
        mongo_col = None
        if save_to_mongo and mongo_uri:
            try:
                client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
                client.admin.command("ping")
                mongo_col = client["real_estate"]["pf_listings"]
                logger.info("MongoDB  : Connected")
            except ConnectionFailure as e:
                logger.error(f"MongoDB  : Connection failed — {e}")

        # ── Proxy setup ──────────────────────────────────────────────────────
        proxy_url = None
        if proxy_config:
            if isinstance(proxy_config, dict) and proxy_config.get("useApifyProxy"):
                try:
                    apify_proxy = await Actor.create_proxy_configuration()
                    if apify_proxy:
                        proxy_url = await apify_proxy.new_url()
                        logger.info("Proxy    : Apify Managed Proxy active")
                except Exception as e:
                    logger.warning(
                        f"Proxy    : Apify Managed Proxy unavailable ({e}). "
                        "Continuing without a proxy. This is expected when running "
                        "locally without an APIFY_TOKEN — it works automatically on "
                        "the Apify platform."
                    )
            elif isinstance(proxy_config, str):
                proxy_url = proxy_config
                logger.info("Proxy    : Custom proxy active")

        # ── Scraper init ─────────────────────────────────────────────────────
        scraper = PropertyFinderScraper(proxy=proxy_url)
        scraped_count = 0

        logger.info("─" * 60)
        logger.info("Extraction started — browser launching...")
        logger.info("─" * 60)
        logger.info(f"{'#':>3}  {'Listing ID':<12} {'Agent Name':<28} {'Phone':<18} {'Email'}")
        logger.info("─" * 60)

        try:
            async for listing in scraper.scrape_stream(
                url=url,
                start_page=start_page,
                end_page=end_page,
                max_results=None if single_listing else max_results,
            ):
                # Hard cap only applies to search/multi-listing mode.
                if not single_listing and scraped_count >= max_results:
                    logger.info(f"Limit reached ({max_results} listings). Stopping.")
                    break

                listing_data = listing.model_dump(exclude_none=True)

                # Agent profile picture URL is not included in the output.
                if "agent" in listing_data:
                    listing_data["agent"].pop("image", None)
                    
                    # Force Excel CSV import to treat the phone number as text
                    if listing_data["agent"].get("phone") and listing_data["agent"]["phone"] != "N/A":
                        listing_data["agent"]["phone"] = f'="{listing_data["agent"]["phone"]}"'

                agent       = listing_data.get("agent", {})
                agent_name  = agent.get("name", "N/A")
                agent_phone = agent.get("phone", "N/A")
                agent_email = agent.get("email", "N/A")

                scraped_count += 1
                logger.info(
                    f"{scraped_count:>3}  {listing.listing_id:<12} "
                    f"{agent_name:<28} {agent_phone:<18} {agent_email}"
                )

                # ── Push to Apify dataset ────────────────────────────────────
                await Actor.push_data(listing_data)

                # ── Optional MongoDB upsert ──────────────────────────────────
                if mongo_col is not None:
                    try:
                        mongo_col.update_one(
                            {"listing_id": listing.listing_id},
                            {"$set": listing_data},
                            upsert=True,
                        )
                    except Exception as e:
                        logger.error(f"MongoDB write error for {listing.listing_id}: {e}")

                if scraped_count % 10 == 0:
                    logger.info(f"── Progress: {scraped_count} listings saved ──")

        except Exception as e:
            logger.error(f"Extraction interrupted: {e}")

        logger.info("─" * 60)
        logger.info(f"Done. Total listings saved: {scraped_count}")
        logger.info("─" * 60)


if __name__ == "__main__":
    asyncio.run(main())
