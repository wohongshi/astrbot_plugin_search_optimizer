"""Bing 搜索工具 — 异步 aiohttp 实现，带连接复用和结果缓存"""

import asyncio
import json
import re
import random
import time
from dataclasses import dataclass, field
from urllib.parse import quote_plus, urlparse

import aiohttp
from bs4 import BeautifulSoup

try:
    from astrbot.api import FunctionTool, logger
    from mcp.types import CallToolResult, TextContent
    _ASTRBOT_AVAILABLE = True
except ImportError:
    FunctionTool = object
    _ASTRBOT_AVAILABLE = False

# 浏览器 UA 池
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# ── 搜索结果短期缓存（5 分钟） ──
_search_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 300  # 秒


def _cache_get(key: str) -> str | None:
    entry = _search_cache.get(key)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    if entry:
        del _search_cache[key]
    return None


def _cache_set(key: str, value: str):
    # 防止缓存无限增长
    if len(_search_cache) > 100:
        oldest = sorted(_search_cache.keys(), key=lambda k: _search_cache[k][0])
        for k in oldest[:30]:
            del _search_cache[k]
    _search_cache[key] = (time.time(), value)


# ── 共享 Session（连接复用） ──
_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    global _session
    async with _session_lock:
        if _session is None or _session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            _session = aiohttp.ClientSession(timeout=timeout)
        return _session


async def close_session():
    """插件卸载时调用，关闭连接池。"""
    global _session
    async with _session_lock:
        if _session and not _session.closed:
            await _session.close()
        _session = None


async def search_bing_async(
    keywords: list[str],
    max_results: int = 8,
    timeout: int = 30,
) -> str:
    """异步 Bing 搜索，支持多关键词并行，返回 JSON 结果字符串。"""
    if not keywords:
        return json.dumps({"error": "未提供关键词"}, ensure_ascii=False)

    # 单关键词走缓存
    if len(keywords) == 1:
        cache_key = f"{keywords[0].lower().strip()}:{max_results}"
        cached = _cache_get(cache_key)
        if cached:
            return cached

    # 多关键词并行搜索
    if len(keywords) > 1:
        tasks = [_search_single(kw, max_results, timeout) for kw in keywords]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        merged = []
        seen_urls = set()
        for r in results_list:
            if isinstance(r, Exception):
                continue
            try:
                data = json.loads(r)
                for item in data.get("results", []):
                    url = item.get("url", "")
                    if url not in seen_urls:
                        seen_urls.add(url)
                        item["rank"] = len(merged) + 1
                        merged.append(item)
            except (json.JSONDecodeError, ValueError):
                continue
        result = json.dumps({
            "query": " ".join(keywords),
            "total_results": len(merged),
            "results": merged[:max_results * 2],
        }, ensure_ascii=False, indent=2)
        return result

    result = await _search_single(keywords[0], max_results, timeout)
    # 缓存成功结果
    try:
        data = json.loads(result)
        if data.get("results"):
            _cache_set(cache_key, result)
    except (json.JSONDecodeError, ValueError):
        pass
    return result


async def _search_single(
    keyword: str,
    max_results: int = 8,
    timeout: int = 30,
) -> str:
    """单关键词搜索。"""
    encoded_kw = quote_plus(keyword)
    url = f"https://www.bing.com/search?q={encoded_kw}&count={max_results}"

    headers = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    max_retries = 3
    last_error = None

    for attempt in range(max_retries):
        try:
            session = await _get_session()
            async with session.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    last_error = f"HTTP {resp.status}"
                    await asyncio.sleep(1)
                    continue
                html = await resp.text()

            results = _parse_bing_html(html, max_results)
            if results:
                return json.dumps({
                    "query": keyword,
                    "total_results": len(results),
                    "results": results,
                }, ensure_ascii=False, indent=2)

            last_error = "解析结果为空"
            await asyncio.sleep(1)

        except asyncio.TimeoutError:
            last_error = f"请求超时 ({timeout}s)"
            await asyncio.sleep(1)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1)

    return json.dumps({
        "query": keyword,
        "total_results": 0,
        "results": [],
        "message": f"搜索失败（重试 {max_retries} 次）: {last_error}",
    }, ensure_ascii=False)


def _parse_bing_html(html: str, max_results: int) -> list[dict]:
    """解析 Bing 搜索结果 HTML。"""
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for li in soup.select("li.b_algo")[:max_results]:
        try:
            h2 = li.find("h2")
            if not h2:
                continue

            a_tag = h2.find("a")
            if not a_tag:
                continue

            title = h2.get_text(strip=True)
            url = a_tag.get("href", "")

            if not title or not url:
                continue

            # 摘要
            snippet = ""
            p_tag = li.find("p")
            if p_tag:
                snippet = p_tag.get_text(strip=True)
            else:
                caption = li.find("div", class_="b_caption")
                if caption:
                    snippet = caption.get_text(strip=True)

            # 日期
            date = ""
            if snippet:
                dm = re.search(r"(\d{4}[-年]\d{1,2}[-月]\d{1,2}日?)", snippet)
                if dm:
                    date = dm.group(1).replace("年", "-").replace("月", "-").replace("日", "")

            source = urlparse(url).netloc.replace("www.", "") if url else ""

            results.append({
                "title": title,
                "url": url,
                "snippet": snippet[:300],
                "date": date,
                "source": source,
                "rank": len(results) + 1,
            })
        except Exception:
            continue

    return results


if _ASTRBOT_AVAILABLE:

    @dataclass
    class BingSearchTool(FunctionTool):
        name: str = "web_search"
        description: str = "搜索互联网获取实时信息。keywords 传搜索关键词列表，支持多个关键词并行搜索。"
        parameters: dict = field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "搜索关键词列表，多个关键词会并行搜索并合并结果",
                    },
                },
                "required": ["keywords"],
            }
        )

        search_timeout: int = 30

        async def run(self, event: "AstrMessageEvent", keywords: list[str]):
            if not keywords:
                return CallToolResult(
                    content=[TextContent(type="text", text="未提供搜索关键词")]
                )
            result = await search_bing_async(
                keywords=keywords,
                timeout=self.search_timeout,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=result)]
            )
