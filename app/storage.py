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

    v2.0.1 加固：
    - 迁移前检查目标盘剩余空间（不足直接报错，不做无谓半迁移）
    - 逐目录 move 建立检查点，任一步失败把已完成项搬回原位（尽力回滚），
      避免"指针指旧根、数据在新根"的分裂状态（曾导致几 GB 模型被判未缓存重下）
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
    if not moves:
        return str(new_root)
    # 空间预检：目标盘剩余须大于待迁数据总量（跨盘 copy 需要）
    need = sum(dir_tree_size(s) for s, _ in moves)
    free = disk_free_mb(new_root)
    if free and need > free:
        raise RuntimeError(
            f"目标盘剩余空间不足：需要约 {need / 1024:.1f} GB，剩余 {free / 1024:.1f} GB")
    done = []
    try:
        total = len(moves)
        for i, (src, dst) in enumerate(moves, 1):
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink(missing_ok=True)
            shutil.move(str(src), str(dst))
            done.append((src, dst))
            if progress_cb:
                progress_cb(f"已迁移 {src.name}（{i}/{total}）")
    except Exception:
        # 尽力回滚：把已完成项搬回原位，保持"数据在旧根、指针在旧根"的一致状态
        for src, dst in reversed(done):
            try:
                if dst.exists():
                    shutil.move(str(dst), str(src))
            except Exception:
                pass
        raise RuntimeError(
            "迁移失败，已完成部分已尝试搬回原位置。"
            "常见原因：目标盘空间不足、杀毒软件占用文件、程序仍在使用模型。"
            "请确认翻译已完全停止后重试。")
    # 配置文件预拷贝到新根（relocate 保存时会覆盖）；拷贝而非移动，失败可回退
    cfgf = Path(cfg.CONFIG_FILE)
    dst_cfg = new_root / "config.json"
    if cfgf.exists() and cfgf.resolve() != dst_cfg.resolve():
        shutil.copy2(str(cfgf), str(dst_cfg))
    return str(new_root)


def dir_tree_size(path) -> float:
    """目录总占用（MB），跳过符号链接（HF 缓存快照是指向 blobs 的链接）。"""
    total = 0.0
    p = Path(path)
    if p.is_file():
        try:
            return p.stat().st_size / 1048576.0
        except OSError:
            return 0.0
    if not p.is_dir():
        return 0.0
    for f in p.rglob("*"):
        try:
            if f.is_symlink():
                continue
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total / 1048576.0
