# web-search-helper

内置网页搜索和抓取工具，自动压缩搜索结果 + 缓存加速，减少 Token 消耗。

## 工具

本插件提供以下 LLM 工具（FunctionTool），可被 Agent 自动调用：

### web_search

搜索互联网获取实时信息，自动抓取详情页内容。搜索结果不相关时自动重试。

**参数：**
- `keywords`（必填）：搜索关键词列表，如 `["AstrBot 插件开发"]`

**特性：**
- Bing 搜索 + 详情页自动抓取
- 搜索结果相关性检测，不相关时自动去虚词重搜
- 结果自动压缩，减少 Token 消耗
- 支持缓存加速

### web_fetch

抓取指定 URL 的网页内容并返回文本。支持 JS 渲染页面（自动使用系统 Chromium）。

**参数：**
- `urls`（必填）：要抓取的网页 URL 列表，如 `["https://example.com"]`

**特性：**
- aiohttp 快速抓取 + trafilatura 智能提取正文
- DrissionPage 系统 Chromium 兜底（JS 渲染页面）
- 内网地址安全校验

## 配置说明

在插件配置中可调整以下选项：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `preprocess_provider_id` | 预处理模型 ID（留空使用纯规则提取） | 空 |
| `optimize_search` | 开启缓存加速（命中缓存时跳过搜索） | false |
| `cache_days` | 缓存保留天数 | 3 |
| `max_cache_entries` | 缓存条目上限 | 200 |
| `max_summary_chars` | 摘要最大字符数 | 1500 |
| `small_model_answer` | 小模型直接回答（省主力模型 Token） | false |
| `search_timeout` | 搜索/抓取超时秒数 | 30 |

## 使用方式

插件安装后自动注册 `web_search` 和 `web_fetch` 工具，Agent 会根据用户问题自动调用。无需手动配置。

管理员指令：
- `搜索优化器` — 查看运行统计
- `清除缓存` — 清空缓存
- `优化开启/关闭` — 开关缓存加速
- `小模型开启/关闭` — 开关小模型直接回答
