"""Search and content caching with LRU eviction."""

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Cache directories
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
SEARCH_CACHE_DIR = CACHE_DIR / "search"
CONTENT_CACHE_DIR = CACHE_DIR / "content"
CACHE_MAX_ENTRIES = 1000

# Create cache directories
SEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CONTENT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Track cache size for LRU eviction
search_cache_index: Dict[str, datetime] = {}
content_cache_index: Dict[str, datetime] = {}

# Shared lock for index + file mutations. Search/content helpers are often
# invoked from asyncio.to_thread / threadpool workers; unlocked direct writes
# previously raced and could leave truncated JSON on disk.
_cache_lock = threading.RLock()

# Cache metrics (shared across modules)
cache_metrics = {"hits": 0, "misses": 0, "evictions": 0}


def generate_cache_key(data: str) -> str:
    """Generate a unique cache key using SHA-256 hash."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def atomic_write_json(cache_file: Path, payload: Dict[str, Any]) -> None:
    """Write JSON via temp file + os.replace so readers never see partial data."""
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_file.with_suffix(cache_file.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, cache_file)


def read_json_cache(cache_file: Path) -> Optional[Dict[str, Any]]:
    """Read a JSON cache file under the shared lock. Returns None on miss/error."""
    with _cache_lock:
        if not cache_file.exists():
            return None
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning(f"Failed to read cache file {cache_file}: {e}")
            cache_file.unlink(missing_ok=True)
            return None


def cleanup_cache(cache_dir: Path, cache_index: Dict[str, datetime], max_age: timedelta):
    """Remove expired cache entries and enforce LRU policy."""
    with _cache_lock:
        current_time = datetime.now()
        files_in_dir = {f.name.split(".")[0]: f for f in cache_dir.glob("*.cache")}

        to_remove = []
        for key, timestamp in list(cache_index.items()):
            if current_time - timestamp > max_age or key not in files_in_dir:
                to_remove.append(key)
                if key in files_in_dir:
                    files_in_dir[key].unlink(missing_ok=True)

        for key in to_remove:
            cache_index.pop(key, None)
            cache_metrics["evictions"] += 1

        if len(cache_index) > CACHE_MAX_ENTRIES:
            sorted_items = sorted(cache_index.items(), key=lambda x: x[1])
            excess_count = len(cache_index) - CACHE_MAX_ENTRIES
            for key, _ in sorted_items[:excess_count]:
                cache_index.pop(key, None)
                cache_file = cache_dir / f"{key}.cache"
                cache_file.unlink(missing_ok=True)
                cache_metrics["evictions"] += 1
