# SPMe 充电效率与 SOC 分区分析设计规范（第二版）

- 文档日期：2026-08-21
- 适用项目：`E:\SPMe`
- 当前运行范围：单一 3C 工况
- 目标输出模式：v3
- 文档状态：设计已逐节确认，待用户终审
- 本文性质：设计规范；不包含代码修改或实施计划

## 1. 目标与范围

本设计在现有 PyBaMM SPMe 老化仿真中增加可审计的充电效率、SOC 分区效率和机理量输出。当前只执行一个 3C 充电工况；0.5C、1C、2C 等倍率暂不自动批量运行，后续由用户手动修改第一段恒流电流。设计仍保留统一的倍率上下文字段，使不同手动运行结果可以在未来进行同口径比较。

一次完整充电严格由以下四段组成：

1. `3c_cc`
2. `4v_cv`
3. `c4_cc`
4. `4p2v_cv`

充电统计边界从 `3c_cc` 开始前的状态，到 `4p2v_cv` 结束时的状态。充电后静置、Step 5、UDDS 及其回馈电流、RPT、RPT 后恢复充电均不计入本次充电。

本设计输出两个不同含义的指标：

1. `useful_charge_efficiency_pct`：负极活性材料颗粒中正常可逆嵌锂的增加量与外部充入电量之比。
2. `reversible_retention_pct`：正常可逆嵌锂增加量与可逆镀锂增加量之和，与外部充入电量之比。

SEI、死锂、可逆镀锂和电量平衡误差分别输出，不能合并成一个不可追溯的“损失”。所有效率原始值均保留，不裁剪到 0%–100%。

## 2. 当前模型能力与结论边界

当前项目采用 PyBaMM 26.7.1 的 SPMe、OKane2022 参数集，并启用了集总热模型、SEI、部分可逆镀锂、颗粒开裂、应力驱动 LAM 等选项。现有模型能够直接或通过明确的状态差分得到以下核心量：

- `Time [s]`
- `Current [A]`
- `Total lithium in negative electrode [mol]`
- 负极表面/平均化学计量比
- 电解液浓度场
- 负极反应过电位
- 镀锂反应过电位
- 总镀锂、死锂、SEI 相关累计容量或等价状态量
- LLI、正负极 LAM
- 反应电流密度、电解液欧姆损失、浓差过电位和发热项

因此，计算充电效率所需的两个核心变量可以得到：

- 分母“外部充入电量”由四段内的 `Time [s]` 与 `Current [A]` 积分得到。
- 分子“电芯内部实际增加的正常可逆嵌锂量”由四段首尾的 `Total lithium in negative electrode [mol]` 差值换算得到。

这里的负极总锂是活性材料颗粒中的锂库存，不把镀在颗粒表面的金属锂、死锂和 SEI 消耗计入正常嵌锂。该定义与用户确认的效率物理含义一致。

模型结论必须标注为“SPMe 预测的趋势”，不能直接表述为实验已经验证的真实局部机理。SPMe 的主要限制包括：

- 颗粒梯度是代表性颗粒径向梯度，不是电极厚度方向的完整固相非均匀性。
- 集总热模型能够给出平均温度和温升，但不能解析电芯内部热点。
- 3C 高倍率下的局部传质与极化精度通常弱于 DFN。
- 在没有多倍率实验数据和参数再标定时，只能做模型内一致的倍率/SOC 比较。

## 3. 总体架构

### 3.1 输出分层

v3 输出由四层组成：

1. `cycle_summary.csv`：每循环一行，只保留核心效率、倍率上下文和有效性状态。
2. `charge_efficiency_summary.csv`：每个完整标准充电一行，保存全充电窗口的详细账本和诊断量。
3. `charge_efficiency_soc_bins.csv`：每个循环固定四行，分别代表 20%–40%、40%–60%、60%–80%、80%–100%。
4. `charge_timeseries/cycle-XXX.csv`：每个有效标准充电一个不可变原始轨迹文件，保存四段充电的时间、电流及效率复算所需库存量。

`charge_rate_soc_comparison.csv` 定义为未来跨运行汇总产物，不在当前单次 3C 运行内自动生成。后续手动运行不同倍率后，由独立比较流程读取各个 v3 运行目录生成，避免把跨运行派生结果混入某个单运行事务。

### 3.2 核心组件

#### ChargeStateSnapshot

在充电窗口起点、终点和 SOC 边界保存可复算状态，包括时间、参考 SOC、温度、负极颗粒总锂、总镀锂、死锂、可逆镀锂、SEI、化学计量比、电解液浓度、反应过电位、LLI/LAM、状态来源和哈希。

#### ChargeStageMeasurement

分别记录四个充电段的起止时间、外部充入电量、电压/温度统计、终止原因。每段独立积分，禁止跨越阶段切换点做一个梯形积分，以免在电流不连续处引入假电量。

#### SocBoundaryLocator

以 PyBaMM 连续解为基础，在 40%、60%、80% 和 100% 两侧求解点之间进行括区和求根，并把精确边界插入原始轨迹及积分节点。禁止用最近求解点代替边界。

#### ChargeEfficiencyCalculator

使用已抽取的轨迹和快照进行纯计算，不在计算器内部再次查询 PyBaMM。它负责全窗口与各 SOC 区间的电量、效率、电量平衡、温度、极化、传质和机理指标。

#### OutputTransactionCoordinator

统一管理 CSV 追加、轨迹文件、manifest、checkpoint、失败恢复和回滚。只有一个循环的全部输出校验通过后，才能提交该循环。

## 4. 数据流与统计边界

每个普通循环按以下数据流执行：

1. 在 `3c_cc` 开始前获取充电起点快照，并将该时刻定义为本次参考 SOC 的 20% 锚点。
2. 依次运行 `3c_cc`、`4v_cv`、`c4_cc`、`4p2v_cv`，分别保存阶段标识、连续解和积分所需数据。
3. 在 `4p2v_cv` 结束后立即获取终点快照；此后才允许进入充电后静置或其他工况。
4. 根据冻结的 `q_ref_ah` 计算参考 SOC 轨迹。
5. 精确定位 40%、60%、80%、100% 边界，并插入轨迹。
6. 计算全窗口汇总和四个 SOC 区间结果。
7. 先在内存和临时文件中完成校验，再将轨迹、两个新增 CSV、`cycle_summary.csv` 及现有循环输出作为同一事务提交。

若 strict-w10 流程在 RPT 后通过恢复充电已经使电芯处于充满状态，从而跳过标准四段充电，则：

- `charge_efficiency_summary.csv` 写一行状态记录；
- `charge_efficiency_soc_bins.csv` 写四行占位记录；
- `primary_status=STANDARD_CHARGE_SKIPPED_AFTER_RPT`；
- 不生成充电轨迹；
- 不纳入效率或机理比较。

## 5. 充电倍率上下文

当前 3C 配置为：

- `configured_charge_current_a = 14.55`
- `nominal_capacity_ah = 4.85`
- `configured_nominal_charge_rate_c = 14.55 / 4.85 = 3.0`
- `effective_charge_rate_c = 14.55 / q_ref_ah`

正式记录以下字段：

- `configured_charge_current_a`
- `configured_nominal_charge_rate_c`
- `effective_charge_rate_c`
- `nominal_capacity_ah`
- `q_ref_ah`
- `q_ref_node`

公式为：

```text
configured_nominal_charge_rate_c
    = abs(configured_charge_current_a) / nominal_capacity_ah

effective_charge_rate_c
    = abs(configured_charge_current_a) / q_ref_ah
```

倍率字段描述第一段 `3c_cc` 的配置。后续三段协议参数保持现有设置。用户未来只需手动修改第一段恒流值，系统必须自动计算并记录新的名义倍率和有效倍率，不依赖文件名推断倍率。

## 6. 计算公式

### 6.1 外部充入电量

项目当前充电电流采用负号约定，因此每段外部充入电量为：

```text
Q_stage_ah = (1 / 3600) * integral(max(-I(t), 0), dt)
```

全充电窗口：

```text
external_charge_ah
    = Q_3c_cc + Q_4v_cv + Q_c4_cc + Q_4p2v_cv

cc_charge_ah = Q_3c_cc + Q_c4_cc
cv_charge_ah = Q_4v_cv + Q_4p2v_cv

cv_charge_fraction_pct
    = 100 * cv_charge_ah / external_charge_ah
```

每段按其自己的时间网格积分，并在 SOC 精确边界处拆分积分。

### 6.2 正常可逆嵌锂增加量

令 `n_neg_start` 和 `n_neg_end` 分别为区间首尾的 `Total lithium in negative electrode [mol]`：

```text
intercalated_charge_increment_ah
    = F / 3600 * (n_neg_end - n_neg_start)
```

其中：

```text
F = 96485.33212 C/mol
```

必须同时输出：

- `negative_particle_lithium_mol_start`
- `negative_particle_lithium_mol_end`
- `faraday_constant_c_per_mol`
- `intercalated_charge_increment_ah`

不得用 `SOC × nominal_capacity`、放电容量、全电芯总锂、镀锂容量或 SEI 容量代替负极颗粒锂差值。

### 6.3 可逆镀锂、死锂与 SEI

PyBaMM 的负极镀锂容量损失变量包含可逆镀锂与死锂，因此主计算采用：

```text
reversible_plating_inventory_ah
    = total_plating_inventory_ah - dead_lithium_inventory_ah

reversible_plating_increment_ah
    = reversible_plating_inventory_end_ah
    - reversible_plating_inventory_start_ah
```

该增量允许为负，表示本次充电窗口中释放了充电前已经存在的可逆镀锂。为避免把负增量误读为新增损失，同时输出：

```text
reversible_plating_depletion_ah
    = max(-reversible_plating_increment_ah, 0)
```

通过浓度乘负极几何体积得到的库存只作为交叉校验，不作为主值。输出交叉校验值、误差和状态。

死锂和 SEI 均按累计库存的区间终值减起值计算。SEI 为模型中所有启用的负极 SEI 与裂纹 SEI 贡献之和；若某个正极项在当前模型中不存在，则由预检清单明确标记为“不适用”，不得静默补零。

### 6.4 两个效率指标

```text
useful_charge_efficiency_pct
    = 100 * intercalated_charge_increment_ah
    / external_charge_ah

reversible_retention_pct
    = 100 * (intercalated_charge_increment_ah
             + reversible_plating_increment_ah)
    / external_charge_ah
```

因此，用户指定的公式可以用于本项目：

```text
真实充电效率
    = 电芯内部正常可逆嵌锂增加量 / 外部充入电量 * 100%
```

前提是分子严格使用负极活性材料颗粒锂库存差值，分母严格使用四段充电电流积分。

### 6.5 电量平衡

```text
accounted_charge_ah
    = intercalated_charge_increment_ah
    + reversible_plating_increment_ah
    + dead_lithium_increment_ah
    + sei_increment_ah

charge_balance_error_ah
    = external_charge_ah - accounted_charge_ah

charge_balance_error_pct
    = 100 * charge_balance_error_ah / external_charge_ah

charge_balance_abs_error_pct
    = abs(charge_balance_error_pct)
```

默认判定阈值纳入运行配置和配置指纹：

- `<= 0.2%`：通过；
- `(0.2%, 1.0%]`：警告；
- `> 1.0%`：失败。

若后续发现模型还有不可忽略但未入账的锂库存项，应显式增加账本科目和版本号，不允许通过放宽阈值掩盖。

## 7. SOC 定义与固定区间

### 7.1 可审计参考 SOC

本设计使用基于负极颗粒锂变化量的参考 SOC，不把端电压当作 SOC：

```text
Q_neg(t)_ah = F / 3600 * n_neg(t)

reference_soc_pct(t)
    = 20
    + 100 * (Q_neg(t)_ah - Q_neg(t0)_ah) / q_ref_ah
```

其中：

- `t0` 是 `3c_cc` 开始前的充电起点；
- `q_ref_ah` 是最新一个成功 RPT 节点得到的参考容量；
- 同一 RPT 节点与下一节点之间，`q_ref_ah` 冻结不变；
- `soc_definition = NEGATIVE_PARTICLE_LITHIUM_DELTA_OVER_FROZEN_Q_REF_V1`。

同时记录：

- `soc_start_pct`
- `soc_end_pct`
- `soc_definition`
- `soc_reference_capacity_ah`
- `capacity_reference_node`
- `soc_anchor_pct`
- `soc_anchor_source`
- `soc_anchor_validation_status`

正常锚点为 20%。首个窗口的来源为 `INITIAL_SOC_CONFIGURATION`；后续正常窗口来源为 `W10_80_PERCENT_DISCHARGE_WINDOW`。若前序放电窗口或锚点验证失败，则标记 `SOC_ANCHOR_INVALID`，不把结果当作有效分区效率。

### 7.2 区间与边界插值

每个标准充电固定输出：

- 20%–40%
- 40%–60%
- 60%–80%
- 80%–100%

边界求解流程为：

1. 在连续参考 SOC 轨迹中查找包围目标 SOC 的相邻时刻；
2. 使用 PyBaMM 连续解进行括区求根；
3. 将精确边界时刻插入轨迹；
4. 对所有积分量在该时刻拆分；
5. 验证边界 SOC 残差不超过 `1e-6` 个百分点。

若某一上边界未达到，仍输出对应占位行，状态为 `SOC_UPPER_BOUND_NOT_REACHED`，并记录实际覆盖的起止 SOC 与覆盖率。若 SOC 非单调幅度超过 `1e-6` 个百分点，标记 `NON_MONOTONIC_SOC`；边界使用“第一次向上穿越”的确定性规则，并记录穿越次数和选择规则。

若在第四段结束前已穿过 100%，四个分区只统计至 100%。100% 之后到充电结束的尾段只进入全窗口汇总，并记录：

- `soc_at_charge_end_pct`
- `post_100_charge_ah`
- `post_100_duration_s`

## 8. 字段模式

### 8.1 `cycle_summary.csv` 新增核心字段

- `configured_nominal_charge_rate_c`
- `effective_charge_rate_c`
- `useful_charge_efficiency_pct`
- `reversible_retention_pct`
- `charge_efficiency_status`
- `complete_soc_bin_count`

详细库存、分项损失和诊断字段只进入专用文件，避免继续扩张循环主表。

### 8.2 `charge_efficiency_summary.csv`

每个循环/标准充电一行，至少包含：

#### 标识与倍率

- `cycle`
- `mode`
- `configured_charge_current_a`
- `configured_nominal_charge_rate_c`
- `effective_charge_rate_c`
- `nominal_capacity_ah`
- `q_ref_ah`
- `q_ref_node`
- `soh_pct`

#### SOC 与窗口

- `soc_start_pct`
- `soc_at_charge_end_pct`
- `soc_definition`
- `soc_reference_capacity_ah`
- `capacity_reference_node`
- `soc_anchor_pct`
- `soc_anchor_source`
- `soc_anchor_validation_status`
- `time_start_s`
- `time_end_s`
- `duration_s`
- `post_100_charge_ah`
- `post_100_duration_s`

#### 电量、库存与效率

- `external_charge_ah`
- `cc_charge_ah`
- `cv_charge_ah`
- `cv_charge_fraction_pct`
- `negative_particle_lithium_mol_start`
- `negative_particle_lithium_mol_end`
- `faraday_constant_c_per_mol`
- `intercalated_charge_increment_ah`
- `total_plating_inventory_start_ah`
- `total_plating_inventory_end_ah`
- `reversible_plating_inventory_start_ah`
- `reversible_plating_inventory_end_ah`
- `reversible_plating_increment_ah`
- `reversible_plating_depletion_ah`
- `dead_lithium_inventory_start_ah`
- `dead_lithium_inventory_end_ah`
- `dead_lithium_increment_ah`
- `sei_inventory_start_ah`
- `sei_inventory_end_ah`
- `sei_increment_ah`
- `useful_charge_efficiency_pct`
- `reversible_retention_pct`
- `accounted_charge_ah`
- `charge_balance_error_ah`
- `charge_balance_error_pct`
- `charge_balance_abs_error_pct`
- `charge_balance_status`

#### 交叉校验与可追溯性

- `reversible_plating_inventory_crosscheck_ah`
- `reversible_plating_crosscheck_error_ah`
- `reversible_plating_crosscheck_status`
- `charge_trace_path`
- `charge_trace_sha256`
- `charge_trace_row_count`
- `charge_integration_method`
- `charge_integration_point_count`
- `primary_status`
- `status_flags`
- `is_valid_for_efficiency_analysis`
- `is_valid_for_mechanism_analysis`

### 8.3 `charge_efficiency_soc_bins.csv`

每个“循环 × 倍率上下文 × SOC 区间”一行。每个正常标准充电必须恰好四行。

#### 标识、SOC 和老化上下文

- `cycle`
- `mode`
- `soc_bin_id`
- `soc_start_pct`
- `soc_end_pct`
- `actual_soc_start_pct`
- `actual_soc_end_pct`
- `soc_coverage_pct`
- `soc_definition`
- `soc_reference_capacity_ah`
- `capacity_reference_node`
- `soc_anchor_pct`
- `soc_anchor_source`
- `configured_charge_current_a`
- `configured_nominal_charge_rate_c`
- `effective_charge_rate_c`
- `nominal_capacity_ah`
- `soh_pct`
- `q_ref_ah`
- `q_ref_node`
- `lli_pct`
- `negative_lam_pct`
- `positive_lam_pct`

LLI、LAM 和 SOH 使用区间起点状态；`soh_pct = 100 * q_ref_ah / q_ref_initial_ah`。

#### 电量、库存和效率

- `external_charge_ah`
- `negative_particle_lithium_mol_start`
- `negative_particle_lithium_mol_end`
- `intercalated_charge_increment_ah`
- `total_plating_inventory_start_ah`
- `total_plating_inventory_end_ah`
- `reversible_plating_inventory_start_ah`
- `reversible_plating_inventory_end_ah`
- `reversible_plating_increment_ah`
- `reversible_plating_depletion_ah`
- `dead_lithium_inventory_start_ah`
- `dead_lithium_inventory_end_ah`
- `dead_lithium_increment_ah`
- `sei_inventory_start_ah`
- `sei_inventory_end_ah`
- `sei_increment_ah`
- `useful_charge_efficiency_pct`
- `reversible_retention_pct`
- `charge_balance_error_ah`
- `charge_balance_error_pct`
- `charge_balance_abs_error_pct`
- `charge_balance_status`

#### 充电过程

- `time_start_s`
- `time_end_s`
- `duration_s`
- `cc_charge_ah`
- `cv_charge_ah`
- `cv_charge_fraction_pct`
- `mean_current_a`
- `mean_voltage_v`
- `maximum_voltage_v`

`mean_current_a` 定义为充电电流幅值 `abs(I)` 的时间加权平均；电压均值为时间加权平均。

#### 温度

- `temperature_start_k`
- `temperature_end_k`
- `temperature_mean_k`
- `temperature_max_k`
- `temperature_rise_k`

#### 极化、传质和机理

- `negative_surface_stoichiometry`
- `negative_average_stoichiometry`
- `negative_particle_radial_stoichiometry_gradient`
- `minimum_electrolyte_concentration_mol_m3`
- `negative_reaction_overpotential_v`
- `plating_reaction_overpotential_v`
- `negative_intercalation_current_density_mean_a_m2`
- `negative_intercalation_current_density_max_a_m2`
- `negative_plating_current_density_mean_a_m2`
- `negative_plating_current_density_extreme_a_m2`
- `negative_sei_current_density_mean_a_m2`
- `negative_sei_current_density_max_a_m2`
- `electrolyte_ohmic_loss_mean_v`
- `electrolyte_ohmic_loss_max_v`
- `negative_particle_concentration_overpotential_mean_v`
- `negative_particle_concentration_overpotential_max_v`
- `irreversible_heating_energy_wh`
- `ohmic_heating_energy_wh`
- `reversible_heating_energy_wh`
- `total_heating_energy_wh`

其中：

- 两个负极化学计量比取区间终点；
- 径向梯度定义为区间终点的“表面值减体积平均值”；
- 最低电解液浓度取该区间所有空间位置和时刻的最小值；
- 负极反应过电位取时间加权平均；
- 镀锂过电位保存最不利极值，并明确沿用 PyBaMM 原始符号；
- 发热能量对区间内相应功率积分后换算为 Wh。

#### 状态与轨迹定位

- `soc_crossing_count`
- `soc_crossing_selection_rule`
- `charge_trace_path`
- `trace_start_row`
- `trace_end_row`
- `trace_start_time_s`
- `trace_end_time_s`
- `primary_status`
- `status_flags`
- `is_valid_for_efficiency_analysis`
- `is_valid_for_mechanism_analysis`

### 8.4 `charge_timeseries/cycle-XXX.csv`

当前项目将明确输出 `Time [s]` 和 `Current [A]` 对应的 CSV 数据。每个有效标准充电创建一个文件，且只包含四段充电窗口：

- `cycle`
- `charge_stage`
- `time_s`
- `current_a`
- `terminal_voltage_v`
- `temperature_k`
- `reference_soc_pct`
- `cumulative_external_charge_ah`
- `negative_particle_lithium_mol`
- `total_plating_inventory_ah`
- `dead_lithium_inventory_ah`
- `reversible_plating_inventory_ah`
- `cumulative_sei_loss_ah`
- `soc_boundary`

字段映射为：

- `time_s` ← PyBaMM `Time [s]`
- `current_a` ← PyBaMM `Current [A]`

轨迹必须包含每个阶段的首尾点，以及插值求得的 20%、40%、60%、80%、100% 精确边界行。`soc_boundary` 在普通行为空，在边界行写相应百分数。

## 9. 状态与异常处理

每行专用输出包含：

- `primary_status`
- `status_flags`
- `is_valid_for_efficiency_analysis`
- `is_valid_for_mechanism_analysis`

`status_flags` 允许多个标志；`primary_status` 按“核心数据错误 > SOC 错误 > 电量平衡错误 > 交叉校验错误 > 警告 > 有效”选取最高优先级。

标准状态包括：

- `VALID`
- `BALANCE_WARNING`
- `CHARGE_BALANCE_FAILURE`
- `PREEXISTING_PLATED_LITHIUM_RELEASED`
- `SOC_ANCHOR_INVALID`
- `SOC_UPPER_BOUND_NOT_REACHED`
- `NON_MONOTONIC_SOC`
- `INVALID_EXTERNAL_CHARGE`
- `INVALID_INTERCALATED_CHARGE`
- `MISSING_MODEL_VARIABLE`
- `PLATING_INVENTORY_CROSSCHECK_FAILURE`
- `STANDARD_CHARGE_SKIPPED_AFTER_RPT`
- `CHARGE_EFFICIENCY_CORE_FAILURE`

具体规则：

- 分母非正或非有限：`INVALID_EXTERNAL_CHARGE`。
- 正常嵌锂增量非有限或显著为负：`INVALID_INTERCALATED_CHARGE`。
- 效率超过 100% 且可逆镀锂库存下降：保留数值并标记 `PREEXISTING_PLATED_LITHIUM_RELEASED`，表示旧镀锂释放可能补充了颗粒嵌锂。
- 效率超过 100% 且没有可解释的可逆镀锂释放：标记 `CHARGE_BALANCE_FAILURE`。
- 核心变量缺失不能用空值继续计算；机理扩展变量缺失是否阻断由预检中的 `required` 明确定义。

运行前生成 `charge_efficiency_variable_inventory.json`，逐字段记录：

- PyBaMM 变量名；
- 单位；
- 标量或空间场形状；
- 聚合方法；
- 是否必需；
- 当前模型是否可用。

任何核心效率变量或被定义为必需的机理变量缺失，必须在循环 1 前阻止老化运行。运行中出现时间序列为空、时间倒退、四段缺失、NaN、负极颗粒总锂不可读取、轨迹哈希或输出前缀不一致等问题时，当前循环事务失败并恢复至最后一个 checkpoint。

科学有效性异常（例如某 SOC 上界未达到或平衡警告）可以作为带状态的数据提交并继续运行，但必须从默认效率/机理比较中排除。

## 10. v3 事务、manifest、checkpoint 与回滚

### 10.1 版本

- `output_schema_version = 3`
- `checkpoint_schema_version = 4`
- `protocol_algorithm_version = w10-window-v3-charge-efficiency`

以上版本进入运行配置、checkpoint 和配置指纹。

### 10.2 提交顺序

单循环事务顺序为：

1. 在内存计算本循环所有汇总与四个 SOC 行；
2. 写临时轨迹并校验行数、时间单调性、积分值和哈希；
3. 原子重命名轨迹为最终不可变文件；
4. 追加 `cycle_summary.csv`、现有退化表、`charge_efficiency_summary.csv`、四行 `charge_efficiency_soc_bins.csv` 及本循环其他输出；
5. 刷新文件缓冲；
6. 构造新 manifest；
7. 保存 checkpoint；
8. 原子替换 manifest，完成提交。

manifest 将新增或更新：

- `last_completed_cycle`
- `last_charge_efficiency_cycle`
- `last_complete_soc_bin_cycle`
- `last_rpt_node`
- `output_schema_version`

普通有效循环中，前三个循环编号必须相同。每个轨迹 artifact 在 manifest 中保存相对路径、大小、SHA-256、循环、行数和起止时间。

### 10.3 回滚

恢复时必须验证所有 append-only CSV 的已提交前缀与轨迹 artifact：

- 截断 manifest 之后的未提交 CSV 尾部；
- 将未提交轨迹移动到隔离目录，不参与恢复；
- 拒绝从部分 SOC 行或只有汇总没有轨迹的状态继续；
- 失败 JSON 写入循环、充电段、SOC 区间、状态、异常和最后 checkpoint 信息；
- 失败轨迹仅用于取证，不能被当成正式结果。

## 11. v2 到 v3 的输出迁移策略

现有未完成的 v2 输出不做原位修改，也不转换成 v3，因为旧结果缺少负极颗粒锂首尾状态、SOC 精确边界和新版事务元数据，转换会制造无法审计的伪精度。

设计采用归档替换：

1. 将旧运行目录完整移动到 `E:\SPMe\outputs\archive\v2\w10-soh-comparison-v1`；
2. 在归档根目录外写 `v2_to_v3_migration.json`，记录源路径、归档路径、时间、旧 schema、文件哈希和原因；
3. 使用新的 v3 运行目录重新开始；
4. v3 明确拒绝载入 v2 checkpoint；
5. 不删除旧数据。

该迁移属于后续实施阶段，本设计阶段不执行。

## 12. 未来跨倍率 × SOC 比较

当前只跑 3C，因此不自动生成跨运行比较文件。用户后续分别手动运行 0.5C、1C、2C、3C 后，可生成 `charge_rate_soc_comparison.csv`，一行代表一个“运行 × 节点/循环 × 倍率 × SOC 区间”。

比较前必须匹配或显式分层以下控制条件：

- 固定 SOC 区间；
- SOH 或相同循环/RPT 节点；
- 环境温度；
- 充电起始温度；
- 电芯参数与配置指纹；
- 截止电压；
- CV 截止电流；
- 充电前静置条件；
- 老化参数；
- SOC 定义和参考容量节点。

二维结果至少比较：

- 有效充电效率；
- 总可逆留存率；
- SEI 增量；
- 可逆镀锂增量；
- 死锂增量；
- 温升；
- CV 电量占比；
- 负极颗粒径向化学计量比梯度；
- 负极反应与镀锂过电位。

若控制条件不匹配，比较行必须标记 `CONTROL_CONDITION_MISMATCH`，不能默认为可比。倍率使用 CSV 正式字段，不从目录名或自由文本解析。

## 13. 验证与验收标准

### 13.1 单元验证

- 以人工构造的恒流、分段电流和带切换点轨迹验证外部电量积分。
- 以已知摩尔差验证法拉第换算。
- 验证总镀锂减死锂得到可逆镀锂，并覆盖正增量、零增量和负增量。
- 验证两种效率、电量平衡和所有状态分支。
- 验证 SOC 40%、60%、80%、100% 插值、首次向上穿越、未达到上界和非单调路径。
- 验证四个区间之和与 20%–100% 汇总一致。

### 13.2 输出与事务验证

- 每个正常标准充电恰好生成 1 行汇总、4 行 SOC 分区和 1 个轨迹文件。
- `charge_timeseries` 明确包含 `time_s` 与 `current_a`。
- CSV 列名、单位、顺序、空值策略和 schema 版本固定。
- manifest 中轨迹大小和 SHA-256 与文件一致。
- 在提交的每一步注入失败，恢复后不得出现半行、重复行、只有部分 SOC 行或无 manifest 的正式轨迹。
- 重启后相同循环结果与连续运行逐字段一致。

### 13.3 数值验收

- 由原始轨迹独立复算两种效率，与汇总差值不超过 `1e-10` 个百分点。
- 由 `time_s/current_a` 独立复算外部电量，与汇总相对误差不超过 `1e-8`。
- SOC 边界残差不超过 `1e-6` 个百分点。
- 可逆镀锂主值与浓度几何交叉校验误差不超过 `max(1e-8 Ah, 1e-5 × total_plating_inventory_ah)`；超限即标记失败。
- 四个 SOC 区间在边界完整时应连续、无重叠、无缺口。
- 20%–100% 各分项之和与相应区间汇总满足相同的浮点误差标准。

### 13.4 当前 3C 冒烟验收

使用缩短的真实 SPMe 3C 流程至少证明：

1. 四个充电段被正确识别；
2. 起止负极颗粒锂和 `Time [s]`、`Current [A]` 均能读取；
3. 外部充入电量与正常嵌锂增加量均为可复算数值；
4. 四个 SOC 区间和精确边界可生成；
5. 两个效率不依赖 SOC×容量近似；
6. 镀锂、死锂、SEI 与平衡误差可分别追踪；
7. 一个完整事务可提交，并能从 checkpoint 无重复地恢复。

### 13.5 分析能力验收

当前单一 3C 结果必须能回答：

- 完整充电的正常可逆嵌锂效率是多少；
- 20%–40%、40%–60%、60%–80%、80%–100% 哪一区间效率最低；
- 各区间的可逆镀锂、死锂和 SEI 如何分配；
- 高 SOC 区域是否伴随 CV 占比、温升、颗粒梯度或过电位上升；
- 随循环/SOH 变化，效率下降由哪类模型内机制共同出现；
- 任一汇总值是否能回溯到明确的原始时间、电流和库存状态。

未来加入不同手动倍率运行后，在控制条件匹配的前提下，以上问题必须能够扩展为“充电倍率 × SOC 区间”的二维比较。

## 14. 明确不在本轮实施的内容

- 不修改任何 Python 源代码、配置、协议或现有输出。
- 不停止或启动仿真进程。
- 不生成实施计划。
- 不自动批量运行 0.5C、1C、2C、3C。
- 不生成当前没有输入数据的 `charge_rate_soc_comparison.csv`。
- 不把 SPMe 机理结论表述为实验真值，也不在本设计内迁移至 DFN。

## 15. 最终设计结论

在上述定义、字段、原始轨迹、SOC 边界、库存账本和事务约束全部实现并通过验收后，当前 SPMe 模型能够完整支撑单次 3C 工况的充电效率计算，以及较完整的“3C × SOC 区间”模型机理分析。它也为未来由用户手动修改倍率后的跨运行比较提供统一数据接口。

最核心的效率公式已经具备可获取且物理口径一致的两个变量：

```text
useful_charge_efficiency_pct
    = 100
    * [F / 3600 * (负极颗粒锂终点 mol - 起点 mol)]
    / [1 / 3600 * integral(max(-Current [A], 0), Time [s])]
```

同时，v3 将输出包含 `time_s` 和 `current_a` 的逐循环充电轨迹 CSV，因此外部充入电量和最终效率可在 PyBaMM 之外独立复核。
