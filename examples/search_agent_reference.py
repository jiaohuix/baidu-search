import json
import asyncio
import re
from typing import Any, Callable, Dict

# from agno.db.sqlite import SqliteDb
from agno.tools.mcp import MCPTools
from agno.agent import Agent
from agno.models.openai import OpenAIResponses
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.tools import tool

from agno.models.openai import OpenAILike

API_URL = "https://api.siliconflow.cn/v1"
API_KEY = "sk-"
MODEL_NAME="Qwen/Qwen3-8B"
llm = OpenAILike(id=MODEL_NAME, api_key=API_KEY, base_url=API_URL, 
                extra_body = {"enable_thinking": False})

# ============================================================
# 2. Tool Hook：拦截搜索工具，提取 URL + title 映射
# ============================================================
async def capture_search_urls(
    run_context: RunContext,
    function_name: str,
    function_call: Callable,
    arguments: Dict[str, Any],
):
    print(f"--- 开始调用工具: {function_name} ---")
    result = await function_call(**arguments)
    print("result type:", type(result))

    if function_name == "web_search":
        try:
            # 假设 result.content 是 JSON 字符串
            items = json.loads(result.content)

            if not isinstance(items, list):
                return result

            # 获取或初始化 url_map: { "1": {"url": "...", "title": "..."} }
            url_map = run_context.session_state.get("url_map", {})
            
            for item in items:
                # 优先级：url > displayUrl > link
                url = item.get("url") or item.get("displayUrl") or item.get("link")
                title = item.get("title") or item.get("snippet", "")[:80] or "(无标题)"
                rank = item.get("rank") or item.get("index")

                if url and rank is not None:
                    url_map[str(rank)] = {"url": url, "title": title}
            
            run_context.session_state["url_map"] = url_map
            print(f"成功提取 {len(url_map)} 个 URL + title 映射")
            
        except (json.JSONDecodeError, TypeError, Exception) as e:
            print(f"解析搜索结果失败: {e}")

    return result


# ============================================================
# 3. Post Hook：注入引用链接 & References 列表
# ============================================================
def inject_citation_urls(
    run_output: RunOutput,
    run_context: RunContext,
) -> None:
    print("inject_citation_urls...")

    url_map = run_context.session_state.get("url_map", {})  # {"1": {"url":.., "title":..}, ...}

    if not run_output.content or not url_map:
        return

    content = run_output.content

    # A. 清洗 [[citation:1]] → [citation:1]
    content = re.sub(r"\[\[([cC]itation:\d+)\]\]", r"[\1]", content)

    # B. 替换 [citation:1] 为 <sup>[1]</sup> （上标 + 可点击链接）
    def replace_citation(match):
        num = match.group(1)
        if num in url_map:
            info = url_map[num]
            url = info["url"]
            return f'<sup><a href="{url}" title="{info["title"]}">[ {num} ]</a></sup>'
            # 或者更简洁： f'<sup>[{num}]({url})</sup>'   但部分渲染器对 Markdown 在 sup 里支持不好
        return match.group(0)

    content = re.sub(
        r"\[[cC]itation:(\d+)\]",
        replace_citation,
        content
    )

    # 额外处理：如果 LLM 直接写了 [1] [2] 这种，也尝试转为上标（可选，视情况加）
    # content = re.sub(r"\[(\d+)\]", lambda m: f'<sup>[{m.group(1)}]</sup>' if m.group(1) in url_map else m.group(0), content)

    # C. References 部分：全部显示，按数字顺序（不管是否被引用）
    all_nums = sorted([int(k) for k in url_map.keys() if k.isdigit()])

    if all_nums:
        reference_section = [
            "\n\n---",
            "### References",
            ""
        ]

        for num in all_nums:
            info = url_map.get(str(num))
            if info:
                title = re.sub(r"\s+", " ", info["title"].strip())
                url = info["url"]
                reference_section.append(f"{num}. [{title}]({url})")

        content += "\n".join(reference_section)

    run_output.content = content
    print("final content length:", len(content))

system_prompt = '''
# Role
    You are a high-performance Retrieval-Augmented Generation (RAG) agent. Your goal is to provide evidence-based, objective, and precise answers by utilizing web search tools.

    # Core Principles
    1. **Search & Fetch**: Use `web_search` to find relevant sources and `fetch_content` to dive deep into the most promising ones. Never rely solely on internal knowledge for factual or time-sensitive queries.
    2. **Strict Grounding**: Do NOT hallucinate. If the search results are contradictory or insufficient, explicitly state: "Information is missing about [topic]."
    3. **Citations**: 
      - Every factual claim MUST be followed by a citation in the format [citation:x].
      - Example: "The capital of France is Paris [citation:1]."
      - Group multiple sources: [citation:1][citation:2].
    4. **Language Consistency**: Always respond in the SAME LANGUAGE as the user's query.

    # Response Structure
    - **Summary**: A concise, direct answer to the user's question.
    - **Detailed Analysis**: A comprehensive explanation backed by evidence and citations.
    - **Source Evaluation**: Briefly mention if any sources were conflicting or biased.
    - **Confidence Score**: (0.0 - 1.0)

    # Critical Output Format
    If the user's query is a multiple-choice question or requires a single definitive conclusion, you MUST output the final result on the VERY LAST LINE in the following format:
    FINAL ANSWER: [Your Answer Here]

    # Execution Task
    Answer the following user query now:
'''

# db = SqliteDb(db_file="tmp/agents.db")

async def main():
    # Connect to your Baidu search MCP server
    mcp_tools = MCPTools(
        transport="streamable-http",
        url="http://127.0.0.1:8080/mcp"
    )
    await mcp_tools.connect()

    try:
        agent = Agent(
            model=llm,
            tools=[mcp_tools],
            tool_hooks=[capture_search_urls],
            post_hooks=[inject_citation_urls],
            session_state={"url_map": {"fake":"http://baidu.com"}},
            instructions=system_prompt,
            # db=db,
            add_history_to_context=True,
            num_history_runs=3,
            markdown=True,
            debug_mode=True,
            debug_level=2,
        )
        query = "强化学习有哪些算法"
        # response = await agent.aprint_response(query, stream=True)
        response = await agent.arun(query)
        print("URL 映射表 agent：", agent.session_state.get("url_map", {}))
        # print("response.session_state",response.session_state.get("url_map", {}))
        with open("output.md", "w") as f:
            f.write(response.content)
        
    finally:
        await mcp_tools.close()


if __name__ == "__main__":
    asyncio.run(main())