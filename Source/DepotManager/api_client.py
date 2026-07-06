"""Async HTTP client for Morrenus/Ryuu manifest APIs."""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Optional, Tuple

import aiohttp

from .config import SOURCES
from .parser import safe_extract, scan_directory

logger = logging.getLogger("DepotManager.APIClient")


# --- EXCEPTIONS ---
class APIError(Exception):
    """Base exception class for API Client errors."""

    pass


class APIAuthError(APIError):
    """Exception raised when an API key is rejected (HTTP 401/403)."""

    pass


class APIHTTPError(APIError):
    """Exception raised for HTTP response errors (non-2xx statuses other than auth)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Server responded with {status}: {message}")
        self.status = status
        self.message = message


class APINetworkError(APIError):
    """Exception raised for network connection errors."""

    pass


# --- API CLIENT ---
class APIClient:
    """Client to perform asynchronous requests to Ryuu/Morrenus manifest APIs."""

    def __init__(self, session: aiohttp.ClientSession, settings: dict) -> None:
        self.session = session
        self.settings = settings

    async def fetch_manifests(
        self, app_id: str, api_key: str, source: str
    ) -> Tuple[Path, dict]:
        """Fetches the zip archive containing manifests and Lua configs for the given AppID,
        extracts it safely, scans it, and returns the temporary directory and inventory dict."""
        logger.debug(
            "API request for AppID: %s (source: %s)",
            app_id,
            SOURCES.get(source, {}).get("label", source),
        )
        timeout = aiohttp.ClientTimeout(total=self.settings.get("request_timeout", 30))

        if source == "ryuu":
            url = self.settings.get("api_base_url_ryuu", "")
            headers = {"User-Agent": "DepotManager/2.0"}
            params: Optional[dict] = {"appid": app_id, "auth_code": api_key}
        else:
            base = self.settings.get("api_base_url_morrenus", "").rstrip("/")
            url = f"{base}/manifest/{app_id}"
            headers = {"User-Agent": "DepotManager/2.0", "X-API-Key": api_key}
            params = None

        temp_dir = Path(tempfile.mkdtemp(prefix="depot_manager_"))
        logger.debug("Temporary directory created: %s", temp_dir)

        try:
            async with self.session.get(
                url, headers=headers, params=params, timeout=timeout
            ) as r:
                if r.status in (401, 403):
                    logger.warning(
                        "API Key rejected (HTTP %s) for AppID %s.", r.status, app_id
                    )
                    raise APIAuthError("API Key rejected by the server.")

                try:
                    r.raise_for_status()
                except aiohttp.ClientResponseError as exc:
                    logger.error("HTTP error %s: %s", exc.status, exc.message)
                    raise APIHTTPError(exc.status, exc.message) from exc

                data = await r.read()

            zip_path = temp_dir / "data.zip"
            await asyncio.to_thread(self._write_file, zip_path, data)
            await asyncio.to_thread(safe_extract, zip_path, temp_dir)
            local_inv = await asyncio.to_thread(scan_directory, temp_dir)

            return temp_dir, local_inv

        except aiohttp.ClientConnectionError as exc:
            logger.exception("Connection error during fetch.")
            raise APINetworkError(f"Connection failed: {exc}") from exc
        except aiohttp.ClientError as exc:
            logger.exception("aiohttp client error.")
            raise APIError(f"HTTP request failed: {exc}") from exc
        except Exception as exc:
            if isinstance(exc, APIError):
                raise exc
            logger.exception("Unexpected error in API Client.")
            raise APIError(f"An unexpected error occurred: {exc}") from exc

    @staticmethod
    def _write_file(path: Path, data: bytes) -> None:
        with open(path, "wb") as f:
            f.write(data)
