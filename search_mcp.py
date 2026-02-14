"""
MCP Server for baidu-search 
增强版：鲁棒抓取，所有工具失败时返回 error JSON

TODO: 缩略url信息和query信息，可以简化llm参数生成复杂度
"""

import os
import json
import argparse
from typing import List, Literal

from fastmcp import FastMCP
from baidu_search import BaiduSearch, CrawlEngine, ContextCompressor


mcp = FastMCP(name="baidu_search_mcp")
searcher = BaiduSearch()
crawl_engine = CrawlEngine(level=2)


def err(msg: str) -> str:
    """统一错误返回 JSON"""
    return json.dumps({"error": msg}, ensure_ascii=False)


@mcp.tool(name="search_baidu")
async def search_baidu(query: str, num_results: int = 5) -> str:
    """
    功能：
        在百度上搜索关键词，并返回结构化结果。

    参数：
        query: 搜索关键词
        num_results: 返回结果数量，默认 5

    返回：
        JSON: 搜索结果
    """
    try:
        result = await searcher.search(query, num_results=num_results)
        if not result:
            return err("search no_results")
        return json.dumps({"text": result}, ensure_ascii=False)
    except Exception as e:
        return err(f"search_failed: {e}")


@mcp.tool(name="fetch_content")
async def fetch_content(
    url: str,
    mode: Literal["full", "head", "tail", "grep", "compress"] = "full",
    n: int = 1000,
    keyword: str = "",
    query: str = ""
) -> str:
    """
    功能：
        抓取网页正文，并根据模式处理内容，返回 JSON 字符串。

    参数：
        url: 网页链接
        mode: 操作模式，可选：
            - full: 返回全文（截断至 n 字符）
            - head: 返回前 n 字符
            - tail: 返回后 n 字符
            - grep: 返回包含 keyword 的段落，最多 n 字符
            - compress: 使用 ContextCompressor 对文本进行 query-aware 压缩
        n: 最大返回字符数
        keyword: grep 模式下使用的关键词
        query: compress 模式下使用的查询上下文

    返回：
        JSON 字符串：
        {
            "text": str,       # 返回的文本
            "orig_len": int,   # 原始文本长度
            "ret_len": int     # 返回文本长度
        }
        如果抓取或处理失败，则返回：
        {"error": "错误信息"}
    """
    try:
        text = await crawl_engine.crawl(url)
    except Exception as e:
        return err(f"crawl_failed: {e}")

    if not text:
        return err("empty_page")

    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8", "ignore")
        except Exception:
            return err("decode_failed")

    orig_len = len(text)

    try:
        if mode == "head":
            result = text[:n]
        elif mode == "tail":
            result = text[-n:]
        elif mode == "grep":
            paras = text.split("\n")
            hits = [p for p in paras if keyword in p]
            result = "\n".join(hits)[:n]
        elif mode == "compress":
            compressor = ContextCompressor(max_chars=n)
            result = compressor.compress(query=query, context=text)
        else:  # full
            result = text[:n]
    except Exception as e:
        return err(f"process_failed: {e}")

    data = {"text": result, "orig_len": orig_len, "ret_len": len(result)}
    return json.dumps(data, ensure_ascii=False)


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