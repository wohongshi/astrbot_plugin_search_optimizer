# 🔍 AstrBot 搜索结果优化器 — 部署方案

> 硬件：骁龙 7+ Gen 2 / ARM Linux  
> 模型：MiniCPM5-1B (Q4_K_M, 688MB)  
> 插件：astrbot_plugin_search_optimizer v5

---

## 一、架构概览

```
用户提问
   ↓
AstrBot 主力模型（如 GPT-4 / DeepSeek / Qwen-72B）
   ↓ 决定搜索
web_search / web_fetch 工具执行
   ↓ 返回大量内容（5000-10000 字符/页）
搜索结果优化器 拦截
   ↓
本地 MiniCPM5-1B（688MB）摘要压缩 → 缓存
   ↓ 省掉 70-85% Token
主力模型只看精炼摘要 → 输出回答
```

**效果：** 每次搜索省 5000-8000 字符的主力模型 Token，本地小模型免费处理。

---

## 二、环境准备

### 2.1 系统要求

| 项目 | 要求 |
|------|------|
| 系统 | ARM64 Linux（Ubuntu 22.04+ / Debian 12+） |
| 内存 | ≥ 3GB（MiniCPM5 占 ~700MB + AstrBot + 系统） |
| 存储 | ≥ 2GB 可用空间 |
| Python | 3.10+ |
| 网络 | 需要联网下载模型和依赖 |

### 2.2 安装 Ollama

```bash
# 一键安装（自动检测 ARM 架构）
curl -fsSL https://ollama.com/install.sh | sh

# 验证安装
ollama --version
# 应输出类似: ollama version 0.6.x

# 启动 Ollama 服务
ollama serve &
# 或者用 systemd（安装脚本通常自动配置）
# sudo systemctl start ollama
```

### 2.3 拉取 MiniCPM5 模型

```bash
# 拉取 Q4_K_M 量化版（688MB，骁龙 7+ Gen 2 推荐）
ollama pull openbmb/minicpm5

# 验证模型
ollama list
# 应显示: openbmb/minicpm5:latest  688MB

# 测试运行
ollama run openbmb/minicpm5 "用一句话总结：人工智能在2025年取得了突破性进展，多家公司发布了新一代模型。"
```

**预期输出（约 1-3 秒）：**
```
2025年AI取得重大突破，多家公司发布新一代模型。
```

### 2.4 安装 AstrBot

如果还没装 AstrBot：

```bash
# 克隆 AstrBot
git clone https://github.com/AstrBotDevs/AstrBot.git
cd AstrBot

# 安装依赖
pip install -r requirements.txt

# 启动（首次会生成配置文件）
python main.py
```

启动后访问 WebUI（默认 http://localhost:6185）。

---

## 三、配置 AstrBot 连接 MiniCPM5

### 3.1 添加 Ollama Provider

1. 打开 AstrBot WebUI → **配置** → **模型服务**
2. 点击 **添加 Provider**
3. 选择 **Ollama**
4. 填写：
   - **名称：** `minicpm5`（随意）
   - **API 地址：** `http://localhost:11434`
   - **模型：** `openbmb/minicpm5`
5. 点击 **保存**

### 3.2 验证连接

在 AstrBot 对话中发送：
```
/test 你好
```
如果能正常回复，说明 Ollama Provider 配置成功。

---

## 四、安装搜索结果优化器插件

### 4.1 方式一：WebUI 安装

1. 打开 WebUI → **插件** → **安装插件**
2. 输入仓库地址：`https://github.com/wohongshi/astrbot_plugin_search_optimizer`
3. 点击安装
4. 重载插件

### 4.2 方式二：手动安装

```bash
cd AstrBot/data/plugins/

# 克隆插件
git clone https://github.com/wohongshi/astrbot_plugin_search_optimizer.git

# 重启 AstrBot 或在 WebUI 中重载插件
```

### 4.3 方式三：zip 包安装

1. 下载 `astrbot_plugin_search_optimizer.zip`
2. 在 WebUI → **插件** → **安装插件** → 上传 zip 包
3. 重载插件

---

## 五、配置插件

打开 WebUI → **插件** → **搜索结果优化器** → **配置**

### 5.1 推荐配置

| 配置项 | 推荐值 | 说明 |
|--------|--------|------|
| **预处理模型** | `minicpm5` | 选择刚才添加的 Ollama Provider |
| **优化网络搜索** | ✅ 开启 | 命中缓存时跳过搜索 |
| **缓存保留天数** | 3 | 时效性搜索 3 天过期 |
| **缓存条目上限** | 200 | LRU 淘汰，防止内存膨胀 |
| **摘要最大字符数** | 1500 | 控制压缩后长度 |

### 5.2 配置说明

- **预处理模型**：选择 `minicpm5`（Ollama），用本地小模型做摘要
- **优化网络搜索**：开启后，相同/相似问题的搜索结果会被缓存，下次直接复用
- **缓存保留天数**：含"今天"、"最新"等时间词的搜索，每天自动刷新缓存
- **缓存条目上限**：超过 200 条时自动淘汰最久没用的

---

## 六、安装搜索插件（如果还没有）

搜索结果优化器需要配合搜索插件使用。推荐安装：

### 6.1 Bing Web Search 插件

```bash
cd AstrBot/data/plugins/
git clone https://github.com/AmedNet/astrbot_plugin_web_tools_ar.git
```

安装依赖：
```bash
cd astrbot_plugin_web_tools_ar
pip install DrissionPage trafilatura
```

确保已安装 Microsoft Edge 浏览器（ARM Linux 上可用 Chromium 替代）。

### 6.2 或使用 AstrBot 内置搜索

在 WebUI → **配置** → **网页搜索** 中配置 Tavily / Brave / BoCha 等搜索源。

---

## 七、验证部署

### 7.1 检查插件状态

在 AstrBot 对话中发送：
```
/搜索优化器
```

应显示：
```
📊 搜索结果优化器
  模式: LLM 摘要
  模型: openbmb/minicpm5
  优化搜索: ✅
  缓存: 0/200 条 (3天)
  摘要上限: 1500 字符
  缓存命中: 0 次
  累计处理: 0 次
  累计节省: 0 字符
```

### 7.2 测试搜索优化

发送一个需要搜索的问题：
```
帮我搜索一下今天AI领域有什么新闻
```

观察日志（WebUI → 日志）：
```
[搜索优化器] 拦截 web_search (3245 字符)
[搜索优化器] [LLM] 3245→892 字符 (省 2353，累计 2353)
```

### 7.3 测试缓存命中

再次发送类似问题：
```
最近AI有什么进展
```

应看到缓存命中日志：
```
[搜索优化器] 缓存命中 (累计 1): 最近ai有什么进展...
```

这次不会触发搜索，直接用缓存回答，速度更快。

---

## 八、性能预期

### 骁龙 7+ Gen 2 上的表现

| 指标 | 预期值 |
|------|--------|
| MiniCPM5 推理速度 | ~20-40 tok/s |
| 单次摘要耗时 | 1-3 秒 |
| 内存占用 | ~700MB（MiniCPM5）+ ~200MB（AstrBot） |
| 每次搜索节省 Token | 3000-8000 字符 |
| 缓存命中延迟 | <10ms（纯内存查找） |

### Token 节省估算

假设每天搜索 20 次，每次节省 5000 字符：
- 日节省：100,000 字符 ≈ 50,000 Token
- 月节省：1,500,000 Token ≈ 750,000 Token

按 GPT-4o 价格（$2.5/1M Token）：
- 月省 ~$1.875
- 按 DeepSeek 价格更省

---

## 九、常见问题

### Q: Ollama 启动失败？

```bash
# 检查端口占用
lsof -i :11434

# 手动启动
ollama serve

# 检查日志
journalctl -u ollama -f
```

### Q: 模型推理很慢？

```bash
# 检查是否在用 CPU（ARM 上正常）
ollama ps

# 如果内存不足，可以用更小的模型
ollama pull openbmb/minicpm  # MiniCPM 0.5B，更轻量
```

### Q: 插件没有拦截到搜索结果？

1. 确认搜索插件已安装且正常工作
2. 确认工具名包含 `web_search`、`web_fetch` 等关键词
3. 检查日志是否有 `[搜索优化器] 拦截` 开头的输出
4. 内容低于 2000 字符不会触发（太短没必要压缩）

### Q: 缓存没命中？

- 含时间词（今天、最新等）的问题，每天缓存自动隔离
- 两次问题的关键词差异太大时不会命中
- 可以 `/清除缓存` 后重试

---

## 十、目录结构参考

```
AstrBot/
├── data/
│   ├── plugins/
│   │   ├── astrbot_plugin_search_optimizer/   ← 优化器插件
│   │   │   ├── main.py
│   │   │   ├── metadata.yaml
│   │   │   ├── _conf_schema.json
│   │   │   └── README.md
│   │   └── astrbot_plugin_web_tools_ar/       ← 搜索插件
│   │       ├── main.py
│   │       └── tools/
│   └── plugin_data/
│       └── search_optimizer/
│           └── cache.json                     ← 缓存文件
└── main.py
```
