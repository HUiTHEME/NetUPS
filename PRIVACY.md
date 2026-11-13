# 隐私政策 / Privacy Policy

**应用 / App:** 网络UPS（Network UPS，App ID: `netsups`）
**发布者 / Publisher:** 疯狂的大叔（HUiTHEME）
**最后更新 / Last updated:** 2026-11-13 · 适用于 1.0.2 及之后的版本 / applies to 1.0.2 and later

---

## 中文

### 一句话概括

本应用**不收集、不上传、不共享任何数据**。所有运行信息都留在你自己的 NAS 上。

### 一、本应用会接触哪些数据

全部数据都只存在于本机，且由应用自身产生：

| 数据 | 内容 | 存放位置 |
|---|---|---|
| 应用配置 | 探测目标 IP、探测间隔、失联再验证间隔、单次超时、连续失败次数、开机宽限期、启用开关 | `/usr/local/netsups/data/config.json` |
| 运行日志 | 每次探测的时间、目标 IP、往返延迟、成功/失败原因、关机事件 | `/usr/local/netsups/logs/ping.log`（按体积轮转） |
| 系统开机时长 | 仅读取 `/proc/uptime`，用于计算开机宽限期 | 不落盘 |

上述文件仅本机 `netsups` 服务账号与系统管理员可读，不会通过网络发送到任何地方。

### 二、网络行为

- 唯一的主动网络行为：按你设定的间隔，向**你自己填写的目标 IP** 发送 ICMP 请求（调用系统 `ping`），用于判断该设备是否在线。
- 不连接互联网，不访问任何远端服务器，不发送遥测、统计、崩溃报告。
- 不包含任何第三方 SDK、广告组件或分析工具。
- 应用不监听任何 TCP 端口，前后端通信只走本机的 Unix Socket（`/var/api/netsups.sock`），由 TOS 平台代理转发。

### 三、权限用途

| 权限 | 用途 |
|---|---|
| 应用目录读写 | 保存上述配置与日志 |
| 发送 ICMP（系统 `ping`） | 探测目标设备是否在线 |
| `org.freedesktop.login1.power-off` 等关机动作（polkit） | 判定市电中断后，通过 systemd 正常关机 |

关机授权是**动作级**的：只授予本应用账号执行登录管理器关机动作的权利，不涉及 root、sudo 或其他任何系统操作；卸载时该授权文件会被一并删除。

### 四、数据共享与披露

不共享、不出售、不转让给任何第三方。除非你本人导出文件，否则没有任何数据会离开这台设备。

### 五、保留与删除

- 配置与日志在你主动修改、清空（应用内「清空日志」）或日志达到轮转阈值之前一直保留在本机。
- **卸载应用时，配置与日志会被一并删除**，不留残余。
- 本应用不建立任何云端副本，因此不存在"需要向服务端申请删除"的数据。

### 六、儿童隐私

本应用是 NAS 系统工具，不面向儿童，也不收集任何年龄、身份或联系方式信息。

### 七、政策变更

本政策随应用版本更新，历史版本可在仓库提交记录中查阅。涉及数据处理方式的实质性变更会在应用更新说明中提示。

### 八、联系方式

问题、疑问或数据相关请求：<https://github.com/HUiTHEME/NetUPS/issues>

---

## English

### Summary

This application **collects nothing, uploads nothing, and shares nothing**. Every piece of runtime information stays on your own NAS.

### 1. What data the app touches

All of it is produced locally by the app itself:

| Data | Content | Location |
|---|---|---|
| Configuration | Target IP, probe interval, re-check interval, per-probe timeout, failure threshold, startup grace, on/off switch | `/usr/local/netsups/data/config.json` |
| Runtime log | Timestamp, target IP, round-trip time, success/failure reason and shutdown events of each probe | `/usr/local/netsups/logs/ping.log` (rotated by size) |
| System uptime | Read from `/proc/uptime` only, to compute the startup grace period | Not persisted |

These files are readable only by the local `netsups` service account and the system administrator. They are never transmitted anywhere.

### 2. Network behaviour

- The only outbound activity: ICMP requests (via the system `ping`) to **the target IP you configured**, at the interval you set.
- No internet access, no remote servers, no telemetry, no usage statistics, no crash reporting.
- No third-party SDKs, advertising components or analytics.
- The app listens on no TCP port; the front end talks to the back end over a local Unix socket (`/var/api/netsups.sock`) proxied by the TOS platform.

### 3. Why permissions are needed

| Permission | Purpose |
|---|---|
| Read/write the app directory | Store the configuration and log described above |
| Send ICMP (system `ping`) | Detect whether the target device is reachable |
| `org.freedesktop.login1.power-off` and related (polkit) | Perform a clean system shutdown through systemd once mains power loss is confirmed |

The shutdown grant is **action-scoped**: it allows this one app account to request a login-manager power-off. It grants no root, no sudo and no other system operation, and the policy file is removed on uninstall.

### 4. Sharing and disclosure

No sharing, no selling, no transfer to third parties. Unless you export a file yourself, no data leaves the device.

### 5. Retention and deletion

- Configuration and logs remain on the device until you change them, clear the log in the app, or the log reaches its rotation threshold.
- **Uninstalling the app deletes the configuration and logs as well**, leaving no residue.
- There is no cloud copy, so there is no server-side data to request deletion of.

### 6. Children's privacy

This is a NAS system utility. It is not directed at children and collects no age, identity or contact information.

### 7. Changes to this policy

This policy is updated together with the app; previous versions remain visible in the repository history. Material changes to data handling will be noted in the release notes.

### 8. Contact

Questions or data-related requests: <https://github.com/HUiTHEME/NetUPS/issues>
