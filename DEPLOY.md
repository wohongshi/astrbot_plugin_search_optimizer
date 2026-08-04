# 部署说明

## 环境要求

- Python >= 3.10
- AstrBot >= 4.16

## 依赖安装

```bash
pip install aiohttp beautifulsoup4 trafilatura DrissionPage
```

## 可选：系统 Chromium

用于抓取 JS 渲染页面（如米游社、部分 Wiki）：

```bash
# Debian/Ubuntu
apt install chromium chromium-driver

# CentOS/RHEL
yum install chromium

# 或使用 Google Chrome
apt install google-chrome-stable
```

不安装 Chromium 也可以正常使用，只是 JS 渲染页面（如米游社）会抓取不到详细内容。

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/wohongshi/astrbot_plugin_search_optimizer.git
cd astrbot_plugin_search_optimizer
pip install -r requirements.txt
```

安装后在 AstrBot WebUI 插件管理页面重载插件即可。

## 配置

在 WebUI 插件管理页面配置，推荐配置：

- **预处理模型**：选择一个低成本模型（如 7B/14B），或留空使用规则提取
- **优化网络搜索**：开启（减少重复搜索）
- **缓存保留天数**：3 天
- **搜索/抓取超时**：30 秒

## 验证

在 AstrBot 中发送 `/搜索优化器` 查看插件状态。
