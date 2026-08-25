# PyBaMM W10 M50T定量容量与老化校准设计

日期：2026-08-18  
状态：书面规范已获用户确认，已转入实施计划  
依赖规范：

- `docs/superpowers/specs/2026-08-17-pybamm-w10-3c-aging-design.md`
- `docs/superpowers/specs/2026-08-17-pybamm-w10-formal-readiness-design.md`
- `docs/superpowers/specs/2026-08-18-pybamm-w10-event-resilience-remediation-design.md`

诊断依据：

- `docs/reports/2026-08-18-pybamm-w10-failure-and-capacity-audit.md`
- `docs/reports/2026-08-18-pybamm-w10-350-cycle-stop-and-remediation.md`
- `data/LG M50T/README.xlsx`
- `data/LG M50T/Lithium-ion battery aging dataset based on electric vehicle real-driving profiles.pdf`

## 1. 目标

建立一个可审计、可恢复、可迁移的M50T定量校准框架，使最终strict-W10 DFN能够：

1. 复现M50T cycle-0绝对容量；
2. 使用cycle 25–225数据校准W10老化衰减；
3. 在参数冻结后预测cycle 250–350留出容量；
4. 分别报告样本内校准精度和样本外时间外推精度；
5. 为后续其他充电倍率或工况实验提供冻结参数和外部验证流程。

本规范不允许使用经验容量修正项替代DFN状态演化，也不允许把留出节点用于调参。

## 2. 与事件可靠性整改的关系

定量校准依赖事件可靠性整改先完成。任何容量校准或老化搜索前，以下条件必须满足：

- Step 6容量事件位于带guard的drive-cycle内部；
- `final time`不再误分类为物理失败；
- 失败上下文、checkpoint schema 3和输出事务可用；
- smoke与生产共用`DriveWindowPlan`；
- 运行目录锁、自动回滚和heartbeat通过测试。

事件整改默认不改变OKane2022参数。只有显式提供本规范定义的版本化校准参数文件时，模型工厂才允许注入容量和退化倍率。

## 3. 已确认的科学口径

### 3.1 主轨迹

定量校准和最终精度评价固定使用：

```text
model = DFN
mode = strict-w10
temperature = 296.15 K
```

virtual模式只保留为机理对照，不作为实验总衰减的定量校准结果。

### 3.2 容量和倍率

- 4.85 Ah继续作为铭牌容量和实验电流基准；
- cycle-0实测容量4.865884391243259 Ah作为初始绝对容量目标；
- 通过显式有效电极面积缩放使DFN容量与M50T一致；
- 不通过修改OCP、最大浓度、活性材料体积分数或外部圆柱尺寸强行匹配容量；
- 14.55 A、1.2125 A、0.24 A和0.05 A实验电流保持不变。

### 3.3 标定观测量

- 容量是主要目标；
- 容量诊断的电压–容量曲线是辅助约束；
- W10 cycling的代表性电压响应是辅助约束；
- 本地W10 cycling文件没有电芯实测温度列，温度不参与拟合；
- 模型温度作为预测输出，不能宣称经过实验温度校准。

### 3.4 参数数量

允许一个初始容量参数和最多三个退化倍率：

```text
capacity_scale_factor
sei_scale
plating_scale
lam_scale
```

不开启更多退化、热、传输或OCP参数。若该低维参数集不能达到验收精度，应报告模型结构或数据可辨识性不足，而不是继续增加自由度拟合留出数据。

## 4. 数据事实与清单

### 4.1 本地可用数据

W10当前包含：

- 14个cycling MAT文件和14个大CSV文件；
- 15个处理后的容量诊断CSV；
- 诊断节点：0、25、75、122、146、148、151、159、188、225、250、275、300、325、350；
- cycle-0容量：4.865884391243259 Ah；
- cycle-350容量：4.459092949711614 Ah。

论文2022版本正文只列出当时已完成的W10前9次诊断；本地README和15个容量诊断文件代表后续扩展数据。节点调度和容量目标以当前本地README及实际文件为准，论文用于确认实验协议和RPT组成，不用其早期节点数量覆盖本地扩展记录。

### 4.2 RPT完整性

README和论文确认，W10每个诊断节点的RPT由以下内容构成：

- 容量测试；
- HPPC；
- EIS；
- RPT后的1C CC-CV充满和1小时静置。

EIS在20%、50%、80% SOC执行。当前工作区没有W10 HPPC/EIS原始或处理文件，因此无法重建完整strict-W10诊断负载、SOC转换和日历时间。

### 4.3 数据门槛

当前数据允许执行cycle-0容量标定，但不允许启动退化参数标定。老化标定的强制数据门槛为：

```text
AGING_DATA_INCOMPLETE
reason = MISSING_W10_HPPC_EIS
```

获得W10 HPPC/EIS数据、核验文件哈希和恢复完整RPT时间线后，才能进入`AGING_CALIBRATION_READY`。

不得把HPPC/EIS缺失造成的退化或日历时间差吸收到SEI、析锂或LAM倍率中。

## 5. 数据拆分与防泄漏

### 5.1 固定拆分

校准集：

```text
cycle 0：初始容量与电压曲线校准
cycle 25–225：退化参数校准
```

留出验证集：

```text
cycle 250、275、300、325、350
```

### 5.2 防泄漏接口

校准数据加载器只能返回cycle 0–225目标值。留出目标由独立评价入口管理，参数文件状态不是`PARAMETERS_FROZEN`时拒绝读取。

参数冻结时生成：

- 参数JSON内容哈希；
- 校准数据清单哈希；
- 目标函数版本；
- 优化器配置；
- 冻结UTC时间；
- `holdout_accessed=false`。

留出评价完成后只追加评价工件，不修改已冻结参数文件。

### 5.3 留出失败

留出RMSE或cycle-350误差超限时，状态为`HOLDOUT_FAILED`。不得使用留出数据继续调参并覆盖原参数版本。如需新研究，必须创建新的校准版本和新的预注册数据拆分。

## 6. 软件架构

新增：

```text
src/pybamm_w10/calibration/
├── __init__.py
├── data.py
├── split.py
├── parameters.py
├── objectives.py
├── surrogate.py
├── workflow.py
└── artifacts.py
```

### 6.1 `data.py`

负责：

- 读取容量诊断；
- 提取终点容量和电压–容量曲线；
- 读取cycling代表性电压段；
- 读取未来HPPC/EIS文件；
- 验证节点、列、单位、单调性、有限性和文件哈希；
- 生成`diagnostic_inventory.json`。

原始数据只读。任何重采样或清洗写入独立处理工件并记录算法版本。

### 6.2 `split.py`

固定校准和留出节点，提供防泄漏的数据视图。生产代码不得通过普通文件路径绕过该视图访问留出目标。

### 6.3 `parameters.py`

定义参数schema、边界、对数变换、原值、有效值和PyBaMM注入。所有参数变化生成独立指纹。

### 6.4 `objectives.py`

负责容量误差、电压曲线误差、正则化、早停和可辨识性指标。目标函数版本固定并写入校准配置。

### 6.5 `surrogate.py`

构建SPMe和粗网格DFN候选代理，验证老化机制支持和参数映射。代理只用于筛选候选，不能作为最终350循环结果。

### 6.6 `workflow.py`

实现校准状态机、候选调度、恢复、逐级筛选、参数冻结和留出评价门槛。

### 6.7 `artifacts.py`

原子写入配置、候选、指标、参数文件、清单、图形和状态。复用正式运行的单写者锁、事务和checkpoint后回滚语义。

## 7. 参数注入

### 7.1 初始容量参数

`capacity_scale_factor`缩放：

```text
effective_electrode_width
  = OKane2022 Electrode width [m]
  × capacity_scale_factor
```

PyBaMM中正负极共享该电极宽度，因此正负极有效面积同比缩放，保持两极容量平衡。同一外部电流下，电流密度随面积同步调整。

不缩放：

- 外部直径和长度；
- Cell volume和cooling surface area；
- 电极高度；
- 电极厚度；
- 活性材料体积分数；
- 最大浓度；
- OCP函数；
- 传输和反应动力学。

### 7.2 退化倍率

`sei_scale`作用于当前solvent-diffusion-limited SEI控制参数：

```text
SEI solvent diffusivity [m2.s-1]
```

`plating_scale`作用于：

```text
Lithium plating kinetic rate constant [m.s-1]
```

`lam_scale`同时作用于：

```text
Negative electrode LAM constant proportional term [s-1]
Positive electrode LAM constant proportional term [s-1]
```

两极使用同一LAM倍率，避免容量数据无法区分的额外自由度。

保持不变：

- SEI其他动力学和热激活参数；
- dead lithium decay；
- plating transfer coefficient；
- crack几何和增长函数；
- reaction-driven LAM；
- 扩散、交换电流和热参数。

### 7.3 边界

```text
0.90 <= capacity_scale_factor <= 1.02
-1 <= log10(sei_scale) <= 1
-1 <= log10(plating_scale) <= 1
-1 <= log10(lam_scale) <= 1
```

任何扩大边界都需要新设计版本，不由优化器自动完成。

## 8. cycle-0容量校准

### 8.1 目标

```text
Q_target = 4.865884391243259 Ah
capacity_relative_error <= 0.002
```

每个候选从相同规范20% SOC和相同参数基线独立执行strict-W10 cycle-0容量RPT。不同参数候选不得复用PyBaMM状态。

### 8.2 求解

先验证参数边界两端能夹住目标且容量随面积单调。随后使用有界一维求根，直到：

- 容量相对误差不超过0.2%；
- 参数区间达到配置收敛容差；
- 最优候选重复求解结果在数值容差内一致。

如果边界不能夹住目标、响应不单调或重复结果不一致，标定失败；不得静默扩大边界。

### 8.3 电压曲线

将模拟与实测容量放电段插值到共同容量网格，至少报告：

- 全2.5–4.2 V区间RMSE；
- 10%–90%容量中段RMSE；
- 最大绝对电压误差；
- 终点容量误差。

初始接受标准：全区间电压RMSE不超过50 mV。超过时状态标记为`CAPACITY_MATCHED_VOLTAGE_FAILED`，不通过增加OCP或动力学参数强行补偿。

## 9. 退化目标函数

退化参数只使用cycle 25–225。

电压辅助目标固定使用：

- cycle 0、25、75、122、146、148、151、159、188、225的容量RPT电压–容量曲线；
- 每个对应cycling批次中，紧邻下一次RPT之前的最后一个完整标准aging cycle的Step 1–6电压响应；
- strict-W10特殊post-RPT循环不作为“标准循环电压”目标，但其状态演化仍属于主轨迹。

如果某批次不存在可验证的完整标准循环，该批次电压辅助项记为缺失并在数据清单中说明，不用相邻批次替代。

设容量相对残差为`e_Q`，电压残差为`e_V`，对数倍率向量为`z`：

```text
J = mean((e_Q / 0.01)^2)
  + mean((e_V / 0.05 V)^2)
  + lambda_reg × ||z||^2
```

容量是主要约束。电压项使用预先定义的代表性曲线和固定重采样网格。`lambda_reg`在优化前写入配置，不能根据留出结果调整。

候选出现以下情况时提前停止并保存失败原因：

- 数值失败；
- 明确物理协议失败；
- 非有限状态；
- 参数越界；
- 已到达校准节点的容量误差超过预设淘汰阈值；
- 代理与完整DFN方向不一致。

## 10. 多保真校准

### 10.1 代理选择

构建：

- 具有相同老化选项和参数注入的SPMe；
- 具有较粗空间离散的DFN。

通过短真实段检查：

- 所需老化机制是否存在；
- 参数倍率是否作用于同一物理量；
- 容量、电压和机制响应方向是否与完整DFN一致；
- checkpoint和严格RPT流程是否可执行。

若SPMe不能支持完整机制或方向检查失败，使用粗网格DFN。若两者都失败，状态为`NO_VALID_SURROGATE`，禁止用经验曲线替代。

### 10.2 筛选顺序

未来获得老化运行授权且HPPC/EIS数据完整后：

1. 代理模型有界候选搜索；
2. 候选推进到cycle 25并早停；
3. 保留候选推进到75；
4. 再推进到122；
5. 最终候选推进到225；
6. 少量候选使用完整DFN逐级复核；
7. 仅根据cycle 0–225确定最优完整DFN参数；
8. 生成不可变冻结参数文件。

每级候选数量和优化预算必须在实施计划中固定，并写入校准配置。运行中不得根据留出结果扩充候选。

### 10.3 最终模型

代理输出不能直接成为正式结果。`DFN_CALIBRATED`必须表示完整strict-W10 DFN已到达cycle 225并满足校准集RMSE标准。

## 11. 精度验收

### 11.1 已确认标准

| 指标 | 标准 |
|---|---:|
| cycle-0绝对容量误差 | <=0.2% |
| cycle 25–225校准容量RMSE | <=1.0% |
| cycle 250–350留出容量RMSE | <=2.0% |
| cycle-350容量误差 | <=2.0% |
| cycle-0全区间电压RMSE | <=50 mV |

所有容量误差同时报告Ah、相对百分比和SOH误差。电压报告RMSE、最大绝对误差和分SOC区间误差。

### 11.2 结果标签

- 达到校准标准但未评价留出：`PARAMETERS_FROZEN`；
- 留出全部达标：`HOLDOUT_PASSED`；
- 留出未达标：`HOLDOUT_FAILED`；
- 容量达标但电压不达标：`CAPACITY_MATCHED_VOLTAGE_FAILED`；
- 总曲线达标但参数不可辨识：保留拟合结果，同时标记`MECHANISMS_NOT_IDENTIFIABLE`。

不得用“恢复精度”笼统替代这些互斥、可量化指标。

## 12. 可辨识性与不确定性

在完整DFN最优点附近计算局部敏感度和残差Jacobian，至少报告：

- 每个倍率对各校准节点容量的敏感度；
- 参数相关矩阵；
- 条件数；
- 基于局部线性近似的置信区间；
- 正则化项对结果的影响。

若参数高度相关或置信区间跨越大部分允许范围，不能宣称SEI、析锂和LAM贡献比例已被唯一识别。总容量预测通过与机理参数可辨识是两个独立结论。

W10只有一只电芯，置信区间不包含电芯间离散性；报告必须明确这一限制。

## 13. 校准状态机

```text
DATA_AUDITED
CAPACITY_CALIBRATION_READY
CAPACITY_CALIBRATED
AGING_DATA_INCOMPLETE
AGING_CALIBRATION_READY
SURROGATE_SCREENED
DFN_CALIBRATED
PARAMETERS_FROZEN
HOLDOUT_EVALUATED
```

允许的主要转换：

```text
DATA_AUDITED
  -> CAPACITY_CALIBRATION_READY
  -> CAPACITY_CALIBRATED

CAPACITY_CALIBRATED
  -> AGING_DATA_INCOMPLETE       # 当前本地状态
  -> AGING_CALIBRATION_READY     # 仅数据完整后
  -> SURROGATE_SCREENED
  -> DFN_CALIBRATED
  -> PARAMETERS_FROZEN
  -> HOLDOUT_EVALUATED
```

失败状态保存原因、最后完整阶段、参数指纹和可恢复工件。未完成候选不能作为正式参数文件。

## 14. 参数文件

版本化JSON示例路径：

```text
calibration/m50t-w10-v1.json
```

至少包含：

- schema和校准ID；
- 校准状态；
- 四个参数值、边界、原值和有效值；
- 参数作用的PyBaMM键；
- strict-W10模式；
- DFN/代理模型标记；
- Python、PyBaMM和求解器版本；
- 数据清单、拆分和文件哈希；
- 目标函数和优化器版本；
- 校准指标；
- 留出访问状态与指标；
- 是否通过完整DFN确认；
- 参数文件自身哈希。

运行入口新增：

```text
--calibration-params <json>
```

未提供时使用OKane2022基线。提供时必须严格校验schema、状态、边界、模型类型、文件哈希和版本。未经完整DFN确认的代理参数不得用于正式350循环。

## 15. 输出目录

```text
outputs/pybamm_w10_calibration/<calibration-id>/
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

### 15.1 本阶段参数文件

本阶段完成cycle-0后：

- `capacity_scale_factor`写入已标定值；
- `sei_scale=1`、`plating_scale=1`、`lam_scale=1`；
- 三个退化倍率状态为`not_calibrated`；
- 总状态最高为`CAPACITY_CALIBRATED`或`AGING_DATA_INCOMPLETE`；
- 不得标记为`DFN_CALIBRATED`或`PARAMETERS_FROZEN`。

### 15.2 候选隔离

每个候选有独立ID、参数指纹、目录、状态和日志。候选输出不能追加到正式运行CSV。完成候选可用于恢复搜索调度；未完成RPT候选从规范初态重跑。

## 16. 本阶段授权边界

本阶段允许：

- 完成事件可靠性整改；
- 实现校准模块、数据门槛和防泄漏；
- 运行全部非求解测试；
- 运行短真实PyBaMM smoke；
- 执行cycle-0 DFN容量因子有界求根；
- 重复最优cycle-0候选；
- 生成电压曲线比较和校准工件。

本阶段禁止：

- 任何aging cycle；
- SEI、析锂或LAM搜索；
- 0–225老化校准；
- 250–350留出评价；
- 350循环正式运行；
- HPPC/EIS数据下载；
- 修改求解器精度或步长；
- 修改或恢复旧`virtual-formal-001`目录。

## 17. 测试设计

### 17.1 数据测试

- 15个容量文件与节点映射；
- cycle-0和cycle-350精确容量；
- 14个cycling区间；
- 文件哈希；
- README中W10容量/HPPC/EIS节点；
- HPPC/EIS缺失时状态阻断；
- 校准加载器不能读取留出目标；
- 参数冻结后独立留出入口可读。

### 17.2 参数测试

- 面积缩放同时作用于正负极；
- 外部热几何不变；
- 三个退化倍率作用于正确PyBaMM键；
- 参数边界、对数变换和指纹；
- 未校准倍率不能标记为冻结；
- 代理参数不能冒充完整DFN参数。

### 17.3 cycle-0测试

- 边界夹住目标；
- 容量对面积响应单调；
- 求根收敛；
- 容量误差不超过0.2%；
- 最优候选重复性；
- 电压共同容量网格；
- 50 mV判定；
- 中断后的候选级恢复；
- 不同候选状态隔离。

### 17.4 校准工作流测试

使用FakeBackend或合成目标验证：

- 状态机合法/非法转换；
- 早停；
- 正则化；
- 候选排序；
- 参数冻结；
- 留出访问审计；
- holdout pass/fail不修改参数；
- 缺少HPPC/EIS禁止老化标定。

### 17.5 真实求解

只执行已授权的事件smoke和cycle-0容量标定。不得在测试名义下运行aging cycle。

## 18. 本阶段完成条件

以下全部满足才视为本阶段完成：

- 事件可靠性整改全部测试通过；
- 校准模块及防泄漏测试通过；
- 数据清单准确指出HPPC/EIS缺失；
- cycle-0容量标定误差不超过0.2%；
- cycle-0电压RMSE不超过50 mV，或明确产生`CAPACITY_MATCHED_VOLTAGE_FAILED`而不伪装成功；
- 最优容量因子重复可复现；
- effective-parameter审计记录面积缩放及所有未校准倍率；
- 参数文件未冒充完成老化标定；
- 旧正式目录未修改；
- 未运行任何aging cycle或350循环。

## 19. 后续阶段门槛

### 19.1 老化校准前

必须：

1. 取得W10 HPPC/EIS诊断数据；
2. 重建15个RPT的完整时间线和SOC转换；
3. 对HPPC/EIS协议做短真实验证；
4. 获得用户对老化求解预算的单独授权；
5. 完成一个完整生产参数strict-W10 aging cycle验收。

### 19.2 留出评价前

必须：

1. 完整DFN到cycle 225；
2. 校准RMSE不超过1%；
3. 参数可辨识性报告完成；
4. 参数文件冻结；
5. 留出目标从未被优化器读取。

### 19.3 其他工况

W10参数冻结后，其他工况首次验证：

- 保持容量、SEI、析锂和LAM参数不变；
- 只更换实验工况、电流和环境条件；
- 报告外部验证误差；
- 不立即重新拟合。

若后续重新标定其他工况，必须生成新参数版本，不能覆盖W10参数文件或把重新拟合结果称为外部预测。

## 20. 预期影响文件

新增或修改：

- `src/pybamm_w10/calibration/`；
- `src/pybamm_w10/config.py`；
- `src/pybamm_w10/model.py`；
- `src/pybamm_w10/runner.py`；
- `src/pybamm_w10/cli.py`；
- `src/pybamm_w10/output.py`；
- `src/pybamm_w10/types.py`；
- `tests/`下校准、数据门槛、参数注入和工作流测试；
- 事件可靠性整改规范中与容量归一化冲突的范围描述。

不复制一套独立DFN运行器。校准候选最终仍调用正式模型工厂、协议状态机、诊断和输出基础设施。
