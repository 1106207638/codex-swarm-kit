# Codex 蜂群的可选本地助手

本地助手负责把蜂群的文字任务送给你已启动的模型服务。在使用 Codex 的电脑上安装蜂群主包，将本助手解压到准备长期保留的目录，进入包含 `mcp_server.py` 的文件夹后按下列步骤操作。[主包下载和安装教程](https://github.com/1106207638/codex-swarm-kit/blob/v1.0.4/README.md)

准备好模型服务的 **API 基址**。服务与 Codex 在同一台电脑上时，可以是 `http://127.0.0.1:8080/v1`；模型在另一台电脑时，填**模型那台电脑**的局域网 IPv4 和实际端口，例如 `http://192.168.1.20:8080/v1`。示例地址请替换。服务需提供兼容的 `/v1/chat/completions`；自动识别模型还需要 `/v1/models`。这里填写以 `/v1` 结尾的基址。

下面的首次注册命令使用 Codex CLI。先确认 `codex mcp add --help` 能运行，CLI 和桌面端使用同一个 Codex 配置目录。在 **`local-worker` 文件夹内**打开终端。配置工具会询问 URL 和模型 ID；如果服务只列出一个模型，模型 ID 直接回车即可。

**Windows · PowerShell**

```powershell
py -3 -m venv .venv
$workerPython = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $workerPython -m pip install -r requirements-mcp.txt
& $workerPython local_agent.py configure
$backend = Get-Content -Raw -Encoding utf8 .\state\backend.json | ConvertFrom-Json
$server = (Resolve-Path .\mcp_server.py).Path
codex mcp add local-worker -- $workerPython $server --endpoint $backend.endpoint --model $backend.model
```

如 Windows 只有 `python` 命令，可将第一行的 `py -3` 换成 `python`，版本须为 3.11 或更高。

**macOS · 终端**

```sh
python3 -m venv .venv
worker_python="$PWD/.venv/bin/python"
"$worker_python" -m pip install -r requirements-mcp.txt
"$worker_python" local_agent.py configure
endpoint=$("$worker_python" -c 'import json; print(json.load(open("state/backend.json"))["endpoint"])')
model=$("$worker_python" -c 'import json; print(json.load(open("state/backend.json"))["model"])')
codex mcp add local-worker -- "$worker_python" "$PWD/mcp_server.py" --endpoint "$endpoint" --model "$model"
```

首次注册后，重启 Codex 桌面端以建立新连接。发一条小任务验证：「请通过外接本地助手把‘点击保存后闪退’分为故障或咨询，核对模型回执并告诉我实际结果。」预期能看到它返回「故障」，并在本次复盘中找到对应的外接任务。如果连不到模型，先在 **Codex 所在的电脑**核对 API 地址与端口；若服务提供多个模型或没有模型列表，填写实际模型 ID。

如果电脑只有 Codex 桌面端，终端找不到 `codex` 命令，可以照上面步骤装好依赖并运行 `configure`，然后用配置文件完成最后的注册。打开 Codex 用户配置目录中的 `config.toml`（默认是用户主目录下的 `.codex/config.toml`），加入下面这段。如果已有同名服务，更新原段落。把示例路径换成本机的绝对路径，地址和模型 ID 填刚生成的 `state/backend.json` 里的值。macOS 的解释器路径以 `.venv/bin/python` 结尾。

```toml
[mcp_servers.local-worker]
command = "C:/Tools/local-worker/.venv/Scripts/python.exe"
args = ["C:/Tools/local-worker/mcp_server.py", "--endpoint", "http://127.0.0.1:8080/v1", "--model", "your-model-id"]
```

### 以后怎样更换模型 URL？

- **Windows**：双击同一个助手文件夹里的 `Change-Model-URL.cmd`，输入新地址。
- **macOS**：在同一个助手文件夹运行 `./.venv/bin/python local_agent.py configure`，按提示输入新地址。
- **已知道模型 ID**：两端都可以给 `configure` 加上 `--endpoint 新地址 --model 模型ID`。设置保存在助手目录的 `state/backend.json`。如果注册时自定义了队列目录，换址也使用它，例如 `local_agent.py --state "队列目录" configure`。

保存后，下一次新派发会读取新地址，无需再次注册或重启。已经提交或排队的任务继续使用原地址，可按原编号查结果；已准备但尚未提交的任务遇到换址会退回总控重新核对。切换期间让旧服务运行到旧任务结束。[外接助手与失败恢复](https://github.com/1106207638/codex-swarm-kit/blob/v1.0.4/docs/EXTERNAL-WORKER.md)
