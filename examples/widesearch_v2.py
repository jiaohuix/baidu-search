'''
https://github.com/ByteDance-Seed/WideSearch/tree/main/src/agent
'''
import asyncio
import json
import os
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict

from pydantic import BaseModel, Field

from agno.agent import Agent
from agno.tools import tool
from agno.tools.mcp import MCPTools
from agno.models.openai import OpenAILike

# ============================================================
# 配置
# ============================================================
API_URL = "https://api.siliconflow.cn/v1"
API_KEY = "sk-"           # ← 替换成真的 key
# MODEL_NAME = "deepseek-ai/DeepSeek-V3"  
MODEL_NAME = "Qwen/Qwen3-8B" 

llm = OpenAILike(
    id=MODEL_NAME,
    api_key=API_KEY,
    base_url=API_URL,
    extra_body={"enable_thinking": False},
)

CONCURRENCY = 4           # 根据你的 API 限额和内存调整
WORKSPACE = "./workspace/"
os.makedirs(WORKSPACE, exist_ok=True)

# ============================================================
# 结构化子代理信息（模仿 WideSearch 的 SubAgentInfo）
# ============================================================
@dataclass
class SubAgentResult:
    index: int
    prompt: str
    content: str
    success: bool = True
    error: Optional[str] = None

    def to_dict(self):
        return asdict(self)

class CreateSubAgentsArgs(BaseModel):
    """Pydantic 模型，帮助 LLM 正确解析参数"""
    sub_agents: List[Dict[str, str | int]] = Field(
        ...,
        description="子代理列表，每个包含 'index' (int, 唯一) 和 'prompt' (str)"
    )

# ============================================================
# Prompts (精简版，参考 Youtu/Manus)
# ============================================================
SEARCHER_INSTRUCTIONS_TEMPLATE = """# 角色设定
你是一位联网信息搜索专家，你需要根据用户的问题，通过联网搜索来搜集相关信息，然后根据这些信息来回答用户的问题。

# 任务描述
当你接收到用户的问题后，你需要充分理解用户的需求，利用我提供给你的工具，获取相对应的信息、资料，以解答用户的问题。
以下是你在执行任务过程中需要遵循的原则：
- 充分理解用户需求：你需要全面分析和理解用户的问题，必要时对用户的问题进行拆解，以确保领会到用户问题的主要意图。
- 灵活使用工具：当你充分理解用户需求后，请你使用我提供的工具获取信息；当你认为上次工具获取到的信息不全或者有误，以至于不足以回答用户问题时，请思考还需要搜索什么信息，再次调用工具获取信息，直至信息完备。"""

PLANNER_INSTRUCTIONS = """# 角色设定
你是一位专业、细心的信息收集和整理专家。你能够充分理解用户需求、熟练使用搜索工具，以最高的效率完成用户布置的任务。

# 任务描述
当你接收到用户的问题后，你需要充分理解用户的需求，并思考和规划如何高效快速地完成用户布置的任务。
为了帮助你更好、更快地完成任务，我给你提供了三种工具：
1. 搜索工具：你可以利用搜索引擎进行信息的检索；
2. 网页链接浏览工具：可以打开链接（可以是网页、pdf等）并根据需求描述汇总页面上的所有相关信息。
3. Sub Agent：Sub Agent能够根据你输入的prompt来完成各种类型的任务，Sub Agent自身也可以使用搜索工具或网页链接浏览工具。你可以根据自己的需要，将自己的任务拆分成多个子任务，然后创建一个或多个Agent来帮助你并行完成这些子任务。
"""

# ============================================================
# WideResearch 主类
# ============================================================
class WideResearch:
    def __init__(self, mcp_url: str = "http://127.0.0.1:8080/mcp"):
        self.mcp_url = mcp_url
        self.mcp_tools: Optional[MCPTools] = None
        self.planner: Optional[Agent] = None

    async def initialize(self):
        self.mcp_tools = MCPTools(transport="streamable-http", url=self.mcp_url, timeout_seconds=60)
        await self.mcp_tools.connect()

        @tool(name="create_sub_agents")
        async def create_sub_agents(sub_agents: List[Dict]) -> str:
            """创建agent函数，可以创建一个或多个Agent，每个agent可以根据输入的prompt完成特定的任务。
                        
            Args:
                sub_agents: 创建的agent列表，每个包含 prompt 和 index。eg: [{"prompt":"", "index":1},...]
            """

            if not sub_agents:
                return "错误：sub_agents 列表为空"

            try:
                args = CreateSubAgentsArgs(sub_agents=sub_agents)
            except Exception as e:
                return f"参数解析失败：{str(e)}"

            print(f"🚀 启动 {len(sub_agents)} 个子代理 | 并发上限: {CONCURRENCY}")

            semaphore = asyncio.Semaphore(CONCURRENCY)

            async def run_one_subagent(info: Dict) -> SubAgentResult:
                idx = info.get("index")
                prompt = info.get("prompt")
                if not isinstance(idx, int) or not isinstance(prompt, str):
                    return SubAgentResult(index=idx or -1, prompt=prompt or "", content="", success=False, error="参数格式错误")

                name = f"Sub-{idx:02d}"
                output_path = os.path.join(WORKSPACE, f"{name}.jsonl")

                async with semaphore:
                    try:
                        sub_agent = Agent(
                            name=name,
                            model=llm,
                            tools=[self.mcp_tools],           # 关键：子代理也带搜索能力
                            instructions=SEARCHER_INSTRUCTIONS_TEMPLATE,
                            markdown=True,
                            debug_mode = True,
                            add_datetime_to_context=True,
                            timezone_identifier="Asia/Shanghai",  # 设置时区
                        )

                        response = await sub_agent.arun(prompt)

                        result = SubAgentResult(
                            index=idx,
                            prompt=prompt,
                            content=response.content,
                        )

                        # 可选：同时写文件，便于调试
                        with open(output_path, "a", encoding="utf-8") as f:
                            f.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")

                        return result

                    except Exception as e:
                        err_msg = f"子代理 {idx} 执行失败: {str(e)}"
                        print(err_msg)
                        return SubAgentResult(index=idx, prompt=prompt, content="", success=False, error=err_msg)

            # 并行执行
            tasks = [run_one_subagent(sa) for sa in sub_agents]
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)

            # 统一处理异常
            results = []
            for r in raw_results:
                if isinstance(r, Exception):
                    results.append(SubAgentResult(-1, "", "", False, str(r)))
                else:
                    results.append(r)

            # 排序（按 index）
            results.sort(key=lambda x: x.index)

            # 返回结构化 JSON（模仿 InternalResponse.data）
            output = {
                "status": "success" if all(r.success for r in results) else "partial",
                "sub_agents_count": len(results),
                "results": [r.to_dict() for r in results],
            }

            return json.dumps(output, ensure_ascii=False, indent=2)

        # 创建主规划者
        self.planner = Agent(
            name="WidePlanner",
            model=llm,
            tools=[self.mcp_tools, create_sub_agents],
            instructions=PLANNER_INSTRUCTIONS,
            markdown=True,
            debug_mode = True,
            add_datetime_to_context=True,
            timezone_identifier="Asia/Shanghai",  # 设置时区

        )

    async def run(self, task: str):
        if not self.planner:
            await self.initialize()
        await self.planner.aprint_response(task, stream=True)

    async def close(self):
        if self.mcp_tools:
            await self.mcp_tools.close()


# ============================================================
# 主程序
# ============================================================
async def main():
    query  =  "调研中国A股市场中所有“新能源汽车”概念股（至少100家以上）的最新季度财务数据，包括每家公司的营收、净利润、市值、PE比率、主要产品线和最近一则重大新闻事件。然后，根据这些数据分类汇总：按市值前10名排序输出表格，按行业子类（如电池、电机、整车）计算平均增长率，并分析整体行业趋势。最后输出一个 Markdown 格式的综合报告，包括数据来源引用。"

    research = WideResearch()
    try:
        await research.run(query)
    finally:
        await research.close()

if __name__ == "__main__":
    asyncio.run(main())