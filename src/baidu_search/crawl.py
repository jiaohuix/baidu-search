"""
CrawlEngine - 统一网页抓取引擎

代价分级: requests(L0) < crawl4ai(L1) < jina(L2)
- level=0: 仅 requests，失败就放弃
- level=1: requests 失败后 fallback 到 crawl4ai
- level=2: requests -> crawl4ai -> jina，逐级升级

用法:
    engine = CrawlEngine(level=1)
    text = await engine.crawl(url)
TODO:
1 像search一样添加cache
"""

import os
import re
import time
import logging
from typing import Optional

import requests as sync_requests

from baidu_search.cache import get_crawl_cache

logger = logging.getLogger(__name__)

# ── 可用后端探测（按需导入，没装就跳过） ──
_HAS_CRAWL4AI = False
_HAS_HTTPX = False
_HAS_READABILITY = False

try:
    from crawl4ai import AsyncWebCrawler
    _HAS_CRAWL4AI = True
except ImportError:
    pass

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    pass

try:
    from readability import Document as ReadabilityDoc
    from markdownify import markdownify as md_convert
    from bs4 import BeautifulSoup
    _HAS_READABILITY = True
except ImportError:
    pass


# ── 内容质量检测 ──
_JS_PATTERN = re.compile(
    r'<script[\s>]|function\s*\(|var\s+\w+\s*=|document\.|window\.|'
    r'addEventListener|createElement|innerHTML',
    re.IGNORECASE,
)


def _is_bad_content(text: str, status_code: int = 200) -> bool:
    """判断抓取内容是否无效"""
    if status_code in (403, 503, 429, 520, 521, 522):
        return True
    if not text or len(text.strip()) < 50:
        return True
    js_hits = len(_JS_PATTERN.findall(text))
    plain = re.sub(r'<[^>]+>', '', text).strip()
    if len(plain) < 100 and js_hits > 5:
        return True
    if len(plain) > 0 and js_hits / max(len(plain) / 100, 1) > 3:
        return True
    return False


def _html_to_markdown(html: str) -> str:
    """用 readability + markdownify 提取正文转 markdown"""
    if not _HAS_READABILITY:
        return re.sub(r'<[^>]+>', '', html).strip()
    doc = ReadabilityDoc(html)
    main_html = doc.summary(html_partial=True)
    soup = BeautifulSoup(main_html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return md_convert(str(soup), heading_style="ATX").strip()


def enhance_markdown_structure(md_text: str) -> str:
    """
    Enhance Chinese structured headings for better RAG chunking.
    """

    lines = md_text.split("\n")
    enhanced = []

    for line in lines:
        stripped = line.strip()

        # 一级标题：一、二、三、
        if re.match(r"^[一二三四五六七八九十]+、", stripped):
            enhanced.append(f"## {stripped}")

        # 二级标题：1、2、
        elif re.match(r"^\d+、", stripped):
            enhanced.append(f"### {stripped}")

        # 星号列表转普通文本（避免乱层级）
        elif stripped.startswith("* "):
            enhanced.append(stripped.replace("* ", "- "))

        else:
            enhanced.append(line)

    return "\n".join(enhanced)


class CrawlEngine:
    """统一爬取引擎，支持多级 fallback。

    Args:
        level: 最高允许使用的后端等级 (0=requests, 1=+crawl4ai, 2=+jina)
        timeout: 超时秒数
        use_readability: 是否用 readability 提取正文(需装 readability-lxml, markdownify)
        jina_api_key: Jina API key，不传则读环境变量
        max_chars: 返回内容最大字符数
    """

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    def __init__(
        self,
        level: int = 0,
        timeout: int = 15,
        use_readability: bool = True,
        jina_api_key: str = "",
        max_chars: int = 30000,
    ) -> None:
        self.level = level
        self.timeout = timeout
        self.use_readability = use_readability
        self.max_chars = max_chars
        self.jina_api_key = jina_api_key or os.environ.get("JINA_API_KEY", "")

    # ── 主入口 ──
    async def crawl(self, url: str) -> Optional[str]:
        """按 level 逐级尝试抓取，返回 markdown 文本或 None"""
        # ── 查缓存 ──
        cache = get_crawl_cache()
        cache_key = f"crawl:{url}"
        cached = await cache.get(cache_key)
        if cached is not None:
            logger.info(f"[crawl][cache hit] {url[:80]}")
            return cached

        # ── 逐级尝试 ──
        backends = self._build_chain()
        attempted = []

        for name, fn in backends:
            attempted.append(name)
            start = time.time()
            try:
                text = await fn(url)
                elapsed = time.time() - start
                if text:
                    logger.info(f"[crawl] {name} 成功: {url[:80]} | ✓ | ⏱: {elapsed:.2f}s")
                    result = text[:self.max_chars]
                    # ── 写缓存 ──
                    await cache.set(cache_key, result)
                    return result
                else:
                    logger.warning(f"[crawl] {name} 未获取有效内容 | ✗ | ⏱: {elapsed:.2f}s")
            except Exception as e:
                elapsed = time.time() - start
                logger.warning(f"[crawl] {name} 异常: {e} | ✗ | ⏱: {elapsed:.2f}s")

        logger.warning(f"[crawl] 所有尝试的后端均失败: {attempted}, url={url}")
        return None

    def _build_chain(self) -> list:
        """根据 level 和可用性构建 fallback 链"""
        chain = [("requests", self._crawl_requests)]
        if self.level >= 1 and _HAS_CRAWL4AI:
            chain.append(("crawl4ai", self._crawl_crawl4ai))
        if self.level >= 2 and _HAS_HTTPX:
            chain.append(("jina", self._crawl_jina))
        return chain

    # ── L0: requests ──
    async def _crawl_requests(self, url: str) -> Optional[str]:
        try:
            resp = sync_requests.get(
                url, headers=self._HEADERS, timeout=self.timeout, allow_redirects=True,
            )
            if _is_bad_content(resp.text, resp.status_code):
                return None
            if self.use_readability and _HAS_READABILITY:
                content = _html_to_markdown(resp.text)
                content = enhance_markdown_structure(content)
                return content
            # 简单去标签
            return re.sub(r'<[^>]+>', '', resp.text).strip()
        except Exception as e:
            logger.warning(f"[requests] {e}")
            return None

    # ── L1: crawl4ai ──
    async def _crawl_crawl4ai(self, url: str) -> Optional[str]:
        if not _HAS_CRAWL4AI:
            return None
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)
            return result.markdown if result and result.markdown else None

    # ── L2: jina ──
    async def _crawl_jina(self, url: str) -> Optional[str]:
        if not _HAS_HTTPX:
            return None
        headers = {}
        if self.jina_api_key:
            headers["Authorization"] = f"Bearer {self.jina_api_key}"
        target = f"https://r.jina.ai/{url}"
        async with httpx.AsyncClient(http2=True, timeout=30.0) as client:
            resp = await client.get(target, headers=headers, follow_redirects=True)
            resp.raise_for_status()
            return resp.text if resp.text else None

    # ── 便捷方法 ──
    def available_backends(self) -> list[str]:
        """返回当前环境可用的后端列表"""
        backends = ["requests"]
        if _HAS_CRAWL4AI:
            backends.append("crawl4ai")
        if _HAS_HTTPX:
            backends.append("jina")
        return backends


async def main():
    # engine = CrawlEngine(level=0)
    engine = CrawlEngine(level=1)
    print(f"可用后端: {engine.available_backends()}")

    url = "https://www.dayi.org.cn/qa/286155.html"
    url = "https://zhuanlan.zhihu.com/p/56592867" # 动态
    text = await engine.crawl(url)
    if text:
        print(text)
    else:
        print("抓取失败")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
