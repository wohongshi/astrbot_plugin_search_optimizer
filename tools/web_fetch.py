"""网页抓取工具 — aiohttp 快速抓取 + DrissionPage 系统 Chromium 兜底"""

import asyncio
import ipaddress
import os
import re
import shutil
from dataclasses import dataclass, field
from urllib.parse import urlparse

import aiohttp
import trafilatura

try:
    from astrbot.api import FunctionTool, logger
    from mcp.types import CallToolResult, TextContent
    _ASTRBOT_AVAILABLE = True
except ImportError:
    FunctionTool = object
    _ASTRBOT_AVAILABLE = False

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

# ── 共享 aiohttp Session ──
_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()

# ── 系统 Chromium 路径 ──
_CHROMIUM_PATHS = [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
]


def _find_chromium() -> str | None:
    for p in _CHROMIUM_PATHS:
        if shutil.which(p) or os.path.isfile(p):
            return p
    return None


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


def validate_url(url: str) -> str | None:
    if not url:
        return "错误：未提供 URL"
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return f"错误：不支持的协议 '{scheme}'，仅允许 http:// 和 https://"
    if not parsed.netloc:
        return "错误：URL 格式不正确，缺少主机名"
    try:
        host = parsed.hostname
    except Exception:
        return f"错误：无法解析 URL 主机名 '{url}'"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            return f"错误：不允许访问内网/私有地址 '{host}'"
    except ValueError:
        pass
    return None


async def fetch_page_async(
    url: str,
    timeout: int = 30,
    max_chars: int = 10000,
) -> str:
    """异步抓取网页。先 aiohttp 快速抓，内容不足时用 DrissionPage 渲染。"""
    error = validate_url(url)
    if error:
        return error

    # ── 第一步：aiohttp 快速抓取 ──
    text = await _fetch_aiohttp(url, timeout, max_chars)

    # 内容足够就直接返回
    if text and len(text) > 200 and "Loading" not in text[:100]:
        return text

    # ── 第二步：DrissionPage 系统 Chromium 渲染 ──
    chromium_path = _find_chromium()
    if chromium_path:
        pw_text = await asyncio.to_thread(
            _fetch_drissionpage, url, chromium_path, timeout, max_chars
        )
        if pw_text and len(pw_text) > len(text or ""):
            return pw_text

    return text or "页面内容为空"


async def _fetch_aiohttp(url: str, timeout: int, max_chars: int) -> str:
    """aiohttp 快速抓取 + trafilatura 提取。"""
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    for attempt in range(2):
        try:
            session = await _get_session()
            async with session.get(
                url, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    if attempt == 0:
                        await asyncio.sleep(1)
                        continue
                    return f"HTTP {resp.status}: {resp.reason}"
                html = await resp.text(errors="replace")

            extracted = trafilatura.extract(
                html, include_comments=False, include_tables=True, url=url
            )
            if extracted:
                text = extracted.strip()
            else:
                text = _fallback_extract(html)

            title = ""
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            if title_match:
                title = title_match.group(1).strip()
            if title:
                text = f"标题: {title}\n\n{text}"

            if max_chars > 0 and len(text) > max_chars:
                text = text[:max_chars] + "\n\n[内容已截断]"

            return text

        except asyncio.TimeoutError:
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return f"请求超时 ({timeout}s)"
        except Exception as e:
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            return f"{type(e).__name__}: {e}"

    return "抓取失败"


def _fetch_drissionpage(url: str, chromium_path: str, timeout: int, max_chars: int) -> str:
    """DrissionPage 同步抓取（在线程中运行）。"""
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
        page.get(url, timeout=timeout)
        page.wait.doc_loaded()

        # 等待页面渲染
        try:
            page.wait.ele_displayed("tag:body", timeout=8)
        except Exception:
            pass

        # 滚动加载更多内容
        for _ in range(3):
            page.scroll.down(1000)
            try:
                page.wait.load_complete(timeout=2)
            except Exception:
                pass

        # 提取正文
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

        # 获取标题
        title = ""
        try:
            title_elem = page.ele("tag:title", timeout=2)
            if title_elem:
                title = title_elem.text.strip()
        except Exception:
            pass

        if title:
            text = f"标题: {title}\n\n{text}"

        # 清理
        text = re.sub(r"\n\s*\n", "\n\n", text).strip()

        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars] + "\n\n[内容已截断]"

        return text

    except Exception as e:
        if _ASTRBOT_AVAILABLE:
            logger.warning(f"[web_fetch] DrissionPage 抓取失败 ({url}): {e}")
        return ""
    finally:
        if page:
            try:
                page.quit()
            except Exception:
                pass


def _fallback_extract(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n\s*\n", "\n\n", text)
        return text.strip()
    except Exception:
        return ""


if _ASTRBOT_AVAILABLE:

    @dataclass
    class WebFetchTool(FunctionTool):
        name: str = "web_fetch"
        description: str = (
            "抓取指定 URL 的网页内容并返回文本。"
            "支持 JS 渲染页面（自动使用系统 Chromium）。urls 传 URL 列表。"
        )
        parameters: dict = field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要抓取的网页 URL 列表（完整链接，含 https://）",
                    },
                },
                "required": ["urls"],
            }
        )

        fetch_timeout: int = 30

        async def run(self, event: "AstrMessageEvent", urls: list[str]):
            if not urls:
                return CallToolResult(
                    content=[TextContent(type="text", text="未提供 URL")]
                )
            tasks = [fetch_page_async(url, timeout=self.fetch_timeout) for url in urls]
            results = await asyncio.gather(*tasks)
            parts = [f"=== {url} ===\n{text}" for url, text in zip(urls, results)]
            combined = "\n\n".join(parts)
            return CallToolResult(
                content=[TextContent(type="text", text=combined)]
            )
