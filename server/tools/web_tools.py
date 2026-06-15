"""Web search and URL fetching tools."""

import asyncio
import os
import re
from typing import Any

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams


async def _search_duckduckgo(query: str, n: int) -> list[dict]:
    """Async wrapper for synchronous DuckDuckGo text/web search."""
    def _ddg_text_search(q: str, max_results: int = 5) -> list[dict]:
        """Synchronous DuckDuckGo text/web search. Run in a thread pool.

        Returns a list of {title, body, href} result dicts (newest DDGS schema).
        """
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            return ddgs.text(q, max_results=max_results) or []

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _ddg_text_search(query, n))


async def _search_tavily(query: str, n: int) -> list[dict]:
    """Search using Tavily API."""
    import aiohttp

    key = os.getenv("TAVILY_API_KEY", "")
    payload = {"api_key": key, "query": query, "max_results": n, "search_depth": "basic"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.post("https://api.tavily.com/search", json=payload) as r:
            r.raise_for_status()
            data = await r.json()
    return [
        {"title": x.get("title", ""), "body": x.get("content", ""), "href": x.get("url", "")}
        for x in data.get("results", [])
    ]


async def _search_brave(query: str, n: int) -> list[dict]:
    """Search using Brave Search API."""
    import aiohttp

    key = os.getenv("BRAVE_API_KEY", "")
    headers = {"X-Subscription-Token": key, "Accept": "application/json"}
    params = {"q": query, "count": n}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get(
            "https://api.search.brave.com/res/v1/web/search", params=params, headers=headers
        ) as r:
            r.raise_for_status()
            data = await r.json()
    return [
        {"title": x.get("title", ""), "body": x.get("description", ""), "href": x.get("url", "")}
        for x in data.get("web", {}).get("results", [])
    ]


def _enabled_search_backends() -> list[tuple[str, Any]]:
    """Return (name, async_fn) for every backend that is currently usable."""
    backends: list[tuple[str, Any]] = [("duckduckgo", _search_duckduckgo)]
    if os.getenv("TAVILY_API_KEY"):
        backends.append(("tavily", _search_tavily))
    if os.getenv("BRAVE_API_KEY"):
        backends.append(("brave", _search_brave))
    return backends


async def _multi_search(query: str, n: int) -> tuple[list[dict], list[str], list[str]]:
    """Fan out to all enabled backends concurrently, merge + dedupe by URL.

    Returns (results, ok_backends, failed_backends). Each result dict carries a
    "sources" list naming the backends that returned it, ordered by corroboration.
    """
    backends = _enabled_search_backends()

    async def _run(name: str, fn: Any) -> tuple[str, Any]:
        try:
            return name, await asyncio.wait_for(fn(query, n), timeout=11)
        except Exception as e:
            logger.warning(f"[WEB] backend '{name}' failed: {e}")
            return name, e

    outcomes = await asyncio.gather(*[_run(name, fn) for name, fn in backends])

    merged: dict[str, dict] = {}
    ok, failed = [], []
    for name, res in outcomes:
        if isinstance(res, Exception):
            failed.append(name)
            continue
        ok.append(name)
        for item in res:
            url = (item.get("href") or "").strip()
            if not url:
                continue
            key = url.rstrip("/")
            if key not in merged:
                merged[key] = {
                    "title": item.get("title", ""),
                    "body": item.get("body", ""),
                    "href": url,
                    "sources": [],
                }
            if name not in merged[key]["sources"]:
                merged[key]["sources"].append(name)

    # Most-corroborated first (returned by the most backends), then cap to n.
    ordered = sorted(merged.values(), key=lambda m: -len(m["sources"]))[:n]
    return ordered, ok, failed


def create_web_tools() -> dict[str, Any]:
    """Factory function to create web search and fetching tools.

    Returns a dictionary of tool functions ready for registration.
    """

    async def web_search(params: FunctionCallParams, query: str, max_results: int = 5):
        """Search the web for current or factual information and return the top results.

        Call this whenever answering needs up-to-date facts, recent events, specific
        figures, or anything you are not confident about from memory. Base your spoken
        answer on the returned results and mention the source.

        Args:
            query: The search query.
            max_results: How many results to return (1–8, default 5).
        """
        q = (query or "").strip()
        if not q:
            await params.result_callback("ERROR: query must not be empty.")
            return
        n = max(1, min(8, int(max_results) if max_results else 5))
        results, ok, failed = await _multi_search(q, n)

        if not results:
            detail = f" (all backends failed: {', '.join(failed)})" if failed else ""
            await params.result_callback(
                f"NO_RESULTS for '{q}'{detail}. Tell the user you couldn't find anything online "
                "and answer from your own knowledge if you can."
            )
            return

        backends_note = f"backends used: {', '.join(ok)}" + (
            f"; unavailable: {', '.join(failed)}" if failed else ""
        )
        lines = [f"Search results for '{q}' ({backends_note}):", ""]
        for i, r in enumerate(results, 1):
            title = (r.get("title") or "").strip()
            body = (r.get("body") or "").strip()
            href = (r.get("href") or "").strip()
            srcs = ", ".join(r.get("sources", []))
            lines.append(f"{i}. {title}\n   {body}\n   Source: {href}  [via {srcs}]")
        logger.info(f"[WEB] web_search '{q}' → {len(results)} merged results (ok={ok} failed={failed})")
        await params.result_callback(
            "\n".join(lines)
            + "\n\nSummarize the answer for the user from these results and cite the source(s). "
            "Results returned by more than one backend are more trustworthy. "
            "If you need the full text of one result, call fetch_url with its Source link."
        )

    async def fetch_url(params: FunctionCallParams, url: str):
        """Fetch the readable text of a web page (e.g. a web_search result) for a deeper answer.

        Args:
            url: The page URL to fetch (use a Source link from web_search).
        """
        u = (url or "").strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            await params.result_callback("ERROR: url must start with http:// or https://")
            return
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.get(u, headers={"User-Agent": "Mozilla/5.0 (cockpit)"}) as resp:
                    if resp.status != 200:
                        await params.result_callback(f"FETCH_ERROR: HTTP {resp.status} for {u}")
                        return
                    html = await resp.text()
        except Exception as e:
            logger.error(f"[WEB] fetch_url failed: {e}")
            await params.result_callback(f"FETCH_ERROR: {e}")
            return
        # Strip tags/scripts to rough plain text and cap length for the context window.
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 6000:
            text = text[:6000] + " …[truncated]"
        logger.info(f"[WEB] fetch_url {u} → {len(text)} chars")
        await params.result_callback(
            f"Readable text from {u}:\n\n{text}\n\nAnswer the user's question from this and cite the source."
        )

    return {
        "web_search": web_search,
        "fetch_url": fetch_url,
    }
