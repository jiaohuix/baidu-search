"""
MCP Server for baidu-search
增强版：鲁棒抓取 + URL 虚拟化
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
from typing import List, Literal, Dict, Optional

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from baidu_search import BaiduSearch, CrawlEngine, ContextCompressor


# ============ URL 虚拟化模块 ============

class URLMemory:
    """URL 映射存储，维护虚拟 URL 与真实 URL 的双向映射"""

    def __init__(self):
        self.cite_to_real: Dict[str, str] = {}  # cite:// -> real URL
        self.real_to_cite: Dict[str, str] = {}  # real URL -> cite://

    def add(self, real_url: str, cite_url: str):
        """添加映射"""
        self.cite_to_real[cite_url] = real_url
        self.real_to_cite[real_url] = cite_url

    def get_real(self, cite_url: str) -> Optional[str]:
        """通过虚拟 URL 获取真实 URL"""
        return self.cite_to_real.get(cite_url)

    def get_cite(self, real_url: str) -> Optional[str]:
        """通过真实 URL 获取虚拟 URL"""
        return self.real_to_cite.get(real_url)

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
crawl_engine = CrawlEngine(level=2)
url_memory = URLMemory()  # URL 映射存储


def err(msg: str) -> str:
    """统一错误返回 JSON"""
    return json.dumps({"error": msg}, ensure_ascii=False)


# ============ HTTP 路由 ============

@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
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
async def search_baidu(query: str, num_results: int = 5) -> str:
    """
    功能：
        Web 搜索工具，用于根据关键词在网络上检索相关信息。
        返回的 URL 已虚拟化为 cite:// 协议，大幅降低 token 占用。

    参数：
        query:str 搜索关键词
        num_results:int 返回结果数量，默认 5

    返回：
        str (JSON 格式字符串): [{"rank":int,"title":str, "abstract":str, "url": str}]
        其中 url 为虚拟引用格式: cite://{domain}/{hash}
    """
    try:
        result_str = await searcher.search(query, num_results=num_results)
        if not result_str:
            return err("search no_results")

        # 解析搜索结果
        results = json.loads(result_str)

        # URL 虚拟化
        for item in results:
            real_url = item.get("url", "")
            if real_url and not is_cite_url(real_url):
                # 生成虚拟 URL
                cite_url = generate_cite_url(real_url)
                # 存储映射
                url_memory.add(real_url, cite_url)
                # 替换为虚拟 URL
                item["url"] = cite_url

        return json.dumps(results, ensure_ascii=False)

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
        支持虚拟 URL (cite://) 和真实 URL (http/https)。

    参数：
        url: 网页链接，支持：
            - cite:// 虚拟引用 URL（自动转换为真实 URL）
            - http/https 真实 URL
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
    # 处理虚拟 URL
    real_url = url
    if is_cite_url(url):
        resolved = url_memory.get_real(url)
        if not resolved:
            return err(f"cite_not_found: {url}")
        real_url = resolved

    try:
        text = await crawl_engine.crawl(real_url)
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