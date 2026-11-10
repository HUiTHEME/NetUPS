#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络UPS (NetUPS) for TerraMaster TOS 7 —— 后端服务

核心逻辑（与飞牛版一致）：
  定时 ping 局域网内的某个设备（通常是路由器）。
    - ping 通     -> 市电正常，按「探测间隔」继续
    - ping 不通   -> 进入「失联再验证」，按更密的间隔重测
    - 连续失联达到阈值 -> 立即安全关机（保护设备与硬盘）

运行形态（TOS 7 规范 8.3.1「WebUI 内部打开」）：
  - 以专用非 root 用户 netsups 运行
  - 只监听 /var/api/netsups.sock（0660），不占任何 TCP 端口
  - 前端由平台 nginx 从 /usr/www/netsups 发出，接口走 /v2/proxy/netsups/
  - 关机交给 systemd（polkit 动作级授权），不自建 root 守护进程

仅使用 Python 3 标准库，无第三方依赖。
"""

import argparse
import errno
import ipaddress
import json
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import priv  # noqa: E402

APP_NAME = "netsups"

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(APP_DIR, "data")
LOG_DIR = os.path.join(APP_DIR, "logs")

# ---------------------------------------------------------------- 默认配置 ---
DEFAULT_CONFIG = {
    "enabled": True,              # 应用开关：关闭后停止探测，也不会关机
    "target_ip": "",              # 被探测的局域网设备 IP/主机名
    "interval_sec": 60,           # 正常时每隔多少秒 ping 一次
    "ping_timeout_sec": 2,        # 单次 ping 等待回包的秒数
    "recheck_sec": 10,            # 失联后进入再验证，每隔多少秒重测一次
    "max_failures": 3,            # 连续多少次失联再验证失败 -> 立即关机
    "startup_grace_sec": 180,     # 启动/开机宽限期，期间失败不计数
    "shutdown_delay_sec": 0,      # 关机前再等待几秒（留时间写日志）
    "log_max_bytes": 2 * 1024 * 1024,
}

# 主机名：首尾必须是字母/数字，中间允许 . 和 -，禁止以 '-' 开头（防止被当成 ping 参数）
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,62}[A-Za-z0-9])?$")

CFG_PATH = os.path.join(STATE_DIR, "config.json")
# 按 TOS 规范，运行期日志放 logs/（不是 data/，也不许放 /tmp）
LOG_PATH = os.path.join(LOG_DIR, "ping.log")
WWW_DIR = os.path.join(APP_DIR, "webui")

# 前缀：平台网关一般会剥掉 /v2/proxy/netsups，但个别固件会整段透传
PATH_PREFIXES = ()
REQUIRE_CSRF = os.environ.get("NETSUPS_REQUIRE_CSRF", "1") not in ("0", "false", "no")
SOCKET_MODE = int(os.environ.get("NETSUPS_SOCK_MODE", "0660"), 8)

# ---------------------------------------------------------------- 常量参数 ---
LOG_RETENTION_DAYS = 3          # 日志保留天数
LOG_TAIL_BYTES = 512 * 1024     # 读取日志时最多回看多少字节
LOG_PURGE_INTERVAL = 3600       # 日志清理间隔（秒）
MAX_BODY_BYTES = 256 * 1024     # 请求体上限
SHUTDOWN_RETRY_COOLDOWN = 60    # 关机失败后，多久才允许重试
PERM_CHECK_INTERVAL = 300       # 关机权限状态的复查间隔（秒）
PROBE_ERROR_ALERT = 3           # 连续多少次探测异常后，界面明确告警


# ------------------------------------------------------------------ 工具函数 ---
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def app_version():
    """版本号唯一来源是 config.ini（= App Center 显示、平台记录的那一个）。

    以前飞牛版把版本号同时写在 manifest 和代码里，漏改一处界面就显示旧版本，
    这里直接读 config.ini，杜绝两处打架。
    """
    try:
        with open(os.path.join(APP_DIR, "config.ini"), "r", encoding="utf-8") as f:
            return str(json.load(f).get("version") or "").strip() or "0.0.0"
    except (OSError, ValueError):
        return "0.0.0"


def rotate_log(max_bytes):
    """超过上限时把旧日志挪成 .1（仅当轮转备份不存在或更小时）"""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > max_bytes:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass


def journal(level, component, message):
    """按 TOS 日志规范写 stdout，由 journald 收集（journalctl -u netsups -f）。"""
    try:
        sys.stdout.write("[%s] [%s] [%s] %s\n" % (now_str(), level, component, message))
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def log_line(tag, msg, max_bytes=DEFAULT_CONFIG["log_max_bytes"]):
    """写应用自己的 ping.log（供界面展示），同时进 journal。"""
    line = "%s [%-6s] %s\n" % (now_str(), tag, msg)
    level = {"OK": "INFO", "FAIL": "WARN", "EVENT": "INFO",
             "TEST": "INFO", "ERROR": "ERROR"}.get(tag, "INFO")
    journal(level, tag.lower(), msg)
    try:
        rotate_log(max_bytes)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def system_uptime():
    """系统开机时长（秒）。

    读不到时返回 None —— 调用方必须把 None 当成「无法判断」，
    **不能**当成 0，否则宽限期会被永久判定为「刚开机」，等于永远不关机。
    """
    try:
        with open("/proc/uptime", "r") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def purge_old_logs(cutoff_ts):
    """清理早于 cutoff_ts 的日志行（ping.log 及其轮转备份），返回清理条数。"""
    removed = 0
    for pth in (LOG_PATH, LOG_PATH + ".1"):
        try:
            if not os.path.exists(pth):
                continue
            with open(pth, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            kept = []
            for line in lines:
                m = re.match(r"^(\d{4}-\d{2}-\d{2}) ", line)
                if m:
                    try:
                        ts = time.mktime(datetime.strptime(m.group(1), "%Y-%m-%d").timetuple())
                    except ValueError:
                        ts = None
                    if ts is not None and ts < cutoff_ts:
                        removed += 1
                        continue
                kept.append(line)
            if len(kept) != len(lines):
                tmp = pth + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(kept)
                os.replace(tmp, pth)
        except OSError:
            continue
    return removed


def atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for k in DEFAULT_CONFIG:
                if k in saved:
                    cfg[k] = saved[k]
    except (OSError, ValueError):
        pass
    return cfg


def current_user():
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return os.getenv("USER") or "?"


def normalize_target(value):
    """校验目标地址：合法 IPv4/IPv6 或主机名。非法返回 None，空串返回 ""。"""
    t = str(value or "").strip()
    if not t:
        return ""
    if len(t) > 64:
        return None
    try:
        ipaddress.ip_address(t)      # IPv4 / IPv6 优先判定（允许冒号）
        return t
    except ValueError:
        pass
    if t.startswith("-") or not HOST_RE.match(t):
        return None
    for label in t.split("."):
        if not label or len(label) > 63 or label.startswith("-") or label.endswith("-"):
            return None
    return t


def detect_gateway():
    """默认网关（一般是路由器）断电即代表市电断开，作为初始探测目标。"""
    for cmd in (["/usr/sbin/ip", "route"], ["/sbin/ip", "route"], ["ip", "route"]):
        if not os.path.exists(cmd[0]):
            continue
        try:
            import subprocess
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            for line in (p.stdout or "").splitlines():
                if line.startswith("default"):
                    return line.split()[2]
        except Exception:
            continue
    return ""


def validate_config(raw):
    """校验并归一化配置，返回 (cfg, errors)"""
    cfg = dict(DEFAULT_CONFIG)
    errors = []

    flag = raw.get("enabled", DEFAULT_CONFIG["enabled"])
    if isinstance(flag, str):
        flag = flag.strip().lower() in ("1", "true", "yes", "on")
    cfg["enabled"] = bool(flag)

    target = normalize_target(raw.get("target_ip", ""))
    if target is None:
        errors.append("目标 IP / 主机名格式不正确")
        target = ""
    cfg["target_ip"] = target

    def num(key, lo, hi, label):
        try:
            v = int(raw.get(key, DEFAULT_CONFIG[key]))
        except (TypeError, ValueError):
            errors.append("%s 必须是数字" % label)
            return DEFAULT_CONFIG[key]
        if v < lo or v > hi:
            errors.append("%s 取值范围 %s ~ %s" % (label, lo, hi))
            return DEFAULT_CONFIG[key]
        return v

    cfg["interval_sec"] = num("interval_sec", 1, 3600, "探测间隔")
    cfg["ping_timeout_sec"] = num("ping_timeout_sec", 1, 30, "超时时间")
    cfg["recheck_sec"] = num("recheck_sec", 1, 3600, "失联再验证")
    cfg["max_failures"] = num("max_failures", 1, 100, "失败次数")
    cfg["startup_grace_sec"] = num("startup_grace_sec", 0, 7200, "启动宽限期")
    cfg["shutdown_delay_sec"] = num("shutdown_delay_sec", 0, 600, "关机延迟")
    cfg["log_max_bytes"] = DEFAULT_CONFIG["log_max_bytes"]
    return cfg, errors


# ------------------------------------------------------------------ 守护进程 ---
class Monitor(object):
    def __init__(self, cfg):
        self.lock = threading.RLock()
        self.cfg = cfg
        self.stop_evt = threading.Event()
        self.wake_evt = threading.Event()   # 配置变更时唤醒循环，立即生效
        self.started_at = time.time()
        self._last_purge = 0.0
        self._shutdown_retry_after = 0.0
        self._perm_checked_at = 0.0
        self._in_grace = False          # 上一轮是否处于开机宽限期
        self._grace_logged_at = 0.0     # 宽限期内失败日志的上次打印时间（限流）
        self.shutdown_started = False
        self.state = {
            "status": "unknown",          # unknown/up/down/unconfigured/disabled/shutting_down/error
            "consecutive_failures": 0,
            "consecutive_errors": 0,
            "last_check_ts": 0,
            "last_ok_ts": 0,
            "last_rtt_ms": None,
            "last_error": "",
            "total_ok": 0,
            "total_fail": 0,
            "total_error": 0,
        }
        self.poweroff_allowed = None      # True/False/None(未知)
        self.last_poweroff_error = ""

    # -- 配置 ---------------------------------------------------------------
    def get_cfg(self):
        """返回配置快照；与默认值合并，保证调用方拿到的键一定齐全。"""
        with self.lock:
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(self.cfg)
            return cfg

    def set_cfg(self, cfg):
        with self.lock:
            self.cfg = cfg
            self.state["consecutive_failures"] = 0
            self.state["consecutive_errors"] = 0
            self.state["status"] = "unknown"
            # 重新配置后允许重新尝试关机
            self._shutdown_retry_after = 0.0
            self.shutdown_started = False
        atomic_write_json(CFG_PATH, cfg)
        self.wake_evt.set()
        log_line("EVENT", "配置已更新: 应用=%s 目标=%s 间隔=%ss 超时=%ss 再验证=%ss 阈值=%s 次 宽限=%ss"
                 % ("开启" if cfg["enabled"] else "关闭", cfg["target_ip"] or "(空)",
                    cfg["interval_sec"], cfg["ping_timeout_sec"], cfg["recheck_sec"],
                    cfg["max_failures"], cfg["startup_grace_sec"]),
                 cfg["log_max_bytes"])

    # -- 可被唤醒的等待 ------------------------------------------------------
    def _sleep(self, seconds):
        """等待指定秒数，若期间配置被修改则立即返回，使新配置马上生效。"""
        self.wake_evt.wait(max(0.1, seconds))
        self.wake_evt.clear()

    # -- 关机权限 -----------------------------------------------------------
    def refresh_poweroff_permission(self, force=False):
        now = time.time()
        with self.lock:
            if not force and now - self._perm_checked_at < PERM_CHECK_INTERVAL:
                return self.poweroff_allowed
            self._perm_checked_at = now
        allowed = priv.poweroff_permitted()
        with self.lock:
            changed = (self.poweroff_allowed is not None and
                       allowed is not None and
                       allowed != self.poweroff_allowed)
            self.poweroff_allowed = allowed
        if changed:
            log_line("EVENT", "关机权限状态变为: %s" % ("允许" if allowed else "被拒绝"),
                     self.get_cfg()["log_max_bytes"])
        return allowed

    # -- 单次探测 ------------------------------------------------------------
    def probe(self, ip, timeout):
        res = priv.ping(ip, timeout)
        return res

    # -- 宽限期 --------------------------------------------------------------
    def grace_remaining(self, cfg):
        """开机宽限期剩余秒数。

        **只认系统开机时长，不认进程启动时间。**
        宽限期的目的只有一个：设备刚上电、刚进系统，网络还没就绪，
        这时 ping 不通不能当成市电中断，否则开一次机就关一次机。

        早先的写法是 max(进程已运行, 系统已运行) 两条都算，后果是每次
        重启服务、升级、甚至改一次配置都会重新拿到一整段宽限，等于把
        「停电保护」关掉几分钟。这里只保留开机时长这一条基准。

        读不到开机时长时不给宽限（宁可保护，不可永久失效）。
        """
        grace = int(cfg.get("startup_grace_sec", 0))
        if grace <= 0:
            return 0
        uptime = system_uptime()
        if uptime is None:
            return 0
        return int(max(0.0, grace - uptime))

    # -- 关机 ----------------------------------------------------------------
    def trigger_shutdown(self, reason):
        """触发关机；成功返回 True，失败会复位以便稍后重试。"""
        with self.lock:
            if self.shutdown_started:
                return False
            self.shutdown_started = True
            self.state["status"] = "shutting_down"
        cfg = self.get_cfg()
        log_line("EVENT", "触发关机: %s" % reason, cfg["log_max_bytes"])

        delay = int(cfg.get("shutdown_delay_sec", 0))
        if delay > 0:
            log_line("EVENT", "等待 %s 秒后执行关机" % delay, cfg["log_max_bytes"])
            self.stop_evt.wait(delay)

        res = priv.poweroff()
        ok = bool(res.get("ok"))
        if ok:
            log_line("EVENT", "已通过 %s 提交系统关机" % (res.get("method") or "systemctl"),
                     cfg["log_max_bytes"])
            self.last_poweroff_error = ""
            return True

        with self.lock:
            self.shutdown_started = False          # 允许之后重试
            self._shutdown_retry_after = time.time() + SHUTDOWN_RETRY_COOLDOWN
            self.last_poweroff_error = res.get("error") or "关机命令执行失败"
            if self.state["status"] == "shutting_down":
                self.state["status"] = "down"
        log_line("ERROR", "关机未能执行: %s；%s 秒后若仍失联将重试"
                 % (self.last_poweroff_error, SHUTDOWN_RETRY_COOLDOWN), cfg["log_max_bytes"])
        return False

    # -- 主循环 --------------------------------------------------------------
    def run(self):
        cfg = self.get_cfg()
        log_line("EVENT", "守护进程启动 v%s (pid=%s, 用户=%s, 目标=%s)"
                 % (app_version(), os.getpid(), current_user(),
                    cfg["target_ip"] or "(未配置)"), cfg["log_max_bytes"])

        self.refresh_poweroff_permission(force=True)
        if self.poweroff_allowed is False:
            log_line("ERROR", "当前系统未授予关机权限，检测到市电中断后不会自动关机；"
                              "请确认 polkit 策略已安装（重装应用可恢复）", cfg["log_max_bytes"])

        warned_no_ip = False
        warned_probe_err = False
        while not self.stop_evt.is_set():
            cfg = self.get_cfg()
            interval = max(1, int(cfg["interval_sec"]))
            recheck = max(1, int(cfg["recheck_sec"]))
            ip = (cfg.get("target_ip") or "").strip()

            # 每小时清理一次超过保留天数的日志
            now = time.time()
            if now - self._last_purge > LOG_PURGE_INTERVAL:
                self._last_purge = now
                removed = purge_old_logs(now - LOG_RETENTION_DAYS * 86400)
                if removed:
                    log_line("EVENT", "已清理 %s 天前的日志 %s 条" % (LOG_RETENTION_DAYS, removed),
                             cfg["log_max_bytes"])

            if not cfg.get("enabled", True):
                with self.lock:
                    self.state["status"] = "disabled"
                    self.state["consecutive_failures"] = 0
                self._sleep(min(interval, 10))
                continue

            if not ip:
                with self.lock:
                    self.state["status"] = "unconfigured"
                if not warned_no_ip:
                    log_line("EVENT", "尚未配置目标 IP，等待在界面中设置", cfg["log_max_bytes"])
                    warned_no_ip = True
                self._sleep(min(interval, 10))
                continue
            warned_no_ip = False

            if self.shutdown_started:
                self._sleep(5)
                continue

            cycle_start = time.time()
            res = self.probe(ip, cfg["ping_timeout_sec"])
            kind = res["kind"]
            left = self.grace_remaining(cfg)
            in_grace = left > 0
            ts = time.time()

            # 宽限期的进出各记一条，避免用户在日志里看到一堆「不计数」却不知道来由
            if in_grace and not self._in_grace:
                self._grace_logged_at = 0.0
                log_line("EVENT", "进入开机宽限期（系统启动 %.0fs / 宽限 %ss，按开机时长计时），失败不计数"
                         % (system_uptime() or 0, cfg["startup_grace_sec"]),
                         cfg["log_max_bytes"])
            elif (not in_grace) and self._in_grace:
                log_line("EVENT", "开机宽限期结束，恢复失联计数", cfg["log_max_bytes"])
            self._in_grace = in_grace

            if kind == "ok":
                rtt = res["rtt_ms"]
                with self.lock:
                    self.state["last_check_ts"] = ts
                    self.state["last_rtt_ms"] = rtt
                    self.state["last_error"] = ""
                    self.state["total_ok"] += 1
                    self.state["consecutive_failures"] = 0
                    self.state["consecutive_errors"] = 0
                    self.state["last_ok_ts"] = ts
                    if self.state["status"] != "shutting_down":
                        self.state["status"] = "up"
                warned_probe_err = False
                log_line("OK", "%s rtt=%sms 连续失败=0/%s"
                         % (ip, ("%.2f" % rtt) if rtt is not None else "-", cfg["max_failures"]),
                         cfg["log_max_bytes"])

            elif kind == "down":
                with self.lock:
                    self.state["last_check_ts"] = ts
                    self.state["last_rtt_ms"] = None
                    self.state["last_error"] = res["error"]
                    self.state["total_fail"] += 1
                    if not in_grace:
                        self.state["consecutive_failures"] += 1
                    self.state["consecutive_errors"] = 0
                    fails = self.state["consecutive_failures"]
                    if self.state["status"] != "shutting_down":
                        self.state["status"] = "down"
                warned_probe_err = False
                if in_grace:
                    # 宽限期最长可能几分钟，而失联再验证是 10 秒一次；
                    # 每 60 秒记一条足够说明状态，不至于把日志刷满
                    if ts - self._grace_logged_at >= 60:
                        self._grace_logged_at = ts
                        log_line("FAIL", "%s 不通(%s) 宽限期内 剩余 %ss，不计数"
                                 % (ip, res["error"], left), cfg["log_max_bytes"])
                else:
                    log_line("FAIL", "%s 不通(%s) 连续失败=%s/%s"
                             % (ip, res["error"], fails, cfg["max_failures"]), cfg["log_max_bytes"])
                if (not in_grace) and fails >= int(cfg["max_failures"]):
                    if time.time() >= self._shutdown_retry_after:
                        self.trigger_shutdown(
                            "连续 %s 次失联再验证不通（每次间隔 %ss），判定市电中断"
                            % (fails, recheck))

            else:  # kind == "error"：探测工具本身出了问题，绝不能当成市电中断
                with self.lock:
                    self.state["last_check_ts"] = ts
                    self.state["last_rtt_ms"] = None
                    self.state["last_error"] = res["error"]
                    self.state["total_error"] += 1
                    self.state["consecutive_errors"] += 1
                    errs = self.state["consecutive_errors"]
                    self.state["status"] = "error"
                log_line("ERROR", "%s 探测异常(%s) 连续异常=%s 次；不计入失联，不会关机"
                         % (ip, res["error"], errs), cfg["log_max_bytes"])
                if errs >= PROBE_ERROR_ALERT and not warned_probe_err:
                    warned_probe_err = True
                    log_line("ERROR", "探测持续异常，请检查 ping 命令与运行权限", cfg["log_max_bytes"])

            # 正常时按「探测间隔」，失联后进入「失联再验证」节奏
            wait = interval if kind == "ok" else recheck
            self._sleep(max(0.2, wait - (time.time() - cycle_start)))

        log_line("EVENT", "守护进程退出", self.get_cfg()["log_max_bytes"])

    # -- 手动探测 ------------------------------------------------------------
    def probe_now(self):
        """界面上的「立即探测」：结果同步到状态，但不计入自动关机计数。"""
        cfg = self.get_cfg()
        ip = (cfg.get("target_ip") or "").strip()
        if not ip:
            return {"ok": False, "error": "未配置目标 IP"}
        res = self.probe(ip, cfg["ping_timeout_sec"])
        ts = time.time()
        with self.lock:
            self.state["last_check_ts"] = ts
            self.state["last_rtt_ms"] = res["rtt_ms"]
            self.state["last_error"] = res["error"]
            if res["kind"] == "ok":
                self.state["consecutive_failures"] = 0
                self.state["consecutive_errors"] = 0
                self.state["last_ok_ts"] = ts
                if self.state["status"] not in ("shutting_down", "disabled"):
                    self.state["status"] = "up"
        log_line("TEST", "%s 手动探测 -> %s%s（不计入自动关机计数）"
                 % (ip, "通" if res["ok"] else ("探测异常" if res["kind"] == "error" else "不通"),
                    (" rtt=%.2fms" % res["rtt_ms"]) if res["rtt_ms"] else (" (%s)" % res["error"])),
                 cfg["log_max_bytes"])
        return {"ok": res["ok"], "kind": res["kind"], "rtt_ms": res["rtt_ms"],
                "error": res["error"], "cost_ms": res["cost_ms"]}

    def snapshot(self):
        cfg = self.get_cfg()
        with self.lock:
            st = dict(self.state)
            poweroff_allowed = self.poweroff_allowed
            poweroff_error = self.last_poweroff_error
        return {
            "app": APP_NAME,
            "version": app_version(),
            "hostname": socket.gethostname(),
            "config": cfg,
            "state": st,
            "grace_left": self.grace_remaining(cfg),
            "uptime_sec": int(time.time() - self.started_at),
            "now": time.time(),
            "log_path": LOG_PATH,
            "judge_seconds": int(cfg["recheck_sec"]) * int(cfg["max_failures"]),
            "poweroff_allowed": poweroff_allowed,
            "poweroff_error": poweroff_error,
            "grace_alert_after": PROBE_ERROR_ALERT,
        }


MONITOR = None  # type: Monitor


# -------------------------------------------------------------------- HTTP ---
def normalize_path(raw_path):
    """平台网关可能带 /v2/proxy/netsups 或 /netsups 前缀，这里统一剥掉。"""
    p = urlparse(raw_path).path or "/"
    for pre in PATH_PREFIXES:
        if p == pre:
            return "/"
        if p.startswith(pre + "/"):
            return p[len(pre):]
    return p


CTYPE = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


def _parse_cookie(header):
    parts = {}
    for item in (header or "").split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            parts[k.strip()] = v.strip()
    return parts


def check_platform_auth(headers):
    """按 TOS 规范 8.10 校验平台鉴权头。

    规则：Cookie 里存在平台 CSRF cookie 时，必须带同值的 X-Csrf-Token 请求头，
    缺一即视为未认证。若根本没有 CSRF cookie（平台未介入，例如直连端口调试），
    这一条不适用，交给上层的平台网关负责。
    """
    if not REQUIRE_CSRF:
        return True
    cookies = _parse_cookie(headers.get("Cookie", ""))
    cookie_csrf = cookies.get("X-Csrf-Token", "")
    header_csrf = headers.get("X-Csrf-Token", "")
    if not cookie_csrf and not header_csrf:
        return True
    if not header_csrf or not cookie_csrf:
        return False
    return header_csrf == cookie_csrf


class Handler(BaseHTTPRequestHandler):
    server_version = "NetUPS"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 访问日志不进 ping.log；systemd 侧另有 journald

    # -- helpers --
    def _cors(self):
        origin = self.headers.get("Origin")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Csrf-Token, Cookie")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self._cors()
            if ctype.startswith("text/html"):
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; "
                    "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                    "connect-src 'self'; frame-ancestors 'self'")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, code=200):
        self._send(code, json.dumps(data, ensure_ascii=False))

    def _error(self, code, msg):
        payload = {"ok": False, "error": msg}
        if code == 401:
            payload["is_login"] = False
        self._json(payload, code)

    def _authorized(self):
        if check_platform_auth(self.headers):
            return True
        self._error(401, "未认证：缺少平台会话或 X-Csrf-Token")
        return False

    # -- OPTIONS（平台要求后端处理预检） --
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    # -- GET --
    def do_GET(self):
        parsed = urlparse(self.path)
        path = normalize_path(self.path)
        query = parse_qs(parsed.query)

        if path == "/healthz":
            return self._send(200, "ok", "text/plain; charset=utf-8")

        if path.startswith("/api/") and not self._authorized():
            return

        if path in ("/", "/index.html"):
            return self._serve_file("index.html")
        if path == "/api/status":
            MONITOR.refresh_poweroff_permission()
            return self._json(MONITOR.snapshot())
        if path == "/api/logs":
            try:
                limit = int(query.get("limit", ["300"])[0])
            except (ValueError, IndexError):
                limit = 300
            limit = max(1, min(2000, limit))
            only = (query.get("only", ["all"])[0] or "all").lower()
            return self._json(read_logs(limit, only))
        if path.startswith("/api/"):
            return self._error(404, "unknown api")
        return self._serve_file(path.lstrip("/"))

    # -- POST --
    def do_POST(self):
        path = normalize_path(self.path)
        if path not in ("/api/config", "/api/probe", "/api/logs/clear"):
            return self._error(404, "unknown api")
        if not self._authorized():
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length > MAX_BODY_BYTES:
            return self._error(413, "请求体过大")
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return self._error(400, "请求体不是合法 JSON")

        if path == "/api/config":
            cfg, errors = validate_config(payload if isinstance(payload, dict) else {})
            if errors:
                return self._json({"ok": False, "error": "; ".join(errors)}, 400)
            MONITOR.set_cfg(cfg)
            return self._json({"ok": True, "config": cfg})
        if path == "/api/probe":
            return self._json(MONITOR.probe_now())
        ok, err = clear_logs()
        return self._json({"ok": ok, "error": err})

    # -- static --
    def _serve_file(self, rel):
        rel = rel or "index.html"
        root = os.path.abspath(WWW_DIR)
        full = os.path.normpath(os.path.join(root, rel))
        if not (full == root or full.startswith(root + os.sep)):
            return self._error(403, "forbidden")
        if not os.path.isfile(full):
            return self._error(404, "not found")
        ext = os.path.splitext(full)[1].lower()
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self._error(500, "read failed")
        return self._send(200, data, CTYPE.get(ext, "application/octet-stream"))


# ------------------------------------------------------------------ 日志读取 ---
_LOG_CACHE = {"key": None, "data": None}


def clear_logs():
    """清空 ping 日志及其轮转备份，并留下一条清空记录。"""
    try:
        for pth in (LOG_PATH, LOG_PATH + ".1"):
            if os.path.exists(pth):
                with open(pth, "w", encoding="utf-8"):
                    pass
        _LOG_CACHE["key"] = None
        log_line("EVENT", "日志已被手动清空（仅为本次操作记录）",
                 MONITOR.get_cfg()["log_max_bytes"] if MONITOR else DEFAULT_CONFIG["log_max_bytes"])
        return True, ""
    except OSError as exc:
        return False, str(exc)


def read_logs(limit=300, only="all"):
    """读取日志尾部；内容未变化时直接命中缓存，避免日志页轮询反复读盘。"""
    if only not in ("all", "fail", "ok"):
        only = "all"
    try:
        st = os.stat(LOG_PATH)
        key = (st.st_mtime_ns, st.st_size, limit, only)
        if _LOG_CACHE["key"] == key:
            return _LOG_CACHE["data"]
    except OSError:
        return {"ok": True, "total": 0, "lines": [], "log_path": LOG_PATH}

    items = []
    try:
        size = st.st_size
        with open(LOG_PATH, "rb") as f:
            if size > LOG_TAIL_BYTES:
                f.seek(size - LOG_TAIL_BYTES)
                f.readline()          # 丢弃可能被截断的首行
            content = f.read().decode("utf-8", "replace")
        for line in content.splitlines():
            if only == "fail" and not any(t in line for t in ("[FAIL", "[EVENT", "[TEST", "[ERROR")):
                continue
            if only == "ok" and "[OK" not in line:
                continue
            items.append(line)
    except OSError:
        pass

    data = {"ok": True, "total": len(items), "lines": items[-limit:], "log_path": LOG_PATH}
    _LOG_CACHE["key"] = key
    _LOG_CACHE["data"] = data
    return data


# --------------------------------------------------------------- HTTP server ---
class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 64        # 规范要求支持 100 并发，队列给足
    allow_reuse_address = True

    def get_request(self):
        req, _addr = super().get_request()
        return req, ("127.0.0.1", 0)


class SocketUnavailable(Exception):
    pass


# -------------------------------------------------------------------- main ---
def main():
    global CFG_PATH, LOG_PATH, WWW_DIR, PATH_PREFIXES, MONITOR

    ap = argparse.ArgumentParser(description="NetUPS - 网络UPS (TOS 7)")
    ap.add_argument("--socket", default="/var/api/netsups.sock",
                    help="监听 Unix socket（TOS 桌面应用形态）")
    ap.add_argument("--prefix", default="/netsups", help="网关可能残留的路径前缀")
    ap.add_argument("--www", default=WWW_DIR, help="前端静态目录")
    ap.add_argument("--data", default=os.environ.get("NETSUPS_DATA", STATE_DIR),
                    help="数据目录（config.json）")
    ap.add_argument("--log", default=os.environ.get("NETSUPS_LOG", LOG_DIR),
                    help="日志目录（ping.log）")
    ap.add_argument("--port", type=int, default=0, help="可选：监听 TCP 端口（仅本地调试）")
    ap.add_argument("--check", action="store_true", help="只做一次探测并输出结果")
    ap.add_argument("--detect-gateway", action="store_true", help="输出默认网关 IP")
    args = ap.parse_args()

    data_dir = os.path.abspath(args.data)
    log_dir = os.path.abspath(args.log)
    CFG_PATH = os.path.join(data_dir, "config.json")
    LOG_PATH = os.path.join(log_dir, "ping.log")
    WWW_DIR = os.path.abspath(args.www)

    # 前缀表：平台网关通常会把 /v2/proxy/netsups 剥掉再转发，但个别固件会整段
    # 透传，甚至变成 /netsups/api/xxx。三种形态都要能对上，多剥一层不花钱。
    prefixes = []
    for raw in (args.prefix, "/v2/proxy/" + APP_NAME, APP_NAME):
        if not raw:
            continue
        p = "/" + raw.strip("/")
        if p not in prefixes:
            prefixes.append(p)
    PATH_PREFIXES = tuple(prefixes)

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    if args.detect_gateway:
        print(detect_gateway())
        return 0

    cfg = load_config()
    if not os.path.exists(CFG_PATH):
        cfg["target_ip"] = cfg["target_ip"] or detect_gateway()
        atomic_write_json(CFG_PATH, cfg)
        log_line("EVENT", "已生成默认配置，探测目标=%s" % (cfg["target_ip"] or "(空)"))

    MONITOR = Monitor(cfg)

    # 启动时清理一次超过保留天数的历史日志
    removed = purge_old_logs(time.time() - LOG_RETENTION_DAYS * 86400)
    if removed:
        log_line("EVENT", "启动时清理 %s 天前日志 %s 条" % (LOG_RETENTION_DAYS, removed),
                 cfg["log_max_bytes"])

    if args.check:
        ip = cfg["target_ip"]
        if not ip:
            print("未配置目标 IP")
            return 2
        res = MONITOR.probe(ip, cfg["ping_timeout_sec"])
        print(json.dumps({"target": ip, "kind": res["kind"], "ok": res["ok"],
                          "rtt_ms": res["rtt_ms"], "error": res["error"],
                          "cost_ms": res["cost_ms"]}, ensure_ascii=False))
        return 0 if res["ok"] else 1

    threading.Thread(target=MONITOR.run, name="monitor", daemon=True).start()

    httpd = None
    if args.port:
        from http.server import ThreadingHTTPServer
        ThreadingHTTPServer.allow_reuse_address = True
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        httpd.daemon_threads = True
        journal("INFO", "main", "调试端口已启动: http://127.0.0.1:%s/" % args.port)
    else:
        sock_path = args.socket
        parent = os.path.dirname(sock_path)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                raise SocketUnavailable("无法创建 socket 目录 %s: %s" % (parent, exc))
        # 规范要求：bind 之前清掉陈旧的 socket 文件
        try:
            if os.path.exists(sock_path) or os.path.islink(sock_path):
                os.unlink(sock_path)
        except OSError as exc:
            raise SocketUnavailable("无法清理陈旧 socket %s: %s" % (sock_path, exc))
        httpd = ThreadingUnixHTTPServer(sock_path, Handler)
        try:
            os.chmod(sock_path, SOCKET_MODE)
        except OSError:
            pass
        journal("INFO", "main", "监听 unix socket: %s (mode %o)" % (sock_path, SOCKET_MODE))

    def _on_signal(signum, _frame):
        journal("INFO", "main", "收到信号 %s，准备退出" % signum)
        MONITOR.stop_evt.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        MONITOR.stop_evt.set()
        httpd.server_close()
        if not args.port:
            try:
                os.unlink(args.socket)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SocketUnavailable as exc:
        journal("ERROR", "main", str(exc))
        sys.exit(1)
