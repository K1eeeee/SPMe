# SPMe W10 第一阶段 SOH 老化参数标定实施计划

日期：2026-08-26  
状态：待用户审阅后实施  
设计依据：`docs/superpowers/specs/2026-08-25-spme-w10-stage1-soh-calibration-design.md`

## 1. 实施目标与授权边界

本计划实现一次显式启动、可断点续算的 W10 第一阶段 SOH 标定流程：

```text
基线 cycle 0–30 门禁
  -> 基线和三个中倍率探针至 cycle 75
  -> 按规则补充最多三个高倍率探针
  -> 生成组合候选 A/B
  -> A/B 独立运行至 cycle 188
  -> 仅用标定集排名并冻结第一名
  -> 第一名续算至 cycle 350
  -> 一次性读取留出集并验收
```

本轮实现包括：

- 固定的 cycle 0、标定和留出节点边界；
- SOH 指标、验收和分层排名；
- 中/高倍率探针及 A/B 组合生成；
- 候选运行、阶段暂停、检查点续算和失败分类；
- 单命令自动编排、进度显示和时间估计；
- 参数冻结、留出集访问审计、最终报告和图；
- 非求解单元测试及短数值回归门禁。

本轮不包括：

- HPPC/EIS 读取、特征提取或重排；
- 新增阻抗、动力学或扩散参数；
- 贝叶斯优化、MCMC、全网格或并行调度框架；
- 机理消融或实验唯一归因；
- 自动修改求解器设置；
- 在编码测试中自动启动约 48 小时的正式标定。

正式长运行只能由用户显式执行生产命令启动。

## 2. 当前基线与保护要求

计划编写时基线为：

```text
140 passed in 21.06s
Git branch: main
HEAD: d22cc85 Initial import of SPMe W10 aging model
```

当前存在用户所有的求解修复改动：

```text
M  src/pybamm_w10/model.py
M  tests/test_solver_profiles.py
?? scripts/probe_cycles100_120_single_step_charge.py
?? scripts/replay_cycle1_122_conservative_standard_charges.py
?? tests/test_conservative_standard_charge_replay.py
```

实施时必须遵守：

1. 不回滚、覆盖或顺手格式化上述文件；
2. 每次提交只暂存任务明确列出的路径；
3. 求解修复未单独提交时，运行清单必须记录 Git HEAD、dirty 状态和相关文件内容哈希；
4. 任一任务开始前若上述文件出现新的非预期变化，先停下核对；
5. 不复用旧求解指纹生成的候选检查点。

求解配置语义已经变化，但当前 `solver_execution_version` 仍为 `stage-local-time-v1`。实施第一步必须将其提升为新版本，从配置和环境两层拒绝旧检查点。

## 3. 最小实现原则

- 不新增依赖；只使用标准库、NumPy 和项目已安装的 SciPy/Matplotlib；
- 不为设计中的六项职责分别创建抽象接口或工厂；
- 复用现有原子 JSON/CSV、目录锁、heartbeat、checkpoint 和回滚逻辑；
- 保留 `W10Runner` 作为唯一真实 PyBaMM 循环执行器；
- 只新增一个生产模块 `src/pybamm_w10/calibration/aging.py` 负责端到端编排；
- 现有 `surrogate.py` 直接改造成确定性候选生成器，不保留已失效的 32 点旧预算；
- 阶段停止是“已提交检查点后的暂停”，不是修改物理 `max_aging_cycles`；
- 时间估计只显示和落盘，绝不停止、跳过或改变候选；
- 所有非平凡分支先写失败测试，再做最小实现。

## 4. 固定常量与文件布局

### 4.1 数据与参数

```text
ANCHOR_NODES      = (0,)
CALIBRATION_NODES = (25, 75, 122, 146, 148, 151, 159, 188)
HOLDOUT_NODES     = (225, 250, 275, 300, 325, 350)

CAPACITY_SCALE_FACTOR = 0.95630859375
DEGRADATION_LOG10_BOUNDS = (-1.0, 1.0)

MID_SCALE  = 3.16
HIGH_SCALE = 10.0
HIGH_PROBE_NOISE_FLOOR_PP = 0.05
HIGH_PROBE_GAP_FRACTION   = 0.25

CALIBRATION_RMSE_LIMIT_PP = 1.0
HOLDOUT_RMSE_LIMIT_PP     = 3.0
CYCLE_350_ABS_LIMIT_PP    = 4.0
```

### 4.2 默认输出

```text
outputs/pybamm_spme_calibration/w10-stage1-soh-v1/
  stage1_manifest.json
  stage1_status.json
  stage1_progress.json
  target_manifest.json
  candidate_manifest.json
  candidate_ranking.csv
  frozen_parameters.json
  holdout_access.json
  stage1_report.json
  stage1_soh_comparison.csv
  figures/stage1_soh_sim_vs_experiment.png
  candidates/<candidate-id>/...
```

候选目录直接复用现有 `W10Runner` 输出结构。候选参数文件置于对应候选目录中，并在首次运行前写入。

### 4.3 单一生产命令

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -B scripts\run_pybamm_w10.py `
  --workspace E:\SPMe `
  --data-root E:\SPMe\data `
  --calibrate-soh-stage1 `
  --calibration-params E:\SPMe\inputs\spme_transferred_parameters.json `
  --output-dir E:\SPMe\outputs\pybamm_spme_calibration\w10-stage1-soh-v1
```

对同一输出目录再次执行相同命令即执行安全续算；不增加第二套 resume CLI。

## 5. 任务 0：固定求解修复基线和新执行指纹

修改：

- `src/pybamm_w10/config.py`
- `tests/test_charge_config_v3.py`
- `tests/test_checkpoint_schema3.py`

只读保护：

- `src/pybamm_w10/model.py`
- `tests/test_solver_profiles.py`
- 当前三个未跟踪求解诊断/回归文件

步骤：

1. 先运行求解配置、标准充电重试和 checkpoint 测试，记录绿色基线；
2. 增加失败测试，要求新的 `solver_execution_version` 与旧 checkpoint 不兼容；
3. 将版本提升为明确的新值，例如 `stage-local-time-v2-robust-charge`；
4. 不改变用户已经修复的 solver profile 数值；
5. 重跑定向测试和全量测试；
6. 后续所有候选使用新版本，不尝试加载旧122圈检查点。

定向命令：

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='E:\SPMe\src'
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -B -m pytest -q -p no:cacheprovider `
  tests/test_charge_config_v3.py `
  tests/test_solver_profiles.py `
  tests/test_standard_charge_retry.py `
  tests/test_checkpoint_schema3.py
```

验收：

- 现有求解修复内容未被改变；
- 新旧执行版本产生不同配置指纹；
- 旧 checkpoint 被明确拒绝；
- 全量测试通过。

建议提交仅包含：

```text
src/pybamm_w10/config.py
tests/test_charge_config_v3.py
tests/test_checkpoint_schema3.py
```

## 6. 任务 1：修正第一阶段数据边界和不可变目标清单

修改：

- `src/pybamm_w10/calibration/data.py`
- `src/pybamm_w10/calibration/split.py`
- `src/pybamm_w10/calibration/__init__.py`
- `tests/calibration/test_data_inventory.py`
- `tests/calibration/test_split_guard.py`

步骤：

1. 先写失败测试，固定 anchor、八个标定节点和六个留出节点；
2. 将当前错误包含在标定集中的 cycle 225 移至留出集；
3. `load_calibration_capacity_targets()` 只返回 cycle 0 加八个标定节点；
4. `load_holdout_capacity_targets()` 只在 `PARAMETERS_FROZEN` 和合法 SHA-256 下返回六个留出节点；
5. 修改公开 capacity inventory，使 cycle 225–350 的容量端点均不向普通标定视图暴露；
6. 第一阶段 `aging_calibration_gate` 只检查容量/循环数据，不再要求 HPPC/EIS；
7. 用现有文件 SHA-256 生成 `target_manifest.json` 所需数据，不复制或修改原始数据；
8. 增加重复节点、缺失节点、集合交叉和 cycle 225 泄漏测试。

第一阶段不修复或解析 HPPC/EIS MAT；该工作留给第二阶段。

定向命令：

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -B -m pytest -q -p no:cacheprovider `
  tests/calibration/test_data_inventory.py `
  tests/calibration/test_split_guard.py
```

验收：

- 普通标定 API 无法读取 cycle 225–350 容量值；
- cycle 225 正确属于留出集；
- HPPC/EIS 缺失不阻塞 SOH 第一阶段；
- 留出访问仍需冻结参数并生成审计文件。

## 7. 任务 2：实现 SOH 指标、验收和确定性排名

修改：

- `src/pybamm_w10/calibration/objectives.py`
- `tests/calibration/test_objectives.py`

步骤：

1. 先增加 cycle 0 双侧归一化失败测试；
2. 增加只接收指定节点的 SOH 指标函数，拒绝缺失、额外、重复、非正或非有限容量；
3. 返回每个节点的仿真/实验 SOH、signed error、absolute error，以及 RMSE、最大绝对误差和终点误差；
4. 增加第一阶段验收函数，精确执行 `1/3/4` 个百分点阈值；
5. 增加候选评分记录和分层排名函数；
6. 排名先取最小 RMSE，再把 `min_rmse + 0.1 pp` 内候选作为同组，依次比较最大误差、cycle 188误差、对数参数范数和重试次数；
7. 数值删失候选不进入数值排名，也不获得人为大误差；
8. 用边界相等、阈值内并列、阈值外、不同重试数和输入顺序打乱测试确定性。

不修改现有完整15节点 `evaluation.py` 行为；校准阶段使用本任务的新子集指标，避免提前加载留出数据。

定向命令：

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -B -m pytest -q -p no:cacheprovider `
  tests/calibration/test_objectives.py `
  tests/test_soh_evaluation.py
```

验收：

- SOH 定义与设计规范完全一致；
- 标定和留出 RMSE 分开计算；
- 三个验收阈值含等号；
- 排名不依赖字典或文件遍历顺序。

## 8. 任务 3：用现有 surrogate 模块实现确定性候选生成

修改：

- `src/pybamm_w10/calibration/surrogate.py`
- `tests/calibration/test_surrogate_budget.py`

步骤：

1. 删除已失效的 `SurrogateExecutionDisabled` 和 32 点旧预算测试；
2. 增加最小的不可变候选记录：候选 ID、三个倍率、来源、父候选和阶段；
3. 固定生成 baseline、三个 `3.16` 中倍率和三个 `10` 高倍率候选；
4. 实现 cycle 75 高倍率触发规则：错误方向、响应小于 `max(0.05, 0.25*gap)`、中倍率删失、cycle 25/75 响应方向不一致；
5. 实现每种机理代表候选选择：先数值有效，再最小化 cycle 75 绝对残差，`0.1 pp` 内优先中倍率和更少重试；
6. 使用 NumPy 构建 cycle 25/75 的 `2x3` 响应矩阵；
7. 使用已安装的 `scipy.optimize` 做有界最小残差和最小范数二阶段求解，生成 A；
8. 使用 SVD 最弱辨识方向和固定步长序列生成 B；B 必须位于边界内，预测 RMSE 不高于 A 超过 `0.1 pp`；
9. 若无法形成充分多样性，返回确定性的次优有效解并写明原因；
10. 所有函数只消费标定响应字典，不接受 data root 或留出目标。

测试必须覆盖：

- 三个中倍率固定生成；
- 四种高倍率触发条件；
- 不触发时不生成多余候选；
- A/B 在 `0.1–10` 内；
- 相同输入产生逐位相同的候选；
- B 与 A 不同，或显式返回多样性不足原因；
- 数值删失探针不被用于响应矩阵。

定向命令：

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -B -m pytest -q -p no:cacheprovider `
  tests/calibration/test_surrogate_budget.py `
  tests/calibration/test_objectives.py
```

验收：

- 不新增优化依赖；
- 候选生成不调用 PyBaMM；
- 不存在旧的32点或自动扩预算路径；
- A/B 可由工件完整复现。

## 9. 任务 4：为 W10Runner 增加已提交检查点暂停和进度回调

修改：

- `src/pybamm_w10/types.py`
- `src/pybamm_w10/runner.py`
- `src/pybamm_w10/config.py`
- `tests/test_runner_stage_pause.py`（新增）
- `tests/test_runner_checkpoint_order.py`
- `tests/test_cli_contract.py`

步骤：

1. 先写失败测试，证明现有 runner 无法在保持 `max_aging_cycles=350` 指纹不变的情况下安全暂停于 cycle 30/75/122/188；
2. 增加 `RunStatus.PAUSED`；
3. 给 `W10Runner.run()` 增加仅限关键字参数：

   ```text
   stop_after_cycle: int | None
   progress_callback: Callable[[ProgressState], None] | None
   postprocess_full_soh: bool = True
   ```

4. `stop_after_cycle` 不写入物理 `RunConfig`，因此分段续算保持同一配置指纹；
5. 非 RPT 节点在普通 cycle checkpoint 提交后暂停；RPT 节点在 RPT 结果和 post-RPT checkpoint 提交后、恢复充电前暂停；
6. 暂停时写 `run_status.json = PAUSED`，不得生成 `RUN_COMPLETED` checkpoint；
7. resume 时继续执行 checkpoint 指定的下一阶段，不能重复 RPT 或 post-RPT recovery；
8. `progress_callback` 与 heartbeat 使用同一 `ProgressState`，仅同步通知编排器，不另起轮询线程；
9. `postprocess_full_soh=False` 时 runner 不读取实验 SOH；第一阶段最终验证由冻结门禁后的编排器处理；
10. 在 `RunConfig` 增加可选的64位 `run_context_fingerprint`，由现有 config fingerprint 自动绑定候选、目标和源代码清单；
11. 保持现有 `--run`、`--resume` 和完整350圈行为不变。

关键测试：

- cycle 30 checkpoint 后暂停并可续算；
- cycle 75 RPT 只执行一次；
- stop target 变化不改变物理 config fingerprint；
- candidate context fingerprint 变化拒绝旧 checkpoint；
- PAUSED 不被完整 SOH 评估器当成 COMPLETED；
- callback 收到 cycle、阶段、solver attempt/profile；
- callback/状态写入失败按 output failure 处理，不污染 checkpoint；
- 普通完整运行仍生成 RUN_COMPLETED。

定向命令：

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -B -m pytest -q -p no:cacheprovider `
  tests/test_runner_stage_pause.py `
  tests/test_runner_checkpoint_order.py `
  tests/test_checkpoint_schema3.py `
  tests/test_output_transactions.py `
  tests/test_rpt_recovery.py
```

验收：

- 阶段暂停始终发生在原子 checkpoint 之后；
- 不通过更改 `max_aging_cycles` 实现阶段运行；
- 不复制不同候选状态；
- 原 runner 回归测试通过。

## 10. 任务 5：实现第一阶段自动编排器、续算和时间显示

新增：

- `src/pybamm_w10/calibration/aging.py`
- `tests/calibration/test_aging_workflow.py`

修改：

- `src/pybamm_w10/calibration/workflow.py`
- `src/pybamm_w10/calibration/artifacts.py`
- `src/pybamm_w10/calibration/__init__.py`
- `tests/calibration/test_workflow.py`

步骤：

1. 扩展现有 workflow 状态，不另建第二套通用状态机；最少保留：

   ```text
   AGING_CALIBRATION_READY
   PROBING
   COMBINATIONS_PROPOSED
   SPME_CALIBRATED
   PARAMETERS_FROZEN
   VALIDATING
   HOLDOUT_EVALUATED
   COMPLETED
   CALIBRATION_FAILED
   VALIDATION_FAILED
   VALIDATION_NUMERICAL_FAILURE
   ```

2. 给现有 `CalibrationWorkflow` 增加显式 `status_path`；默认仍写旧的 `calibration_status.json`，第一阶段编排器明确传入 `stage1_status.json`，避免复制第二套状态持久化逻辑；
3. 增加从 `stage1_status.json` 恢复的构造入口；状态和候选 manifest 每次原子重写；
4. 用根输出目录锁阻止同一标定并发启动；候选继续使用自己的 run lock；
5. 计算 source/config/data/基础参数清单哈希，并据此生成 `run_context_fingerprint`；
6. 新求解指纹下始终新运行 baseline，不扫描或猜测历史输出；
7. baseline 先暂停于 cycle 30，通过门禁后从同一 checkpoint 继续至75；
8. 依次执行三个中倍率候选至75，按确定性规则补充高倍率候选；
9. 生成 A/B 参数文件，各自从 cycle 0 运行至122，再从自身 checkpoint 运行至188；
10. 将 W10 `NUMERICAL_FAILURE` 映射为 `NUMERICALLY_CENSORED`，将 `PHYSICAL_PROTOCOL_FAILURE` 映射为 `PHYSICALLY_INFEASIBLE`；
11. 失败候选保留最后有效 checkpoint，编排器继续其他候选；
12. 硬中断时不把候选误标成失败；重新执行同一命令，从 manifest 和最近有效 checkpoint 继续；
13. 已完成阶段幂等跳过，参数或指纹不一致时拒绝覆盖旧输出；
14. 回调每次阶段/循环变化时同时更新终端和 `stage1_progress.json`。

终端每条进度至少显示：

```text
阶段 | 候选ID | SEI/plating/LAM | cycle | solver attempt/profile |
累计时间 | 最近每圈耗时 | 当前阶段ETA | 全流程ETA | 最新节点SOH残差
```

时间估计使用最近最多10个已完成循环的实际墙钟时间。样本不足时使用本次已完成循环平均值；仍无样本时显示 `ETA unavailable`。时间结果只报告，不触发停止或剪枝。

候选执行顺序固定为：

```text
baseline 0->30->75
SEI-M -> PLATING-M -> LAM-M
按 SEI、PLATING、LAM 顺序执行被触发的高倍率
A 0->122 -> B 0->122
A 122->188 -> B 122->188
```

测试使用 fake runner，禁止真实 PyBaMM 长求解。覆盖：

- 无高倍率、部分高倍率、全部高倍率；
- A/B 各自独立起点；
- 同一命令续算和完成阶段跳过；
- 数值删失继续下一候选；
- manifest 指纹不匹配拒绝；
- ETA 只显示、不改变执行序列；
- Ctrl+C/KeyboardInterrupt 保留最近 checkpoint 并向上传播。

定向命令：

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -B -m pytest -q -p no:cacheprovider `
  tests/calibration/test_aging_workflow.py `
  tests/calibration/test_workflow.py `
  tests/test_failure_artifacts.py `
  tests/test_heartbeat.py
```

验收：

- 一次入口自动按规范顺序执行；
- 中断后执行相同命令即可续算；
- 时间超过48小时也不会自动删减步骤；
- 没有并行候选、任务队列或外部数据库。

## 11. 任务 6：参数冻结、留出验证和最终报告

修改：

- `src/pybamm_w10/calibration/aging.py`
- `src/pybamm_w10/calibration/parameters.py`
- `src/pybamm_w10/evaluation.py`
- `tests/calibration/test_aging_workflow.py`
- `tests/calibration/test_parameters.py`
- `tests/test_soh_evaluation.py`

步骤：

1. A/B 到188后，只加载 cycle 0和八个标定目标，生成 `candidate_ranking.csv`；
2. 第一名标定 RMSE 大于1个百分点时，写 `CALIBRATION_FAILED` 并停止，不读取留出目标；
3. 标定通过时写不可变 `frozen_parameters.json`：

   ```text
   calibration_status = PARAMETERS_FROZEN
   degradation_parameter_status = soh_stage1_calibrated
   holdout_accessed = false
   full_dfn_confirmed = false
   ```

4. 冻结参数文件写入后立即计算 SHA-256，后续验证不得修改该文件；
5. 使用冻结第一名从自己的 cycle 188 checkpoint 续算到350，设置 `postprocess_full_soh=False`；
6. 数值完成后调用留出 gate，生成 `holdout_access.json`，再读取六个留出容量；
7. 分别计算标定 RMSE、留出 RMSE和cycle 350绝对误差；
8. 仅在第一名发生终止性数值失败且第二名也满足标定 RMSE时，允许第二名作为预先确定的数值备用候选运行到350；
9. 第一名数值完成但精度未通过时，不运行第二名挑选更好的验证结果；
10. 最终调用已授权的完整 SOH 报告/绘图路径，输出标定与验证分区标识；
11. `stage1_report.json` 写出验收状态、所有阈值、每节点误差、运行时间、重试/失败统计、冻结参数哈希和工件路径；
12. 引用最优候选已有 `degradation_summary.csv` 中的 SEI、dead/reversible lithium 和正负极 LAM 趋势，不重新求解；
13. 报告明确标记为模型内部趋势，不输出实验唯一机理贡献百分比。

验收状态固定为：

```text
COMPLETED                         三个阈值均通过
CALIBRATION_FAILED                cycle 0–188 RMSE > 1 pp
VALIDATION_FAILED                 验证 RMSE > 3 pp 或 cycle350误差 > 4 pp
VALIDATION_NUMERICAL_FAILURE      验证因终止性数值失败未完成
```

测试必须证明：

- 标定失败时没有 `holdout_access.json`；
- 冻结文件哈希在留出访问前后不变；
- 验证阈值使用六个节点而非全部15节点；
- cycle 350使用绝对误差；
- 验证精度失败不会触发备用候选；
- 只有数值失败能触发已预定备用候选；
- backup 不满足标定阈值时不得验证；
- 最终报告含模型内部趋势免责声明。

定向命令：

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -B -m pytest -q -p no:cacheprovider `
  tests/calibration/test_aging_workflow.py `
  tests/calibration/test_parameters.py `
  tests/calibration/test_split_guard.py `
  tests/test_soh_evaluation.py `
  tests/test_aging_metric_output.py
```

验收：

- 留出数据在参数冻结前不可访问；
- 第一名不会因验证结果被替换；
- 最终报告足以独立复核三个验收条件；
- 参数工件仍不被误标为完成 DFN/论文级机理确认。

## 12. 任务 7：接入单命令 CLI 和自动显示

修改：

- `src/pybamm_w10/cli.py`
- `scripts/run_pybamm_w10.py`（仅在帮助文本需要时修改）
- `tests/test_cli_contract.py`
- `README.md`

步骤：

1. 在现有互斥 action 中增加 `--calibrate-soh-stage1`；
2. 要求 `--calibration-params` 指向容量已标定、三项老化倍率仍未标定的 SPMe 参数工件；
3. 默认使用当前 `virtual` 模式，不改变普通 `--run` 或容量标定模式；
4. 默认输出到 `outputs/pybamm_spme_calibration/w10-stage1-soh-v1`；
5. 新目录开始运行；相同 manifest 的已有目录自动续算；不匹配或未知非空目录拒绝覆盖；
6. CLI 将编排器进度直接打印到终端；完成时打印最优倍率、备用倍率、标定/验证指标、状态和报告路径；
7. `DATA_INVALID` 或配置错误返回退出码2；标定/验证未通过返回1；完整通过返回0；用户中断保留标准中断行为；
8. CLI 单元测试 monkeypatch 编排器，不启动真实模型；
9. README 写明启动、同命令续算、查看 `stage1_progress.json` 和结果目录的方法。

定向命令：

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -B -m pytest -q -p no:cacheprovider `
  tests/test_cli_contract.py `
  tests/calibration/test_aging_workflow.py
```

验收：

- 默认无 action 仍然只是 dry run；
- 只有显式 `--calibrate-soh-stage1` 会启动长流程；
- 同一命令安全续算；
- 终端和 JSON 都能看到当前候选、cycle、重试、累计时间和 ETA。

## 13. 任务 8：完整回归、短数值门禁与生产交接

修改：

- 仅在测试暴露真实缺口时修改相关任务文件；
- 不为了通过测试放宽物理或求解验收规则。

### 13.1 全量非长运行测试

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYTHONPATH='E:\SPMe\src'
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -B -m pytest -q -p no:cacheprovider
```

要求：

- 当前140项基线及全部新增测试通过；
- 测试不读取真实留出容量，除明确的临时测试数据；
- 测试不启动标准75/188/350圈求解。

### 13.2 真实短数值门禁

使用新 solver execution version 和正式容量参数，在新候选输出目录中执行 baseline cycle 0–30。该步骤由正式单命令自动完成，必须检查：

- cycle 23附近无终止性失败；
- 若发生标准重试，失败尝试未污染提交状态；
- cycle 30 checkpoint 可加载；
- 连续运行和从已提交 checkpoint 续算在现有状态/容量容差内一致；
- `stage1_progress.json` 和终端显示同步；
- 旧求解指纹 checkpoint 被拒绝。

门禁失败时停止整个标定并保留证据；不自动修改 solver。

### 13.3 生产运行交接

编码和短门禁通过后，不由测试自动启动正式长运行。向用户交付第4.3节单命令。用户执行后：

- 保持终端运行即可自动完成全部阶段；
- 中断后重新执行同一命令续算；
- `stage1_progress.json` 提供机器可读状态；
- `stage1_report.json` 是最终验收入口；
- 因新求解指纹要求重新生成 baseline 至 cycle 75，名义总时间约为39–51小时；求解重试可能继续增加墙钟时间；
- 48小时仍是计划目标，时间估算器不会强制停止流程。

## 14. 阶段提交与回归顺序

每个任务遵循：

1. 写最小失败测试；
2. 运行定向测试确认因目标缺口失败；
3. 做最小实现；
4. 定向测试通过；
5. 运行相邻回归测试；
6. 仅暂存该任务文件；
7. 检查 `git diff --cached --check`；
8. 提交该任务；
9. 每完成任务4、6、8运行全量测试。

建议提交序列：

```text
chore: version repaired charge solver execution
fix: isolate stage1 calibration and holdout nodes
feat: add stage1 soh objectives and ranking
feat: generate deterministic degradation candidates
feat: pause W10 runs at committed calibration stages
feat: orchestrate resumable stage1 soh calibration
feat: freeze and validate stage1 soh parameters
feat: expose one-command stage1 calibration
docs: document stage1 calibration operation
```

用户现有求解修复文件不得被混入上述提交，除非用户明确要求将其单独提交。

## 15. 最终完成定义

实施完成要求同时满足：

- 全量测试通过；
- 没有新增第三方依赖；
- 一条显式命令可启动并自动推进完整第一阶段；
- 同一命令可从有效 checkpoint 续算；
- cycle 25只做安全检查，cycle 75决定高倍率；
- A/B 都独立运行并在cycle 188排名；
- cycle 225–350在参数冻结前不可读；
- 时间估计只显示、不控制；
- 数值删失不伪装成SOH误差；
- 最终报告分别给出标定 RMSE、留出 RMSE和cycle 350误差；
- 终端和工件均能显示当前阶段、候选、cycle、重试和 ETA；
- 正式长运行仍需用户显式启动。

## 16. 明确延后项

以下内容不应在本计划中顺手实现：

- HPPC/EIS MAT 解析和数据门禁修正；
- HPPC脉冲特征和EIS阻抗特征；
- 二阶段候选重排；
- 新增阻抗/动力学参数；
- 多进程候选并行；
- Web UI、数据库、任务队列或远程监控；
- 三组单机理消融和机理贡献分解。

只有第一阶段完成并形成2–3个近优候选后，才为HPPC/EIS二阶段单独编写规范和实施计划。
