"""Base service with common methods for all XNAT services."""

from __future__ import annotations

from typing import Any, Iterator, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from xnatctl.core.client import XNATClient


class BaseService:
    """Base service class with common functionality."""

    def __init__(self, client: "XNATClient") -> None:
        """Initialize service with XNAT client.

        Args:
            client: Authenticated XNATClient instance
        """
        self.client = client

    def _get(self, path: str, **kwargs: Any) -> Any:
        """Execute GET request and return JSON data.

        Args:
            path: API endpoint path
            **kwargs: Additional request parameters

        Returns:
            Parsed JSON response data
        """
        resp = self.client.get(path, **kwargs)
        return resp.json()

    def _post(self, path: str, **kwargs: Any) -> Any:
        """Execute POST request and return response.

        Args:
            path: API endpoint path
            **kwargs: Additional request parameters

        Returns:
            Parsed JSON response or response text
        """
        resp = self.client.post(path, **kwargs)
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    def _put(self, path: str, **kwargs: Any) -> Any:
        """Execute PUT request and return response.

        Args:
            path: API endpoint path
            **kwargs: Additional request parameters

        Returns:
            Parsed JSON response or response text
        """
        resp = self.client.put(path, **kwargs)
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    def _delete(self, path: str, **kwargs: Any) -> bool:
        """Execute DELETE request.

        Args:
            path: API endpoint path
            **kwargs: Additional request parameters

        Returns:
            True if successful
        """
        self.client.delete(path, **kwargs)
        return True

    def _paginate(
        self,
        path: str,
        page_size: int = 100,
        result_key: str = "ResultSet.Result",
    ) -> Iterator[dict[str, Any]]:
        """Iterate over paginated results.

        Args:
            path: API endpoint path
            page_size: Results per page
            result_key: Dot-notation key to extract results

        Yields:
            Individual result items
        """
        yield from self.client.paginate(path, page_size, result_key)

    def _extract_results(
        self,
        data: dict[str, Any],
        result_key: str = "ResultSet.Result",
    ) -> list[dict[str, Any]]:
        """Extract results from XNAT response.

        Args:
            data: Raw API response data
            result_key: Dot-notation key to extract results

        Returns:
            List of result items
        """
        results = data
        for key in result_key.split("."):
            if isinstance(results, dict):
                results = results.get(key, [])
            else:
                return []
        return results if isinstance(results, list) else []

    def _build_path(self, *parts: str) -> str:
        """Build API path from parts.

        Args:
            *parts: Path segments

        Returns:
            Joined path string
        """
        return "/" + "/".join(p.strip("/") for p in parts if p)
