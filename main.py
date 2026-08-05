"""
AstrBot 搜索结果优化器 v6.1.0
功能：
1. 内置 web_search + web_fetch 工具（自包含，无需额外插件）
2. 两阶段压缩：规则提取 → 小模型精简（可选）
3. 缓存预处理结果，命中缓存时跳过搜索，直接注入上下文
4. LRU 缓存淘汰，防止缓存无限增长
5. 搜索结果相关性检测，自动去虚词重搜
6. 多 URL 并行预处理
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

from .tools.bing_search import BingSearchTool
from .tools.web_fetch import WebFetchTool

# ─── 搜索工具名特征 ────────────────────────────────────────
_SEARCH_TOOL_PATTERNS = (
    "web_search", "web_fetch", "web_search_tool", "web_fetch_tool",
    "bing_search", "google_search",
    "tavily", "brave_search", "duckduckgo",
)

# shell 输出中出现这些域名时，视为搜索结果
_SEARCH_DOMAIN_HINTS = (
    "so.com/s", "baidu.com/s", "bing.com/search", "google.com/search",
    "sogou.com/web", "yandex.com/search", "duckduckgo.com",
    "search.yahoo.com", "bilibili.com/search", "zhihu.com/search",
)


def _is_search_tool(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in _SEARCH_TOOL_PATTERNS)


def _is_shell_search(tool_name: str, tool_args: dict | None, text: str) -> bool:
    """判断 shell 工具的输出是否为网页搜索结果。"""
    if "execute_shell" not in tool_name.lower() and "shell" not in tool_name.lower():
        return False
    # 检查命令参数中是否包含搜索 URL
    cmd = ""
    if tool_args:
        cmd = str(tool_args.get("command", "")).lower()
    if any(d in cmd for d in _SEARCH_DOMAIN_HINTS):
        return True
    # 检查输出内容是否包含搜索结果特征（大量 URL + 短文本片段）
    url_count = len(re.findall(r'https?://', text))
    if url_count >= 3 and len(text) > 2000:
        return True
    return False


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
    t = re.sub(r'[，。、；：！？,.;:?\-\[\]\(\)\{\}\"\'<>]', '', t)
    has_time = any(kw in t for kw in _TIME_KEYWORDS)
    if has_time:
        today = datetime.now().strftime("%Y%m%d")
        t = f"{t}__date_{today}"
    return t


class SearchOptimizerPlugin(Star):
    """搜索结果优化器：内置搜索/抓取工具 + 两阶段压缩 + 缓存加速。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._load_config()

        self._total_chars_saved = 0
        self._preprocess_count = 0
        self._cache_hits = 0
        self._cache_dirty = False

        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        self._cache_dir = str(
            Path(get_astrbot_data_path()) / "plugin_data" / "search_optimizer"
        )
        os.makedirs(self._cache_dir, exist_ok=True)
        self._cache_file = os.path.join(self._cache_dir, "cache.json")
        self._cache: dict = self._load_cache()
        self._clean_expired_cache()
        self._evict_lru()

        search_timeout = self.config.get("search_timeout", 30)
        try:
            self.context.add_llm_tools(
                BingSearchTool(search_timeout=search_timeout),
                WebFetchTool(fetch_timeout=search_timeout),
            )
            logger.info("[搜索优化器] 内置工具注册成功: web_search, web_fetch")
        except Exception as e:
            logger.error(f"[搜索优化器] 注册工具失败: {e}")

        if not self.preprocess_provider_id:
            logger.info("[搜索优化器] 未配置预处理模型，使用规则提取模式")
        else:
            logger.info(f"[搜索优化器] 预处理模型: {self.preprocess_provider_id}")

        # 注册插件 Page 后端 API
        try:
            self.context.register_web_api(
                "/astrbot_plugin_search_optimizer/test-model",
                self._api_test_model,
                ["POST"],
                "小模型压缩测试",
            )
            logger.info("[搜索优化器] Page API 注册成功: /test-model")
        except Exception as e:
            logger.warning(f"[搜索优化器] Page API 注册失败: {e}")

    def _load_config(self):
        self.preprocess_provider_id = self.config.get("preprocess_provider_id", "")
        self.optimize_search = self.config.get("optimize_search", False)
        self.cache_days = self.config.get("cache_days", 3)
        self.max_cache_entries = self.config.get("max_cache_entries", 200)
        self.max_summary_chars = self.config.get("max_summary_chars", 1500)
        self.small_model_answer = self.config.get("small_model_answer", False)
        self.cache_similarity_threshold = self.config.get("cache_similarity_threshold", 0.75)

    # ══════════════════════════════════════════════════════════
    # 缓存系统
    # ══════════════════════════════════════════════════════════

    def _load_cache(self) -> dict:
        try:
            if os.path.exists(self._cache_file):
                with open(self._cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"[搜索优化器] 加载缓存失败: {e}")
        return {}

    def _save_cache(self, force=False):
        if not force and not self._cache_dirty:
            return
        try:
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            self._cache_dirty = False
        except Exception as e:
            logger.warning(f"[搜索优化器] 保存缓存失败: {e}")

    def _clean_expired_cache(self):
        now = time.time()
        max_age = self.cache_days * 86400
        expired = [
            k for k, v in self._cache.items()
            if now - v.get("ts", 0) > max_age
        ]
        for k in expired:
            del self._cache[k]
        if expired:
            self._cache_dirty = True
            self._save_cache()
            logger.info(f"[搜索优化器] 清理 {len(expired)} 条过期缓存")

    def _evict_lru(self):
        if len(self._cache) <= self.max_cache_entries:
            return
        sorted_keys = sorted(
            self._cache.keys(),
            key=lambda k: (
                self._cache[k].get("hits", 0),
                self._cache[k].get("ts", 0),
            ),
        )
        to_remove = len(self._cache) - self.max_cache_entries
        for k in sorted_keys[:to_remove]:
            del self._cache[k]
        if to_remove > 0:
            self._cache_dirty = True
            logger.info(f"[搜索优化器] LRU 淘汰 {to_remove} 条缓存")

    def _cache_get(self, query: str) -> Optional[str]:
        key = _normalize_query(query)
        entry = self._cache.get(key)
        if not entry:
            q_chars = set(key.replace(' ', ''))
            for cached_key, cached_entry in self._cache.items():
                c_chars = set(cached_key.replace(' ', ''))
                if not q_chars or not c_chars:
                    continue
                overlap = len(q_chars & c_chars)
                union = len(q_chars | c_chars)
                if union > 0 and overlap / union >= self.cache_similarity_threshold:
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
        self._cache[key] = {
            "content": content,
            "ts": time.time(),
            "hits": 0,
            "chars": len(content),
        }
        self._cache_dirty = True
        self._evict_lru()

    def _cache_clear(self):
        count = len(self._cache)
        self._cache.clear()
        self._cache_dirty = True
        self._save_cache(force=True)
        return count

    # ══════════════════════════════════════════════════════════
    # 钩子：on_agent_done
    # ══════════════════════════════════════════════════════════

    @filter.on_agent_done()
    async def on_agent_done(self, event, run_context, resp):
        try:
            messages = None
            ctx = run_context
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
                # 跳过已被 on_llm_tool_respond 处理过的消息
                if getattr(msg, '_search_opt_processed', False):
                    continue
                content = getattr(msg, 'content', None)
                # 检查内容项是否已被标记
                if isinstance(content, list):
                    if any(getattr(item, '_search_opt_processed', False) for item in content):
                        continue
                tool_name = getattr(msg, 'name', '') or ''
                text = self._extract_text(content)
                if not text or len(text) < 2000:
                    continue
                if '<compressed>' in text:
                    continue

                is_search = (
                    _is_search_tool(tool_name)
                    or _is_json_search_result(text)
                    or self._has_many_urls(text)
                    or len(text) > 5000
                    or any(d in text.lower() for d in _SEARCH_DOMAIN_HINTS)
                )
                if not is_search:
                    continue

                logger.info(f"[搜索优化器] 发现搜索内容: {tool_name} ({len(text)} 字符)")
                source_urls = self._extract_urls(text)
                cleaned = self._strip_noise(text)
                if not cleaned:
                    continue

                # 第一阶段：规则提取
                summary = self._rule_extract(tool_name, {}, cleaned, source_urls)
                mode = '规则'

                # 第二阶段：小模型精简
                if summary and self.preprocess_provider_id:
                    llm_result = await self._llm_preprocess(tool_name, {}, summary, source_urls)
                    if llm_result and len(llm_result) < len(summary):
                        summary = llm_result
                        mode = '规则+LLM'

                if summary and len(summary) < len(text):
                    summary = f"<compressed>\n{summary}"
                    self._replace_content(msg, content, summary)
                    saved = len(text) - len(summary)
                    self._total_chars_saved += saved
                    self._preprocess_count += 1
                    logger.info(
                        f"[搜索优化器] [{mode}] {len(text)}→{len(summary)} "
                        f"(省 {saved}，累计 {self._total_chars_saved})"
                    )
                    if self.optimize_search:
                        self._cache_set(tool_name, summary)

        except Exception as e:
            logger.error(f"[搜索优化器] on_agent_done 失败: {e}")

    # ══════════════════════════════════════════════════════════
    # 钩子：on_llm_request（缓存注入）
    # ══════════════════════════════════════════════════════════

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        if self.optimize_search:
            await self._inject_cache(req)

    def _has_many_urls(self, text: str, min_count: int = 3) -> bool:
        urls = re.findall(r'https?://[^\s<>\]\)\"\']+', text)
        return len(set(urls)) >= min_count

    async def _inject_cache(self, req: ProviderRequest):
        user_msg = self._get_user_message(req)
        if not user_msg or len(user_msg) < 5:
            return
        cached = self._cache_get(user_msg)
        if not cached:
            return
        self._cache_hits += 1
        logger.info(
            f"[搜索优化器] 缓存命中 (累计 {self._cache_hits}): {user_msg[:30]}..."
        )

        if self.small_model_answer and self.preprocess_provider_id:
            answer = await self._small_model_answer(user_msg, cached)
            if answer:
                try:
                    from astrbot.core.agent.message import TextPart
                    inject_text = (
                        f"<cached_answer>\n"
                        f"以下是根据搜索结果生成的回答，直接输出即可：\n\n"
                        f"{answer}\n"
                        f"</cached_answer>"
                    )
                    if hasattr(req, "extra_user_content_parts"):
                        req.extra_user_content_parts.append(
                            TextPart(text=inject_text).mark_as_temp()
                        )
                    logger.info(
                        f"[搜索优化器] 小模型已生成答案 ({len(answer)} 字符)"
                    )
                except Exception as e:
                    logger.warning(f"[搜索优化器] 注入答案失败: {e}")
            return

        try:
            from astrbot.core.agent.message import TextPart
            inject_text = (
                f"<cached_search_results>\n"
                f"以下是之前搜索「{user_msg[:50]}」的预处理结果，"
                f"可直接用于回答，无需再次搜索：\n\n{cached}\n"
                f"</cached_search_results>"
            )
            if hasattr(req, "extra_user_content_parts"):
                req.extra_user_content_parts.append(
                    TextPart(text=inject_text).mark_as_temp()
                )
        except Exception as e:
            logger.warning(f"[搜索优化器] 注入缓存失败: {e}")

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
        """用小模型直接生成回答（不使用人格配置）。"""
        prompt = (
            f"用户问题：{user_msg}\n\n"
            f"以下是相关的搜索结果摘要：\n{cached}\n\n"
            f"请根据以上搜索结果，用简洁的中文回答用户的问题。"
            f"如果搜索结果中没有相关信息，请说明。"
        )
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.preprocess_provider_id,
                prompt=prompt,
                system_prompt="",  # 不使用人格配置
            )
            return resp.completion_text
        except Exception as e:
            logger.error(f"[搜索优化器] 小模型回答失败: {e}")
            return None

    # ══════════════════════════════════════════════════════════
    # 钩子：拦截工具返回结果
    # ══════════════════════════════════════════════════════════

    @filter.on_llm_tool_respond()
    async def on_tool_respond(
        self, event: AstrMessageEvent, tool,
        tool_args: dict | None, tool_result,
    ):
        tool_name = getattr(tool, "name", "")
        # 检查是否为已知搜索工具，或 shell 执行了搜索命令
        is_known_search = _is_search_tool(tool_name)
        is_shell_search = False
        if not is_known_search:
            # 对 shell 工具，先提取输出再判断
            if tool_result and hasattr(tool_result, "content"):
                combined = self._extract_text(tool_result.content)
                is_shell_search = _is_shell_search(tool_name, tool_args, combined)
        if not is_known_search and not is_shell_search:
            return
        try:
            await self._process_tool_result(
                event, tool_name, tool_args, tool_result
            )
            # 标记 tool_result 内容，防止 on_agent_done 重复处理
            if tool_result and hasattr(tool_result, "content"):
                for item in tool_result.content:
                    if hasattr(item, "text") and "<compressed>" in (item.text or ""):
                        setattr(item, "_search_opt_processed", True)
        except Exception as e:
            logger.error(f"[搜索优化器] 处理 {tool_name} 失败: {e}")

    async def _process_tool_result(
        self, event, tool_name, tool_args, tool_result
    ):
        if not tool_result or not hasattr(tool_result, "content"):
            return

        pending = []
        for item in tool_result.content:
            text = getattr(item, "text", None)
            if text and len(text) >= 2000:
                pending.append((item, text))

        if not pending:
            return

        source_urls = self._extract_urls_from_args(tool_args) or (
            self._extract_urls(pending[0][1]) if pending else []
        )

        async def _process_one(item, text):
            original_len = len(text)
            logger.info(f"[搜索优化器] 拦截 {tool_name} ({original_len} 字符)")
            cleaned = self._strip_noise(text)
            if not cleaned:
                return None, None
            # 第一阶段：规则提取
            summary = self._rule_extract(
                tool_name, tool_args, cleaned, source_urls
            )
            # 第二阶段：小模型精简
            if summary and self.preprocess_provider_id:
                llm_result = await self._llm_preprocess(
                    tool_name, tool_args, summary, source_urls
                )
                if llm_result and len(llm_result) < len(summary):
                    summary = llm_result
            return item, (original_len, summary)

        results = await asyncio.gather(
            *[_process_one(item, text) for item, text in pending],
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"[搜索优化器] 并行处理异常: {result}")
                continue
            item, data = result
            if not item or not data:
                continue
            original_len, summary = data
            if summary and len(summary) < original_len:
                item.text = summary
                saved = original_len - len(summary)
                self._total_chars_saved += saved
                self._preprocess_count += 1
                mode = "规则+LLM" if self.preprocess_provider_id else "规则"
                logger.info(
                    f"[搜索优化器] [{mode}] {original_len}→{len(summary)} "
                    f"(省 {saved}，累计 {self._total_chars_saved})"
                )
                if self.optimize_search:
                    cache_key = self._extract_search_query(tool_args)
                    if cache_key:
                        self._cache_set(cache_key, summary)

    def _extract_search_query(self, tool_args: dict | None) -> str:
        if not tool_args:
            return ""
        # 标准工具参数
        for key in ("keywords", "query", "q", "keyword"):
            val = tool_args.get(key)
            if val:
                if isinstance(val, list):
                    return " ".join(str(v) for v in val)
                return str(val)
        # shell 命令中提取搜索关键词
        cmd = tool_args.get("command", "")
        if cmd:
            m = re.search(r'[?&]q=([^&"\s]+)', cmd)
            if m:
                from urllib.parse import unquote_plus
                return unquote_plus(m.group(1))
        return ""

    def _extract_urls_from_args(self, tool_args: dict | None) -> list[str]:
        if not tool_args:
            return []
        urls = tool_args.get("urls", tool_args.get("url", []))
        if isinstance(urls, str):
            urls = [urls]
        if isinstance(urls, list):
            return [str(u) for u in urls if u]
        return []

    # ══════════════════════════════════════════════════════════
    # 统一去噪
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

        # 搜索页面噪声：其他人还搜了、相关搜索、热搜榜等（删除该行及之后所有内容）
        for pat in [
            r'其他人还搜了[：:].*$',
            r'相关搜索[：:].*$',
            r'热搜榜[：:].*$',
            r'猜您关注.*$',
            r'查看更多推荐.*$',
            r'换一换.*$',
        ]:
            text = re.sub(pat, '', text, flags=re.DOTALL)

        # 页脚噪声：关于我们、反馈、隐私、版权等导航行
        footer_pats = [
            r'(?:关于我们|加入我们|关于本站|联系方式)[^\n]*(?:官网|相关信息|我们)[^\n]*',
            r'(?:反馈|隐私管理|违法举报|产品论坛|网站收录|使用帮助|推广合作|站长平台)\s*[|｜]\s*',
            r'(?:Copyright|©|版权).*?\n',
            r'(?:All Rights Reserved|保留所有权利).*?\n',
            r'(?:ICP备|备案号|公安备).*?\n',
            r'(?:关于我们|加入我们|反馈|隐私|举报|论坛|收录|帮助|推广|站长)[^\n]*',
            r'查看更多的*推荐.*',
            r'在搜索里推广您的产品.*',
        ]
        for p in footer_pats:
            text = re.sub(p, '', text, flags=re.IGNORECASE)

        # 来源链接（旧版可能残留）
        text = re.sub(r'来源:\s*\n(?:\s*-\s*https?://[^\n]+\n?)+', '', text)

        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()
        return text if len(text) >= 50 else ""

    # ══════════════════════════════════════════════════════════
    # LLM 预处理（不使用人格配置）
    # ══════════════════════════════════════════════════════════

    async def _llm_preprocess(
        self, tool_name, tool_args, content, source_urls
    ) -> Optional[str]:
        prompt = self._build_prompt(tool_name, tool_args, content)
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=self.preprocess_provider_id,
                prompt=prompt,
                system_prompt="",  # 不使用人格配置，避免小模型输出异常
            )
            result = resp.completion_text
            if not result:
                return None
            return result
        except Exception as e:
            logger.error(f"[搜索优化器] LLM 压缩失败: {e}")
            return None

    def _build_prompt(self, tool_name, tool_args, content):
        p = "以下内容已经过初步清洗，请进一步精简归纳，保留核心信息，输出精炼摘要。"
        parts = [p]
        parts.append(f"\n--- 内容 ---\n{content}")
        parts.append(f"\n--- 输出摘要（≤{self.max_summary_chars} 字符）---")
        return "\n".join(parts)

    # ══════════════════════════════════════════════════════════
    # 规则提取
    # ══════════════════════════════════════════════════════════

    def _rule_extract(self, tool_name, tool_args, content, source_urls=None):
        s = content.strip()
        if re.search(r'===\s*https?://', content):
            result = self._extract_multi_fetch(content)
        elif _is_json_search_result(s):
            result = self._extract_json_search(s)
        else:
            result = self._extract_web_text(s)
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
        lines = text.split('\n')
        filtered = []
        for line in lines:
            s = line.strip()
            if not s:
                filtered.append('')
            elif len(s) > 15 or any(c in s for c in '。，、；：！？'):
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

    def _extract_text(self, content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if hasattr(item, "text"):
                    parts.append(item.text)
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        return ""

    def _replace_content(self, msg, old_content, new_text):
        if isinstance(old_content, str):
            msg.content = new_text
        elif isinstance(old_content, list):
            for item in old_content:
                if hasattr(item, "text"):
                    item.text = new_text
                    break

    # ══════════════════════════════════════════════════════════
    # 指令（需要管理员权限）
    # ══════════════════════════════════════════════════════════

    @filter.command("搜索优化器", require_admin=True)
    async def cmd_status(self, event: AstrMessageEvent):
        """查看运行统计（管理员）"""
        model = "规则提取"
        if self.preprocess_provider_id:
            model = await self._get_provider_display_name(
                self.preprocess_provider_id
            ) or self.preprocess_provider_id

        cache_count = len(self._cache)

        ratio = "-"
        if self._preprocess_count > 0 and self._total_chars_saved > 0:
            avg_saved = self._total_chars_saved / self._preprocess_count
            ratio = f"~{avg_saved / (avg_saved + self.max_summary_chars) * 100:.0f}%"

        tokens_saved = self._total_chars_saved // 1.5 if self._total_chars_saved > 0 else 0

        total_queries = self._cache_hits + self._preprocess_count
        hit_rate = f"{self._cache_hits / total_queries * 100:.0f}%" if total_queries > 0 else "-"

        if self.small_model_answer and self.preprocess_provider_id:
            mode_desc = "小模型直接回答"
        elif self.preprocess_provider_id:
            mode_desc = "规则提取 + LLM 精简"
        else:
            mode_desc = "规则提取（零 LLM）"

        lines = [
            "📊 搜索结果优化器",
            "─" * 24,
            f"模式: {mode_desc}",
            f"模型: {model}",
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

    @filter.command("清除缓存", require_admin=True)
    async def cmd_clear_cache(self, event: AstrMessageEvent):
        """清空搜索优化器缓存（管理员）"""
        count = self._cache_clear()
        yield event.plain_result(f"✅ 已清除 {count} 条缓存")

    @filter.command("优化开启", require_admin=True)
    async def cmd_enable_optimize(self, event: AstrMessageEvent):
        """开启优化搜索（管理员）"""
        self.optimize_search = True
        self.config["optimize_search"] = True
        yield event.plain_result("✅ 优化搜索已开启")

    @filter.command("优化关闭", require_admin=True)
    async def cmd_disable_optimize(self, event: AstrMessageEvent):
        """关闭优化搜索（管理员）"""
        self.optimize_search = False
        self.config["optimize_search"] = False
        yield event.plain_result("✅ 优化搜索已关闭")

    @filter.command("小模型开启", require_admin=True)
    async def cmd_enable_small_model(self, event: AstrMessageEvent):
        """开启小模型直接回答（管理员）"""
        if not self.preprocess_provider_id:
            yield event.plain_result("❌ 未配置预处理模型，请先在 WebUI 配置")
            return
        self.small_model_answer = True
        self.config["small_model_answer"] = True
        yield event.plain_result("✅ 小模型直接回答已开启")

    @filter.command("小模型关闭", require_admin=True)
    async def cmd_disable_small_model(self, event: AstrMessageEvent):
        """关闭小模型直接回答（管理员）"""
        self.small_model_answer = False
        self.config["small_model_answer"] = False
        yield event.plain_result("✅ 小模型直接回答已关闭")

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
    # 插件 Page API
    # ══════════════════════════════════════════════════════════

    async def _api_test_model(self):
        from astrbot.api.web import error_response, json_response, request

        payload = await request.json(default={})
        content = payload.get("content", "").strip()
        if not content:
            return error_response("内容不能为空", status_code=400)

        original_chars = len(content)

        # 第一阶段：规则提取
        source_urls = self._extract_urls(content)
        cleaned = self._strip_noise(content)
        if not cleaned:
            cleaned = content
        rule_result = self._rule_extract("test", {}, cleaned, source_urls)
        if not rule_result:
            rule_result = cleaned

        mode = "规则"
        model_name = ""
        compressed = rule_result

        # 第二阶段：小模型精简
        if self.preprocess_provider_id:
            llm_result = await self._llm_preprocess("test", {}, rule_result, source_urls)
            if llm_result and len(llm_result) < len(rule_result):
                compressed = llm_result
                mode = "规则+LLM"
            try:
                model_name = await self._get_provider_display_name(
                    self.preprocess_provider_id
                ) or self.preprocess_provider_id
            except Exception:
                model_name = self.preprocess_provider_id

        compressed_chars = len(compressed)
        ratio = round((1 - compressed_chars / original_chars) * 100, 1) if original_chars > 0 else 0
        tokens_saved = max(0, round((original_chars - compressed_chars) / 1.5))

        return json_response({
            "compressed": compressed,
            "original_chars": original_chars,
            "compressed_chars": compressed_chars,
            "compression_ratio": ratio,
            "tokens_saved": tokens_saved,
            "mode": mode,
            "model": model_name,
        })

    # ══════════════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════════════

    async def terminate(self):
        self._save_cache(force=True)
        from .tools.bing_search import close_session as close_bing
        from .tools.web_fetch import close_session as close_fetch
        await close_bing()
        await close_fetch()
        if self._preprocess_count > 0 or self._cache_hits > 0:
            logger.info(
                f"[搜索优化器] 停止: 处理 {self._preprocess_count} 次，"
                f"缓存命中 {self._cache_hits} 次，"
                f"节省 {self._total_chars_saved} 字符"
            )
