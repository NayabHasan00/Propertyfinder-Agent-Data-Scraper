import asyncio
import random
from abc import ABC, abstractmethod
from typing import AsyncGenerator, Optional

from models import Listing

class BaseScraper(ABC):
    def __init__(self, proxy: Optional[str] = None, concurrency_limit: int = 4):
        self.proxy = proxy
        self.semaphore = asyncio.Semaphore(concurrency_limit)

    @abstractmethod
    def scrape_stream(self, url: str, start_page: int, end_page: int) -> AsyncGenerator[Listing, None]:
        """
        Production ready streaming implementation.
        Must be implemented by subclasses to yield listings in real-time.
        """
        pass

    async def random_delay(self, min_sec: float = 0.5, max_sec: float = 2.0):
        """Helper for adding human-like delays."""
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    def get_context_options(self) -> dict:
        """Standard Playwright context options with proxy support."""
        options = {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
            "viewport": {"width": 1920, "height": 1080},
        }
        if self.proxy:
            options["proxy"] = {"server": self.proxy}
        return options