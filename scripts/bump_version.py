"""一键同步版本号到三个文件（v2.0.0 起替代手工三处修改）。

用法：
    python scripts/bump_version.py 2.1.0
    python scripts/bump_version.py --check   # CI 校验模式，不一致时退出码 1

覆盖：app/config.py (APP_VERSION)、setup.iss (MyAppVersion)、
version_info.txt (filevers/prodvers/FileVersion/ProductVersion)。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(p):
    return (ROOT / p).read_text(encoding="utf-8")


def write(p, s):
    (ROOT / p).write_text(s, encoding="utf-8", newline="\n")


def current_versions():
    out = {}
    m = re.search(r'APP_VERSION = "([\d.]+)"', read("app/config.py"))
    out["config.py"] = m.group(1) if m else None
    m = re.search(r'#define MyAppVersion "([\d.]+)"', read("setup.iss"))
    out["setup.iss"] = m.group(1) if m else None
    vi = read("version_info.txt")
    ms = re.findall(r"\((\d+), (\d+), (\d+), 0\)", vi)
    strs = re.findall(r"'(FileVersion|ProductVersion)', '([\d.]+)'", vi)
    out["version_info.txt"] = tuple(sorted({t[: -len(".0")] for _, t in strs} or
                                           {".".join(x) for x in ms}))
    return out


def set_version(ver):
    c = read("app/config.py")
    write("app/config.py", re.sub(r'APP_VERSION = "[\d.]+"', f'APP_VERSION = "{ver}"', c))
    s = read("setup.iss")
    write("setup.iss", re.sub(r'#define MyAppVersion "[\d.]+"', f'#define MyAppVersion "{ver}"', s))
    v = read("version_info.txt")
    parts = ver.split(".") + ["0"] * (3 - len(ver.split(".")))
    a, b, d = parts[0], parts[1], parts[2]
    v = re.sub(r"filevers=\([\d, ]+\)", f"filevers=({a}, {b}, {d}, 0)", v)
    v = re.sub(r"prodvers=\([\d, ]+\)", f"prodvers=({a}, {b}, {d}, 0)", v)
    v = re.sub(r"'FileVersion', '[\d.]+'", f"'FileVersion', '{ver}.0'", v)
    v = re.sub(r"'ProductVersion', '[\d.]+'", f"'ProductVersion', '{ver}.0'", v)
    write("version_info.txt", v)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    if sys.argv[1] == "--check":
        vs = current_versions()
        vals = set()
        for f, v in vs.items():
            if f == "version_info.txt":
                vals |= set(v)
            else:
                vals.add(v)
        if len(vals) != 1:
            print(f"版本不一致: {vs}")
            sys.exit(1)
        print(f"版本一致: {vals.pop()}")
        return
    ver = sys.argv[1].lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", ver):
        print(f"版本号格式应为 x.y.z，收到: {ver}")
        sys.exit(2)
    set_version(ver)
    print(f"已同步 {ver} -> app/config.py, setup.iss, version_info.txt")
    print(current_versions())


if __name__ == "__main__":
    main()
