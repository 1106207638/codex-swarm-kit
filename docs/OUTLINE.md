# 有大纲任务的协作入口

这份说明补充原 README 的委派闭环。主线程负责决定是否使用本模式，不按文件名或标题自动触发：任务须有已确认的目标、范围、模块成果、依赖和验收。普通小修继续原路径；只要求分析时不实施。已在执行的任务先恢复原台账，不能为套用新模式重复派发。

## 选择与边界

显式 `mode: "outline"` 使用大纲协作；省略 mode 或 `mode: "normal"` 保留原规则。默认中枢要求实际 `gpt-6-astra`，按风险选择强度；`main_recommend` 不因当前子工作项是实施或研究而把中枢降为执行模型。仅手选其他模型不等于固定：按原主线程设置入口、已有授权及真实轮次边界处理，当前模型未符合时停止相关派发，不假写 current。

用户明确固定 model、effort 或 no_switch 时，将其及 source 放在 `controller_pins`，仅限制相应字段；全任务同模型仍用 `override`。仅统一型号不统一角色强度。禁止切换可保留实际主模型。明确禁止委派时保留原 direct 例外，需独立性的验收必须记录 `limitation_acknowledged` 和隔离不足；缺授权、缺工具不等于禁止委派。

`operation: "controller"` 只允许 `features.stage: "strategy"`，用于调度、裁决和证据核对。常规业务用 `operation: "business"`；准备清楚后交 Luna，调查/技术规划交 Sol，研究交 Terra，独立审核交新的 Sol 上下文，具体型号仍查当次宿主目录。不能把实现标成 controller 绕过委派。

当前版本不支持大纲模式下高风险业务的自动执行拆分：必须保留真实 risk，阻塞时由中枢回到原高风险工作流处理，不能在已登记的大纲台账中漏填 mode 或改低风险绕过。要改变任务范围/执行约定，先明确调整原计划和授权。Goal 自动跨轮续跑按 README 的显式 Goal 合同接入，用户明确启动及消息发送授权分别核对。大纲模式保持有效中枢限制，子业务难度不触发中枢频繁换模。

## 登记同一份计划

复用胶囊唯一 `model-routing-state`；先 status 并核对宿主，无活动、未知或未签收旧操作后，才显式 `outline_register`。它将该台账升级为 schema 2。未登记的旧台账仍是 schema 1，不自动转换；旧 3.2.1 拒绝 schema 2，不可直接用旧脚本恢复新台账。

请求共同字段为 `op,state_dir,owner,epoch`。`outline_register` 的 `outline` 对象完整字段如下；所有路径、哈希和授权必须按事实填写，示例不是授权：

```json
{
  "ref": "D:/project/task_plan.md",
  "revision": "v1",
  "sha256": "计划文件当前字节的64位小写sha256",
  "source": "已核实的用户目标和实施授权出处",
  "controller_pins": {},
  "override": null,
  "delegation": {"authorized": true, "prohibited": false, "source": "实际委派授权出处"},
  "modules": [{
    "id": "parse",
    "requirement_ids": ["R1"],
    "acceptance_ids": ["A1"],
    "work": [
      {"task_id": "parse_impl", "operation": "business", "stage": "execution", "requirement_ids": ["R1"], "acceptance_ids": ["A1"]},
      {"task_id": "parse_review", "operation": "business", "stage": "review", "requirement_ids": ["R1"], "acceptance_ids": ["A1"]}
    ]
  }],
  "final_acceptance_ids": ["integrated"]
}
```

module/work 的编号稳定且唯一；工作覆盖本模块全部需求和验收项。当前合同每个模块含 review 工作项，不要求额外规划代理；明确禁止委派时按原直接审核例外处理。审核依赖同模块全部非审核工作，实际验收核对 review 的独立角色回执，不仅看 stage 文本。全局限制登记一次，各工作项完整继承，不能凭依赖猜测。

原 Markdown 计划仍负责目标/决定；登记的 ref 以字节哈希冻结。可变进度记在同胶囊 progress 中，避免随手改已冻结计划。如果既有唯一计划必须更新，按下方 revision 流程保存，不另造竞争计划。

## 当前模块任务包

先调用 `template` 补齐原完整 task，再增加以下字段。`features.stage` 须与登记一致；执行必须 plan_ready=true、ambiguity=clear。任务只携带当前模块说明，后续模块可以登记身份，不提前交付实施内容。

```json
{
  "mode": "outline", "operation": "business",
  "outline_ref": "D:/project/task_plan.md", "outline_revision": "v1",
  "module_id": "parse", "requirement_ids": ["R1"], "acceptance_ids": ["A1"],
  "controller_pins": {},
  "resources": ["workspace:project"],
  "read_paths": ["D:/project/parse-brief.md", "D:/project/checks.py"],
  "snapshot_files": {"D:/project/parse-brief.md": "实际sha256", "D:/project/checks.py": "实际sha256"},
  "artifacts": ["D:/project/parser.py"]
}
```

artifacts 为输出文件，必须落在 ownership。read_paths 是许可输入范围，snapshot_files 是需要固定版本的实际文件；审核必须包含依赖已签收产物及对应哈希。read_paths 与 ownership 不提供 OS 隔离，主线程仍需核对工具轨迹。outline_ref 是关联，不自动赋予读取整个计划或其他组产物的权限。

共享设备、GUI 会话、端口等用稳定同名 resources 表达独占占用，不同名字不会被程序识别为同一设备。文件读写检查只覆盖同台账的声明；原未接单/已停止证据规则不变。`prepare` 除原参数外，委派需 `host_slots_available`（主线程刚核对的剩余子代理名额，0–2）；未绑定派发会占预留名额。不靠子代理再分裂绕过上限。

沿用 prepare → 原样原生 spawn_args → bind → report → verify。未知回执先冻结并对账，不再派；模型故障仍 human-only，已有唯一 Spark 例外不扩张。`direct` 不生成代理，但也需原完成回执。

## 交接与逐模块放行

outline 的 report/complete_direct 增加 `handoff`：

```json
{
  "artifacts": {"D:/project/parser.py": "实际64位小写sha256"},
  "checks": [{"command": "实际执行的命令", "exit_code": 0, "expected_exit_code": 0, "evidence_ref": "实际输出记录路径或工具块"}],
  "remaining": [], "risks": [],
  "scope_evidence": ["中枢核对过的工具轨迹和范围证据"]
}
```

只读审核没有输出文件时 artifacts 为 `{}`，被审文件放在 snapshot_files。report 可记录失败/未完成结果；verify 不接受未验项、退出码不符、缺产物、输入或输出哈希变化。命令字符串/证据路径只是声明，主线程必须核对实际执行结果；哈希只证明字节，不证明质量。

| op | 额外参数与条件 |
| --- | --- |
| outline_status | 返回当前模块、工作状态、plan_current、complete；ready_for_prepare 仍需资源/模型/授权检查 |
| outline_accept | module_id, acceptance_evidence（验收ID→非空证据列表）, runtime_evidence（非空列表）, verifier_context_id=owner；当前模块所有工作已验证、输入有效且无活动/未知操作才放行下一模块 |
| outline_revise | outline（完整新登记）, from_module（首个受影响旧模块）, reason；先停写、核对影响，再使用从未使用的新 revision；保留原模块顺序及工作身份，不通过改名清除预算；全局限制变化从首模块失效 |
| outline_complete | evidence（final_acceptance_ids→非空证据列表）, verifier_context_id=owner；全部模块有效后再核对集成行为及最终冻结输入/输出 |

update 单个工作项会使本模块和后续模块旧验收失效，即使漏了显式依赖也不会保留过期签收。修改计划则先 outline_revise，再 update 受影响工作项；历史原样保留。最终集成允许后模块以签收输出替换先前文件版本，不能靠未登记改写冻结输入伪造通过。整体验收之后如有变更，重新进入受影响模块，不能拿旧完成状态签收新版本。

## 可复现检查与恢复

源码测试 `test_outline_policy.py`、`test_outline_state.py`、`test_outline_cli.py`、`test_work_evidence.py` 提供完整 API 合同与跨进程例子；CONTRACT.md 的测试命令包含这些测试。实际模型需要原生调用及宿主 turn_context，测试不替代它。

迁移/回退先停止原操作并备份完整台账。安装不修改既有台账。新台账必须使用能理解全部已保存门禁的 3.5.0 或兼容入口读取，禁止降 schema、删字段或还原旧台账抹掉已执行历史。恢复代码时先比对当前文件与安装 hash；后来其他聊天有变更则逐项处理，不整目录覆盖。新文件回退时可以保留未被旧入口引用的文件，无需删除。

本包提供顺序、限制与回执回归。目标电脑原生协作须单独实测；历史电脑证据不能替代本机验收。不宣称速度或费用收益。




3.5.0可靠性扩展沿用 CONTRACT.md 的冻结check_plan、文件任务包和audit_tools合同，适用于现有outline工作，不改变原模块放行与handoff结构。outline_ref等引用是元数据，只有snapshot_files/artifacts声明的文件参与审核哈希；文件hash不能替代实际过程和运行验收。使用新字段的台账必须继续由3.5.0或兼容入口处理，不能退回旧CLI跳过分类或包回执；回退保留完整台账和失败历史。
