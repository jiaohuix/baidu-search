import json
import re
from typing import Any, Callable, Dict

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
# 1. 假的搜索工具
# ============================================================
@tool
def web_search(query: str) -> str:
    """搜索网页，返回搜索结果列表。

    Args:
        query: 搜索关键词
    """
    fake_results = [
        {
            "title": "Python 官方教程",
            "url": "https://docs.python.org/3/tutorial/",
            "snippet": "Python 是一种易于学习、功能强大的编程语言。",
        },
        {
            "title": "Python 维基百科",
            "url": "https://zh.wikipedia.org/wiki/Python",
            "snippet": "Python 由 Guido van Rossum 于 1991 年首次发布。",
        },
        {
            "title": "Real Python 教程",
            "url": "https://realpython.com/",
            "snippet": "Real Python 提供高质量的 Python 教程和文章。",
        },
    ]
    return json.dumps(fake_results, ensure_ascii=False)


# ============================================================
# 2. Tool Hook：拦截搜索工具，提取 URL 映射
#    tool_hooks 支持 run_context 参数
# ============================================================
def capture_search_urls(
    run_context: RunContext,
    function_name: str,
    function_call: Callable,
    arguments: Dict[str, Any],
):
    print("开始工具调用....")
    result = function_call(**arguments)
    print("capture_search_urls 调用成功, function_name:",function_name)
    print("run_context.session_state",run_context.session_state)
    if function_name == "web_search":
        try:
            items = json.loads(result)
            url_map = run_context.session_state.get("url_map", {})
            # idx = len(url_map) + 1
            numeric_keys = [int(k) for k in url_map.keys() if str(k).isdigit()]
            idx = max(numeric_keys, default=0) + 1
            for item in items:
                url_map[str(idx)] = item.get("url", "")
                idx += 1
            print("url_map",url_map)
            run_context.session_state["url_map"] = url_map
        except (json.JSONDecodeError, TypeError):
            pass

    return result

# ============================================================
# 3. Post Hook：用 agent 参数获取 session_state
#    注意：只用 run_output 一个参数！
#    然后通过 agent 拿 session_state
# ============================================================
def inject_citation_urls(
    run_output: RunOutput,
    run_context: RunContext,
) -> None:
    """用 run_context.session_state 获取 URL 映射，和 tool_hook 是同一个对象"""
    url_map = {}
    if run_context.session_state:
        url_map = run_context.session_state.get("url_map", {})

    print(f"[post_hook] url_map = {url_map}")

    if not run_output.content or not url_map:
        return

    def replace_citation(match):
        num = match.group(1)
        url = url_map.get(num, "")
        if url:
            return f"[{num}]({url})"
        return match.group(0)

    run_output.content = re.sub(
        r"\[(\d+)\](?!\()", replace_citation, run_output.content
    )



# ============================================================
# 4. 创建 Agent
# ============================================================
agent = Agent(
    model=llm,
    tools=[web_search],
    tool_hooks=[capture_search_urls],
    post_hooks=[inject_citation_urls],
    session_state={"url_map": {"fake":"http://baidu.com"}},
    instructions=[
        "你是一个搜索助手。回答问题时请使用搜索工具获取信息。",
        "在回答中使用编号引用，格式为 [1]、[2]、[3] 等，对应搜索结果的顺序。",
        "不要在引用中写 URL，只写编号即可。",
        "必须必须调用工具！！！"
    ],
    debug_mode = True,
    markdown=True,
)

# ============================================================
# 5. 运行
# ============================================================
if __name__ == "__main__":
    response = agent.run("python最新版本是多少，python是什么")


    print("=" * 60)
    print("最终输出（引用已自动填充 URL）：")
    print("=" * 60)
    print(response.content)
    print()
    print("URL 映射表 agent：", agent.session_state.get("url_map", {}))
    print("response.session_state",response.session_state.get("url_map", {}))