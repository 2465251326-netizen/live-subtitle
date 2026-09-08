"""存储位置管理与迁移（建议1）。

支持把识别模型（hf）、离线语言包（argos）、翻译缓存（trans_cache.json）
整体搬到自定义根目录；配置中记录 storage_root，启动时自动重定位。
"""
import shutil
from pathlib import Path


def usage_summary():
    """当前数据占用情况：模型 / 语言包 / 缓存各自体积（MB）。"""
    from app import config as cfg
    from app.translate.offline_pack import dir_size_mb

    cache_mb = 0.0
    try:
        if cfg.CACHE_FILE.exists():
            cache_mb = cfg.CACHE_FILE.stat().st_size / 1048576.0
    except OSError:
        pass
    return {
        "root": Path(cfg.HF_HOME).parent,
        "hf_mb": dir_size_mb(cfg.HF_HOME),
        "argos_mb": dir_size_mb(cfg.ARGOS_DATA),
        "cache_mb": cache_mb,
    }


def disk_free_mb(path) -> float:
    try:
        return shutil.disk_usage(str(path)).free / 1048576.0
    except Exception:
        return 0.0


def migrate_root(new_root, progress_cb=None) -> str:
    """把数据目录迁移到新根；返回新根路径字符串。

    - 逐目录 shutil.move（跨盘自动 copy+delete）
    - trans_cache.json 若在旧位置存在则一并搬移
    - 完成后由调用方执行 Config.relocate() 重定位指针
    """
    from app import config as cfg

    new_root = Path(new_root)
    new_root.mkdir(parents=True, exist_ok=True)
    moves = []
    for src in (cfg.HF_HOME, cfg.ARGOS_DATA, cfg.CACHE_FILE):
        src = Path(src)
        dst = new_root / src.name
        if src.exists() and src.resolve() != dst.resolve():
            moves.append((src, dst))
    total = len(moves)
    for i, (src, dst) in enumerate(moves, 1):
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink(missing_ok=True)
        shutil.move(str(src), str(dst))
        if progress_cb:
            progress_cb(f"已迁移 {src.name}（{i}/{total}）")
    # 配置文件预拷贝到新根（relocate 保存时会覆盖）；拷贝而非移动，失败可回退
    cfgf = Path(cfg.CONFIG_FILE)
    dst_cfg = new_root / "config.json"
    if cfgf.exists() and cfgf.resolve() != dst_cfg.resolve():
        shutil.copy2(str(cfgf), str(dst_cfg))
    return str(new_root)
