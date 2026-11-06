# port-patches (runtime src patches)

Payload src patches applied to the official kernel tree before
`stamp_version.py` / fnpack. All 8 patches carry `a/src/...` paths, so the strip
level depends on **which tree you are patching** — both were verified against
upstream v0.23.0:

| tree layout | files on disk | command |
| --- | --- | --- |
| fnOS payload root (has `src/`) | `src/hermes_cli/web_server.py` | `patch -p1` |
| upstream kernel tree (flat: `hermes_cli/` at repo root, no `src/`) | `hermes_cli/web_server.py` | `patch -p2` |

`-p2` strips both `a/` and `src/`; using it on a payload tree (or `-p1` on a
flat upstream tree) silently targets the wrong path. `BUILD-NOTES-build-023.md`
says "-p2" because the 0.23 build replays patches on the upstream tree before
assembling the payload. `scripts/verify_kernel_bump.py --kernel-src <tree>`
dry-runs every patch at both strip levels so a kernel bump cannot silently
ship unapplied fixes.

Current inventory (`verify.py` L0 asserts each fingerprint; this file's older
"all four fingerprints" line was stale):

- `adapter-create-retry.patch` — gateway/run_adapters.py: adapter creation failed -> retry once in 3s
- `downloader-read-guard.patch` — pm/downloader.py: guarded reads with their own timeout
- `feishu-video-shrink.patch` — plugins/platforms/feishu/adapter.py: >30MB videos re-encoded to ~26MB before upload
- `gateway-restart-detach.patch` — hermes_cli/web_server.py: append `--detach` (P0-13)
- `launcher-bind-interpreter.patch` — hermes_cli/_launchers.py: bind packaged interpreter (P0-12)
- `restart-coalesce-alive.patch` — hermes_cli/web_server.py: only coalesce while child is alive (P0-10)
- `skill-lock-perm.patch` — tools/skill_usage.py: self-heal unwritable skill locks
- `stale-lock-selfheal.patch` — gateway/run_bootstrap.py: clear stale gateway.lock

`sock_proxy` keepalive and `cmd/main` watchdog/seed logic are tracked directly
in repo files (not as patches).

## fnos 0.22.5.3 distribution fixes (2026-10-29, user P0 audit doc)

- P0-1/P0-2/P2: handled in cmd scripts (tracked): main stop/start reaps the
  systemd-user hermes-gateway-* unit + PPID=1 orphan gateways, removes units
  whose ExecStart path vanished (203/EXEC storm guard), seeds
  unmanaged_idle_exit_seconds=0 + loop_watchdog_max_strikes=8; uninstall_callback
  disables+deletes the unit so reinstalls start clean.
- P0-3: feishu deps bundled into payload venv (lark-oapi 1.7.5 + pycryptodome
  + requests-toolbelt). Rebuild recipe: scripts/payload_hygiene.py (also fixes
  P1-1 shebangs + editable-install leftovers + pyvenv.cfg build paths).
- P1-2: install_callback writes an explicit warning to TRIM_TEMP_LOGFILE when
  a strata engine exists but key extraction fails.
- P0-4 (TUI SIGHUP -> gw.kill on graceful exit, compiled ui-tui entry.js):
  NOT patched in minified JS. Mitigated: gateway runs as systemd unit
  (Restart=always self-heal) and cmd stop now reaps it cleanly; stale
  gateway_state.json fatal state fixed by P0-1. Upstream fix tracked.

## 0.22.5.3 追加（第二轮审计）
- pm/downloader.py：`response.read` 全部改为 `_read_guarded`（read1 + socket settimeout 90s），
  修复连接超时后读取阶段无独立超时导致的下载线程活锁/事件循环 liveness 失联（exit 75 根因之一）。
- skills/.locks 权限自愈：skill_usage 锁打开失败时 chmod/unlink 重建（防历史 700 锁文件卡死全部技能写入）。
- P0-5 双主互杀（今日实机复现）：dashboard 自动拉起的 unmanaged gateway 与 systemd-user
  unit（hermes-gateway-<hash>.service）同时存在时互抢 gateway.lock，落败方 26s 一次
  Restart 风暴、连接反复中断。5.3 修复：cmd/main reap_gateway 卸载全部 hermes-gateway-*
  unit（stop+disable+rm+daemon-reload）；install_callback 改 `disable-linger`（linger 会
  诱发上游建 unit）。fnOS 单一监护 = dashboard（实测 8s 内自动重拉 gateway）。
- verify.py L0 新增 5 项：no-reap-guard / single-supervisor / no-linger /
  downloader-read-guard / skill-lock-selfheal。
- cmd/main：`HERMES_NO_REAP=1` 护栏，测试/验证环境不再误杀托管网关。
- P0-4（TUI SIGHUP）仍追踪上游，本机靠 systemd Restart + clean reap 兜底。
- PM 依赖环境未提交（installs/ 无 facts.json）：根本修复需上游 pm repair 语义或随包提交
  committed generation，当前以 payload venv 直接捆绑依赖规避，列为遗留。

## 0.22.5.3 追加（第三轮：群沉默）
- P0-6 飞书群机器人进群不回话：上游默认 group_policy=allowlist + 空白名单 → 全部群消息静默丢弃。
  cmd/main 启动时若 .env 已配 FEISHU_APP_ID 且未设 FEISHU_GROUP_POLICY，则播种 `FEISHU_GROUP_POLICY=open`
  （require_mention 仍默认 true，只回被 @ 消息）。实机：本机 2026-10-30 外部群复现，改 .env+重启后恢复。
- 对外共享开法已写入技能 hermes-workflows 2.4（外部群不是硬限制）。


## 2026-10-30 第四轮：外部审计介入与 P0-7 收编
- 事件：另一审计方（root）在实机热修 `run_bootstrap.py`（备份 .bak-1030）、开 linger、加 systemd drop-in、向 venv 装 lark-oapi，并用手工 tar 重打包 `/tmp/hermes-si-new.fpk`（顶层 app/ 布局，非 fnpack 结构，禁止安装）。
- 其报告两处失实：19:19 包"不含补丁"（verify-53i L0 18 项在该包全绿）；"1267 次/日重启"为双主修复前数据（18-20 时段 gateway.start=0）。
- P0-7（收编）：`src/gateway/run_bootstrap.py` `_hermes_fnos_clear_stale_lock()`——启动前校验 gateway.lock 的 pid（os.kill 0 + cmdline 含 gateway + start_time），死锁删 gateway.lock/gateway_state.json/gateway.pid。repo 台账 `sim/target/src/gateway/run_bootstrap.py`。
- verify L0 新增 `stale-lock-selfheal`。
- 遗留风险：实机现为 systemd-user 单监护（linger=yes + drop-in），与 fnOS dashboard 设计冲突；5.3 安装后由 P0-5 reap 自动卸载单元回归 dashboard 单监护。


## 2026-10-30 第五轮：trim.hermes 实例全量汇总对照（doc_afbcf7da）
逐条对照结论（0.22.5.3 = 53k）：
- 已修：1.1 停止不清网关(P0-1/5)；2.1 死锁不回收(P0-7)；2.4 重启风暴(P0-5 单监护+watchdog 容忍)；3.1 shebang(P1-1)；3.2 skill_usage 空文件(已恢复 760 行，L0 断言防回归)；3.3 lark-oapi(P0-3 捆绑)；3.4 engine_key(P1-2 告警)；4.2 空闲退出(P2 seed=0)。
- 新增 P0-8：install_callback 幂等创建 /home/<app-user> 并 chown，修 `gateway install` PermissionError。
- 架构分歧（有意不采纳其验收第 6 条）：该文档假设 systemd-user + linger 监护架构；我们的分发架构为 fnOS dashboard 单监护 + disable-linger。linger 开启正是上游重建 unit→双主互杀→1267 次风暴的入口。其 3.7"安装清 linger"实为我们设计的有意行为。
- 2.3 status.py PermissionError→return True：上游有意设计（注释明示），且 P0-7+单监护已封住触发面；该条审计者自标"未复现"。跟踪不补丁。
- 1.2 SIGHUP：审计者自标"推断未证实"；上游 P0-4 家族继续跟踪。
- 4.1 重启广播刷屏：风暴根因已除，正常重启仍广播一次（上游行为），跟踪。
- 4.3 auxiliary.approval：用户端点相关，不分发播种；实机已由审计方调优。

## 2026-10-31 第六轮：restart 合并吞请求（P0-10）
- 实机：点"重启网关"后日志 `Coalescing gateway restart ... (pid 2552791)` —— 被合并的重启子进程早已死，合并只看时间戳不判活 → 网关死了且不再重试。
- P0-10：web_server.py `_spawn_gateway_restart` 合并分支加 `child_alive`（poll() is None）；死子进程=上次尝试已失败，放行新请求。P0-7（run_bootstrap 死锁自愈，收编自 trim.hermes 实例补丁）保证新尝试能清掉死锁。
- 澄清：包内 run_bootstrap 的 `_hermes_fnos_clear_stale_lock` 即其实现的收编版（唯一一份，无覆盖冲突）；README 台账记其贡献。
- verify L0 新增 restart-coalesce-alive；0.22.5.6 构建 56a ALL-PASS。

- launcher-bind-interpreter.patch: 0.22.5.7 P0-12 官方动作子进程启动器绑定自带解释器（重启按钮必挂根因）
- stale-lock-selfheal.patch: 0.22.5.4+ P0-7 载荷层死锁自愈（run_bootstrap）
- restart-coalesce-alive.patch: 0.22.5.6 P0-10 重启合并判活（web_server）
- downloader-read-guard.patch: pm/downloader 读超时保护（read1+settimeout）

## 2026-11-03 kernel 0.22.6 → fnos 0.22.6.1（本地 ALL-PASS，VM 回归待用户重启）
- port-patches 全部 7 个补丁已按 0.22.6 内核重生成（per-file 统一 diff，src/ 前缀，patch -p1 于 payload 根）；旧 0.22.5 代 diff 已替换。
- 内核构建链固化于 `port/hermes-work/build-026/`：codeload 直连拉 tag tar（github.com 被墙）→ 组装 stage（cp -al 硬链接）→ fnosdiffs 重放（`pm/downloader.py` 上游已漂移，3 站点手工移植见 port_downloader.py）→ uv venv（specs=旧 venv 110 包集×新 uv.lock 版本 + ruamel.yaml==0.18.16/clib + 上游 pyproject override-dependencies，见 mk_overrides.py）→ generate_icons → node-deps.mjs → tui.mjs/web.mjs（**必须 cd src 内跑**，babel 从 cwd 解析 react-compiler 插件）→ 摆放 products → fix_web_dist → build-pack.sh（hygiene_026 + delink_both + **lib64 假象树剔除** + prune_src[tests/apps/website] + stamp + fnpack）。
- verify.py --tiers 0,1,2,4 ALL-PASS（45 项，verify-026a.log）；L2 真浏览器端到端零 4xx、WS/TUI 渲染通过；L4 启动器复现通过。
- stamp_version.py 常量已更新：commit f0d94882 / date 1793635052（v0.22.6 tag）。
- 包体 223M（0.22.5.7=241M）。md5 见 H空间交付时现算。**未交付未 push**：VM（192.168.30.30）fnOS 后端死（11-02 起，SSH 会话通道即断），待用户在虚拟机管理器重启后跑覆盖升级 + tier3。

## 2026-11-04 0.22.6.1 VM 回归揪出打包 bug → 0.22.6.2（verify 全绿，VM 复验中）
- **根因**：build-main.sh P6c 把 `out/tui/` 整目录拷进 `hermes_cli/tui_dist/`，产物 `dist/entry.js` 被嵌了一层；`main_tui_launch._find_bundled_tui` 只认 `tui_dist/entry.js` 顶层。后果=新装用户 CHAT 静默降级到 ui-tui **源码构建**路径（需 node+npm+联网重编），离线新装聊天必挂。**已发布的 0.22.5.7 同带此缺陷**（NAS 本机无恙只因运行时目录是开发期部署带的 entry.js）。
- **上游正确语义**（scripts/build/agent.py plant_surfaces）：`tui/dist/*` 内容平铺进 `tui_dist/`，package.json 并列。build-main.sh P6c 已改平铺+断言 entry.js 存在；verify.py 新增 `L0.tui-bundled-entry` 静态断言（此前 45 项全绿仍漏包，因 verify 从不做包体 tui 布局检查）。
- VM 验证顺序：VM 后端已被用户重启恢复；0.22.6.1 VM 装成后 tier5 聊天端到端（补 entry.js 后）state.db 实锤 `assistant='vmcheckE…'`；0.22.6.2 重打包 md5 8296cfa708bf48a2776d52436be9cdbd，verify ALL-PASS 143s，VM 覆盖安装+tier6（sqlite 断言版）进行中。

## 2026-11-05 P0-13：点「重启网关」后 UI 永远停在「正在重启网关」（7.4 待实测）

现象：点击后网关实际已重启成功（端口、会话都正常），但 dashboard 的 action 徽标一直转圈；`GET /api/actions/gateway-restart/status` 恒返回 `running: true`，刷新页面才恢复。这不是偶发，是移植层架构与上游重启语义冲突导致的必现。

证据链（内核 v0.22.7，均可在 app.tgz 内 `src/` 复核）：

1. `src/hermes_cli/web_routers/actions.py:424-439`：`exit_code = proc.poll()`；`running = exit_code is None`。UI 的唯一判据是 **gateway-restart 这个 action 子进程还活着没有**，与网关自身死活无关。
2. `src/hermes_cli/web_server.py:712` `_spawn_gateway_restart()` 派发 `hermes gateway restart [--interrupt]`，**没有** `--detach`。
3. `src/hermes_cli/gateway.py` `_cmd_restart()`（约 5470 起）：fnOS 单监护移除了 systemd-user unit → `_installed_service_kind_for()`（4987）只在 unit 存在时返回 `"systemd"`，故返回 None → service-call / linger 早退分支全部跳过 → 落到 manual fallback：`stop_profile_gateway()` 后调 `start_restart_replacement(args, force=...)`。
4. `src/hermes_cli/gateway_supervised_restart.py:138-149`：

   ```python
   if getattr(args, "detach", False):
       sys.exit(start_detached_replacement(gw.get_hermes_home(), force=force))
   gw.run_gateway(verbose=0, force=force)
   ```

   不带 `--detach` 时 **网关就跑在这个 action 子进程里**，永不退出 → `proc.poll()` 永远 None → UI 永久转圈。该文件模块注释原文即：「In the foreground that caller became the long-lived gateway it only meant to restart」。
5. 上游本来为这个场景留了开关：`src/hermes_cli/subcommands/gateway.py:152` 的 `--detach` help 原文 «… used by apps that run the restart as a short-lived client»。`gateway_runtime_start.py:101` `start_detached_replacement()` → `spawn_unmanaged_gateway()` 后台起网关 → 返回 0 → `sys.exit(0)` → action 子进程退出 → UI 轮询结束。fnOS dashboard 正是 short-lived client。
6. `gateway_runtime_start.py` 的 `DAEMON_STRIPPED_ENV` 明确把 `INVOCATION_ID` 从守护进程环境里剔除，避免守护进程自认 systemd 托管。

**不采纳**「`cmd/main` 里 export `INVOCATION_ID` + SIGUSR1 / exit 75 交回 systemd 拉起」：

- `gateway/control_socket.py` 的 `identify` 会因 `INVOCATION_ID` 把网关报成 supervisor=`systemd`，于是 `_cmd_restart` 走 `restart_externally_supervised_gateway()`（`gateway_supervised_restart.py:72`）：SIGUSR1 drain 后 `_wait_for_supervised_gateway_replacement()` 等 15s 新 PID。
- fnOS 没有真实 systemd unit / KeepAlive；`cmd/process_supervisor.sh` 是 owner-death reaper（只收尸，不拉起）。15s 等不到新 PID → 打印 «Supervisor did not relaunch the gateway» → `sys.exit(1)`，**网关真死**。
- 即该方案把「UI 卡住」的显示问题换成「网关起不来」的可用性事故，且与上游 `DAEMON_STRIPPED_ENV` 的设计相反。

修法：`gateway-restart-detach.patch`，在 `_spawn_gateway_restart()` 的 subcommand 追加 `--detach`。只改 payload 内 Python 源码（port-patches 打进 app.tgz），不动 `cmd/main`、不动封装、不与 0.22.7.3 制品耦合。`verify.py` 新增 `L0.gateway-restart-detach` 静态断言。

状态：**仅静态验证，未出包、未实机验证**。已验证：patch 对内核 v0.22.7 源码 clean apply（含与 `restart-coalesce-alive.patch` 两种应用顺序）、`py_compile` 通过、`--detach` 为 `gateway restart` 合法 flag。待 0.22.7.4-test 包实机回归清单：

- 点重启后 action 状态数秒内变 `running: false` / `exit_code: 0`；UI 自动恢复，无需刷新；
- 网关新 PID ≠ 旧 PID、API 端口可访问、Web/WS 与飞书会话正常；
- 连点两次不产生双主（`gateway.lock` 唯一 owner），`--interrupt` 强制路径仍生效；
- action 日志出现 `✓ Started the gateway in the background`，且无 `Refusing...` / `Supervisor did not relaunch`。

## 0.23.0 内核升级（2026-11-06 复核）

- 8 个补丁在上游 v0.23.0（release commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`）源码上按 `-p1` / `-p2` 两种布局**全部干净重放**；`restart-coalesce-alive.patch` + `gateway-restart-detach.patch` 叠加后逻辑自洽、`py_compile` 通过。`--detach` 在 0.23 仍是 `gateway restart` 的合法 flag（`subcommands/gateway.py:152`、`gateway_supervised_restart.py:146`）。
- **新增风险**：0.23.0 的 `_cmd_restart` 在 `start_restart_replacement()` 之前加了 `gateway_declares_external_supervisor()` 和 `refuse_restart_of_service_managed_gateway()` 两道护栏。若网关自报 systemd，前者会 SIGUSR1 drain 后等 15s 新 PID，等不到就 `exit 1` 且明确拒绝拉起替代网关；此时 `--detach` **根本走不到**。fnOS 从 systemd unit 起应用，`INVOCATION_ID` 会经 dashboard 一路继承到网关，`_detect_supervisor()` 读到此变量即返回 `systemd` —— 这正是上一节「不采纳 export INVOCATION_ID」所分析路径的镜像风险，只是这次是**意外继承**而非主动 export。
- **修法 P0-14**（落在 `cmd/main`，不是 payload 补丁）：启动 dashboard 的子 shell 里 `unset` 掉 `INVOCATION_ID` / `JOURNAL_STREAM` / `NOTIFY_SOCKET` / `WATCHDOG_*` 及 launchd 标记，让 unmanaged 网关自报 `manual`。依据上游 `hermes_cli/gateway_runtime_start.py::DAEMON_STRIPPED_ENV` 的同名处理。`verify.py` 新增 `L0.no-systemd-supervisor-marker` 断言。
- **仍需实机**：点「重启网关」，确认走 `--detach` 分支、action 数秒内结束、网关新 PID 可用；若日志出现 `Restarting externally-supervised gateway` 或 `Supervisor did not relaunch`，说明护栏没生效。
