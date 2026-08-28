# Stage 1 SOH 标定最新报错根因复核报告

- 报告日期：2026-08-28（Asia/Shanghai）
- 复核对象：`outputs/pybamm_spme_calibration/w10-stage1-soh-v1`
- 最新终止时间：2026-08-28 03:11:18（本地时间）
- 最新业务终态：`CALIBRATION_FAILED`
- 复核性质：只读取证与隔离求解诊断；未修改模型、协议、候选参数或正式输出

## 1. 结论摘要

这次并不是一个偶发、随机、文件损坏或“程序无缘无故崩溃”的错误。当前 Stage 1 反复失败由两个独立问题叠加造成。

### 1.1 最新 A/B 组合候选的主失败

最新候选 B 在完成 22 个 aging cycles 后，于 cycle 23 的标准充电第二段 `4v_cv` 中触发：

```text
IDA_ERR_FAIL: Error test failures occurred too many times during one step
or minimum step size was reached
```

首次档 `certified_charge` 失败后，程序已从同一个充电前快照完整回滚，并用 `certified_charge_retry` 重算；第二档仍在同一阶段失败。候选 A 则在 cycle 1 的同一 `4v_cv` 阶段失败。

直接数值原因已经进一步缩小到：**4.0 V 恒压约 100 s 后，负极局部电解液浓度长期贴近零；接近 CV 小电流截止区时，镀锂/剥离与嵌锂电流出现强烈反向抵消，使恒压代数约束落入病态分支，最终迫使 IDA 步长缩到机器精度附近。** BDF 阶数决定求解器在哪一时刻失稳，但不是最底层物理—数值触发源。

| cycle 23 隔离诊断档 | 结果 |
|---|---|
| 生产首次档：BDF 3，`max_error_test_failures=30` | `IDA_ERR_FAIL` |
| 生产重试档：BDF 2，`max_error_test_failures=100` | `IDA_ERR_FAIL` |
| BDF 2，失败额度提高到 200 | 仍为 `IDA_ERR_FAIL` |
| 更严格容差与 `dt_max=0.1 s`，BDF 3/2 | 仍为 `IDA_ERR_FAIL` |
| 诊断档：BDF 1，`max_error_test_failures=100` | 四段充电完成，但轨迹与 BDF 2/3 明显分叉，不能作为精度等价解 |

BDF 1 诊断档完成了 `VOLTAGE → CURRENT → VOLTAGE → CURRENT` 四个预期终止，重复运行也得到相同阶段时长和终端状态哈希；但深入比较表明，它在约 100 s 后逐渐走上与 BDF 2/3 不同的数值分支。到 CV 约 501.2 s 时，BDF 1 仍给出约 `-10.09 A`，而 BDF 2 已降到约 `-0.111 A`。所以 BDF 1 的“能跑完”只证明存在一条数值可积分轨迹，不能证明它与生产档代表同一个可信解。

模型没有注册“电解液浓度降至零”的终止事件，因此这里没有一个被框架正式记录的物理 cutoff；但是 BDF 2/3 的负极局部电解液浓度已经降至 `10^-5–10^-13 mol.m^-3` 量级，实质上处于模型方程的奇异边界附近。单纯增加失败次数额度、缩小最大步长或收紧容差不能解决这类病态性。

因此，不能把生产重试直接改成 BDF 1。新的轨迹对比已经表明 BDF 1 不满足“与 BDF 2/3 精度等价”的前提，直接启用会改变正式科学结果，而不只是提高鲁棒性。

### 1.2 为什么总会把计算推到这个不稳定区

根本原因位于标定候选生成层：实验 SOH 的早期衰减明显强于当前三个单因素探针能够解释的幅度，二维局部线性代理又要用三个机理倍率拟合，因此搜索把倍率推到上界附近；与此同时，已观测到的镀锂倍率数值删失没有被转化为组合候选的可行域约束。

关键事实如下：

| 候选 | `(SEI, plating, LAM)` | 结果 |
|---|---:|---|
| `PLATING-1P5` | `(1, 1.5, 1)` | 完成至 cycle 75 |
| `PLATING-M` | `(1, 3.16, 1)` | cycle 61 的 `4v_cv` 数值失败 |
| `PLATING-H` | `(1, 10, 1)` | cycle 1 的 `4v_cv` 数值失败 |
| `A` | `(10, 7.6242, 6.7493)` | cycle 1 的 `4v_cv` 数值失败 |
| `B` | `(10, 2.0448, 10)` | cycle 22 首次档失败、重试成功；cycle 23 两档失败 |

这组数据呈现非常一致的模式：高 plating scale 与 `4v_cv` 失败强相关，倍率越高，失败越早。工作流虽然已经停用 `PLATING-M/PLATING-H` 作为正式探针并改用 1.5 倍探针，但组合搜索仍允许 plating scale 在 `0.1–10` 的原始全局边界内取值，最终又生成了 2.0448 和 7.6242。也就是说，**探针层知道高镀锂倍率不稳定，组合生成层却没有继承这条信息**。

### 1.3 另一类被误报成“数值失败”的问题

`SEI-H` 在 cycle 59 的 Step 6 UDDS 中实际触发：

```text
event: Voltage < 2.5 [V] [experiment]
```

按项目规范，这应归类为“容量目标之前先触发 2.5 V”的物理协议失败。当前事件映射器能展开 `2.5 V`，但不能展开协议注册时使用的方向式名称 `< 2.5 V`，隔离调用会返回 `UNKNOWN`。因此该候选被错误写成：

```text
UNKNOWN_TERMINATION / NUMERICAL_FAILURE / NUMERICALLY_CENSORED
```

这不是最新候选 B 的直接失败原因，但它会放大“系统一直在数值报错”的观感，并污染候选失败类别审计。

## 2. 最新错误链

最新异常链可完整还原为：

```text
Stage1AgingCalibration
  -> candidate B, cycle 23
  -> ProtocolStateMachine.run_standard_cycle
  -> PyBaMMBackend.run_standard_charge_sequence
  -> attempt 1: certified_charge
  -> step index 1: 4v_cv
  -> IDA_ERR_FAIL
  -> 回滚并验证 pre-charge state hash 不变
  -> attempt 2: certified_charge_retry
  -> step index 1: 4v_cv
  -> IDA_ERR_FAIL
  -> NumericalFailure(SOLVER_FAILURE)
  -> candidate B = NUMERICALLY_CENSORED
  -> A、B 均无有效 cycle-188 指标
  -> Stage 1 = CALIBRATION_FAILED
```

对应的关键取证字段为：

| 字段 | 值 |
|---|---|
| `completed_aging_cycles` | 22 |
| `failure cycle` | 23 |
| `phase` | `STANDARD_CHARGE` |
| `charge_stage` | `4v_cv` |
| `failed_step_index` | 1 |
| `sundials_error_code` | `IDA_ERR_FAIL` |
| `solver_attempt` | 2 |
| `solver_profile` | `certified_charge_retry` |
| 最近有效 checkpoint | `checkpoints/cycle-022.pkl` |
| `resume_eligible` | `true` |
| 充电前状态哈希 | `d5e4fd90e347ff1090a4b7c2304a46a5dd06f5197a6f7ef03fc6a5aa0eec5223` |

失败尝试没有被提交，最后有效 checkpoint 和已提交输出仍然完整。这个错误不是由输出事务、checkpoint 损坏或恢复错位引起的。

## 3. 数值失稳在失败前已有连续预警

候选 B 并非在 cycle 23 突然毫无征兆地坏掉。

### 3.1 `4v_cv` 模拟时长持续拉长

| cycle | `4v_cv` 模拟时长（s） | 求解情况 |
|---:|---:|---|
| 1 | 2847.81 | 首次档成功 |
| 10 | 3249.76 | 首次档成功 |
| 18 | 3774.03 | 首次档成功 |
| 20 | 4035.82 | 首次档成功 |
| 21 | 4386.14 | 首次档成功，但已接近边界 |
| 22 | 4010.23 | 首次档失败，BDF 2 重试后成功 |
| 23 | 未提交 | BDF 3、BDF 2 均失败 |

cycle 22 已经是明确的预失败信号，但工作流的固定规则允许一次重试成功后继续进入下一圈，因此 cycle 23 才成为终止点。

### 3.2 老化状态不断增强方程耦合

候选 B 从 cycle 1 到 22 的部分内部状态变化为：

| 指标 | cycle 1 | cycle 22 |
|---|---:|---:|
| LLI | 0.04645% | 0.42325% |
| total SEI loss | 0.01384 Ah | 0.07531 Ah |
| total plated lithium inventory | 0.000303 Ah | 0.002554 Ah |
| negative LAM | 0.00295% | 0.06485% |
| negative porosity | 0.24407 | 0.21767 |
| CV charge fraction | 71.27% | 73.77% |

这些状态仍然有限，也没有触发明确模型物理事件，但它们使恒压控制的代数电流与 SEI、镀锂、孔隙率和活性材料状态之间的耦合越来越强。结合精确时间定位和终点状态扫描，可以判断求解器正在处理一个由局部电解液耗尽、可逆镀锂/剥离和恒压电流代数控制共同形成的病态 DAE 区域。

这里需要严格区分“已证实”和“推断”：

- 已证实：生产 BDF 3 在 CV 开始约 `505.78 s` 后失败，生产重试 BDF 2 在约 `501.27 s` 后失败；二者都已稳定跨过 CC→CV 切换瞬间。
- 已证实：失败时并未耗尽配置的 error-test 次数。BDF 3 累计 17 次（上限 30）、BDF 2 累计 16 次（上限 100），但末步分别缩到约 `1.50e-16 s` 和 `2.76e-14 s`，因此本次实际落在错误文本中的“minimum step size was reached”分支。
- 已证实：BDF 2/3 在失稳前负极局部电解液浓度贴近零，且镀锂电流与嵌锂电流发生反向抵消；BDF 1 后续进入完全不同的高电流分支。
- 已证实：从同一个 cycle-022 checkpoint 出发，仅把下一次充电的 plating scale 从 `2.0448` 降为 `1.5`、`1.0` 或 `0.5`，BDF 2 与 BDF 3 均可完成 4.0 V CV。
- 尚未直接计算：失败点 Jacobian 的奇异值、条件数以及具体残差分量；因此可以锁定病态状态组合，但不能声称已定位到某一个唯一的离散方程行。

### 3.3 对 4.0 V CV 的精确时间定位

用候选 B 的 `cycle-022.pkl` 重放“3C CC → 4.0 V CV”前两段，并改变 CV 的强制终止时长，得到稳定且可重复的边界：

| profile | 最长成功 CV 截断 | 最短失败 CV 截断 | 完整求解失稳时刻（距 CV 开始） |
|---|---:|---:|---:|
| BDF 3 / `certified_charge` | 505.7 s | 505.8 s | 约 505.78 s |
| BDF 2 / `certified_charge_retry` | 501.2 s | 501.3 s | 约 501.27 s |

这证明“失败阶段是 `4v_cv`”不能简化成“CC 刚切到 CV 就失败”。生产配置已成功穿过切换点，并运行了约 8.4 分钟后才崩溃。关闭 `suppress_algebraic_error` 的反事实测试会在 CV 开始约 0.01 s 就失败，说明切换初始层本身确实很刚；但当前生产 profile 正是依靠 `suppress_algebraic_error=true` 穿过了该初始层。正式失败属于后续的另一处病态区。

### 3.4 失稳前最后可接受状态

| profile / CV 时刻 | 外部电流 | 负极电解液最小值 | 负极电解液最大值 | 镀锂界面电流密度 | 嵌锂界面电流密度 |
|---|---:|---:|---:|---:|---:|
| BDF 2 / 500.0 s | -0.1561 A | `1.16e-6` mol/m³ | 545.28 mol/m³ | -0.2307 A/m² | +0.1821 A/m² |
| BDF 2 / 501.2 s | -0.1110 A | `2.15e-5` mol/m³ | 939.87 mol/m³ | -0.9935 A/m² | +0.9590 A/m² |
| BDF 3 / 505.7 s | -0.1075 A | `3.81e-5` mol/m³ | 860.76 mol/m³ | -0.7596 A/m² | +0.7261 A/m² |
| BDF 1 / 501.2 s | -10.0858 A | 16.30 mol/m³ | 568.49 mol/m³ | -0.0532 A/m² | -3.0880 A/m² |

三个阶次在 CV 100 s 时仍近似一致：电流约 `-6.923 A`，负极局部电解液浓度已接近 `1e-13 mol/m³`。此后 BDF 1 与 BDF 2/3 分叉；BDF 2/3 继续向小电流截止靠近，并在失稳前出现强烈的局部浓度梯度和副反应/主反应电流抵消。BDF 1 则回到高电流分支，因而它虽能跑完，却不能作为可信的“同解救援档”。

这与 OKane2022/PyBaMM 方程结构一致：

- 负极嵌锂交换电流密度包含 `c_e^0.5`，当 `c_e → 0` 时函数导数变得病态；
- 镀锂交换电流密度正比于 `k_plating * c_e`；
- 剥锂交换电流密度正比于 `k_plating * c_plated_Li`；
- 当前 checkpoint 已累积大量可逆镀锂状态，因此当局部 `c_e` 几乎耗尽时，镀锂/剥锂、嵌锂和恒压代数电流之间会形成非常敏感的补偿关系。

### 3.5 反事实验证

从完全相同的 checkpoint 状态出发，只改变下一次充电的一个因素：

| 反事实 | BDF 3 | BDF 2 | 含义 |
|---|---|---|---|
| plating scale `2.0448 → 1.5` | 完成 CV | 完成 CV | 降低镀锂动力学即可离开失败分支 |
| plating scale `2.0448 → 1.0` | 完成 CV | 完成 CV | 同上 |
| plating scale `2.0448 → 0.5` | 完成 CV | 完成 CV | 同上 |
| CV cutoff `0.05 → 0.12 A` | 在 505.35 s 正常终止 | 在 500.95 s 正常终止 | 在病态点之前结束可避免失败 |
| CV cutoff `0.05 → 0.10 A` | 失败 | 失败 | 病态边界位于约 0.10–0.12 A 区间 |

cutoff 测试只是定位手段，不能直接把正式协议从 0.05 A 改成 0.12 A，否则会改变实验定义。plating-scale 测试也保留了既有老化状态、只改变下一次充电动力学，因此是因果隔离证据，不是可直接用于正式结果的续跑方案。

## 4. 候选生成为什么会系统性越界

### 4.1 实验目标与基线之间的差距过大

以 cycle 0 容量归一化后：

| 节点 | 实验 SOH | 基线 SOH | 需要补偿的差值 |
|---:|---:|---:|---:|
| 25 | 99.33664% | 99.81098% | -0.47434 pp |
| 75 | 97.92028% | 99.60934% | -1.68907 pp |

三个代表探针对每 1 个 `log10(scale)` 的局部响应矩阵为：

```text
cycle 25: [-0.19823, -0.09363, -0.07732] pp
cycle 75: [-0.34817, -0.24398, -0.23082] pp
             SEI       plating    LAM
```

即便组合倍率被推到很高，代理模型仍不能完全追上实验衰减：

| 候选 | 预测 cycle 25 SOH | 预测 cycle 75 SOH | 两节点预测 RMSE |
|---|---:|---:|---:|
| A | 99.46603% | 98.85453% | 0.66692 pp |
| B | 99.50634% | 98.95456% | 0.74113 pp |

这表明搜索不是在一个“内部存在良好解”的区域里微调，而是在模型响应不足时沿边界外推。

### 4.2 两个观测节点拟合三个倍率，本身欠定

代理矩阵维度是 `2 × 3`，秩为 2，非零奇异值约为 `0.53472` 和 `0.04622`，非零条件数约为 11.57。它天然存在一个无法由 cycle 25/75 唯一辨识的机理方向。B 候选正是沿最弱辨识方向构造，因此“SOH 预测相近”不意味着“数值可执行性相近”。

### 4.3 当前算法缺少的约束

当前组合生成只要求：

- 每个倍率位于全局 `0.1–10` 边界；
- A 的预测 RMSE 接近有界最优；
- B 与 A 不同且预测 RMSE 不明显变差。

它没有要求：

- 组合倍率不得超过同机理已完成探针的数值可行范围；
- 已数值删失的探针应形成禁区或删失边界；
- 代理最优点贴边且仍无法拟合目标时，应报告“当前模型/参数族响应不足”，而不是继续生成极端组合；
- 组合候选在启动长跑前先通过针对 `4v_cv` 的短数值压力门。

因此，本次失败不是单个参数偶然抽坏，而是候选生成规则会重复制造高风险候选的结构性结果。

## 5. 为什么现有重试没有救回来

生产重试已经做了正确的事务保护：

- 四段充电作为一个连续 Experiment 求解；
- 失败后丢弃整次候选解；
- 从完全相同的充电前状态重算；
- 保持 `rtol/atol/dt_max` 不变；
- BDF 从 3 降到 2；
- `max_error_test_failures` 从 30 增至 100；
- 排除代数变量的局部误差测试；
- 不允许第三次尝试。

cycle 22 的首次失败被该策略救回，证明重试机制本身有效；cycle 23 的 BDF 2 仍失败，说明当前状态已经越过这套“两档固定策略”的适用边界。

隔离诊断还排除了几个常见误判：

- 把 error-test 额度提高到 200 仍失败，因此不是简单的额度太小；
- 把充电 `dt_max` 缩至 0.1 s 并收紧容差仍失败，因此不是简单的最大步长过大；
- BDF 1 可以完成，因此不是 checkpoint 不可读、初始状态含 NaN、协议事件不可达或必然物理失败。

## 6. 独立的 UDDS 终止误分类缺陷

`SEI-H` 的原始终止文本是：

```text
event: Voltage < 2.5 [V] [experiment]
```

后端注册的允许事件名是：

```text
< 2.5 V
```

事件映射函数只会把无方向的标量形式（例如 `2.5 V`）展开为 PyBaMM 的 `Voltage < 2.5 [V]`，但传入 `< 2.5 V` 时正则不匹配，最后返回 `UNKNOWN`。隔离调用已经复现该返回值。

影响为：

1. 本应为 `PHYSICAL_PROTOCOL_FAILURE / PHYSICAL_EVENT_BEFORE_TARGET`；
2. 实际被记录为 `NUMERICAL_FAILURE / UNKNOWN_TERMINATION`；
3. `candidate_failure_classes` 错误标记为 `NUMERICALLY_CENSORED`；
4. 报告读者会误以为又发生了一次求解器崩溃。

这处缺陷不会造成候选 B 的 `IDA_ERR_FAIL`，但必须单独修正，避免污染标定决策与审计。

## 7. 不是根因的项目

根据现有证据，可以排除或降级以下假设：

| 假设 | 结论 | 依据 |
|---|---|---|
| 数据文件损坏 | 排除 | 目标清单和输入哈希存在；基线及多个探针已读到 cycle 75 |
| UDDS 容量窗口计算误差导致最新 B 失败 | 排除 | B 的 cycle 1–22 窗口误差约 `1e-14–1e-15`，失败发生在充电前半段 |
| 输出或 checkpoint 写坏 | 排除 | failure context 指向已提交 cycle-022，`resume_eligible=true`，失败尝试未提交 |
| 仍在使用累计绝对时间求解 | 排除 | 当前版本为 `stage-local-time-v2-robust-charge`，核心源码哈希与运行报告一致 |
| 仅仅是 `max_num_steps` 不足 | 不支持 | 错误码为 `IDA_ERR_FAIL` 而非工作量耗尽；提高 error-test 额度也无效 |
| 已注册的物理事件导致 B 终止 | 不支持 | 没有触发模型事件；但局部电解液浓度已实质贴零，当前模型恰好没有为此注册终止事件 |
| 单元测试失败 | 排除 | 与重试、失败分类、代理候选相关的 27 项定向测试全部通过 |

最后一项也说明：现有测试覆盖了代码规则是否按设计执行，但没有覆盖“删失探针形成组合可行域”和“候选 B cycle 23 的真实 BDF 鲁棒性”，所以测试全绿并不与长运行失败矛盾。

## 8. 报告与审计层的次要缺陷

这些问题不触发求解失败，但会降低可解释性：

1. 顶层 `stage1_status.json` 的 `reason` 为 `null`，没有直接汇总 A/B 的失败原因。
2. 顶层 `.run.lock` 已正常释放，却仍保留 `business_status: RUNNING`。
3. `candidate_ranking.csv` 中 A/B 的 `retry_count` 都是 0；B 在 cycle 22 实际使用过一次重试。原因是候选未到 cycle 188 时，跳过分支重新构造了 `retry_count=0` 的评分记录。
4. failure JSON 没有失败局部时间、IDA 内部步长、残差或 Jacobian 统计，导致只能通过隔离求解继续缩小原因。
5. `run.log` 只记录成功循环窗口误差，没有写最终求解失败摘要；完整堆栈只存在于 `run_status.json`。

## 9. 建议的处理顺序

### P0：保留现场，不要直接从 cycle 22 盲目续跑

用当前代码和当前两档 profile 恢复 B，极高概率会在同一个 cycle 23、同一个 `4v_cv` 再次失败。`resume_eligible=true` 仅表示 checkpoint 完整，不表示原失败原因已经消失。

### P1：先修正组合候选的数值可行域

建议把数值删失当成真实约束，而不只是排名标签：

- `PLATING-M=3.16` 和 `PLATING-H=10` 已经删失，不能再允许组合搜索无条件提出 7.62；
- `PLATING-1P5=1.5` 只证明到 cycle 75 可执行，不应自动宣称到 188 安全；
- 在 1.5 与 3.16 之间增加有界可行性探针，或采用删失感知的上界更新；
- A/B 组合在长跑前增加 cycle 1、关键历史状态和 cycle 25 的标准充电压力门。

### P2：代理模型检测“目标不可达/边界饱和”

当有界最优仍留下明显残差且多个参数贴上界时，应输出模型响应不足状态，停止把倍率继续推向边界。下一步应重新审视参数族、模型结构或目标分阶段策略，而不是把数值失败候选当作普通搜索成本。

### P3：不要把 BDF 1 作为救援档；增加状态可接受性门

BDF 1 虽然能跨过最新 cycle 23，但它在 CV 约 100 s 后与 BDF 2/3 分叉，并在 501.2 s 给出约 `-10.09 A` 而非约 `-0.111 A`。现有证据已不足以支持把它当作同解的稳健替代。建议：

- 为负极电解液最小浓度增加诊断输出和预注册的数值可接受性阈值；
- 明确规定局部 `c_e` 接近零时是“状态不可接受/候选数值删失”，而不是继续依靠积分阶次选择分支；
- 若要研究 BDF 1，只能把它用于分支敏感性分析，并与网格加密、不同求解器或 DFN 参考交叉验证；
- 不应将“BDF 1 能跑完”作为恢复正式长跑的放行条件。

此外，`suppress_algebraic_error=false` 会在切入 CV 后约 0.01 s 失败，因此简单翻转该开关也不是修复方案；真正需要处理的是高 plating 参数、局部电解液耗尽与恒压小电流截止共同形成的病态区。

### P4：修正 `< 2.5 V` 事件映射和顶层审计

应增加方向式电压事件的精确映射测试，并同步修正 `SEI-H` 的失败分类规则；同时补齐顶层 reason、锁终态、失败候选 retry count 和 run.log 摘要。

## 10. 最终判断

当前“总是报错”的本质不是一个点，而是一条因果链：

```text
当前 SPMe 三机理局部响应不足
  -> 2 节点 / 3 参数代理欠定且最优点贴边
  -> 数值删失信息没有约束组合搜索
  -> 生成高 plating scale 的 A/B
  -> aging 后的 3C/4.0 V CV 把负极局部电解液推至近零
  -> 接近 0.05 A 截止前，镀锂/剥离与嵌锂电流强烈抵消
  -> 恒压 DAE Jacobian/误差控制病态，IDA 步长塌缩
  -> BDF 3、BDF 2 在约 505.78 s / 501.27 s 失败
  -> A/B 均无法到 cycle 188
  -> Stage 1 必然得到 CALIBRATION_FAILED
```

同时还存在：

```text
UDDS 触发 2.5 V 物理事件
  -> 方向式事件名未被映射器识别
  -> 被误记为 UNKNOWN_TERMINATION / NUMERICAL_FAILURE
```

因此，下一步的重点不应是简单地“再点一次继续运行”或只增加求解次数，而应同时处理：

1. 组合候选的删失感知可行域；
2. 局部电解液浓度可接受性门与候选 CV 压力测试；
3. UDDS 物理事件分类缺陷；
4. 顶层失败报告的可审计性。

## 11. 主要证据索引

- 最新总状态：`outputs/pybamm_spme_calibration/w10-stage1-soh-v1/stage1_status.json`
- 最新总报告：`outputs/pybamm_spme_calibration/w10-stage1-soh-v1/stage1_report.json`
- 候选清单：`outputs/pybamm_spme_calibration/w10-stage1-soh-v1/candidate_manifest.json`
- B 失败状态：`outputs/pybamm_spme_calibration/w10-stage1-soh-v1/candidates/B/run_status.json`
- B 求解尝试：`outputs/pybamm_spme_calibration/w10-stage1-soh-v1/candidates/B/solver_attempts.jsonl`
- B 周期趋势：`outputs/pybamm_spme_calibration/w10-stage1-soh-v1/candidates/B/cycle_summary.csv`
- CV 深度诊断数据：`outputs/pybamm_spme_calibration/w10-stage1-soh-v1/candidates/B/diagnostics/cycle-023-4v-cv-deep-diagnostic.json`
- CV 反事实诊断数据：`outputs/pybamm_spme_calibration/w10-stage1-soh-v1/candidates/B/diagnostics/cycle-023-4v-cv-counterfactual-interventions.json`
- 可重复诊断脚本：`scripts/diagnose_candidate_b_cycle23_cv.py`
- 反事实诊断脚本：`scripts/diagnose_candidate_b_cycle23_cv_interventions.py`
- SEI-H 误分类现场：`outputs/pybamm_spme_calibration/w10-stage1-soh-v1/candidates/SEI-H/run_status.json`
- 标准充电执行：`src/pybamm_w10/backend.py`
- 求解器 profile：`src/pybamm_w10/model.py`
- 组合候选生成：`src/pybamm_w10/calibration/surrogate.py`
- Stage 1 编排与排名：`src/pybamm_w10/calibration/aging.py`
