# 📡 baidu-search

**异步百度搜索 · 正文提取 · BM25 上下文压缩**

轻量级异步 Python 库，专为 **LLM Agent / RAG** 场景打造。

核心能力一览：

- 🚀 异步百度搜索：标题、链接、摘要快速获取
- 🔄 多后端抓取 + 自动 fallback：requests（默认，轻快） → Crawl4AI（浏览器稳） → Jina（重型备选）
- 🧠 正文提取 & 智能压缩：用 readability 抽取干净正文 + **BM25** 按查询相关性压缩长文本
- 📌 内置缓存：相同查询/页面自动命中，减少重复爬取

适合塞进 Agent 的 search tool、RAG pipeline 或任何需要高效中文搜索的异步应用。
当前版本（dev 分支）：v0.5.0  
还在积极迭代中 ⚙️

---

## 🚀 核心特性

* 📌 **异步接口支持** — 使用 `async/await`，友好集成到现代 Python async 应用中
* 🔄 **多后端爬取 fallback** — 支持多种抓取策略自动降级，提高成功率（requests / Crawl4AI / Jina）
* 🧠 **正文提取 & 文本压缩** — 可用 readability 提取正文，或用 `ContextCompressor` 按查询压缩文本
* 🧪 自带测试覆盖，结构清晰、便于维护

---

## 🧱 安装

```bash
uv sync
# 或者
pip install -e .
```

### 🔧 可选增强环境（浏览器抓取 / Crawl4AI）

如果你想使用 Crawl4AI 或 Playwright 浏览器抓取能力，请先准备环境：

```bash
uv sync --extra crawl4ai
playwright install
```

---

## 📦 快速开始

### 1️⃣ 简单搜索并打印结果

```python
import asyncio
from baidu_search import BaiduSearch

async def main():
    searcher = BaiduSearch()
    results = await searcher.search("强化学习", num_results=5)
    print(results)

asyncio.run(main())
```

### 2️⃣ 自定义抓取引擎

```python
from baidu_search.engine import CrawlEngine

engine = CrawlEngine(level=1, use_readability=True)
text = await engine.crawl("https://example.com/article/123")
print(text)
```

### 3️⃣ 使用 ContextCompressor 压缩长文本

```python
from baidu_search.compressor import ContextCompressor

query = "Agent 强化学习 RL 例子 讲解 简单入门"
text = """强化学习里的 Agent 其实就是那个“决策者”，它通过不断试错来学会最大化累计奖励。昨天我同事又在群里发他家猫的视频，太懒了根本不想动。最经典的入门例子就是 CartPole（小车倒立摆），环境是一个小车，上面竖着一根杆子，Agent 的任务是通过左右推小车，让杆子一直保持竖直不倒。..."""
comp = ContextCompressor(max_chars=500, splitter="simple")
compressed = comp.compress(query, text)
print(f"原文长度: {len(text)}")
print(f"压缩后长度: {len(compressed)}")
print(compressed)
```

✅ 特点：

* 可指定压缩最大长度 `max_chars`
* 支持不同拆分策略（如 `simple`、`jina`）
* 自动保留与查询最相关的内容
* 非相关信息会被过滤掉，提高后续搜索或 QA 的效率

---

## 🔍 可用后端层级

| Level | 后端名称       | 说明                |
| ----- | ---------- | ----------------- |
| 0     | `requests` | 纯 HTTP 请求抓取       |
| 1     | `crawl4ai` | API / 智能抓取（如可用）   |
| 2     | `jina`     | 外部抓取服务（需环境配置 Key） |

---

## 📌 注意事项

* 🔹 百度搜索页面结构可能随时改变，抓取结果可能失效
* 🔹 动态加载内容建议启用浏览器抓取（Playwright / Crawl4AI）
* 🔹 API Key / 环境变量配置请按代码注释说明填写
* 🔹 `ContextCompressor` 适合文本中存在散落不相关内容的场景

---

## 🧪 测试

```bash
uv run tests
```

请确保依赖安装完整，磁盘空间充足（缓存目录可能较大）。

---

## 📄 贡献 & 协议

欢迎提交 Issue、PR 💪
遵循 MIT 许可证开源
