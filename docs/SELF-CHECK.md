# 在 Codex 内检查实际能力

此检查只验证本机，不能沿用另一台电脑的模型或工具清单。用户明确请求执行自检时，按下列范围行动；文档本身不提供权限。

1. 读取实际 Codex 主目录的 swarm-kit/active.json，核当前release、runtime解释器/platform。用解压包的 setup.py doctor 检查安装完整性；不要读取凭证或整个聊天历史。路径来自当前本机记录。
2. 检查当前原生工具是否提供子代理创建/等待/回报，以及实际可调用的具体型号和 reasoning effort。列出 Sol/Luna/Terra 家族可用与缺失情况；有需求的家族才阻塞对应工作。缺工具或目录不明确时报告缺口，不以网页、配置文件或模型自报替代。
3. 检查主线程切换所需的 read_thread、send_message_to_thread、本聊天可验证上下文；Goal 还要原生 get_goal/create_goal 和来源记录。只检查，不创建 Goal、不发送消息、不改模型设置。把“接口存在”与“已实测切换”分开。
4. 用户明确授权子代理自检且入口齐全时，在指定工作区创建新的隔离测试台账（不覆盖旧台账），使用安装release/runtime/routing.py 和 runtime/agents 走 template→init→add→prepare。任务仅让只读子代理返回一个固定的小结果。prepare 返回 blocked 就报告原因；delegate 原样调用原生 spawn_args 后立即 bind，不使用 create_thread 代替。文件任务包必须真实加载并校验。
5. 核输出、任务包hash、宿主停写记录，再 report/verify。可获取宿主实际model/effort就记录，无元数据记 unknown，不能用自报填充。实际型号不符不得验收。模型拒绝按原合同留证并交用户，不自动改用其他型号。
6. 最终分别报告：文件完整性、原生子代理入口、实际模型证据、主线程切换接口、Goal接口，以及未实测项。此自检不证明大任务质量、速度、费用，也不证明后台无人运行。

macOS 完成后仍需补：Finder启动、Gatekeeper实际表现、APFS大小写/权限、进程锁与中断恢复、原生子代理/日志路径。当前发行包将这些列为待实机，不将 Git Bash 或模拟测试算作 Mac 证据。
