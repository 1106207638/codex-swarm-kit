# 调度与验收合同

入口为当前 release 的 runtime/routing.py；角色目录为同目录 agents。下面的路径示例须换成当前机器绝对路径。

## 委派闭环

全任务限制必须原样传给同一用户任务拆出的每个工作项与子代理；CLI 不根据依赖关系猜测继承。任务包分别携带 controller_settings 与 whole_task_model_restriction。手选设置变化后，先核实宿主当前设置，只在无活动/未知操作时更新后续工作项 current；已准备操作按原快照收尾，不热改回执。

3.0 已准备的 direct 回执仅在旧决策与新语义可证明等价时兼容验收：旧 controller 的 inherit 解析为记录的 current.effort；旧无范围 override 必须恰好等于已记录的主线程 model/effort。仍严格核对原回执、输入、依赖与独立验收要求，保存原始 prepared_decision；该兼容不允许旧 override 发起新工作。3.1 新回执记录 policy_contract=3.1，不走旧格式兼容；其他旧形态保持冻结，按已有有证据的 reconcile 流程处理，不自动重做。

台账 prepare 保留目录来源合同，即使最终判定 direct，也要提供 catalog 与 catalog_provenance；可使用带当次真实来源的空 models 列表。该步骤不对照子代理列表验证 current 型号或强度。

为协作任务复用现有胶囊中的一个 `model-routing-state/`，每个工作区的相关任务共用它。简单直接操作不强制建账。使用台账的直接任务也必须 prepare/complete_direct，以便下游依赖放行。

每次 JSON 状态请求包含 `op,state_dir,owner,epoch`；init 不需 epoch。owner 是实际聊天/会话身份，epoch 从 init/status/handoff 结果取得。它是协调标识，不是密码或 OS 访问控制。

| op | 额外字段与结果 |
| --- | --- |
| template | 返回待填写 task 模板，无副作用 |
| route | task,catalog；只选路，不准备派发 |
| init | owner；只创建新台账，已有状态绝不重置 |
| status | 返回已有状态与事件；owner/epoch须匹配。其他聊天可只读查看文件 |
| call_log | 查询本台账调用日志；可传 task_id、attempt_id、call_id、after_line、limit |
| add / update | 完整 task；update 保存版本并使下游旧验收失效，活动写入期间拒绝更新 |
| prepare | task_id,catalog,catalog_provenance,profiles_dir；重新选路并先保存派发意图，返回 spawn_args 或 direct/blocked |
| bind | attempt_id,agent_id；关联宿主回执，同一回执幂等 |
| report | attempt_id,input_digest,agent_id,summary,evidence；只到 reported |
| verify | attempt_id,input_digest,verifier_context_id,evidence；验收 reported 结果，要求已停写，禁止执行者自签 |
| complete_direct | task_id,direct_id,input_digest,execution_evidence,acceptance_evidence,verifier_context_id；直接任务验收，不伪造代理 |
| fail | attempt_id,category,evidence，已绑定时 agent_id；category 为 model/input/environment/validation/checker |
| reconcile | attempt_id,action=no_spawn或stopped,evidence；核对失联/停止，不以超时猜测未派发 |
| reconcile_direct | task_id,direct_id,action=no_action或stopped,evidence；释放有证据的直接操作占用 |
| retry | attempt_id 或直接任务 task_id+direct_id，controller_reason或root_cause_and_fix,human_authorized；绑定最新尝试；可附精确 requested_model/requested_effort |
| handoff | new_owner,evidence；全部旧操作已明确结束后移交，epoch加一 |
| external_bind | attempt_id,input_digest,receipt；绑定外接 MCP 的真实提交回执 |
| external_poll | attempt_id,input_digest,receipt；登记查询进度或候选结果，终态附 summary/evidence；大纲任务附 handoff |
| external_resume | attempt_id,input_digest；取回同一冻结请求或已绑定的查询参数 |

完整示例以 `test_routing_cli.py`（每步独立 CLI 进程）和 `test_task_state.py` 为准。`prepare` 必须保存成功结果后，主线程才调用 **宿主** `collaboration.spawn_agent(**spawn_args)`，再用真实返回的 agent_id/path 调 bind；普通 Python 无法调用这些聊天工具。请求失败时读取错误原因，不重复发送未知状态的 spawn。

`catalog` 结构为 `{ "source":"runtime-tool", "models":[{"id":"具体型号","reasoning_efforts":["支持档位"]}] }`；另传 `catalog_provenance={"source":"runtime-tool","captured_at":"当次采集时刻","entrypoint":"collaboration.spawn_agent"}`。主线程每次新派发核对当次工具列表，不复用历史快照冒充实时来源。这些字段是调用方声明，程序不能认证真实性。

## 查询某次调用

外接本地智能体遵守同一台账的依赖和验收要求，派发与恢复详见 [外接使用说明](EXTERNAL-WORKER.md)。

从 1.0.1 起，经 CLI 向已有台账登记任务、派发、接收回执、提交结果、验收或变更协作状态时，台账目录的 `calls.jsonl` 会追加开始与结束记录。`route` 和 `main_recommend` 在传入有效 `state_dir,owner,epoch` 时才记录。独立预览、读取任务包、查询台账和查日志本身跳过记录。

```json
{"op":"call_log","state_dir":"替换为台账绝对路径","owner":"替换为当前协调者","epoch":1,"task_id":"任务编号","limit":100}
```

可单独按 `attempt_id` 查某次派发，或用 CLI 回执的 `call_log.id` 作为 `call_id` 查某一次命令。结果按追加顺序返回 `records`，每页最多 500 条。下一页把 `next_after_line` 填到 `after_line`，保持相同筛选条件。`skipped_lines` 是本次扫描遇到的中断残行或无效记录。

日志记录 UTC 时间、命令耗时、分派理由、请求的模型和强度、已提交的状态与失败类别。代理结果若带宿主型号证据，日志以 `reported_model` 和 `reported_effort` 记载调用方提交的字段。命令耗时是调度器处理时间。任务和尝试编号以摘要关联，任务正文、模型原始输出、文件路径、凭证和原始错误文字留在各自原有入口。日志局部诊断信息由本机台账目录的访问权限保护。

- `prepare` 的 `dispatch_pending` 表示已保存派发意图，`bind` 才表示已登记宿主回执。`report` 后仍需 `verify`。查询失败类别时，结合原 CLI 错误返回和宿主轨迹。
- 只有开始记录时，先用 `status` 和宿主回执对账，确认是否提交、是否已派发。`start_write_failed` 表示此次 CLI 未执行状态操作。`finish_write_failed` 表示操作已有返回结果，优先按原回执与台账核对。
- 日志由使用新版 CLI 的当前台账产生；旧状态兼容，过往调用无需迁移。原生工具直调、代理内部命令与绕开此入口的动作，需从对应宿主记录查询。日志追加保存在本地，随台账单独管理。

## 失败分类、任务文件与审核工具（3.5.0）

这三项扩展现有prepare/report/verify流程。原模型分工、手选与全任务限制、独立审核、模块顺序、三次尝试上限和模型故障人工决定保持有效。中枢必须核对真实工具轨迹；程序不能拦截绕开入口的操作，不能保证代理始终遵守自然语言约束。

### 事前声明检查，真实失败先留证

对已知自动验收，在task中填写check_plan，prepare时随输入冻结。例如：

```json
{"check_plan":[{"id":"unit","command":"python -B -m unittest test_feature","kind":"acceptance","expected_exit_codes":[0],"expected_signal":""}]}
```

kind可为acceptance（只接受0）、probe（普通资料发现）或expected_failure（事先约定的负向验证）。任何允许非零的检查都须填写可区分原因的expected_signal；普通probe的0结果不用匹配非零特征。首次TDD预期红灯只有事前声明并匹配原因才不算意外失败，不得失败后修改预期洗绿。命令及其输入应在准备前冻结，保存输入哈希并在签收时核对。

`record_check`仍带op/state_dir/owner/epoch，委派用attempt_id/input_digest/agent_id，直接操作用task_id/direct_id/input_digest。追加check_results：

```json
{"check_results":[{"id":"unit","exit_code":0,"error_kind":null,"observed_signal":"实际输出","evidence_ref":"宿主原始输出记录"}]}
```

error_kind为null/checker/model/environment/permission；退出码必须是真实整数，未获得退出码的错误可为null。也可在report/complete_direct同时提交check_results。真正的失败先落盘，再检查成功回执；多条结果中有效红灯不会因其他坏条被抹去，后续成功不能覆盖已记录红灯。尚未完成的检查、原输入/依赖不符、未解决失败都不能验收。

失败后代理停止扩大操作并回报中枢。中枢确认停止后reconcile stopped，再由retry保存根因和修复决定；直接任务同样用reconcile_direct stopped，再以task_id/direct_id调用retry。停止证据与重试授权分开，不能用猜测超时释放占用。两轮同类validation/checker失败需要真实用户决定，model失败仍human-only；input update不清历史，真正成功签收才分隔连续失败。旧台账按已存事件恢复原许可，不抹失败或另造任务绕预算。

record_check只用于已有冻结检查身份的结果。无计划旧回执、检查计划外新异常或派发前的模型拒绝，沿原fail/reconcile合同先保留失败，不能临时捏造check id；未接单模型错误仍须明确host_no_spawn_evidence。分类入口不取代原失败通道。

check_plan选填是旧调用兼容，不代表新任务可以省略已知验收。无计划回执明确为unclassified_legacy，不具备分类门禁；有计划回执不能漏交结果降为旧模式。新增检查需中枢按原update/prepare和尝试规则办理，不能在执行者手中改写冻结计划。

### 完整任务包按文件传递

新模板默认packet_transport=file。prepare在同台账packets目录保存UTF-8原包，包含完整角色职责、只读/模型约束、当前模块输入和唯一attempt/input身份，返回较短的spawn_args。主线程仍须原样调用宿主spawn_agent并立即bind；Python不代替原生派发。

代理先执行包内指定的 `routing.py --packet <绝对路径> --sha256 <摘要> --attempt-id <身份>`，完整读到JSON正文后才工作。该入口只读包文件，空参数/孤立校验参数拒绝，不回退到普通可写请求。只授权额外读取这份包，不授权读取整个台账或metadata指向的其他模块。加载失败立即报告，不修包、不猜路径。成功report须带packet_sha256，verify重新核对包字节和原台账内容；失败留证先于成功回执校验。

旧inline调用保持兼容。包文件不是新的派发许可；崩溃遗留的孤立文件也不允许重发，恢复仍先status，对未知派发按原流程冻结。宿主加密的消息正文不可作为明文JSON解析；真实读取证明来自loader的实际命令输出与原包对账，不能只信代理自报。

### 共用只读审核工具

运行 `python -B <脚本目录>/audit_tools.py --spec <绝对路径JSON>`。输入严格为：

```json
{"format":"codex-audit-input-v1","read_paths":["D:/project"],"snapshot_files":{"D:/project/input.txt":"实际64位小写sha256"},"artifacts":{},"metadata":{"outline_ref":"说明引用，不自动读取"}}
```

整个输入先校验，再读取明确声明且位于read_paths内的文件。snapshot_files/artifacts为文件哈希映射；metadata仅保存说明。metadata单独存在不能通过。哈希只证明读取时的字节，不能证明产品语义、源头真实性或跨进程隔离。

输出固定为 `{format:"codex-audit-v1",status,details:[...]}`：passed退出0，product_failed退出1，checker_error/evidence_unavailable/evidence_changed退出2。文件审核只检查字节；产品判断可以用库中的run_check，显式True/False或ProductFailure表示产品结论，其他异常/不合法返回值表示checker_error。JSON输出安全转义字符，解析后保留原说明。

主线程用 `normalize(原始输出, 实际退出码)` 检查二者一致，再以 `to_check_result(check_id, 原始输出, 实际退出码, 宿主证据引用)` 转为现有check_results。检查器故障映射checker，证据缺失/变更映射environment，产品不合格映射validation；原始报告保留具体区别。结构损坏、重复JSON键、过深输入、退出码与“通过”冲突均不能变成成功。该适配器不执行任意命令，host输出和退出码真实性由中枢核实。旧outline handoff字段不变；新增结果不能替代原scope/运行态/独立审核证据。

## 结果与模型证据

report 的可选 `host_stopped_evidence` 是明确的宿主已结束/停止证据字符串；没有它可在稍后 reconcile stopped。代理自己的“做完了”不足以证明已停写。尚未取得 agent 回执的模型拒绝须用 fail(category=model,host_no_spawn_evidence=明确未接单证据) 登记，不用无分类的 no_spawn 对账抹去模型故障。

`host_reported_model` / `host_reported_effort` 只填写可信宿主元数据，不填代理自报；没有则 null。已知与请求不一致时 verify 拒绝。台账保留 requested_model/effort，实际未知时如实写 unknown。usage 当前记录 unknown，不从时长推算 token 或费用。创建/更新时间提供事件时间，外部用量遥测尚未接入。

规划和审查分开上下文；新 attempt 的 bind 拒绝历史其他 attempt 用过的 agent。相同任务、型号、强度尚未结束的连续工作可 followup 原代理，留在同 attempt；改型号或强度、终结后重派必须新 context。独立审查一律新 context。

明确用户禁止委派时，可在完整保留禁止出处的前提下自查；complete_direct 对需要独立性的任务还要求 `limitation_acknowledged=true` 并记录隔离不足。仅缺授权或工具不可用不自动豁免独立验收。

## 恢复与限制

状态和事件在同一 state.json 原子提交，用 OS 文件锁串行写入；锁文件常驻。主线程每次派发前核对宿主当前总活跃名额；名额已满则排队，不通过子代理继续分裂绕过宿主上限。默认最多 2 个活动子代理，与主线程共占 3 个宿主并发名额；每任务最多 3 次派发尝试，跨输入版本保留，耗尽后停止，由主线程说明需要的新决定，不另造任务偷偷绕过预算。直接操作也占文件责任范围，但不占子代理名额。

状态实现名称：dispatch_pending 对应已准备但回执未知；bound 合并已派发/运行中；reported 是待验收；verified 才完成。failure_reported 保留尚未证明停止的占用；failed/no_spawn/stopped 是有证据的终态，stopped 也表达安全取消。审批/输入/环境/型号阻塞保存在判断和事件中，不制造假代理。

中断后先 status，核对 task/attempt/input/依赖版本及宿主可见代理。派发无回执不能自动重发：确认未接单才 reconcile no_spawn；确认旧执行者停止才 reconcile stopped；证据不足保持冻结。失效句柄不证明未执行。不能靠锁的年龄自动夺取协调权。handoff 需要旧操作全部结束和交接证据，新 owner 使用新 epoch，旧 owner 写入被拒。

同类验证失败两次后必须用户决定，普通首次失败修复要留根因和修复说明。模型失败锁跨版本保留；改输入、旧授权或换任务角色不能冒充人工批准。任务/上游变更会更新验收代次，旧报告、旧验收和旧直接完成回执不能签收新代次。

保证范围是经过此入口、共用同一 state_dir 的协作。不同胶囊、外部工具和绕开程序的进程不受它的文件责任检查；角色只读约定不提供 OS 沙箱。列表来源、审批、停止证据均需主线程核对，不能称为全工具不可绕过。桌面退出后不保证原生代理存活，也不承诺后台继续调度。

## 验证与恢复安装

```powershell
python -B -m unittest -v
node --test test_main_switch_host.js
```

在源码目录执行。真实入口验收另需原生派发回执、结果验证；单元测试与配置不能证明后台实际模型。版本号、固定分工和动态分工的同输入对照只是有限样本，不能证明通用质量或节省比例。

## 蜂群任务复盘

普通委派时，总控为本次用户目标生成一个新的不含业务信息的编号，给各工作项填写同一个 `task.recap_scope`，重试和续接沿用。登记大纲已经确定了范围。用 `task.recap_assignment` 写一句可展示的分工，提前终止时也能交代这项工作原本要做什么。所有操作复用同一台账、owner 和 epoch。

总控核对实际结果后，用以下操作留下可展示的分工、结果与用途。三个摘要仅填写经总控核实、适合给协作使用者看的内容。任务正文、原始回执、文件路径和凭证留在原有访问范围。用 `partial` 写明取了哪部分，`returned` 写清退回原因，`unused` 写清弃用原因。采用要求此前已完成当前版本的独立验收。一次尝试的决定可原样重放；新一轮返工按新的尝试分别说明。

```json
{"op":"recap_note","attempt_id":"<本次尝试>","input_digest":"<冻结输入摘要>","choice":"adopted","assignment":"核对三条记录的执行状态","output":"三条记录均已分类","reason":"逐条与原始回执一致，用作验收样例"}
```

在正常结束和提前终止时，将收尾回执的 `recap_query` 加上原台账参数提交。也可直接查询。

```json
{"op":"swarm_recap","scope":{"group":"<本次目标编号>"}}
```

大纲使用 `{"outline":"<登记的 ref>"}`。旧普通任务使用 `{"legacy_tasks":["<原工作项 ID>"]}` 显式列全，先从原计划核范围。旧账没有留下的处置理由标明缺失。查询不补写历史。返回 `text` 是可读底稿，`attempts` 包含执行者、状态、依赖工作项摘要、当前版本有效性、结果用途及证据。每条 `calls` 的 start/finish 是同台账 calls.jsonl 从 1 开始的行号，id 可以交给 call_log 查询。另列当前范围内可归属的调度拒绝。缺少回执、处置说明或日志时照实报告；长任务将完整明细另存为 UTF-8 文本，最终答复逐项概括，附本地日志链接。桌面端由主线程在最终回复中呈现。

新任务正常收尾时逐项处理 `needs_decision`，补记已终止尝试的真实用途并重新查询。提前终止或旧任务无法补证时，在答复中说明缺口。只将有执行回执的尝试计入使用次数，同号查询与幂等重放只增加日志行。诊断日志保留可识别的原生型号，外接型号仅返回摘要。



