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
import asyncio
import logging
import random
import time
from enum import Enum
from urllib.parse import urlparse

import httpx
from aiolimiter import AsyncLimiter
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

NOISE_PATTERNS  = r"高清视频|在线观看|实时回复|精选笔记|淘宝"
BANED_SITES = ["www.taobao.com"]

class UrlResolveStatus(str, Enum):
    SKIPPED = "skipped"      # 不需要解析
    RESOLVED = "resolved"    # 成功拿到 Location
    FAILED = "failed"        # 尝试了但失败


class ContentFilter:
    def __init__(self, banned_sites=None, noise_patterns=None):
        self.banned_sites = banned_sites or []
        self.re_noise = re.compile(noise_patterns) if noise_patterns else None

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

    async def search(self, query: str, num_results: int = 5) -> str:
        """搜索百度并返回结果"""
        res = await self.search_baidu(query, num_results=num_results)

        # filter
        if self.content_filter:
            results = self.content_filter.filter_results(res["data"], num_results)
        else:
            results = res["data"][:num_results]

        # format
        formatted_results = []
        for i, r in enumerate(results, 1):
            formatted_results.append(f"{i}. {r['title']} ({r['url']})")
            if "abstract" in r:
                formatted_results[-1] += f"\nAbstract: {r['abstract']}"

        msg = "\n".join(formatted_results)
        return msg


    async def search_baidu(self, query, num_results=10):
        """百度搜索主流程。
        思路：sem + qps 两层控制即可，低频调用零等待，高频自动排队。
        """
        pages_needed = (num_results + 9) // 10
        t0 = time.time()

        # http2=True + 固定 headers 在 client 级别
        async with httpx.AsyncClient(headers=self._HEADERS, http2=True) as client:
            t1 = time.time()
            logger.info(f"[计时] 初始化 {t1-t0:.2f}s")

            # ② 搜索页：gather 并发，sem + qps 自动限速
            results = await self._fetch_pages_concurrent(client, query, pages_needed)
            t2 = time.time()
            logger.info(f"[计时] 搜索页 {pages_needed} 页 → {len(results)} 条，{t2-t1:.2f}s")

            # ③ link 解析：gather 并发，sem + qps 自动限速
            if self.resolve_real_url:
                await self._resolve_urls_concurrent(client, results)
                t3 = time.time()
                logger.info(f"[计时] URL 解析 {len(results)} 条，{t3-t2:.2f}s")
            else:
                # 标记为跳过解析
                for item in results:
                    item["url_status"] = UrlResolveStatus.SKIPPED.value
                t3 = time.time()
                logger.info(f"[计时] URL 解析已关闭")

            # ④ 降级保留
            cleaned = [
                item for item in results
                if item.get("url_status") != UrlResolveStatus.FAILED.value
                or item.get("url", "").startswith("http")
            ]
            if not cleaned:
                logger.warning(f"URL 全部解析失败: {query}，返回原始结果")
                cleaned = results

            logger.info(f"[计时] 总耗时 {t3-t0:.2f}s，返回 {len(cleaned)} 条")
            return {"data": cleaned}

    # ── 搜索页：并发抓取 ─────────────────────────────────────
    async def _fetch_pages_concurrent(self, client, query, pages_needed):
        """所有页 gather 并发，由 sem + qps 自动控制节奏。"""
        tasks = [
            asyncio.create_task(self._fetch_page_throttled(client, query, i))
            for i in range(pages_needed)
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
        """解析百度跳转链接，获取真实 URL（纯逻辑，不含限速）。"""
        if not url:
            return url, UrlResolveStatus.SKIPPED

        # 非跳转 URL 直接跳过
        if not ("link?url=" in url or "baidu.php" in url):
            return url, UrlResolveStatus.SKIPPED

        try:
            resp = await client.head(
                url, follow_redirects=False, timeout=2.0,
            )
            location = resp.headers.get("Location")
            if location:
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
            for i, container in enumerate(containers):
                title_node = container.select_one("h3") or container.select_one(".t")
                if not title_node: continue
                
                title = title_node.get_text(strip=True)
                raw_url = title_node.find("a")["href"] if title_node.find("a") else ""
                
                # 提取并清洗摘要
                raw_abstract = self.extract_abstract(container)
                clean_abs = self.clean_abstract(raw_abstract)

                page_items.append({
                    "rank": page_idx * 10 + i + 1,
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
    # results = await searcher.search(keyword, num_results=10)
    # print(results)
    
    results = await searcher.search_baidu(keyword, num_results=10)
    for item in results["data"]:
        print(f"[{item['rank']}] {item['title']}")
        print(f"来源/地址: {item['url']}")
        print(f"内容摘要: {item['abstract']}")
        print("-" * 40)



if __name__ == "__main__":
    asyncio.run(main())
   