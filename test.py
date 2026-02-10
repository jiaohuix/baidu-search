import asyncio
from baidu_search import BaiduSearch

async def main():
    searcher = BaiduSearch()
    results = await searcher.search("强化学习", num_results=5)
    print(results)

asyncio.run(main())