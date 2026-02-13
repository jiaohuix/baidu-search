# 📡 Baidu Search — 异步百度搜索抓取工具

**`baidu-search`** 是一个用 Python 实现的异步百度搜索库 + 抓取引擎。
它可以在 Python 程序中执行百度搜索并抓取结果页面的正文内容，支持多级 fallback 后端（requests、Crawl4AI、Jina 等），适合构建自定义搜索与抓取流程。

✨ 目标是简洁、易用、适合开发者在自己的项目中直接调用。

---

## 🚀 核心特性

* 📌 **异步接口支持** — 使用 `async/await` 运行，更友好地集成在现代 Python async 应用中
* 🔄 **多后端爬取 fallback** — 支持多种抓取策略自动降级，提高成功率（如浏览器 / API / HTTP 请求等）
* 🧠 **可提取正文** — 支持 readability 提取或简单去标签文本
* 🧪 自带测试覆盖，结构清晰、便于维护

---

## 🧱 安装

你可以使用 `uv` 或 `pip` 安装：

```bash
uv sync
# 或者
pip install -e .
```

📌 如果需要增强爬取能力（如使用 Crawl4AI / Playwright），请按提示安装额外依赖。

---

## 📦 快速开始

示例 1 — 简单搜索并打印结果：

```python
import asyncio
from baidu_search import BaiduSearch

async def main():
    searcher = BaiduSearch()
    results = await searcher.search("你的关键词", num_results=5)
    print(results)

asyncio.run(main())
```

示例 2 — 自定义抓取引擎：

🌐 浏览器抓取能力
```
uv sync --extra crawl4ai
playwright install
```

```python
from baidu_search.engine import CrawlEngine

engine = CrawlEngine(level=1, use_readability=True)
text = await engine.crawl("https://example.com/article/123")
print(text)
```

---

## 🔍 可用后端层级

`CrawlEngine` 支持按层级降级抓取：

| Level | 后端名称       | 说明                |
| ----- | ---------- | ----------------- |
| 0     | `requests` | 纯 HTTP 请求抓取       |
| 1     | `crawl4ai` | API / 智能抓取（如可用）   |
| 2     | `jina`     | 外部抓取服务（需环境配置 Key） |

你可以通过 `level` 参数控制最大使用等级。

---

## 📌 注意事项

🔹 **百度搜索页面结构可能随时改变**，抓取结果会随之失效。
🔹 对于动态加载内容，建议启用浏览器级抓取（Playwright/Crawl4AI）。
🔹 API Key / 环境变量配置请按 **项目代码注释** 说明填写。

---

## 🧪 测试

项目包含测试模块，可运行：

```bash
uv run tests
```

请先确保所有依赖安装并且磁盘空间充足（有时依赖包编译、缓存目录可能较大）。

---

## 📄 贡献 & 协议

欢迎提交 Issue、PR，一起完善功能 💪
遵循 MIT 许可证开源。



