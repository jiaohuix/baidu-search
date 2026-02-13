"""
异步缓存模块：融合 youtu 装饰器风格 + arpo asyncio.Lock 并发安全 + TTL 过期。

支持两种用法：
1. 装饰器：@async_cache(ttl=3600)
2. 手动调用：cache_manager.get(key) / cache_manager.set(key, value)

存储后端：内存 dict（默认） / SQLite（可选持久化）
"""

import asyncio
import functools
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AsyncCacheManager:
    """异步安全的缓存管理器，支持 TTL + 可选 SQLite 持久化。"""

    def __init__(self, default_ttl: int | None = 3600, db_path: str | Path | None = None):
        """
        Args:
            default_ttl: 默认过期时间（秒），None 表示永不过期
            db_path: SQLite 文件路径，None 则纯内存缓存
        """
        self._memory: dict[str, dict] = {}  # {key: {"value": ..., "ts": ...}}
        self._lock = asyncio.Lock()
        self.default_ttl = default_ttl
        self._db_path = str(db_path) if db_path else None
        if self._db_path:
            self._init_db()

    # ── SQLite 初始化 ──────────────────────────────────────
    def _init_db(self):
        if not self._db_path:
            return

        # ── 确保数据库目录存在 ──────────────────────────────
        db_file = Path(self._db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)

        # ── 初始化 SQLite ──────────────────────────────────
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    timestamp REAL
                )
            """)
            conn.commit()

    # ── 核心 API ───────────────────────────────────────────
    async def get(self, key: str, ttl: int | None = ...) -> Any | None:
        """获取缓存，过期返回 None。ttl 不传则用 default_ttl。"""
        effective_ttl = self.default_ttl if ttl is ... else ttl

        # 先查内存
        entry = self._memory.get(key)
        if entry and self._is_valid(entry["ts"], effective_ttl):
            logger.debug(f"[cache hit/mem] {key[:60]}")
            return entry["value"]

        # 再查 SQLite
        if self._db_path:
            row = self._db_get(key)
            if row and self._is_valid(row[1], effective_ttl):
                value = json.loads(row[0])
                # 回填内存
                self._memory[key] = {"value": value, "ts": row[1]}
                logger.debug(f"[cache hit/db] {key[:60]}")
                return value

        return None

    async def set(self, key: str, value: Any):
        """写入缓存（内存 + 可选 SQLite）。"""
        if value is None:
            return
        async with self._lock:
            ts = time.time()
            self._memory[key] = {"value": value, "ts": ts}
            if self._db_path:
                self._db_set(key, value, ts)
            logger.debug(f"[cache set] {key[:60]}")

    async def clear_expired(self):
        """清理过期条目。"""
        if self.default_ttl is None:
            return
        now = time.time()
        async with self._lock:
            expired = [k for k, v in self._memory.items()
                       if now - v["ts"] > self.default_ttl]
            for k in expired:
                del self._memory[k]
            logger.debug(f"[cache] cleared {len(expired)} expired entries")

    # ── 内部工具 ───────────────────────────────────────────
    @staticmethod
    def _is_valid(ts: float, ttl: int | None) -> bool:
        if ttl is None:
            return True
        return (time.time() - ts) < ttl

    def _db_get(self, key: str):
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT value, timestamp FROM cache WHERE key = ?", (key,)
            ).fetchone()
            return row

    def _db_set(self, key: str, value: Any, ts: float):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, value, timestamp) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), ts),
            )
            conn.commit()


def make_cache_key(*args, **kwargs) -> str:
    """根据函数参数生成缓存 key（md5）。"""
    raw = str(args) + str(sorted(kwargs.items()))
    return hashlib.md5(raw.encode()).hexdigest()


# ── 全局缓存实例（模块级单例） ─────────────────────────────  
# 查询级：1h
_search_cache = AsyncCacheManager(
    default_ttl=3600,
    db_path=".cache/search.db"
)
# URL级：24h
_url_cache = AsyncCacheManager(
    default_ttl=86400,
    db_path=".cache/url.db"
)


def async_cache(cache: AsyncCacheManager | None = None, ttl: int | None = ...,
                key_fn=None):
    """异步缓存装饰器。

    Args:
        cache: 缓存管理器实例，默认用 _search_cache
        ttl: 过期时间（秒），... 表示用 cache 的 default_ttl
        key_fn: 自定义 key 生成函数 (args, kwargs) -> str
    """
    _cache = cache or _search_cache

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # 生成 key：跳过 self 参数
            cache_args = args[1:] if args and hasattr(args[0], func.__name__) else args
            if key_fn:
                key = key_fn(*cache_args, **kwargs)
            else:
                key = f"{func.__name__}:{make_cache_key(*cache_args, **kwargs)}"

            # 查缓存
            effective_ttl = ttl if ttl is not ... else None
            cached = await _cache.get(key, ttl=effective_ttl)
            if cached is not None:
                return cached

            # 执行原函数
            result = await func(*args, **kwargs)

            # 写缓存
            await _cache.set(key, result)
            return result

        return wrapper
    return decorator


def get_search_cache() -> AsyncCacheManager:
    return _search_cache

def get_url_cache() -> AsyncCacheManager:
    return _url_cache

