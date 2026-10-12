# API Key 配置与业务读取

已有宿主安全配置或环境注入时直接复用。火山用 `VOLCENGINE_ASR_API_KEY`，百炼用 `DASHSCOPE_API_KEY`；两者不得混用。首次填写或更换 Key 时使用随附页面。用户已经明确提供并授权保存的凭据可直接写入系统凭据库，不要求重复填写；不将 Key 放进代码、普通文件、命令参数或日志。离线 review/render 不读取 Key。

## 首次配置

将当前 SKILL.md 所在绝对目录记为 `SKILL_DIR`。页面需要 Node.js 22.18+，首次在组件目录安装锁定依赖：

```bash
npm --prefix "$SKILL_DIR/scripts/credential-ui" ci --ignore-scripts
node "$SKILL_DIR/scripts/credential-ui/src/profile.ts" status volcengine
node "$SKILL_DIR/scripts/credential-ui/src/profile.ts" setup volcengine
```

先查 status：退出码 0 表示当前业务凭据可读取，2 表示缺失，1 表示配置或系统后端失败。缺失或用户要求更换时才启动 setup，把返回的本机链接展示给用户，由用户亲自填写保存。不要自动操作真实 Key 页面，不让用户贴进聊天。

页面不回填原值；已有项留空保留，替换需要用户确认。只把 `saved` 当作全部保存成功；`partial`、超时和中断后先重新查状态，再补未完成项。配置成功仅证明保存和可读取，实际 API 可用性以业务调用为准。

## 服务与用途绑定

| 配置名 | 业务环境变量 | 系统凭据引用 | 说明 |
| --- | --- | --- | --- |
| default | `DASHSCOPE_API_KEY` | `video-editor/dashscope/default` | 阿里云百炼 DashScope API Key（仅用于语音转录） |
| volcengine | `VOLCENGINE_ASR_API_KEY` | `video-editor/volcengine/default` | 豆包语音控制台 API Key，需授权 `volc.seedasr.auc`（2.0 标准版） |

选择 `volcengine` 时需要豆包语音控制台 Key，不使用火山方舟 Ark Key。极速版 `volc.bigasr.auc_turbo` 不属于此入口，不自动回退或要求额外开通。服务参考：[任务提交](https://docs.volcengine.com/docs/6561/2606791?lang=zh)。

## 运行业务

推荐使用统一入口 `bash "$SKILL_DIR/scripts/run.sh" cut /path/to/video.mp4`，它读取 `~/.config/video-editor/config.json` 的 `asr_backend`，选择对应系统凭据注入。该 Key 只给 ASR 使用；语义判断由调用 Skill 的 Agent 完成。底层火山命令如下：

```bash
node "$SKILL_DIR/scripts/credential-ui/src/profile.ts" run volcengine -- "$SKILL_DIR/.venv/bin/python3" "$SKILL_DIR/scripts/video_editor.py" cut /path/to/video.mp4
```

Windows PowerShell 使用：

```powershell
.\scripts\run.ps1 cut C:\path\to\video.mp4
```

上述底层注入入口中，环境变量优先；缺失时仅从系统库读取当前配置所需的 Key，并只注入可信业务子进程。参数、普通文件和状态输出都不含 Key。使用页面保存的凭据后，后续云端业务命令同样经统一入口执行。`review` 和 `render` 直接使用本地计划，无需 Key。

系统后端分别为 macOS 钥匙串、Windows 凭据管理器、Linux Secret Service。CI、容器与远程服务器使用已有 Secret 注入，不把本机页面开放到网络。
