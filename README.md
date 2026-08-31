# QR Tunnel — 离线内网云桌面 Git 同步方案

> **0.5.0-dev（第二阶段第 2 项）**：在 0.4.0-dev 的文件日志、启动探针、能力协商和窗口启动顺序修复基础上，加入标准 QR Bulk 高吞吐路径（大响应按阈值切换、更大安全分块、`--disable-bulk` 回退），修复 `--max-pages` 按压缩后页数计算的问题，并完成 B 端渲染提速（整帧合成，现场实测单帧转换 7ms、约 6 倍吞吐）。**路线图后续项（PAUSE/RESUME、SQLite 断点恢复、自定义视觉编码、诊断包）已按用户决定暂停，先日常使用观察、按需再恢复**。


**适用场景**：开发机在外网，Git 服务器在内网，中间隔着一台云桌面（RDP）。云桌面能访问内网 Git，但开发机不能直接访问。

## 给 AI 协作工具：请先读 AGENTS.md

本仓库面向 AI 代理（Cursor、Copilot、Claude 等）编写了 **[AGENTS.md](AGENTS.md)**，其中包含代理开展工作所需的完整上下文：

- 项目架构与数据流（剪贴板单向 A→B + 屏幕二维码 B→A）
- **关键协议约束**（B 端剪贴板严格只读、A 端非空 `QRT:IDLE`、请求串行 + DONE 单写、zxing-cpp `.bytes`、Bulk 协商开关、整帧合成渲染勿回退）
- 目录结构与每个文件的职责
- 协议速查（数据页/Meta 页格式、MISSING/backfill、STOPPED 终止流程、探针能力协商）
- 运行与测试方法、部署拓扑（根目录 `config.yaml` + 启动脚本相对结构）
- 按事故历史整理的常见坑与修改守则（含版本三处同步、bat CRLF、日志纪律）

**使用方式**：AI 工具在本仓库上做任何分析、改代码、排查问题之前，先读取 `AGENTS.md`；人类新接手者也可先读它快速建立全貌，再按需查阅下文各章节与 `a_end/`、`b_end/` 源码。修改协议、帧流程、启动脚本或配置字段时尤其要遵守其中的约束，避免重蹈历史事故（B 端误写剪贴板、二进制页误用 `b.text`、VERSION 失同步等）。

## 开发仓库约定（重要）

今后二维码传输方案的**唯一开发、测试、提交、发布和生成部署包的仓库**是：

```text
/Users/cangwei/Personal.localized/develop/github/qr-git-tunnel
```

对应 GitHub 仓库为 [CangWeiohh/qr-git-tunnel](https://github.com/CangWeiohh/qr-git-tunnel)，默认分支为 `main`。

旧目录 `/Users/cangwei/Personal.localized/develop/python/qrtunnel` 仅作为历史本地副本和备份保留：

- 不再在旧目录中开发、修改或提交代码
- 不再从旧目录生成新的对外交付版本
- 如需查阅历史实现，只读对照即可
- 所有新改动、测试、提交和部署包都必须从本仓库产生

如果 AI 工具收到二维码传输方案的开发任务，应直接进入本仓库，不要继续使用旧的 `/develop/python/qrtunnel` 目录。

---

## 它是怎么工作的

```
IDEA 点 Fetch
  ↓ HTTP 请求
A 端代理（本机/虚拟机，监听 9999 端口）
  ↓ 把请求写入剪贴板（QRT:b64:...）
  ↓ SetForegroundWindow 把云桌面窗口拉到前台
  ↓ RDP 剪贴板自动同步到云桌面
B 端隧道（云桌面内）
  ↓ 读剪贴板，拿到请求
  ↓ 转发到内网 Git 服务器（如 192.168.21.14:8888）
  ↓ 拿到响应 → gzip 压缩 → 切块 → 生成多张二维码
  ↓ 全屏同时显示多张二维码（网格布局，自适应屏幕大小）
A 端代理
  ↓ 全屏截取云桌面画面
  ↓ zxing-cpp 解码所有二维码
  ↓ 集齐后组装 HTTP 响应
  ↓ 返回给 IDEA
IDEA 显示 Fetch 结果
```

**关键点**：剪贴板是 A→B 单向的（A 写的内容能同步到 B，但 B 写的内容不会同步回 A）。所以 B 端的响应只能通过屏幕二维码传回 A 端——A 端全屏截图，解码二维码，拿到数据。

---

## 当前版本与升级阶段

当前开发版本只记录在根目录 `VERSION`（唯一来源），A/B 两端不再各自携带同名文件；脚本读取优先序：自身目录 `VERSION` → 仓库根目录 `VERSION` → 内置默认值（与根目录保持一致）。

**版本升级流程（每次升级必须三处同步改）：**

1. `a_end/a_proxy.py` 内置 `VERSION = "..."`（`except OSError` 回退分支）→ 新版本号
2. `b_end/b_tunnel.py` 内置 `VERSION = "..."`（`except OSError` 回退分支）→ 新版本号
3. 根目录 `VERSION` 文件 → 新版本号

然后跑 `tests/test_upgrade.py`：`test_invariants` 会断言「根目录 VERSION 非空 + 两端内置默认值 == 根目录 VERSION + 两端目录不得再有 VERSION 文件」，三处漏改任何一处测试直接红。通过后部署：**把 `a_end/a_proxy.py`、`b_end/b_tunnel.py` 连同根目录 `config.yaml` 和对应的 `start_*.bat` 按相对目录复制到两台机器**；不要只拷贝 Python 文件，否则入口找不到配置。

### 第二阶段第 2 项（0.5.0-dev）：Bulk 高吞吐路径

Bulk **不引入自定义视觉编码**，仍使用现有 Version-40 标准 QR、多 QR 网格、MISSING 选择性重传和 zxing-cpp 解码，降低协议风险。它的作用是对较大的、压缩后页数超过阈值的响应，使用更大的 QR 安全分块，从而减少总页数；收益主要是减少帧数，不能替代自定义视觉编码。

- 普通响应继续走原来的 `--chunk`，普通 QR 是默认链路。
- 压缩后数据页数超过 `--bulk-threshold`（默认 400）且 A 端探针能力包含 `bulk` 时，B 端切换到 `--bulk-chunk`（默认 2900，不能超过 Version-40 QR 安全上限 2916）。
- B 端把 `bulk: true` 写入 Meta；A 端记录 `BULK` 日志并在摘要中记录 `bulk: true`。
- A 端是旧版/未协商/不带 `bulk` 能力时，B 端自动使用普通 QR，保证向后兼容。
- `--disable-bulk` 强制关闭 Bulk，便于现场回退和 A/B 对比。
- `--max-pages` 现在按**压缩后、最终传输分块方案**计算；Bulk 如果把页数降到上限内，会正常发送，不再提前返回 507。

> **2026-08-27 真实验证已通过**：B 端 `[BULK] bulk mode: chunk 2800->2900B, pages 5872->5669 (threshold 400)` 与 A 端 `[BULK] response received via BULK path` 闭环日志均出现，摘要带 `bulk:true`。实测上限：QR 通道约 500 页 / 1.4MB，更大的响应由 `--max-pages` 快速 507 并提示走 gitsync。

示例：

```powershell
# 默认：普通响应普通 QR，大响应在协商到 bulk 后自动切换
python b_tunnel.py --target 192.168.21.14:8888 --page-ms 300 --loops 5 --ack-ms 800

# 故障对比/强制回退到原普通 QR
python b_tunnel.py --target 192.168.21.14:8888 --disable-bulk

# 现场调参（仍须 bulk-chunk >= chunk 且 <= 2916）
python b_tunnel.py --chunk 2800 --bulk-threshold 400 --bulk-chunk 2900
```

### 第二阶段第 2 项附加（0.5.0-dev）：B 端渲染提速（整帧合成）

真实验证发现 B 端渲染瓶颈**不在二维码生成**（zxing-cpp 每页约 3ms），而在每页一次的 `ImageTk.PhotoImage`（PIL→Tk）转换——云桌面上每页约 220ms，8 页/帧约 2 秒，是 A 端只能收到约 4.5 页/秒的原因。

- **整帧合成**：B 端展示由「8 个网格 Label 各转一张 PhotoImage」改为「一帧所有二维码先按网格粘贴到一张灰度画布，整体做**一次** `ImageTk.PhotoImage` 转换，单个居中 Label 显示」。
- 转换总像素量基本不变（8 页 370×370 ≈ 一帧画布 1604×870），但每帧的 ImageTk/Tk 调用从 8 次降到 1 次；目标 3~6 倍（100 页 fetch 约 22s → 4~7s；16.4MB clone 约 21min → 4~7min）。
- **帧级 PhotoImage 缓存**（LRU 48）：各循环轮的帧内容完全一致，一帧只转换一次，后续轮直接命中；页面 PIL（未转换）缓存（LRU 256）保留，大 clone 内存可控。
- **纯 B 端改动**：协议、线格式、A 端代码、剪贴板方向都不变；`FEATURES` 增加 `compose` 仅作诊断标记。
- 现场验证：日志新增 `[RENDER]` 行——单帧超过 `--page-ms` 时告警并打印 compose/convert 毫秒数；每次播放结束输出渲染统计（帧数、缓存命中、平均 compose/convert、总耗时）。

> **2026-08-27 21:47 真实验证通过**：B 端 `[RENDER] render stats: frames=4, cache hits=4, compose avg=32ms, convert avg=7ms, total=0.2s`——单帧整幅 PhotoImage 转换仅 **7ms**（旧每页 ~220ms × 8 ≈ 1.76s/帧），证明旧瓶颈是 per-image 固定开销而非按像素计费（整幅画布像素更多反而 7ms）。渲染已完全隐藏在 `--page-ms` 之后，帧率转由 page-ms + A 端解码决定，理论上限约 **26 页/s**（原 ~4.5 页/s，约 6 倍）。实际部署参数（2026-08-27 定稿）：`--page-ms 300 --loops 5 --ack-ms 800 --max-pages 20000 --max-qr 8`——page-ms 300 稳定档（A 端解码余量足；想激进可试 200，按 MISSING/backfill 轮次判断）；max-pages 20000 放开 507 防线（≈58MB，超大响应走 QR 流式播放，一轮约 2.4min，配合 A 端超时中止可控）。

### 第二阶段第 1 项（0.4.0-dev）：文件日志 + 完整版本探针

- **两端旋转文件日志**：`log_event`/`blog_event` 的每一行控制台日志同时镜像写入各自 `logs/tunnel.log`（`RotatingFileHandler`，5 MiB × 3 个备份，UTF-8）。日志目录与摘要目录一致（脚本同级 `logs/`），便于排查长会话问题。
- **A 端启动探针**：A 端启动后在后台等待可信 HSRClient 窗口出现，再发送一次内部探针请求（`GET /__qrtunnel/probe`），复用剪贴板 + QR 通道，但**不**经过 IDEA、**不**写入 transfer-history。这样支持先启动 A 端、后启动 HSRClient，不会因探针过早发送而误判 legacy；代理 HTTP 监听不被等待阻塞。可用 `--no-probe` 关闭。
- **B 端探针应答**：B 端识别 `probe` 标记或保留路径 `/__qrtunnel/probe`，**本地**应答自己的 `{role, version, protocol, features, server_time}` JSON，绝不转发内网 Git；探针用 `probe_completed` 状态，不进入 transfer-history。
- **能力协商**：A 端解析探针响应存入 `_peer_capability`，校验协议/版本是否匹配；旧版 B 端不认识探针时（转发给 Git 返回 404/非 JSON），A 端降级为 `legacy` 标记并继续正常运行——为后续 Bulk 等特性提供能力开关依据。
- **启动顺序无关的窗口发现**：A 端不再按面积盲选任意全屏窗口（曾把 `TextInputHost.exe` / “Windows 输入体验”误当 HSRClient）。系统窗口加入黑名单；自动模式只有 HSRClient 进程或可信关键词命中才接受，否则保持 `TARGET_HWND=None`；后台 `qrtunnel-window-monitor` 周期重扫，支持先启动 A 端、后启动 HSRClient，无需重启 A 端。
- **焦点与本地应答修复**：A 端补齐 `_last_alt_sent` 的 `global` 声明，使 Layer-2 Alt 解锁真正执行；B 端探针/426 本地应答路径跳过空的 forward worker，避免 `None.join()` 崩溃循环。

> 本轮暂不启用 PAUSE/RESUME、SQLite 断点恢复、自定义视觉编码或修改 `start_a.bat` / `start_b.bat`。普通 QR、多 QR、选择性重传、`QRT:IDLE` 和 B 端剪贴板严格只读保持不变。

### 第一阶段（0.3.0-dev）

第一阶段在不改变普通 QR 主链路的前提下增加：

- A/B 启动时禁用 Windows 控制台 QuickEdit，避免误点控制台进入“选择”模式后暂停 Python 进程；失败只告警，不阻止启动。
- A/B 两端各自增加 Windows 命名互斥锁，避免多个同端进程同时争抢剪贴板、二维码窗口和截屏循环。
- A 端 `/__qrtunnel/health` 健康接口，返回版本、协议、能力、Horizon 窗口是否已匹配和最近一次传输摘要。
- A/B 请求 JSON 与响应 Meta 页带协议/版本元数据；B 端对携带元数据但协议或版本不匹配的请求返回 HTTP 426，对旧版无元数据请求保持兼容。
- A 端支持 Git/IDE 使用的 `HEAD` 请求，HEAD 响应只发送状态和响应头、不发送正文，并保留上游表示长度。
- B 端转发时强制 `Accept-Encoding: identity`，若上游仍返回 gzip/deflate 则先解压并移除 `Content-Encoding`，避免 A 端转发被错误标记的压缩正文。
- B 端对剪贴板请求做结构校验（dict、id/method/path 类型、headers 键值对、body 类型），畸形请求被忽略，不会反复触发同一错误。
- 两端将最近一次传输摘要原子写入各自 `logs/latest-transfer-summary.json`，并把终态追加到 `logs/transfer-history.jsonl`，不记录认证头值或 HTTP 正文。

> 本轮暂不启用 Bulk、自定义视觉编码、SQLite 断点恢复或修改 `start_a.bat` / `start_b.bat`。普通 QR、多 QR、选择性重传、`QRT:IDLE` 和 B 端剪贴板严格只读保持不变。

查看 A 端健康状态（把地址替换成 A 端实际 IP）：

```powershell
curl.exe http://127.0.0.1:9999/__qrtunnel/health
```

升级后启动日志应包含类似：

```text
[A][...][INFO][CONSOLE][req:--------] QuickEdit: DISABLED ...
[B][...][INFO][CONSOLE][req:--------] QuickEdit: DISABLED ...
[B][...][INFO][START][req:--------] single-instance mutex acquired
```

---

## 启动配置（config.yaml）

根目录 `config.yaml` 集中管理两端启动参数，采用扁平 `key: value` 格式，A/B 参数分别使用 `a_*` / `b_*` 前缀。根目录 `start_a.bat`、`start_b.bat` 会读取对应的 `a_python` / `b_python`，并把 `--config` 传给 Python 入口；A/B 入口自身再读取各自参数。

参数优先级为：**命令行 CLI > config.yaml > 程序内置默认值**。因此日常只需修改 `config.yaml`；现场临时调参仍可在启动命令后追加参数覆盖。例如：

```powershell
# 使用根目录配置
.\start_b.bat

# 临时覆盖配置（仅本次启动生效）
.\start_b.bat --page-ms 200
.\start_a.bat --window-keywords "HSRClient"
```

A 端使用无 pip 依赖的内置扁平 YAML 解析器，B 端同样保持单文件入口；不需要安装 PyYAML。部署时应保持以下相对结构，或将根目录中的 `config.yaml`、启动脚本和对应端目录一起复制：

```text
qr-git-tunnel/
├── config.yaml
├── start_a.bat
├── start_b.bat
├── a_end/a_proxy.py
└── b_end/b_tunnel.py
```

### 配置文件字段与命令行参数对应关系

`config.yaml` 中的字段去掉 `a_` / `b_` 前缀后，对应 Python 入口的 argparse 参数，例如 `b_page_ms` 对应 `--page-ms`、`a_listen` 对应 `--listen`。`a_python` / `b_python` 只由根目录启动脚本选择解释器；其余字段由 Python 入口读取。布尔字段 `a_no_probe`、`b_disable_bulk` 使用 `true` / `false`。
## 目录结构

下面是**完整 Git 仓库**的目录结构。Downloads 中生成的轻量部署包会保留运行所需的代码和配置，但会去掉 Python 安装包与 `b_end/whl/` 依赖文件；使用轻量包前需先在目标机器安装好 Python 和依赖。

```
qr-git-tunnel/
├── AGENTS.md                  # 给 AI 协作代理的项目导读（先读它）
├── VERSION                    # 当前开发版本标识（唯一来源）
├── config.yaml                # A/B 两端集中启动配置
├── start_a.bat                # 根目录 A 端启动脚本
├── start_b.bat                # 根目录 B 端启动脚本
├── a_end/                     # A 端（跑在能看见云桌面窗口的机器上）
│   ├── a_proxy.py             # HTTP 代理 + 截屏 + 解码
│   ├── python-3.11.9-embed-arm64.zip  # A 端离线 Python（ARM64 可嵌入版，可选）
│   └── requirements.txt       # 依赖列表
├── b_end/                     # B 端（跑在云桌面内，离线）
│   ├── b_tunnel.py            # 剪贴板监听 + 转发 + 二维码生成
│   ├── python-3.11.9-amd64.exe      # B 端离线 Python 安装包（x86-64）
│   └── whl/                   # B 端离线依赖 wheel（qrcode/pillow/pywin32/zxing-cpp）
│       └── requirements.txt   # 依赖列表
└── text2qr.html               # 独立二维码测试工具（可选）
```

### 轻量部署包生成规则（固定）

Downloads 中的轻量部署包必须严格对应生成时仓库的最新提交，不能沿用旧提交的包名或目录名。每次生成时按以下规则执行：

1. 进入唯一开发仓库，读取当前提交的 7 位短哈希：

   ```bash
   cd /Users/cangwei/Personal.localized/develop/github/qr-git-tunnel
   commit=$(git rev-parse --short=7 HEAD)
   ```

2. ZIP 文件名必须是 `qr-git-tunnel-<commit>.zip`，例如 `qr-git-tunnel-703c87d.zip`。
3. ZIP 解压后的唯一顶层目录必须是 `qr-git-tunnel-<commit>/`，例如 `qr-git-tunnel-703c87d/`；ZIP 内容必须来自同一个 `HEAD`，文件名、目录名和提交哈希不能混用。
4. 轻量包必须排除已安装的运行环境文件：
   - `a_end/python-3.11.9-embed-arm64.zip`
   - `b_end/python-3.11.9-amd64.exe`
   - `b_end/whl/`
   - `.mnemon/`、`__pycache__/`、`.DS_Store`、`logs/`
5. 轻量包必须保留运行所需的代码、配置、启动脚本、`requirements.txt`、README、AGENTS、VERSION 和测试文件。目标机器必须预先安装 Python 及 `requirements.txt` 中的依赖。
6. 生成后必须执行 ZIP 完整性检查，并确认只有一个顶层目录且上述排除项不存在。

推荐使用 Git 归档生成，保证包内容与提交一致：

```bash
cd /Users/cangwei/Personal.localized/develop/github/qr-git-tunnel
commit=$(git rev-parse --short=7 HEAD)
git archive --format=zip \
  --prefix="qr-git-tunnel-${commit}/" \
  -o "/Users/cangwei/Downloads/qr-git-tunnel-${commit}.zip" HEAD -- . \
  ':(exclude)a_end/python-3.11.9-embed-arm64.zip' \
  ':(exclude)b_end/python-3.11.9-amd64.exe' \
  ':(exclude)b_end/whl' \
  ':(exclude).mnemon'
```

> GitHub 完整仓库仍保留离线 Python 安装包和 `b_end/whl/`；上面的规则只适用于 Downloads 轻量部署包。

---

## 部署：先选你的拓扑

两种拓扑，代码完全相同，差别只在 A 端跑在哪、IDEA 填什么 URL。

| | **拓扑 A：Windows 物理机**（推荐，更简单） | **拓扑 B：Mac + Win 虚拟机** |
|---|---|---|
| 开发机 | Windows 10/11 x64 物理机 | Mac |
| A 端跑在哪 | **本机**（和 IDEA、云桌面客户端同机） | Win 虚拟机里（Parallels/VMware） |
| IDEA 填什么 | `http://127.0.0.1:9999/...` | `http://<虚拟机IP>:9999/...` |
| A 端监听 | `127.0.0.1:9999` | **必须** `0.0.0.0:9999` |
| Python | 官网 x64 安装包 | ARM64 embeddable 或 x64 安装包 |
| B 端离线件 | **完整仓库**内置（`b_end/python-3.11.9-amd64.exe` + `b_end/whl/`） | **完整仓库**内置（同左，x86-64） |

> Downloads 生成的是轻量部署包：压缩包目录名带对应提交短哈希（例如 `qr-git-tunnel-703c87d/`），不包含 Python 安装包和 `b_end/whl/`；目标机器需先自行安装 Python 及依赖。

---

## 拓扑 A 部署：Windows 物理机 + 云桌面

适用：日常开发就在 Windows 物理机上，本机装着 IDEA 和云桌面客户端（HSRClient）。

### 第 1 步：在云桌面部署 B 端（一次性）

云桌面完全离线，需要把 Python 和依赖包拷进去。

> **部署包说明：** GitHub 完整仓库包含 B 端离线 Python 安装包和 `b_end/whl/`；Downloads 生成的轻量部署包（目录名带提交短哈希）不包含这些文件。使用轻量包前，请先自行安装 Python 3.11 x86-64 及 `qrcode`、`pillow`、`pywin32`、`zxing-cpp` 依赖。

**1.1 准备 B 端 Python 和依赖：**

- **完整仓库：**可直接使用 `b_end/python-3.11.9-amd64.exe` 和 `b_end/whl/`，无需另行下载。
- **轻量部署包：**不含 Python 安装包和 wheel，请在有网络的机器下载 Python 3.11 x86-64 安装包，并执行 `pip download qrcode pillow pywin32 zxing-cpp -d <离线依赖目录>`，再将安装包和依赖拷入云桌面。

**1.2 把 B 端文件拷进云桌面：**

把以下内容从本机拷进云桌面（比如拷到桌面同一个目录）:
- 根目录 `config.yaml`、`start_b.bat`
- `b_end/` 整个文件夹（至少含 `b_tunnel.py`；完整仓库还含 Python 安装包和 `whl/`）

启动目录应保持为：

```text
<部署目录>\
├── config.yaml
├── start_b.bat
└── b_end\
    └── b_tunnel.py
```

**1.3 在云桌面安装 Python：**

使用已准备好的 Python 3.11 x86-64 安装包完成安装，**勾选 "Add Python to PATH"**。

**1.4 在云桌面安装依赖（离线安装，不联网）：**

```powershell
cd <离线依赖目录>
pip install --no-index --find-links=. qrcode pillow pywin32 zxing-cpp
```

> `--no-index --find-links=.` 表示只从离线依赖目录安装，不访问网络。

**1.5 启动 B 端：**

```powershell
cd C:\Users\<你的用户名>\Desktop\qr-git-tunnel
start_b.bat
```

> 修改根目录 `config.yaml` 中的 `b_target` 即可切换内网 Git 服务；参数统一从配置文件读取，临时追加的 CLI 参数优先覆盖。

看到 `B-end tunnel started` 和 `Waiting for clipboard requests` 就说明 B 端就绪了。**保持这个窗口不要关。**

### 第 2 步：在本机部署 A 端（一次性）

**2.1 安装 Python 3.11+ x64：**

从 https://www.python.org/downloads/windows/ 下载 x64 安装包，勾选 "Add Python to PATH"。

**2.2 拷贝 A 端代码和启动文件：**

先把本仓库根目录的 `config.yaml`、`start_a.bat` 与 `a_end/` 文件夹一起拷到本机同一个目录（例如 `C:\qr-git-tunnel\`），保持 `a_end\\a_proxy.py` 的相对路径。默认配置适用于拓扑 B；拓扑 A 请把 `a_listen` 改为 `127.0.0.1:9999`，并按本机 Python 修改 `a_python`。

**2.3 安装依赖：**

```powershell
cd C:\qr-git-tunnel\a_end
pip install -r requirements.txt
```

> 即安装 `mss`、`pillow`、`zxing-cpp`、`numpy` 四个包。

**2.4 启动 A 端代理：**

```powershell
cd C:\qr-git-tunnel
start_a.bat
```

> 拓扑 A 的 `config.yaml` 应配置 `a_listen: 127.0.0.1:9999`；参数统一从配置文件读取，临时追加的 CLI 参数优先覆盖。

看到 `Proxy listening on 127.0.0.1:9999` 就说明 A 端就绪了。**保持这个窗口不要关。**

**2.5 确保云桌面窗口可见：**

HSRClient（云桌面客户端）窗口必须**可见、不最小化**。全屏最佳。A 端需要截取云桌面窗口画面来解码二维码。

### 第 3 步：配置 IDEA

在 IDEA 的 Git 仓库设置里，把远程仓库 URL 改为：

```
http://<用户名>:<密码>@127.0.0.1:9999/<group>/<repo>.git
```

示例：
```
http://jiaxiaoxia2:****@127.0.0.1:9999/fsdp/cmus-service.git
```

- URL 内嵌账号密码，git 会自动做 Basic 认证
- 走本机回环 `127.0.0.1`，无需防火墙放行

### 第 4 步：使用

在 IDEA 里直接点 Git 按钮：
- **Fetch / Pull**：已验证端到端稳定可用
- **Push**：协议已支持，大包会慢一些
- **Clone（小仓库）**：直接走隧道即可
- **Clone（大仓库 / 完整历史）**：见下方「全量克隆」小节

完整一次 fetch 通常三步 HTTP，整体约 10-30 秒。

> **重要**：建议把 IDEA 的 `Settings → Version Control → Git → Check for incoming commits` 设为 **Never**。否则 IDEA 会后台周期性跑 `ls-remote`，每次都触发一轮隧道，B 端会莫名其妙弹二维码。

### 全量克隆（大仓库）

`git clone` 的第二步（`POST git-upload-pack`）会返回**整个仓库历史**，可能几十 MB，
需要几千张 QR 页，超出 B 端 `--max-pages`（默认 500）时会被主动回 **507**（`curl 22`，这是设计好的保护，不是隧道坏了）。如需完整历史，按以下步骤：

1. **Mac 上关闭 git 低速超时**（A 端整体缓冲响应，收集 QR 的几分钟内 git 收不到任何字节，默认 30s 就断了）：

   ```bash
   git config --global http.lowSpeedLimit 0
   git config --global http.lowSpeedTime 999999
   ```

2. **B 端临时调大上限**（每页 2800B，`--max-pages N` ≈ N×2800 字节；克隆需要 `页数 = 响应字节/2800`，给 1.5 倍余量）。例：16MB 需要约 5872 页，用 `--max-pages 8000`：

   ```powershell
   cd <部署目录>
   start_b.bat --max-pages 8000
   ```

   这里的 `--max-pages 8000` 只对本次启动覆盖 `config.yaml`；其他 B 端参数继续从配置文件读取。

3. 正常 `git clone`，B 端日志会显示预估时长（`est ~X.X min per full pass`）。播放期间云桌面窗口必须保持全屏可见、不可最小化，耐心等 DONE。

- 大响应按 8 页/帧播放，**B 端装了 zxing-cpp 后**每帧 ≈ page_ms（300ms），16MB ≈ 734 帧 ≈ 单遍 4-7 分钟；漏页会进回退重播，可能翻倍，属正常
- 若一屏停了远超 page_ms（几秒）才换下一屏，说明 B 端 QR 生成回退到了纯 Python `qrcode`（没装 zxing-cpp），大响应会非常慢
- B 端 QR 图片缓存已做 LRU 有界（256 张），万页级响应不会撑爆云桌面内存
- 仓库历史太大（> 30MB 量级）仍不现实（半小时以上），建议首次克隆用内网机直连做，隧道只跑增量 fetch

---

## 拓扑 B 部署：Mac + Win 虚拟机 + 云桌面

适用：日常开发在 Mac，用 Win 虚拟机跑云桌面客户端和 A 端代理。

### 第 1 步：在云桌面部署 B 端（一次性）

云桌面是 x86_64 架构，完全离线。

> **部署包说明：** GitHub 完整仓库包含 B 端 x86-64 Python 安装包和 `b_end/whl/`；Downloads 生成的轻量部署包（目录名带提交短哈希）不包含这些文件。使用轻量包前，请先在 Win 虚拟机准备好 Python 3.11 x86-64 及 `qrcode`、`pillow`、`pywin32`、`zxing-cpp` 依赖。

**1.1 准备 B 端 Python 和依赖：**

- **完整仓库：**可直接使用 `b_end/python-3.11.9-amd64.exe` 和 `b_end/whl/`，其中 wheel 已按 `win_amd64` 平台准备。
- **轻量部署包：**不含 Python 安装包和 wheel，请在 Win 虚拟机或其他有网络的 x86-64 机器下载 Python 3.11 x86-64 安装包，并执行 `pip download --platform win_amd64 --only-binary=:all: qrcode pillow pywin32 zxing-cpp -d <离线依赖目录>`，再将安装包和依赖拷入云桌面。

**1.2 拷进云桌面：**

- 本仓库根目录的 `config.yaml`、`start_b.bat`
- `b_end/` 整个文件夹（保持 `b_end\\b_tunnel.py` 的相对路径；至少含 `b_tunnel.py`）

**1.3 在云桌面安装 Python：**

使用已准备好的 Python 3.11 x86-64 安装包完成安装，**勾选 "Add Python to PATH"**。

**1.4 离线安装依赖：**

```powershell
cd <离线依赖目录>
pip install --no-index --find-links=. qrcode pillow pywin32 zxing-cpp
```

> `--no-index --find-links=.` 表示只从离线依赖目录安装，不访问网络。

> **zxing-cpp 是可选但强烈建议**：B 端 QR 生成默认用 zxing-cpp 的 C++ 编码器（每页 ~3ms），
> 没装则回退到纯 Python `qrcode`（每页 ~130ms 起）。大响应（几百页以上）纯 Python
> 编码会拖慢每一屏（云桌面实测一屏 8 码可停 8 秒），务必安装 zxing-cpp。

**1.5 启动 B 端：**

```powershell
cd <部署目录>
start_b.bat
```

> 修改根目录 `config.yaml` 中的 `b_target` 即可切换内网 Git 服务；参数统一从配置文件读取，临时追加的 CLI 参数优先覆盖。

看到 `B-end tunnel started` 和 `Waiting for clipboard requests` 就说明 B 端就绪了。**保持这个窗口不要关。**

### 第 2 步：在 Win 虚拟机部署 A 端（一次性）

**2.1 安装 Python：**

推荐用 Python 3.11 embeddable zip（ARM64），解压到 `C:\Python311`。

修改 `python311._pth` 文件：去掉 `#import site` 前面的注释。

启用 Windows 长路径：注册表 `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled` 设为 `1`。

**2.2 安装依赖：**

```powershell
# 安装 A 端依赖
C:\Python311\Scripts\pip.exe install mss pillow zxing-cpp numpy
```

> **解码库必须用 `zxing-cpp`**，不要用 `pyzbar`（ARM64 缺原生 DLL）。

**2.3 删除旧端口转发（如果 9999 被占）：**

```powershell
netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=9999
```

**2.4 启动 A 端（必须监听 0.0.0.0）：**

```powershell
cd <部署目录>
start_a.bat
```

> 根目录 `config.yaml` 的拓扑 B 默认已配置 `a_listen: 0.0.0.0:9999` 和 `a_python: C:\Python311\python.exe`；参数统一从配置文件读取，临时追加的 CLI 参数优先覆盖。

看到 `Proxy listening on 0.0.0.0:9999` 就说明 A 端就绪了。**保持这个窗口不要关。** A 端和 HSRClient 的启动顺序不限：如果先启动 A 端，启动日志会暂时显示 `RDP window not found` 和 `waiting for trusted HSRClient window before startup probe`；HSRClient 出现后后台窗口监视器会自动重扫，正常应出现 `rescanned RDP window, new hwnd=...`，随后才发送探针。不得出现匹配 `TextInputHost.exe` / “Windows 输入体验”。

**2.5 确保虚拟机内云桌面窗口可见、不最小化。**

### 第 3 步：配置 IDEA（Mac）

```
http://<用户名>:<密码>@<虚拟机IP>:9999/<group>/<repo>.git
```

示例（Parallels 共享网络下虚拟机 IP 常见是 `10.211.55.x`）：
```
http://jiaxiaoxia2:****@10.211.55.4:9999/fsdp/cmus-service.git
```

### 第 4 步：使用

同拓扑 A。

---

## 通信协议

### 请求（A→B，剪贴板）

```
QRT:b64:<base64(JSON{id,method,path,headers,body,retry})>
```

A 端把 HTTP 请求编码成 JSON，base64 后写入剪贴板。`retry` 是发送序号——每次重写时递增，防止 RDP 对相同内容去重不触发同步。

### 确认（B→A，屏幕 ACK 码）

```
QRT-ACK:<req_id>
```

B 端收到请求后先弹出一个小型置顶二维码。A 端写完剪贴板后 3 秒内没在屏幕上看到 ACK（或任何响应页），就重写剪贴板，最多 8 次。B 端按 `req_id` 去重，重复送达无副作用。

### 缺失页反馈（A→B，剪贴板）

```
QRT:MISSING:<req_id>:<页号列表>
```

A 端在收集响应页期间每 500ms 写入一次，列出还没收到的页号（0=meta 页，1..N=data 页，支持范围语法如 `0-3,5,7-10`）。B 端每轮播放结束后读取此信号，后续轮次只播缺失页。这避免了重复播放已收到的页，大幅减少大响应的传输时间。

### 提前完成（A→B，剪贴板）

```
QRT:DONE:<req_id>
```

A 端集齐全部页后立即写入。B 端检测到后立即停止播放（跳过收尾缓冲），先清理旧控制信号，再短暂显示 `QRT-STOPPED:<req_id>` 屏幕确认码；A 端看到确认后立即放行下一个 Git HTTP 请求，未看到则保守等待最多 2.2 秒后兼容旧版本。DONE 信号丢失时 B 端照常补播，只损失提速、不影响正确性。

### 停播确认（B→A，屏幕二维码）

```
QRT-STOPPED:<req_id>
```

B 端收到 DONE 并关闭响应二维码窗口后显示约 500ms。它用于替代 A 端原来的固定 2 秒等待，减少 Git 401 认证等连续请求之间的空档。

### 取消（A→B，剪贴板）

```
QRT:CANCEL:<req_id>
```

A 端检测到 IDEA 断开连接（取消 fetch）后写入。B 端收到后立即停止二维码播放。A 端每 500ms 检测一次客户端连接状态，检测到断连后约 1.5 秒内叫停 B 端。

### 响应（B→A，全屏二维码）

- **Meta 页**（JSON 文本）：`status` / `headers` / `chunks` / `gzip` / `raw_len`；自动使用较小 QR Version，模块更大；第一屏有空位时最多复制两份，提高 RDP 环境下的首轮识别率
- **Data 页**（二进制）：`[0x01][seq:4B BE][id_hex:32B ASCII][chunk]`
- Data 页使用 QR Version 40 + ECC L，约 2953 字节/帧，每帧承载 2800 字节
- **多 QR 网格**：B 端根据屏幕尺寸同时显示多张二维码（1920×1080 上 6-8 张），A 端一次截屏解码全部；缺失页较少时自动改用更大的居中网格
- QR 图片只按整数倍、最近邻方式缩放，避免模块边缘被插值模糊
- 二进制页必须用 zxing-cpp 的 `b.bytes`（`b.text` 会损坏二进制）

### 控制台日志格式

A、B 两端现在使用统一格式，且每一行同时镜像写入各自脚本同级 `logs/tunnel.log`（旋转日志，5 MiB × 3 备份）：

```text
[A][10:52:11][INFO][SEND][req:cb460506] GET /repo/info/refs attempt 1/8
[B][10:52:12][INFO][HTTP][req:cb460506] response HTTP 200, body=37286B
```

字段含义：

| 字段 | 含义 |
|------|------|
| `A` / `B` | 日志来源；A 是 IDEA 所在端，B 是云桌面端 |
| 时间 | 当前端本地时间 |
| `INFO` / `WARN` / `ERROR` | 普通信息 / 可恢复异常 / 当前请求失败或需要关注 |
| 阶段 | `SEND` 发请求、`ACK` 请求已送达、`HTTP` 访问内网 Git、`ENCODE` 编码页面、`RECV` 接收二维码、`DISPLAY` 播放二维码、`DONE` 完成、`CANCEL` 取消、`STOP` 停播交接 |
| `req:XXXXXXXX` | 当前请求 ID 前 8 位；A、B 两端相同的短 ID 属于同一个 HTTP 请求 |

### 常见日志怎么看

正常请求通常按这个顺序出现：

```text
A SEND → B REQ → B ACK/HTTP → B ENCODE/DISPLAY → A RECV → A DONE → B DONE
```

- A 端看到 `no ACK ... rewriting request`：说明 RDP 剪贴板同步可能丢了一次，自动重试，不一定是失败。
- A 端看到 `collection: meta=YES, chunks=4/5, missing=[3]`：表示只缺第 3 个数据页，B 端后续会只补播该页。
- A 端看到 `B-end STOPPED confirmed`：表示当前请求已经完成，下一请求可以开始。
- B 端看到 `backfill ... missing pages`：表示正在补播 A 端尚未解析成功的二维码。
- `WARN` 需要关注但通常会自动恢复；`ERROR` 通常表示本次请求会返回 502/504，或需要检查环境。

启动探针相关日志：

```text
[A][...][INFO][PROBE][req:--------] starting startup probe (id=XXXXXXXX)
[A][...][INFO][PROBE][req:XXXXXXXX] B-end peer: version=0.5.0-dev, protocol=qrtunnel-qr-1, features=[...], compat=OK
[B][...][INFO][DISPLAY][req:XXXXXXXX] loop 1: playing all pages
```

- A 端 `B-end peer ... compat=OK`：探针成功，两端协议/版本一致，能力协商完成。
- A 端 `assuming legacy peer`：B 端是旧版（不认识探针），降级为 legacy 继续运行，不影响普通请求。
- 启动后约几秒内 A 端应出现 `PROBE` 日志；若想完全关闭，用 `--no-probe`。

排障时优先用同一个 `req:XXXXXXXX` 在 A、B 两个窗口中搜索，不要只按时间猜测请求对应关系。

---

## 可调参数

### A 端参数

| 参数 | 内置默认值 | 当前 `config.yaml` | 说明 |
|------|------------|-------------------|------|
| `--listen` | `127.0.0.1:9999` | `0.0.0.0:9999` | 监听地址。拓扑 A 建议改回 `127.0.0.1:9999`，拓扑 B 必须 `0.0.0.0:9999` |
| `--chunk` | `2800` | `2800` | 每帧 QR 承载字节数（**必须和 B 端一致**） |
| `--display-index` | `-1` | `-1` | 截屏显示器索引（-1=自动，0=主屏，1=副屏） |
| `--window-keywords` | 空 | 空 | HSRClient 匹配失败时的标题关键词回退 |
| `--no-probe` | 关闭 | `false` | 跳过启动时向 B 端发送的版本/能力探针 |

### B 端参数

| 参数 | 内置默认值 | 当前 `config.yaml` | 说明 |
|------|------------|-------------------|------|
| `--target` | `192.168.21.14:8888` | `192.168.21.14:8888` | 内网 Git 服务器地址 |
| `--page-ms` | `200` | `300` | 每屏二维码停留毫秒（稳定运行推荐 300） |
| `--chunk` | `2800` | `2800` | 每帧字节数（**必须和 A 端一致**） |
| `--loops` | `3` | `5` | 循环轮数；配合选择性重传，后续轮次只播缺失页 |
| `--ack-ms` | `800` | `800` | 请求确认 QR 的最短显示时间；期间并行访问内网 Git |
| `--max-pages` | `500` | `20000` | 单响应最大 QR 页数，按压缩后/最终 Bulk 分块计算 |
| `--max-qr` | `0` | `8` | 每帧最大 QR 数（0=自动，1920×1080 上当前固定 8） |
| `--min-box-size` | `2` | `2` | 每 QR 模块最小像素（解码不稳时调到 3） |
| `--display` | `tkinter` | `tkinter` | 显示方式（`tkinter` 全屏或 `html`） |
| `--disable-bulk` | 关闭 | `false` | 强制回退普通 QR，便于故障对比 |
| `--bulk-threshold` | `400` | `400` | 压缩后页数超过阈值时启用 Bulk |
| `--bulk-chunk` | `2900` | `2900` | Bulk 每页字节数，必须不超过 2916 且不小于 `--chunk` |
| `--bulk-threshold` | `400` | 压缩后普通路径超过此页数才尝试 Bulk |
| `--bulk-chunk` | `2900` | Bulk 每个 QR 的响应字节数，范围为 `--chunk` 至 `2916` |

### 推荐配置

```powershell
# B 端
python b_tunnel.py --target 192.168.21.14:8888 --page-ms 300 --loops 5 --ack-ms 800

# A 端（拓扑 A）
python a_proxy.py --listen 127.0.0.1:9999

# A 端（拓扑 B）
python a_proxy.py --listen 0.0.0.0:9999
```

---

## 限制

- 仅支持 git HTTP(S)，不支持 SSH
- HSRClient 云桌面窗口必须保持可见（不可最小化）
- 不适合 clone / 全量大传输：超过 `--max-pages`（默认 500）会直接回 507；小仓库或临时调大上限可走隧道，超大历史（>30MB）首次克隆建议内网机直连
- A、B 端 `--chunk` 参数必须一致
- 请求串行处理（剪贴板 + 二维码是单通道）
- 剪贴板 A→B 单向，B→A 只有屏幕

---

## 常见问题

| 现象 | 处理 |
|------|------|
| fetch/clone 时听到与翻码同频的"嗒嗒"声 | HSRClient 的剪贴板粘贴/同步提示音（A 端每 500ms 写一次 MISSING）；关闭该音效即可，非隧道故障 |
| clone 一屏停几秒才换屏 | B 端没装 zxing-cpp，QR 编码回退到纯 Python qrcode；离线安装 `cd <部署目录>\b_end\whl && pip install --no-index --find-links=. zxing-cpp` |
| Mac 连不上 9999（拓扑 B） | A 端改 `--listen 0.0.0.0:9999`；删旧 `netsh portproxy`；确认虚拟机 IP |
| 本机 IDEA 连不上（拓扑 A） | 确认 A 端已启动；URL 用 `127.0.0.1` 而不是局域网 IP |
| B 端收不到请求 | 确认 HSRClient 未最小化；看 A 端是否打印 `[focus] brought RDP...` |
| 只解到 meta、data 永远不齐 | 确认 A 端用 `zxing-cpp` 的 `.bytes`（不要 pyzbar / `.text`） |
| `Malformed encoding found in chunked-encoding` | A 端需过滤 `Transfer-Encoding` 等响应头（已内置） |
| IDEA 卡住 / 认证丢失 | 确认两端都是最新代码；A 端日志 `No ACK after 3.0s, rewriting clipboard` 是正常自动补发 |
| B 端无人操作也弹二维码 | IDEA 后台「Check for incoming commits」在跑 `ls-remote`；设为 Never |
| clone / 大响应收到 507 | 超过 `--max-pages` 被 B 端拒绝；调大上限重试，或首次克隆用内网机直连 |
| clone 中途 curl 22「Operation too slow」 | git 低速超时：A 端缓冲整包响应，先设 `http.lowSpeedLimit 0` + `http.lowSpeedTime 999999` |
| clone 后 fetch 时 B 端仍在播放上一轮二维码 | B 端可能丢失了 `QRT:DONE`，在回放上一轮全部 QR 页；已修复：播放中检测不同 req_id 的新 `QRT:b64:` 立即中止，连续无 MISSING 则停止回放 |
| 第一次 fetch 正常、等一会后第二次 fetch 无反应，B 端停在 `waiting for request` | 请求根本没有到达云桌面剪贴板：旧版 A/B 收尾都写空剪贴板，且 B 端会反向写清理值，可能令 HSRClient 双向剪贴板通道失步。最新修复：A 端以非空 `QRT:IDLE` 收尾；B 端严格只读剪贴板；B 端每 10 秒心跳输出 `clipboard='...'`。需同时更新两端 |
| 拉取差异大收到 504 | 页太多漏帧收不齐（连续 120s 无新页即放弃）；或 B 端 `--page-ms 300` 提速 |
| B 端播放中想手动停 | 按 **Esc** |
| 多屏截不到 QR | 确认云桌面 QR 在主显示器；或 A 端加 `--display-index` 指定 |
| IDEA 取消 fetch 后卡很久 | 确认两端都是最新代码（含取消机制）；旧版不检测断连会卡满 120s |
| 多 QR 模式解码不靠谱 | B 端加 `--min-box-size 3` 增大每模块像素；或 `--max-qr 4` 减少 QR 数 |
