# baidu-search

百度搜索爬虫工具，异步抓取百度搜索结果。

## 安装

```bash
uv sync
```

## 使用

```python
import asyncio
from baidu_search import BaiduSearch

async def main():
    searcher = BaiduSearch()
    results = await searcher.search("你的关键词", num_results=5)
    print(results)

asyncio.run(main())
```


🌐 浏览器抓取能力
```
uv sync --extra crawl4ai
playwright install
```