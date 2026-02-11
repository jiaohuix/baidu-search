'''
@date: 2026/02/11
@author: jiaohuix
@description: baidu搜索
'''

import re
import asyncio
import logging
import random
from enum import Enum
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
# from agno.tools import tool

logger = logging.getLogger(__name__)

NOISE_PATTERNS  = r"高清视频|在线观看|实时回复|淘宝"
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


class BaiduSearchPro:
    """Baidu Search."""
    _UA_LIST = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    ]

    def __init__(self, config: dict = None) -> None:
        self.url = "https://www.baidu.com/s"
        config = config or {}
        search_banned_sites = config.get("search_banned_sites", [])
        search_noise_patterns = config.get("search_noise_patterns", "")
        self.content_filter = ContentFilter(search_banned_sites, search_noise_patterns)
        self.max_results = config.get("max_results", 100)

    def _random_headers(self):
        return {
            "User-Agent": random.choice(self._UA_LIST),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.baidu.com/",
            "Connection": "keep-alive",
        }

    # @tool
    async def search(self, query: str, num_results: int = 5) -> str:
        """搜索百度并返回结果"""
        res = await self.search_baidu(query, num_results = min(2*num_results,  self.max_results))

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
                # formatted_results[-1] += f"\nAbstract: {r['abstract']}\n{r['url_status']}"

        msg = "\n".join(formatted_results)
        return msg


    async def search_baidu(self, query, num_results=10):
        """Search Baidu using web scraping to retrieve relevant search results.

        - WARNING: Uses web scraping which may be subject to rate limiting or anti-bot measures.

        Returns:
            Example result:
            {
                'rank': 1,
                'title': '百度百科',
                'abstract': '百度百科是一部内容开放、自由的网络百科全书...',
                'url': 'https://baike.baidu.com/',
                'url_status': 'resolved'
            }
            
        """
        # 计算需要抓取的页数 (每页10条)
        pages_needed = (num_results // 10) + (1 if num_results % 10 != 0 else 0)

        # 关闭 http2 减少 TLS 指纹特征；不在 client 级别设 headers，每次请求单独带
        async with httpx.AsyncClient(http2=False) as client:
            # ① 预热：先访问首页拿 BAIDUID cookie，模拟真实浏览器行为
            try:
                await client.get("https://www.baidu.com/", headers=self._random_headers(), timeout=5.0)
            except Exception:
                pass

            # ② 串行翻页，模拟人类逐页浏览（不再 gather 并发）
            results = []
            for i in range(pages_needed):
                if i > 0:
                    await asyncio.sleep(random.uniform(1.5, 3.5))
                page_data = await self.fetch_page(client, query, i)
                if not page_data:
                    break  # 被拦截或无结果，停止翻页
                results.extend(page_data)

            # ③ 串行解析真实 URL，每个之间加微小间隔
            for item in results:
                url, status = await self.get_real_url(client, item["url"])
                item["url"] = url
                item["url_status"] = status.value

            # ④ 安全降级：URL 解析失败时，只要原始链接可用就保留
            results_cleaned = [
                item for item in results
                if item["url_status"] != UrlResolveStatus.FAILED.value
                or item["url"].startswith("http")
            ]

            if not results_cleaned:
                # 兜底：如果全部 FAILED，返回原始结果而非空列表
                logger.warning(f"All URL resolves failed for: {query}, returning raw results")
                results_cleaned = results

            return {"data": results_cleaned}


    async def get_real_url(self, client, url):
        if not url:
            return url, UrlResolveStatus.SKIPPED

        # 只跳过非跳转 URL
        if not ("link?url=" in url or "baidu.php" in url):
            return url, UrlResolveStatus.SKIPPED

        try:
            await asyncio.sleep(random.uniform(0.1, 0.3))
            resp = await client.head(url, headers=self._random_headers(), follow_redirects=False, timeout=3.0)
            location = resp.headers.get("Location")
            if location:
                return location, UrlResolveStatus.RESOLVED
            else:
                return url, UrlResolveStatus.FAILED
        except Exception:
            return url, UrlResolveStatus.FAILED

    def clean_abstract(self, text):
        """精准清洗：只去掉乱码和冗余换行"""
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
        """单页请求任务"""
        url = f"https://www.baidu.com/s?wd={keyword}&pn={page_idx * 10}&ie=utf-8"
        try:
            resp = await client.get(url, headers=self._random_headers(), timeout=10.0)

            # 检测验证码拦截
            if "百度安全验证" in resp.text:
                logger.warning(f"触发百度安全验证，第 {page_idx} 页抓取中止")
                return []

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
        except:
            return []


async def main():

    config = {
        "search_noise_patterns": NOISE_PATTERNS,
        "search_banned_sites": BANED_SITES
    }
    searcher = BaiduSearchPro(config)
    keyword = "强化学习"
    print(f"开始抓取关键词: {keyword} ...")
    results = await searcher.search(keyword, num_results=5)
    print(results)
    
    # results = await searcher.search_baidu(keyword, num_results=40)
    # for item in results["data"]:
    #     # print(item)
    #     print(f"[{item['rank']}] {item['title']}")
    #     print(f"来源/地址: {item['url']}")
    #     print(f"内容摘要: {item['abstract']}")
    #     print("-" * 40)



# 测试
if __name__ == "__main__":
    asyncio.run(main())
    