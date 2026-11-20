# Codex Swarm Kit

## 1. 这是什么

**Codex Swarm Kit 是一个在 Codex 桌面端使用的模型调度工具包。** 安装后，任务还是在原来的 Codex 聊天里提。遇到复杂任务，能力更强的模型负责统筹和验收，中等档的模型负责规划，轻量模型负责步骤明确的执行和资料收集。

这里的分工按任务难度来定。看不清问题出在哪儿，先交给擅长分析的模型。步骤和结果已经说清楚了，就可以交给轻量模型执行。简单问答和小改动通常由当前聊天直接处理。

## 2. 它是怎么运转的

以「给网站加一个报名名单导入功能」为例。除了做出上传入口，还要决定接受什么格式、怎样提示错误，以及导入失败会不会影响已有数据。

1. **主线程定目标。** 当前这个 Codex 聊天就是主线程。它先核对要交付什么、哪些地方能改、怎样才算做好，然后把任务分成可以分别验收的几块。
2. **规划模型安排做法。** 它先查清现有系统怎么处理名单，再建议先做格式校验、后接入上传页面，并列出重复报名、缺字段等需要检查的情况。对外行为有疑问时，主线程先把问题确认清楚。
3. **执行模型按工作单做事。** 工具包会把要做的事、可以读取和修改的文件、应交回的结果写进一份工作单。被分派工作的 Codex 子代理按这份单子执行，例如整理测试样例、实现校验或补上错误提示。有清楚边界、互不影响的工作可以并行。
4. **另一位子代理独立检查，主线程验收。** 审核者检查实现有没有漏掉约定的情况。主线程再核对改动、实际运行结果和未解决的问题。比如发现导入失败后仍写入了一部分数据，就先修好并重验，再进入下一批。

安装器把协作规则放进 Codex 会读取的配置目录。执行时，主线程先查看 Codex 当时提供的模型，再借助附带的调度工具安排分工，并把「谁接了任务、做到了哪里、哪些结果已经验收」记进任务记录。中断后可以先查记录再接着做。如果无法确认原先的执行者是否已经停下，会先核实状态，以免把同一件事派两次。

常用分工是 Astra 统筹，Sol 做技术规划和独立审核，Luna 做明确的实现和整理，Terra 做需要综合多份来源的研究。实际能用哪些模型、推理强度和子代理功能，由当前 Codex 环境和账号决定。主线程也可以在阶段交接时调整下一轮的模型。通过 Codex 的 Goal 自动续跑，需要当前环境支持、用户明确启动 Goal，并授权续接消息。

## 3. 什么场景怎么用

在 Codex 聊天里照常描述想要的结果。对于复杂任务，补上交付物、可以动的范围，以及怎样验收。下面的提示可以按实际情况替换。

**一项功能牵涉几个部分。** 例如要同时改页面、处理输入和补检查。可以这样说：

> 给报名页增加名单导入。先把方案、模块顺序和验收标准列出来，等我确认后按蜂群协作执行。每批完成后独立检查，再做下一批。

**资料多，需要从收集走到结论。** 可以把查找、汇总和判断分开，最后由主线程交付建议：

> 整理这三种工具的官方文档。分工收集能力、限制和部署要求，保留出处；主线程核对冲突，给出对照表和适用场景。先给我研究大纲，确认后再分派。

**一项长任务做到一半。** 让 Codex 先找回原计划和已验收的结果：

> 从已有计划和任务记录接着做。先确认上一批通过了哪些检查、还有哪些工作在进行，再推进当前模块。

像改一个按钮文案、修一条明确的提示或回答一个问题，直接告诉 Codex 要改什么即可。

### 任务结束后，怎么看蜂群协作复盘？

先看最终答复。写有 **「本次任务使用了蜂群协作」**，表示至少有一次派发拿到了执行者回执。每项工作都可以顺着三件事往下读：谁接了什么任务、交回了什么、总控最后用了哪部分以及原因。只有主线程直接处理的小任务会照常收尾。

例如，假设你让蜂群整理客户反馈，复盘里出现以下两项：

| 谁接了什么工作 | 交回的结果 | 怎样理解总控的决定 |
| --- | --- | --- |
| 本地助手给 20 条反馈分类 | 18 条有明确的故障或咨询依据，另外 2 条描述不完整 | **部分采纳**。18 条进入汇总，2 条留给人工确认。下次可在分类标准里补上「信息不足」。 |
| 独立审查助手核对优先级 | 找到一条「登录失败」被排在低优先级，但原文说所有用户均无法登录 | **采纳审查意见**。总控退回原排序，要求按影响范围重新排。 |

要追查某一项，打开该项附带的日志链接或完整明细。明细里的行号对应**这次任务台账目录下**的 `calls.jsonl`，从第 1 行开始计数。同一次调用的开始和结束共用一个 `call_id`。反复查同一张任务单只算一次执行。看到「已交回」说明助手给了结果，「已验收」说明总控检查过，最终是否用于交付看「采纳／部分采纳／退回／未采用」及理由。「执行情况待核实」表示缺回执，暂时无法判断助手有没有执行。

也可以直接问 Codex：「按这次任务的台账，找出被退回的子任务，列出原始调用行号、退回原因，以及下次该补哪条验收标准。」旧任务缺少的理由会标明。日志字段和查询方法见[蜂群任务复盘](docs/CONTRACT.md#蜂群任务复盘)。

## 4. 部署教程

1. **准备环境。** 安装并登录 Codex 桌面端，准备 **Python 3.11 或更新版本**。需要安装 Python 时，可从 [Python 官网](https://www.python.org/downloads/)获取。
2. **下载并解压。** 打开 [GitHub Releases](https://github.com/1106207638/codex-swarm-kit/releases)，在 Assets 里下载文件名以 `codex-swarm-kit-` 开头的部署 ZIP。将整个 ZIP 解压到一个文件夹。`.zip.sha256` 文件可用来核对下载是否完整。
3. **运行安装入口。** Windows 双击 `Install-Windows.cmd`。Mac 双击 `Install-macOS.command`，也可以在解压目录的终端运行 `sh ./Install-macOS.command`。
4. **核对并确认。** 安装器会显示目标 Codex 目录和将写入的规则。确认后输入 `y`。工具包保存在 Codex 主目录的 `swarm-kit` 里，规则加到 `AGENTS.md`，已有普通规则会保留。发现另一套调度规则时，安装器会提示处理冲突。
5. **打开新聊天检查安装。** 在 Codex 中发送下面的提示。详细步骤见[自检说明](docs/SELF-CHECK.md)。

> 请按 Codex Swarm Kit 的自检说明检查安装，列出当前可用的子代理、模型和推理强度。可以在隔离目录做一次只读子代理试跑。

更新时解压新版再运行同一个安装入口。需要查看安装是否完整、恢复中断的安装或回退时，按 [安装与恢复说明](docs/INSTALLATION.md) 操作。协作细节见 [工作规则](docs/RULES.md)、[大纲协作](docs/OUTLINE.md) 和 [运行说明](docs/RUNTIME.md)。

### 在另一台电脑接入本地模型（可选）

在安装了 Codex 的那台电脑上操作。先装上面的蜂群主包，再从同一版本的 Assets 下载 **`codex-local-worker-1.0.4.zip`**，解压到准备长期保留的目录。进入解压后的 `local-worker` 文件夹，确认能看到 `mcp_server.py`。本地助手负责把蜂群的任务送给你已启动的模型服务。[下载 v1.0.4 主包和助手](https://github.com/1106207638/codex-swarm-kit/releases/tag/v1.0.4)

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

### 以后怎样更换模型 URL？

- **Windows**：双击同一个助手文件夹里的 `Change-Model-URL.cmd`，输入新地址。
- **macOS**：在同一个助手文件夹运行 `./.venv/bin/python local_agent.py configure`，按提示输入新地址。
- **已知道模型 ID**：两端都可以给 `configure` 加上 `--endpoint 新地址 --model 模型ID`。设置保存在助手目录的 `state/backend.json`。如果注册时自定义了队列目录，换址也使用它，例如 `local_agent.py --state "队列目录" configure`。

保存后，下一次新派发会读取新地址，无需再次注册或重启。已经提交或排队的任务继续使用原地址，可按原编号查结果；已准备但尚未提交的任务遇到换址会退回总控重新核对。切换期间让旧服务运行到旧任务结束。[外接助手与失败恢复](docs/EXTERNAL-WORKER.md)
