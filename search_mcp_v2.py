"""
MCP Server for baidu-search
增强版：鲁棒抓取 + URL 虚拟化 + 内容压缩优化
- 将长 URL 压缩为 cite://{domain}/{hash} 格式
- 在工具层维护短链与真实 URL 的映射
- 提供 HTTP API 查询真实 URL

example:
curl "http://127.0.0.1:8080/resolve_cite?cite=cite://abc123"
curl "http://127.0.0.1:8080/resolve_cite?cites=cite://a1,cite://b2,cite://c3"

{"cite":"cite://mp.weixin.qq.com/bd13621488","real":"https://mp.weixin.qq.com/s?__biz=MzA3"}

"""

import os
import json
import argparse
import hashlib
from urllib.parse import urlparse
from typing import List, Dict, Optional,Union
import asyncio

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from baidu_search import BaiduSearch, CrawlEngine, ContextCompressor


# ============ URL 虚拟化模块 ============

class URLMemory:
    """URL 映射存储，维护虚拟 URL 与真实 URL 的双向映射，并记录搜索意图"""

    def __init__(self):
        self.cite_to_real: Dict[str, str] = {}  # cite:// -> real URL
        self.real_to_cite: Dict[str, str] = {}  # real URL -> cite://
        self.cite_to_keywords: Dict[str, List[str]] = {}  # cite:// -> [keywords]

    def add(self, real_url: str, cite_url: str, keywords: Optional[str] = None):
        """
        添加映射

        Args:
            real_url: 真实 URL
            cite_url: 虚拟 URL
            keywords: 搜索关键词（可选）
        """
        self.cite_to_real[cite_url] = real_url
        self.real_to_cite[real_url] = cite_url

        # 记录搜索关键词
        if keywords:
            if cite_url not in self.cite_to_keywords:
                self.cite_to_keywords[cite_url] = []
            if keywords not in self.cite_to_keywords[cite_url]:
                self.cite_to_keywords[cite_url].append(keywords)

    def get_real(self, cite_url: str) -> Optional[str]:
        """通过虚拟 URL 获取真实 URL"""
        return self.cite_to_real.get(cite_url)

    def get_cite(self, real_url: str) -> Optional[str]:
        """通过真实 URL 获取虚拟 URL"""
        return self.real_to_cite.get(real_url)

    def get_keywords(self, cite_url: str) -> Optional[str]:
        """
        获取 cite:// URL 关联的搜索关键词

        Returns:
            最近一次的搜索关键词，如果有多个则返回最后一个
        """
        keywords_list = self.cite_to_keywords.get(cite_url, [])
        return keywords_list[-1] if keywords_list else None

    def exists(self, cite_url: str) -> bool:
        """检查虚拟 URL 是否存在"""
        return cite_url in self.cite_to_real


def generate_cite_url(real_url: str) -> str:
    """
    生成短虚拟引用 URL
    格式: cite://{domain}/{sha1(real_url)[:10]}

    Args:
        real_url: 原始完整 URL

    Returns:
        短虚拟引用 URL
    """
    parsed = urlparse(real_url)
    domain = parsed.netloc.lower()
    # 移除 www. 前缀
    if domain.startswith("www."):
        domain = domain[4:]

    hash_id = hashlib.sha1(real_url.encode("utf-8")).hexdigest()[:10]
    return f"cite://{domain}/{hash_id}"


def is_cite_url(url: str) -> bool:
    """判断是否为虚拟 URL"""
    return url.startswith("cite://")


# ============ 初始化 ============

mcp = FastMCP(name="search_mcp")
searcher = BaiduSearch()
crawl_engine = CrawlEngine(level=0)
compressor = ContextCompressor(splitter="jina")  # 全局压缩器
url_memory = URLMemory()  # URL 映射存储


def err(msg: str) -> str:
    """统一错误返回 JSON，自动提取核心错误信息"""
    if not msg:
        return json.dumps({"error": "unknown_error"}, ensure_ascii=False)

    msg = msg.replace("\n", " ").strip()

    if "Timeout" in msg or "timed out" in msg.lower():
        core = "timeout"
    elif "Connection" in msg or "ConnectError" in msg:
        core = "connection_error"
    elif "403" in msg:
        core = "http_403"
    elif "404" in msg:
        core = "http_404"
    elif "CancelledError" in msg:
        core = "cancelled"
    else:
        core = msg[:120]

    return json.dumps({"error": core}, ensure_ascii=False)



# ============ HTTP 路由 ============

@mcp.custom_route("/health", methods=["GET"])
async def health_check(_: Request) -> JSONResponse:
    """健康检查"""
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/resolve_cite", methods=["GET"])
async def resolve_cite(request: Request) -> JSONResponse:
    """
    解析虚拟 URL 为真实 URL

    查询参数:
        - cite: 单个虚拟 URL (cite://...)
        - cites: 多个虚拟 URL，逗号分隔

    返回:
        - 单个: {"cite": "cite://...", "real": "https://..."}
        - 多个: [{"cite": "cite://...", "real": "https://..."}, ...]
    """
    cite = request.query_params.get("cite")
    cites = request.query_params.get("cites")

    if cite:
        # 单个查询
        real_url = url_memory.get_real(cite)
        if real_url:
            return JSONResponse({"cite": cite, "real": real_url})
        else:
            return JSONResponse({"error": "cite_not_found", "cite": cite}, status_code=404)

    elif cites:
        # 批量查询
        cite_list = [c.strip() for c in cites.split(",") if c.strip()]
        results = []
        for c in cite_list:
            real_url = url_memory.get_real(c)
            results.append({
                "cite": c,
                "real": real_url if real_url else None
            })
        return JSONResponse(results)

    else:
        return JSONResponse({"error": "missing_parameter", "hint": "use ?cite=... or ?cites=..."}, status_code=400)


# ============ MCP 工具 ============

@mcp.tool(name="web_search")
async def search_baidu(query: str, offset: int = 0, limit: int = 10) -> str:
    """
    功能：
        Web 搜索工具，用于根据关键词在网络上检索相关信息,支持分页查询。

    参数：
        query:str 搜索关键词,空格分隔且勿加双引号
        offset:int 偏移量，从第几条开始返回，默认 0
        limit:int 返回结果数量，默认 10

    返回：
        str (JSON 格式字符串): [{"rank":int,"title":str, "abstract":str, "url": str}]

    示例：
        - offset=0, limit=5: 返回前 5 条
        - offset=5, limit=5: 返回第 6-10 条
    """
    try:
        result_str = await searcher.search(query, offset=offset, limit=limit)
        if not result_str:
            return err("search no_results")

        # 解析搜索结果
        results = json.loads(result_str)

        # URL 虚拟化并记录搜索意图
        for item in results:
            real_url = item.get("url", "")
            if real_url and not is_cite_url(real_url):
                # 生成虚拟 URL
                cite_url = generate_cite_url(real_url)
                # 存储映射并关联搜索关键词
                url_memory.add(real_url, cite_url, keywords=query)
                # 替换为虚拟 URL
                item["url"] = cite_url

        return json.dumps(results, ensure_ascii=False)

    except Exception as e:
        return err(f"search_failed: {e}")


# @mcp.tool(name="fetch_content")
# async def fetch_content(url: str, n: int = 500, query: str = "") -> str:
#     """
#     抓取网页正文并进行上下文压缩。
#     支持虚拟 URL (cite://) 和真实 URL (http/https)。

#     参数：
#         url: 网页链接
#             - cite:// 虚拟 URL（自动转换，无需传 query）
#             - http/https 真实 URL（可选传 query）
#         n: 最大返回字符数
#         query: 压缩关键词（可选）
#             - cite:// URL 自动使用搜索词
#             - http:// URL 可选传入

#     返回：
#         JSON 字符串
#     """
#     # 处理虚拟 URL 并获取关键词
#     real_url = url
#     is_cite = is_cite_url(url)

#     if is_cite:
#         resolved = url_memory.get_real(url)
#         if not resolved:
#             return err(f"cite_not_found: {url}")
#         real_url = resolved

#         # cite:// URL 自动获取搜索关键词
#         if not query:
#             query = url_memory.get_keywords(url) or ""

#     # 抓取网页内容
#     try:
#         text = await crawl_engine.crawl(real_url)
#     except Exception as e:
#         return err(f"crawl_failed: {e}")

#     if not text:
#         return err("empty_page")

#     if isinstance(text, bytes):
#         try:
#             text = text.decode("utf-8", "ignore")
#         except Exception:
#             return err("decode_failed")

#     orig_len = len(text)

#     # 上下文压缩
#     try:
#         if query:
#             result = compressor.compress(query=query, context=text, max_chars=n)
#         else:
#             result = text[:n]
#     except Exception as e:
#         return err(f"compress_failed: {e}")

#     ratio = round(len(result) / orig_len, 2) if orig_len > 0 else 1.0
#     data = {"text": result, "ratio": ratio}
#     return json.dumps(data, ensure_ascii=False)

@mcp.tool(name="fetch_content")
async def fetch_multiple_contents(
    urls: List[str],  # 强制使用 List，去掉 Union
    n: int = 500, 
    query: str = ""
) -> str:
    """
    批量抓取并对比多个网页内容。
    
    当你从搜索结果中获得多个相关链接时，请务必通过此工具一次性传入所有 URL 列表。
    
    参数：
        urls: 必须是一个包含多个 URL 的字符串列表。例如：["https://a.com", "https://b.com"]
        n: 每个网页压缩后的最大字符数，默认为 500。
        query: 统一的压缩关键词，工具会根据此词在所有网页中提取最相关的片段。
    """

    # 定义单任务处理函数
    async def process_single_url(url: str) -> Dict:
        real_url = url
        current_query = query
        
        # 1. 处理虚拟 URL 逻辑
        if is_cite_url(url):
            resolved = url_memory.get_real(url)
            if not resolved:
                return {"url": url, "error": "cite_not_found"}
            real_url = resolved
            if not current_query:
                current_query = url_memory.get_keywords(url) or ""

        # 2. 抓取内容
        try:
            text = await crawl_engine.crawl(real_url)
            if not text:
                return {"url": url, "error": "empty_page"}
            
            if isinstance(text, bytes):
                text = text.decode("utf-8", "ignore")
        except Exception as e:
            return {"url": url, "error": f"crawl_failed: {str(e)}"}

        # 3. 压缩内容
        orig_len = len(text)
        try:
            if current_query:
                result = compressor.compress(query=current_query, context=text, max_chars=n)
            else:
                result = text[:n]
        except Exception as e:
            return {"url": url, "error": f"compress_failed: {str(e)}"}

        ratio = round(len(result) / orig_len, 2) if orig_len > 0 else 1.0
        return {"url": url, "text": result, "ratio": ratio}

    # --- 并发执行核心逻辑 ---
    # 使用 asyncio.gather 同时发起请求，不再一个一个排队
    tasks = [process_single_url(u) for u in urls]
    results = await asyncio.gather(*tasks)

    # 封装最终结果
    return json.dumps(results, ensure_ascii=False)


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