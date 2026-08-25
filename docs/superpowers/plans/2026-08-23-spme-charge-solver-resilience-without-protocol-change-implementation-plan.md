# SPMe标准充电求解稳定性修复实施计划

- 文档日期：2026-08-23
- 适用工程：`E:\SPMe`
- 上位设计：`E:\SPMe\docs\superpowers\specs\2026-08-23-spme-charge-solver-resilience-without-protocol-change-design.md`
- 文档状态：待用户审查
- 实施授权：本文只规定实施顺序，不授权启动生产仿真
- 核心约束：不改变实验充放电流程、控制量、终止条件、模型参数或分析规则

## 1. 实施目标

把现有四次独立Simulation的标准充电执行方式改为一次包含四步的
PyBaMM Experiment，并增加一次从充电前快照开始的确定性保守重试。

实施完成后，协议层仍严格观察到：

```text
3c_cc -> 4v_cv -> c4_cc -> 4p2v_cv
-> post_charge_rest -> step5_c4_discharge -> step6_udds
```

本次只改变数值执行边界、错误结构、审计和恢复能力。现有科学输出的列名
和含义保持不变。

## 2. 固定版本决策

本次实施采用以下版本策略：

```text
output_schema_version = 3                         # 不变
protocol_algorithm_version = w10-window-v3-charge-efficiency  # 不变
checkpoint_schema_version = 5                    # 从4升级
solver_execution_version = standard-charge-sequence-v1
solver_attempt_audit_version = solver-attempt-v1
```

检查点升级到schema 5，因为检查点必须持久化并验证
`solver_execution_version`。旧schema 4检查点只能用于隔离诊断，不能在新版
正式结果目录中恢复。

## 3. 工作约束

### 3.1 不变量

实施和测试中不得修改：

- `charge_3c_a=14.55`；
- `discharge_c4_a=1.2125`；
- 4.0 V与4.2 V目标；
- `cv_cutoff_a=0.05`；
- 1800 s充电后静置；
- Step 5容量目标；
- Step 6 UDDS波形及事件保护；
- RPT节点与流程；
- `rtol=1e-5`与`atol=1e-7`；
- SPMe模型选项及退化参数；
- SOC、电量、库存和效率算法。

### 3.2 数据保护

- 不修改或删除`outputs/pybamm_spme/w10-350-spme-uncalibrated-v1`。
- 不覆盖任何既有检查点或失败工件。
- 单元测试临时文件写入工程内独立临时目录。
- 第10圈诊断输出、0–25回归输出和350圈正式输出必须使用三个不同目录。
- 350圈正式运行必须经过独立用户授权，本计划完成不等于授权启动。
- `E:\SPMe`当前不是Git仓库；实施前必须生成源文件和测试文件的SHA-256
  基线清单，以便审计和人工回退。

### 3.3 测试优先

每个编码任务依次执行：

1. 添加能够证明当前缺失行为的失败测试；
2. 运行最小测试集合并确认按预期失败；
3. 完成最小实现；
4. 重跑目标测试；
5. 运行相邻模块回归；
6. 到阶段门槛时运行全量测试。

测试解释器固定为：

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe
```

测试关闭pytest缓存和字节码写入，并使用工程内临时目录。

## 4. 关键PyBaMM行为约束

连续Experiment必须显式使用一个包含四步的cycle：

```python
pybamm.Experiment([
    (
        pybamm.step.current(-config.protocol.charge_3c_a, termination="4.0 V"),
        pybamm.step.voltage(4.0, termination=f"{config.protocol.cv_cutoff_a} A"),
        pybamm.step.current(-config.protocol.discharge_c4_a, termination=f"{config.cell.upper_cutoff_v} V"),
        pybamm.step.voltage(
            config.cell.upper_cutoff_v,
            termination=f"{config.protocol.cv_cutoff_a} A",
        ),
    )
])
```

不得使用未分组的四元素列表，因为它可能被PyBaMM解释为四个单步cycle。

使用`starting_solution`时，新执行cycle不一定是`solution.cycles[0]`。实现
必须记录求解前cycle偏移，并选取本次新增cycle；在验证cycle总数后，可等价
使用`solution.cycles[-1]`。

PyBaMM 26.7.1在同一Experiment的后续步骤失败时，可能停止当前cycle并返回
部分解，而不是向调用者重新抛出异常。因此实现必须同时：

1. 用回调捕获`on_experiment_error`中的底层`SolverError`；
2. 求解返回后验证新cycle恰好包含四个步骤；
3. 若步骤不足，使用回调捕获的底层错误构造结构化失败；
4. 若没有捕获到底层错误，也必须以`INCOMPLETE_CHARGE_SEQUENCE`失败；
5. 禁止把部分cycle当作成功状态提交。

## 5. 任务1：建立实施基线和协议不变量测试

### 修改/新增文件

- 新增`tests/test_standard_charge_protocol_invariants.py`
- 新增实施基线清单，位置使用`tmp/solver-resilience-baseline/`
- 不修改生产代码

### 测试内容

对`RunConfig`和协议构造进行显式断言：

- 四个充电阶段名称及顺序；
- 两个CC电流；
- 两个CV电压；
- 两个CV截止电流；
- 充电后静置时间；
- Step 5和Step 6仍在标准充电之后；
- RPT节点未改变；
- `rtol`和`atol`未改变。

测试应直接失败于“尚无连续标准充电步骤构造器”，而不是复制生产常量后
自行通过。

### 验证命令

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -m pytest -q -p no:cacheprovider tests/test_standard_charge_protocol_invariants.py
```

### 完成条件

- 协议不变量测试能够在后续每个阶段防止意外改流程；
- SHA-256基线包含`src`、`tests`、`scripts`、`README.md`、`pyproject.toml`
  和设计/计划文档；
- 基线不包含大型输出和检查点。

## 6. 任务2：增加求解执行版本与配置模型

### 修改文件

- `src/pybamm_w10/config.py`
- `src/pybamm_w10/types.py`
- `tests/test_charge_config_v3.py`
- `tests/test_checkpoint_schema3.py`
- `tests/test_model_construction.py`

### 实现内容

在`RunConfig`增加：

```python
solver_execution_version: str = "standard-charge-sequence-v1"
solver_attempt_audit_version: str = "solver-attempt-v1"
```

将两个版本加入：

- `__post_init__`非空校验；
- `normalized()`；
- `to_json()`；
- 配置指纹；
- 有效参数/环境审计所需元数据。

将`checkpoint_schema_version`从4升级为5。在`Checkpoint`增加
`solver_execution_version`，保存和加载时做严格相等校验。

输出schema和物理协议算法版本保持不变。

### 测试内容

- 新配置序列化包含两个版本；
- 配置指纹随求解执行版本变化；
- schema 5检查点能加载；
- schema 4被明确拒绝；
- 求解执行版本不同的schema 5检查点被拒绝；
- output schema仍为3；
- 协议算法版本未变化。

### 阶段回归

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -m pytest -q -p no:cacheprovider tests/test_charge_config_v3.py tests/test_checkpoint_schema3.py tests/test_model_construction.py tests/test_standard_charge_protocol_invariants.py
```

## 7. 任务3：建立结构化求解失败和求解配置

### 修改/新增文件

- `src/pybamm_w10/types.py`
- `src/pybamm_w10/model.py`
- 新增`tests/test_solver_profiles.py`
- 新增`tests/test_solver_failure_classification.py`

### 类型设计

增加不可变类型，职责至少包括：

```text
SolverProfile
  name
  dt_init_s
  max_step_s
  max_num_steps
  max_error_test_failures
  max_order_bdf
  rtol
  atol

SolverStepFailure
  sundials_error_code
  raw_message
  failed_step_index
  charge_stage
  retryable
  original_exception
```

增加单一错误分类函数。优先读取结构化错误码；PyBaMM未暴露时，只在该函数
中解析消息。白名单固定为：

```text
IDA_ERR_FAIL
IDA_CONV_FAIL
IDA_LSETUP_FAIL
```

### 求解器工厂

将`build_spme()`中的IDAKLU构造抽成可测试的求解器工厂。定义两个固定
profile：

```text
default:
  rtol=1e-5
  atol=1e-7
  dt_init=0
  dt_max=1
  max_num_steps=200000
  max_error_test_failures=10
  max_order_bdf=5
  suppress_algebraic_error=false

conservative_cv_transition:
  rtol=1e-5
  atol=1e-7
  dt_init=1e-8
  dt_max=1
  max_num_steps=200000
  max_error_test_failures=30
  max_order_bdf=3
  suppress_algebraic_error=true
```

不得修改线性求解器、模型选项或容差。保守重试从局部误差测试中排除
代数变量，但仍严格求解代数残差和恒压约束；该设置不适用于首次尝试。

### 测试内容

- 两个profile的完整选项准确传入IDAKLU；
- 默认profile等价于当前配置；
- 三个白名单错误可重试；
- 未知IDA错误、物理事件、NaN、I/O错误不可重试；
- 通用包装保留原始异常链；
- 错误码解析不依赖调用点散布的字符串判断。

## 8. 任务4：实现连续标准充电候选求解

### 修改/新增文件

- `src/pybamm_w10/backend.py`
- `src/pybamm_w10/types.py`
- 新增`tests/test_standard_charge_sequence.py`
- 扩展`tests/test_backend_termination_mapping.py`
- 扩展`tests/test_charge_backend_extraction.py`

### 后端接口

增加一个标准充电序列入口，输入包括：

- 四个现有协议参数；
- 求解器profile；
- 充电前快照；
- 阶段回调；
- 已解析的充电变量清单。

输出不可变`StandardChargeSequenceResult`，至少包括：

- 四个按顺序排列的`StageOutcome`；
- 四个`ChargeStageTrace`；
- 四个模拟阶段时长；
- 四个墙钟时长；
- 候选终端快照；
- 求解尝试元数据。

该入口不得在成功判定前修改`backend.solution`。

### PyBaMM回调适配器

增加单一回调适配器，负责：

- `on_step_start`：将step索引映射到阶段名并通知协议/心跳；
- `on_step_end`：结束阶段墙钟计时；
- `on_experiment_error`：保存底层异常和失败step；
- 不执行文件写入；
- 不吞掉用户中断。

### 完整性校验

求解返回后必须验证：

- 本次新增cycle数量符合预期；
- 新cycle包含四个且仅四个step；
- step顺序与构造顺序一致；
- 每一步由预期事件终止；
- 状态和终端标量有限；
- 时间不倒退；
- 能够构造候选终端状态哈希。

如果PyBaMM返回部分cycle，抛出结构化`SolverStepFailure`或明确的
`INCOMPLETE_CHARGE_SEQUENCE`，不得提交候选解。

### 轨迹提取

优先从每个step solution直接提取轨迹，避免依赖整个历史solution按时间切片。
现有`extract_charge_stage_trace()`的输出语义必须保持。阶段边界允许共享
时间点，但每段轨迹必须保留自己的起点和终点。

### 单元测试

使用假的Simulation/callback覆盖：

- Experiment确实只有一个四步cycle；
- 使用配置值而不是硬编码；
- starting solution带cycle偏移时选择新cycle；
- 四步完整时生成四个结果；
- 第二步失败并返回部分cycle时被识别；
- 回调捕获的原始IDA错误进入结构化失败；
- 无回调错误但步骤不足时使用明确完整性错误；
- 候选失败不改变主后端；
- 阶段轨迹次序、边界和终止原因正确。

## 9. 任务5：在协议层接入连续充电但保持分析接口

### 修改文件

- `src/pybamm_w10/protocol.py`
- `tests/test_charge_protocol_capture.py`
- `tests/test_protocol_failure_matrix.py`
- `tests/test_udds_and_protocol.py`
- `tests/test_rpt_recovery.py`

### 实现内容

把普通循环中的四次独立stage调用替换成一次连续序列调用。协议层仍需：

- 按原名称报告四个阶段；
- 将四个`StageOutcome`按原规则分类；
- 将四个`ChargeStageTrace`按原顺序交给
  `build_charge_analysis_bundle()`；
- 在四段充电分析完成后才进入`post_charge_rest`；
- 保持`charge_already_complete=true`的RPT恢复路径不变；
- 保持Step 5和Step 6代码不变。

### 兼容性要求

现有fake backend测试可增加序列接口，也可通过窄适配器迁移；不得为了测试
同时长期维护两套生产标准充电逻辑。

### 测试内容

- 普通循环只调用一次连续序列入口；
- 四条阶段轨迹仍在静置前完成捕获；
- 分析器收到固定四段顺序；
- 静置、Step 5、Step 6顺序不变；
- RPT后已完成充电路径不重复充电；
- 任何阶段终止类别异常时整个标准充电失败；
- 现有物理失败矩阵结果不变。

## 10. 任务6：实现原子回滚和一次保守重试

### 修改/新增文件

- `src/pybamm_w10/backend.py`
- `src/pybamm_w10/protocol.py`
- `src/pybamm_w10/types.py`
- 新增`tests/test_standard_charge_retry.py`

### 重试控制

重试控制放在能够同时拥有充电前快照、候选后端和四段序列语义的单一位置。
不得在每个CV函数内分别重试。

流程固定为：

```text
capture pre_charge_snapshot
attempt 1 with default profile
  success -> validate -> atomic commit
  retryable failure ->
    discard candidate
    restore snapshot
    verify state hash
    attempt 2 with conservative profile
      success -> validate -> atomic commit
      failure -> final numerical failure
  non-retryable failure -> final failure
```

### 强制规则

- 最大尝试次数为2；
- 第二次尝试从`3c_cc`重新开始；
- 重试前状态哈希必须等于快照哈希；
- 首次部分轨迹不得写入正式输出；
- 第二次仍失败后主后端仍等于充电前状态；
- 不得在重试时修改协议或容差；
- 用户中断不重试。

### 测试内容

- 首次成功只调用一次；
- 第二步`IDA_ERR_FAIL`触发一次完整重试；
- `IDA_CONV_FAIL`和`IDA_LSETUP_FAIL`同样可重试；
- 未知异常不重试；
- 状态哈希不一致时拒绝重试；
- 第二次失败后停止；
- 两次失败上下文均保留；
- 重试成功只提交第二次结果；
- 失败时没有正式CSV或事务副作用。

## 11. 任务7：扩展心跳与进度监控

### 修改文件

- `src/pybamm_w10/progress.py`
- `src/pybamm_w10/runner.py`
- `tests/test_heartbeat.py`
- `tests/test_terminal_monitor.py`

### 实现内容

在`ProgressState`增加可序列化字段：

```text
solver_attempt
solver_profile
```

普通阶段默认：

```text
solver_attempt = 1
solver_profile = default
```

重试开始后，阶段重新显示`3c_cc`，同时变为：

```text
solver_attempt = 2
solver_profile = conservative_cv_transition
```

step回调负责实时阶段切换；心跳线程继续每60秒原子写入。终态继续写
`status=TERMINATED`及准确业务状态。

### 测试内容

- 新字段JSON序列化稳定；
- 每个step开始时阶段正确更新；
- 重试时attempt递增且阶段从`3c_cc`开始；
- 心跳序列继续单调递增；
- `run_progress.json`仍不进入checkpoint manifest；
- 终态不会被后台心跳覆盖回RUNNING。

## 12. 任务8：增加求解尝试审计和完整失败取证

### 修改/新增文件

- `src/pybamm_w10/types.py`
- `src/pybamm_w10/output.py`
- `src/pybamm_w10/runner.py`
- 新增`tests/test_solver_attempt_audit.py`
- 扩展`tests/test_failure_artifacts.py`
- 扩展`tests/test_output_transactions.py`

### 审计输出

新增事务保护的append输出：

```text
solver_attempts.jsonl
```

每个完成或最终失败的标准充电事务只追加一条汇总记录。记录包括：

- audit版本；
- cycle和transaction；
- 尝试次数；
- 每个profile；
- 初始错误码；
- 最终状态；
- 充电前后状态哈希；
- 每段终止原因；
- 每段模拟和墙钟时长。

该文件加入输出manifest和回滚偏移保护，但不进入科学指标计算。

### 失败上下文

扩展`FailureContext`：

```text
charge_stage
failed_step_index
solver_attempt
solver_profile
sundials_error_code
pre_charge_state_hash
last_successful_stage
last_committed_checkpoint
resume_checkpoint
resume_eligible
attempt_failures
```

`attempt_failures`保留两次异常的结构化摘要。底层错误码和原始消息必须
保留，不能只剩应用层`RuntimeError`。

### 恢复语义

只有当最近检查点通过schema、配置、输入、环境、算法和manifest校验时，
才写：

```text
resume_eligible = true
resume_checkpoint = <最近完整循环checkpoint>
```

失败过程中的部分solution或forensic-only pickle不得作为恢复入口。

### 测试内容

- 审计文件一事务一行；
- 首次成功、重试成功和最终失败三种记录完整；
- rollback按checkpoint偏移截断审计尾部；
- manifest包含审计文件；
- 失败JSON同时保留两次异常；
- resume字段只在检查点验证通过时为真；
- forensic快照仍不可被`load_checkpoint()`加载。

## 13. 任务9：跨模块回归和静态协议保护

### 测试集合

至少运行：

```text
tests/test_standard_charge_protocol_invariants.py
tests/test_solver_profiles.py
tests/test_solver_failure_classification.py
tests/test_standard_charge_sequence.py
tests/test_standard_charge_retry.py
tests/test_charge_protocol_capture.py
tests/test_charge_backend_extraction.py
tests/test_charge_efficiency_math.py
tests/test_charge_soc_boundaries.py
tests/test_protocol_failure_matrix.py
tests/test_udds_and_protocol.py
tests/test_rpt_recovery.py
tests/test_heartbeat.py
tests/test_terminal_monitor.py
tests/test_solver_attempt_audit.py
tests/test_failure_artifacts.py
tests/test_output_transactions.py
tests/test_checkpoint_schema3.py
tests/test_smoke_contract.py
```

然后运行全量测试：

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -m pytest -q -p no:cacheprovider
```

### 静态检查

检查：

- 不存在CC/CV之间新增的rest或ramp；
- `charge_3c_a`、`discharge_c4_a`、`cv_cutoff_a`未变化；
- Step 5、Step 6和RPT实现没有无关修改；
- 没有第二套隐藏的标准充电生产路径；
- 新增审计文件不参与科学指标；
- 旧输出目录没有变化。

### 完成门槛

全量测试必须通过；否则不得进入真实PyBaMM诊断。

## 14. 任务10：真实PyBaMM四段充电smoke

### 修改文件

- `src/pybamm_w10/smoke.py`
- `src/pybamm_w10/cli.py`
- `tests/test_smoke_contract.py`
- 必要时新增`scripts/run_standard_charge_sequence_smoke.ps1`

### Smoke范围

新增显式动作只运行：

- 模型构建和变量预检；
- 一次四段标准充电序列；
- 阶段轨迹与终止判定；
- 默认profile和可注入的保守profile。

不运行Step 5、UDDS、完整老化循环或生产输出。

### 验收

- 新cycle恰好有四个step；
- 四个终止事件正确；
- 所有输出有限；
- 阶段局部电量积分通过；
- 回调阶段顺序正确；
- 默认profile下无重试时，审计显示一次尝试；
- smoke输出只写独立临时目录。

## 15. 任务11：第10圈隔离复现与修复验证

### 输入保护

只读使用：

```text
outputs/pybamm_spme/w10-350-spme-uncalibrated-v1/checkpoints/cycle-009.pkl
```

不得调用正式resume路径修改原运行目录。应提供明确标记为diagnostic-only的
隔离入口，将旧checkpoint状态读入临时候选后端。

### 三步验证

1. 基线复现  
   使用旧四次Simulation逻辑在隔离目录复现cycle 10的`IDA_ERR_FAIL`，
   保存求解器统计，不写原输出目录。

2. 连续序列验证  
   使用新版默认profile从同一物理状态运行四段充电，要求成功；若首次仍
   失败，则按规范执行一次保守重试。

3. 结果审计  
   验证四段终止事件、状态有限性、阶段轨迹、电量平衡、SOC边界和状态
   原子性。

该诊断仅证明修复针对性，不可追加到正式科学结果。

## 16. 任务12：cycle 0–25回归门槛

### 运行要求

- 使用新版schema 5；
- 使用全新输出目录；
- 从cycle 0开始；
- 完成cycle 25及其RPT；
- 不访问或修改旧输出；
- 运行前再次核验协议不变量。

### 验收标准

- 状态为正常完成指定验证范围；
- 无数值失败或未知重试；
- 所有四段终止类别正确；
- 无NaN/Inf；
- 容量窗口相对误差不超过`1e-3`；
- 电量平衡状态不比旧实现降级；
- 所有重试均有结构化原因；
- cycle 1–9与旧结果比较满足：
  - 终端电压差不超过`1e-6 V`；
  - 终端温度差不超过`1e-4 K`；
  - 阶段充放电量差不超过`1e-6 Ah`；
  - 终止事件类别完全相同；
- cycle 25检查点能够用相同配置加载；
- RPT结果、manifest和审计文件完整。

若新旧回归容限不满足，必须停止并分析数值路径差异；不得通过放宽验收
阈值直接进入350圈验证。

## 17. 任务13：文档与运行入口收尾

### 修改文件

- `README.md`
- 相关运行脚本的帮助文本
- 必要的schema/版本说明

### README内容

补充：

- 物理充放电流程未改变；
- 标准四段充电现在使用单一连续Experiment；
- 一次保守重试的触发条件；
- `solver_attempts.jsonl`字段用途；
- checkpoint schema 5；
- 旧schema 4只能诊断、不能正式续跑；
- 正式验证必须使用新目录从cycle 0开始。

不得把求解重试描述成协议阶段，也不得暗示旧输出已被迁移。

### 最终静态审查

对设计、实施计划、README和代码逐项核对，确保没有：

- 隐藏协议变化；
- 未受限重试；
- 容差放宽；
- 旧检查点混用；
- 部分解提交；
- 失败审计遗漏。

## 18. 350圈正式验证门槛

350圈运行不是普通测试步骤。只有以下条件全部满足，并获得用户单独授权
后才允许启动：

1. 全量单元测试通过；
2. 真实四段充电smoke通过；
3. 第10圈隔离修复验证通过；
4. cycle 0–25回归通过；
5. 用户审阅回归报告；
6. 新输出目录为空且路径明确；
7. 监控命令和失败恢复入口准备完成。

正式运行必须：

- 从cycle 0开始；
- 使用schema 5；
- 使用`standard-charge-sequence-v1`；
- 不覆盖旧运行；
- 保持现有350圈和全部RPT节点；
- 完成后执行输出清单、SOH和审计校验。

## 19. 任务依赖顺序

```text
协议不变量
  -> 配置和checkpoint版本
  -> 结构化错误与solver profiles
  -> 连续标准充电候选求解
  -> 协议层接入
  -> 原子回滚和一次重试
  -> 心跳与进度
  -> 审计、失败取证和恢复
  -> 跨模块全量回归
  -> 真实四段充电smoke
  -> 第10圈隔离验证
  -> cycle 0–25回归
  -> 文档收尾
  -> 用户单独授权350圈正式验证
```

不得为了尽快复现第10圈而跳过协议不变量、候选状态原子性或结构化错误
测试。

## 20. 实施完成定义

编码阶段完成必须同时满足：

1. 设计规范全部转化为代码或自动化断言；
2. 四段标准充电由一个四步cycle的Simulation执行；
3. 物理协议所有参数与顺序不变；
4. 后续step失败返回部分cycle时能够可靠识别；
5. 白名单错误最多重试一次；
6. 重试从相同状态哈希开始；
7. 失败候选不污染主状态和正式输出；
8. 逐阶段轨迹、效率和SOC分析兼容；
9. 进度能够显示真实阶段和尝试次数；
10. 审计和失败上下文完整；
11. schema 5检查点执行版本兼容性生效；
12. 全量测试通过；
13. 真实smoke通过；
14. 第10圈隔离验证通过；
15. cycle 0–25回归通过；
16. README与实际行为一致；
17. 未启动或覆盖任何未经单独授权的350圈正式运行。
