import httpx
import asyncio
from typing import List, Tuple, Optional

class ContextEngine:
    def __init__(self, base_url: str = "http://localhost:8180", api_key: str = "sk-"):
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        self.highlight_prompt = "【最相关核心事实】\n• {core_sents}\n\n【参考上下文】\n{compressed_ctx}"

    async def _post(self, endpoint: str, payload: dict):
        """通用异步 POST 请求处理"""
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await client.post(f"{self.base_url}{endpoint}", json=payload, headers=self.headers)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                print(f"Request to {endpoint} failed: {e}")
                return None

    async def rerank(
        self, 
        query: str, 
        documents: List[str], 
        model: str = "Qwen3-Reranker-0.6B", 
        max_length: int = 512
    ) -> List[Tuple[int, float]]:
        """异步重排序"""
        if not documents:
            return []
            
        payload = {
            "model": model,
            "query": query,
            "documents": [doc[:max_length] for doc in documents]
        }
        
        data = await self._post("/v1/rerank", payload)
        if not data:
            return []
            
        results = data.get("results", [])
        return [(item["index"], item.get("relevance_score", 0.0)) for item in results]

    async def compress(
        self,
        query: str,
        context: str,
        repeate_core_ctx: bool = True,
        window_size: int = 1,
        threshold: float = 0.5,
        model: str = "semantic-highlight-bilingual-v1"
    ) -> str:
        """异步高亮与上下文压缩"""
        payload = {
            "model": model,
            "question": query,
            "context": context,
            "threshold": threshold,
            "language": "zh",
            "window_size": window_size,
            "return_sentence_metrics": True
        }
        
        data = await self._post("/v1/highlight", payload)
        if not data:
            return context # 失败时返回原文
            
        highlighted = data.get("highlighted_sentences", [])
        compressed_ctx = data.get("compressed_context", "")
        
        if not repeate_core_ctx:
            return compressed_ctx
            
        core_sents = '\n• '.join(highlighted) if highlighted else '无'
        formatted_ctx = self.highlight_prompt.format(
            core_sents=core_sents, 
            compressed_ctx=compressed_ctx
        )
        return formatted_ctx

# --- 使用示例 ---
async def main():
    engine = ContextEngine()
    
    # 1. 测试 Rerank
    docs = ["这是一条测试文档", "苹果是一种水果", "人工智能改变世界"]
    rank_results = await engine.rerank("什么是苹果？", docs)
    print("Rerank Results:", rank_results)
    
    # 2. 测试 Highlight
    context_text = "苹果含有丰富的维生素。它是落叶乔木的果实。研究表明经常吃苹果对身体好。"
    final_ctx = await engine.compress("苹果的营养价值", context_text)
    print("\nFormatted Context:\n", final_ctx)

if __name__ == "__main__":
    asyncio.run(main())