#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络UPS —— TOS 7 打包脚本

一次产出两个架构的 deb（载荷是纯 Python + 静态文件，两个架构内容完全一致，
只有 control 的 Architecture 不同，所以不需要交叉编译工具链）：

    dist/netsups_x86_64.deb     Architecture: amd64
    dist/netsups_aarch64.deb    Architecture: arm64

在 macOS / Linux 上都能跑，不依赖 dpkg-deb；装了 dpkg-deb 的话会额外用它
自检一次包结构。

用法：
    python3 build.py            # 构建两个架构
    python3 build.py --keep     # 保留 build/ 目录便于排查
"""

import argparse
import bz2
import gzip
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build")

APPID = "netsups"
APP_PATH = "usr/local/" + APPID

# TOS 平台名 → Debian 架构名
TARGETS = (("x86_64", "amd64"), ("aarch64", "arm64"))

# 打包前必须统一成 LF 的文本文件（TOS 规范 4.6：CRLF 会导致解释器路径变成 /bin/bash\r）
TEXT_EXT = (".sh", ".py", ".ini", ".lang", ".service", ".conf", ".env", ".json", ".md", ".html", ".js", ".css")

# TOS 静态检查 T02：tar 载荷不得使用 USTAR(POSIX) / PAX 格式，必须用 GNU 格式
# （dpkg-deb 自己出的包就是 GNU：magic 为 "ustar  \0"，POSIX 是 "ustar\0" + "00"）
TAR_FORMAT = tarfile.GNU_FORMAT

EXEC_PATHS = ("DEBIAN/postinst", "DEBIAN/prerm", "DEBIAN/postrm", "DEBIAN/config",
              "usr/local/netsups/bin/netsups")

# 不打进包里的开发期垃圾
EXCLUDE_NAMES = (".DS_Store",)

MTIME = 1767225600  # 2026-01-01 00:00:00 UTC —— 固定时间戳，保证可重现


# ----------------------------------------------------------------- 基础工具 ---
def read_version():
    with open(os.path.join(HERE, "VERSION"), "r", encoding="utf-8") as f:
        v = f.read().strip()
    if not v:
        raise SystemExit("VERSION 文件为空")
    return v


def render(text, **kw):
    for k, v in kw.items():
        text = text.replace("__%s__" % k.upper(), v)
    left = [t for t in ("__VERSION__", "__PLATFORM__", "__ARCH__") if t in text]
    if left:
        raise SystemExit("模板里还有未替换的占位符: %s" % ", ".join(left))
    return text


def to_lf(path):
    if path.endswith(TEXT_EXT):
        with open(path, "rb") as f:
            data = f.read()
        if b"\r\n" in data:
            with open(path, "wb") as f:
                f.write(data.replace(b"\r\n", b"\n"))


def norm_mode(path, root):
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    if rel in EXEC_PATHS:
        mode = 0o755
    elif os.path.isdir(path):
        mode = 0o755
    else:
        mode = 0o644
    os.chmod(path, mode)
    return mode


# --------------------------------------------------------------- tar 构造 ---
def add_tree(tar, root, arcbase="", dot_prefix=True):
    """把 root 下的内容按稳定顺序写进 tar，uid/gid 归零，权限取文件真实权限。"""
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_NAMES)
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        if rel_dir:
            entries.append((dirpath, rel_dir, True))
        for name in sorted(filenames):
            if name in EXCLUDE_NAMES or name.startswith("._"):
                continue
            rel = os.path.join(rel_dir, name) if rel_dir else name
            entries.append((os.path.join(dirpath, name), rel, False))

    for full, rel, is_dir in entries:
        arcname = (arcbase + "/" + rel).lstrip("/") if arcbase else rel
        info = tarfile.TarInfo(("./" + arcname) if dot_prefix else arcname)
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        info.mtime = MTIME
        st = os.lstat(full)
        if is_dir:
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            tar.addfile(info)
        else:
            info.type = tarfile.REGTYPE
            info.mode = stat.S_IMODE(st.st_mode)
            info.size = st.st_size
            with open(full, "rb") as f:
                tar.addfile(info, f)


def make_targz(files_or_root, gz_path, root_entry=True):
    """生成 tar.gz；files_or_root 是目录时递归打包。"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=TAR_FORMAT) as tar:
        if root_entry:
            info = tarfile.TarInfo("./")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = MTIME
            tar.addfile(info)
        add_tree(tar, files_or_root)
    with open(gz_path, "wb") as out:
        with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=9, mtime=0) as gz:
            gz.write(raw.getvalue())


# --------------------------------------------------------------- ar 构造 ---
def ar_header(name, size, mode="100644"):
    return ("%-16s%-12d%-6d%-6d%-8s%-10d`\n" % (name, MTIME, 0, 0, mode, size)).encode("ascii")


def write_ar(out_path, members):
    """members: [(name, bytes_or_path)]，按顺序写入规范 ar 容器。"""
    with open(out_path, "wb") as out:
        out.write(b"!<arch>\n")
        for name, payload in members:
            data = payload if isinstance(payload, bytes) else open(payload, "rb").read()
            out.write(ar_header(name, len(data)))
            out.write(data)
            if len(data) % 2:
                out.write(b"\n")


# ----------------------------------------------------------------- 主流程 ---
BUILD_ROOT = ""


def build_one(platform, arch, version, keep):
    global BUILD_ROOT
    BUILD_ROOT = os.path.join(BUILD, platform)
    root = os.path.join(BUILD_ROOT, "root")
    if os.path.isdir(BUILD_ROOT):
        shutil.rmtree(BUILD_ROOT)
    os.makedirs(root)

    # 1) 拷贝载荷
    shutil.copytree(os.path.join(SRC, "usr"), os.path.join(root, "usr"))

    # 2) 渲染 config.ini
    with open(os.path.join(root, APP_PATH, "config.ini.in"), "r", encoding="utf-8") as f:
        cfg = render(f.read(), version=version, platform=platform)
    os.remove(os.path.join(root, APP_PATH, "config.ini.in"))
    with open(os.path.join(root, APP_PATH, "config.ini"), "w", encoding="utf-8") as f:
        f.write(cfg)

    # 3) 前端 → webui.bz2（安装时由 postinst 解压成 webui/）
    webui_dir = os.path.join(root, APP_PATH, "webui")
    if not os.path.isdir(webui_dir):
        raise SystemExit("缺少前端目录 webui/")
    bz = os.path.join(root, APP_PATH, "webui.bz2")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=TAR_FORMAT) as tar:
        # 与平台既有应用保持一致：条目不带宽高前导 "./"，直接是 webui/index.html
        add_tree(tar, webui_dir, arcbase="webui", dot_prefix=False)
    with open(bz, "wb") as out:
        out.write(bz2.compress(raw.getvalue(), 9))
    shutil.rmtree(webui_dir)   # 只发压缩包，和平台约定一致

    # 3.5) 隐私政策：随包内嵌一份（审核项 C3），仓库根目录那份是唯一来源
    privacy = os.path.join(HERE, "PRIVACY.md")
    if os.path.exists(privacy):
        shutil.copy2(privacy, os.path.join(root, APP_PATH, "PRIVACY.md"))

    # 4) 规范化：换行、权限、垃圾文件
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_NAMES]
        for name in list(filenames):
            full = os.path.join(dirpath, name)
            if name in EXCLUDE_NAMES or name.startswith("._"):
                os.remove(full)
                continue
            to_lf(full)
            norm_mode(full, root)
        os.chmod(dirpath, 0o755)

    # 5) DEBIAN
    ddir = os.path.join(root, "DEBIAN")
    os.makedirs(ddir, exist_ok=True)
    with open(os.path.join(SRC, "DEBIAN", "control.in"), "r", encoding="utf-8") as f:
        control = render(f.read(), version=version, arch=arch)
    with open(os.path.join(ddir, "control"), "w", encoding="utf-8") as f:
        f.write(control)
    for name in ("postinst", "prerm", "postrm"):
        src = os.path.join(SRC, "DEBIAN", name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(ddir, name))
            os.chmod(os.path.join(ddir, name), 0o755)

    # md5sums（信息类，lintian 不再强制，但留着便于核对）
    lines = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if os.path.relpath(dirpath, root).startswith("DEBIAN"):
            continue
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as f:
                lines.append("%s  %s" % (hashlib.md5(f.read()).hexdigest(), rel))
    if lines:
        with open(os.path.join(ddir, "md5sums"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(os.path.join(ddir, "md5sums"), 0o644)

    # 6) 组两个 tar.gz
    ctl_tar = os.path.join(BUILD_ROOT, "control.tar.gz")
    data_tar = os.path.join(BUILD_ROOT, "data.tar.gz")
    make_targz(ddir, ctl_tar)
    make_targz(root, data_tar)

    # 7) 拼成 deb
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, "%s_%s.deb" % (APPID, platform))
    write_ar(out, [
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", ctl_tar),
        ("data.tar.gz", data_tar),
    ])
    size = os.path.getsize(out)
    print("  → %-42s %6.1f KB  (%s/%s)" % (os.path.basename(out), size / 1024.0, platform, arch))
    return out


def verify(deb):
    """有 dpkg-deb 时用它自检；没有就跳过（macOS 默认没有）。"""
    dpkg = shutil.which("dpkg-deb")
    if not dpkg:
        return None
    try:
        out = subprocess.run([dpkg, "-c", deb], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return False, out.stderr.strip()
        must = ["config.ini", "%s.lang" % APPID, "images/icons/%s.svg" % APPID,
                "bin/%s" % APPID, "init.d/%s.service" % APPID, "webui.bz2", "PRIVACY.md"]
        listing = out.stdout
        missing = [m for m in must if m not in listing]
        return (not missing), ("缺少: %s" % missing) if missing else "OK"
    except Exception as exc:
        return False, str(exc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留 build/ 目录")
    args = ap.parse_args()

    version = read_version()
    print("网络UPS / TOS 7 —— 打包 v%s" % version)
    if os.path.isdir(BUILD):
        shutil.rmtree(BUILD)
    os.makedirs(DIST, exist_ok=True)

    made = []
    for platform, arch in TARGETS:
        made.append(build_one(platform, arch, version, args.keep))

    print("\n自检：")
    for deb in made:
        res = verify(deb)
        if res is None:
            print("  · %s：本机没有 dpkg-deb，跳过结构自检（可在 TNAS 上 dpkg-deb -c 验证）"
                  % os.path.basename(deb))
        else:
            ok, msg = res
            print("  · %s：%s %s" % (os.path.basename(deb), "通过" if ok else "失败", msg))

    if not args.keep:
        shutil.rmtree(BUILD, ignore_errors=True)
    print("\n完成，产物在 dist/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
