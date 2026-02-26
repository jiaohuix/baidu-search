"""
CrawlEngine - 统一网页抓取引擎

代价分级: httpx(L0) < crawl4ai(L1) < jina(L2)
- level=0: 仅 httpx，失败就放弃
- level=1: httpx 失败后 fallback 到 crawl4ai
- level=2: httpx -> crawl4ai -> jina，逐级升级

用法:
    engine = CrawlEngine(level=1)
    text = await engine.crawl(url)

已完成:
1. 添加 cache √
2. 完全异步化，使用 httpx.AsyncClient 替代 sync requests √
3. HTML 解析放到线程池避免阻塞 event loop √
"""

import os
import re
import time
import logging
import asyncio
from typing import Optional

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
    import chardet
    _HAS_CHARDET = True
except ImportError:
    _HAS_CHARDET = False

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

# 反爬虫/验证码检测
_ANTI_BOT_PATTERN = re.compile(
    r'验证码|请开启JavaScript|访问过于频繁|人机验证|'
    r'verify|captcha|forbidden|access denied|cloudflare',
    re.IGNORECASE,
)


def _is_bad_content(text: str, status_code: int = 200) -> bool:
    """判断抓取内容是否无效"""
    if status_code in (403, 503, 429, 520, 521, 522):
        return True
    if not text or len(text.strip()) < 50:
        return True

    # 检测反爬虫/验证码
    if _ANTI_BOT_PATTERN.search(text):
        logger.debug("[内容检测] 触发反爬虫/验证码")
        return True

    js_hits = len(_JS_PATTERN.findall(text))
    plain = re.sub(r'<[^>]+>', '', text).strip()
    if len(plain) < 100 and js_hits > 5:
        return True
    if len(plain) > 0 and js_hits / max(len(plain) / 100, 1) > 3:
        return True
    return False


def _html_to_markdown_sync(html: str) -> str:
    """用 readability + markdownify 提取正文转 markdown（同步版本，需在线程池调用）"""
    if not _HAS_READABILITY:
        return re.sub(r'<[^>]+>', '', html).strip()
    doc = ReadabilityDoc(html)
    main_html = doc.summary(html_partial=True)
    soup = BeautifulSoup(main_html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return md_convert(str(soup), heading_style="ATX").strip()


async def _html_to_markdown(html: str) -> str:
    """异步版本：在线程池中执行 HTML 解析，避免阻塞 event loop"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _html_to_markdown_sync, html)


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

    # ── L0: httpx (异步) ──
    async def _crawl_requests(self, url: str) -> Optional[str]:
        if not _HAS_HTTPX:
            logger.warning("[httpx] httpx 未安装，跳过")
            return None
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=self.timeout) as client:
                resp = await client.get(url, headers=self._HEADERS)

                # 智能编码检测，避免乱码
                content = resp.content  # 原始 bytes

                # 1. 优先使用响应头的编码
                encoding = resp.encoding
                logger.debug(f"[编码] 响应头编码: {encoding}")

                # 2. 如果响应头没有或不可靠，尝试从 HTML meta 标签提取
                if not encoding or encoding == "ISO-8859-1":
                    # 从 HTML 中查找 charset
                    import re as re_module
                    charset_match = re_module.search(
                        rb'<meta[^>]+charset=["\']?([^"\'>\s]+)',
                        content[:2048],  # 只检查前 2KB
                        re_module.IGNORECASE
                    )
                    if charset_match:
                        encoding = charset_match.group(1).decode('ascii', errors='ignore')
                        logger.debug(f"[编码] HTML meta 编码: {encoding}")

                # 3. 降级到 chardet 自动检测（如果安装了）
                if not encoding or encoding == "ISO-8859-1":
                    if _HAS_CHARDET:
                        import chardet
                        detected = chardet.detect(content[:10000])  # 检测前 10KB
                        if detected and detected.get('confidence', 0) > 0.7:
                            encoding = detected['encoding']
                            logger.info(f"[编码] chardet 检测: {encoding} (置信度: {detected.get('confidence'):.2f})")

                # 4. 最终降级到 UTF-8
                if not encoding:
                    encoding = "utf-8"

                logger.info(f"[编码] 最终使用: {encoding}")

                # 解码
                try:
                    text = content.decode(encoding, errors="replace")  # replace 比 ignore 更安全
                except (LookupError, TypeError) as e:
                    logger.warning(f"[编码] {encoding} 解码失败: {e}，降级到 utf-8")
                    text = content.decode("utf-8", errors="replace")

                logger.debug(f"[内容] 解码后长度: {len(text)} 字符，前100字符: {text[:100]}")

                # 检测内容质量（包含反爬虫检测）
                if _is_bad_content(text, resp.status_code):
                    logger.info(f"[httpx] 内容无效或触发反爬虫，升级到下一级: {url[:80]}")
                    return None

                if self.use_readability and _HAS_READABILITY:
                    content = await _html_to_markdown(text)
                    content = enhance_markdown_structure(content)
                    return content
                # 简单去标签
                return re.sub(r'<[^>]+>', '', text).strip()
        except Exception as e:
            logger.warning(f"[httpx] {e}")
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
    # 设置日志级别为 INFO 以查看详细信息
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    # engine = CrawlEngine(level=0)
    engine = CrawlEngine(level=2)  # 使用 level=2 测试所有后端
    print(f"可用后端: {engine.available_backends()}")

    url = "https://www.dayi.org.cn/qa/286155.html"
    url = "https://zhuanlan.zhihu.com/p/56592867" # 动态
    url = "https://baijiahao.baidu.com/s?id=1850641902495454566&wfr=spider&for=pc"
    text = await engine.crawl(url)
    if text:
        print(text[:10000])  # 只打印前 500 字符
        print(f"\n... (总长度: {len(text)} 字符)")
    else:
        print("抓取失败")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
