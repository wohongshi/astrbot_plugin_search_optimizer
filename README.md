# 🔍 搜索结果优化器

AstrBot 插件 — 内置网页搜索和抓取工具 + 压缩搜索结果 + 缓存加速，减少 Token 消耗。

> 自包含 `web_search` 和 `web_fetch` 工具，无需额外安装其他搜索插件。

## ✨ 功能特性

### 1. 内置网页搜索工具

- **Bing 搜索**（异步 aiohttp + BeautifulSoup）
  - 多关键词并行搜索，结果自动合并去重
  - 搜索结果自动抓取前 N 个网页的详细内容
  - 搜索结果相关性检测，不相关时自动去虚词重搜
  - 5 分钟短期缓存，相同关键词直接返回

- **网页内容抓取**（异步 aiohttp + trafilatura）
  - 静态页面：aiohttp 快速抓取，trafilatura 智能提取正文
  - JS 渲染页面：自动降级 DrissionPage + 系统 Chromium 渲染
  - 多 URL 并行抓取
  - 内网地址、非法协议安全校验

### 2. 搜索结果预处理

拦截搜索/抓取工具返回结果，压缩后再交给主力模型：

- **有预处理模型** → 用低成本 LLM 摘要压缩（压缩率 ~85%）
- **无预处理模型** → 规则提取（去噪、去重、截断，零 LLM 调用，压缩率 ~70%）

### 3. 优化网络搜索（可选）

命中缓存时跳过搜索，直接注入上下文：

```
用户提问 → 检查缓存 → 命中 → 跳过搜索，直接回答
          → 未命中 → 搜索 → 预处理 → 缓存 → 回答
```

### 4. LRU 缓存淘汰

缓存超过上限时自动淘汰命中次数最少 + 最久未用的条目，防止内存膨胀。

### 5. 搜索结果相关性检测

当搜索结果与查询不相关时（如境外服务器搜中文人名被拆字），自动：
- 去掉「是谁/什么/怎么」等虚词
- 重新组合关键词重搜
- 直到找到相关结果或所有组合用尽

## 📦 安装

### 依赖

```bash
pip install aiohttp beautifulsoup4 trafilatura DrissionPage
```

### 系统要求

- Python >= 3.10
- AstrBot >= 4.16
- （可选）系统 Chromium — 用于抓取 JS 渲染页面

```bash
# Debian/Ubuntu
apt install chromium chromium-driver

# 或使用 Google Chrome
apt install google-chrome-stable
```

### 安装方式

**方式一：WebUI 插件市场安装**

在 AstrBot WebUI 的插件管理页面搜索 `astrbot_plugin_search_optimizer` 安装。

**方式二：手动安装**

```bash
cd AstrBot/data/plugins
git clone https://github.com/wohongshi/astrbot_plugin_search_optimizer.git
pip install -r astrbot_plugin_search_optimizer/requirements.txt
```

## ⚙️ 配置

在 AstrBot WebUI → 插件管理 → 搜索结果优化器 中配置：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| 预处理模型 | 低成本 LLM，用于摘要压缩。留空使用规则提取 | 空 |
| 优化网络搜索 | 命中缓存时跳过搜索，直接注入上下文 | 关 |
| 小模型直接回答 | 缓存命中时用预处理模型直接回答，主力模型不参与 | 关 |
| 缓存保留天数 | 过期自动清理 | 3 天 |
| 缓存条目上限 | 超限时 LRU 淘汰 | 200 条 |
| 摘要最大字符数 | 控制压缩后长度 | 1500 字符 |
| 搜索/抓取超时 | 单次请求超时时间 | 30 秒 |

### 预处理模型选择建议

| 方案 | 说明 | 适用场景 |
|------|------|----------|
| 留空（规则提取） | 零 LLM 调用，纯规则去噪去重 | 节省成本，压缩率 ~70% |
| 小参数量模型 | 如 7B/14B 等低成本模型 | 追高压缩率 ~85%，Token 敏感 |

## 🎮 使用

### 自动工作

插件加载后自动生效：

1. LLM 调用 `web_search` 搜索时，插件自动拦截结果并压缩
2. LLM 调用 `web_fetch` 抓取网页时，插件自动拦截结果并压缩
3. 开启「优化网络搜索」后，相似查询直接注入缓存

### 指令

| 指令 | 说明 |
|------|------|
| `/搜索优化器` | 查看运行统计（处理次数、缓存命中、节省 Token 等） |
| `/清除缓存` | 清空全部搜索缓存 |

### 工具

插件注册两个 LLM 工具供 Agent 调用：

- **`web_search`** — 搜索互联网，自动抓取详情页
  - 参数：`keywords`（关键词列表）
  - 支持多关键词并行搜索
  - 自动检测结果相关性，不相关时重搜

- **`web_fetch`** — 抓取网页内容
  - 参数：`urls`（URL 列表）
  - 支持多 URL 并行抓取
  - JS 渲染页面自动降级 Chromium

## 📊 工作原理

```
用户提问
  ↓
LLM 判断需要搜索 → 调用 web_search
  ↓
插件拦截搜索结果
  ├─ 检测相关性 → 不相关 → 去虚词重搜
  ├─ 自动抓取前 3 个结果的详情页
  └─ 返回 摘要 + 详情
  ↓
搜索优化器压缩结果
  ├─ 有预处理模型 → LLM 摘要（~85% 压缩率）
  └─ 无预处理模型 → 规则提取（~70% 压缩率）
  ↓
压缩后内容交给主力模型
  ↓
主力模型基于精炼内容回答用户
```

## 🔧 技术细节

### 搜索引擎

- 使用 Bing 搜索（`cn.bing.com`），支持中国地区优化
- User-Agent 池随机轮换，降低反爬风险
- 连接池复用（aiohttp ClientSession），减少握手开销

### 内容提取

- **trafilatura**：智能网页正文提取，自动去除广告、导航、页脚等噪声
- **BeautifulSoup**：trafilatura 失败时的降级方案
- **DrissionPage**：JS 渲染页面的终极兜底，使用系统 Chromium

### 缓存策略

- 搜索结果 5 分钟短期缓存（内存）
- 预处理结果持久化缓存（文件，可配置天数）
- LRU 淘汰：命中次数最少 + 最久未用的条目优先淘汰
- 时间敏感词检测：包含「今天/最新/最近」等词的查询自动加入日期标记

### 安全性

- URL 校验：拒绝内网地址、非法协议
- 错误处理：工具调用失败不崩溃，自动降级
- 缓存隔离：每个插件实例独立缓存目录

## 📝 更新日志

### v6.5.0
- 搜索结果相关性检测 + 自动去虚词重搜
- 修复境外服务器搜中文人名被拆字的问题

### v6.4.0
- DrissionPage 使用系统 Chromium 替代 Playwright

### v6.3.0
- 移除 Playwright 依赖，纯 aiohttp + DrissionPage 方案

### v6.2.0
- 搜索后自动抓取前 N 个结果的详情页
- DrissionPage 无头浏览器兜底

### v6.1.0
- aiohttp 连接池复用
- 搜索结果短期缓存
- 多关键词并行搜索

### v6.0.0
- 自包含 web_search + web_fetch 工具
- 移除对 astrbot_plugin_web_tools_ar 的依赖
- 修复 FunctionTool pydantic dataclass 注册错误

## 📄 License

MIT

## 🔗 链接

- [GitHub](https://github.com/wohongshi/astrbot_plugin_search_optimizer)
- [AstrBot 官方文档](https://docs.astrbot.app)
- [插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)
