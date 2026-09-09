"""GPU / CUDA 环境检测与引导（建议4）。

打包版内置 CPU 推理；源码运行 + NVIDIA 显卡用户可按引导安装 CUDA 版
PyTorch（提供 cuBLAS/cuDNN 运行时）后选择「自动（优先 GPU）」获得加速。
所有检测均为只读、失败静默降级，绝不影响正常使用。
"""
import shutil
import subprocess


def detect() -> dict:
    """汇总 GPU 环境信息，永不抛异常。"""
    import sys

    info = {
        "nvidia_gpu": "",
        "driver": "",
        "cuda_devices": 0,
        "vram_mb": 0,
        "frozen": bool(getattr(sys, "frozen", False)),
        "smi_error": "",
    }
    smi = shutil.which("nvidia-smi")
    if not smi:
        info["smi_error"] = "未找到 nvidia-smi（未安装 NVIDIA 驱动或无 NVIDIA 显卡）"
        info.update(_cuda_count(info))
        return info
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8, encoding="utf-8", errors="replace")
        line = (out.stdout or "").strip().splitlines()
        if line:
            parts = [p.strip() for p in line[0].split(",")]
            info["nvidia_gpu"] = parts[0] if parts else ""
            info["driver"] = parts[1] if len(parts) > 1 else ""
            # v2.0.9：读显存容量（如 "6144 MiB"）——按模型档位给显存建议
            if len(parts) > 2:
                try:
                    info["vram_mb"] = int(str(parts[2]).split()[0])
                except (ValueError, IndexError):
                    pass
    except Exception as e:
        info["smi_error"] = f"nvidia-smi 查询失败: {e}"
    info.update(_cuda_count(info))
    return info


def _cuda_count(info):
    try:
        import ctranslate2
        info["cuda_devices"] = int(ctranslate2.get_cuda_device_count())
    except Exception as e:
        # v2.0.9：修掉残迹 `info.setdefault(...) or None`（结果被丢弃的死代码）
        if not info.get("smi_error"):
            info["smi_error"] = f"CUDA 设备检测失败: {e}"
    return info


def guidance_text(info: dict) -> str:
    """根据检测结果生成图文教程文本。"""
    lines = []
    if info["nvidia_gpu"]:
        drv_ok = _driver_at_least(info["driver"], 525)
        vram = info.get("vram_mb") or 0
        vram_txt = f"，显存 {vram}MB" if vram else ""
        lines.append(f"· 显卡：{info['nvidia_gpu']}（驱动 {info['driver'] or '未知'}{vram_txt}"
                     + ("，满足 CUDA 12.x 要求 ≥525" if drv_ok else "，CUDA 12.x 需驱动 ≥525，请先升级驱动") + "）")
    else:
        lines.append("· 未检测到 NVIDIA 显卡；无独显时 CPU 模式已可实时，无需 GPU 加速")
    if info["cuda_devices"] > 0:
        lines.append("· CUDA 运行环境可用：把「计算方式」切到「自动（优先 GPU）」即可加速")
    elif info["nvidia_gpu"]:
        lines.append("· 有 NVIDIA 显卡但 CUDA 运行环境未就绪：源码运行可一键安装 CUDA 版 PyTorch；"
                     "打包版内置 CPU 推理，请下载 GPU 通道安装包（后续版本提供）")
    if info["frozen"]:
        lines.append("· 当前为打包版：内置 CPU 推理；GPU 需 GPU 通道安装包")
    else:
        lines.append("· 当前为源码运行：可一键安装 CUDA 版 PyTorch（约 +2GB），提供 cuDNN/cuBLAS 运行时")
    lines.append("· 笔记本双显卡（Optimus）：需在系统设置把 LiveSubtitle 指定为独显运行")
    # v2.0.9：按显存 × 模型档位给明确建议表（此前只有笼统的"medium 需约 5GB"）
    lines.append("")
    lines.append("—— 按你的显存，各模型推荐如下 ——")
    lines += vram_advice(info)
    return "\n".join(lines)


def vram_advice(info: dict):
    """按检测到的显存容量输出各模型档位的可运行性建议（GPU 就绪时才有意义）。"""
    gpu_ok = info.get("cuda_devices", 0) > 0
    vram = info.get("vram_mb") or 0
    if not gpu_ok:
        return ["· CUDA 未就绪，以下建议在配置好 GPU 后生效"]
    # GPU 显存需求（保守估计，含运行时开销）：tiny/base ≈1.5GB、small ≈2.5GB、
    # medium ≈5GB、large-v3-turbo ≈5GB（fp16）
    tiers = [("tiny / base", 1500, "CPU 已实时，GPU 收益小"),
             ("small（推荐日常）", 2500, "GPU 下约 1-2 秒/段，字幕跟手"),
             ("medium", 5000, "GPU 下约 2-3 秒/段"),
             ("large-v3-turbo", 5000, "GPU 下约 3-4 秒/段，精度接近 large")]
    out = []
    for name, need, gain in tiers:
        if vram and vram < need:
            out.append(f"· {name}：需约 {need // 1024}GB 显存，你的 {vram}MB 不足——保持 CPU")
        else:
            out.append(f"· {name}：可 GPU 运行（{gain}）")
    if vram and vram < 4000:
        out.append("· 显存 < 4GB：系统/浏览器也会占显存，实际可用更少——建议模型档位降一档")
    return out


def _driver_at_least(version, minimum):
    try:
        major = int(str(version).split(".")[0])
        return major >= minimum
    except Exception:
        return False
