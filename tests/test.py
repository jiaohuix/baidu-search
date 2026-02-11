import asyncio
from baidu_search import BaiduSearch

async def main():
    query = "福冈宣言》指出，缺乏共鸣的医生应被视为："
    query = "我国首个水生实验动物是"
    query = "血管导管相关血流感染率的计算周期是？"
    query = "医用气体管道标识中，负压吸引系统的颜色是？"
    query = "医疗机构制剂配制用水需检测的指标是？"
    query = "医院消毒供应中心生物监测培养的温度和时间是？"
    searcher = BaiduSearch()
    results = await searcher.search(query, num_results=10)
    print(results)

asyncio.run(main())