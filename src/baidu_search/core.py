"""
@date: 2026/02/13
@author: jiaohuix
@description: BaiduSearch - 异步百度搜索模块

已实现：
1. 多页并发搜索（asyncio + httpx）
2. 并发控制（Semaphore + QPS limiter）
3. 抖动 + 指数退避 + 全局冷却（抗风控）
4. 百度 302 跳转解析（可关闭）
5. URL 去重、噪声过滤、snip提取与清洗
6. 摘要提取与清洗

TODO：
1. 网页正文抓取（进入真实 URL 抓取 HTML，主内容提取）
2. 查询结果缓存（query 级 / url 级，支持 TTL）
3. 关键片段摘取（BM25 / 语义重排）
"""

import re
import json
import asyncio
import logging
import random
import time
from enum import Enum
from urllib.parse import urlparse

import httpx
from aiolimiter import AsyncLimiter
from bs4 import BeautifulSoup

from baidu_search.cache import async_cache, get_search_cache, get_url_cache

logger = logging.getLogger(__name__)

NOISE_PATTERNS = (
    r"高清视频|在线观看|实时回复|精选笔记|"
    r"点击(查看|咨询)|立即(购买|咨询)|"
    r"厂家直销|源头厂家|爱采购"
)
BANED_SITES = [
    "taobao.com",
    "tmall.com",
    "jd.com",
    "pinduoduo.com",
    "1688.com"
]


class UrlResolveStatus(str, Enum):
    SKIPPED = "skipped"      # 不需要解析
    RESOLVED = "resolved"    # 成功拿到 Location
    FAILED = "failed"        # 尝试了但失败


class ContentFilter:
    def __init__(self, banned_sites=None, noise_patterns=None):
        self.banned_sites = banned_sites or BANED_SITES
        self.re_noise = re.compile(noise_patterns) if noise_patterns else re.compile(NOISE_PATTERNS)

    def _is_banned_site(self, url: str) -> bool:
        netloc = urlparse(url).netloc
        return any(site in netloc for site in self.banned_sites)

    def filter_results(self, results: list[dict], limit: int) -> list[dict]:
        """Filter search results by URL validity, duplicates, banned sites, and noise."""
        res = []
        seen_urls = set()

        for result in results:
            url = result.get("url") or ""
            title = result.get("title", "")
            abstract = result.get("abstract", "")

            # 合并所有跳过条件
            if (
                not url.startswith("http") or
                url in seen_urls or
                self._is_banned_site(url) or
                (self.re_noise and (self.re_noise.search(title) or self.re_noise.search(abstract)))
            ):
                continue

            seen_urls.add(url)
            result["rank"] = len(res) + 1
            res.append(result)

            if len(res) >= limit:
                break

        return res


# ── 默认并发配置 ──────────────────────────────────────────
# 可通过 config["concurrency"] 覆盖，方便调试
DEFAULT_CONCURRENCY = {
    # 百度搜索页
    "search_sem": 2,          # 同时最多几个搜索页在飞
    "search_qps": 0.5,          # 每秒最多发几个搜索页请求
    "search_jitter": (0.05, 0.15),  # 搜索页请求前的随机抖动(秒)
    # link 解析 (302)：轻量 HEAD
    "resolve_sem": 15,        # 同时最多几个解析在飞 【速度瓶颈在url解析这】
    "resolve_qps": 10,        # 每秒最多发几个解析请求 resolve_qps = min(10, search_qps * 10)
    "resolve_jitter": (0.02, 0.08), # URL 解析请求前的随机抖动(秒)
    # 重试（仅搜索页）
    "max_retries": 2,
    "retry_backoff": 3.0,
    "resolve_real_url": True,
    # "resolve_real_url": False,
}


class BaiduSearch:
    """百度搜索 + sem/qps 保护。"""
    # 固定 headers，跟 core.py 保持一致（同连接内 UA 不变，更像真实浏览器）
    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;"
                  "q=0.9,image/webp,*/*;q=0.8",
        "Referer": "https://www.baidu.com/",
    }

    def __init__(self, config: dict = None) -> None:
        self.url = "https://www.baidu.com/s"
        config = config or {}
        search_banned_sites = config.get("search_banned_sites", [])
        search_noise_patterns = config.get("search_noise_patterns", "")
        self.content_filter = ContentFilter(search_banned_sites, search_noise_patterns)
        self.max_results = config.get("max_results", 100)

        # ── 并发参数（可通过 config["concurrency"] 覆盖） ──
        cc = {**DEFAULT_CONCURRENCY, **config.get("concurrency", {})}
        self._cc = cc
        # 搜索页并发控制
        self._search_sem = asyncio.Semaphore(cc["search_sem"])
        self._search_qps = self._make_limiter(cc["search_qps"])
        # link 解析并发控制
        self._resolve_sem = asyncio.Semaphore(cc["resolve_sem"])
        self._resolve_qps = self._make_limiter(cc["resolve_qps"])
        self._cooldown_until = 0
        # 是否解析真实url
        self.resolve_real_url = cc.get("resolve_real_url", True)

    @staticmethod
    def _make_limiter(qps: float) -> AsyncLimiter:
        """构造 AsyncLimiter，确保 max_rate >= 1 以避免 acquire 报错。
        例如 qps=0.33 → AsyncLimiter(1, 1/0.33≈3.03)，即 3 秒 1 次。
        """
        if qps >= 1:
            return AsyncLimiter(qps, 1)
        else:
            # 反转：1 次 / (1/qps) 秒
            return AsyncLimiter(1, 1.0 / qps)

    async def search(self, query: str, offset: int = 0, limit: int = 10) -> str:
        """搜索百度并返回结果（支持分页）

        Args:
            query: 搜索关键词
            offset: 偏移量，从第几条开始返回（基于过滤后的结果）
            limit: 返回结果数量

        Returns:
            JSON 字符串
        """
        # ⭐ 直接查询原始数据，不做预估
        res = await self.search_baidu(query, offset=offset, limit=limit)

        # ⭐ 统一过滤
        if self.content_filter:
            filtered = []
            seen_urls = set()
            for item in res["data"]:
                url = item.get("url") or ""
                title = item.get("title", "")
                abstract = item.get("abstract", "")

                # 跳过条件
                if (
                    not url.startswith("http") or
                    url in seen_urls or
                    self.content_filter._is_banned_site(url) or
                    (self.content_filter.re_noise and
                     (self.content_filter.re_noise.search(title) or
                      self.content_filter.re_noise.search(abstract)))
                ):
                    continue

                seen_urls.add(url)
                item.pop("url_status", None)
                filtered.append(item)

            results = filtered
        else:
            for r in res["data"]:
                r.pop("url_status", None)
            results = res["data"]

        # ⭐ 统一编号（业务层排序）
        for idx, item in enumerate(results, start=1):
            item["rank"] = idx

        return json.dumps(results, ensure_ascii=False)


    async def search_baidu(self, query: str, offset: int = 0, limit: int = 10):
        """百度搜索主流程（支持分页）。

        ⭐ Segment Cache 策略（行业标准）：
        - 按页缓存原始数据：search:{query}:page:{page_idx}
        - 每页固定 10 条（百度搜索引擎原生结构）
        - 缓存未过滤的原始数据，保证每页完整性
        - 过滤逻辑在上层 search() 统一执行

        示例：
        - offset=0, limit=8   → 查询 page:0，缓存 10 条原始数据，返回 [0:8]
        - offset=5, limit=8   → 命中 page:0，查询 page:1，返回 [5:13]
        - offset=50, limit=5  → 命中 page:5，返回 [50:55]

        Args:
            query: 搜索关键词
            offset: 偏移量
            limit: 返回结果数量

        Returns:
            {"data": [...]}  # 原始数据，未过滤
        """
        PAGE_SIZE = 10  # 百度每页固定 10 条

        # 计算需要的页范围
        start_page = offset // PAGE_SIZE
        end_page = (offset + limit - 1) // PAGE_SIZE
        pages_needed = list(range(start_page, end_page + 1))

        # ── 尝试从缓存中获取各页数据 ──
        _search_cache = get_search_cache()
        all_results = []
        pages_to_fetch = []

        for page_idx in pages_needed:
            cache_key = f"search:{query}:page:{page_idx}"
            cached = await _search_cache.get(cache_key)
            if cached is not None:
                logger.info(f"[cache hit] page={page_idx}")
                all_results.extend(cached)
            else:
                pages_to_fetch.append(page_idx)

        # ── 查询缺失的页 ──
        if pages_to_fetch:
            t0 = time.time()
            async with httpx.AsyncClient(headers=self._HEADERS, http2=True) as client:
                t1 = time.time()
                logger.info(f"[计时] 初始化 {t1-t0:.2f}s")

                # 并发抓取缺失的页
                new_results = await self._fetch_pages_concurrent(client, query, pages_to_fetch)
                t2 = time.time()
                logger.info(f"[计时] 搜索页 {len(pages_to_fetch)} 页 → {len(new_results)} 条，{t2-t1:.2f}s")

                # URL 解析
                if self.resolve_real_url:
                    await self._resolve_urls_concurrent(client, new_results)
                    t3 = time.time()
                    logger.info(f"[计时] URL 解析 {len(new_results)} 条，{t3-t2:.2f}s")
                else:
                    for item in new_results:
                        item["url_status"] = UrlResolveStatus.SKIPPED.value
                    t3 = time.time()

                logger.info(f"[计时] 总耗时 {t3-t0:.2f}s，获取 {len(new_results)} 条")

                # ⭐ 按页缓存原始数据（未过滤，保证每页完整 10 条）
                # 简化：按查询顺序分配到各页
                for idx, page_idx in enumerate(pages_to_fetch):
                    start_idx = idx * PAGE_SIZE
                    end_idx = start_idx + PAGE_SIZE
                    page_data = new_results[start_idx:end_idx]

                    if page_data:
                        cache_key = f"search:{query}:page:{page_idx}"
                        await _search_cache.set(cache_key, page_data)
                        logger.info(f"[cache set] page={page_idx}, {len(page_data)} 条（原始数据）")

                all_results.extend(new_results)

        # ── 按 rank 排序并切片返回 ──
        all_results.sort(key=lambda x: x.get("rank", 0))

        # ⭐ 修复：使用相对偏移而不是取模
        # all_results 只包含 pages_needed 的数据，需要计算相对偏移
        start_idx = offset - start_page * PAGE_SIZE
        end_idx = start_idx + limit
        data = all_results[start_idx:end_idx]

        return {"data": data}

    # ── 搜索页：并发抓取 ─────────────────────────────────────
    async def _fetch_pages_concurrent(self, client, query, pages_needed):
        """所有页 gather 并发，由 sem + qps 自动控制节奏。

        Args:
            pages_needed: 需要抓取的页索引列表，例如 [0, 1, 2] 或 [5, 6]
        """
        tasks = [
            asyncio.create_task(self._fetch_page_throttled(client, query, page_idx))
            for page_idx in pages_needed
        ]
        pages = await asyncio.gather(*tasks)
        # 合并结果，跳过被拦截的页（None）
        results = []
        for page in pages:
            if page is not None:
                results.extend(page)
        return results

    async def _fetch_page_throttled(self, client, query, page_idx):
        """单页请求：抖动 + sem + qps 限速，被拦截时 backoff 重试。"""
        max_retries = self._cc["max_retries"]
        backoff = self._cc["retry_backoff"]
        jitter = self._cc["search_jitter"]

        for attempt in range(1 + max_retries):

            # 👇 每次尝试前检查冷却
            now = time.time()
            wait = max(0, self._cooldown_until - now)
            if wait > 0:
                logger.warning(f"全局冷却中，等待 {wait:.1f}s")
                await asyncio.sleep(wait)

            # 抖动：让同批 task 错开到达
            await asyncio.sleep(random.uniform(*jitter))
            async with self._search_qps:
                async with self._search_sem:
                    data = await self.fetch_page(client, query, page_idx)
            if data is not None:
                return data
            # 被拦截，backoff 重试
            if attempt < max_retries:
                # wait = backoff * (attempt + 1) # 线性退避
                wait = backoff * (2 ** attempt) # 指数退避
                logger.warning(
                    f"搜索页 {page_idx} 被拦截，{wait:.1f}s 后重试 "
                    f"({attempt+1}/{max_retries})"
                )
                await asyncio.sleep(wait)
        return None  # 重试耗尽

    # ── link 解析：并发解析 ──────────────────────────────────
    async def _resolve_urls_concurrent(self, client, results):
        """所有 URL gather 并发，由 sem + qps 自动控制节奏。"""
        tasks = [
            asyncio.create_task(self._resolve_one(client, item))
            for item in results
        ]
        if tasks:
            await asyncio.gather(*tasks)

    async def _resolve_one(self, client, item):
        """单条 URL 解析：抖动 + sem + qps 限速，不重试。"""
        jitter = self._cc["resolve_jitter"]
        # 抖动：让同批 task 错开到达
        await asyncio.sleep(random.uniform(*jitter))
        async with self._resolve_qps:
            async with self._resolve_sem:
                url, status = await self.get_real_url(client, item["url"])
        item["url"] = url
        item["url_status"] = status.value


    async def get_real_url(self, client, url):
        """解析百度跳转链接，获取真实 URL（纯逻辑，不含限速）。
        URL 级缓存：同一个 302 链接只解析一次，24h 有效。
        """
        if not url:
            return url, UrlResolveStatus.SKIPPED

        # 非跳转 URL 直接跳过
        if not ("link?url=" in url or "baidu.php" in url):
            return url, UrlResolveStatus.SKIPPED

        # ── URL 级缓存 ──
        _url_cache = get_url_cache()
        cached = await _url_cache.get(url)
        if cached is not None:
            logger.debug(f"[cache hit] url resolve: {url[:80]}")
            return cached["url"], UrlResolveStatus(cached["status"])

        try:
            resp = await client.head(
                url, follow_redirects=False, timeout=2.0,
            )
            location = resp.headers.get("Location")
            if location:
                await _url_cache.set(url, {"url": location, "status": UrlResolveStatus.RESOLVED.value})
                return location, UrlResolveStatus.RESOLVED
            return url, UrlResolveStatus.FAILED
        except Exception as e:
            logger.exception(f"fetch_page 异常: {e}")
            return url, UrlResolveStatus.FAILED

    def clean_abstract(self, text):
        """清洗乱码和冗余换行"""
        if not text: return ""
        
        # 1. 去掉特殊的编码字符（如 \ue680, \ue67d 等百度图标字体）
        text = re.sub(r'[\ue600-\ue6ff]', '', text)
        
        # 2. 将多个换行符、制表符统一替换为单个空格，保持结构紧凑
        text = re.sub(r'[\n\t\r]+', ' ', text)
        
        # 3. 去掉纯粹的交互词噪声（如“播报”、“暂停”、“点击查看”）
        noise = ["播报", "暂停", "查看更多", "展开全部"]
        for n in noise:
            text = text.replace(n, "")
        
        # 4. 去除首尾及中间多余空格
        text = re.sub(r'\s+', ' ', text).strip()
        return text


    def extract_abstract(self, container):
        """从容器中提取摘要文本块"""
        # 尝试百度最常用的几个内容类名
        selectors = [".c-abstract", ".content-right_8Zs4j", ".content-abstract", ".op-se-share-content",".c-span-last"]

        for s in selectors:
            node = container.select_one(s)
            if node: return node.get_text()

        # 兜底：如果找不到指定类，就找包含文本最多的子块
        child_nodes = container.find_all(["div", "span"])
        if child_nodes:
            # 过滤掉字数太少的（比如只有“广告”两个字的）
            texts = [t.get_text().strip() for t in child_nodes if len(t.get_text().strip()) > 20]
            if texts:
                return max(texts, key=len)
        return ""

    async def fetch_page(self, client, keyword, page_idx):
        """单页请求（纯逻辑，不含限速）。返回 None 表示被拦截，[] 表示解析异常。"""

        params =    {
            "wd": keyword,
            "pn": page_idx * 10,
            "ie": "utf-8",
        }
        try:
            resp = await client.get(self.url, params=params, timeout=5.0)

            # 检测验证码拦截 → 返回 None 触发上层重试
            if "百度安全验证" in resp.text:
                logger.warning(f"触发百度安全验证，第 {page_idx} 页")

                # 设置全局冷却 30 秒
                self._cooldown_until = time.time() + 30
                return None

            soup = BeautifulSoup(resp.text, "lxml")
            containers = soup.select(".c-container")
            
            page_items = []
            for container in containers:
                title_node = container.select_one("h3") or container.select_one(".t")
                if not title_node: continue
                
                title = title_node.get_text(strip=True)
                raw_url = title_node.find("a")["href"] if title_node.find("a") else ""
                
                # 提取并清洗摘要
                raw_abstract = self.extract_abstract(container)
                clean_abs = self.clean_abstract(raw_abstract)

                page_items.append({
                    "title": title,
                    "abstract": clean_abs,
                    "url": raw_url
                })
            return page_items
        except Exception as e:
            logger.exception(f"fetch_page 异常: {e}")
            return []
    


async def main():

    config = {
        "search_noise_patterns": NOISE_PATTERNS,
        "search_banned_sites": BANED_SITES,
        "concurrency": {
            # 搜索页
            "search_sem": 2,
            "search_qps": 0.5,
            "search_jitter": (0.05, 0.15),

            # URL 解析
            "resolve_sem": 15,
            "resolve_qps": 10,
            "resolve_jitter": (0.02, 0.08),

            # 重试
            "max_retries": 2,
            "retry_backoff": 3.0,

            # 是否解析真实 URL
            "resolve_real_url": True,
        }
    }
    searcher = BaiduSearch(config)
    keyword = "强化学习"
    print(f"开始抓取关键词: {keyword} ...")

    # 测试分页功能
    print("\n=== 第一次查询：offset=0, limit=10（会查询 page:0 和 page:1）===")
    t0 = time.time()
    results = await searcher.search(keyword, offset=0, limit=10)
    print("results",results)
    t1 = time.time()
    print(f"耗时: {t1-t0:.3f}s")
    print(f"返回 {len(json.loads(results))} 条结果")

    print("\n=== 第二次查询：offset=0, limit=5（应该命中缓存 page:0）===")
    t0 = time.time()
    results = await searcher.search(keyword, offset=0, limit=5)
    t1 = time.time()
    print(f"耗时: {t1-t0:.3f}s（应该 < 0.1s）")
    print(f"返回 {len(json.loads(results))} 条结果")

    print("\n=== 第三次查询：offset=5, limit=5（应该命中缓存 page:0）===")
    t0 = time.time()
    results = await searcher.search(keyword, offset=5, limit=5)
    t1 = time.time()
    print(f"耗时: {t1-t0:.3f}s（应该 < 0.1s）")
    print(f"返回 {len(json.loads(results))} 条结果")

    print("\n=== 第四次查询：offset=10, limit=5（应该命中缓存 page:1）===")
    t0 = time.time()
    results = await searcher.search(keyword, offset=10, limit=5)
    t1 = time.time()
    print(f"耗时: {t1-t0:.3f}s（应该 < 0.1s）")
    print(f"返回 {len(json.loads(results))} 条结果")



if __name__ == "__main__":
    asyncio.run(main())
   