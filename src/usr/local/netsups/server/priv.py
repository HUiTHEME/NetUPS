#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络UPS —— 需要「一丁点系统权限」或「系统工具」的两件事

本应用以非 root 的专用用户运行（TOS 规范 10.3 强制要求），所以这里不做任何
提权动作，只调用系统既有工具：

  ping          iputils-ping。TOS 7 的 net.ipv4.ping_group_range 已放开到
                普通用户，因此非 root 也能发 ICMP，不需要 CAP_NET_RAW。

  poweroff      必须由 systemd 收尾（tnas-system-stop 会 sync、卸载卷、响蜂鸣），
                所以不能自己调 reboot(2)，而是调 systemctl poweroff。该动作需要
                polkit 授权，由 deb 安装脚本投放一条**动作级**策略：
                仅 login1.power-off*、仅本应用用户，卸载即撤销。

关键设计：ping 的失败必须区分「目标真的不通」与「ping 工具本身出错」。
后者绝不能计入失联次数 —— 否则工具/权限出问题就会把 NAS 关掉。
"""

import math
import os
import re
import shutil
import subprocess
import time

PING_CANDIDATES = ("/usr/bin/ping", "/bin/ping", "/sbin/ping", "/usr/sbin/ping")

# ping 的退出码（iputils）：0=收到回应，1=无回应，2=其它错误
PING_EXIT_OK = 0
PING_EXIT_NO_REPLY = 1

RTT_RE = re.compile(r"=\s*[0-9.]+/([0-9.]+)/")


def _clean_env():
    """把环境收窄到最小：systemd 下 PATH 已由 EnvironmentFile 给好。"""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "LANG": "C",
        "LC_ALL": "C",
    }


def _run(cmd, timeout):
    """执行命令，返回 (returncode, stdout, stderr, cost_seconds)；异常时 rc=-1。"""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=_clean_env())
        return proc.returncode, proc.stdout or "", proc.stderr or "", time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return -1, "", "命令执行超时", time.monotonic() - t0
    except FileNotFoundError:
        return -1, "", "找不到命令: %s" % cmd[0], time.monotonic() - t0
    except OSError as exc:
        return -1, "", str(exc), time.monotonic() - t0


def ping_bin():
    for cand in PING_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return shutil.which("ping") or "/usr/bin/ping"


def _human_error(stdout, stderr):
    low = (stdout + "\n" + stderr).lower()
    if "permission denied" in low or "operation not permitted" in low:
        return "ping 无权发送 ICMP 包"
    if "unknown host" in low or "name or service not known" in low or "cannot resolve" in low:
        return "无法解析目标地址"
    if "network is unreachable" in low:
        return "本机网络不可达"
    if "destination host unreachable" in low:
        return "目标主机不可达"
    if "100% packet loss" in low:
        return "超时无响应"
    text = (stderr or stdout or "").strip()
    return text.splitlines()[-1][:120] if text else "无响应"


def ping(host, timeout):
    """探测一次。

    返回 dict：
      kind      ok   收到回应
                down 目标确实不通（应计入失联次数）
                error 探测本身失败（工具/权限/网络栈问题，**不计入**失联）
      ok        bool(kind == "ok")
      rtt_ms    往返时延，失败为 None
      error     人类可读的原因
      cost_ms   本次耗时
    """
    binary = ping_bin()
    wait = str(max(1, int(math.ceil(float(timeout)))))
    cmd = [binary, "-n", "-q", "-c", "1", "-W", wait, host]

    rc, out, err, cost = _run(cmd, timeout=float(timeout) + 5)

    if rc == PING_EXIT_OK:
        rtt = None
        m = RTT_RE.search(out)
        if m:
            try:
                rtt = float(m.group(1))
            except ValueError:
                rtt = None
        return {"kind": "ok", "ok": True, "rtt_ms": rtt, "error": "",
                "cost_ms": int(cost * 1000)}

    if rc == PING_EXIT_NO_REPLY:
        return {"kind": "down", "ok": False, "rtt_ms": None,
                "error": _human_error(out, err), "cost_ms": int(cost * 1000)}

    # rc == 2 或执行异常：这是「探测不可用」，不是「市电中断」
    return {"kind": "error", "ok": False, "rtt_ms": None,
            "error": _human_error(out, err), "cost_ms": int(cost * 1000)}


# ------------------------------------------------------------------ 关机 ---
POWEROFF_ACTIONS = (
    "org.freedesktop.login1.power-off",
    "org.freedesktop.login1.power-off-multiple-sessions",
)


def poweroff_permitted():
    """查询 polkit 是否已经允许本用户关机。

    这是**只读查询**，不会真的关机，用来在界面上提前告知用户权限状态。
    返回 True / False / None（无法判断，例如没有 polkit）。
    """
    pkcheck = shutil.which("pkcheck") or "/usr/bin/pkcheck"
    if not os.path.exists(pkcheck):
        return None
    rc, _out, err, _cost = _run(
        [pkcheck, "--action-id", POWEROFF_ACTIONS[0], "--process", str(os.getpid())],
        timeout=8)
    if rc == 0:
        return True
    if rc == 2:
        return False
    # 其它返回码说明查询本身出了问题，不要据此下结论
    if "not authorized" in (err or "").lower():
        return False
    return None


def poweroff():
    """触发系统安全关机。返回 dict(ok, method, error)。"""
    # 先把文件系统刷一遍，能省一点关机时间（systemd 收尾时还会再 sync）
    try:
        os.sync()
    except AttributeError:
        pass

    attempts = (
        ("systemctl", ["/usr/bin/systemctl", "poweroff"]),
        ("shutdown", ["/usr/sbin/shutdown", "-h", "now"]),
        ("poweroff", ["/usr/sbin/poweroff"]),
    )

    last_err = ""
    auth_error = False
    for name, cmd in attempts:
        if not os.path.exists(cmd[0]):
            continue
        rc, out, err, _cost = _run(cmd, timeout=60)
        if rc == 0:
            return {"ok": True, "method": name, "error": ""}
        text = (err or out or "").strip()
        if text:
            # systemctl 失败时可能连打多行：一行是 logind 的真实原因，
            # 另一行是它自己降级到 poweroff.target 的失败，取第一行更有用
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            last_err = (lines[0] if lines else "退出码 %s" % rc)[:200]
        else:
            last_err = "退出码 %s" % rc
        if "interactive authentication required" in text.lower() or "access denied" in text.lower():
            auth_error = True
            continue
        # 其它失败继续试下一种方式

    if auth_error:
        # 分清到底是「真没授权」还是「授权了但 systemd 那一步出错」，
        # 这句提示会直接显示在日志和界面上，不能误导用户
        permitted = poweroff_permitted()
        if permitted is False:
            return {"ok": False, "method": "",
                    "error": "系统未授予关机权限（polkit）: %s" % last_err}
        return {"ok": False, "method": "",
                "error": "systemd 拒绝了关机请求: %s" % last_err}

    return {"ok": False, "method": "", "error": last_err or "关机命令执行失败"}
