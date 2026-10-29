# hermes-fnos

Hermes Agent 官方 dashboard 打包为 fnOS（飞牛 OS）fpk 应用的移植层。内核为官方 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) 源码（v0.22.4），本仓库只包含 fnOS 打包层，不含内核源码与二进制运行时。

## 版本约定

manifest `version` 跟随官方内核版本；fnOS 打包修订用后缀：`0.22.4` → `0.22.4.1/.2/...`。git 历史以 tag（`fnos-v<version>`）标识每次验证通过的发布。

## 目录结构

```
manifest            fnOS 应用清单（version = 内核版本 + fnOS 修订）
cmd/                生命周期脚本（start/stop/status/install/uninstall）
config/             fnOS 应用配置
ui/                 应用中心展示资源（图标等）
sock_proxy.py       网关 TCP → unix socket 代理（重写请求行 + Host、剥 Origin）
seeds/              首次启动注入 Hermes home 的 persona/skills（SOUL.md、AGENTS.md、fnOS 知识）
fix_web_dist.py     构建后补丁：Vite preload 前缀改写 + 资产改名破 immutable 缓存
verify.py           五层自验证 harness（L0 包体审计 / L1 启动回归 / L2 浏览器端到端 / L3 消息真往返 / L4 崩溃复现）
```

打包时另需（不入库）：`python314/`（捆绑解释器）、`venv/`（依赖）、`src/`（官方内核）——从官方发行渠道获取后放入 `app/` 下，用 fnpack 构建。

## fnOS 移植要点（沉淀）

- 网关不剥路径前缀：sock_proxy 自写请求行重写 + Host 重写 + 剥 Origin。
- 官方启动器以 `python -I -c`（弹光 PYTHONHOME/PYTHONPATH/VIRTUAL_ENV）拉起 gateway 子进程；捆绑解释器靠 site-packages 里的 `.pth` 挂载 venv 依赖（`-I` 下 site.py 仍执行 .pth）。
- Vite 构建把 preload 基路径烤成 `/assets/...`，fnOS 前缀网关下 404：构建期改写 preload 基路径为 `/app/<app>/`，并把内容哈希资产改名（`-fN` 后缀 + 引用重写）破除 `cache-control: immutable`。
- 跨应用凭证不可读他应用私有目录（700）：provider key 经 dashboard 自己的 API（`/api/providers/custom-endpoints`）直写。
- 每次升级四步铁律：打包 → 真包解包回归（verify.py）→ git push + tag → 才交付安装。

## 验证

```bash
python3 verify.py --tiers 0,1,2,4   # 快速全层（约 70s）
python3 verify.py --tiers 3         # 真引擎消息往返（需配置 provider）
```

## License

移植层随上游 hermes-agent 的许可证约束；上游内核版权归 NousResearch。
