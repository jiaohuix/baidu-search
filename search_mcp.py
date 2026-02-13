"""
MCP Server for baidu-search project
- 使用 HTTP transport
- 支持自定义端口
- 三工具：
  1. 百度搜索
  2. 页面抓取
  3. 文本摘要（使用 ContextCompressor）
"""

import os
import argparse
from fastmcp import FastMCP
from typing import List
from pydantic import Field

# 引入你的项目模块
from baidu_search import BaiduSearch, CrawlEngine, ContextCompressor
# from baidu_search.engine import CrawlEngine
# from baidu_search.compressor import ContextCompressor

# --- 初始化 MCP ---
mcp = FastMCP(
    name="baidu_search_mcp",
)

# --- 初始化搜索和抓取 ---
searcher = BaiduSearch()
crawl_engine = CrawlEngine()

# --- Tool 1: 百度搜索 ---
@mcp.tool(name="search_baidu", description="在百度上进行网页搜索并返回结构化结果")
async def search_baidu(
    query: str = Field(description="搜索关键词"),
    num_results: int = Field(default=5, description="返回结果数量")
) -> List[dict]:
    results = await searcher.search(query, num_results=num_results)
    return results

# --- Tool 2: 页面抓取 ---
@mcp.tool(name="fetch_page", description="抓取指定页面并提取正文")
async def fetch_page(
    url: str = Field(description="要抓取的网页链接")
) -> str:
    text = await crawl_engine.crawl(url)
    return text

# --- Tool 3: 摘要生成 ---
# todo: 应该集成到内容获取，而不是让llm传入ctx然后压缩
@mcp.tool(name="summarize_text", description="对文本生成摘要（使用 ContextCompressor）")
async def summarize_text(
    text: str = Field(description="需要摘要的文本"),
    query: str = Field(default="", description="可选的查询上下文，用于 ContextCompressor"),
    max_chars: int = Field(default=500, description="摘要最大字符数")
) -> str:
    compressor = ContextCompressor(max_chars=max_chars)
    summary = compressor.compress(query=query, context=text)

    return summary

# --- 启动 MCP Server ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="启动 MCP Server (HTTP)")
    parser.add_argument(
        "--host", type=str, default="127.0.0.1",
        help="绑定主机，默认 127.0.0.1"
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MCP_PORT", 8080)),
        help="HTTP 端口，默认 8080，可用环境变量 MCP_PORT 配置"
    )
    args = parser.parse_args()

    print(f"Starting MCP HTTP server on {args.host}:{args.port} ...")
    mcp.run(transport="http", host=args.host, port=args.port)