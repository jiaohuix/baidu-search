"""
ContextCompressor - 基于 BM25 的上下文压缩器

将爬取的长网页文本，根据 query 相关性压缩到指定字符数内。

两种分句模式：
- "simple": 按 。！？.!? 切分，粒度小，干净，适合网页正文
- "jina":   jina chunker 语义分块，粒度大，保留结构，适合 markdown

用法：
    comp = ContextCompressor(max_chars=2000, splitter="simple")
    result = comp.compress("鱼刺卡喉咙怎么办", page_text)
"""

import re
from functools import lru_cache
from typing import Literal

import jieba
from rank_bm25 import BM25Okapi

from baidu_search.jina_chunker import chunk_text_simple

# jieba 初始化：预加载词典 + 并行分词
jieba.setLogLevel(jieba.logging.WARNING)
jieba.initialize()
jieba.enable_parallel(4)

# 简单正则分句：按中英文句末标点切分，保留标点
_SIMPLE_SPLIT_RE = re.compile(r'(?<=[。！？.!?\n])')

# 网页噪声模式
_NOISE_RE = re.compile(
    r"大家还在搜|相关搜索|为你推荐|猜你喜欢|"
    r"关注微信|扫描关注|下载APP|"
    r"正在加载|点击查看|展开全部|"
    r"版权所有|备案号|ICP备",
)


@lru_cache(maxsize=4096)
def _tokenize(text: str) -> tuple[str, ...]:
    """jieba 分词，过滤空白 token。返回 tuple 以支持 lru_cache。"""
    return tuple(w for w in jieba.cut(text) if w.strip())


def _is_noise(text: str) -> bool:
    """判断句子是否为网页噪声"""
    return bool(_NOISE_RE.search(text))


def _split_simple(text: str) -> list[str]:
    """简单正则分句，按句末标点切分，保留标点在句尾。"""
    return [s for s in _SIMPLE_SPLIT_RE.split(text) if s.strip()]


def _split_jina(text: str) -> list[str]:
    """jina chunker 分句。"""
    return [c for c in chunk_text_simple(text) if c.strip()]


@lru_cache(maxsize=512)
def _split_and_filter(
    text: str, min_len: int, splitter: str,
) -> tuple[str, ...]:
    """分句 + 过滤噪声和碎片，带缓存。"""
    print("splitter",splitter)
    chunks = _split_simple(text) if splitter == "simple" else _split_jina(text)
    return tuple(
        c for c in chunks
        if len(c.strip()) >= min_len and not _is_noise(c)
    )


class ContextCompressor:
    """基于 BM25 的上下文压缩器。

    Args:
        max_chars: 压缩后最大字符数，默认 2000
        max_input_chars: 输入文本截断长度，防止超长文本，默认 50000
        min_sentence_len: 最短句子长度，过滤碎片，默认 10
        splitter: 分句模式，"simple"(默认) 或 "jina"
    """

    def __init__(
        self,
        max_chars: int = 2000,
        max_input_chars: int = 50000,
        min_sentence_len: int = 10,
        splitter: Literal["simple", "jina"] = "simple",
    ) -> None:
        self.max_chars = max_chars
        self.max_input_chars = max_input_chars
        self.min_sentence_len = min_sentence_len
        self.splitter = splitter

    def compress(self, query: str, context: str) -> str:
        """压缩上下文，返回与 query 最相关的文本片段。

        Args:
            query: 搜索查询词（可以是 title + abstract 拼接）
            context: 爬取的网页全文

        Returns:
            压缩后的文本，长度不超过 max_chars
        """
        if not context or not query:
            return context or ""

        context = context[:self.max_input_chars]

        if len(context) <= self.max_chars:
            return context

        # 1. 分句 + 过滤（带缓存）
        sentences = list(
            _split_and_filter(context, self.min_sentence_len, self.splitter)
        )
        if not sentences:
            return context[:self.max_chars]

        # 2. BM25 打分
        scores = self._bm25_score(query, sentences)

        # 3. 按分数排序，贪心选句直到达到 max_chars
        sorted_indices = sorted(
            range(len(scores)), key=lambda i: scores[i], reverse=True,
        )

        selected = []
        total_chars = 0
        for idx in sorted_indices:
            sent_len = len(sentences[idx])
            if total_chars + sent_len > self.max_chars and selected:
                break
            selected.append(idx)
            total_chars += sent_len

        # 4. 恢复原文顺序，拼接
        selected.sort()
        return "".join(sentences[i] for i in selected)

    def _bm25_score(self, query: str, sentences: list[str]) -> list[float]:
        """计算 query 与每个句子的 BM25 分数"""
        tokenized_corpus = [list(_tokenize(s)) for s in sentences]
        tokenized_query = list(_tokenize(query))

        if not tokenized_corpus or not tokenized_query:
            return [0.0] * len(sentences)

        bm25 = BM25Okapi(tokenized_corpus)
        return bm25.get_scores(tokenized_query).tolist()

