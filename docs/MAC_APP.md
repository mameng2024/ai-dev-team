# 做成 Mac App（双击打开）

不上架 Mac App Store：上架必须运行在沙盒里，而 AI Dev Team 要调用 git、OpenCode、swift，
还要改你的代码仓库，这些沙盒都不允许。App 用下面的脚本在你自己的 Mac 上生成。
本机生成的 App 不会被 Gatekeeper 拦下。

## 第一次

```bash
cd ~/Downloads/ai-dev-team            # 解压后的文件夹
./packaging/macos/build_app.sh --install
```

脚本会做这些事：
1. 找到 Python 3.12+。没有的话先运行 `brew install python@3.12`。
2. 在 `~/Library/Application Support/AI Dev Team/` 建立独立的运行环境。模型配置、数据库也放在这里。
3. 记下你终端里的 PATH，以及 opencode、git、swift 的位置。双击打开的 App 默认找不到它们。
4. 生成 `dist/AI Dev Team.app`。加了 `--install` 会顺便复制到「应用程序」文件夹。

然后在启动台或「应用程序」里双击 **AI Dev Team**，浏览器会打开统一页面：
- **项目与运行**：添加项目（可以点「选择…」用访达选文件夹），写需求，预览或保存规划，
  试运行或正式开发，处理合并队列。右侧是操作记录和实时日志，可以停止。
- **看板**：任务状态、事件、模型用量。
- **模型设置**：每个 Agent 的模型、备用模型、API Key、测试按钮。

再次双击会回到同一个页面，不会重复启动。点右上角「退出」会关掉后台服务。

## 更新

下载新版本后，在新文件夹里再运行一次 `./packaging/macos/build_app.sh --install`。
模型设置、Key、项目和任务都会保留。

## 可选

- `--dmg`：生成 `dist/AI-Dev-Team-<版本>.dmg`，方便拷到别的 Mac。在别的 Mac 上第一次打开时，
  要右键选「打开」。那台 Mac 也需要先运行一次 build_app.sh，因为运行环境不在 App 里面。
- `--sign "Developer ID Application: 你的名字 (TEAMID)"`：有 Apple 开发者账号时可以签名，
  之后再用 `xcrun notarytool` 公证，别人打开时就不会看到警告。

## 出问题时

- 日志：`~/Library/Application Support/AI Dev Team/server.log`
- 页面提示找不到 OpenCode：装好 OpenCode 后再运行一次 build_app.sh。
- 想完全重来：退出 App，删除 `~/Library/Application Support/AI Dev Team/venv`，再运行 build_app.sh。
  API Key 和 worktree 都在 `~/.ai-dev-team`，不受影响。
