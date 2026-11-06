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
#   仅 2 包移位、无新增依赖；ruamel.yaml/clib 手工 pin 规则不变。
