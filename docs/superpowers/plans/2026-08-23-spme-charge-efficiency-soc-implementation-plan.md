# SPMe 充电效率与 SOC 分区分析具体实施细则

- 文档日期：2026-08-23
- 适用工程：`E:\SPMe`
- 上位设计：`E:\SPMe\docs\superpowers\specs\2026-08-21-spme-charge-efficiency-soc-design.md`
- 目标版本：输出 schema 3、checkpoint schema 4、协议算法 `w10-window-v3-charge-efficiency`
- 当前工况：仅 3C；其他倍率由用户后续手动修改第一段恒流电流
- 文档状态：待用户审查；本文只规定实施步骤，不授权修改代码、迁移输出或启动仿真

## 1. 实施目标与边界

本细则把已批准设计拆成可执行、可测试、可回退的编码任务。最终应在每个普通老化循环的标准四段充电中，得到：

1. 全充电窗口的外部充入电量、负极颗粒正常嵌锂增量、可逆镀锂、死锂、SEI、电量平衡及两个效率；
2. 20%–40%、40%–60%、60%–80%、80%–100% 四个固定 SOC 区间的相同电量账本和机理指标；
3. 包含 `time_s`、`current_a` 的逐循环原始充电轨迹；
4. schema 3 输出、schema 4 checkpoint、manifest、原子提交、恢复与回滚；
5. 当前单一 3C 运行可用，未来只改 `charge_3c_a` 即可形成同口径倍率上下文。

本轮编码与部署不得包含：

- 自动扫描或批量执行 0.5C、1C、2C、3C；
- 在单次运行中生成 `charge_rate_soc_comparison.csv`；
- 修改四段充电之外的 W10、RPT、Step 5、UDDS 协议物理参数；
- 把 RPT 后恢复充电、充电后静置或 UDDS 回馈算入标准充电；
- 将旧 v2 数据推算、补齐或伪转换成 v3；
- 在实现和测试未通过前启动生产 350 循环仿真。

## 2. 实施原则与工作约束

### 2.1 测试优先顺序

每个任务严格执行：

1. 先编写失败测试，证明当前代码缺少该能力；
2. 只运行该任务的目标测试并确认因预期原因失败；
3. 完成最小实现；
4. 重跑目标测试；
5. 重跑相关回归测试；
6. 阶段门槛处运行全量测试。

解释器固定为：

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe
```

测试必须关闭 pytest 缓存，并把临时目录指向工程内专用临时目录，不能污染正式输出：

```powershell
Set-Location -LiteralPath E:\SPMe
$env:TEMP = 'E:\SPMe\tmp\charge-efficiency-tests'
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -m pytest -q -p no:cacheprovider
```

### 2.2 代码和数据保护

- `E:\SPMe` 当前不是 Git 仓库，因此不能用提交记录作为回退依据。
- 编码前对 `src`、`tests`、`scripts`、`README.md`、`pyproject.toml` 和设计/计划文档生成 SHA-256 基线清单。
- 不删除旧输出；v2 只做完整归档移动。
- 所有开发 smoke 输出写入 `E:\SPMe\tmp` 或单独的验证运行目录，不得写入拟用的生产 v3 目录。
- 编码期间不改变 `E:\battery\data` 的任何文件。
- 生产 v3 启动是独立的最终操作门槛；通过测试不等于自动启动生产运行。

## 3. 目标代码结构

### 3.1 新增模块

#### `src/pybamm_w10/charge_variables.py`

职责：

- 定义充电分析所需的逻辑变量角色与 PyBaMM 候选变量名；
- 执行模型变量预检和别名解析；
- 记录变量单位、形状、聚合方法、必需性和最终解析名称；
- 从 PyBaMM 连续解按指定时刻提取标量或空间场；
- 生成 `charge_efficiency_variable_inventory.json`。

该模块不计算效率，不写 CSV，不改变 backend 状态。

#### `src/pybamm_w10/charge_efficiency.py`

职责：

- 合并四段充电轨迹并处理重复边界点；
- 计算参考 SOC；
- 查找 40%、60%、80%、100% 精确边界；
- 进行阶段内、SOC 区间内电流积分和时间加权统计；
- 计算库存差、效率、电量平衡及状态；
- 生成一个全窗口汇总、四个 SOC 分区结果和一份标准轨迹。

该模块必须是纯计算模块：输入已提取轨迹、快照和配置，输出不可变结果；不得在内部启动 PyBaMM 求解、追加文件或修改 backend。

### 3.2 修改现有模块

- `src/pybamm_w10/config.py`：版本号、阈值、SOC 定义和充电分析配置。
- `src/pybamm_w10/types.py`：充电快照、阶段轨迹、汇总、SOC 行、分析包、状态、manifest 与 checkpoint 类型。
- `src/pybamm_w10/backend.py`：从当前连续解提取充电阶段轨迹和任意时刻状态，不改变现有阶段终止判定。
- `src/pybamm_w10/protocol.py`：在四段标准充电边界采样并在静置前调用分析器。
- `src/pybamm_w10/output.py`：schema 3 字段顺序、批量 CSV 追加、临时轨迹、manifest 元数据、回滚。
- `src/pybamm_w10/runner.py`：变量预检、每循环事务提交、schema 4 checkpoint 和跳过充电占位行。
- `src/pybamm_w10/cli.py`：必要时增加只运行充电效率 smoke 的显式动作，不改变正式运行默认行为。
- `src/pybamm_w10/smoke.py`：增加真实 SPMe 四段充电分析 smoke，但不执行 Step 5、UDDS 或一个完整老化循环。
- `README.md`：说明 v3 输出、复算公式、运行目录和旧 v2 归档状态。

## 4. 固定实现常量

以下常量进入配置或模块常量，并纳入相应指纹：

```text
output_schema_version = 3
checkpoint_schema_version = 4
protocol_algorithm_version = w10-window-v3-charge-efficiency
charge_efficiency_algorithm_version = charge-efficiency-v1
soc_definition = NEGATIVE_PARTICLE_LITHIUM_DELTA_OVER_FROZEN_Q_REF_V1
soc_anchor_pct = 20.0
soc_boundaries_pct = [20.0, 40.0, 60.0, 80.0, 100.0]
soc_boundary_residual_tolerance_pct = 1e-6
soc_nonmonotonic_tolerance_pct = 1e-6
charge_balance_pass_limit_pct = 0.2
charge_balance_failure_limit_pct = 1.0
plating_crosscheck_abs_tolerance_ah = 1e-8
plating_crosscheck_relative_tolerance = 1e-5
faraday_constant_c_per_mol = 96485.33212
charge_integration_method = STAGE_LOCAL_TRAPEZOID_WITH_EXACT_SOC_BOUNDARIES_V1
soc_crossing_selection_rule = FIRST_UPWARD_CROSSING
```

`RunConfig.__post_init__()` 必须验证：边界严格递增且固定为五个值；所有容差为正；平衡通过阈值小于失败阈值；版本号非空。`normalized()`、`to_json()` 和 `fingerprint()` 必须包含全部新增配置。

## 5. PyBaMM 变量角色和库存换算

### 5.1 核心必需角色

预检必须解析并验证以下角色：

| 逻辑角色 | 当前 SPMe 主变量 | 处理方式 |
|---|---|---|
| 时间 | `solution.t` / `Time [s]` | 秒，严格递增 |
| 外部电流 | `Current [A]` | 充电为负；积分 `max(-I,0)` |
| 端电压 | `Terminal voltage [V]` | 标量 |
| 平均温度 | `X-averaged cell temperature [K]` | 标量 |
| 负极颗粒锂 | `Total lithium in negative electrode [mol]` | 首尾差乘 `F/3600` |
| 总负极镀锂库存 | `Loss of capacity to negative lithium plating [A.h]` | 已包含可逆镀锂与死锂 |
| 死锂浓度 | `Volume-averaged negative dead lithium concentration [mol.m-3]` | 乘负极体积和 `F/3600` |
| 可逆镀锂浓度 | `Volume-averaged negative lithium plating concentration [mol.m-3]` | 仅作几何交叉校验 |
| 常规负极 SEI | `Loss of capacity to negative SEI [A.h]` | 与裂纹 SEI 相加 |
| 裂纹负极 SEI | `Loss of capacity to negative SEI on cracks [A.h]` | 与常规 SEI 相加 |

负极几何体积使用当前有效参数：

```text
negative_electrode_volume_m3
    = Negative electrode thickness [m]
    * Electrode width [m]
    * Electrode height [m]
```

死锂库存：

```text
dead_lithium_inventory_ah
    = volume_averaged_negative_dead_lithium_concentration_mol_m3
    * negative_electrode_volume_m3
    * F / 3600
```

可逆镀锂主值：

```text
reversible_plating_inventory_ah
    = total_plating_inventory_ah - dead_lithium_inventory_ah
```

浓度几何交叉校验值：

```text
reversible_plating_inventory_crosscheck_ah
    = volume_averaged_negative_lithium_plating_concentration_mol_m3
    * negative_electrode_volume_m3
    * F / 3600
```

实施时先用一个已处理的 SPMe 模型检查单位和表达式，确认 `Loss of capacity to negative lithium plating [A.h]` 的确等于可逆镀锂浓度项与死锂浓度项之和；该检查写入单元测试，防止未来 PyBaMM 升级后静默改变口径。

### 5.2 机理必需角色

预检还要解析：

- `X-averaged negative particle surface stoichiometry`
- `X-averaged negative particle stoichiometry`，无该值时允许使用已验证等价的 `R-averaged negative particle stoichiometry`
- `Electrolyte concentration [mol.m-3]`
- `X-averaged negative electrode reaction overpotential [V]`
- `X-averaged negative electrode lithium plating reaction overpotential [V]`
- `X-averaged negative electrode interfacial current density [A.m-2]`
- `X-averaged negative electrode lithium plating interfacial current density [A.m-2]`
- `X-averaged negative electrode SEI interfacial current density [A.m-2]`
- `X-averaged negative electrode SEI on cracks interfacial current density [A.m-2]`
- `X-averaged battery electrolyte ohmic losses [V]`
- `Battery negative particle concentration overpotential [V]`
- `Irreversible electrochemical heating [W]`
- `Ohmic heating [W]`
- `Reversible heating [W]`
- `Total heating [W]`
- `Loss of lithium inventory [%]`
- `LAM_ne [%]`
- `LAM_pe [%]`

所有设计中要求输出的机理角色在当前 v3 均标记为 `required_for_mechanism_analysis=true`。正极 SEI、正极镀锂等未纳入本设计的变量明确记录为 `not_applicable`，不能补零冒充已测量。

### 5.3 变量清单工件

`charge_efficiency_variable_inventory.json` 至少包含：

- `inventory_schema_version`
- `charge_efficiency_algorithm_version`
- `model_name`
- `pybamm_version`
- `model_options_fingerprint`
- 每个角色的 `candidate_names`、`resolved_name`、`unit`、`declared_shape`、`aggregation`、`required_for_core`、`required_for_mechanism_analysis`、`available`、`not_applicable_reason`
- `core_preflight_passed`
- `mechanism_preflight_passed`
- `inventory_sha256`

该文件在循环 1 之前原子写入并进入 manifest/checkpoint 指纹。核心角色或必需机理角色缺失时，运行以 `MISSING_MODEL_VARIABLE` 终止，不能先跑循环再留空列。

## 6. 类型和接口细则

### 6.1 新增类型

在 `types.py` 增加：

- `ChargeEfficiencyStatus`：包含设计规范第 9 节的全部状态；
- `ChargeStateSnapshot`：时间、SOC、温度、库存、机理瞬时量、状态哈希；
- `ChargeStageTrace`：阶段名、时间数组、核心数组、空间场聚合结果；
- `ChargeStageMeasurement`：阶段首尾、时长、外部充电量、电压/温度统计和终止信息；
- `ChargeEfficiencySummary`：`charge_efficiency_summary.csv` 的固定字段；
- `ChargeSocBinResult`：`charge_efficiency_soc_bins.csv` 的固定字段；
- `ChargeTraceArtifact`：最终轨迹相对路径、SHA-256、行数、起止时间；
- `ChargeAnalysisBundle`：一个汇总、恰好四个分区、轨迹列、状态和可选轨迹 artifact。

数组字段不进入 `cycle_summary.csv`；`_flat_dataclass()` 不允许自动展开这些对象。

### 6.2 `AgingBackend` 新接口

增加只读接口：

```text
extract_charge_stage_trace(stage_name, start_time_s, end_time_s) -> ChargeStageTrace
evaluate_charge_state(time_s, resolved_variables) -> ChargeStateSnapshot
```

实现要求：

- 只读取 `self.solution`，不得触发新的 `Simulation.solve()`；
- 仅保留 `[start_time_s, end_time_s]` 范围，阶段首尾必须存在；
- 时间相同的阶段连接点由合并器确定性去重，后一个阶段保留自己的阶段标签；
- 空间场在 `charge_variables.py` 中按角色指定方法聚合；
- 任意数组为空、长度不一致、时间倒退或必需值非有限时抛出结构化 `NumericalFailure`。

现有 `StageOutcome` 不承载大数组，避免污染失败上下文和 checkpoint。

## 7. 计算器实施细则

### 7.1 四段边界

`protocol.py` 必须在 `3c_cc` 开始前记录窗口起点，然后对以下四次 `stage()` 调用分别提取轨迹：

```text
3c_cc -> 4v_cv -> c4_cc -> 4p2v_cv
```

在 `4p2v_cv` 返回后立即记录窗口终点并完成分析；只有分析包已经构造成功后，才执行 `post_charge_rest`。这样静置绝不可能混入电量或库存差。

`charge_already_complete=true` 时不采集这四段，返回状态为 `STANDARD_CHARGE_SKIPPED_AFTER_RPT` 的占位分析包。

### 7.2 阶段积分

每段单独计算：

```text
Q_stage_ah = trapz(max(-current_a, 0), time_s) / 3600
```

再汇总：

```text
external_charge_ah = Q_3c_cc + Q_4v_cv + Q_c4_cc + Q_4p2v_cv
cc_charge_ah = Q_3c_cc + Q_c4_cc
cv_charge_ah = Q_4v_cv + Q_4p2v_cv
```

禁止把四个阶段简单拼接后跨越电流跳变点做一个梯形积分。SOC 边界落在阶段内部时，把边界点插入该阶段时间网格后再拆分；边界恰好等于阶段端点时只保留一份边界样本。

### 7.3 SOC 轨迹和边界

参考 SOC：

```text
reference_soc_pct(t)
    = 20 + 100 * [F/3600 * (n_neg(t) - n_neg(t0))] / q_ref_ah
```

实现流程：

1. 用阶段原始求解点计算离散参考 SOC；
2. 对每个目标边界扫描全部相邻点，找到所有向上穿越括区；
3. 选择第一个向上穿越括区；
4. 使用 `scipy.optimize.brentq` 和 PyBaMM 连续变量求精确时刻；
5. 在该时刻重新读取所有轨迹变量并插入行；
6. 记录穿越次数和 `FIRST_UPWARD_CROSSING`；
7. 验证残差不超过 `1e-6` 个百分点。

相邻 SOC 下降超过 `1e-6` 个百分点时加入 `NON_MONOTONIC_SOC`。某上界不存在时仍返回该区间占位行，实际覆盖率小于 100%，状态为 `SOC_UPPER_BOUND_NOT_REACHED`。若 100% 在充电结束前达到，则四个 bin 截止到 100%，剩余部分只计入全窗口的 `post_100_charge_ah` 和 `post_100_duration_s`。

### 7.4 库存、效率和平衡

所有区间均用首尾库存差：

```text
intercalated_charge_increment_ah
    = F/3600 * (negative_particle_lithium_mol_end
                - negative_particle_lithium_mol_start)

reversible_plating_increment_ah
    = reversible_plating_inventory_end_ah
    - reversible_plating_inventory_start_ah

dead_lithium_increment_ah
    = dead_lithium_inventory_end_ah
    - dead_lithium_inventory_start_ah

sei_increment_ah
    = sei_inventory_end_ah - sei_inventory_start_ah
```

效率和平衡完全沿用已批准设计公式，不做 0%–100% 裁剪。`reversible_plating_increment_ah < 0` 时保存原值，同时保存 `reversible_plating_depletion_ah=max(-increment,0)`。

状态优先级固定为：

```text
核心变量/数值错误
  > SOC 锚点或边界错误
  > 电量平衡失败
  > 镀锂交叉校验失败
  > 警告或旧镀锂释放
  > VALID
```

多个状态按稳定顺序以分号连接写入 `status_flags`。主状态和两个有效性布尔值由单一函数生成，CSV 写出层不得自行重判。

### 7.5 机理聚合规则

- `mean_current_a`：`abs(Current [A])` 的时间加权均值；
- `mean_voltage_v`、反应过电位和各平均电流密度：时间加权均值；
- `maximum_voltage_v`、温度最大值：区间内包含精确边界后的最大值；
- 最低电解液浓度：区间内所有时间和空间节点的最小值；
- 负极表面/平均化学计量比：区间终点值；
- 颗粒径向梯度：终点表面化学计量比减终点体积平均化学计量比；
- 镀锂过电位：保留 PyBaMM 原始符号并取最不利极值；该极值方向由变量清单中的 `aggregation` 明确记录；
- 负极 SEI 电流密度：常规 SEI 与裂纹 SEI 的同口径贡献相加后再统计，不能只取其中一项；
- 发热能量：对功率 `[W]` 按时间积分并除以 3600 得到 Wh；
- LLI、负极 LAM、正极 LAM：区间起点值；
- `soh_pct = 100 * q_ref_ah / q_ref_initial_ah`，其中 `q_ref_initial_ah` 来自成功的 RPT node 0。

## 8. 输出 schema 3 实施细则

### 8.1 固定字段表

`output.py` 增加显式字段常量：

- `CYCLE_SUMMARY_V3_FIELDS`
- `CHARGE_EFFICIENCY_SUMMARY_V3_FIELDS`
- `CHARGE_EFFICIENCY_SOC_BIN_V3_FIELDS`
- `CHARGE_TIMESERIES_V3_FIELDS`

字段内容和顺序必须与设计规范第 8 节一一对应。禁止依赖 `dict` 或 dataclass 的偶然字段顺序生成正式表头。

所有专用 CSV 行增加 `output_schema_version=3` 和 `charge_efficiency_algorithm_version`；若设计字段为不可用，占位行使用空字符串，数值有效行不得混用字符串 `nan`、`inf` 或 `None`。

### 8.2 `cycle_summary.csv`

保留现有核心循环字段，只增加：

- `configured_nominal_charge_rate_c`
- `effective_charge_rate_c`
- `useful_charge_efficiency_pct`
- `reversible_retention_pct`
- `charge_efficiency_status`
- `complete_soc_bin_count`

详细库存只进入专用文件。普通有效充电 `complete_soc_bin_count=4`；跳过或未达到完整边界时按实际完整 bin 数填写。

### 8.3 汇总和四行 SOC 输出

新增批量追加函数：

```text
append_charge_efficiency_summary(path, summary)
append_charge_soc_bins(path, four_rows)
```

`append_charge_soc_bins` 在写入前验证：

- 行数恰好为 4；
- `soc_bin_id` 固定且不重复；
- 四行循环、倍率、q_ref 和算法版本一致；
- 表头与既有 schema 完全一致。

四行序列化为一个批次并一次刷新；任何一行校验失败时一个字节也不追加。

### 8.4 原始轨迹

轨迹路径固定为：

```text
charge_timeseries/cycle-XXX.csv
```

写入流程：

1. 在同目录创建随机命名临时文件；
2. 写固定表头和全部行；
3. `flush + fsync`；
4. 重读校验表头、行数、时间单调性、阶段首尾、边界行和外部电量；
5. 计算 SHA-256；
6. 用原子替换形成最终文件；
7. 已存在的最终文件若属于已提交 artifact，禁止覆盖；若是未提交文件，由恢复流程先隔离。

轨迹必须至少有四个阶段首尾及 20/40/60/80/100 边界；普通采样行的 `soc_boundary` 为空，精确边界行为数值。`time_s`、`current_a` 永远是正式列。

## 9. 协议和运行器接入

### 9.1 协议接入

`ProtocolStateMachine.run_standard_cycle()` 调整为：

1. 创建充电捕获上下文；
2. 获取四段前起点；
3. 每段成功后立即提取该段轨迹；
4. 第四段结束后完成分析包；
5. 再执行 `post_charge_rest`、Step 5 和 UDDS；
6. 把分析包挂到 `CycleResult`，但大数组只在 runner 完成输出后释放。

若四段中任一段异常终止，不提交本循环充电结果；失败上下文增加 `charge_stage`。若科学计算成功但有平衡或 SOC 状态，按设计允许提交带状态结果。

### 9.2 运行前预检

`W10Runner._run_locked()` 在构建模型和环境工件后、初始 RPT/循环 1 前：

1. 解析变量角色；
2. 原子写变量 inventory；
3. 校验模型/配置/算法指纹；
4. 失败则不进入老化循环；
5. 成功后把 inventory 哈希写入 checkpoint。

### 9.3 单循环事务顺序

runner 对普通循环严格执行：

1. 协议返回 `CycleResult + ChargeAnalysisBundle`；
2. 在内存检查一个汇总、四行 bin 和轨迹；
3. 写临时轨迹、复算、原子改名；
4. 追加 `cycle_summary.csv`；
5. 追加 `degradation_summary.csv`；
6. 追加 `charge_efficiency_summary.csv`；
7. 以一个批次追加四行 `charge_efficiency_soc_bins.csv`；
8. 写本循环其他已批准输出；
9. 刷新全部 append-only 文件；
10. `backend.compact_state()`；
11. 增加 transaction；
12. 构造 manifest；
13. 原子保存 schema 4 checkpoint；
14. 原子替换 `output_manifest.json`，完成提交。

步骤 3–14 任一步失败，运行终止并留下结构化取证；下次从最后成功 checkpoint 恢复时，回滚未提交尾部和轨迹。不能在失败后继续执行 Step 5 或下一个循环。

### 9.4 RPT 后跳过标准充电

`charge_already_complete=true` 时：

- 汇总写一行；
- SOC 文件写四行；
- 数值分析字段为空；
- `primary_status=STANDARD_CHARGE_SKIPPED_AFTER_RPT`；
- 两个有效性字段均为 false；
- `complete_soc_bin_count=0`；
- 不生成轨迹；
- 仍与当前循环其他输出一同进入事务，保证行数可审计。

## 10. manifest、checkpoint 和回滚

### 10.1 类型升级

`OutputCommitManifest` 增加：

- `output_schema_version`
- `last_charge_efficiency_cycle`
- `last_complete_soc_bin_cycle`

`ArtifactCommit` 对充电轨迹增加可选元数据：

- `artifact_kind=charge_timeseries`
- `cycle`
- `row_count`
- `start_time_s`
- `end_time_s`

`Checkpoint` schema 4 增加：

- `charge_efficiency_algorithm_version`
- `charge_efficiency_variable_inventory_sha256`
- `last_charge_efficiency_cycle`
- `last_complete_soc_bin_cycle`

schema 4 loader 必须明确拒绝 schema 3，稳定错误原因仍使用 `UNSUPPORTED_CHECKPOINT_SCHEMA`。

### 10.2 append-only 文件

`APPEND_OUTPUTS` 增加：

- `charge_efficiency_summary.csv`
- `charge_efficiency_soc_bins.csv`

manifest 对每个文件记录提交字节偏移、数据行数和前缀 SHA-256。正常循环提交时：

```text
last_completed_cycle
  == last_charge_efficiency_cycle
  == last_complete_soc_bin_cycle
```

跳过标准充电的循环虽然没有有效效率和轨迹，但已提交占位行，因此三个循环边界仍必须一致；完整 bin 数由行内字段表达。

### 10.3 恢复规则

恢复先验证已提交前缀和所有 artifact，再处理尾部：

- 汇总 CSV 超出 checkpoint 的尾部截断并归档；
- SOC CSV 超出 checkpoint 的尾部截断并归档；
- 未提交的 `charge_timeseries/cycle-XXX.csv` 移入 `rollback/<timestamp>/`；
- 已提交轨迹缺失、哈希变化、行数变化或时间范围变化时拒绝恢复；
- 只有部分 SOC 行、只有汇总无轨迹、或轨迹存在但汇总缺失，均视为未提交事务并回滚；
- 回滚中断后再次执行必须幂等。

失败 JSON 增加可选字段：`charge_stage`、`soc_bin_id`、`charge_efficiency_status`、`charge_trace_temp_path`；失败轨迹继续标记 forensic-only，不能用于恢复。

## 11. v2 到 v3 的安全切换

该步骤只在代码、全量测试和 v3 smoke 全部通过后执行。

### 11.1 停止条件

1. 只识别命令行明确指向 `E:\SPMe` 的 Python/PowerShell 仿真或监控进程；
2. 对仿真进程发出可控终止，等待当前求解调用退出；
3. 验证目标进程已经消失，`.run.lock` 不再被持有；
4. 记录停止前最后 checkpoint、manifest、循环和事务号；
5. 不终止其他 Python、Anaconda 或用户进程。

### 11.2 归档

源目录固定为：

```text
E:\SPMe\outputs\pybamm_spme\w10-soh-comparison-v1
```

目标目录固定为：

```text
E:\SPMe\outputs\archive\v2\w10-soh-comparison-v1
```

归档脚本必须：

- 解析并验证源、目标都位于 `E:\SPMe\outputs`；
- 拒绝源不存在、目标已存在、锁仍持有或仿真进程仍运行；
- 移动前后核对相对路径、文件大小和 SHA-256；
- 失败时保留源，不做删除或覆盖；
- 在 `E:\SPMe\outputs\archive\v2_to_v3_migration.json` 写源/目标、时间、v2 schema、最后 checkpoint、完整清单哈希和归档原因。

旧数据不转换、不追加、不删除。v3 生产目录建议固定为：

```text
E:\SPMe\outputs\pybamm_spme\w10-soh-comparison-v3-3c
```

### 11.3 切换回退

若 v3 尚未开始生产运行，回退只需保留归档并修复代码，不移动旧目录回原位。若必须恢复旧 v2 运行，需作为单独获批操作执行，且只能从 v2 自己的 schema 3 checkpoint 和原归档副本恢复，不能由 v3 loader 打开。

## 12. 分阶段实施任务

### 阶段 A：冻结基线和防回归

#### A1：创建实施基线清单

新增：

- `docs/audit/charge_efficiency_v3_baseline_manifest.json`

验证现有全量测试结果、Python/PyBaMM 版本、当前 schema、关键文件哈希和旧输出状态。只做读取和审计，不迁移输出。

#### A2：固定现有行为测试

更新/新增：

- `tests/test_regression_boundaries.py`
- `tests/test_config_and_diagnostics.py`

固定 14.55 A、4.85 Ah、3.0C、四段顺序、充电电流负号和 post-charge rest 排除规则。

阶段门槛：现有测试和新增基线测试全部通过。

### 阶段 B：配置、类型和纯公式

修改/新增：

- `src/pybamm_w10/config.py`
- `src/pybamm_w10/types.py`
- `src/pybamm_w10/charge_efficiency.py`
- `tests/test_charge_efficiency_math.py`
- `tests/test_charge_efficiency_status.py`

先实现不依赖 PyBaMM 的积分、库存差、效率、平衡和状态函数。

重点用例：恒流、CV 变流、阶段切换、已知 mol 差、镀锂正/零/负增量、效率超过 100%、分母为零、NaN、平衡通过/警告/失败。

### 阶段 C：变量解析与预检

新增/修改：

- `src/pybamm_w10/charge_variables.py`
- `src/pybamm_w10/model.py`
- `src/pybamm_w10/runner.py`
- `tests/test_charge_variable_inventory.py`
- `tests/test_charge_inventory_conversions.py`

验证当前 SPMe 变量名、表达式、单位、形状、必需性和库存换算；用 fake 缺失变量覆盖阻断路径。

阶段门槛：`--prepare` 能生成通过状态的 inventory，且不执行老化循环。

### 阶段 D：SOC 边界与机理聚合

修改/新增：

- `src/pybamm_w10/charge_efficiency.py`
- `tests/test_charge_soc_boundaries.py`
- `tests/test_charge_mechanism_aggregation.py`

覆盖精确穿越、阶段端点穿越、多次穿越、未达到上界、100% 后尾段、四 bin 连续性和空间场最小值。

阶段门槛：人工轨迹能从原始时间/电流/库存独立复算全部核心字段。

### 阶段 E：backend 和协议采样

修改/新增：

- `src/pybamm_w10/backend.py`
- `src/pybamm_w10/protocol.py`
- `tests/test_charge_backend_extraction.py`
- `tests/test_charge_protocol_capture.py`
- 更新 `tests/test_udds_and_protocol.py`

fake backend 验证四段都被捕获、静置和后续工况未被捕获、任一阶段失败时不产生分析包、RPT 后跳过产生占位包。

### 阶段 F：输出 schema 3

修改/新增：

- `src/pybamm_w10/output.py`
- `tests/test_charge_output_schema_v3.py`
- `tests/test_charge_trace_output.py`
- 更新 `tests/test_output_schema_v2.py`，将其改为“v3 拒绝旧表头/旧 checkpoint”的兼容性测试，不能直接删除历史断言。

验证固定表头、一个汇总、四行 bin、轨迹哈希、时间/电流列和批量追加前校验。

### 阶段 G：事务、checkpoint 4 和回滚

修改/新增：

- `src/pybamm_w10/types.py`
- `src/pybamm_w10/output.py`
- `src/pybamm_w10/runner.py`
- `tests/test_charge_output_transactions_v3.py`
- `tests/test_checkpoint_schema4.py`
- 更新 `tests/test_output_transactions.py`
- 更新 `tests/test_runner_checkpoint_order.py`

在轨迹写入、三个 CSV 追加、manifest 构造、checkpoint 保存和 manifest 替换各点注入失败。每个用例恢复后都必须回到同一提交前缀，无重复或半组 SOC 行。

### 阶段 H：真实 SPMe 充电 smoke

修改/新增：

- `src/pybamm_w10/smoke.py`
- `src/pybamm_w10/cli.py`
- `tests/test_charge_efficiency_smoke.py`

新增独立 `--charge-efficiency-smoke` 动作。它先在虚拟诊断分支执行 RPT node 0，取得真实 `q_ref_ah` 且不改变主状态；随后主状态从 20% 初始 SOC 执行真实四段标准 3C 充电并生成 v3 分析输出。它不执行 post-charge rest、Step 5、UDDS 或增加 aging cycle 编号，也不把虚拟 RPT 的预处理/恢复充电纳入标准充电统计。

smoke 验证：

1. 四段都成功且边界正确；
2. `Time [s]`、`Current [A]`、负极颗粒总锂可读取；
3. 一个汇总、四个 bin、一个轨迹；
4. 两个核心变量均为有限可复算值；
5. 轨迹独立复算误差满足设计阈值；
6. inventory、manifest 和 schema 4 checkpoint 指纹一致；
7. smoke 不创建正式老化循环结果。

### 阶段 I：文档和 v2 归档工具

修改/新增：

- `README.md`
- `scripts/archive_pybamm_w10_v2.ps1`
- `tests/test_v2_archive_guard.py` 或等价 PowerShell dry-run 检查

归档工具默认 dry-run，只有显式 `-Execute` 才移动目录；必须带路径范围、目标存在、进程和锁检查。

### 阶段 J：部署切换

只有 A–I 全部通过后：

1. 停止并确认旧 SPMe 运行不再活动；
2. 执行 v2 dry-run 归档并审阅清单；
3. 执行正式归档并复核哈希；
4. 在新的 v3 验证目录再运行一次 `--prepare`；
5. 检查 run config 明确是 14.55 A、名义 3.0C；
6. 生成生产启动命令供用户确认；
7. 未得到生产运行确认前不启动 350 循环。

## 13. 测试矩阵和验收门槛

### 13.1 单元测试门槛

- 外部电量：恒流解析值误差不超过 `1e-12 Ah`；分段轨迹与手算一致；不跨阶段积分。
- 法拉第换算：已知 mol 差误差不超过双精度舍入。
- SOC 边界：残差不超过 `1e-6` 个百分点。
- 效率：轨迹独立复算与对象结果差不超过 `1e-10` 个百分点。
- 外部电量复算：相对误差不超过 `1e-8`。
- 镀锂交叉校验：采用 `max(1e-8 Ah, 1e-5 × total_plating_inventory_ah)`。
- 四 bin：边界完整时连续、无重叠、无缺口，分项和与 20%–100% 汇总一致。

### 13.2 schema 门槛

- 正常标准充电：1 行汇总、4 行 SOC、1 个轨迹。
- RPT 后跳过：1 行汇总、4 行占位、0 个轨迹。
- `cycle_summary.csv` 只增加六个核心字段。
- 所有表头、顺序、空值策略和版本固定。
- schema 3 禁止向 v2 CSV 追加；schema 4 禁止加载 schema 3 checkpoint。

### 13.3 事务门槛

- 每个失败注入点恢复后无半行、无重复行、无部分四行组、无未提交正式轨迹。
- 已提交 CSV 前缀和轨迹任一字节被修改时拒绝恢复。
- 连续运行与 checkpoint 恢复运行的相同循环逐字段一致，轨迹哈希一致。
- manifest 的三个循环边界一致，artifact 元数据与实际文件一致。

### 13.4 全量门槛命令

目标测试通过后运行：

```powershell
Set-Location -LiteralPath E:\SPMe
C:\Users\Lenovo\anaconda3\envs\battery\python.exe -m pytest -q -p no:cacheprovider
```

然后执行无老化 prepare：

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe scripts\run_pybamm_w10.py `
  --workspace E:\SPMe `
  --data-root E:\battery\data `
  --prepare
```

最后执行独立充电效率 smoke，输出目录不得与生产目录相同：

```powershell
C:\Users\Lenovo\anaconda3\envs\battery\python.exe scripts\run_pybamm_w10.py `
  --workspace E:\SPMe `
  --data-root E:\battery\data `
  --mode virtual `
  --calibration-params E:\SPMe\inputs\spme_transferred_parameters.json `
  --charge-efficiency-smoke `
  --output-dir E:\SPMe\outputs\pybamm_spme\charge-efficiency-v3-smoke
```

smoke 失败时停止在此，不归档 v2，不生成生产启动命令。

## 14. 独立复核脚本

实施时新增一个测试用只读复核入口，可放在 `tests/helpers/charge_efficiency_recalculator.py`，不得依赖生产计算器内部函数。它只读取：

- `charge_timeseries/cycle-XXX.csv`
- `charge_efficiency_summary.csv`
- `charge_efficiency_soc_bins.csv`

独立完成：

- `time_s/current_a` 外部电量积分；
- 负极颗粒锂首尾差换算；
- 两个效率复算；
- 四 bin 合计与连续性检查；
- 轨迹 SHA-256 与行定位检查。

必须使用独立实现，避免生产公式和测试公式共享同一个错误。

## 15. 实施完成定义

以下条件全部满足才可以宣布代码实施完成：

1. 新增模块职责清晰，生产计算器不直接访问文件或启动求解；
2. 当前模型的核心和必需机理变量全部通过预检；
3. 四段充电边界与排除项均由测试固定；
4. 全窗口和四个 SOC 区间都有可复算的外部电量与正常嵌锂增量；
5. `charge_timeseries` 明确输出 `time_s` 和 `current_a`；
6. 两种效率、四类库存和电量平衡按批准公式输出且不裁剪；
7. 正常循环、跳过充电、边界未达到、非单调 SOC、旧镀锂释放和缺失变量路径均被覆盖；
8. schema 3、checkpoint 4、manifest 和回滚故障注入全部通过；
9. 全量测试、prepare 和真实 3C 充电 smoke 全部通过；
10. 未自动执行其他倍率，也未自动启动生产 350 循环；
11. v2 仅在最终切换门槛后完整归档，迁移审计哈希一致；
12. 生成实施报告，列出改动文件、测试结果、smoke 数值摘要、输出样例、已知 SPMe 局限和生产启动命令。

## 16. 推荐实施顺序结论

推荐按 `纯公式 -> 变量预检 -> SOC 边界 -> backend/协议采样 -> 输出 -> 事务恢复 -> 真实 smoke -> v2 归档切换` 的顺序执行。该顺序把最容易独立验证的科学计算放在前面，把具有外部状态变化的停机、归档和生产启动放在最后，任何阶段失败都能停在明确门槛，不会破坏旧结果。

实施后，真正充电效率的两个核心量将分别来自：

```text
分母 = 四个充电段内 integral(max(-Current [A], 0), Time [s]) / 3600

分子 = F / 3600
       * (Total lithium in negative electrode [mol] 终点 - 起点)
```

并且二者的原始来源会同时保存在逐循环充电轨迹和汇总 CSV 中，可在 PyBaMM 之外独立复算与审计。
