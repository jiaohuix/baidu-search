'''
Jina分块器 - 基于官方正则表达式的智能文本分块工具

功能描述：
1. 使用Jina官方正则表达式对文本进行智能分块
2. 支持多种文本格式：标题、列表、代码块、表格、引用等
3. 基于官方源码参数配置，确保分块效果与Jina一致
4. 提供详细的分块信息：文本内容、位置、长度等

参考链接：
- Jina官方文档：https://jina.ai/tokenizer
- 知乎文章：https://zhuanlan.zhihu.com/p/716391771
- CSDN文章：https://blog.csdn.net/problc/article/details/141284546
'''
import re
import regex
from typing import List, Dict, Any

# ==================== 常量定义 ====================
# 基于Jina官方源码 (Updated: Aug. 15, 2024)
# 这些常量控制分块算法的各种参数，确保分块效果与Jina官方一致

# 标题相关参数
MAX_HEADING_LENGTH = 7                    # 最大标题长度（#号数量）
MAX_HEADING_CONTENT_LENGTH = 200          # 最大标题内容长度
MAX_HEADING_UNDERLINE_LENGTH = 200        # 最大标题下划线长度
MAX_HTML_HEADING_ATTRIBUTES_LENGTH = 100  # 最大HTML标题属性长度

# 独立行和列表相关参数
MAX_STANDALONE_LINE_LENGTH = 800          # 最大独立行长度
MAX_LIST_ITEM_LENGTH = 200                # 最大列表项长度
MAX_NESTED_LIST_ITEMS = 6                 # 最大嵌套列表项数量
MAX_LIST_INDENT_SPACES = 7                # 最大列表缩进空格数

# 引用块相关参数
MAX_BLOCKQUOTE_LINE_LENGTH = 200          # 最大引用行长度
MAX_BLOCKQUOTE_LINES = 15                 # 最大引用行数

# 代码块相关参数
MAX_CODE_LANGUAGE_LENGTH = 20             # 最大代码语言长度
MAX_CODE_BLOCK_LENGTH = 1500              # 最大代码块长度
MAX_INDENTED_CODE_LINES = 20              # 最大缩进代码行数

# 表格相关参数
MAX_TABLE_CELL_LENGTH = 200               # 最大表格单元格长度
MAX_TABLE_ROWS = 20                       # 最大表格行数
MAX_HTML_TABLE_LENGTH = 2000              # 最大HTML表格长度

# 其他格式参数
MIN_HORIZONTAL_RULE_LENGTH = 3            # 最小水平线长度
MAX_HTML_TAG_ATTRIBUTES_LENGTH = 100      # 最大HTML标签属性长度
MAX_HTML_TAG_CONTENT_LENGTH = 1000        # 最大HTML标签内容长度

# 文本内容参数（调小以获得更细粒度的分块，适合 BM25 句级打分）
MAX_SENTENCE_LENGTH = 150                 # 最大句子长度（官方400，调小到150）
MAX_QUOTED_TEXT_LENGTH = 300              # 最大引用文本长度
MAX_PARENTHETICAL_CONTENT_LENGTH = 200    # 最大括号内容长度
MAX_NESTED_PARENTHESES = 5                # 最大嵌套括号层数

# 数学公式参数
MAX_MATH_INLINE_LENGTH = 100              # 最大行内数学公式长度
MAX_MATH_BLOCK_LENGTH = 500               # 最大数学块长度

# 段落和查找参数（调小以获得更细粒度的分块）
MAX_PARAGRAPH_LENGTH = 400                # 最大段落长度（官方1000，调小到400）
MAX_STANDALONE_LINE_LENGTH = 300          # 最大独立行长度（官方800，调小到300）
LOOKAHEAD_RANGE = 50                      # 向前查找范围（官方100，调小到50）


# ==================== 核心函数 ====================

def create_complete_chunk_regex():
    """
    创建完整的Jina分块正则表达式
    
    功能：
    - 基于Jina官方正则表达式模式
    - 支持多种文本格式的智能分块
    - 包括标题、列表、代码块、表格、引用等
    
    Returns:
        regex.Pattern: 编译后的正则表达式对象
    """
    return regex.compile(
        rf"("
        # 1. Headings (Setext-style, Markdown, and HTML-style, with length constraints)
        rf"(?:^(?:[#*=-]{{1,{MAX_HEADING_LENGTH}}}|\w[^\r\n]{{0,{MAX_HEADING_CONTENT_LENGTH}}}\r?\n[-=]{{2,{MAX_HEADING_UNDERLINE_LENGTH}}}|<h[1-6][^>]{{0,{MAX_HTML_HEADING_ATTRIBUTES_LENGTH}}}>)[^\r\n]{{1,{MAX_HEADING_CONTENT_LENGTH}}}(?:</h[1-6]>)?(?:\r?\n|$))"
        rf"|"
        # New pattern for citations
        rf"(?:\[[0-9]+\][^\r\n]{{1,{MAX_STANDALONE_LINE_LENGTH}}})"
        rf"|"
        # 2. List items (bulleted, numbered, lettered, or task lists, including nested, up to three levels, with length constraints)
        rf"(?:(?:^|\r?\n)[ \t]{{0,3}}(?:[-*+•]|\d{{1,3}}\.\w\.|\[[ xX]\])[ \t]+(?:(?:\b[^\r\n]{{1,{MAX_LIST_ITEM_LENGTH}}}\b(?:[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))|(?:\b[^\r\n]{{1,{MAX_LIST_ITEM_LENGTH}}}\b(?=[\r\n]|$))|(?:\b[^\r\n]{{1,{MAX_LIST_ITEM_LENGTH}}}\b(?=[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?:.{{1,{LOOKAHEAD_RANGE}}}(?:[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))?))"
        rf"(?:(?:\r?\n[ \t]{{2,5}}(?:[-*+•]|\d{{1,3}}\.\w\.|\[[ xX]\])[ \t]+(?:(?:\b[^\r\n]{{1,{MAX_LIST_ITEM_LENGTH}}}\b(?:[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))|(?:\b[^\r\n]{{1,{MAX_LIST_ITEM_LENGTH}}}\b(?=[\r\n]|$))|(?:\b[^\r\n]{{1,{MAX_LIST_ITEM_LENGTH}}}\b(?=[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?:.{{1,{LOOKAHEAD_RANGE}}}(?:[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))?)))"
        rf"{{0,{MAX_NESTED_LIST_ITEMS}}}(?:\r?\n[ \t]{{4,{MAX_LIST_INDENT_SPACES}}}(?:[-*+•]|\d{{1,3}}\.\w\.|\[[ xX]\])[ \t]+(?:(?:\b[^\r\n]{{1,{MAX_LIST_ITEM_LENGTH}}}\b(?:[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))|(?:\b[^\r\n]{{1,{MAX_LIST_ITEM_LENGTH}}}\b(?=[\r\n]|$))|(?:\b[^\r\n]{{1,{MAX_LIST_ITEM_LENGTH}}}\b(?=[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?:.{{1,{LOOKAHEAD_RANGE}}}(?:[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))?)))"
        rf"{{0,{MAX_NESTED_LIST_ITEMS}}})?)"
        rf"|"
        # 3. Block quotes (including nested quotes and citations, up to three levels, with length constraints)
        rf"(?:(?:^>(?:>|\s{{2,}}){{0,2}}(?:(?:\b[^\r\n]{{0,{MAX_BLOCKQUOTE_LINE_LENGTH}}}\b(?:[.!?…]|\.{3}|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))|(?:\b[^\r\n]{{0,{MAX_BLOCKQUOTE_LINE_LENGTH}}}\b(?=[\r\n]|$))|(?:\b[^\r\n]{{0,{MAX_BLOCKQUOTE_LINE_LENGTH}}}\b(?=[.!?…]|\.{3}|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?:.{{1,{LOOKAHEAD_RANGE}}}(?:[.!?…]|\.{3}|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))?))\r?\n?){{1,{MAX_BLOCKQUOTE_LINES}}})"
        rf"|"
        # 4. Code blocks (fenced, indented, or HTML pre/code tags, with length constraints)
        rf"(?:(?:^|\r?\n)(?:\`\`\`|~~~)(?:\w{{0,{MAX_CODE_LANGUAGE_LENGTH}}})?\r?\n[\s\S]{{0,{MAX_CODE_BLOCK_LENGTH}}}?(?:\`\`\`|~~~)\r?\n?"
        rf"|(?:(?:^|\r?\n)(?: {4}|\t)[^\r\n]{{0,{MAX_LIST_ITEM_LENGTH}}}(?:\r?\n(?: {4}|\t)[^\r\n]{{0,{MAX_LIST_ITEM_LENGTH}}}){{0,{MAX_INDENTED_CODE_LINES}}}\r?\n?)"
        rf"|(?:<pre>(?:<code>)?[\s\S]{{0,{MAX_CODE_BLOCK_LENGTH}}}?(?:</code>)?</pre>))"
        rf"|"
        # 5. Tables (Markdown, grid tables, and HTML tables, with length constraints)
        rf"(?:(?:^|\r?\n)(?:\|[^\r\n]{{0,{MAX_TABLE_CELL_LENGTH}}}\|(?:\r?\n\|[-:]{{1,{MAX_TABLE_CELL_LENGTH}}}\|)?"
        rf"(?:\r?\n\|[^\r\n]{{0,{MAX_TABLE_CELL_LENGTH}}}\|){{0,{MAX_TABLE_ROWS}}}"
        rf"|<table>[\s\S]{{0,{MAX_HTML_TABLE_LENGTH}}}?</table>))"
        rf"|"
        # 6. Horizontal rules (Markdown and HTML hr tag)
        rf"(?:^(?:[-*_]){{{MIN_HORIZONTAL_RULE_LENGTH},}}\s*$|<hr\s*/?>)"
        rf"|"
        # 10. Standalone lines or phrases (including single-line blocks and HTML elements, with length constraints)
        rf"(?:^(?:<[a-zA-Z][^>]{{0,{MAX_HTML_TAG_ATTRIBUTES_LENGTH}}}>)?(?:(?:[^\r\n]{{1,{MAX_STANDALONE_LINE_LENGTH}}}(?:[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))|(?:[^\r\n]{{1,{MAX_STANDALONE_LINE_LENGTH}}}(?=[\r\n]|$))|(?:[^\r\n]{{1,{MAX_STANDALONE_LINE_LENGTH}}}(?=[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?:.{{1,{LOOKAHEAD_RANGE}}}(?:[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))?))(?:</[a-zA-Z]+>)?(?:\r?\n|$))"
        rf"|"
        # 7. Sentences or phrases ending with punctuation (including ellipsis and Unicode punctuation)
        rf"(?:(?:[^\r\n]{{1,{MAX_SENTENCE_LENGTH}}}(?:[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))|(?:[^\r\n]{{1,{MAX_SENTENCE_LENGTH}}}(?=[\r\n]|$))|(?:[^\r\n]{{1,{MAX_SENTENCE_LENGTH}}}(?=[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?:.{{1,{LOOKAHEAD_RANGE}}}(?:[.!?…]|\.\.\.|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))?))"
        rf"|"
        # 8. Quoted text, parenthetical phrases, or bracketed content (with length constraints)
        rf"(?:"
        rf"(?<!\w)\"\"\"[^\"]{{0,{MAX_QUOTED_TEXT_LENGTH}}}\"\"\"(?!\w)"
        rf"|(?<!\w)(?:['\"\`\'])[^\r\n]{{0,{MAX_QUOTED_TEXT_LENGTH}}}\\1(?!\w)"
        rf"|\([^\r\n()]{{0,{MAX_PARENTHETICAL_CONTENT_LENGTH}}}(?:\([^\r\n()]{{0,{MAX_PARENTHETICAL_CONTENT_LENGTH}}}\)[^\r\n()]{{0,{MAX_PARENTHETICAL_CONTENT_LENGTH}}}){{0,{MAX_NESTED_PARENTHESES}}}\)"
        rf"|\[[^\r\n\[\]]{{0,{MAX_PARENTHETICAL_CONTENT_LENGTH}}}(?:\[[^\r\n\[\]]{{0,{MAX_PARENTHETICAL_CONTENT_LENGTH}}}\][^\r\n\[\]]{{0,{MAX_PARENTHETICAL_CONTENT_LENGTH}}}){{0,{MAX_NESTED_PARENTHESES}}}\]"
        rf"|\$[^\r\n$]{{0,{MAX_MATH_INLINE_LENGTH}}}\$"
        rf"|\"[^\"\r\n]{{0,{MAX_MATH_INLINE_LENGTH}}}\""
        rf")"
        rf"|"
        # 9. Paragraphs (with length constraints)
        rf"(?:(?:^|\r?\n\r?\n)(?:<p>)?(?:(?:[^\r\n]{{1,{MAX_PARAGRAPH_LENGTH}}}(?:[.!?…]|\.{3}|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))|(?:[^\r\n]{{1,{MAX_PARAGRAPH_LENGTH}}}(?=[\r\n]|$))|(?:[^\r\n]{{1,{MAX_PARAGRAPH_LENGTH}}}(?=[.!?…]|\.{3}|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?:.{{1,{LOOKAHEAD_RANGE}}}(?:[.!?…]|\.{3}|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))?))(?:</p>)?(?=\r?\n\r?\n|$))"
        rf"|"
        # 11. HTML-like tags and their content (including self-closing tags and attributes, with length constraints)
        rf"(?:<[a-zA-Z][^>]{{0,{MAX_HTML_TAG_ATTRIBUTES_LENGTH}}}(?:>[\s\S]{{0,{MAX_HTML_TAG_CONTENT_LENGTH}}}?</[a-zA-Z]+>|\s*/>))"
        rf"|"
        # 12. LaTeX-style math expressions (inline and block, with length constraints)
        rf"(?:(?:\$\$[\s\S]{{0,{MAX_MATH_BLOCK_LENGTH}}}?\$\$)|(?:\$[^\$\r\n]{{0,{MAX_MATH_INLINE_LENGTH}}}\$))"
        rf"|"
        # 14. Fallback for any remaining content (with length constraints)
        rf"(?:(?:[^\r\n]{{1,{MAX_STANDALONE_LINE_LENGTH}}}(?:[.!?…]|\.{3}|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))|(?:[^\r\n]{{1,{MAX_STANDALONE_LINE_LENGTH}}}(?=[\r\n]|$))|(?:[^\r\n]{{1,{MAX_STANDALONE_LINE_LENGTH}}}(?=[.!?…]|\.{3}|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?:.{{1,{LOOKAHEAD_RANGE}}}(?:[.!?…]|\.{3}|[\u2026\u2047-\u2049]|[\p{{Emoji_Presentation}}\p{{Extended_Pictographic}}])(?=\s|$))?))"
        rf")",
        regex.MULTILINE | regex.UNICODE
    )

# 创建完整的正则表达式
chunk_regex = create_complete_chunk_regex()

def chunk_text(text: str) -> List[Dict[str, Any]]:
    """
    使用Jina风格的正则表达式对文本进行智能分块
    
    功能：
    - 将文本按照语义结构分割成多个块
    - 支持标题、段落、列表、代码块等多种格式
    - 返回详细的分块信息包括位置和长度
    
    Args:
        text (str): 要分块的原始文本
        
    Returns:
        List[Dict[str, Any]]: 分块信息列表，每个字典包含：
            - text: 分块文本内容
            - start: 在原文本中的起始位置
            - end: 在原文本中的结束位置
            - length: 分块文本长度
    """
    chunks = []
    matches = chunk_regex.finditer(text)
    
    for match in matches:
        chunk_text = match.group(0).strip()
        if chunk_text:  # 只保留非空的分块
            chunks.append({
                'text': chunk_text,
                'start': match.start(),
                'end': match.end(),
                'length': len(chunk_text)
            })
    
    return chunks

def chunk_text_simple(text: str) -> List[str]:
    """
    简化版本的分块函数，只返回文本块列表
    
    功能：
    - 对文本进行智能分块
    - 只返回分块后的文本内容，不包含位置信息
    - 适用于只需要文本内容的场景
    
    Args:
        text (str): 要分块的原始文本
        
    Returns:
        List[str]: 分块后的文本列表
    """
    chunks = chunk_text(text)
    return [chunk['text'] for chunk in chunks]

