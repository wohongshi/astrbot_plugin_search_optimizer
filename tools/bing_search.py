"""Bing 搜索工具 — 搜索 + 自动抓取详情页 + 结果相关性检测 + DrissionPage 兜底"""

import asyncio
import json
import os
import re
import random
import time
import shutil
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

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

_search_cache: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 300

_SKIP_DETAIL_DOMAINS = {
    "youtube.com", "bilibili.com", "tiktok.com", "douyin.com",
    "weibo.com", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "reddit.com", "zhihu.com",
    "v.qq.com", "iqiyi.com", "youku.com",
}

_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()


def _cache_get(key: str) -> str | None:
    entry = _search_cache.get(key)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    if entry:
        del _search_cache[key]
    return None


def _cache_set(key: str, value: str):
    if len(_search_cache) > 100:
        oldest = sorted(_search_cache.keys(), key=lambda k: _search_cache[k][0])
        for k in oldest[:30]:
            del _search_cache[k]
    _search_cache[key] = (time.time(), value)


async def _get_session() -> aiohttp.ClientSession:
    global _session
    async with _session_lock:
        if _session is None or _session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            _session = aiohttp.ClientSession(timeout=timeout)
        return _session


async def close_session():
    global _session
    async with _session_lock:
        if _session and not _session.closed:
            await _session.close()
        _session = None


def _should_fetch_detail(url: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        for skip in _SKIP_DETAIL_DOMAINS:
            if skip in domain:
                return False
    except Exception:
        return False
    return True


def _results_relevant(query: str, results: list[dict]) -> bool:
    """检查搜索结果是否与查询相关。"""
    if not results:
        return False
    # 提取查询中的中文子串（2-4字滑动窗口）
    cn_text = ''.join(re.findall(r'[\u4e00-\u9fff]+', query))
    if not cn_text:
        return True
    query_words = set()
    for size in (4, 3, 2):
        for i in range(len(cn_text) - size + 1):
            query_words.add(cn_text[i:i+size])
    if not query_words:
        return True
    hits = 0
    for r in results[:3]:
        text = (r.get('title', '') + r.get('snippet', '')).lower()
        if any(w in text for w in query_words):
            hits += 1
    return hits >= 1


def _find_chromium() -> str | None:
    for p in ["/usr/bin/chromium", "/usr/bin/chromium-browser",
              "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]:
        if shutil.which(p) or os.path.isfile(p):
            return p
    return None


async def _fetch_detail(url: str, max_chars: int = 3000) -> str:
    """抓取详情页：先 aiohttp，内容不足时用 DrissionPage。"""
    text = await _fetch_detail_aiohttp(url, max_chars)
    if text and len(text) > 200 and "Loading" not in text[:100]:
        return text

    chromium = _find_chromium()
    if chromium:
        dp_text = await asyncio.to_thread(
            _fetch_detail_drissionpage, url, chromium, max_chars
        )
        if dp_text and len(dp_text) > len(text or ""):
            return dp_text

    return text or ""


async def _fetch_detail_aiohttp(url: str, max_chars: int) -> str:
    try:
        import trafilatura
        session = await _get_session()
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        async with session.get(
            url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                return ""
            html = await resp.text(errors="replace")

        extracted = trafilatura.extract(
            html, include_comments=False, include_tables=True, url=url
        )
        if extracted:
            text = extracted.strip()
        else:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            text = re.sub(r"\n\s*\n", "\n\n", text).strip()

        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars] + "\n...[已截断]"
        return text
    except Exception:
        return ""


def _fetch_detail_drissionpage(url: str, chromium_path: str, max_chars: int) -> str:
    page = None
    try:
        from DrissionPage import ChromiumPage, ChromiumOptions
        co = ChromiumOptions()
        co.set_browser_path(chromium_path)
        co.set_argument("--headless=new")
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-gpu")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--disable-blink-features=AutomationControlled")

        page = ChromiumPage(addr_or_opts=co)
        page.get(url, timeout=15)
        page.wait.doc_loaded()
        try:
            page.wait.ele_displayed("tag:body", timeout=8)
        except Exception:
            pass
        for _ in range(3):
            page.scroll.down(1000)
            try:
                page.wait.load_complete(timeout=2)
            except Exception:
                pass

        text = ""
        try:
            body = page.ele("tag:body", timeout=3)
            if body:
                text = body.text.strip()
        except Exception:
            pass
        if not text:
            try:
                text = page.run_js("document.body?.innerText || ''").strip()
            except Exception:
                pass
        if not text:
            return ""

        title = ""
        try:
            t = page.ele("tag:title", timeout=2)
            if t:
                title = t.text.strip()
        except Exception:
            pass
        if title:
            text = f"标题: {title}\n\n{text}"

        text = re.sub(r"\n\s*\n", "\n\n", text).strip()
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars] + "\n...[已截断]"
        return text
    except Exception:
        return ""
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                pass


async def _search_single(keyword: str, max_results: int, timeout: int) -> str:
    encoded_kw = quote_plus(keyword)
    url = f"https://cn.bing.com/search?q={encoded_kw}&cc=cn&setlang=zh-Hans&count={max_results}"
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

            snippet = ""
            p_tag = li.find("p")
            if p_tag:
                snippet = p_tag.get_text(strip=True)
            else:
                caption = li.find("div", class_="b_caption")
                if caption:
                    snippet = caption.get_text(strip=True)

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


async def search_bing_async(
    keywords: list[str],
    max_results: int = 8,
    timeout: int = 30,
    fetch_detail: bool = True,
    detail_top_n: int = 3,
    detail_max_chars: int = 3000,
) -> str:
    """Bing 搜索，支持相关性检测 + 自动重搜 + 详情抓取。"""
    if not keywords:
        return json.dumps({"error": "未提供关键词"}, ensure_ascii=False)

    query = " ".join(keywords) if len(keywords) > 1 else keywords[0]

    if len(keywords) == 1:
        cache_key = f"{keywords[0].lower().strip()}:{max_results}"
        cached = _cache_get(cache_key)
        if cached:
            return cached

    # ── 搜索 ──
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
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        item["rank"] = len(merged) + 1
                        merged.append(item)
            except (json.JSONDecodeError, ValueError):
                continue
        result_data = {
            "query": query,
            "total_results": len(merged),
            "results": merged[:max_results * 2],
        }
    else:
        raw = await _search_single(keywords[0], max_results, timeout)
        try:
            result_data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
        if result_data.get("results"):
            _cache_set(cache_key, raw)

    # ── 相关性检测：不相关时自动重搜 ──
    if result_data.get("results") and not _results_relevant(query, result_data["results"]):
        # 去掉常见虚词，保留核心关键词
        _stop_words = {'是谁', '什么', '怎么', '为什么', '哪个', '哪些', '哪里', '如何', '的', '了', '吗', '呢', '吧', '啊'}
        cn_segments = re.findall(r'[\u4e00-\u9fff]+', query)
        core_words = []
        for seg in cn_segments:
            # 去掉虚词后缀
            for sw in sorted(_stop_words, key=len, reverse=True):
                if seg.endswith(sw) and len(seg) > len(sw):
                    seg = seg[:-len(sw)]
                    break
            if len(seg) >= 2:
                core_words.append(seg)

        if core_words:
            alt_queries = [
                ''.join(core_words),             # 拼接核心词
                ' '.join(core_words),             # 空格分隔
            ]
            for alt in alt_queries:
                if alt.lower() == query.lower():
                    continue
                raw = await _search_single(alt, max_results, timeout)
                try:
                    alt_data = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if alt_data.get("results") and _results_relevant(alt, alt_data["results"]):
                    result_data = alt_data
                    break

    # ── 缓存 ──
    if len(keywords) == 1 and result_data.get("results"):
        _cache_set(cache_key, json.dumps(result_data, ensure_ascii=False, indent=2))

    # ── 抓取详情 ──
    if fetch_detail and result_data.get("results"):
        detail_urls = []
        for r in result_data["results"]:
            url = r.get("url", "")
            if url and _should_fetch_detail(url) and len(detail_urls) < detail_top_n:
                detail_urls.append((r, url))

        if detail_urls:
            tasks = [_fetch_detail(url, detail_max_chars) for _, url in detail_urls]
            details = await asyncio.gather(*tasks, return_exceptions=True)
            for (result_item, _), detail in zip(detail_urls, details):
                if isinstance(detail, Exception) or not detail:
                    continue
                result_item["detail_content"] = detail

    return json.dumps(result_data, ensure_ascii=False, indent=2)


if _ASTRBOT_AVAILABLE:

    @dataclass
    class BingSearchTool(FunctionTool):
        name: str = "web_search"
        description: str = (
            "搜索互联网获取实时信息，自动抓取详情页内容。"
            "搜索结果不相关时自动重试。keywords 传关键词列表。"
        )
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
                fetch_detail=True,
                detail_top_n=3,
                detail_max_chars=2000,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=result)]
            )
