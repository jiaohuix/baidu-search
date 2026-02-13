"""
Wide Research implementation using Agno framework.
Inspired by https://manus.im/blog/introducing-wide-research
https://github.com/TencentCloudADP/youtu-agent/blob/main/examples/wide_research/main.py
"""

import asyncio
import json
import pathlib
import traceback
from typing import List, Optional

from pydantic import BaseModel, Field

from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.tools import tool
from agno.tools.mcp import MCPTools
from agno.models.openai import OpenAILike
# from agno.tools.duckduckgo import DuckDuckGoTools

API_URL = "https://api.siliconflow.cn/v1"
API_KEY = "sk-"
MODEL_NAME="Qwen/Qwen3-8B"

llm = OpenAILike(id=MODEL_NAME, api_key=API_KEY, base_url=API_URL, 
                extra_body = {"enable_thinking": False})



CONCURRENCY = 20

# ============================================================
# Prompts
# ============================================================
PLANNER_INSTRUCTIONS = """\
You are a research planner. Given a user's research task, you should:
1. Break it down into homogeneous subtasks that can be researched independently.
2. Define a JSON Schema for the output of each subtask (all subtasks share the same schema).
3. Call the `search_wide` tool with the task, subtasks, output_schema, and output_fn.
4. After getting results, synthesize them into a final answer (e.g. markdown table).

Use the search_wide tool when there are >= 5 homogeneous subtasks.
For simpler tasks, use the web_search tool directly.
"""

SEARCHER_INSTRUCTIONS_TEMPLATE = """\
You are a research agent. Search the web to find information for the given subtask.
Return your findings as a JSON object strictly matching this schema:
{schema}

Return ONLY the JSON object, no extra text.
"""


# ============================================================
# The search_wide tool — Planner Agent calls this
# ============================================================
@tool
async def search_wide(
    task: str,
    subtasks: list,
    output_schema: dict,
    output_fn: str,
) -> str:
    """Perform structured wide research. Given a task with several search subtasks,
    this tool will perform research simultaneously using concurrent searcher agents.

    Use this tool when the root task has >= 5 subtasks and the subtasks are
    homogeneous (have the same output schema).
    The output will be saved to a JSONL file.

    Args:
        task: The root task to perform research on.
        subtasks: Homogeneous subtasks contained in the root task.
        output_schema: The desired output format of each subtask as valid JSON Schema.
        output_fn: The file name to save the output in JSONL format, e.g. output.jsonl
    """
    mcp_tools = MCPTools(
        transport="streamable-http",
        url="http://127.0.0.1:8080/mcp"
    )
    await mcp_tools.connect()
    print(f"[WideSearch] Processing {len(subtasks)} subtasks for: {task}")
    print(f"[WideSearch] Output schema: {json.dumps(output_schema, ensure_ascii=False)}")

    # Build a searcher agent (one instance per subtask via closure)
    searcher_instructions = SEARCHER_INSTRUCTIONS_TEMPLATE.format(
        schema=json.dumps(output_schema, ensure_ascii=False)
    )

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def run_subtask(idx: int, subtask: str) -> str:
        async with semaphore:
            try:
                searcher = Agent(
                    name=f"Searcher-{idx}",
                    model=llm,
                    # tools=[DuckDuckGoTools()],
                    tools=[mcp_tools],
                    instructions=searcher_instructions,
                    markdown=False,
                )
                response = await searcher.arun(subtask)
                content = response.content
                print(f"[WideSearch] Subtask {idx} done: {subtask[:60]}...")
                return content
            except Exception as e:
                traceback.print_exc()
                return json.dumps({"error": str(e)})

    # Run all subtasks concurrently
    results = await asyncio.gather(
        *[run_subtask(i, st) for i, st in enumerate(subtasks)]
    )

    # Save to JSONL
    output_path = pathlib.Path(output_fn)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for result in results:
            f.write(str(result) + "\n")

    with open(output_path, "r", encoding="utf-8") as f:
        content = f.read()

    return f"Results saved to {output_path}:\n{content}"


# ============================================================
# WideResearch orchestrator
# ============================================================
class WideResearch:
    def __init__(self):
        self.planner = Agent(
            name="PlannerAgent",
            model=llm,
            tools=[search_wide],
            # tools=[search_wide, DuckDuckGoTools()],
            instructions=PLANNER_INSTRUCTIONS,
            markdown=True,
        )

    async def run(self, task: str) -> str:
        response = await self.planner.arun(task)
        return response.content

    async def run_streamed(self, task: str) -> str:
        await self.planner.aprint_response(task, stream=True)
        output = self.planner.get_last_run_output()
        return output.content if output else ""


# ============================================================
# Main
# ============================================================
TASK = (
    "Find the outstanding papers of ACL 2025, extract their title, "
    "author list, keywords, abstract, url. Return a markdown table"
)


async def main():
    wide_research = WideResearch()
    query = input("What would you like to research? ").strip() or TASK
    print(f"Processing task: {query}")
    result = await wide_research.run_streamed(query)
    print(f"{'=' * 80}\n{result}\n{'=' * 80}")


if __name__ == "__main__":
    asyncio.run(main())