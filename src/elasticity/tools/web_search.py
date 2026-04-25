"""Web search adapter tool."""

import os
import json
from typing import Optional, Dict, Any

try:
    import httpx
except ImportError:
    httpx = None

# Global state for search provider configuration
_search_provider: Optional[str] = None
_api_key: Optional[str] = None
_ddgs_backends: str = "google,duckduckgo"


def _default_api_key_env(provider: str) -> str:
    """Get the default API key environment variable name for a provider."""
    defaults = {
        "brave": "BRAVE_API_KEY",
        "serpapi": "SERPAPI_API_KEY",
    }
    return defaults.get(provider, "BRAVE_API_KEY")


def _tool_init(config: Dict[str, Any]) -> None:
    """Initialize the web search tool with provider configuration.
    
    Args:
        config: Tool configuration dict with:
            - provider: Search provider name (e.g., "brave", "serpapi", "duckduckgo")
            - api_key_env: Environment variable name containing the API key (not needed for duckduckgo)
    """
    global _search_provider, _api_key
    
    provider = config.get("provider", "brave")
    _search_provider = provider
    
    if provider == "duckduckgo":
        # No API key needed for DuckDuckGo
        global _ddgs_backends
        _ddgs_backends = config.get("backends", "google,duckduckgo")
        _api_key = None
        return
    
    api_key_env = config.get("api_key_env", _default_api_key_env(provider))
    _api_key = os.getenv(api_key_env)
    
    if not _api_key:
        raise ValueError(
            f"API key not found in environment variable '{api_key_env}'. "
            f"Please set it before using the web_search tool."
        )


def search(query: str) -> str:
    """Search the web using the configured search provider.
    
    Args:
        query: Search query string
        
    Returns:
        Search results as a formatted string
        
    Raises:
        ValueError: If the tool is not initialized or provider is not supported
        ImportError: If required packages are not installed
        Exception: If the search request fails
    """
    if _search_provider is None:
        raise ValueError(
            "Web search tool not initialized. "
            "Please configure provider in tool config."
        )
    
    # DuckDuckGo doesn't require httpx
    if _search_provider != "duckduckgo":
        if httpx is None:
            raise ImportError(
                "The 'httpx' package is required for web search.\n"
                "Install it with: pip install httpx"
            )
        if _api_key is None:
            raise ValueError(
                "Web search tool not initialized. "
                "Please configure provider and API key in tool config."
            )
    
    if _search_provider == "brave":
        return _search_brave(query)
    elif _search_provider == "serpapi":
        return _search_serpapi(query)
    elif _search_provider == "duckduckgo":
        return _search_duckduckgo(query)
    else:
        raise ValueError(f"Unsupported search provider: {_search_provider}")


def _search_brave(query: str) -> str:
    """Search using Brave Search API."""
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": _api_key,
    }
    params = {"q": query, "count": 10}
    
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            
            results = []
            if "web" in data and "results" in data["web"]:
                for result in data["web"]["results"][:10]:
                    title = result.get("title", "")
                    url = result.get("url", "")
                    description = result.get("description", "")
                    results.append(f"Title: {title}\nURL: {url}\n{description}\n")
            
            return "\n---\n".join(results) if results else "No results found"
    except httpx.HTTPError as e:
        raise Exception(f"Brave search API request failed: {e}") from e


def _search_serpapi(query: str) -> str:
    """Search using SerpAPI."""
    url = "https://serpapi.com/search"
    params = {
        "q": query,
        "api_key": _api_key,
        "num": 10,
    }
    
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            results = []
            if "organic_results" in data:
                for result in data["organic_results"][:10]:
                    title = result.get("title", "")
                    link = result.get("link", "")
                    snippet = result.get("snippet", "")
                    results.append(f"Title: {title}\nURL: {link}\n{snippet}\n")
            
            return "\n---\n".join(results) if results else "No results found"
    except httpx.HTTPError as e:
        raise Exception(f"SerpAPI request failed: {e}") from e


def _search_duckduckgo(query: str) -> str:
    """Search using DuckDuckGo (no API key required)."""
    try:
        from ddgs import DDGS
    except ImportError:
        raise ImportError(
            "The 'ddgs' package is required for DuckDuckGo search.\n"
            "Install it with: pip install ddgs"
        )

    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=10, backend=_ddgs_backends):
                title = r.get("title", "")
                url = r.get("href", "")
                body = r.get("body", "")
                results.append(f"Title: {title}\nURL: {url}\n{body}\n")
        
        return "\n---\n".join(results) if results else "No results found"
    except Exception as e:
        raise Exception(f"DuckDuckGo search failed: {e}") from e
