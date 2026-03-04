import json
import random
from datetime import datetime, timedelta
import requests
import json
from typing import List,  Optional,Tuple

results = [
    {
      "rank": 1,
      "title": "学挖掘机到蓝翔-2025",
      "abstract": "学挖掘机到蓝翔学挖掘机到蓝翔学挖掘机到蓝翔..",
      "url": "https://www.lanxiang.cn/view/55014"
    },
    {
      "rank": 1,
      "title": "晚期胃癌免疫治疗最新指南2024-2025",
      "abstract": "生活质量得到一定改善。 近年来免疫治疗在晚期胃癌中显示出较好的疗效和可接受的安全性。 根据CSCO指南，免疫治疗已被推荐为晚期胃癌的一线或二线治疗方案。 目前临床上常用的方案...",
      "url": "https://www.thepaper.cn/view/55014"
    },
    {
      "rank": 2,
      "title": "晚期胃癌PD-1/PD-L1抑制剂最新指南2024-2025",
      "abstract": "晚期胃癌（IV期）主要治疗手段包括PD-1/PD-L1抑制剂。 生活质量得到一定改善。 近年来P...",
      "url": "https://zhihu.com/view/4880"
    },
    {
      "rank": 3,
      "title": "晚期胃癌帕博利珠单抗最新指南2024-2025",
      "abstract": "晚期胃癌（IV期）主要治疗手段包括帕博利珠单抗。 根据CSCO指南，帕博利珠单抗已被推荐为晚期胃癌的一线或二线治疗方案。 近年来帕博利珠单抗在晚期胃癌中显示出较好的疗效和可接受的安全性。",
      "url": "https://baike.baidu.com/view/59595"
    },
    {
      "rank": 4,
      "title": "姑息治疗在晚期胃癌中的应用与疗效观察",
      "abstract": "近年来姑息治疗在晚期胃癌中显示出较好的疗效和可接受的安全性。 晚期胃癌（IV期）主要治疗手段包括姑息治疗。 根据CSCO指南，姑息治疗已被推荐为晚期胃癌的一线或二线治疗方案。 目前临床上常用的方案...",
      "url": "https://sina.com.cn/view/25787"
    },
    {
      "rank": 5,
      "title": "国内专家共识：晚期胃癌首选HER2靶向药",
      "abstract": "近年来HER2靶向药在晚期胃癌中显示出较好的疗效和可接受的安全性。 根据CSCO指南，HER2靶向药已被推...",
      "url": "https://www.dxy.cn/view/26372"
    }
]




def rerank(
    query: str, 
    documents: List[str],
    model: str = "Qwen3-Reranker-0.6B",
    top_n: Optional[int] = None,
    url: str = "http://localhost:8180/v1/rerank",
    api_key: str = "sk-",
    max_length: int = 512
)->List[Tuple[int, float]]:
    '''
    重排序，返回(原始索引, relevance_score) 的降序列表
    '''
    if not documents:
        return []
    
    documents = [doc[:max_length] for doc in documents]
    
    payload = {
        "model": model,
        "query": query,
        "documents": documents
    }
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        index_with_scores = [
            (item["index"], item.get("relevance_score", 0.0))
            for item in results
        ]

        # 按分数从高到低排序 (已经是降序)
        # index_with_scores.sort(key=lambda x: x[1], reverse=True)
        return index_with_scores

    except Exception as e:
        print(f"rerank failed: {e}")
        return []




if __name__ == "__main__":
    query = "晚期胃癌的治疗"
    documents = [item["title"]+item["abstract"] for item in results]
    ranked_pairs = rerank(query, documents)
    print("rerank 返回的排序对：")
    print(ranked_pairs)
    sorted_results = [results[idx].copy() | {"sim_score": round(score, 4), "rank": i+1}
                    for i, (idx, score) in enumerate(ranked_pairs) if 0 <= idx < len(results)]
    print("排序后的结果：")
    for item in sorted_results:
        print(f"{item['rank']}. {item['title']} ({item['url']})")
        print(f"  相似度: {item['sim_score']}")
        print(f"  摘要: {item['abstract']}\n")