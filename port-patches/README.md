# port-patches (runtime src patches)

Payload src patches applied to the official kernel tree after extracting app.tgz
(patch -p1 inside the payload root, before stamp_version.py / fnpack):

- adapter-create-retry.patch — gateway/run_adapters.py: adapter creation failed -> retry once in 3s
- feishu-video-shrink.patch — plugins/platforms/feishu/adapter.py: >30MB videos re-encoded to ~26MB before upload

sock_proxy keepalive and cmd/main watchdog seed are tracked directly in repo files.
verify.py L0 asserts all four fingerprints.

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
