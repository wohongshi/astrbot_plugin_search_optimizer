"""
AstrBot 搜索结果优化器 v6
集成 Bing 搜索功能，搜索后立刻压缩再返回给 LLM。
第一次搜索就能节省 Token。
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# ─── 尝试导入 Bing 搜索模块 ─────────────────────────────────
_BING_AVAILABLE = False
_search_bing = None
_fetch_page = None
_BROWSER_CHOICE = "auto"  # 由插件初始化时设置

try:
    # 尝试从已安装的 Bing 插件导入
    import importlib
    import sys

    # 尝试多种导入路径
    for mod_path in (
        "astrbot_plugin_web_tools_ar.tools.bing_search",
        "Bing_Web_Search.tools.bing_search",
    ):
        try:
            mod = importlib.import_module(mod_path)
            _search_bing = getattr(mod, "search_bing", None)
            break
        except ImportError:
            continue

    for mod_path in (
        "astrbot_plugin_web_tools_ar.tools.web_fetch",
        "Bing_Web_Search.tools.web_fetch",
    ):
        try:
            mod = importlib.import_module(mod_path)
            _fetch_page = getattr(mod, "fetch_page", None)
            break
        except ImportError:
            continue

    if _search_bing and _fetch_page:
        _BING_AVAILABLE = True
except Exception:
    pass

# ─── 内置搜索实现（Bing 插件未安装时使用）──────────────────────
if not _BING_AVAILABLE:
    try:
        import threading
        import tempfile
        import shutil
        import platform
        from functools import lru_cache
        from DrissionPage import ChromiumPage, ChromiumOptions

        _next_port = 10000
        _port_lock = threading.Lock()

        def _alloc_port() -> int:
            global _next_port
            with _port_lock:
                port = _next_port
                _next_port += 1
                if _next_port > 60000:
                    _next_port = 10000
                return port

        @lru_cache(maxsize=1)
        def _find_browser_path(browser_choice: str = "auto") -> str:
            system = platform.system()
            if browser_choice == "chromium":
                candidates = ["/usr/bin/chromium", "/usr/bin/chromium-browser"]
            elif browser_choice == "chrome":
                candidates = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]
            elif browser_choice == "edge":
                candidates = ["/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"]
            elif system == "Windows":
                candidates = [
                    "C:/Program Files/Google/Chrome/Application/chrome.exe",
                    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
                    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
                    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
                ]
            elif system == "Darwin":
                candidates = [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                ]
            else:
                candidates = [
                    "/usr/bin/chromium",
                    "/usr/bin/chromium-browser",
                    "/usr/bin/google-chrome",
                    "/usr/bin/google-chrome-stable",
                    "/usr/bin/microsoft-edge",
                    "/usr/bin/microsoft-edge-stable",
                ]
            for p in candidates:
                if os.path.exists(p):
                    return p
            raise FileNotFoundError("未找到浏览器，请安装 Chromium/Chrome/Edge")

        _MAX_CONCURRENT = 4
        _browser_semaphore = threading.Semaphore(_MAX_CONCURRENT)
        _DRISSIONPAGE_USERDATA = os.path.join(tempfile.gettempdir(), "DrissionPage", "userData")

        def _search_bing(keywords: list, max_results: int = 8, timeout: int = 45) -> str:
            """内置 Bing 搜索实现"""
            if not keywords:
                return json.dumps({"error": "未提供关键词"}, ensure_ascii=False)
            keyword = keywords[0] if isinstance(keywords, list) else keywords
            max_retries = 3
            with _browser_semaphore:
                user_data_dir = None
                port = _alloc_port()
                co = ChromiumOptions()
                co.set_browser_path(_find_browser_path(_BROWSER_CHOICE))
                co.set_argument("--disable-blink-features=AutomationControlled")
                co.set_argument("--no-sandbox")
                co.set_argument("--remote-debugging-port=0")
                co.set_argument("--disable-gpu")
                co.set_local_port(port)
                try:
                    user_data_dir = tempfile.mkdtemp(prefix="bing_search_")
                    co.set_argument(f"--user-data-dir={user_data_dir}")
                except Exception:
                    pass
                page = ChromiumPage(addr_or_opts=co)
                try:
                    for retry in range(max_retries):
                        page.get("https://www.bing.com", timeout=timeout)
                        try:
                            search_box = page.ele('#sb_form_q', timeout=5)
                            if search_box:
                                search_box.clear()
                                search_box.input(keyword)
                                search_btn = page.ele('#search_icon', timeout=2)
                                if search_btn:
                                    search_btn.click()
                                else:
                                    search_box.send_keys('\n')
                                page.wait.doc_loaded()
                        except Exception:
                            from urllib.parse import quote
                            page.get(f"https://www.bing.com/search?q={quote(keyword)}&count={max_results}", timeout=timeout)
                        for _ in range(4):
                            try:
                                page.wait.ele_displayed('#b_results', timeout=3)
                                break
                            except Exception:
                                page.refresh()
                        for _ in range(3):
                            try:
                                before = len(page.eles('#b_results .b_algo'))
                            except Exception:
                                before = 0
                            page.scroll.down(500)
                            for _ in range(3):
                                try:
                                    after = len(page.eles('#b_results .b_algo'))
                                except Exception:
                                    after = 0
                                if after > before:
                                    break
                                time.sleep(0.5)
                        b_results = page.ele('#b_results', timeout=1)
                        if b_results:
                            result_items = b_results.children('.b_algo', timeout=0.5) or b_results.children('li', timeout=0.5)
                        else:
                            result_items = page.eles('.b_algo', timeout=1.5)
                        results = []
                        for li in (result_items or [])[:max_results]:
                            try:
                                h2 = li.ele('tag:h2', timeout=0.3)
                                if not h2:
                                    continue
                                a_tag = h2.ele('tag:a', timeout=0.3)
                                if not (a_tag and h2.text and a_tag.attr('href')):
                                    continue
                                title = h2.text.strip()
                                url = a_tag.attr('href')
                                snippet = ''
                                p_tag = li.ele('tag:p', timeout=0.3)
                                if p_tag:
                                    snippet = p_tag.text.strip()
                                date = ''
                                if snippet:
                                    dm = re.search(r'(\d{4}[-年]\d{1,2}[-月]\d{1,2}日?)', snippet)
                                    if dm:
                                        date = dm.group(1).replace('年', '-').replace('月', '-').replace('日', '')
                                source = url.split('/')[2].replace('www.', '') if '/' in url else ''
                                results.append({"rank": len(results)+1, "title": title, "url": url, "snippet": snippet, "date": date, "source": source})
                            except Exception:
                                continue
                        if results:
                            return json.dumps({"query": keyword, "total_results": len(results), "results": results}, ensure_ascii=False, indent=2)
                        time.sleep(1)
                    return json.dumps({"query": keyword, "total_results": 0, "results": [], "message": "多次重试后未获得结果"}, ensure_ascii=False)
                except Exception as e:
                    return json.dumps({"error": str(e), "query": keyword}, ensure_ascii=False)
                finally:
                    try:
                        page.quit()
                    except Exception:
                        pass
                    if user_data_dir:
                        shutil.rmtree(user_data_dir, ignore_errors=True)
                    try:
                        if os.path.isdir(_DRISSIONPAGE_USERDATA):
                            shutil.rmtree(_DRISSIONPAGE_USERDATA, ignore_errors=True)
                    except Exception:
                        pass

        def _fetch_page(url: str, timeout: int = 10, retries: int = 3, max_chars: int = 10000) -> str:
            """内置网页抓取实现"""
            try:
                import trafilatura
                import ipaddress
                from urllib.parse import urlparse
            except ImportError:
                return "缺少依赖: pip install trafilatura"
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return f"不支持的协议: {parsed.scheme}"
            with _browser_semaphore:
                for attempt in range(retries):
                    page = None
                    user_data_dir = None
                    try:
                        port = _alloc_port()
                        co = ChromiumOptions()
                        co.set_browser_path(_find_browser_path(_BROWSER_CHOICE))
                        co.set_argument("--disable-blink-features=AutomationControlled")
                        co.set_argument("--no-sandbox")
                        co.set_argument("--remote-debugging-port=0")
                        co.set_argument("--disable-gpu")
                        co.set_local_port(port)
                        try:
                            user_data_dir = tempfile.mkdtemp(prefix="web_fetch_")
                            co.set_argument(f"--user-data-dir={user_data_dir}")
                        except Exception:
                            pass
                        page = ChromiumPage(addr_or_opts=co)
                        page.get(url, timeout=timeout)
                        page.wait.doc_loaded()
                        try:
                            page.wait.ele_displayed('tag:body', timeout=10)
                        except Exception:
                            pass
                        for _ in range(3):
                            page.scroll.down(2000)
                            try:
                                page.wait.load_complete(timeout=2)
                            except Exception:
                                pass
                        try:
                            html_content = page.html
                            extracted = trafilatura.extract(html_content, include_comments=False, include_tables=True)
                            if extracted:
                                text = re.sub(r'\n\s*\n', '\n\n', extracted.strip())
                                title = ''
                                try:
                                    t = page.ele('tag:title', timeout=3)
                                    if t:
                                        title = t.text.strip()
                                except Exception:
                                    pass
                                if title:
                                    text = f"标题: {title}\n\n{text}"
                                if max_chars > 0 and len(text) > max_chars:
                                    text = text[:max_chars] + "\n\n[内容已截断]"
                                return text
                        except Exception:
                            pass
                        try:
                            body = page.ele('tag:body', timeout=5)
                            text = body.text.strip() if body else ""
                        except Exception:
                            text = ""
                        if max_chars > 0 and len(text) > max_chars:
                            text = text[:max_chars] + "\n\n[内容已截断]"
                        return text or "页面内容为空"
                    except Exception as e:
                        if attempt < retries - 1:
                            time.sleep(2 ** attempt)
                    finally:
                        if page:
                            try:
                                page.quit()
                            except Exception:
                                pass
                        if user_data_dir:
                            shutil.rmtree(user_data_dir, ignore_errors=True)
                        try:
                            if os.path.isdir(_DRISSIONPAGE_USERDATA):
                                shutil.rmtree(_DRISSIONPAGE_USERDATA, ignore_errors=True)
                        except Exception:
                            pass
                return f"获取页面失败，已重试{retries}次"

        _BING_AVAILABLE = True
        logger.info("[搜索优化器] 使用内置搜索实现")
    except ImportError:
        logger.warning("[搜索优化器] 未安装 Bing 插件且缺少 DrissionPage，搜索功能不可用")

# ─── 搜索工具名特征 ────────────────────────────────────────
_SEARCH_TOOL_PATTERNS = (
    "web_search", "web_fetch", "web_search_tool", "web_fetch_tool",
    "bing_search", "google_search",
    "tavily", "brave_search", "duckduckgo",
    "astrbot_execute_shell",
)


def _is_search_tool(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in _SEARCH_TOOL_PATTERNS)


def _is_json_search_result(text: str) -> bool:
    s = text.strip()
    if not s.startswith("{"):
        return False
    try:
        d = json.loads(s)
        if isinstance(d, dict):
            for k in ("results", "items", "data", "hits", "organic"):
                if k in d and isinstance(d[k], list):
                    return True
    except (json.JSONDecodeError, ValueError):
        pass
    return False


# 时间敏感词
_TIME_KEYWORDS = (
    "今天", "今日", "今晚", "今晨", "今早",
    "昨天", "昨日", "昨晚", "前天", "前日", "前晚",
    "明天", "明日", "后天",
    "最新", "最近", "近日", "近期", "近来", "晚近",
    "目前", "当前", "当下", "此刻", "此时", "眼下", "现阶段",
    "如今", "现今", "现时", "现在",
    "刚刚", "刚才", "方才",
    "正在", "即将", "马上", "就要", "快要",
    "本周", "这周", "上周", "下周",
    "本月", "上个月", "下个月", "这个月",
    "今年", "去年", "明年",
    "这几天", "前段时间", "接下来", "今后", "往后", "此后",
    "today", "tonight", "yesterday", "tomorrow",
    "this week", "this month", "this year",
    "last night", "last week", "last month",
    "latest", "recent", "recently",
    "currently", "presently", "just now", "just",
    "now", "nowadays", "ongoing", "upcoming", "breaking",
    "right now", "at the moment", "so far",
)


def _normalize_query(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r'\s+', ' ', t)
    t = re.sub(r'[，。、；：！？,.;:!?\-\[\]\(\){}\"\'<>]', '', t)
    has_time = any(kw in t for kw in _TIME_KEYWORDS)
    if has_time:
        today = datetime.now().strftime("%Y%m%d")
        t = f"{t}__date_{today}"
    return t


# ══════════════════════════════════════════════════════════
# FunctionTool 定义：搜索 + 预处理
# ══════════════════════════════════════════════════════════

try:
    from pydantic import Field
    from pydantic.dataclasses import dataclass
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.tool import FunctionTool, ToolExecResult, ToolSet
    from astrbot.core.astr_agent_context import AstrAgentContext
    from mcp.types import CallToolResult, TextContent

    class OptimizedSearchTool(FunctionTool[AstrAgentContext]):
        """优化版 Bing 搜索：搜索后立刻压缩结果。"""

        name: str = "web_search"
        description: str = "并发搜索互联网，可同时传入多个关键词。返回压缩后的精炼结果。"
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "搜索关键词列表，可同时传入多个关键词并发搜索",
                    },
                },
                "required": ["keywords"],
            }
        )

        _plugin = None  # 指向插件实例

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            keywords = kwargs.get("keywords", [])
            if not keywords:
                return ToolExecResult(content="未提供搜索关键词")

            plugin = self._plugin
            if not plugin or not _BING_AVAILABLE:
                return ToolExecResult(content="搜索功能不可用")

            # 执行搜索
            loop = asyncio.get_event_loop()
            raw_result = await loop.run_in_executor(None, _search_bing, keywords)

            # 预处理
            processed = await plugin._preprocess_tool_content("web_search", raw_result, keywords)
            return ToolExecResult(content=processed)

    class OptimizedFetchTool(FunctionTool[AstrAgentContext]):
        """优化版网页抓取：抓取后立刻压缩内容。"""

        name: str = "web_fetch"
        description: str = "并发抓取多个 URL 的页面正文，返回压缩后的精炼内容。"
        parameters: dict = Field(
            default_factory=lambda: {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要抓取的 URL 列表",
                    },
                },
                "required": ["urls"],
            }
        )

        _plugin = None

        async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
            urls = kwargs.get("urls", [])
            if not urls:
                return ToolExecResult(content="未提供 URL")

            plugin = self._plugin
            if not plugin or not _BING_AVAILABLE:
                return ToolExecResult(content="抓取功能不可用")

            loop = asyncio.get_event_loop()

            # 并发抓取
            tasks = [loop.run_in_executor(None, _fetch_page, url) for url in urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            parts = []
            for url, text in zip(urls, results):
                if isinstance(text, Exception):
                    parts.append(f"=== {url} ===\n抓取失败: {text}")
                else:
                    parts.append(f"=== {url} ===\n{text}")

            raw_result = "\n\n".join(parts)

            # 预处理
            processed = await plugin._preprocess_tool_content("web_fetch", raw_result, urls)
            return ToolExecResult(content=processed)

    _TOOLS_AVAILABLE = True
except ImportError:
    _TOOLS_AVAILABLE = False
    logger.info("[搜索优化器] FunctionTool 框架不可用，仅作为后处理插件运行")


# ══════════════════════════════════════════════════════════
# 插件主类
# ══════════════════════════════════════════════════════════

class SearchOptimizerPlugin(Star):
    """搜索结果优化器：集成搜索 + 压缩 + 缓存。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._load_config()

        self._total_chars_saved = 0
        self._preprocess_count = 0
        self._cache_hits = 0
        self._cache_dirty = False

        # 缓存
        self._cache_dir = str(Path(get_astrbot_data_path()) / "plugin_data" / "search_optimizer")
        os.makedirs(self._cache_dir, exist_ok=True)
        self._cache_file = os.path.join(self._cache_dir, "cache.json")
        self._cache: dict = self._load_cache()
        self._clean_expired_cache()
        self._evict_lru()

        # 设置浏览器选择
        global _BROWSER_CHOICE
        _BROWSER_CHOICE = self.browser

        # 注册优化版搜索工具
        self._tools_registered = False
        if _TOOLS_AVAILABLE and _BING_AVAILABLE:
            self._register_tools()

        logger.info(
            f"[搜索优化器] 启动: 搜索={'✅' if _BING_AVAILABLE else '❌'} "
            f"工具={'✅' if _TOOLS_AVAILABLE else '❌'} "
            f"模式={'LLM' if self.preprocess_provider_id else '规则'}"
        )

    def _load_config(self):
        self.preprocess_provider_id = self.config.get("preprocess_provider_id", "")
        self.optimize_search = self.config.get("optimize_search", False)
        self.cache_days = self.config.get("cache_days", 3)
        self.max_cache_entries = self.config.get("max_cache_entries", 200)
        self.max_summary_chars = self.config.get("max_summary_chars", 1500)
        self.small_model_answer = self.config.get("small_model_answer", False)
        self.browser = self.config.get("browser", "auto")

    def _register_tools(self):
        """注册优化版搜索工具，替换原始工具。"""
        try:
            search_tool = OptimizedSearchTool()
            search_tool._plugin = self
            fetch_tool = OptimizedFetchTool()
            fetch_tool._plugin = self
            self.context.add_llm_tools(search_tool, fetch_tool)
            self._tools_registered = True
            logger.info("[搜索优化器] 已注册优化版 web_search / web_fetch 工具")
        except Exception as e:
            logger.warning(f"[搜索优化器] 注册工具失败: {e}")

    # ══════════════════════════════════════════════════════════
    # 核心：搜索结果预处理
    # ══════════════════════════════════════════════════════════

    async def _preprocess_tool_content(
        self, tool_name: str, raw_content: str, args
    ) -> str:
        """工具执行后立刻预处理，返回压缩内容。"""
        if not raw_content or len(raw_content) < 2000:
            return raw_content

        original_len = len(raw_content)
        logger.info(f"[搜索优化器] {tool_name} 原始 {original_len} 字符，开始压缩...")

        # 提取 URL（去噪前）
        source_urls = self._extract_urls(raw_content)

        # 去噪
        cleaned = self._strip_noise(raw_content)
        if not cleaned:
            return raw_content

        # 压缩
        if self.preprocess_provider_id:
            summary = await self._llm_preprocess(tool_name, args, cleaned, source_urls)
        else:
            summary = self._rule_extract(tool_name, args, cleaned, source_urls)

        if summary and len(summary) < original_len:
            saved = original_len - len(summary)
            self._total_chars_saved += saved
            self._preprocess_count += 1
            mode = "LLM" if self.preprocess_provider_id else "规则"
            logger.info(
                f"[搜索优化器] [{mode}] {original_len}→{len(summary)} "
                f"(省 {saved}，累计 {self._total_chars_saved})"
            )
            # 缓存
            if self.optimize_search:
                cache_key = self._extract_query_from_args(args)
                if cache_key:
                    self._cache_set(cache_key, summary)
            return summary

        return raw_content

    def _extract_query_from_args(self, args) -> str:
        if isinstance(args, list):
            return " ".join(str(a) for a in args)
        if isinstance(args, dict):
            for k in ("keywords", "query", "q", "urls"):
                v = args.get(k)
                if v:
                    return " ".join(str(x) for x in v) if isinstance(v, list) else str(v)
        return ""

    # ══════════════════════════════════════════════════════════
    # 缓存系统
    # ══════════════════════════════════════════════════════════

    def _load_cache(self) -> dict:
        try:
            if os.path.exists(self._cache_file):
                with open(self._cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_cache(self, force=False):
        if not force and not self._cache_dirty:
            return
        try:
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            self._cache_dirty = False
        except Exception:
            pass

    def _clean_expired_cache(self):
        now = time.time()
        max_age = self.cache_days * 86400
        expired = [k for k, v in self._cache.items() if now - v.get("ts", 0) > max_age]
        for k in expired:
            del self._cache[k]
        if expired:
            self._cache_dirty = True
            self._save_cache()

    def _evict_lru(self):
        if len(self._cache) <= self.max_cache_entries:
            return
        sorted_keys = sorted(
            self._cache.keys(),
            key=lambda k: (self._cache[k].get("hits", 0), self._cache[k].get("ts", 0)),
        )
        to_remove = len(self._cache) - self.max_cache_entries
        for k in sorted_keys[:to_remove]:
            del self._cache[k]
        if to_remove > 0:
            self._cache_dirty = True

    def _cache_get(self, query: str) -> Optional[str]:
        key = _normalize_query(query)
        entry = self._cache.get(key)
        if not entry:
            q_words = set(key.split())
            for cached_key, cached_entry in self._cache.items():
                c_words = set(cached_key.split())
                if not q_words or not c_words:
                    continue
                if len(q_words & c_words) / min(len(q_words), len(c_words)) > 0.6:
                    entry = cached_entry
                    key = cached_key
                    break
        if not entry:
            return None
        max_age = self.cache_days * 86400
        if time.time() - entry.get("ts", 0) > max_age:
            del self._cache[key]
            return None
        entry["hits"] = entry.get("hits", 0) + 1
        self._cache_dirty = True
        return entry.get("content", "")

    def _cache_set(self, query: str, content: str):
        key = _normalize_query(query)
        if not key:
            return
        self._cache[key] = {"content": content, "ts": time.time(), "hits": 0, "chars": len(content)}
        self._cache_dirty = True
        self._evict_lru()

    def _cache_clear(self):
        count = len(self._cache)
        self._cache.clear()
        self._cache_dirty = True
        self._save_cache(force=True)
        return count

    # ══════════════════════════════════════════════════════════
    # 钩子：缓存注入
    # ══════════════════════════════════════════════════════════

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.optimize_search:
            return
        user_msg = self._get_user_message(req)
        if not user_msg or len(user_msg) < 5:
            return
        cached = self._cache_get(user_msg)
        if not cached:
            return
        self._cache_hits += 1
        logger.info(f"[搜索优化器] 缓存命中 (累计 {self._cache_hits}): {user_msg[:30]}...")

        try:
            from astrbot.core.agent.message import TextPart
            if self.small_model_answer and self.preprocess_provider_id:
                answer = await self._small_model_answer(user_msg, cached)
                if answer:
                    inject = (
                        f"<cached_answer>\n"
                        f"以下是根据搜索结果生成的回答，直接输出即可。"
                        f"已有足够信息，无需调用 web_search 或 web_fetch。\n\n"
                        f"{answer}\n"
                        f"</cached_answer>"
                    )
                    req.extra_user_content_parts.append(TextPart(text=inject).mark_as_temp())
                    logger.info(f"[搜索优化器] 小模型已生成答案 ({len(answer)} 字符)")
                return

            inject = (
                f"<cached_search_results>\n"
                f"以下是之前搜索「{user_msg[:50]}」的预处理结果。"
                f"已有足够信息回答此问题，无需调用 web_search 或 web_fetch。"
                f"请直接基于以下内容回答：\n\n{cached}\n"
                f"</cached_search_results>"
            )
            req.extra_user_content_parts.append(TextPart(text=inject).mark_as_temp())
        except Exception as e:
            logger.warning(f"[搜索优化器] 注入失败: {e}")

    def _get_user_message(self, req: ProviderRequest) -> str:
        try:
            if hasattr(req, "contexts") and req.contexts:
                for msg in reversed(req.contexts):
                    if hasattr(msg, "role") and msg.role == "user":
                        content = getattr(msg, "content", None)
                        if isinstance(content, str):
                            return content.strip()
                        if isinstance(content, list):
                            for item in content:
                                if hasattr(item, "text"):
                                    return item.text.strip()
        except Exception:
            pass
        return ""

    async def _small_model_answer(self, user_msg: str, cached: str) -> Optional[str]:
        prompt = (
            f"用户问题：{user_msg}\n\n"
            f"搜索结果摘要：\n{cached}\n\n"
            f"请根据搜索结果回答用户问题。如果搜索结果中没有相关信息，请说明。"
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.preprocess_provider_id, prompt=prompt,
            )
            return resp.completion_text
        except Exception as e:
            logger.error(f"[搜索优化器] 小模型回答失败: {e}")
            return None

    # ══════════════════════════════════════════════════════════
    # 钩子：on_agent_done（兜底，处理其他来源的搜索结果）
    # ══════════════════════════════════════════════════════════

    @filter.on_agent_done()
    async def on_agent_done(self, event, run_context, resp):
        """兜底：扫描对话中的搜索结果，预处理并缓存。"""
        try:
            ctx = run_context
            messages = None
            for attr in ('messages', 'conversation_context', 'history', 'contexts'):
                messages = getattr(ctx, attr, None)
                if messages and isinstance(messages, list):
                    break
            if not messages:
                inner = getattr(ctx, 'context', None)
                if inner:
                    for attr in ('messages', 'conversation_context', 'history'):
                        messages = getattr(inner, attr, None)
                        if messages and isinstance(messages, list):
                            break
            if not messages:
                return
            for msg in messages:
                role = getattr(msg, 'role', None)
                if role not in ('tool', 'function'):
                    continue
                tool_name = getattr(msg, 'name', '') or ''
                content = getattr(msg, 'content', None)
                text = self._extract_text(content)
                if not text or len(text) < 2000 or '<compressed>' in text:
                    continue
                is_search = (
                    _is_search_tool(tool_name)
                    or _is_json_search_result(text)
                    or len(text) > 5000
                )
                if not is_search:
                    continue
                logger.info(f"[搜索优化器] 兜底处理: {tool_name} ({len(text)} 字符)")
                source_urls = self._extract_urls(text)
                cleaned = self._strip_noise(text)
                if not cleaned:
                    continue
                if self.preprocess_provider_id:
                    summary = await self._llm_preprocess(tool_name, {}, cleaned, source_urls)
                else:
                    summary = self._rule_extract(tool_name, {}, cleaned, source_urls)
                if summary and len(summary) < len(text):
                    summary = f"<compressed>\n{summary}"
                    self._replace_content(msg, content, summary)
                    saved = len(text) - len(summary)
                    self._total_chars_saved += saved
                    self._preprocess_count += 1
                    logger.info(f"[搜索优化器] 兜底压缩: {len(text)}→{len(summary)} (省 {saved})")
        except Exception as e:
            logger.error(f"[搜索优化器] on_agent_done 失败: {e}")

    # ══════════════════════════════════════════════════════════
    # 去噪 + 规则提取 + LLM 压缩（与之前版本相同）
    # ══════════════════════════════════════════════════════════

    def _strip_noise(self, text: str) -> str:
        if not text:
            return text
        text = re.sub(r'https?://[^\s<>\]\)\"\']+', '', text)
        text = re.sub(r'www\.[^\s<>\]\)\"\']+', '', text)
        error_pats = [
            r'^(?:\[[\w\s]*\]\s*|[\w_]+:\s*|\s*)*(?:获取页面失败|访问失败|抓取失败|抓取超时|页面加载超时|连接超时|搜索.*超时|页面内容为空|无法访问|多次重试.*仍未)\s*[,，:：(（].*$',
            r'^(?:\[[\w\s]*\]\s*|\s*)*(?:获取页面失败|访问失败|抓取失败|抓取超时|页面加载超时|连接超时|搜索.*超时|页面内容为空|无法访问)\s*$',
            r'^(?:\[[\w\s]*\]\s*|[\w_]+:\s*|\s*)*(?:failed to fetch|fetch failed|connection refused|403 forbidden|404 not found|500 internal|empty page).*$',
            r'^(?:\[[\w\s]*\]\s*|\s*)*(?:重试|retry|尝试)\s*\d+/\d+.*$',
        ]
        for pat in error_pats:
            text = re.sub(pat, '', text, flags=re.IGNORECASE | re.MULTILINE)
        text = re.sub(r'\[内容已截断.*?\]', '', text)
        text = re.sub(r'标题:\s*\n', '', text)
        text = re.sub(r'===\s*===\s*\n?', '', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()
        return text if len(text) >= 50 else ""

    async def _llm_preprocess(self, tool_name, tool_args, content, source_urls):
        prompt = self._build_prompt(tool_name, tool_args, content)
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.preprocess_provider_id, prompt=prompt,
            )
            result = resp.completion_text
            if not result:
                return None
            if source_urls:
                result += "\n\n来源:\n" + "\n".join(f"- {u}" for u in source_urls[:5])
            return result
        except Exception as e:
            logger.error(f"[搜索优化器] LLM 失败，回退规则: {e}")
            return self._rule_extract(tool_name, tool_args, content, source_urls)

    def _build_prompt(self, tool_name, tool_args, content):
        name = tool_name.lower()
        if "fetch" in name or "browse" in name:
            p = "从以下网页正文中提取关键信息，去除无关内容，输出精炼摘要。"
        elif "search" in name:
            p = "从以下搜索结果中提取关键信息，保留标题和核心内容。"
        else:
            p = "从以下内容中提取关键信息，输出精炼摘要。"
        parts = [p]
        if isinstance(tool_args, dict):
            for k in ("keywords", "query", "urls", "url"):
                if k in tool_args:
                    parts.append(f"\n{k}: {tool_args[k]}")
        elif isinstance(tool_args, list):
            parts.append(f"\n关键词: {', '.join(str(a) for a in tool_args)}")
        parts.append(f"\n--- 原始内容 ---\n{content}")
        parts.append(f"\n--- 输出摘要（≤{self.max_summary_chars} 字符）---")
        return "\n".join(parts)

    def _rule_extract(self, tool_name, tool_args, content, source_urls=None):
        s = content.strip()
        if re.search(r'===\s*https?://', content):
            result = self._extract_multi_fetch(content)
        elif _is_json_search_result(s):
            result = self._extract_json_search(s)
        else:
            result = self._extract_web_text(s)
        if source_urls and result:
            result += "\n\n来源:\n" + "\n".join(f"- {u}" for u in source_urls[:5])
        return result

    def _extract_json_search(self, text):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return self._extract_web_text(text)
        results = None
        for k in ("results", "items", "data", "hits", "organic"):
            if k in data and isinstance(data[k], list):
                results = data[k]
                break
        if not results:
            return self._extract_web_text(text)
        cleaned = []
        seen_snippets = []
        for item in results:
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            snippet = (item.get("snippet") or item.get("description") or "").strip()
            date = (item.get("date") or "").strip()
            source = (item.get("source") or "").strip()
            if not title and not url:
                continue
            snippet = self._clean_snippet(snippet, date, source)
            if snippet and self._is_dup_snippet(snippet, seen_snippets):
                continue
            if snippet:
                seen_snippets.append(snippet)
            cleaned.append({"title": title, "snippet": snippet, "date": date})
        if not cleaned:
            return self._extract_web_text(text)
        lines = []
        q = data.get("query", "")
        if q:
            lines.append(f"搜索: {q}")
        for i, item in enumerate(cleaned[:8], 1):
            line = f"{i}. {item['title']}"
            if item['date']:
                line += f" ({item['date']})"
            if item['snippet']:
                line += f"\n   {item['snippet']}"
            lines.append(line)
        result = "\n".join(lines)
        if len(result) > self.max_summary_chars:
            result = result[:self.max_summary_chars] + "\n...[已截断]"
        return result

    def _clean_snippet(self, snippet, date="", source=""):
        if not snippet:
            return ""
        if date:
            snippet = re.sub(r'\d{4}[-年/]\d{1,2}[-月/]\d{1,2}[日]?\s*', '', snippet, count=1)
        if source:
            snippet = re.sub(rf'\s*[-–—|·•]\s*{re.escape(source)}\s*$', '', snippet)
        for p in [r'\s*[-–—]\s*$', r'\s*\.\.\.?$', r'\s*[-–—|·]\s*$']:
            snippet = re.sub(p, '', snippet)
        for p in [r'^\s*Web\s*', r'^\s*网页\s*', r'^\s*视频\s*']:
            snippet = re.sub(p, '', snippet, flags=re.IGNORECASE)
        snippet = re.sub(r'\s+', ' ', snippet).strip()
        if len(snippet) > 200:
            t = snippet[:200]
            lp = max(t.rfind('。'), t.rfind('. '), t.rfind('！'))
            snippet = t[:lp+1] if lp > 100 else t + "..."
        return snippet

    def _is_dup_snippet(self, snippet, existing, threshold=0.4):
        if not existing:
            return False
        s1 = snippet[:100]
        b1 = set(s1[i:i+2] for i in range(len(s1)-1))
        if not b1:
            return False
        for prev in existing:
            s2 = prev[:100]
            b2 = set(s2[i:i+2] for i in range(len(s2)-1))
            if not b2:
                continue
            if len(b1 & b2) / len(b1 | b2) > threshold:
                return True
        return False

    def _extract_multi_fetch(self, content):
        sections = re.split(r'===\s*(https?://[^\s=]+)\s*===', content)
        if len(sections) < 2:
            return self._extract_web_text(content)
        parts = []
        per_page = self.max_summary_chars // max(1, len(sections) // 2)
        for i in range(1, len(sections), 2):
            url = sections[i].strip()
            body = sections[i+1].strip() if i+1 < len(sections) else ""
            if not body:
                continue
            cleaned = self._clean_web_text(body)
            if len(cleaned) > per_page:
                cleaned = self._smart_truncate(cleaned, per_page)
            parts.append(f"=== {url} ===\n{cleaned}")
        return "\n\n".join(parts)

    def _extract_web_text(self, text):
        cleaned = self._clean_web_text(text)
        if len(cleaned) > self.max_summary_chars:
            cleaned = self._smart_truncate(cleaned, self.max_summary_chars)
        return cleaned

    def _clean_web_text(self, text):
        text = re.sub(r'^标题:\s*.*\n+', '', text, count=1)
        text = re.sub(r'\[内容已截断.*?\]', '', text)
        for p in [
            r'(?:Copyright|©|版权).*?\n',
            r'(?:All Rights Reserved|保留所有权利).*?\n',
            r'(?:ICP备|备案号|公安备).*?\n',
        ]:
            text = re.sub(p, '', text, flags=re.IGNORECASE)
        # Wiki/Fandom 噪音
        wiki_noise = [
            r'\{\{[^}]*\}\}', r'\[\[Category:[^\]]*\]\]',
            r'\[\[File:[^\]]*\]\]', r'\[\[Image:[^\]]*\]\]',
            r'<ref[^>]*>.*?</ref>', r'<ref[^>]*/>',
            r'</?nowiki>', r'\{\|[^|]*\|}',
            r'^\s*\|.*$', r'^\s*!\s.*$',
        ]
        for pat in wiki_noise:
            text = re.sub(pat, '', text, flags=re.IGNORECASE | re.MULTILINE)
        text = re.sub(r'\[\[([^\]|]*?)\]\]', r'\1', text)
        text = re.sub(r'\[\[[^\|]*?\|([^\]]*?)\]\]', r'\1', text)
        lines = text.split('\n')
        filtered = []
        for line in lines:
            s = line.strip()
            if not s:
                filtered.append('')
            elif len(s) > 15 or any(c in s for c in '。，、；！？'):
                filtered.append(line)
        return '\n'.join(filtered).strip()

    def _smart_truncate(self, text, max_chars):
        if len(text) <= max_chars:
            return text
        t = text[:max_chars]
        for sep in ('\n\n', '。', '. ', '！', '？'):
            pos = t.rfind(sep)
            if pos > max_chars * 0.5:
                return t[:pos + len(sep)] + "\n...[已截断]"
        return t + "\n...[已截断]"

    def _extract_urls(self, text):
        urls = re.findall(r'https?://[^\s<>\]\)\"\']+', text)
        seen = set()
        out = []
        for u in urls:
            u = u.rstrip(".,;:!?。，；：！？")
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _extract_text(self, content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for item in content:
                if hasattr(item, "text"):
                    texts.append(item.text)
                elif isinstance(item, str):
                    texts.append(item)
            return "\n".join(texts) if texts else None
        return None

    def _replace_content(self, msg, content, new_text):
        if isinstance(content, str):
            msg.content = new_text
        elif isinstance(content, list):
            for item in content:
                if hasattr(item, "text"):
                    item.text = new_text
                    break

    # ══════════════════════════════════════════════════════════
    # 指令
    # ══════════════════════════════════════════════════════════

    @filter.command("搜索优化器")
    async def cmd_status(self, event: AstrMessageEvent):
        model = "规则提取"
        if self.preprocess_provider_id:
            model = await self._get_provider_display_name(self.preprocess_provider_id) or self.preprocess_provider_id
        cache_count = len(self._cache)
        ratio = "-"
        if self._preprocess_count > 0 and self._total_chars_saved > 0:
            avg = self._total_chars_saved / self._preprocess_count
            ratio = f"~{avg / (avg + self.max_summary_chars) * 100:.0f}%"
        tokens_saved = self._total_chars_saved / 1.5 if self._total_chars_saved > 0 else 0
        total_queries = self._cache_hits + self._preprocess_count
        hit_rate = f"{self._cache_hits / total_queries * 100:.0f}%" if total_queries > 0 else "-"
        if self.small_model_answer and self.preprocess_provider_id:
            mode_desc = "小模型直接回答"
        elif self.preprocess_provider_id:
            mode_desc = "LLM 摘要压缩"
        else:
            mode_desc = "规则提取（零 LLM）"
        lines = [
            "📊 搜索结果优化器",
            "─" * 24,
            f"模式: {mode_desc}",
            f"模型: {model}",
            f"搜索功能: {'✅ 内置' if self._tools_registered else '✅ 依赖外部插件' if _BING_AVAILABLE else '❌ 不可用'}",
            f"摘要上限: {self.max_summary_chars} 字符",
            "─" * 24,
            f"优化搜索: {'✅' if self.optimize_search else '❌'}",
            f"小模型回答: {'✅' if self.small_model_answer else '❌'}",
            f"缓存: {cache_count}/{self.max_cache_entries} 条（{self.cache_days} 天过期）",
            "─" * 24,
            f"累计处理: {self._preprocess_count} 次",
            f"缓存命中: {self._cache_hits} 次（命中率 {hit_rate}）",
            f"压缩率: {ratio}",
            f"节省字符: {self._total_chars_saved:,}",
            f"节省 Token: ~{int(tokens_saved):,}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("清除缓存")
    async def cmd_clear_cache(self, event: AstrMessageEvent):
        count = self._cache_clear()
        yield event.plain_result(f"✅ 已清除 {count} 条缓存")

    # ══════════════════════════════════════════════════════════
    # Provider 辅助
    # ══════════════════════════════════════════════════════════

    async def _get_all_providers(self):
        result = []
        try:
            pm = getattr(self.context, "provider_manager", None)
            if not pm:
                return result
            providers = getattr(pm, "providers", None)
            if not providers:
                return result
            if isinstance(providers, dict):
                for pid, prov in providers.items():
                    info = {"id": pid}
                    m = getattr(prov, "model_name", None) or getattr(prov, "model", None)
                    if m:
                        info["model"] = str(m)
                    result.append(info)
        except Exception:
            pass
        return result

    async def _get_provider_display_name(self, pid):
        if not pid:
            return None
        for p in await self._get_all_providers():
            if p["id"] == pid:
                return p.get("model", pid)
        return None

    # ══════════════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════════════

    async def terminate(self):
        self._save_cache(force=True)
        if self._preprocess_count > 0 or self._cache_hits > 0:
            logger.info(
                f"[搜索优化器] 停止: 处理 {self._preprocess_count} 次，"
                f"缓存命中 {self._cache_hits} 次，"
                f"节省 {self._total_chars_saved} 字符"
            )
