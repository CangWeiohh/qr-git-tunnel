# AGENTS.md — 给 AI 协作代理的项目导读

> 本文件面向将在这个仓库上工作的 AI 代理（或任何新接手的人）。**先读本文件，再读代码**。README.md 是给人类用户的快速上手；本文件是给代理的完整上下文：架构、协议约束、目录职责、部署拓扑、常见坑与修改守则。

## 一句话

这是一个 **离线内网云桌面 Git 同步方案（QR Tunnel）**：在外网的开发机（Mac/IDEA）无法直连内网 Git，但通过一台能访问内网 Git 的云桌面（RDP）中转。A 端（Windows VM 或物理机）提供 HTTP 代理，把 Git 请求经 **RDP 剪贴板（单向 A→B）** 传到 B 端（云桌面），B 端转发到内网 Git；响应经 **全屏二维码（单向 B→A）** 传回 A 端解码还原。对 Git/IDEA 来说，它只是一个普通 HTTP 代理。

```text
Git / IDEA ──HTTP──> A proxy ──剪贴板 QRT:b64:──> B tunnel ──HTTP──> 内网 Git
                          <────── 屏幕二维码（多 QR 网格）──────────┘
```

## 开发仓库约定（重要）

- **今后二维码传输方案的唯一开发、测试、提交和发布仓库**：`/Users/cangwei/Personal.localized/develop/github/qr-git-tunnel`
- GitHub 远程：`https://github.com/CangWeiohh/qr-git-tunnel.git`，默认分支为 `main`
- 新版本应从本仓库提交、测试并生成部署包；旧目录已删除，不存在可供参考的旧工作副本

## 最重要的约束（改代码前必须懂）

1. **剪贴板严格单向 A→B**：云桌面策略下 B 端写剪贴板不会同步回 A（B→A 剪贴板被禁）。所以 **B 端代码绝不写剪贴板**（审计断言：`SetClipboardData`/`EmptyClipboard`/`GlobalAlloc` 在 b_tunnel.py 中禁止出现，`tests/test_upgrade.py::test_invariants` 会检查）。B→A 的唯一信道是屏幕二维码，必须用 zxing-cpp 解码。
2. **A 端绝不写空剪贴板**：写完请求后若无内容，写非空 `QRT:IDLE` 基线标记，避免某些剪贴板实现把空字符串当“没变化”而跳过同步。
3. **请求串行 + DONE 单写**：同一时刻只处理一个 Git 请求，A 端用 `_request_lock` 串行化；响应集齐后只写一次 `QRT:DONE:<req_id>`，DONE 丢失时 B 端照常补播（只损失提速，不影响正确性）。
4. **协议标记（剪贴板，全部 A→B）**：请求 `QRT:b64:<base64(JSON)>`；缺失反馈 `QRT:MISSING:<req_id>:<页号列表>`（支持范围语法 `0-3,5,7-10`）；完成 `QRT:DONE:<req_id>`；取消 `QRT:CANCEL:<req_id>`；空闲 `QRT:IDLE`。
5. **协议标记（屏幕，B→A）**：ACK `QRT-ACK:<req_id>`；停播确认 `QRT-STOPPED:<req_id>`；数据页为二进制二维码（见下）。
6. **二进制 QR 必须用 zxing-cpp 的 `.bytes`**：数据页是 `[0x01][seq 4B 大端][id_hex 32 ASCII][chunk]` 的二进制，`b.text` 会损坏二进制（真实踩坑）。文本型内容（meta/信号）才走 text 渲染。
7. **Bulk 高吞吐路径是协商式的**：只有 A 端探针 `features` 明确含 `bulk`、压缩后普通页数超过 `--bulk-threshold`（默认 400）、且未 `--disable-bulk` 时才启用，把每页分块从 2800B 提到 2900B（Version-40 QR ECC-L 数据上限 2916B）。旧版/legacy/未协商的 A 端自动回退普通 QR，保证向后兼容。
8. **渲染瓶颈已由整帧合成修复**：B 端 `show_pages` 先把一帧所有 QR 粘贴到一张灰度画布（`compose_qr_frame`），只做一次 `ImageTk.PhotoImage` 转换（convert avg≈7ms/帧），配页面 PIL LRU 256 + 帧级 PhotoImage LRU 48。**不要退回逐页转 PhotoImage**（旧实现每页 ~220ms，8 页/帧 ~2s，是历史吞吐瓶颈）。
9. **启动探针**：A 端启动后等待可信 HSRClient 窗口出现，再发一次 `GET /__qrtunnel/probe`（或 `probe:true`），B 端**本地**应答 `{role, version, protocol, features, server_time}`，绝不转发内网 Git。探针决定能力协商，版本/协议不匹配返回 HTTP 426。

## 目录结构

| 路径 | 职责 |
|---|---|
| `config.yaml` | 全部可调参数集中地（`a_*` / `b_*` 前缀），优先级 **CLI > config.yaml > 内置默认值** |
| `start_a.bat` / `start_b.bat` | 根目录启动脚本：读 `a_python`/`b_python` 选解释器，透传 `--config`。**必须保持 CRLF**（`.gitattributes` 强制） |
| `VERSION` | 版本号唯一来源（根目录）；两端内置默认值必须与其一致（`test_invariants` 断言） |
| `a_end/a_proxy.py` | A 端入口：HTTP 代理（`ThreadingHTTPServer`）+ 全屏截屏 + zxing-cpp 解码 + HSRClient 焦点自动化 + MISSING/DONE/CANCEL/IDLE 写剪贴板；内置零依赖 YAML 解析器（无 PyYAML，A 端嵌入 Python 无 pip） |
| `b_end/b_tunnel.py` | B 端入口：剪贴板监听（只读）+ 转发内网 Git + 响应 gzip/切块/QR 生成 + 多 QR 网格播放 + 选择重传 + 整帧合成渲染；同样内置 YAML 解析器 |
| `tests/test_upgrade.py` | 回归测试（macOS 可跑，AST 抽取纯函数隔离验证）：协议解析校验、meta 页校验、范围编码、Bulk 计划、VERSION/config 不变式、窗口选择守卫、探针、`test_compose_frame`（需本地装 Pillow） |
| `text2qr.html` | 独立二维码测试工具（可选，离线单页） |
| `a_end/requirements.txt` / `b_end/` 离线 wheel | 依赖说明与离线安装包：A 端 mss/pillow/zxing-cpp/numpy；B 端 qrcode/pillow/pywin32（zxing-cpp 强烈建议，纯 Python qrcode 编码慢一个数量级） |

## 协议速查

**数据页格式**：`[0x01][seq 4B BE][id_hex 32 ASCII][chunk bytes]`，页头 37B；Version-40 QR byte-mode ECC-L 上限 2953B → 数据页净荷上限 2916B。普通 `--chunk` 默认 2800、Bulk `--bulk-chunk` 默认 2900。

**Meta 页**（JSON 文本，页面 0）：`{meta:true, id, status, headers, chunks, gzip, raw_len, bulk?}`。B 端 gzip 压缩决策由 `_compress_plan()` 统一（修复过 `--max-pages` 按原始大小误算导致压缩响应误 507 的问题）。

**播放与重传**：B 端按 `--page-ms`（稳定档 300ms）逐帧播放，每帧最多 `--max-qr`（当前 8）张 QR；A 端每 500ms 写一次 `QRT:MISSING` 反馈缺失页，B 端后续轮次（`--loops`，当前 5）只播缺失页，播完固定轮次后继续 backfill 缺失页直到 DONE/CANCEL/Esc。

**响应终止**：A 端集齐所有页 → 写 `QRT:DONE` → B 端停播并清剪贴板控制信号 → 短暂显示 `QRT-STOPPED` 屏幕码 → A 端确认或保守等待 ≤2.2s 后放行下一个请求。

## 运行与测试

```bash
# 本地回归测试（macOS 可直接跑；test_compose_frame 需要本机有 Pillow）
python3 tests/test_upgrade.py
```

启动（真实环境，Windows）：

```text
start_b.bat   # B=云桌面，先启；转发 b_target（192.168.21.14:8888）
start_a.bat   # A=Windows VM，后启；监听 a_listen（0.0.0.0:9999）
```

等价手动：`python b_end/b_tunnel.py --config config.yaml`、`python a_end/a_proxy.py --config config.yaml`。临时调参可追加 CLI 覆盖，如 `start_b.bat --page-ms 200`。

## 部署拓扑

- **A 端**：Windows ARM VM 或物理机，`C:\Python311`（A 端 `a_python`，embeddable），目录含 `a_end/`；IDE 远程 URL 示例 `http://<用户>:<密码>@<VM IP>:9999/<组>/<仓库>.git`。
- **B 端**：离线云桌面（x86_64），`python`（`b_python` 留空走 PATH），目录含 `b_end/`。
- **部署必须保持相对结构**（根目录 `config.yaml` + `start_*.bat` + 对应 `a_end/`/`b_end/`），不能只拷 Python 单文件，否则入口自动找不到配置。可整目录复制。
- 启动顺序：B 先后 A 均可，但 A 端启动探针会等待可信 HSRClient 窗口出现；**保持 A 端 VM/HSRClient 窗口前台**，失焦会导致剪贴板写入不传播（不是协议 bug）。
- 日志/摘要在各端脚本同级 `logs/`：`tunnel.log`（旋转 5MiB×3）、`latest-transfer-summary.json`、`transfer-history.jsonl`。

## 常见坑（按事故历史）

1. **B 端写剪贴板 = 数据永远不会到 A**（B→A 剪贴板被禁）。B 端只读剪贴板，一切 B→A 走屏幕二维码。
2. **二进制页用 `b.text` 解码** → 数据损坏。必须 `b.bytes`。
3. **`--max-pages` 按原始字节算** → gzip 压缩大响应误 507。已修：按压缩后、最终分块方案的页数计算。
4. **每页单独 `ImageTk.PhotoImage`** → 云桌面每页 ~220ms 的固定开销，8 页/帧 ~2s。已修：整帧合成 + 帧级缓存；`[RENDER]` 日志可查 compose/convert 耗时。
5. **A 端误选系统窗口**（TextInputHost 全屏得分最高）→ 剪贴板同步失效。已修：系统窗口黑名单 + 禁止盲目按面积选窗 + 后台周期重扫 + 可信 HWND 出现后才发探针。
6. **`_last_alt_sent` 漏 `global`** → 聚焦 Alt 解锁首次执行即 UnboundLocalError。已修。
7. **bat 用 LF 行尾 / 括号块提前展开** → cmd 解析错乱，`PY_EXE` 被清空。必须 CRLF + `setlocal EnableDelayedExpansion` + `!VAR!` 展开 + `goto :launch` 避免括号块。
8. **升级版本只改一处** → 两端与根目录 VERSION 不一致。必须三处同步（A 内置/B 内置/根 VERSION），跑 `test_invariants` 验证。

## 修改守则

- **改协议/帧格式**：先想清 A/B 两端都怎么变；`tests/test_upgrade.py` 的 AST 抽取测试同步更新。
- **改配置**：同时改 `config.yaml`、入口 argparse 默认值、README 参数表；`a_*`/`b_*` 前缀经 `side_defaults()` 映射到 argparse dest（`b_page_ms` → `--page-ms`）。
- **改版本号**：三处同步（a_proxy.py 内置、b_tunnel.py 内置、根 VERSION），跑测试，然后按相对结构整目录部署。
- **改 .bat**：保持纯 CRLF（`.gitattributes` 兜底），用 `EnableDelayedExpansion` 后的 `!VAR!`。
- **日志纪律**：`log_event`/`blog_event` 一行镜像到 `logs/tunnel.log`；不记录剪贴板正文、HTTP body、Authorization、密码（`safe` 摘要只记字节数/状态）。
- **提交信息**：遵循仓库历史风格，一句中文或 `fix:`/`feat:`/`perf:`/`chore:` 前缀 + 中文正文。