'''
uv pip install agno mcp
'''
import asyncio
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.tools.mcp import MCPTools
from agno.models.openai import OpenAILike

API_URL = "https://api.siliconflow.cn/v1"
API_KEY = "sk-"
MODEL_NAME="Qwen/Qwen3-8B"

llm = OpenAILike(id=MODEL_NAME, api_key=API_KEY, base_url=API_URL, 
                extra_body = {"enable_thinking": False})


system_prompt = '''
You are a helpful assistant.

You can use the MCP tools to:
- search the web
- open and read specific web pages when necessary

When searching:
- Generate 3–6 concise search terms derived from the user query.
- Prefer precise and relevant keywords.

If the search results are insufficient or ambiguous:
- Open and read the most relevant web pages to extract accurate information.
- Base your final answer on the content of the pages you read.

Always prioritize accuracy and cite information from the pages you accessed when appropriate.
'''

db = SqliteDb(db_file="tmp/agents.db")

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
            session_state={"round": 0, "answers": []},
            tools=[mcp_tools],
            instructions=system_prompt,
            db=db,
            add_history_to_context=True,
            num_history_runs=3,
            markdown=True,
            debug_mode=True,
            debug_level=2,
        )

        query = "GLM5编程能力如何"
        await agent.aprint_response(query, stream=True)
        query = "读取网页内容https://zhuanlan.zhihu.com/p/56592867"
        await agent.aprint_response(query, stream=True)
        
    finally:
        await mcp_tools.close()

if __name__ == "__main__":
    asyncio.run(main())