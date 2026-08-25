# PyBaMM W10事件整改与M50T cycle-0校准详细实施计划

日期：2026-08-18  
状态：已纳入`E:\battery\new`隔离工作区要求，待用户确认后实施  
基线参数集：`OKane2022`  
派生参数集名称：`OKane2022-M50T-W10-v1`

## 1. 目标与本轮授权边界

本计划落实两份已批准规范：

- `docs/superpowers/specs/2026-08-18-pybamm-w10-event-resilience-remediation-design.md`
- `docs/superpowers/specs/2026-08-18-pybamm-w10-m50t-quantitative-calibration-design.md`

本轮交付目标：

1. 修复Step 6容量事件与drive-cycle终点重合导致的第1循环中止；
2. 建立阶段终止分类、失败取证、schema-3 checkpoint、回滚、heartbeat和参数审计；
3. 建立模块化M50T校准框架、数据门槛和留出集防泄漏；
4. 只执行短真实PyBaMM smoke和strict-W10 cycle-0容量因子校准；
5. 生成`OKane2022-M50T-W10-v1`的cycle-0参数工件，但明确标记退化倍率未校准。

本轮严禁：

- 执行任何标准aging cycle；
- 搜索`sei_scale`、`plating_scale`或`lam_scale`；
- 执行cycle 25–225老化校准；
- 向优化器提供cycle 250–350留出容量；
- 执行完整生产参数1-cycle或350-cycle；
- 下载HPPC/EIS；
- 修改求解器容差、最大步长或最大步数；
- 修改、恢复或删除`outputs/pybamm_w10/virtual-formal-001`。
- 直接修改`E:\battery\src`、`E:\battery\tests`、`E:\battery\scripts`、`E:\battery\pyproject.toml`或`E:\battery\README.md`中的原始工程文件。

### 1.1 强制工作区隔离

后续产生或修改的所有代码、测试、脚本、配置和新工程文档必须位于：

```text
E:\battery\new
```

实施开始时先把以下只读基线复制到`E:\battery\new`，随后只修改副本：

```text
E:\battery\src              -> E:\battery\new\src
E:\battery\tests            -> E:\battery\new\tests
E:\battery\scripts          -> E:\battery\new\scripts
E:\battery\pyproject.toml   -> E:\battery\new\pyproject.toml
E:\battery\README.md        -> E:\battery\new\README.md
E:\battery\docs\superpowers\specs -> E:\battery\new\docs\superpowers\specs
E:\battery\docs\superpowers\plans -> E:\battery\new\docs\superpowers\plans
```

不复制以下内容：

- `E:\battery\data`：体积大且属于原始实验数据，始终只读访问；
- `E:\battery\outputs`：包含旧正式运行及失败证据；
- `tmp`、pytest缓存、`__pycache__`、虚拟环境和其他生成物。

新工程增加显式数据入口：

```text
--data-root E:\battery\data
```

所有新输出都写到`E:\battery\new\outputs`。任何命令若解析出的代码路径、测试路径或输出路径不在`E:\battery\new`内，必须在写入前拒绝执行。`E:\battery\data`是唯一允许的外部只读输入根目录。

除本次按用户要求更新这份位于原目录的实施计划外，正式编码阶段不再修改`E:\battery\new`之外的工程文件。

## 2. 实施原则

每个编码任务遵循同一顺序：

1. 先增加能稳定复现缺口的失败测试；
2. 只运行对应测试，确认测试因预期原因失败；
3. 做最小实现；
4. 重跑对应测试；
5. 重跑相关回归测试；
6. 在阶段门槛处运行全量非求解测试。

指定解释器和统一pytest命令：

```powershell
Set-Location -LiteralPath E:\battery\new
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -m pytest -q -p no:cacheprovider
```

当前工作区不是Git仓库，因此不安排commit步骤。每个阶段以测试结果、输出哈希和计划检查表作为可审计边界。计划中未写绝对路径的`src/`、`tests/`、`scripts/`、`docs/`、`README.md`和`pyproject.toml`均相对于`E:\battery\new`，不得解释为原工程路径。

## 3. 固定实现常量

### 3.1 事件与运行可靠性

```text
protocol_algorithm_version = w10-window-v2
output_schema_version = 2
checkpoint_schema_version = 3
udds_event_guard_fraction = 0.005
udds_event_guard_solver_steps = 10
heartbeat_interval_s = 60
checkpoint_every_cycles = 1
capacity_window_relative_tolerance = 0.001
```

### 3.2 cycle-0容量校准

```text
target_capacity_ah = 4.865884391243259
capacity_scale_lower = 0.90
capacity_scale_upper = 1.02
capacity_scale_interval_tolerance = 1e-4
capacity_relative_tolerance = 0.002
capacity_search_max_evaluations = 16
capacity_repeat_max_relative_difference = 0.0002
voltage_grid_points = 1001
voltage_full_rmse_limit_v = 0.050
```

求根使用确定性有界二分法，不新增优化依赖。每个候选重新构建DFN并从规范20% SOC开始。最优候选完成后再做一次独立新鲜求解；两次容量相对差必须不超过0.02%。

### 3.3 未来老化搜索预算（本轮只实现配置和门槛，不执行）

```text
search_seed = 20260818
surrogate_candidates_total = 32
survivors_cycle_25 = 16
survivors_cycle_75 = 8
survivors_cycle_122 = 4
survivors_cycle_225 = 2
full_dfn_candidates = 2
full_dfn_validation_nodes = [25, 75, 122, 225]
adaptive_budget_expansion = false
```

32个候选包含基线`[1,1,1]`和31个确定性Sobol点；变量为三个退化倍率的`log10`值。预算内不能达到标准时状态为校准失败，不自动扩大参数边界、候选数量或读取留出集。

## 4. 阶段A：复制隔离工程、保护基线并固定旧故障

### 任务A1：建立只读基线清单并复制隔离工程

涉及文件：

- 只读：`E:\battery\src/**`、`E:\battery\tests/**`、`E:\battery\scripts/**`
- 只读：`E:\battery\outputs\pybamm_w10\virtual-formal-001/**`
- 新建隔离工程：`E:\battery\new/**`
- 新增审计：`E:\battery\new\docs\audit\baseline_copy_manifest.json`
- 新增测试：`E:\battery\new\tests\test_regression_boundaries.py`
- 更新副本：`E:\battery\new\README.md`

步骤：

1. 验证`E:\battery\new`的解析后绝对路径，并确认当前为空；若实施时已非空，先清点并拒绝覆盖未知文件；
2. 记录原工程代码及旧正式目录的相对路径、大小和SHA-256，不向原目录写审计文件；
3. 只复制`src`、`tests`、`scripts`、`pyproject.toml`、`README.md`及已批准规范/计划；
4. 复制时排除`__pycache__`、`.pytest_cache`、临时文件和其他生成物；
5. 在`E:\battery\new\docs\audit\baseline_copy_manifest.json`记录源/目标哈希并逐项验证一致；
6. 从原工程运行现有20个pytest并保存基线结果；
7. 后续所有pytest使用`E:\battery\new`作为工作目录；
8. 增加回归测试，固定当前配置的20% SOC、4.85 Ah名义容量、14.55 A、2.5/4.2 V和2600 s W10单元；
9. 新README增加“原工程与旧失败目录只读”和本轮禁止aging的提示。

验收：

- 基线测试通过；
- 原工程代码和旧正式目录的实施前清单可用于最终逐文件比对；
- 副本与源文件逐项哈希一致且没有复制原始数据或旧输出；
- `E:\battery\new`之外没有新增或修改工程代码；
- 测试本身不调用`W10Runner.run()`。

## 5. 阶段B：类型、配置与UDDS guard

### 任务B1：扩展显式类型和不可变配置

修改：

- `src/pybamm_w10/config.py`
- `src/pybamm_w10/types.py`
- `tests/test_types_and_config_v2.py`

实现：

- 增加规范要求的运行配置常量并进入`normalized()`、`to_json()`和`fingerprint()`；
- 增加独立`data_root`配置，默认由CLI显式传入`E:\battery\data`，不得从新工作区隐式复制数据；
- 将W10 MAT、容量诊断和cycling数据路径统一解析到只读`data_root`；
- 将checkpoint默认周期从5改为1；
- 增加`TerminationKind`、`FailureReason`、`DriveWindowPlan`、`StageSpec`、`StageOutcome`和`FailureContext`；
- `PhysicalProtocolFailure`与`NumericalFailure`保存`FailureContext`；
- 更新`CycleResult`字段，删除歧义字段`udds_remaining_ah`，改为计划/实际/guard字段；
- 更新`RPTResult`中的下一循环计划字段命名。

测试：

- 配置变更必然改变指纹；
- dataclass JSON化时未知值为`null`；
- failure reason和termination kind只接受枚举值；
- cycle/RPT字段公式无歧义；
- 默认checkpoint周期为1。

### 任务B2：实现唯一的生产级drive-window构造器

修改：

- `src/pybamm_w10/udds.py`
- `tests/test_drive_window_plan.py`
- 调整`tests/test_udds_and_protocol.py`

实现：

- 新增`build_drive_window_plan(base, remaining_ah, max_step_s, config)`；
- 按规范计算两项guard并取最大值；
- 复用`repeat_to_net_discharge()`生成`remaining + guard`的profile；
- 验证有限性、正值、严格递增、容量构造误差、事件在profile末端之前和指纹稳定性；
- 保留`repeat_to_net_discharge()`作为底层精确截断工具，但协议和smoke不再直接调用它构造Step 6。

重点测试：

- 0.5%项占优；
- 10个最大求解步容量项占优；
- profile容量严格大于事件剩余目标；
- 事件理论时刻严格早于profile末端；
- NaN、Inf、零、负数和非递增波形拒绝；
- 对已复现的cycle-0数值，事件与曲线终点不再相等。

目标命令：

```powershell
Set-Location -LiteralPath E:\battery\new
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -m pytest -q -p no:cacheprovider tests\test_drive_window_plan.py tests\test_udds_and_protocol.py
```

## 6. 阶段C：结构化终止与协议状态机

### 任务C1：backend返回`StageOutcome`

修改：

- `src/pybamm_w10/backend.py`
- 新增`tests/test_backend_termination_mapping.py`

实现：

- `_run()`保存原始PyBaMM终止文本并返回阶段原始结果；
- 通过当前step注册的完整事件名和model事件表做阶段限定映射；
- 自定义事件名固定为`W10_CAPACITY_WINDOW`；
- 所有协议原语接收或内部构造`StageSpec`并返回`StageOutcome`；
- 检查末状态、电压、温度、容量和时间有限；
- 移除当前`_require_event()`的宽泛字符串包含判断。

测试使用fake solution覆盖：预期容量、预期电压、预期电流、定时final time、2.5 V、注册物理事件、未知文本、NaN/Inf和solver异常。

### 任务C2：协议状态机做唯一业务分类

修改：

- `src/pybamm_w10/protocol.py`
- 重写`tests/test_udds_and_protocol.py`中的FakeBackend
- 新增`tests/test_protocol_failure_matrix.py`

实现：

- Step 5与Step 6保存同一`q_window_start`；
- Step 6调用`build_drive_window_plan()`；
- 在每阶段前更新phase和轻量进度回调；
- 校验Step 5及总窗口相对误差不超过0.1%；
- 成功要求CAPACITY且终止时间严格小于profile最终时间；
- 2.5 V或注册模型物理事件先到归物理失败；
- 非预期final time、未知终止、容量超差、非法状态和solver异常归数值失败；
- 构建完整`FailureContext`，不存在的字段保持`None`；
- 用单调时钟记录墙钟时长，保留原模拟时长。

必须覆盖的状态矩阵：

| 注入结果 | 预期终态/原因 |
|---|---|
| CAPACITY | 成功 |
| 2.5 V先到 | `PHYSICAL_EVENT_BEFORE_TARGET` |
| 模型物理事件先到 | `PHYSICAL_EVENT_BEFORE_TARGET` |
| final time | `UNEXPECTED_FINAL_TIME` |
| UNKNOWN | `UNKNOWN_TERMINATION` |
| 容量误差>0.1% | `CAPACITY_TOLERANCE_FAILURE` |
| 非有限状态 | `INVALID_STATE` |
| solver异常 | `SOLVER_FAILURE` |

## 7. 阶段D：输出、schema-3恢复与失败取证

### 任务D1：升级CSV/JSONL schema并修正LAM绘图

修改：

- `src/pybamm_w10/output.py`
- `src/pybamm_w10/figures.py`
- 新增`tests/test_output_schema_v2.py`
- 新增`tests/test_figures.py`

实现：

- cycle CSV写入`actual_udds_remaining_target_ah`、`udds_profile_available_ah`、`udds_guard_ah`、`udds_actual_ah`和各阶段墙钟时长；
- RPT CSV写入`next_step5_target_ah`、`next_window_target_ah`和`planned_udds_remaining_ah`；
- 恢复时拒绝向schema不符的CSV追加；
- LAM图改用`negative_lam_pct`和`positive_lam_pct`；
- 绘图测试拦截传入序列，不能只断言PNG存在。

### 任务D2：checkpoint schema 3与兼容拒绝

修改：

- `src/pybamm_w10/types.py`
- `src/pybamm_w10/output.py`
- `src/pybamm_w10/runner.py`
- 更新`tests/test_output_transactions.py`
- 更新`tests/test_runner_checkpoint_order.py`
- 新增`tests/test_checkpoint_schema3.py`

实现：

- checkpoint加入协议算法、输出schema、guard指纹、最后成功边界/阶段和参数审计指纹；
- 仅接受schema 3及所有指纹完全一致；
- schema 2返回稳定原因`UNSUPPORTED_CHECKPOINT_SCHEMA`；
- failure PKL明确拒绝；
- checkpoint之后的CSV、JSONL、日志、时序、图形、参数审计和failure工件归档到`rollback/`；
- checkpoint已提交边界内任一前缀、大小或哈希不一致时拒绝恢复；
- 回滚中断后重复执行仍幂等。

### 任务D3：失败JSON/PKL和锁终态

修改：

- `src/pybamm_w10/output.py`
- `src/pybamm_w10/runner.py`
- 新增`tests/test_failure_artifacts.py`

实现：

- 原子写`failures/failure-*.json`和仅取证`failure-*.pkl`；
- PKL包装固定`forensic_only=true`、`resume_eligible=false`、`schema_version=1`；
- `run_status.json`、`run.log`和failure JSON使用同一稳定reason；
- 锁元数据在释放前写入业务终态，`release_reason`只表示句柄释放方式；
- 输出写入失败统一转为`OUTPUT_FAILURE`，同时避免递归写失败。

## 8. 阶段E：heartbeat、参数审计与运行器集成

### 任务E1：实现线程安全heartbeat

修改：

- 新增`src/pybamm_w10/progress.py`
- `src/pybamm_w10/runner.py`
- `src/pybamm_w10/output.py`
- 新增`tests/test_heartbeat.py`

实现：

- `ProgressState`只含轻量不可变标量，不持有PyBaMM Solution；
- 锁获取后、preflight前启动heartbeat线程；
- 每60秒原子写`run_progress.json`；
- 阶段切换时立即写一次，避免短任务无进度；
- 所有业务终态最后写`TERMINATED`再停止线程；
- heartbeat不进入checkpoint静态清单和前缀损坏判断。

测试通过注入短间隔时钟验证创建、周期更新、阶段更新、终止和异常清理；不让单元测试真实等待60秒。

### 任务E2：构建`effective_parameters.json`

修改：

- `src/pybamm_w10/model.py`
- `src/pybamm_w10/runner.py`
- 新增`tests/test_effective_parameters.py`

实现：

- 从原始`OKane2022`和最终有效参数生成审计，不改变模型参数；
- 记录4.85 Ah名义覆盖、几何、热参数、两极理论容量窗口、化学计量端点和允许校准项；
- 明确区分原值、M50T实验覆盖、校准倍率和值来源；
- cycle-0前容量相关字段为`null`，RPT成功后原子补全；
- 生成稳定参数审计指纹并写入checkpoint。

## 9. 阶段F：生产等价短smoke

### 任务F1：重构smoke并增加禁止aging断言

修改：

- `src/pybamm_w10/smoke.py`
- `tests/test_smoke_contract.py`

实现：

1. 短恒流容量事件；
2. 使用生产`build_drive_window_plan()`的短UDDS容量事件；
3. 缩短目标的Step 5+Step 6组合；
4. 连续路径与schema-3 checkpoint恢复路径比较；
5. virtual诊断分支不侵入主状态；
6. 未提交输出回滚；
7. 两进程锁竞争；
8. heartbeat生命周期。

smoke工件必须记录事件时间、profile终点、目标/实际/guard容量、终止种类和状态哈希。测试拦截协议调度，断言没有完成或写入任何aging-cycle结果。

### 阶段F验收门槛

先运行全量pytest，再运行真实smoke：

```powershell
Set-Location -LiteralPath E:\battery\new
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -m pytest -q -p no:cacheprovider
C:\Users\Lenovo\anaconda3\envs\battery\python.exe E:\battery\new\scripts\run_pybamm_w10.py --workspace E:\battery\new --data-root E:\battery\data --mode virtual --smoke --output-dir E:\battery\new\outputs\pybamm_w10\event-remediation-smoke-v2
```

若smoke任何一项失败，停止在本阶段，不启动cycle-0校准。

## 10. 阶段G：校准包、数据门槛与防泄漏

### 任务G1：建立校准包与诊断清单

新增：

- `src/pybamm_w10/calibration/__init__.py`
- `src/pybamm_w10/calibration/data.py`
- `src/pybamm_w10/calibration/artifacts.py`
- `tests/calibration/test_data_inventory.py`

实现：

- 发现15个容量诊断CSV、14个cycling MAT和14个cycling CSV；
- 校验固定节点、表头、单位、有限性、容量单调性和文件哈希；
- README只用于确认W10在容量/HPPC/EIS三个sheet的15个节点；
- 本地HPPC/EIS缺失时写`AGING_DATA_INCOMPLETE / MISSING_W10_HPPC_EIS`；
- 原始数据始终只读；
- 生成`diagnostic_inventory.json`。

留出文件在冻结前允许做存在性、大小和SHA-256清单，但不得将cycle 250–350数值目标返回给校准工作流。

### 任务G2：实现固定拆分和防泄漏入口

新增：

- `src/pybamm_w10/calibration/split.py`
- `tests/calibration/test_split_guard.py`

实现：

- 容量初始目标只提供cycle 0；
- 退化校准视图只提供25–225；
- holdout入口只接受状态`PARAMETERS_FROZEN`且写访问审计；
- 普通路径加载不能绕过视图；
- `holdout_accessed`初始为false；
- holdout失败不能修改原冻结参数哈希。

真实cycle-350解析器精度可在隔离的低层解析测试中验证，但测试不得把结果传给目标函数、候选排序或本阶段校准工件。

## 11. 阶段H：参数schema和OKane2022注入

### 任务H1：实现版本化校准参数

新增/修改：

- `src/pybamm_w10/calibration/parameters.py`
- `src/pybamm_w10/model.py`
- `src/pybamm_w10/config.py`
- `tests/calibration/test_parameters.py`

实现：

- 定义`capacity_scale_factor`、`sei_scale`、`plating_scale`和`lam_scale`；
- 容量因子只缩放共享`Electrode width [m]`；
- 三个退化倍率只映射到规范列出的PyBaMM键；
- 外部圆柱几何、热面积/体积、电极高度/厚度、活性分数、最大浓度和OCP保持不变；
- 参数边界、log变换、来源、有效值和指纹可审计；
- 当前阶段的三个退化倍率必须为1且状态`not_calibrated`；
- 文件状态未完整DFN确认时禁止作为正式350循环参数。

派生参数集不是修改PyBaMM内置OKane2022，而是运行时按以下顺序组装：

```text
OKane2022原始值
  -> M50T已批准实验覆盖
  -> capacity_scale_factor
  -> 未来才允许的三个退化倍率
```

### 任务H2：CLI参数校验

修改：

- `src/pybamm_w10/cli.py`
- `scripts/run_pybamm_w10.py`（仅在入口转发需要时）
- 新增`tests/test_cli_contract.py`

实现：

- 新增动作`--calibrate-capacity`；
- 新增可选`--calibration-params <json>`；
- 新增必需或显式默认的`--data-root <path>`，本项目正式值为只读`E:\battery\data`；
- `--workspace`必须解析为`E:\battery\new`，所有可写路径必须位于该根目录；
- `--calibrate-capacity`默认输出到`E:\battery\new\outputs\pybamm_w10_calibration\m50t-w10-v1`；
- 该动作内部固定strict-W10 cycle-0语义，不允许aging调度；
- 正式`--run/--resume`只接受`PARAMETERS_FROZEN`且完整DFN确认的参数；
- 当前`CAPACITY_CALIBRATED`文件只能用于prepare、受控smoke或后续获批的校准流程，不能冒充正式老化参数。

## 12. 阶段I：cycle-0目标、电压比较与状态机

### 任务I1：实现容量与电压目标

新增：

- `src/pybamm_w10/calibration/objectives.py`
- `tests/calibration/test_objectives.py`

实现：

- 容量相对误差使用4.865884391243259 Ah；
- 将实测和模拟放电曲线插值到1001点共同归一容量网格；
- 分别计算全2.5–4.2 V范围、10%–90%中段RMSE、最大绝对误差和终点容量误差；
- 重复容量点先按稳定规则聚合，非单调或无重叠区间显式失败；
- 全区间RMSE大于50 mV时标记`CAPACITY_MATCHED_VOLTAGE_FAILED`，不调整OCP或动力学参数。

### 任务I2：实现校准状态机和原子工件

新增：

- `src/pybamm_w10/calibration/workflow.py`
- `src/pybamm_w10/calibration/surrogate.py`
- `tests/calibration/test_workflow.py`
- `tests/calibration/test_surrogate_budget.py`

实现：

- 固定规范中的状态转换；
- 当前数据只允许到`CAPACITY_CALIBRATED`后转`AGING_DATA_INCOMPLETE`；
- `surrogate.py`本轮只实现配置、参数映射验证和拒绝门槛，不运行候选；
- 固定32→16→8→4→2预算并禁止自适应扩展；
- 所有工件原子写入，候选有独立目录、状态、日志和指纹；
- 复用正式运行的单写者锁和回滚语义；
- 中断候选不复用未提交主状态。

## 13. 阶段J：cycle-0 DFN容量求根

### 任务J1：实现独立候选求解器

新增：

- `src/pybamm_w10/calibration/capacity.py`
- `tests/calibration/test_capacity_search.py`

实现：

1. 在0.90和1.02分别从新建DFN及规范20% SOC运行cycle-0 RPT；
2. 验证目标被夹住、容量响应单调；
3. 用有界二分法搜索，最多16次候选求解；
4. 达到容量误差0.2%且面积区间不大于`1e-4`后收敛；
5. 对最优面积因子做一次完全独立复算；
6. 容量复算差不超过0.02%；
7. 比较cycle-0实验电压曲线；
8. 写入参数、搜索轨迹、状态、图形和审计。

所有candidate必须：

- 使用独立模型、参数、solver和canonical initial state；
- 不读取前一candidate checkpoint作为初态；
- 不进入`run_standard_cycle()`；
- 不增加aging-cycle编号；
- 不写正式`cycle_summary.csv`。

### 任务J2：输出工件

预期目录：

```text
E:\battery\new\outputs\pybamm_w10_calibration\m50t-w10-v1\
├── calibration_config.json
├── diagnostic_inventory.json
├── capacity_search.csv
├── capacity_calibration.json
├── voltage_curve_comparison.csv
├── effective_parameters.json
├── calibrated_parameters.json
├── calibration_status.json
├── run.log
├── checkpoints/
├── figures/
└── candidates/
```

`calibrated_parameters.json`必须满足：

- 参数集名`OKane2022-M50T-W10-v1`；
- 写入求得的`capacity_scale_factor`；
- 三个退化倍率均为1且`not_calibrated`；
- 状态最高为`CAPACITY_CALIBRATED`或`AGING_DATA_INCOMPLETE`；
- `full_dfn_confirmed=false`仅指老化参数尚未完成完整DFN确认；
- `holdout_accessed=false`；
- 不含cycle 250–350评价指标；
- 文件自身哈希可复算。

## 14. 阶段K：真实执行与验收顺序

真实执行严格串行过门槛。

### K1：全量非求解测试

```powershell
Set-Location -LiteralPath E:\battery\new
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -m pytest -q -p no:cacheprovider
```

失败即停止。

### K2：prepare只构建检查

```powershell
Set-Location -LiteralPath E:\battery\new
C:\Users\Lenovo\anaconda3\envs\battery\python.exe E:\battery\new\scripts\run_pybamm_w10.py --workspace E:\battery\new --data-root E:\battery\data --prepare
```

核验20% SOC、W10 2600 s、175个完整单元、参数审计和环境版本。

### K3：短真实PyBaMM smoke

```powershell
Set-Location -LiteralPath E:\battery\new
C:\Users\Lenovo\anaconda3\envs\battery\python.exe E:\battery\new\scripts\run_pybamm_w10.py --workspace E:\battery\new --data-root E:\battery\data --mode virtual --smoke --output-dir E:\battery\new\outputs\pybamm_w10\event-remediation-smoke-v2
```

成功要求容量事件早于profile末端、容量误差不超过0.1%、guard未计入实际容量、恢复状态一致、锁与heartbeat通过、无aging输出。

### K4：cycle-0容量校准

仅在K1–K3全部通过后执行：

```powershell
Set-Location -LiteralPath E:\battery\new
C:\Users\Lenovo\anaconda3\envs\battery\python.exe E:\battery\new\scripts\run_pybamm_w10.py --workspace E:\battery\new --data-root E:\battery\data --calibrate-capacity --output-dir E:\battery\new\outputs\pybamm_w10_calibration\m50t-w10-v1
```

成功要求：

- cycle-0容量相对误差不超过0.2%；
- 最优候选独立复算差不超过0.02%；
- 电压RMSE不超过50 mV，或明确标记`CAPACITY_MATCHED_VOLTAGE_FAILED`；
- HPPC/EIS缺失状态如实记录；
- 不存在任何aging-cycle结果。

### K5：最终回归与只读证明

1. 再跑全量pytest；
2. 比对原工程代码和`virtual-formal-001`实施前后文件清单、大小和哈希；
3. 搜索新输出中是否出现cycle 1或更大aging-cycle提交；
4. 验证未访问holdout；
5. 验证所有JSON指纹和参数文件哈希；
6. 输出实施报告，列出通过项、失败项、实际容量因子、电压误差和后续门槛。

## 15. 预计文件变更清单

以下路径全部位于`E:\battery\new`。原工程同名文件不修改。

修改：

- `README.md`
- `src/pybamm_w10/backend.py`
- `src/pybamm_w10/cli.py`
- `src/pybamm_w10/config.py`
- `src/pybamm_w10/figures.py`
- `src/pybamm_w10/model.py`
- `src/pybamm_w10/output.py`
- `src/pybamm_w10/protocol.py`
- `src/pybamm_w10/runner.py`
- `src/pybamm_w10/smoke.py`
- `src/pybamm_w10/types.py`
- `src/pybamm_w10/udds.py`
- 现有相关测试文件。

新增：

- `src/pybamm_w10/progress.py`
- `src/pybamm_w10/calibration/__init__.py`
- `src/pybamm_w10/calibration/artifacts.py`
- `src/pybamm_w10/calibration/capacity.py`
- `src/pybamm_w10/calibration/data.py`
- `src/pybamm_w10/calibration/objectives.py`
- `src/pybamm_w10/calibration/parameters.py`
- `src/pybamm_w10/calibration/split.py`
- `src/pybamm_w10/calibration/surrogate.py`
- `src/pybamm_w10/calibration/workflow.py`
- `tests/calibration/`下测试；
- 事件、schema、heartbeat、failure和CLI相关测试文件。

外部只读依赖：

- `E:\battery\data/**`
- `E:\battery\outputs\pybamm_w10\virtual-formal-001/**`（仅最终哈希比对）

## 16. 本轮完成定义

只有以下条件全部满足，才可报告本轮实施完成：

- 事件可靠性整改测试全部通过；
- 真实短smoke全部通过；
- Step 6真实短测试由容量事件提前终止；
- final time不再误分类为物理失败；
- checkpoint schema 3、回滚、锁和heartbeat通过；
- failure JSON/PKL可审计且不可恢复；
- 输出schema和LAM图字段正确；
- 校准数据门槛和留出防泄漏通过；
- cycle-0容量校准达到0.2%或明确失败，不伪装成功；
- cycle-0电压比较完成；
- 参数集明确是`OKane2022-M50T-W10-v1`，不是篡改内置OKane2022；
- 三个退化倍率仍为1且标记未校准；
- HPPC/EIS缺失仍阻断老化标定；
- 未执行任何aging cycle、完整1-cycle或350-cycle；
- 旧失败目录逐文件未改变；
- 原工程代码、测试、脚本、配置和README逐文件未改变；
- 所有新代码与可写输出均位于`E:\battery\new`；
- 原始数据只通过`--data-root E:\battery\data`读取且未被修改。

## 17. 本轮结束后的下一授权门槛

本轮完成后仍不能直接运行350循环。下一阶段至少需要：

1. 取得并核验W10 HPPC/EIS；
2. 实现和验证完整strict-W10 RPT时间线；
3. 用户单独批准一个完整生产参数aging cycle；
4. 单循环审计通过；
5. 用户单独批准老化参数搜索预算；
6. 只用cycle 25–225完成完整DFN校准并冻结参数；
7. 冻结后才允许读取cycle 250–350留出目标；
8. 留出通过后再单独批准350循环正式运行。
