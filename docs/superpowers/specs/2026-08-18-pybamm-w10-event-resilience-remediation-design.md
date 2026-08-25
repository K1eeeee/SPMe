# PyBaMM W10 事件可靠性与正式运行整改设计

日期：2026-08-18  
状态：书面规范已获用户确认，已转入实施计划  
依据：

- `docs/superpowers/specs/2026-08-17-pybamm-w10-3c-aging-design.md`
- `docs/superpowers/specs/2026-08-17-pybamm-w10-formal-readiness-design.md`
- `docs/reports/2026-08-18-pybamm-w10-350-cycle-stop-and-remediation.md`

## 1. 目标

修复正式运行在第1个 aging cycle Step 6 因容量事件与 drive-cycle 最终时刻重合而终止的问题，并补齐终止分类、失败取证、输出语义、恢复兼容、参数审计、长任务进度和生产等价测试。

整改完成后只运行非求解测试和受控短时真实 PyBaMM smoke，不执行完整生产参数 aging cycle，也不启动350循环。

### 1.1 与既有设计的优先关系

本规范只覆盖既有设计中“把Step 6驱动曲线精确截断到UDDS剩余目标Ah”的构造要求。新要求是：

```text
容量事件目标精确保持不变；
驱动曲线在目标后提供可审计guard；
实际求解必须在guard开始前由容量事件终止。
```

既有设计中的80%累计容量窗口、同一`q_window_start`、2.5 V物理边界、W10波形、`Q_ref`更新、RPT模式和其他物理要求继续有效。若既有文档与本规范在drive-cycle终点构造上冲突，以本规范为准。

## 2. 已确认的范围与科学口径

### 2.1 纳入范围

- Step 6 UDDS 容量事件 guard；
- 生产、smoke 和测试共用 drive-window 构造器；
- 结构化阶段结果和终止分类；
- 结构化失败上下文及不可恢复的取证快照；
- checkpoint schema、协议算法版本和恢复兼容检查；
- UDDS目标、实际值和guard输出字段；
- LAM图形字段修正；
- `effective_parameters.json`；
- 每完整循环checkpoint；
- 每60秒运行heartbeat；
- 模拟时长和墙钟时长；
- 单元、状态机、输出、恢复和短真实求解测试。

### 2.2 事件整改默认不改变的内容

- PyBaMM 26.7.1、DFN、集总热模型和现有老化机制；
- 未提供显式校准参数时，OKane2022电极、锂库存和老化参数保持原值；
- 4.85 Ah作为标称容量和实验电流基准；
- 14.55 A、1.2125 A、0.24 A及0.05 A协议电流；
- 4.2 V和2.5 V电压边界；
- 显式20% SOC规范初态；
- virtual和strict-W10 RPT科学定义；
- `Q_ref`来自最近一次成功RPT并在批内冻结；
- 求解器相对误差、绝对误差、根容差、最大步长和最大步数；
- W10 Step 14、2600 s最小完整重复单元和1 Hz相位平均波形；
- checkpoint后未提交输出自动回滚；
- 运行目录全生命周期单写者锁。

### 2.3 明确不做

- 事件可靠性修复本身不隐式改变DFN容量或拟合退化参数；
- 容量归一化和退化标定只能通过独立的版本化校准参数文件显式启用，并遵守`2026-08-18-pybamm-w10-m50t-quantitative-calibration-design.md`；
- 不实现HPPC或EIS；
- 不自动迁移schema-2 checkpoint；
- 不恢复或覆盖`outputs/pybamm_w10/virtual-formal-001`；
- 不用更大求解步长做性能优化；
- 不把`final time`当成容量事件成功；
- 不放宽0.1%容量验收误差。

## 3. 协议语义

一个标准循环从约20% SOC开始，充满、静置后执行：

1. Step 5以C/4从约100%放电到协议定义的约80%；
2. Step 6重复W10的2600 s UDDS单元，从约80%放电到协议定义的约20%；
3. 最后一个UDDS单元按需要部分执行；
4. Step 6成功达到累计80%容量窗口后，aging-cycle编号才增加。

这里的80%和20%是相对最近一次成功RPT容量`Q_ref`定义的协议容量窗口，不是直接读取DFN内部绝对SOC变量：

```text
step5_target_ah = 0.20 × Q_ref
window_target_ah = 0.80 × Q_ref
actual_udds_remaining_target_ah = window_target_ah - Delta_Q5_actual
```

Step 5与Step 6始终使用同一个`q_window_start`。UDDS中的回馈电流按净库仑积分抵消放电量。

## 4. 架构与模块边界

### 4.1 `config.py`

新增不可变配置：

```text
protocol_algorithm_version = "w10-window-v2"
output_schema_version = 2
udds_event_guard_fraction = 0.005
udds_event_guard_solver_steps = 10
heartbeat_interval_s = 60
checkpoint_every_cycles = 1
```

所有新增配置进入`RunConfig.fingerprint()`和`run_config.json`。`checkpoint_every_cycles=1`是新正式运行默认值，RPT节点仍额外提交checkpoint。

### 4.2 `types.py`

新增类型：

- `DriveWindowPlan`：事件目标、UDDS剩余目标、guard、profile可提供Ah和`CurrentProfile`；
- `StageSpec`：阶段、预期终止种类、允许的物理终止；
- `StageOutcome`：规范终止种类、PyBaMM原始终止文本、终止时间、事件值和末状态指标；
- `TerminationKind`：`CAPACITY`、`VOLTAGE`、`CURRENT`、`FINAL_TIME`、`MODEL_PHYSICAL_EVENT`和`UNKNOWN`；
- `FailureReason`：稳定的机器可读失败原因；
- `FailureContext`：失败时的完整协议与状态上下文。

异常仍区分`PhysicalProtocolFailure`和`NumericalFailure`，但两者必须携带`FailureContext`，不能只携带字符串。

### 4.3 `udds.py`

新增唯一的生产级drive-window构造器。输入为基础UDDS、当前UDDS剩余目标和求解器最大步长；输出`DriveWindowPlan`。

正式协议、smoke和相关测试必须调用该构造器。不得在smoke中手写不同的“曲线容量/事件容量”关系。

### 4.4 `backend.py`

各协议原语返回`StageOutcome`，不再只返回`None`。backend负责：

- 执行单个PyBaMM step；
- 检查解、末状态和关键标量是否有限；
- 保存原始终止文本；
- 按当前`StageSpec`将终止映射为规范种类；
- 返回阶段末电压、温度、容量计数器、状态哈希等取证指标。

backend不负责决定运行终态。是否成功、物理失败或数值失败由协议状态机根据`StageSpec + StageOutcome`决定。

### 4.5 `protocol.py`

状态机在进入每个阶段前更新当前`ProtocolPhase`和`FailureContext`。它负责：

- Step 5/6共享`q_window_start`；
- 构造`DriveWindowPlan`；
- 检查预期终止事件；
- 验证Step 5和累计窗口误差不超过0.1%；
- 计算`udds_actual_ah`；
- 产生完整`CycleResult`；
- 将明确物理边界转为`PhysicalProtocolFailure`；
- 将final-time、未知终止、容量超差和非法状态转为`NumericalFailure`。

### 4.6 `runner.py`与`output.py`

runner负责运行生命周期、事务、checkpoint、heartbeat和终态。output负责原子文件、清单、回滚和取证工件。

新增：

- `run_progress.json`：动态进度；
- `failures/*.json`：机器可读失败上下文；
- `failures/*.pkl`：不可恢复取证快照；
- `effective_parameters.json`：参数审计。

`run_progress.json`、锁审计和恢复审计不进入checkpoint的已提交静态工件清单。正式CSV、JSONL、时序、图形和参数审计继续受事务清单保护。

### 4.7 `model.py`

增加只读参数审计构造器，不改变传给PyBaMM的物理参数。审计输出见第9节。

## 5. UDDS容量事件guard

### 5.1 计算

设：

```text
remaining_ah = window_target_ah - Delta_Q5_actual
max_abs_current_a = max(abs(base_udds.current_a))
```

guard定义为：

```text
guard_ah = max(
    udds_event_guard_fraction × remaining_ah,
    udds_event_guard_solver_steps
      × max_abs_current_a
      × solver.max_step_s
      / 3600
)
```

驱动曲线按下式生成：

```text
profile_available_target_ah = remaining_ah + guard_ah
profile = repeat_to_net_discharge(base_udds, profile_available_target_ah)
```

### 5.2 不变的事件目标

自定义容量事件仍为：

```text
window_target_ah
  - (Discharge capacity [A.h] - q_window_start)
```

guard只延长控制函数定义域，不修改事件目标。PyBaMM必须在guard部分开始前终止，guard容量不得进入实际循环结果。

### 5.3 构造期验证

`DriveWindowPlan`构造时必须验证：

- 所有输入有限且为正；
- 时间严格递增且无NaN/Inf；
- profile可提供Ah大于`remaining_ah`；
- profile可提供Ah与`remaining_ah + guard_ah`的误差不超过数值构造容差；
- 容量事件零点严格位于profile最终时刻之前；
- profile指纹可复现。

### 5.4 求解后验证

成功Step 6必须满足：

- 终止种类为`CAPACITY`；
- 实际终止时间严格小于profile最终时间；
- `abs(window_actual_ah - window_target_ah) / window_target_ah <= 0.001`；
- `udds_actual_ah = window_actual_ah - Delta_Q5_actual`；
- 结束电压未违反2.5 V先到规则；
- 状态和关键指标有限。

## 6. 终止事件分类

### 6.1 阶段声明

每个阶段显式声明预期终止：

| 阶段 | 预期终止 |
|---|---|
| CC充电 | 电压事件 |
| CV充电 | 电流事件 |
| 定时静置 | final time |
| RPT容量放电 | 2.5 V电压事件 |
| Step 5 | W10容量事件 |
| Step 6 | W10容量事件 |

Step 5/6同时允许2.5 V和当前DFN已注册模型物理事件作为“明确物理边界先到”，但这会导致物理协议失败，不是阶段成功。

### 6.2 映射规则

优先使用当前experiment step中注册的稳定事件名称和当前model事件表建立明确映射。自定义容量事件使用稳定唯一名称`W10_CAPACITY_WINDOW`。

原始PyBaMM终止文本完整保留。若当前PyBaMM API只能提供字符串，则使用版本固定、阶段限定、完整事件名匹配；不得使用“只要包含voltage/current”等宽泛规则。

### 6.3 互斥分类

| 实际结果 | 分类与原因 |
|---|---|
| 当前阶段预期事件 | 阶段成功 |
| 2.5 V或注册模型物理边界先到 | `PHYSICAL_PROTOCOL_FAILURE / PHYSICAL_EVENT_BEFORE_TARGET` |
| capacity step得到final time | `NUMERICAL_FAILURE / UNEXPECTED_FINAL_TIME` |
| 未知终止 | `NUMERICAL_FAILURE / UNKNOWN_TERMINATION` |
| 求解器异常 | `NUMERICAL_FAILURE / SOLVER_FAILURE` |
| NaN/Inf或非法状态 | `NUMERICAL_FAILURE / INVALID_STATE` |
| Step 5或窗口误差超过0.1% | `NUMERICAL_FAILURE / CAPACITY_TOLERANCE_FAILURE` |
| 输出或checkpoint写入失败 | `NUMERICAL_FAILURE / OUTPUT_FAILURE` |

任何未映射情况默认归数值失败，禁止猜测为物理失败。

## 7. 失败取证

### 7.1 `FailureContext`

失败上下文至少包含：

- run id、mode、cycle、RPT node、`ProtocolPhase`；
- `Q_ref`、来源RPT节点和`q_window_start`；
- Step 5目标、实际值和相对误差；
- window目标、实际值和相对误差；
- UDDS计划剩余、实际剩余目标、guard、profile可提供Ah和UDDS实际Ah；
- 规范终止种类、PyBaMM原始终止文本、终止时间和事件值；
- 末端电压、温度、容量计数器和状态哈希；
- 最后有效checkpoint、事务号和已完成循环数；
- 异常类型、稳定失败原因、消息和traceback；
- 取证快照路径和`resume_eligible=false`。

未知或在当前阶段尚不存在的数值写为JSON `null`，不得用0伪装。

### 7.2 文件

失败时原子写入：

```text
failures/failure-<utc>-cycle-<NNN>-<phase>.json
failures/failure-<utc>-cycle-<NNN>-<phase>.pkl
```

PKL包装对象必须包含：

```text
forensic_only = true
resume_eligible = false
schema_version = 1
```

CLI的`--resume`只接受`checkpoints/*.pkl`中schema-3 `Checkpoint`，显式拒绝failure snapshot。

### 7.3 日志与锁

`run.log`追加结构化单行失败摘要。`run_status.json`保存互斥终态及`FailureContext`摘要。锁释放前将锁元数据中的业务结果更新为最终运行状态；`release_reason`继续表示句柄释放方式，不再承担业务状态含义。

## 8. Checkpoint、恢复和旧运行

### 8.1 schema 3

checkpoint升级为schema 3，并新增：

- `protocol_algorithm_version`；
- `output_schema_version`；
- guard配置及其指纹；
- 最后成功协议边界；
- 最后成功阶段；
- effective-parameter审计指纹。

现有配置、输入MAT、基础UDDS、规范初态、环境和输出清单指纹继续保留。

### 8.2 恢复策略

- schema-3且所有指纹一致：允许验证、回滚未提交尾部并恢复；
- schema-2：明确拒绝，错误原因`UNSUPPORTED_CHECKPOINT_SCHEMA`；
- failure snapshot：明确拒绝；
- guard、算法版本或输出schema不同：明确拒绝；
- 已提交前缀不一致：拒绝；
- checkpoint后未提交输出：归档到`rollback/`后继续。

不提供自动迁移或“强制忽略指纹”参数。

### 8.3 旧正式目录

`outputs/pybamm_w10/virtual-formal-001`保持只读，不删除、不覆盖、不恢复。整改验证使用独立目录，未来350循环从新目录、规范20% SOC和新的cycle-0 RPT开始。

## 9. 输出设计

### 9.1 循环与RPT字段

RPT批次边界输出：

```text
next_step5_target_ah
next_window_target_ah
planned_udds_remaining_ah
```

其中：

```text
planned_udds_remaining_ah = next_window_target_ah - next_step5_target_ah
```

删除歧义字段`next_udds_remaining_ah`。

完成循环输出新增：

```text
actual_udds_remaining_target_ah
udds_profile_available_ah
udds_guard_ah
udds_actual_ah
wall_clock_<stage>_s
```

其中：

```text
actual_udds_remaining_target_ah = window_target_ah - Delta_Q5_actual
udds_actual_ah = window_actual_ah - Delta_Q5_actual
```

CSV schema由`output_schema_version=2`固定。恢复时不得向旧schema CSV追加新行。

### 9.2 LAM图形

图形与CSV统一使用：

```text
negative_lam_pct
positive_lam_pct
```

图形测试必须检查两条LAM数据序列实际传入绘图，不只检查PNG是否存在。

### 9.3 `effective_parameters.json`

至少包含：

- 参数集名称和PyBaMM版本；
- 每个允许覆盖项的参数名、单位、OKane2022原值、当前有效值和来源；
- Total heat transfer coefficient保留值；
- 未覆盖的电极高度、宽度、厚度、活性材料体积分数和最大浓度；
- 0%/100% SOC化学计量端点；
- 正负极理论容量窗口；
- 标称容量；
- cycle-0 RPT完成后追加或关联其容量、相对标称容量偏差、相对W10首次实测偏差和有效倍率；
- 参数审计指纹。

cycle-0 RPT之前不存在的结果写为`null`；RPT成功后原子更新并在随后checkpoint中提交新指纹。

## 10. Heartbeat与阶段耗时

### 10.1 `run_progress.json`

heartbeat线程每60秒原子更新：

- run id、PID、host；
- `RUNNING`或`TERMINATED`；
- mode、cycle、RPT node和当前phase；
- phase开始UTC时间；
- 最近heartbeat UTC时间；
- 最近成功checkpoint和事务号；
- 当前业务终态（仅终止后存在）。

heartbeat线程不访问或序列化正在求解的PyBaMM Solution，只读取runner维护的线程安全轻量进度对象。

### 10.2 生命周期

- 获取运行目录锁后、preflight开始前启动；
- 整个run/resume生命周期保持；
- 正常完成、物理失败或数值失败写出终态后停止；
- 最后一次原子写入`TERMINATED`；
- 进程崩溃时文件保留最后heartbeat，操作系统释放运行锁。

`run_progress.json`是动态监控文件，不作为checkpoint已提交工件，也不参与已提交前缀损坏判定。

### 10.3 墙钟耗时

协议状态机在每个阶段调用前后使用单调时钟记录墙钟耗时，与现有模拟时间阶段耗时分开输出。系统时钟调整不得造成负耗时。

## 11. 测试设计

### 11.1 纯逻辑测试

- guard两项取最大值；
- `DriveWindowPlan`可提供Ah严格大于事件目标；
- 事件零点严格位于profile末端前；
- Step 5/6共享基准；
- planned/actual UDDS字段公式；
- guard、算法版本和schema进入指纹；
- LAM字段一致；
- `FailureContext`缺失值为`null`；
- 非有限输入拒绝。

### 11.2 FakeBackend状态机测试

注入：

- 预期容量事件；
- 2.5 V先到；
- 注册模型物理事件先到；
- 非预期final time；
- 未知终止；
- 容量误差超过0.1%；
- 求解器异常；
- NaN/Inf状态。

逐项验证互斥终态、稳定失败原因、run status、日志和failure snapshot。

### 11.3 输出和恢复测试

- schema-3保存、加载、指纹验证；
- schema-2拒绝；
- failure snapshot拒绝；
- guard/算法/schema不匹配拒绝；
- checkpoint后CSV、JSONL、日志、时序、图形、failure文件回滚；
- 已提交前缀损坏拒绝；
- 回滚中断后幂等重试；
- 新CSV schema字段；
- effective-parameter审计更新；
- LAM绘图数据；
- heartbeat创建、周期更新、终止和崩溃残留语义；
- 两进程单写者锁。

### 11.4 短时真实PyBaMM smoke

只执行显著缩短目标的真实求解：

1. 短恒流容量事件；
2. 使用生产`DriveWindowPlan`的短UDDS容量事件；
3. 缩小目标的Step 5 + Step 6组合，验证共享`q_window_start`；
4. checkpoint保存、加载和短段续算；
5. virtual分支非侵入；
6. 未提交输出回滚；
7. 两进程锁竞争；
8. heartbeat生命周期。

成功标准：

- Step 6终止种类为`CAPACITY`；
- 终止时间严格早于profile最终时间；
- 容量误差不超过0.1%；
- 实际容量不包含guard；
- 连续与恢复路径的状态、时间和容量在既有容差内一致；
- smoke输出目录独立；
- 不产生aging-cycle结果。

### 11.5 本次不执行的验收

不执行完整生产参数1-cycle和350-cycle。交付时必须明确提示：在未来正式350循环前，仍需经用户单独授权完成一个隔离的完整生产参数aging cycle验收。

## 12. 实施影响文件

预计修改：

- `src/pybamm_w10/config.py`
- `src/pybamm_w10/types.py`
- `src/pybamm_w10/udds.py`
- `src/pybamm_w10/backend.py`
- `src/pybamm_w10/protocol.py`
- `src/pybamm_w10/model.py`
- `src/pybamm_w10/output.py`
- `src/pybamm_w10/runner.py`
- `src/pybamm_w10/smoke.py`
- `src/pybamm_w10/figures.py`
- `src/pybamm_w10/cli.py`
- `tests/`下相关及新增测试文件。

不复制virtual和strict-W10两套协议实现，不做与本整改无关的重构。

## 13. 实施顺序

1. 先写失败测试，固定旧边界问题和新schema；
2. 实现类型、配置和`DriveWindowPlan`；
3. 实现`StageSpec`、`StageOutcome`和互斥分类；
4. 接入协议状态机和`FailureContext`；
5. 升级checkpoint、输出和回滚；
6. 加入参数审计、LAM修正、heartbeat和墙钟时长；
7. 更新smoke，使其复用生产构造器；
8. 运行全部非求解测试；
9. 使用指定解释器运行短真实PyBaMM smoke；
10. 检查输出和正式运行命令，但不启动完整单循环或350循环。

## 14. 完成条件

以下条件全部满足才视为本次整改完成：

- 全部现有和新增pytest通过；
- 短真实PyBaMM smoke通过；
- Step 6在短真实UDDS测试中以容量事件终止且早于profile末端；
- final time不再误分类为物理失败；
- 2.5 V先到仍正确分类为物理失败；
- 容量误差超差归数值失败；
- failure JSON、PKL、run status和run log一致；
- 新checkpoint只接受schema-3及完全匹配指纹；
- 未提交输出回滚保持通过；
- heartbeat、阶段墙钟时间和每循环checkpoint通过；
- UDDS输出字段无歧义；
- LAM图使用正确字段；
- effective-parameter审计可独立确认4.85 Ah名义覆盖、当前是否启用显式面积缩放，以及所有校准倍率的原值与有效值；
- 旧正式目录未被修改；
- 没有运行完整生产参数aging cycle或350-cycle。

## 15. 后续正式运行门槛

本整改完成不等于已经批准350-cycle正式运行。后续顺序必须是：

1. 用户单独授权完整生产参数1-cycle验收；
2. 单循环成功并审计CSV、JSON、checkpoint、heartbeat和图形；
3. 新建正式运行目录；
4. 从显式20% SOC和新cycle-0 RPT开始；
5. cycle 1成功提交后立即人工核验；
6. 再继续350-cycle多日运行。

旧`virtual-formal-001`只作为失败证据保留。
