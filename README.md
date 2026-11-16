# Codex Swarm Kit

**Codex Swarm Kit 是专为 Codex 桌面端设计的蜂群协作与模型调度工具包。** 安装后仍在 Codex 聊天里下达任务。简单任务由主线程直接处理，有明确大纲的复杂任务由主线程统筹，按需要调用 Codex 原生子代理。

## 能做什么

| 功能 | 实际用法 |
| --- | --- |
| 按任务选择模型和推理强度 | 主线程负责方向和最终判断。Sol 做技术规划和独立审核，Luna 做方案明确的实现和整理，Terra 做多来源研究。每次派发前核对当前 Codex 能调用的模型和推理档位。 |
| 按大纲推进多模块任务 | 先明确目标、模块、依赖和验收，再逐批实施。主线程分派当前模块，核对结果和独立审核后放行下一批。 |
| 明确子代理的工作边界 | 把输入、文件范围、角色约束和完成标准写进任务包。子代理领取任务后校验任务包版本，并按其中的范围工作。 |
| 分开执行和验收 | 实现和独立审查使用不同上下文。主线程检查产物、测试输出和宿主回执，记录失败原因和验收结果。 |
| 从中断处恢复 | 任务台账保留派发、回执和验收记录。恢复任务时可按已有计划和台账核对交接点。回执未知时先查清状态，避免重复派发。 |
| 主线程换模与 Goal 续跑 | 当 Codex 提供相关接口且用户已授权时，可在阶段交接处为下一轮选择模型和推理强度。使用 Goal 自动续轮需要用户明确启动持续目标，并另行授权续接消息。 |

桌面手选的模型和推理强度是主线程的起始设置。用户明确固定主线程或要求全任务统一模型时，按指定范围执行。子代理、模型和 Goal 的可用性取决于当前 Codex 环境和账号权限。

## 安装

在 [GitHub Releases](https://github.com/1106207638/codex-swarm-kit/releases) 的 Assets 下载部署 ZIP 和 `.zip.sha256` 校验文件。

1. 安装并登录 Codex 桌面端，准备 **Python 3.11 或更新版本**。
2. 将整个 ZIP 解压到普通文件夹。
3. 在 Windows 双击 **Install-Windows.cmd**。在 Mac 双击 **Install-macOS.command**，也可以在解压目录运行 `sh ./Install-macOS.command`。
4. 核对显示的 Codex 目录和规则块，输入 `y` 确认。期间不要同时编辑规则文件。
5. 新建一个 Codex 聊天，使用下面的自检提示核实功能。

Python 缺失时入口会提示安装，不主动下载。可从 [Python 官网](https://www.python.org/downloads/)获取。Windows 入口兼容 `py -3` 和 `python`；Mac 使用 `python3`。多套 Python 时可设置当前进程的 `CODEX_SWARM_PYTHON` 为所选解释器的路径。

## 怎么用

安装后可以在 Codex 桌面聊天里这样提出大任务。

> 先给这个项目制定模块大纲和验收方案，确认范围后按蜂群规则推进。每次只做当前模块，通过验收后继续下一模块。

首次使用可以先让 Codex 检查本机能力。

> 请读取已安装的 Codex Swarm Kit 规则，并按 docs/SELF-CHECK.md 检查本机能力。先检查文件和当前工具/模型列表；如条件满足，运行一次不改业务文件的子代理验证。不要创建 Goal、发送换模消息或改我的设置。缺什么请明确说明，不要替换成别的模型。

## 检查、更新与恢复

在解压目录可以使用以下命令。

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

开发者可用 `python verify.py` 运行回归（另需已安装 Node），用 `python build.py` 构建白名单 ZIP 和 `.sha256`。校验值用于发现文件变化，不是发布者数字签名。
