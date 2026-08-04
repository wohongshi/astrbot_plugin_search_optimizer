"""网页抓取工具 — 双引擎：aiohttp 快速抓取 + Playwright 无头浏览器兜底"""

import asyncio
import ipaddress
import os
import re
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

# ── Playwright 可用性检测 ──
_PLAYWRIGHT_AVAILABLE = False
try:
    from playwright.async_api import async_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)

# ── 共享 aiohttp Session ──
_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()

# ── Playwright 浏览器实例 ──
_browser = None
_browser_lock = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    global _session
    async with _session_lock:
        if _session is None or _session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            _session = aiohttp.ClientSession(timeout=timeout)
        return _session


# 系统 Chromium 路径（apt install chromium）
_SYSTEM_CHROMIUM_PATHS = [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
]


def _find_system_chromium() -> str | None:
    """查找系统安装的 Chromium/Chrome。"""
    import shutil
    for p in _SYSTEM_CHROMIUM_PATHS:
        if shutil.which(p) or os.path.isfile(p):
            return p
    return None


async def _get_browser():
    """获取共享的 Playwright 浏览器实例（优先用系统 Chromium）。"""
    global _browser
    if not _PLAYWRIGHT_AVAILABLE:
        return None
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            try:
                pw = await async_playwright().start()
                chromium_path = _find_system_chromium()
                launch_args = {
                    "headless": True,
                    "args": [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-extensions",
                    ],
                }
                if chromium_path:
                    launch_args["executable_path"] = chromium_path
                    logger.info(f"[web_fetch] 使用系统 Chromium: {chromium_path}")
                _browser = await pw.chromium.launch(**launch_args)
            except Exception as e:
                logger.warning(f"[web_fetch] Playwright 启动失败: {e}")
                _browser = None
        return _browser


async def close_session():
    global _session, _browser
    async with _session_lock:
        if _session and not _session.closed:
            await _session.close()
        _session = None
    if _PLAYWRIGHT_AVAILABLE:
        async with _browser_lock:
            if _browser:
                try:
                    await _browser.close()
                except Exception:
                    pass
                _browser = None


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
    """异步抓取网页。先用 aiohttp 快速抓取，内容不足时自动用 Playwright 渲染。"""
    error = validate_url(url)
    if error:
        return error

    # ── 第一步：aiohttp 快速抓取 ──
    text = await _fetch_aiohttp(url, timeout, max_chars)

    # 判断内容是否足够（JS 渲染页面通常只有很少内容）
    if text and len(text) > 200 and "Loading" not in text[:100]:
        return text

    # ── 第二步：Playwright 无头浏览器渲染 ──
    if _PLAYWRIGHT_AVAILABLE:
        pw_text = await _fetch_playwright(url, timeout, max_chars)
        if pw_text and len(pw_text) > len(text or ""):
            return pw_text

    # 返回 aiohttp 的结果（可能是空的或 Loading）
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

            if not text:
                text = ""

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


async def _fetch_playwright(url: str, timeout: int, max_chars: int) -> str:
    """Playwright 无头浏览器渲染抓取。"""
    browser = await _get_browser()
    if not browser:
        return ""

    page = None
    try:
        page = await browser.new_page(
            user_agent=_USER_AGENT,
            locale="zh-CN",
        )
        await page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
        # 等待页面渲染
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        # 额外等待动态内容
        await asyncio.sleep(1)

        # 获取渲染后的文本
        text = await page.evaluate("""
            () => {
                // 移除无关元素
                document.querySelectorAll('script, style, nav, header, footer, aside, iframe').forEach(e => e.remove());
                return document.body ? document.body.innerText : '';
            }
        """)

        if not text or len(text.strip()) < 50:
            return ""

        text = text.strip()

        # 获取标题
        title = await page.title()
        if title:
            text = f"标题: {title}\n\n{text}"

        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars] + "\n\n[内容已截断]"

        return text

    except Exception as e:
        logger.warning(f"[web_fetch] Playwright 抓取失败 ({url}): {e}")
        return ""
    finally:
        if page:
            try:
                await page.close()
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
            "支持 JS 渲染页面（自动使用无头浏览器）。urls 传 URL 列表。"
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

            tasks = [
                fetch_page_async(url, timeout=self.fetch_timeout)
                for url in urls
            ]
            results = await asyncio.gather(*tasks)

            parts = []
            for url, text in zip(urls, results):
                parts.append(f"=== {url} ===\n{text}")

            combined = "\n\n".join(parts)
            return CallToolResult(
                content=[TextContent(type="text", text=combined)]
            )
