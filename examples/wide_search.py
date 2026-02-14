import asyncio
import json
import pathlib
from typing import Optional

from agno.agent import Agent
from agno.tools import tool
from agno.tools.mcp import MCPTools
from agno.models.openai import OpenAILike

# ============================================================
# 配置与模型初始化
# ============================================================
API_URL = "https://api.siliconflow.cn/v1"
API_KEY = "sk-xuytewnxqbcczitcswbjkmcvfvpcgfiropkjrnjyhjliyaii"
MODEL_NAME = "Qwen/Qwen3-8B"
MODEL_NAME = "deepseek-ai/DeepSeek-V3.2"
llm = OpenAILike(id=MODEL_NAME, api_key=API_KEY, base_url=API_URL,
                 extra_body={"enable_thinking": False})

CONCURRENCY = 20

# ============================================================
# Prompts (精简版，参考 Youtu/Manus)
# ============================================================
PLANNER_INSTRUCTIONS = """\
You are a senior researcher. You excel at "wide research" - collecting lots of structured info from the web.

Workflow:
1. **Quick Explore**: Use search to get an overview (e.g., find key entities, main sections, list of items).
2. **Decompose & Parallel**: Break the task into 5+ parallel subtasks, then use `search_wide`.
   - For multi-item tasks: one subtask per item (e.g., "Research paper A", "Research paper B")
   - For single-item deep analysis: one subtask per dimension (e.g., "Find innovation 1 details", "Find code implementation", "Find training data method")
3. **Synthesize**: Combine all results into a detailed Markdown report (1000+ words).

IMPORTANT - You MUST use search_wide for:
- Collecting info on 5+ similar items
- Deep analysis with 5+ different dimensions/aspects
- Any task that can be parallelized

search_wide parameters:
- task: main task description
- subtasks: list of specific queries, e.g. ["查找创新点1的细节", "查找代码实现", "查找训练数据方法"]
- output_schema: simple string like "topic:str, details:str, source_url:str"
- output_fn: filename like "results.jsonl"

Rules:
- ALWAYS try to decompose into parallel subtasks
- One tool call per step, summarize before next step
- Prefer search_wide over sequential search calls
"""

SEARCHER_INSTRUCTIONS_TEMPLATE = """\
You are a research assistant. Your task: {subtask}

Use search and fetch_content tools to find the information.
Output ONLY a JSON object with these fields: {schema}
No extra text, no markdown blocks - just the JSON.
"""

# ============================================================
# WideSearch Orchestrator
# ============================================================
class WideResearch:
    def __init__(self, mcp_url: str = "http://127.0.0.1:8080/mcp"):
        self.mcp_url = mcp_url
        self.mcp_tools = MCPTools(transport="streamable-http", url=mcp_url)
        self.planner: Optional[Agent] = None

    async def initialize(self):
        """初始化连接，避免并发中的连接竞争"""
        await self.mcp_tools.connect()
        
        # 这里的 search_wide 我们动态绑定，以便它能访问 self.mcp_tools
        @tool
        async def search_wide(task: str, subtasks: list, output_schema, output_fn: str) -> str:
            """Perform massive parallel research for homogeneous subtasks.

            Args:
                task: The main research task description
                subtasks: List of subtask strings to execute in parallel
                output_schema: Simple string describing output fields, e.g. "title:str, authors:list[str], summary:str"
                output_fn: Output filename to save results (jsonl format)
            """
            # 兼容 dict 和 str
            if isinstance(output_schema, dict):
                output_schema = json.dumps(output_schema, ensure_ascii=False)

            print(f"🚀 [WideSearch] Task: {task}")
            print(f"🚀 [WideSearch] Launching {len(subtasks)} concurrent agents...")
            print(f"📋 Output schema: {output_schema}")

            semaphore = asyncio.Semaphore(CONCURRENCY)

            async def run_subtask(idx: int, subtask: str) -> str:
                async with semaphore:
                    try:
                        searcher = Agent(
                            name=f"Searcher-{idx}",
                            model=llm,
                            tools=[self.mcp_tools],
                            instructions=SEARCHER_INSTRUCTIONS_TEMPLATE.format(
                                subtask=subtask,
                                schema=output_schema
                            ),
                            markdown=False,
                        )
                        response = await searcher.arun(subtask)
                        return response.content
                    except Exception as e:
                        return json.dumps({"error": f"Task {idx} failed: {str(e)}"})

            results = await asyncio.gather(*[run_subtask(i, st) for i, st in enumerate(subtasks)])

            # 存储结果
            p = pathlib.Path(output_fn)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                for r in results:
                    f.write(str(r).strip() + "\n")

            return f"Successfully processed {len(subtasks)} items. Results saved to {output_fn}."

        self.planner = Agent(
            name="PlannerAgent",
            model=llm,
            tools=[self.mcp_tools, search_wide], # Planner 拥有两种工具
            instructions=PLANNER_INSTRUCTIONS,
            markdown=True,
            # show_tool_calls=True
        )

    async def run(self, task: str):
        if not self.planner:
            await self.initialize()
        await self.planner.aprint_response(task, stream=True)

# ============================================================
# Main
# ============================================================
async def main():
    research_sys = WideResearch()
    # 建议的任务：先让它查有哪些论文，再并行提取
    query = "Find the award-winning papers from ACL 2024. For each paper, get title, authors, and a 1-sentence summary. Output a table."
    query = "深度调研这篇论文的具体创新点是什么，如何理解，有没有代码，结合代码给我讲解每个创新点，并且告诉我新的任务如何造数据和训练，举例子。STAR: Similarity-guided Teacher-Assisted Refinement for Super-Tiny Function Calling Models"
    
    try:
        await research_sys.run(query)
    finally:
        # 优雅关闭连接（非常重要，防止报错）
        if research_sys.mcp_tools:
            # 某些版本的 MCP 库可能需要显式关闭 session
            await research_sys.mcp_tools.close()

if __name__ == "__main__":
    asyncio.run(main())