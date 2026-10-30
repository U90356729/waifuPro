"""本地图库：把图片按类别放进 config/images/<类别>/，聊天时随机取一张发出去。"""

import base64
import logging
import os
import random
import time

logger = logging.getLogger(__name__)

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
DEFAULT_CATEGORY = "default"
# QQ 对图片大小有限制，超过这个体积就不发
MAX_IMAGE_BYTES = 4 * 1024 * 1024


class ImageLibrary:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self._last_sent = 0.0

    # ------------------------------------------------------------------ 图库

    def root(self) -> str:
        return os.path.join(self.base_dir, "images")

    @staticmethod
    def _images_in(path: str) -> list:
        try:
            return sorted(
                os.path.join(path, name)
                for name in os.listdir(path)
                if name.lower().endswith(IMAGE_EXTS)
            )
        except OSError:
            return []

    def categories(self) -> list:
        root = self.root()
        if not os.path.isdir(root):
            return []
        return [
            name
            for name in sorted(os.listdir(root))
            if os.path.isdir(os.path.join(root, name))
            and self._images_in(os.path.join(root, name))
        ]

    def pick(self, category: str = ""):
        """按类别取一张，类别不存在就退回默认类别。返回 (类别, 路径) 或 None。"""
        cats = self.categories()
        if not cats:
            return None

        name = (category or "").strip()
        if name not in cats:
            name = DEFAULT_CATEGORY if DEFAULT_CATEGORY in cats else random.choice(cats)

        files = self._images_in(os.path.join(self.root(), name))
        if not files:
            return None
        return name, random.choice(files)

    # ------------------------------------------------------------------ 频控

    def cooldown_ok(self, min_gap_seconds: int) -> bool:
        return (time.time() - self._last_sent) >= max(0, int(min_gap_seconds or 0))

    def mark_sent(self) -> None:
        self._last_sent = time.time()

    # ------------------------------------------------------------------ 读取

    @staticmethod
    def to_data_uri(path: str) -> str:
        """读成 data URI，超过大小上限返回空串。"""
        try:
            size = os.path.getsize(path)
        except OSError:
            return ""
        if size <= 0 or size > MAX_IMAGE_BYTES:
            logger.warning(f"图片体积不合适，跳过: {path} ({size} 字节)")
            return ""

        ext = os.path.splitext(path)[1].lower().lstrip(".")
        mime = "jpeg" if ext in ("jpg", "jpeg") else ext
        if mime not in ("jpeg", "png", "gif", "webp"):
            logger.warning(f"图片格式不支持，跳过: {path}")
            return ""

        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            logger.error(f"读取图片失败: {e}")
            return ""
        return f"data:image/{mime};base64," + base64.b64encode(raw).decode()
