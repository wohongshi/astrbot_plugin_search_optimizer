"""Bing 搜索工具 — 异步 aiohttp 实现，无需浏览器"""

import asyncio
import json
import re
import random
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

# 浏览器 UA 池，降低被反爬风险
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


async def search_bing_async(
    keywords: list[str],
    max_results: int = 8,
    timeout: int = 30,
) -> str:
    """异步 Bing 搜索，返回 JSON 结果字符串。"""
    if not keywords:
        return json.dumps({"error": "未提供关键词"}, ensure_ascii=False)

    keyword = keywords[0] if isinstance(keywords, list) else keywords
    encoded_kw = quote_plus(keyword)
    url = f"https://www.bing.com/search?q={encoded_kw}&count={max_results}"

    headers = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }

    max_retries = 3
    last_error = None

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
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

    # Bing 搜索结果在 <li class="b_algo"> 中
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

            # 摘要：优先 p，其次 div.b_caption
            snippet = ""
            p_tag = li.find("p")
            if p_tag:
                snippet = p_tag.get_text(strip=True)
            else:
                caption = li.find("div", class_="b_caption")
                if caption:
                    snippet = caption.get_text(strip=True)

            # 日期：从摘要中正则提取
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
        description: str = "搜索互联网获取实时信息。keywords 传搜索关键词列表。"
        parameters: dict = field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "搜索关键词列表",
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
