"""Reuse parsed manifests until the source file is replaced or changed."""
from functools import lru_cache
from pathlib import Path
import yaml


@lru_cache(maxsize=4)
def _yaml(path, modified, changed, size, inode):
    return yaml.load(Path(path).read_bytes(), Loader=yaml.CSafeLoader) or {}


def cached_yaml(path):
    path = Path(path)
    stat = path.stat()
    return _yaml(str(path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
