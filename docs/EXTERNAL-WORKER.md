# 总控调用外接本地智能体

总控可以把一份已准备好的文字材料交给外接助手处理，同时继续当前模块内独立的工作。助手返回候选结果，总控对照标准验收后才放行下游。用现有 MCP 服务的 local-worker/v1 协议，通用助手可单独维护。

## 适合交给它的工作

| 材料与目标 | 任务 kind | 总控核对 |
| --- | --- | --- |
| 工单、反馈、日志的初步标签 | classify | 标签、编号、漏项和错分 |
| 描述中的字段、差异、动作建议 | extract | 字段类型、空值、原文依据 |
| 日志或文案翻译 | translate | 术语、编号、否定与数值 |
| 已有材料的归纳和证据查找 | summarize | 引文逐字存在且支持结论 |

总控负责取数、说明判断规则和最终决策。外接助手处理提供的文本，系统操作由主流程按授权执行。代码草稿、工单修复建议等作为文字候选，经过总控及对应检查后采用。

## 接入和派发

需要频繁换模型服务时，在外接助手目录运行 `python local_agent.py configure --endpoint http://127.0.0.1:8080/v1`（示例地址请换成实际地址）。Windows 可双击助手自带的 `Change-Model-URL.cmd`。只有一个模型时自动识别，多个模型或服务未提供模型列表时加 `--model 模型ID`。助手与命令需使用同一个队列目录。URL 支持局域网或回环 IPv4 的 HTTP(S) 模型 API 基址，以 /v1 结尾。

蜂群 1.0.4 与支持快速换址的助手配套使用。更新现有助手脚本后首次重连 MCP，后续换址只运行配置命令。总控用最新能力回执准备工作，原样提交带 `backend_ref` 的参数。切换前已提交的工作按原编号查询；只准备、尚未提交的工作如果遇到地址或型号变化，会在入队前被拒。总控先对账，再按原重试规则重新准备。旧版固定地址助手仍沿用原接口。

1. 在使用环境中启用 local_worker MCP，确认有 describe_protocol、submit_task、get_task 三个工具。助手服务的安装和启动由该服务维护。部署蜂群工具包后可从当前工具列表核对。
2. 总控调用 describe_protocol 取得当前协议、配置模型、任务类型和限制，然后填写工作项的 external_worker。模型名使用该次服务实际返回值。主线程手选型号继续按原规则判断，全任务固定模型时外接派发会被阻止。
3. 调用原 prepare，同时传 external_capabilities 为本次 describe_protocol 的结构化结果。原 catalog 与 catalog_provenance 仍需据实填写，原生代理列表可为空。
4. prepare 返回 external_call，其 tool 指向 mcp__local_worker__submit_task。由总控使用原生工具提交原样 arguments。请求编号由台账按此次尝试生成，内容和标准在 prepare 时冻结。
5. 用 external_bind 登记提交回执，再用 get_task 查询。查询回执交 external_poll。成功结果停在 reported，失败结果登记失败类别。queued/running 可以稍后继续查询原编号。
6. 总控核对内容，使用原 verify 验收。依赖项在 verify 后放行。大纲工作项继续要求当前模块、结构化 handoff 和该模块的独立审核。

external_bind 和 external_poll 都传 state_dir、owner、epoch、attempt_id、input_digest 和 receipt。其中 receipt 为真实 MCP 结构化回执。external_poll 在终态还需 summary、evidence，大纲任务附 handoff。MCP 的工具错误先交回总控核查，工具错误文本不可拼成成功回执。工具来源和回执真实性由总控对照实际调用核验。

以下 external_worker 填入原 task。其余任务授权、范围、依赖和验收字段按原模板填写。

```json
{
  "external_worker": {
    "protocol": "local-worker/v1",
    "model": "替换为describe_protocol返回的configured_model",
    "reason": "总控选定对已提供材料进行有标准的初筛",
    "task": {
      "kind": "classify",
      "instruction": "闪退归故障，使用方法问题归咨询。",
      "items": [{"id": "EX-01", "text": "点击保存后闪退。"}],
      "labels": ["故障", "咨询"]
    },
    "standard": {
      "criteria": ["保留原编号，依据材料判断。"],
      "checks": []
    }
  }
}
```

features 用 execution、clear、plan_ready=true，risk 为 low 或 normal，complexity 为 simple 或 normal。available_tools 须含 mcp__local_worker__describe_protocol、mcp__local_worker__submit_task 和 mcp__local_worker__get_task，所需工具也填入 required_tools。总控根据任务事实填写，存在歧义时先澄清。外接工作使用 standard.checks 做机器校验，check_plan 中的命令检查由主流程工作项承接。

## 中断和失败

提交回执丢失时用 external_resume 读取原冻结请求，同时传本次 external_capabilities 和 recovery_evidence。总控须核对服务仍使用同一共享队列及后端，并用服务合同或实际同号回执证明幂等，即相同请求只产生一单，保留证明来源。程序会检查本次能力、大纲、输入快照和依赖，再给出原请求。已绑定任务返回原 get_task 参数。无法确认服务或队列身份时保留未知状态，交回总控处理。换队列可能失去服务端去重记录，恢复时必须核对环境。

后端故障登记为 model，延续人工决定规则。v1 服务将型号不符也归为 invalid_output，因此该类别按后端故障处理。明确的自动验收失败 acceptance_failed 记录为 validation，重试需要总控记录根因与修复说明。每任务三次尝试、连续失败停点和全任务模型限制继续有效。

已有作业在故障上报后仍可按原编号查询。后续的终态回执可确认队列作业结束，并保留原失败与候选结果，交总控处理。

同一台账最多一个活动外接请求，原生子代理的两个名额独立计算。多个台账可向同一服务提交，其共享队列逐单执行。各自独立的队列由服务维护者协调。服务接口当前支持提交和查询，运行中的推理停止能力取决于服务端。总控可以决定拒收候选结果。

## 日志和维护

calls.jsonl 记录 prepare、external_bind、external_poll、external_resume 及 verify 的时间、状态、失败类别和编号摘要。模型名及候选原文保留在任务台账与服务回执中，诊断日志用 external_model_ref 关联。原生模型的实际运行证据仍遵守原合同，外接回执的来源是 MCP 服务。

通用协议的边界校验随蜂群版本冻结在 external_contract.py。升级助手或协议后，以 describe_protocol 和正负样本复核兼容性，再安排边界升级。
