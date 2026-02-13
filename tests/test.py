import asyncio
from baidu_search import BaiduSearch

async def main():
    query = "强化学习"
    searcher = BaiduSearch()
    results = await searcher.search(query, num_results=10)
    print(results)

asyncio.run(main())