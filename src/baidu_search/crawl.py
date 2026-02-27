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
    r'验证码|请开启JavaScript|访问过于频繁|人机验证|环境异常|完成验证'
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


def _clean_html_noise(html: str) -> str:
    """结构化过滤：在 HTML 阶段就移除噪声标签（同步，需在线程池调用）"""
    if not _HAS_READABILITY:
        return html

    soup = BeautifulSoup(html, "lxml")

    # 1. 移除噪声标签（导航、页脚、侧边栏、脚本、样式）
    noise_tags = ["script", "style", "noscript", "nav", "footer", "aside", "header"]
    for tag in soup(noise_tags):
        tag.decompose()

    # 2. 移除常见广告/推荐容器（通过 class/id 启发式匹配）
    ad_patterns = re.compile(r'ad|advertisement|banner|promo|recommend|related|sidebar', re.I)
    for tag in soup.find_all(attrs={"class": ad_patterns}):
        tag.decompose()
    for tag in soup.find_all(attrs={"id": ad_patterns}):
        tag.decompose()

    return str(soup)


def _html_to_markdown_sync(html: str) -> str:
    """用 readability + markdownify 提取正文转 markdown（同步版本，需在线程池调用）"""
    if not _HAS_READABILITY:
        return re.sub(r'<[^>]+>', '', html).strip()

    # 先清洗 HTML 噪声
    html = _clean_html_noise(html)

    # 再用 readability 提取主内容
    doc = ReadabilityDoc(html)
    main_html = doc.summary(html_partial=True)
    soup = BeautifulSoup(main_html, "lxml")

    # 二次清理（防止 readability 遗漏）
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    return md_convert(str(soup), heading_style="ATX").strip()


async def _html_to_markdown(html: str) -> str:
    """异步版本：在线程池中执行 HTML 解析，避免阻塞 event loop"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _html_to_markdown_sync, html)



def clean_web_noise(md_text: str, link_density_threshold: float = 0.4) -> str:
    """
    通用网页噪声清洗：结合链接密度、启发式规则、区域截断

    Args:
        md_text: Markdown 格式的文本
        link_density_threshold: 链接密度阈值（0-1），超过则判定为导航/推荐区

    Returns:
        清洗后的文本
    """
    if not md_text:
        return ""

    # ── 预处理：移除残留的 HTML 标签（防御性） ──
    text = re.sub(r"<script.*?>.*?</script>", "", md_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)

    lines = text.split("\n")
    cleaned = []

    # ── 噪声关键词（中英文通用） ──
    noise_patterns = re.compile(
        r'icon_|登录|注册|首页|搜索|分享|收藏|关注|点赞|评论|举报|反馈|'
        r'到百度首页|百度首页|百度APP|下载|立即|查看更多|展开|收起|'
        r'作者最新文章|相关推荐|热门推荐|猜你喜欢|为您推荐|'
        r'京公网安备|ICP备|版权所有|Copyright|All Rights Reserved|'
        r'login|register|sign in|sign up|share|subscribe|follow|'
        r'^\[!\[|^\[\]\(|^!\[|^\*\*\s*$|'  # 空链接、空图片、空粗体
        r'^\d+阅读$|^\d+\s*阅读$|^\d+\s*views?$|'  # "12阅读" / "12 views"
        r'^热$|^新$|^荐$|^hot$|^new$',  # 单字标签
        re.IGNORECASE
    )

    # ── 区域截断标记（检测到后丢弃后续所有内容） ──
    in_noise_section = False
    noise_section_markers = re.compile(
        r'^#+\s*(作者最新文章|相关推荐|热门推荐|猜你喜欢|为您推荐|'
        r'related articles?|recommended|you may also like|more from)',
        re.IGNORECASE
    )

    for line in lines:
        stripped = line.strip()

        # ── 规则 1: 区域截断（最高优先级） ──
        if noise_section_markers.search(stripped):
            in_noise_section = True
            continue

        if in_noise_section:
            continue

        # ── 规则 2: 保留空行（维持段落结构） ──
        if not stripped:
            cleaned.append(line)
            continue

        # ── 规则 3: 基础长度过滤 ──
        # 保留标题（以 # 开头），其他行至少 5 字符
        if len(stripped) < 5 and not re.match(r'^#+\s', stripped):
            continue

        # ── 规则 4: 链接密度过滤 ──
        # 计算 Markdown 链接占比：[text](url)
        links = re.findall(r"\[(.*?)\]\(.*?\)", stripped)
        link_text_len = sum(len(l) for l in links)
        total_len = len(stripped)

        density = link_text_len / total_len if total_len > 0 else 0
        if density > link_density_threshold:
            continue  # 链接密度过高，判定为导航/推荐区

        # ── 规则 5: 关键词噪声过滤 ──
        if noise_patterns.search(stripped):
            continue

        # ── 规则 6: 特殊格式过滤 ──
        # 跳过纯数字链接行（如 "- 1[美用技术...]"）
        if re.match(r'^-?\s*\d+\[', stripped):
            continue

        # ── 规则 7: 清理 Markdown 格式噪声 ──
        # 移除图片: ![alt](url)
        cleaned_line = re.sub(r"!\[.*?\]\(.*?\)", "", stripped)
        # 将链接转换为纯文本: [text](url) -> text
        cleaned_line = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", cleaned_line)
        # 移除孤立 URL
        cleaned_line = re.sub(r"https?://\S+", "", cleaned_line)
        # 移除 Markdown 格式符号（可选，保留结构）
        # cleaned_line = re.sub(r"[*_>]", "", cleaned_line)

        cleaned_line = cleaned_line.strip()

        # ── 规则 8: 二次长度检查 ──
        if len(cleaned_line) >= 10 or re.match(r'^#+\s', cleaned_line):
            cleaned.append(cleaned_line)

    # ── 后处理：压缩多余空行 ──
    result = "\n".join(cleaned)
    result = re.sub(r"\n{3,}", "\n\n", result)  # 最多保留 2 个连续换行

    return result.strip()


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
                if text and not _is_bad_content(text, 200):  # 再次验证内容质量
                    logger.info(f"[crawl] {name} 成功: {url[:80]} | ✓ | ⏱: {elapsed:.2f}s")
                    result = text[:self.max_chars]
                    # ── 写缓存（只缓存有效内容）──
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
            if result and result.markdown:
                # 对 crawl4ai 的输出也应用清洗
                cleaned = clean_web_noise(result.markdown)
                cleaned = enhance_markdown_structure(cleaned)
                return cleaned
            return None

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

    # 清除缓存以测试新的反爬虫检测逻辑
    # import os
    # cache_file = ".cache/crawl.db"
    # if os.path.exists(cache_file):
    #     os.remove(cache_file)
    #     print(f"已删除缓存文件: {cache_file}")

    # engine = CrawlEngine(level=0)
    engine = CrawlEngine(level=2)  # 使用 level=2 测试所有后端
    print(f"可用后端: {engine.available_backends()}")

    url = "https://www.dayi.org.cn/qa/286155.html"
    url = "https://zhuanlan.zhihu.com/p/56592867" # 动态
    url = "https://baijiahao.baidu.com/s?id=1850641902495454566&wfr=spider&for=pc"
    url = "https://mp.weixin.qq.com/s?__biz=MzA5OTg0MzgzOQ==&mid=2247518674&idx=1&sn=cd7a35ca06b6b0f1f6166d41c837659b&chksm=9190f5ca03c8e53244ba6826f60ba29c7dfa0e07349eeeff500dd1a3bde2095227b409a41db7&scene=27"

    text = await engine.crawl(url)
    if text:
        print(text[:10000])  # 只打印前 500 字符
        print(f"\n... (总长度: {len(text)} 字符)")
    else:
        print("抓取失败")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
