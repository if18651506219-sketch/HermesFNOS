# build-023 (kernel 0.23.0) 构建要点 — 从 build-027 迁移时的差异
#
# P6c TUI 放置（本次踩坑，必须遵守）：
#   out/tui/ 结构是 {dist/{entry.js,...}, package.json}。
#   正确放置 = dist/ 的【内容】平铺进 src/hermes_cli/tui_dist/ + package.json 同级：
#     cp -a $B/out/tui/dist/. $SRC/hermes_cli/tui_dist/
#     cp    $B/out/tui/package.json $SRC/hermes_cli/tui_dist/
#   直接 cp -a out/tui tui_dist 会把 dist/ 嵌套进去 -> verify L0.tui-bundled-entry FAIL，
#   且运行时 _find_bundled_tui 静默回落 ui-tui 源码构建（需 npm+网络）。
#
# P6d fix_web_dist 幂等：对已修过的 web_dist 再跑会 assert hits=1+1。
#   重新放置 out/web（cp -a out/web/. web_dist/）后只跑一次。
#
# stamp_version.py 三元组（0.23.0）：
#   KERNEL=0.23.0 COMMIT=2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19 COMMIT_DATE=1793908658
#
# port-patches 在 0.23.0 上按 -p2（payload root = stage/app）全部干净重放；
# 旧 build-027-asm.sh 用 patch -p1 是因当时 src 多一层。新内核试补丁先 -p2 --dry-run。
#
# venv spec：gen_spec_023.py（old-spec 名字集 × 0.23.0 uv.lock 版本），
#   无新增依赖；实测 0.22.7 -> 0.23.0 uv.lock 包集合完全一致（356 个、0 增 0 删），
#   仅 5 个版本变化：filelock 3.32.7->4.2.6 / pyasn1 0.6.4->0.6.5 /
#   pypdf 6.21.2->6.21.4 / setuptools 83.0.0->84.1.0 (GHSA-xmj3-9gf7-h52w) /
#   tornado 6.5.10->6.6。原笔记「仅 2 包移位」不准确。ruamel.yaml/clib 手工 pin 不变。
#
# === 0.23.0.1 审计补充（2026-11-06）===
# - 版本三元组：本笔记只提 stamp 三元组，漏了 manifest。原仓库 manifest 仍是
#   0.22.7.3，已同步为 0.23.0.1；门禁 scripts/verify_kernel_bump.py 会拦住下次再犯。
# - -p1 / -p2 说清：补丁头是 a/src/...。payload 树含 src/ 用 -p1；上游 v0.23.0 是
#   平铺树（hermes_cli/ 在仓库根、没有 src/）用 -p2。两种都实测干净重放 8 个补丁。
# - fix_web_dist 幂等：本笔记的「重新放置后只跑一次」只是绕过，脚本已修——
#   assert fixed + already == 1 改为 >= 1（按文件计数），并修掉 len(re.Match) 的
#   TypeError（旧 helper 残留时原本会抛异常而不是 exit 2）。现在两阶段可安全重跑。
# - P0-14：fnOS 从 systemd unit 起应用，INVOCATION_ID 会继承到网关。0.23.0 的
#   _detect_supervisor() 据此自报 systemd，_cmd_restart 新增的外部监护护栏会
#   SIGUSR1 drain 后等 15s 一个不存在的 KeepAlive 拉起，然后 exit 1（网关真死），
#   --detach 根本走不到。cmd/main 已在启动 dashboard 的子 shell 里 unset 这批标记
#   （依据上游 DAEMON_STRIPPED_ENV）。实机点「重启网关」验证。
# - 仍缺：build-027-asm.sh / gen_spec_023.py / 0.23 fpk 都未入库，整个包不可复现。
#   verify_kernel_bump.py 只覆盖版本三元组与补丁重放，不是装配脚本。
