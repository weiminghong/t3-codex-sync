# t3-codex-sync

一个可安装的 **Codex skill + 独立 Python CLI**，将本机 Codex CLI 会话增量同步到 T3 Code Desktop。skill 负责理解调用意图，随附脚本执行扫描、去重、备份和同步。

支持导入新会话，并比较已有会话中的消息后追加增量；支持 T3 中续聊产生的分叉会话。它直接写入 T3 数据库，针对 **T3 Code Desktop v0.1.11** 的结构编写，不修改安装包。其他版本需先验证兼容性。

## 安装为 skill

需要 Windows、Python 3.10+（标准库 SQLite 支持 FTS5）。无需安装 Python 第三方依赖。

将本仓库克隆至 Codex 的 skills 目录。默认位置示例：

```powershell
git clone https://github.com/weiminghong/t3-codex-sync.git "$HOME\.codex\skills\t3-codex-sync"
```

如果设置了自定义 `CODEX_HOME`，安装到该目录下的 `skills/t3-codex-sync`。若目标目录已存在，先检查内容，不要覆盖本地文件。

也可以使用 Codex 的 skill-installer，提供本仓库地址并说明安装仓库根目录的 skill。

安装后调用：

```text
用 $t3-codex-sync 检查待同步会话
用 $t3-codex-sync 同步指定项目
用 $t3-codex-sync 同步全部会话并重启 T3
```

仅输入 `$t3-codex-sync` 默认检查状态。明确要求同步才写入；明确要求重启才关闭并重启 T3。

## 直接使用 CLI

在仓库目录运行：

```powershell
# 只读预览，不写数据库
.\scripts\t3-codex-sync.cmd status
.\scripts\t3-codex-sync.cmd sync --dry-run

# T3 已关闭时同步
.\scripts\t3-codex-sync.cmd sync

# 关闭 T3、同步并重新启动；可能中断 T3 内正在运行的代理
.\scripts\t3-codex-sync.cmd sync --restart

# 限定范围（名称或 ID 子串匹配）
.\scripts\t3-codex-sync.cmd status --project '项目名称'
.\scripts\t3-codex-sync.cmd sync --session '完整会话ID' --restart

# 只读监听变动；Ctrl+C 退出
.\scripts\t3-codex-sync.cmd watch
```

可双击 `scripts/一键同步并重启T3.cmd` 执行同步与重启。也可直接用 `python scripts/t3_codex_sync.py` 代替 `.cmd` 启动器。

工具不预设个人安装目录。T3 程序优先使用 `--t3-exe`，其次使用环境变量 `T3_CODE_EXE`，未配置时查找 PATH 中的 `T3 Code (Beta).exe`。重启前找不到程序会停止，不关闭 T3 或写入数据库。可以显式指定路径（替换占位符）：

```powershell
.\scripts\t3-codex-sync.cmd sync --restart --t3-exe '<T3可执行文件的完整路径>'
```

## 数据行为

- 扫描 `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` 与 `~/.codex/session_index.jsonl`，按工作目录匹配 T3 项目。
- 默认目标为 `~/.t3/userdata/statev2.sqlite`，写入 `orchestration_events` 和对应 v2 投影、位置与运行时元数据。
- 写入前通过 SQLite Backup API 自动备份至 `~/.t3/userdata/backups/t3-codex-sync/`，默认保留 10 份。
- 新会话默认仅导入末尾 **200 条**消息。需要全量新导入时，预览和同步均加 `--no-truncate`（该参数属于 `sync`）。
- 已有会话使用消息锚点、角色与规范化文本比较做增量去重。重复文本和复杂分叉存在匹配歧义，不保证任意历史下都能无损判定；先预览并保留备份。
- T3 正在运行时，普通 `sync` 拒绝写入。`sync --restart` 在有变更且检测到 T3 运行时执行关闭及重启；没有变更时不会重启。
- `watch` 默认仅提醒。`--auto-sync` 会在 T3 关闭时自动写入；`--auto-restart` 会自动关闭并重启 T3，只有需要该行为时才启用。
- `.jsonl.zst` 归档为可选功能，需要额外的解压能力；默认不扫描。详见命令帮助。

## 验证

```powershell
python -m unittest discover -s tests -v
python scripts/t3_codex_sync.py --help
```

仓库测试使用合成消息检查截断历史、分叉去重及重复同步行为，并检查重启路径的查找与失败保护；不接触真实数据库或运行中的 T3。测试不代表已验证所有 T3 版本或完整 GUI 刷新流程。不要上传个人会话、数据库、备份或 GitHub 凭据。
