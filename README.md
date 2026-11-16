# Codex Swarm Kit

把复杂任务拆给合适的模型执行，由主线程统筹和验收。小修和简单任务直接处理；有明确大纲、模块和验收的大任务才进入蜂群协作。

这是桌面 Codex 的协作规则与调度工具包，沿用 3.5.0 调度合同。它不会安装模型、提供账号权限或替换 Codex。目标账号必须实际具备相应原生工具和模型；Astra、Sol、Luna、Terra 名称在不同宿主不一定可用，缺失时会说明并停下相关派发。

## 安装

在 [Releases](../../releases) 的 Assets 下载部署 ZIP 和 `.zip.sha256` 校验文件。

1. 安装并登录 Codex 桌面端，准备 **Python 3.11 或更新版本**。
2. 将整个 ZIP 解压到普通文件夹。
3. Windows 双击 **Install-Windows.cmd**；苹果电脑双击 **Install-macOS.command**。
4. 核对显示的 Codex 目录和规则块，输入 `y` 确认。期间不要同时编辑规则文件。
5. 新建一个 Codex 聊天，使用下面的自检提示核实功能。

macOS **待实机验证**。当前有 POSIX shell 语法/模拟和静态检查，不能据此声称 Finder、权限、Gatekeeper 或 Mac 上的原生 Codex 调用已通过。脚本未签名/公证，不自动修改安全设置；系统阻止运行时，先核对来源，再按 [Apple 官方说明](https://support.apple.com/en-us/102445)处理。也可在明确知晓脚本内容后用终端 `sh ./Install-macOS.command` 启动。

Python 缺失时入口会提示安装，不主动下载。可从 [Python 官网](https://www.python.org/downloads/)获取。Windows 入口兼容 `py -3` 和 `python`；Mac 使用 `python3`。多套 Python 时可设置当前进程的 `CODEX_SWARM_PYTHON` 为所选解释器的路径。

## 怎么用

安装后继续在 Codex 桌面聊天里提出任务，例如：

> 先给这个项目制定模块大纲和验收方案，确认范围后按蜂群规则推进。每次只做当前模块，通过验收后继续下一模块。

首次使用，发送：

> 请读取已安装的 Codex Swarm Kit 规则，并按 docs/SELF-CHECK.md 检查本机能力。先检查文件和当前工具/模型列表；如条件满足，运行一次不改业务文件的子代理验证。不要创建 Goal、发送换模消息或改我的设置。缺什么请明确说明，不要替换成别的模型。

主线程负责方向与最终判断；Sol 做技术规划和独立审核，Luna 做方案明确的执行，Terra 做多来源研究。型号按本次可调用列表选择，角色文件中的型号只是兼容快照。用户明确指定的模型和授权边界优先。主线程换模只能在宿主支持的新轮次边界发生，自动续轮另需明确 Goal 和消息授权；安装成功不等于这些能力已经可用。

## 检查、更新与恢复

在解压目录使用本机 Python：

```text
python setup.py preview
python setup.py doctor
python setup.py install
python setup.py restore --transaction <安装回执里的编号>
python setup.py resume --transaction <未完成事务编号>
```

Mac 通常将 `python` 换成 `python3`。入口脚本也接受这些参数。自定义目录用 `--codex-home <绝对目录>`，应与 Codex 的 `CODEX_HOME` 一致。更新时解压新包再运行安装入口；旧版本和备份保留。重复安装不会重复追加规则。

安装只在 Codex 主目录新增 `swarm-kit`，并合并 AGENTS.md 中一个受管块；保留其他规则，不改 config.toml、全局角色和账号信息。已有另一套调度规则会提示冲突，不自动覆盖。不要把已安装目录、个人 AGENTS 或事务备份作为分享包；分享生成的 ZIP。

- [安装与恢复的完整说明](docs/INSTALLATION.md)
- [规则和分工](docs/RULES.md) · [运行入口](docs/RUNTIME.md)
- [调度与验收合同](docs/CONTRACT.md) · [大纲协作](docs/OUTLINE.md)
- [验证范围与待验项](docs/ACCEPTANCE.md)

开发者可用 `python verify.py` 运行回归（另需已安装 Node），用 `python build.py` 构建白名单 ZIP 和 `.sha256`。校验值用于发现文件变化，不是发布者数字签名。没有公平效率实测，不承诺提速或费用下降。
