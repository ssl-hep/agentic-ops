"""Read-only GitLab REST client.

Only GET requests are possible through this class, so a bug elsewhere cannot turn into a
write against the repository, whatever scope the token has.
"""

import asyncio
from typing import Any
from urllib.parse import quote

import httpx


class GitLabError(Exception):
    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class GitLabNotFound(GitLabError):
    pass


class ReadOnlyGitLabClient:
    def __init__(
        self,
        base_url: str,
        project: str,
        token: str | None,
        *,
        timeout: float = 30.0,
        max_concurrency: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.project = project
        self.token_configured = bool(token)
        headers = {"PRIVATE-TOKEN": token} if token else {}
        self._http = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v4",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )
        self._project_path = f"/projects/{quote(project, safe='')}"
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        async with self._semaphore:
            try:
                response = await self._http.get(path, params=params)
            except httpx.HTTPError as exc:
                raise GitLabError(None, f"Cannot reach GitLab at {self.base_url}: {exc}") from exc
        if response.status_code == 404:
            raise GitLabNotFound(404, _error_message(response))
        if response.status_code == 401:
            raise GitLabError(401, "GitLab rejected the token (401). It may have expired or been revoked.")
        if response.is_error:
            raise GitLabError(response.status_code, f"GitLab returned {response.status_code}: {_error_message(response)}")
        return response

    async def _get_all(self, path: str, params: dict[str, Any], limit: int = 2000) -> list[dict[str, Any]]:
        """Follow rel="next" links, for both offset and keyset pagination."""
        items: list[dict[str, Any]] = []
        response = await self._get(path, {"per_page": 100, **params})
        while True:
            items.extend(response.json())
            next_url = response.links.get("next", {}).get("url")
            if not next_url or len(items) >= limit:
                return items[:limit]
            response = await self._get(next_url)

    async def project_info(self) -> dict[str, Any]:
        return (await self._get(self._project_path)).json()

    async def branches(self) -> list[dict[str, Any]]:
        return await self._get_all(f"{self._project_path}/repository/branches", {}, limit=500)

    async def tags(self) -> list[dict[str, Any]]:
        return await self._get_all(
            f"{self._project_path}/repository/tags", {"order_by": "updated", "sort": "desc"}, limit=200
        )

    async def commit(self, ref: str) -> dict[str, Any]:
        return (await self._get(f"{self._project_path}/repository/commits/{quote(ref, safe='')}")).json()

    async def tree(self, path: str, ref: str, recursive: bool = True) -> list[dict[str, Any]]:
        return await self._get_all(
            f"{self._project_path}/repository/tree",
            {"path": path, "ref": ref, "recursive": recursive, "pagination": "keyset"},
            limit=20000,
        )

    async def raw_file(self, path: str, ref: str) -> str:
        response = await self._get(
            f"{self._project_path}/repository/files/{quote(path, safe='')}/raw", {"ref": ref}
        )
        return response.text

    async def registry_repositories(self) -> list[dict[str, Any]]:
        return await self._get_all(f"{self._project_path}/registry/repositories", {}, limit=100)

    async def registry_tag(self, repository_id: int, tag: str) -> dict[str, Any]:
        return (
            await self._get(f"{self._project_path}/registry/repositories/{repository_id}/tags/{quote(tag, safe='')}")
        ).json()


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body)
    return str(body)
