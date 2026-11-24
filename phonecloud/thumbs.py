"""图片缩略图：按需生成，缓存到 data/thumbs，并在 thumb_cache 表登记。"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .core import AppError, Core, Space

# 手机主摄可达 2 亿像素，Pillow 默认上限（约 0.9 亿）会把正常照片当成"解压炸弹"。
Image.MAX_IMAGE_PIXELS = 300_000_000


def _render(source: Path, target: Path, size: int) -> None:
    with Image.open(source) as image:
        # 对 JPEG：让解码器直接按缩小后的尺寸解码，大图省内存也快得多
        image.draft("RGB", (size * 2, size * 2))
        image = ImageOps.exif_transpose(image)  # 手机竖拍靠 EXIF 方向标记，不处理会横过来
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        if image.mode in ("RGBA", "LA", "P"):
            # JPEG 没有透明通道，透明区域铺白底
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")
        temp = target.with_name(f"{target.name}.{secrets.token_hex(4)}.tmp")
        try:
            image.save(temp, "JPEG", quality=80, optimize=True)
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()


def get_thumbnail(core: Core, space: Space, rel: str, source: Path) -> Path:
    """返回缩略图文件路径；源文件的大小或修改时间变了就重新生成。"""
    try:
        st = source.stat()
    except OSError:
        raise AppError(404, "文件不存在") from None

    with core.db.session() as conn:
        row = conn.execute(
            "SELECT * FROM thumb_cache WHERE space_id = ? AND rel_path = ?", (space.id, rel)
        ).fetchone()
    if row is not None and row["src_size"] == st.st_size and row["src_mtime_ns"] == st.st_mtime_ns:
        cached = core.settings.thumbs_dir / row["thumb_file"]
        if cached.is_file():
            return cached

    size = core.settings.thumb_size
    digest = hashlib.sha1(f"{space.id}\0{rel}\0{st.st_size}\0{st.st_mtime_ns}\0{size}".encode("utf-8")).hexdigest()
    thumb_file = f"{digest[:2]}/{digest}.jpg"
    target = core.settings.thumbs_dir / thumb_file
    target.parent.mkdir(parents=True, exist_ok=True)

    with core.thumb_slots:
        try:
            _render(source, target, size)
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
            # 损坏的图片、伪装成图片的文件等：当作"无法生成"，前端显示占位图标
            raise AppError(415, "无法为该文件生成缩略图") from None

    with core.db.session() as conn:
        conn.execute(
            "INSERT INTO thumb_cache (space_id, rel_path, src_size, src_mtime_ns, thumb_file, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (space_id, rel_path) DO UPDATE SET src_size = excluded.src_size,"
            " src_mtime_ns = excluded.src_mtime_ns, thumb_file = excluded.thumb_file,"
            " created_at = excluded.created_at",
            (space.id, rel, st.st_size, st.st_mtime_ns, thumb_file, int(time.time())),
        )
    if row is not None and row["thumb_file"] != thumb_file:
        try:
            (core.settings.thumbs_dir / row["thumb_file"]).unlink()
        except OSError:
            pass
    return target
