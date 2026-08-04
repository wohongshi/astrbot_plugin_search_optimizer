"""网页抓取工具 — 异步 aiohttp + trafilatura 实现，无需浏览器"""

import asyncio
import ipaddress
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
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


def validate_url(url: str) -> str | None:
    """校验 URL 合法性和安全性。返回 None=通过，返回 str=错误信息。"""
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
        pass  # 域名，跳过 IP 检查

    return None


async def fetch_page_async(
    url: str,
    timeout: int = 30,
    max_chars: int = 10000,
) -> str:
    """异步抓取网页内容，返回纯文本。"""
    error = validate_url(url)
    if error:
        return error

    headers = {
        "User-Agent": _USER_AGENT,
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
                    allow_redirects=True,
                ) as resp:
                    if resp.status != 200:
                        last_error = f"HTTP {resp.status}: {resp.reason}"
                        await asyncio.sleep(1)
                        continue
                    html = await resp.text(errors="replace")

            # 用 trafilatura 提取正文
            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                url=url,
            )

            if extracted:
                text = extracted.strip()
            else:
                # fallback: 从 HTML 中粗提取
                text = _fallback_extract(html)

            if not text:
                text = "页面内容为空"

            # 提取标题
            title = ""
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            if title_match:
                title = title_match.group(1).strip()

            if title:
                text = f"标题: {title}\n\n{text}"

            # 截断
            if max_chars > 0 and len(text) > max_chars:
                text = text[:max_chars] + "\n\n[内容已截断，全文超过字符限制]"

            return text

        except asyncio.TimeoutError:
            last_error = f"请求超时 ({timeout}s)"
            await asyncio.sleep(1)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            await asyncio.sleep(1)

    return f"获取页面失败（重试 {max_retries} 次）: {last_error}"


def _fallback_extract(html: str) -> str:
    """从 HTML 粗提取文本（trafilatura 失败时的降级方案）。"""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        # 移除 script/style
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # 清理多余空行
        text = re.sub(r"\n\s*\n", "\n\n", text)
        return text.strip()
    except Exception:
        return ""


if _ASTRBOT_AVAILABLE:

    @dataclass
    class WebFetchTool(FunctionTool):
        name: str = "web_fetch"
        description: str = "抓取指定 URL 的网页内容并返回文本。urls 传 URL 列表。"
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

            # 并行抓取
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
