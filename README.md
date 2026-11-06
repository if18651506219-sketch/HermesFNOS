# hermes-fnos

Hermes Agent 官方 dashboard 打包为 fnOS（飞牛 OS）fpk 应用的移植层。内核为官方 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 源码（当前目标 v0.23.0），本仓库只包含 fnOS 打包层，不含内核源码与二进制运行时。

## 版本约定

manifest `version` 跟随官方内核版本；fnOS 打包修订用后缀：`0.23.0` → `0.23.0.1/.2/...`。git 历史以 tag（`fnos-v<version>`）标识每次验证通过的发布。

**打包前必须跑门禁**：`python3 scripts/verify_kernel_bump.py`（带 `--kernel-src <上游源码树>` 可同时 dry-run 全部 port-patches）。它校验 `manifest version` 前三段 == `install-stamp.json baseVersion` == `stamp_version.py KERNEL`、stamp 的 `commit` == `stamp_version.py COMMIT`、COMMIT_DATE 合理，并模拟一次 `stamp_version.py` 调用复现 `verify.py L0.version-consistency`。0.23.0 升级时这份约定只写在文档里没人执行，结果 manifest 仍停在 `0.22.7.3` 而 KERNEL 已是 `0.23.0`，成品必然 L0 FAIL、`repack_fpk.py` 也拒绝出包。`stamp_version.py` 会拒绝内核与常量不匹配的调用，`verify.py` 的 `L0.version-consistency` 是最后一道兜底；标着 0.22.7 却装着 0.22.6 commit 的包，以前能一路全绿。

## 目录结构

```
manifest            fnOS 应用清单（version = 内核版本 + fnOS 修订）
cmd/                生命周期脚本（start/stop/status/install/uninstall）
config/             fnOS 应用配置
ui/                 应用中心展示资源（图标等）
sock_proxy.py       网关 unix socket → TCP 代理（请求行/Host 重写、完整请求体转发、X-Forwarded-* 净化、WS 隧道、分阶段超时）
seeds/              首次启动注入 Hermes home 的 persona/skills（SOUL.md、AGENTS.md、fnOS 知识）
fix_web_dist.py     构建后补丁：Vite preload 前缀改写 + 资产改名破 immutable 缓存（两阶段均可重跑）
stamp_version.py    构建期写 install-stamp.json（KERNEL/COMMIT/COMMIT_DATE 三元组强校验）
scripts/verify_kernel_bump.py 内核升级门禁：版本三元组模拟 + port-patches 干跑重放（不需要 fpk）
tools/verify_fix_web_dist.py  fix_web_dist 两阶段幂等回归（合成 dist，不需要上游产物或 fpk）
scripts/payload_hygiene.py  打包前清理 venv shebang / 构建机路径
scripts/verify_proxy.py     sock_proxy 行为回归（真 unix socket + 假上游，不依赖 fpk，可 --proxy 指定被测文件）
scripts/verify_proxy_live.py 实机网关回归：对运行中的 dashboard 打不存在的路由，验证大 POST 快速返回而非挂死（路径硬编码，按 NAS 环境调整）
scripts/repack_fpk.py       基于官方 release fpk 重封移植层（不需要构建 NAS / fnpack）
verify.py           六层自验证 harness（L0 包体审计 / L1 启动回归 / L2 浏览器端到端 / L3 消息真往返 / L4 崩溃复现 / L5 代理行为）
```

**从零构建**仍需（不入库）：`python314/`（捆绑解释器）、`venv/`（依赖）、`src/`（官方内核）与 fnpack，只在构建 NAS 上跑。**0.23.0 已知缺口**：`BUILD-NOTES-build-023.md` 引用的 `build-027-asm.sh`、`gen_spec_023.py` 尚未入库，TUI 平铺放置 / venv spec / 装配顺序目前只在构建机上，第三方无法复现整个 0.23 包；`scripts/verify_kernel_bump.py` 只能覆盖版本三元组与补丁重放，不能替代装配脚本。**移植层改动不需要从零构建**：官方 release 的 fpk 是无签名的 gzip tar，`scripts/repack_fpk.py` 逐成员原样复制（mode/uid/gid/uname/gname 全保留），只替换 `cmd/main`、`app.tgz!sock_proxy.py`、`install-stamp.json` 的 fnOS 修订号，并从 **release 包内的 manifest** 派生新 manifest（改 version + checksum）。注意重封只适用于「同内核、换移植层文件」；跨内核大版本升级（如 0.22.7 → 0.23.0）必须走完整构建。

> **坑（0.22.7.2 两版都踩过）**：fnOS 要求 `manifest.checksum == MD5(app.tgz)`，安装时校验。这个字段是 fnpack 构建期写进 release manifest 的，**仓库里签入的 manifest 没有**。早期重封直接用了仓库 manifest，结果包能过所有 L0 断言、却会在应用中心校验阶段被拒。现在 `repack_fpk.py` 强制从 release manifest 派生并重算，`verify.py` 也有 `L0.manifest-checksum` 断言兜底。

## 重封安装包

```bash
# 下载官方 release 的 HermesFNOS.fpk，然后：
python3 scripts/repack_fpk.py \
  --base HermesFNOS.fpk --src . \
  --out ../HermesFNOS-0.23.0.1.fpk --version 0.23.0.1
```

版本号必须严格高于已装版本（应用中心只对更高版本提供覆盖升级），且前三段必须等于 `stamp_version.py KERNEL`；脚本会同时校验源 manifest、`install-stamp.json` 的 `baseVersion`/`commit` 与 `stamp_version.py` 的 COMMIT，任一不符直接拒绝。出包前先跑 `python3 scripts/verify_kernel_bump.py`。输出包可用 `verify.py --tiers 0,5` 做真包解包回归。

## fnOS 移植要点（沉淀）

- 网关不剥路径前缀：sock_proxy 自写请求行重写 + Host 重写 + 剥 Origin（仅 WebSocket 握手）。
- 官方启动器以 `python -I -c`（弹光 PYTHONHOME/PYTHONPATH/VIRTUAL_ENV）拉起 gateway 子进程；捆绑解释器靠 site-packages 里的 `.pth` 挂载 venv 依赖（`-I` 下 site.py 仍执行 .pth）。
- Vite 构建把 preload 基路径烤成 `/assets/...`，fnOS 前缀网关下 404：构建期改写 preload 基路径为 `/app/<app>/`，并把内容哈希资产改名（`-fN` 后缀 + 引用重写）破除 `cache-control: immutable`。
- 跨应用凭证不可读他应用私有目录（700）：provider key 经 dashboard 自己的 API（`/api/providers/custom-endpoints`）直写。
- 每次升级五步铁律：跑 `verify_kernel_bump.py` 门禁 → 打包 → 真包解包回归（verify.py）→ git push + tag → 才交付安装。

## 0.22.7.3 修复（2026-11-04 审计，2026-11-05 重封）

官方 `0.22.7.1` release 包（内核 v0.22.7 / commit b200f5bc / sha256 af1936fc…）的产品内容保持不变，只替换移植层四个文件。版本号推到 `.3` 是因为市面上已有两个互不兼容的 `0.22.7.2`（本仓库早期重封版、Qwen 修复版），应用中心只对**严格更高**的版本提供覆盖升级，且两版都漏了 `manifest.checksum`。

- **sock_proxy 请求体**：`recv_head` 只转发首次 `recv(4096)` 附带的字节，之后不再读客户端 → 任何 >4KB 或 header/body 分次写的 POST 永久挂死。现按 Content-Length / chunked 完整转发，并保留 pipelining 余量。
- **分阶段超时**：`socket.create_connection(timeout=10)` 的超时会留在 socket 上，空闲 10s 的 SSE/WebSocket 被 `recv` 超时当成 EOF 截断（现有 TCP keepalive 补丁治不了这个）。响应头阶段有界超时，进入流式阶段后两端都切回阻塞模式；超时返回 504 而不是静默断连。
- **转发头净化**：客户端自带的 `X-Forwarded-*` 会被原样转发并与代理追加的可信值重复，伪造值在前。现在统一剥离后只添加一个可信 `X-Forwarded-Prefix`。
- **Expect: 100-continue**：代理自行应答 100，避免上游 100 与未发出的请求体互等。
- **cmd/main**：`systemctl_user()` 吞掉 stdout 导致 `list-units` / `show` 永远为空（拆出 `systemctl_user_out`）；`OPENAI_API_KEY` 硬编码占位符导致提取的 engine_key 从未生效；首装 config 播种顺序与格式错误（gateway 段要等第二次启动、扁平格式与 verify 验证过的嵌套格式冲突）；启动失败仍返回 0 / `status()` 只看 pidfile 的假绿。
- **验证体系**：新增 L5 行为回归（`scripts/verify_proxy.py`，8 个场景）；`verify.py` 去掉 `/vol2/...` 硬编码与顶层 websocket 导入，生产端口改为可选（`HERMES_PROD_PORTS`），新增 `L0.version-consistency` 与 `L0.manifest-checksum`。原来的 L0 多为字符串存在性断言，能放行死代码，故关键链路改由行为断言覆盖。

## 0.23.0.1 修复（2026-11-06 审计）

上游 v0.23.0（The Hearth Release，release commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`）只提交了 `stamp_version.py` 三元组与一份构建笔记，没有 tag、没有 fpk、没有装配脚本。本次修复**只动移植层**，不改内核源码，也不声称产出了可安装包。

- **P0 版本三元组**：`manifest` 从 `0.22.7.3` 同步为 `0.23.0.1`。此前 `KERNEL=0.23.0` 而 manifest 仍是 `0.22.7.3`，模拟 `L0.version-consistency` 为 FAIL，`repack_fpk.py --version 0.23.0.1` 也会因源 manifest 不匹配直接拒绝。新增 `scripts/verify_kernel_bump.py` 把这个约定变成可执行门禁。
- **P0-14 systemd 监护标记**：fnOS 从 systemd unit 起应用，`INVOCATION_ID` / `JOURNAL_STREAM` / `NOTIFY_SOCKET` 会经 dashboard 传给网关。0.23.0 的 `gateway/control_socket.py::_detect_supervisor()` 读到 `INVOCATION_ID` 就自报 `systemd`，新增的 `_cmd_restart` 外部监护护栏随即把网关当受监护进程、SIGUSR1 drain 后等 15s 等一个 fnOS 根本不存在的 KeepAlive 拉起，然后 `exit 1`——网关真死，且 `--detach`（P0-13）根本走不到。`cmd/main` 现在在启动 dashboard 的子 shell 里剥掉这批标记，依据上游 `hermes_cli/gateway_runtime_start.py::DAEMON_STRIPPED_ENV` 的同一处理。`verify.py` 新增 `L0.no-systemd-supervisor-marker` 断言。**此项仍需实机点「重启网关」确认**。
- **fix_web_dist.py**：`assert fixed + already == 1` 在含多个 modulepreload helper 的 dist 上、以及重跑时会崩（笔记里记了 `hits=1+1` 却只写「重新 cp 后只跑一次」绕开）。改为按文件计数、要求 `>= 1`，两阶段可安全重跑；另修 `len(re.Match)` 的 `TypeError`——原 `leftover` 计算只要有旧 helper 残留就抛异常，而不是按设计返回 exit 2。
- **文档**：README 目标内核、版本约定与重封示例同步到 0.23；`port-patches/README.md` 说清 `-p1`（payload 含 `src/`）与 `-p2`（上游平铺树）的差别；补台账；诚实标注 0.23 装配脚本仍未入库、重封只适合同内核换移植层。

**已实证排除**（避免误报）：三元组 sha/日期真实；8 个 port-patches 在 v0.23.0 源码上按 `-p1` 和 `-p2` 两种布局全部干净重放；`--detach` 在 0.23 仍是合法 flag，两个重启补丁叠加后 `py_compile` 通过；`hermes dashboard --skip-build` / `web_dist` 路径未变；`uv.lock` 两版 356 包、0 增 0 删，仅 5 个版本变化。

## 验证

```bash
python3 scripts/verify_kernel_bump.py                       # 版本三元组门禁（无需 fpk）
python3 scripts/verify_kernel_bump.py --kernel-src <tree>  # 附带全部补丁干跑重放
python3 tools/verify_fix_web_dist.py                      # preload/rename 两阶段幂等回归
python3 scripts/verify_proxy.py --quick          # 代理行为回归（跳过 12s 慢流，约 2s）
python3 scripts/verify_proxy.py                 # 全量，含 12s 空闲流不被截断
python3 verify.py --tiers 5                     # 从 harness 调用上面这套
python3 verify.py --tiers 0,1,2,4,5             # 快速全层（需 pkg/hermes-si.fpk）
python3 verify.py --tiers 3                     # 真引擎消息往返（需配置 provider）
```

`verify.py` 可选环境变量：`HERMES_PROD_PORTS`（实机在跑的生产端口，未设置则 prod-untouched 记 SKIP）、`HERMES_KEY_ENV` / `HERMES_ENGINE_API_KEY` / `HERMES_ENGINE_URL` / `HERMES_MODEL`。

**全新安装实机重点验证**：`config.yaml` 首装播种的嵌套 provider 格式是否被内核正确读取（此前从未被测过，`write_sim_config` 抢先覆盖了它）；大 POST（保存配置、上传文件）；WebSocket 空闲 >10s 不掉线。

## License

移植层随上游 hermes-agent 的许可证约束；上游内核版权归 NousResearch。
