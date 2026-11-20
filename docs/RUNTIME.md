# 运行入口

安装器生成 active.json 指向当前 release，其中 runtime 字段记录实际 Python、platform、Codex 主目录。运行脚本在 release/runtime；角色在 release/runtime/agents；显式传 profiles_dir。新电脑从新台账开始，旧台账含绝对路径和历史回执，不能当作可复制配置迁移。

Python 3.11+，仅标准库。先用绝对解释器运行 `runtime/routing.py --request <JSON文件>`；或向 stdin 传单个 JSON。输出 `{ok,result}`；常规拒绝退出 2。`decision.action=blocked` 即使退出 0 也禁止派发。`{"op":"template"}` 返回完整 task 模板，必须据实填写，模板没有授权。完整流程见 CONTRACT.md，模块协作见 OUTLINE.md。所有宿主声明和授权事实均由中枢核对。

Windows 的 task bootstrap 使用 PowerShell，POSIX 使用 /bin/sh。路径和解释器逐参数转义，明确 exec_command.shell 和 login=false。不要把 POSIX 命令转交 PowerShell，或相反。

支持日志的台账调用会附带 `call_log` 回执。现场排查用 `op: "call_log"` 查同一台账内的记录，具体筛选字段及中断后的对账方式见 [CONTRACT.md](CONTRACT.md#查询某次调用)。

## 主线程切换

此能力依赖 Codex 宿主提供 read_thread、send_message_to_thread、本地会话日志；Goal 还依赖原生 create_goal/get_goal 与可验证来源事件。安装不保证目标电脑具备这些接口或任何特定模型。缺少入口时保留当前主线程处理可做工作，显式固定条件不满足则停止受限工作。

1. `main_observe` 读取本聊天 rollout 的实际线程、轮次、model/effort。日志只从当前 CODEX_HOME/sessions（未设则用户 ~/.codex/sessions）读取。安装目标用自定义 home 时，应与 Codex 启动环境 CODEX_HOME 一致，不从另一个目录猜日志。
2. `main_recommend` 输入 task,catalog,substantial,pins；catalog 必须来自当前发送入口的声明。pins 仅记录明确固定字段和 source。结束旧操作，持久化交接目标/成果/剩余，不能为短步骤频繁切换。
3. `main_switch_prepare` 带 task_id,thread_id,host_id,before_turn_id,rollout_path,checkpoint,phase,substantial,pins,catalog,catalog_provenance,authorization 及台账 state_dir,owner,epoch。owner 为本聊天 UUID 加 -root。authorization={message_id,answer,purpose,max_sends,reviewed_through}，常规 purpose=task_continuation；实际用户许可原文格式为“允许在本聊天为当前任务自动续接1次”（次数1–3），不可替用户作答。获取真实消息身份并检查后续没有撤回。用户自行明确持续目标才创建 Goal，另外具备消息授权后传 next_source=goal 和本次原生 get_goal 返回的 goal_state。
4. 在当前主线程加载 `runtime/main_switch_host.js`，作为表达式获得 bridge；调用 `bridge.catalog(ALL_TOOLS)` 读取当前目录。调用 `bridge.stage(tools, ALL_TOOLS, {script,state_dir,owner,epoch,request_id,runtime})`；runtime={platform:"windows"或"posix",python:"实际绝对解释器"} 来自本次安装记录，script 指 routing.py。桥接显式选 shell，不推断默认 shell。
5. staged 仅是下一轮设置已保存。默认用户新消息或授权 Goal 续轮后，调用 `bridge.confirm(tools,{script,state_dir,owner,epoch,request_id,runtime})` 和 main_observe 核实际来源与型号。主线程不能通过子代理给父聊天续接。工具失败/unknown/mismatch 冻结且不重发；先 main_switch_get 恢复事实。只有 prepared 且未调用才可取消。解除未知按 CLI 原合同要求新的明确用户许可与停止证据。

桥接确认会直接复查 Goal。它不提供查询与发送的原子锁、不保证应用退出后运行，也不绕过宿主消息授权。新安装必须实际验证，旧电脑的运行证据不代替新电脑证据。

未知结果解除：先证明旧执行者已停止；在该次尝试之后，用户须明确发送“允许解除本聊天续接请求 <请求编号> 的冻结”。将真实消息作为 authorization（purpose=resolve_switch，max_sends=0）连同停止 evidence 交 main_switch_resolve。不能把旧许可、目标创建许可或代理自报当作解除依据；解除不自动授权再发消息。完整参数以运行时校验和 test_main_switch.py 为准。
