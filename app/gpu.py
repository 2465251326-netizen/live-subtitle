"""GPU / CUDA 环境检测与引导（建议4）。

打包版内置 CPU 推理；源码运行 + NVIDIA 显卡用户可按引导安装 CUDA 版
PyTorch（提供 cuBLAS/cuDNN 运行时）后选择「强制 GPU」获得加速。
所有检测均为只读、失败静默降级，绝不影响正常使用。
"""
import shutil
import subprocess


def torch_cuda_state() -> str:
    """检测 CUDA 运行时载体（PyTorch）的状态（v2.1.2）。

    返回："cuda"=已装 CUDA 版（运行时就绪）/ "cpu"=装了 CPU 版（需升级）/
    "missing"=未装 PyTorch / "unknown"=检测失败。
    驱动级 CUDA 可见（nvidia-smi/ctranslate2 枚举）≠ 运行时可用——
    cuDNN/cuBLAS 动态库随 CUDA 版 PyTorch 分发，以此为运行时就绪判据。
    """
    import importlib.util
    if importlib.util.find_spec("torch") is None:
        return "missing"
    try:
        import torch
        return "cuda" if getattr(torch.version, "cuda", None) else "cpu"
    except Exception:
        return "unknown"


def detect() -> dict:
    """汇总 GPU 环境信息，永不抛异常。"""
    import sys

    info = {
        "nvidia_gpu": "",
        "driver": "",
        "cuda_devices": 0,
        "vram_mb": 0,
        "torch_cuda": "unknown",
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
    info["torch_cuda"] = torch_cuda_state()
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


def recommended_model(info=None):
    """v2.2.12：按硬件推荐识别档位 → (model_code, 理由)。永不抛异常。

    判据用 ctranslate2 可见的 CUDA 设备数（ASR 实际推理运行时），
    不依赖 torch——本应用 GPU 路径走 CT2 + cudnn 独立 wheel。"""
    try:
        info = info if info is not None else detect()
    except Exception:
        info = {}
    try:
        vram = int(info.get("vram_mb") or 0)
        gpu_ok = int(info.get("cuda_devices") or 0) > 0
    except (TypeError, ValueError):
        vram, gpu_ok = 0, False
    if gpu_ok and vram >= 5000:
        return "large-v3-turbo", f"检测到 GPU（显存 {vram}MB），可直接跑顶级档"
    if gpu_ok and vram >= 2500:
        return "small", f"检测到 GPU（显存 {vram}MB），small 流畅且省显存"
    if gpu_ok:
        return "base", f"检测到 GPU 但显存仅 {vram}MB，建议小档"
    return "small", "未检测到可用 GPU：small 档在 4 核以上 CPU 可实时"


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
    # v2.1.2：按"运行时就绪"三态给结论——驱动可见 ≠ 运行时可用
    torch_state = info.get("torch_cuda", "unknown")
    if info.get("nvidia_gpu"):
        if torch_state == "cuda":
            lines.append("· CUDA 运行时已就绪（CUDA 版 PyTorch 已安装）："
                         "把「计算方式」切到「强制 GPU」即可加速")
        elif torch_state == "cpu":
            lines.append("· 已装 PyTorch 但是 CPU 版（缺 cuDNN/cuBLAS 运行时）："
                         "点下方「一键安装 CUDA 版 PyTorch」升级即可")
        else:
            lines.append("· 有 NVIDIA 显卡但 CUDA 运行时未安装（cuDNN/cuBLAS 缺失，"
                         "此状态选择 GPU 推理会挂死）：点下方「一键安装 CUDA 版 PyTorch」")
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
    """按检测到的显存容量输出各模型档位的可运行性建议（运行时就绪才有意义）。"""
    gpu_ok = (info.get("cuda_devices", 0) > 0
              and info.get("torch_cuda") == "cuda")
    vram = info.get("vram_mb") or 0
    if not gpu_ok:
        return ["· CUDA 运行时尚未就绪，以下建议在「一键安装 CUDA 版 PyTorch」后生效"]
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
