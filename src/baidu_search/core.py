'''
@date: 2026/02/11
@author: jiaohuix
@description: baidu搜索
'''
import re
import asyncio
import logging
from enum import Enum
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

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


class BaiduSearch:
    """Baidu Search."""
    def __init__(self, config: dict = None) -> None:
        self.url = "https://www.baidu.com/s"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Referer": "https://www.baidu.com/"
        }
        config = config or {}
        search_banned_sites = config.get("search_banned_sites", [])
        search_noise_patterns = config.get("search_noise_patterns", "")
        self.content_filter = ContentFilter(search_banned_sites, search_noise_patterns)
        self.max_results = config.get("max_results", 100)


    async def search(self, query: str, num_results: int = 5) -> str:
        """standard search interface."""
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
        
        async with httpx.AsyncClient(headers=self.headers, http2=True) as client:
            # 第一阶段：并发请求所有列表页
            fetch_tasks = [self.fetch_page(client, query, i) for i in range(pages_needed)]
            pages_data = await asyncio.gather(*fetch_tasks)
            
            results = [item for page in pages_data for item in page]
            
            # 第二阶段：并发获取所有真实地址
            url_tasks = [self.get_real_url(client, item["url"]) for item in results]
            real_urls = await asyncio.gather(*url_tasks)

            results_cleaned = []
            for item, (url, status) in zip(results, real_urls):
                item["url"] = url
                item["url_status"] = status.value

                if status == UrlResolveStatus.FAILED:
                    continue
                results_cleaned.append(item)

            if len(results_cleaned) == 0:
                logger.warning(f"No results found from Baidu search: {query}")

            return {"data": results_cleaned}


    async def get_real_url(self, client, url):
        if not url:
            return url, UrlResolveStatus.SKIPPED

        # 只跳过非跳转 URL
        if not ("link?url=" in url or "baidu.php" in url):
            return url, UrlResolveStatus.SKIPPED

        try:
            resp = await client.head(url, follow_redirects=False, timeout=2.0)
            location = resp.headers.get("Location")
            if location:
                return location, UrlResolveStatus.RESOLVED
            else:
                return url, UrlResolveStatus.FAILED
        except Exception:
            return url, UrlResolveStatus.FAILED

    def clean_abstract(self, text):
        """精准清洗：只去掉乱码和冗余换行，保留职称、时间、医院等有效信息"""
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
            resp = await client.get(url, timeout=5.0)
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

