# PyBaMM W10 3C 老化仿真设计

## 1. 目标与范围

使用本机 `battery` Conda 环境中的 PyBaMM 26.7.1，对 LG Chem INR21700-M50T 电芯执行 W10 3C 工况的 350 个 aging cycles。模型采用 `OKane2022` 参数集、DFN 电化学模型和集总热模型，并显式模拟 SEI、锂析出、颗粒开裂和活性材料损失。

默认运行模式为非侵入式虚拟 RPT，用于隔离 W10 3C aging protocol 本身造成的退化。另实现简化的 `strict-w10` 模式，使容量诊断及 post-RPT 恢复过程参与主电芯状态演化，供后续定量验证。

本次 strict-W10 仅实现容量测试及相关预处理、恢复流程。HPPC 和 EIS 只定义稳定接口，不实现物理过程。

## 2. 参考数据与实验条件

主要参考文件：

- `E:/battery/data/LG M50T/Lithium-ion battery aging dataset based on electric vehicle real-driving profiles.pdf`
- `E:/battery/data/LG M50T/README.xlsx`
- `E:/battery/data/LG M50T/cycling/W10/W10-1.mat`
- `E:/battery/data/LG M50T/cycling/w10_dataset/W10-1.csv`
- `E:/battery/data/LG M50T/_processed_mat/W10_capacity_diagnostic_01.csv` 至 `W10_capacity_diagnostic_15.csv`

电芯与边界条件：

| 项目 | 数值 |
|---|---:|
| 型号 | LG Chem INR21700-M50T |
| 化学体系 | NMC 正极，石墨/硅负极 |
| 标称容量 | 4.85 Ah |
| 标称电压 | 3.63 V |
| 充电截止电压 | 4.2 V |
| 放电截止电压 | 2.5 V |
| CV 截止电流 | 0.05 A |
| 质量 | 0.06925 kg |
| 直径 | 0.02144 m |
| 长度 | 0.07080 m |
| 环境温度 | 23 degC |

W10 容量诊断节点为 aging-cycle 0、25、75、122、146、148、151、159、188、225、250、275、300、325 和 350。

## 3. 软件环境

必须使用以下解释器：

`C:/Users/Lenovo/anaconda3/envs/battery/python.exe`

运行元数据必须记录 Python、PyBaMM、NumPy、CasADi、求解器及操作系统版本。代码不得静默切换到其他 Python 环境。

## 4. 物理模型

### 4.1 基础模型

- PyBaMM `pybamm.lithium_ion.DFN`
- 参数集 `OKane2022`
- 集总热模型，初始温度和环境温度均为 296.15 K
- 全空间副反应计算，不启用 x-average side reactions

### 4.2 老化机制

启用以下 PyBaMM 模型选项：

- solvent-diffusion-limited SEI
- SEI porosity change
- SEI on cracks
- partially reversible lithium plating
- lithium plating porosity change
- negative-electrode swelling and cracking
- positive-electrode swelling
- stress-driven loss of active material
- stress-induced diffusion

保留 `OKane2022` 中死锂形成、SEI、析锂、开裂和 LAM 的原始参数，不使用 W10 容量数据拟合或校准退化参数。

### 4.3 参数覆盖

仅覆盖有明确实验依据的参数：

- `Nominal cell capacity [A.h] = 4.85`
- `Upper voltage cut-off [V] = 4.2`
- `Lower voltage cut-off [V] = 2.5`
- `Ambient temperature [K] = 296.15`
- `Initial temperature [K] = 296.15`
- `Cell volume [m3]` 使用圆柱体外形计算值
- `Cell cooling surface area [m2]` 使用圆柱体侧面与两端面积之和

`Total heat transfer coefficient [W.m-2.K-1]` 保持 `OKane2022` 的 10。电芯质量写入配置及结果元数据，但在没有实测整电芯比热的情况下，不据此臆造新的热容量；热容量继续使用 `OKane2022` 的分层材料参数。不得把外部圆柱尺寸错误地覆盖为卷芯电极宽度或高度。

## 5. UDDS 数据处理

### 5.1 数据来源与符号

UDDS 必须来自 W10 实测 cycling 数据，不使用通用标准 UDDS 文件。原始 W10 数据中放电电流为负、回馈充电为正；进入 PyBaMM 前转换为放电为正、充电为负。

论文不同位置对电流符号的文字说明存在矛盾，因此数据中的容量积分和电压响应是符号判定的最终依据。

### 5.2 代表波形生成

从 W10 Step 14 数据中识别约 2600 s 的重复单元。将多个完整重复单元重采样至 1 Hz 后进行相位平均，以去除测量噪声而保留脉冲、回馈和动态结构。

将平均后的 2600 s 单元重复，并截取最后一个不完整单元，使 Step 5 与 Step 6 从 Step 5 起累计净移出 `0.80 * Q_ref`。Step 5 实际净移出量记为 `Delta_Q_5,actual`，因此当前循环的 UDDS 剩余目标为：

`Q_UDDS,remaining = 0.80 * Q_ref - Delta_Q_5,actual`

在 Step 5 的容量事件精确触发于 `0.20 * Q_ref` 时，`Q_UDDS,remaining` 等于 `0.60 * Q_ref`；实现仍必须使用累计80%定义，不能在进入 UDDS 时把容量计数器重新归零并无条件再移出60%。

其中 `Q_ref` 是最近一次成功容量 RPT 测得的 0.24 A 放电容量。每个 RPT 节点只更新一次 `Q_ref`；该值在本节点之后、下一次 RPT 之前的整批 aging cycles 内保持不变。不得按每个 aging cycle 的瞬时状态重新估算目标。计划内 RPT 必须成功产生新的、有限且为正的容量结果后才能进入下一批；cycle-350 最终 RPT 也必须成功产生该结果才能满足完整验收。不得在 RPT 失败后静默沿用上一批的旧值。

不得将测得的 10 Hz 电流噪声直接作为 DFN 控制命令，也不得采用 5 至 10 s 粗平均作为默认方案。

### 5.3 UDDS 验证

生成的 1 Hz 曲线必须验证：

- Step 5 与 UDDS 从 Step 5 起累计净移出量相对当前 `0.80 * Q_ref` 目标的误差不超过 0.1%
- UDDS 阶段净移出量相对 `Q_UDDS,remaining` 的误差不超过 0.1%
- 回馈充电量有记录并与原始 Step 14 比较
- RMS 电流、最小值和最大值与原始 Step 14 比较
- 时间严格单调、无 NaN、无重复时间戳
- PyBaMM 符号转换后容量积分方向正确

基础 2600 s 单元只需验证一次波形统计量；每次根据 `Q_ref` 和 `Delta_Q_5,actual` 重复并截断后，还必须验证该循环生成的 UDDS 阶段满足剩余目标，并使 Step 5–6 累计达到80%批次目标。验证结果写入机器可读 JSON 和运行日志。

## 6. Aging protocol

### 6.1 标准 3C aging cycle

标准循环从约 20% SOC 开始：

1. 以 3C，即 14.55 A，CC 充电至 4.0 V。
2. 在 4.0 V 下 CV 充电至电流低于 0.05 A。
3. 以 C/4，即 1.2125 A，CC 充电至 4.2 V。
4. 在 4.2 V 下 CV 充电至电流低于 0.05 A。
5. 静置 30 min。
6. 在 Step 5 开始时记录容量基准 `q_window_start`，以 C/4 放电，直至从该基准起累计净移出达到 `0.20 * Q_ref`，使电芯到协议定义的约 80% SOC；记录切换时的实际值 `Delta_Q_5,actual`。
7. 不重置容量基准，施加处理后的 UDDS 波形，直至从 `q_window_start` 起累计净移出达到 `0.80 * Q_ref`，使电芯到协议定义的约 20% SOC。UDDS 阶段的剩余目标是 `0.80 * Q_ref - Delta_Q_5,actual`。

只有第 7 步完成后，aging-cycle 编号才增加 1。`Q_ref` 必须来自最近一次成功 RPT，并在两个相邻 RPT 节点之间冻结；下一次 RPT 成功后才更新下一批循环的20%阶段目标和80%累计目标。容量移出量按净库仑积分判断：Step 5 开始时读取 PyBaMM 的 `Discharge capacity [A.h]` 作为 `q_window_start`，Step 5 和 Step 6 均使用该变量相对同一基准的增量，因此 UDDS 中的回馈电流会抵消一部分已移出容量。

Step 5 的20%阶段和随后约60%的 UDDS 阶段共同构成相对于批次参考容量 `Q_ref` 的80%累计协议窗口，不是 DFN 内部电极化学计量比的绝对 SOC 边界。实现使用 PyBaMM 自定义容量终止事件：Step 5 检测相对 `q_window_start` 的 `0.20 * Q_ref`，Step 6 继续检测相对同一基准的 `0.80 * Q_ref`；两阶段均保留 2.5 V 电压下限。若先触发 2.5 V 而未达到当前累计容量目标，则记录为 `PHYSICAL_PROTOCOL_FAILURE`。

### 6.2 初始状态

默认 `virtual` 模式的主老化轨迹从约 20% SOC 开始。先在 cycle-0 节点执行非侵入式虚拟容量 RPT 以取得首个 `Q_ref`，丢弃诊断分支状态后，再以未改变的主状态执行第一批标准 3C aging cycles。

`strict-w10` 模式从 cycle-0 容量诊断开始；cycle-0 诊断后的恢复充电构成第一个特殊 post-RPT aging cycle 的充电阶段。

## 7. RPT 模式

### 7.1 默认 virtual 模式

在每个诊断节点：

1. 获取主 DFN 的 `last_state`。
2. 从该状态建立独立诊断分支。
3. 在分支上以 1C 充至 4.2 V，再 CV 至 0.05 A，并静置 1 h。
4. 在0.24 A容量放电开始前读取 `Discharge capacity [A.h]` 为 `q_rpt_start`，再以固定 0.24 A 放电至 2.5 V，并读取终点值 `q_rpt_end`。
5. 仅以容量放电阶段的局部增量 `Q_RPT = q_rpt_end - q_rpt_start` 作为测得容量；不得使用包含预处理充电的全分支累计值。验证 `Q_RPT` 有限且为正后，记录相对初始诊断容量的 SOH 和相对 4.85 Ah 的标称 SOH。节点小于 cycle 350 时把 `Q_RPT` 保存为后续批次的 `Q_ref`；cycle-350 节点只把它记录为最终容量与 SOH，不再派生没有后续用途的控制目标。
6. 丢弃诊断分支的 DFN 状态。

RPT、预处理充电和容量测试均不得改变主老化状态。代码必须在诊断前后比较主状态向量、循环编号和累计主老化时间，证明其非侵入性。诊断产生的标量 `Q_ref` 是允许保留的协议控制结果：它只更新下一批循环的容量目标，不把诊断过程中的时间、温度或退化写回主轨迹。

该模式用于隔离 3C-W10 aging protocol 本身的退化效应，不等同于严格复现 W10 实验的完整测试历史。

### 7.2 strict-w10 模式

strict-W10 容量诊断在主状态上执行，因此会推进主电芯的日历时间、温度和退化状态，但诊断过程本身不增加 aging-cycle 编号。

诊断及后续特殊循环的状态机为：

1. 在主状态上以 1C CC 充电至 4.2 V、CV 至 0.05 A并静置 1 h，使容量测试从满充状态开始；该步骤属于诊断预处理，不增加 aging-cycle 编号。
2. 在0.24 A容量放电开始前记录 `q_rpt_start`，放电至 2.5 V 后记录 `q_rpt_end`，仅以 `q_rpt_end - q_rpt_start` 作为容量；节点小于 cycle 350 时将其设为下一批循环的 `Q_ref`，cycle-350 节点仅记录最终容量和 SOH。不得把诊断预处理充电计入容量。
3. 若当前节点小于 cycle 350，以 1C CC 充电至 4.2 V、CV 至 0.05 A并静置 1 h。
4. 第 3 步是下一个特殊 post-RPT aging cycle 的充电阶段，不在该步骤结束时增加循环编号。
5. 随后在 Step 5 开始时记录唯一的 `q_window_start`，以 C/4使累计净移出达到 `0.20 * Q_ref`，再以 UDDS 继续使相对同一基准的累计净移出达到 `0.80 * Q_ref`。
6. 完整特殊循环结束后 aging-cycle 编号增加 1。
7. 再下一个循环恢复标准 3C aging protocol。

cycle-350 容量 RPT 执行完成后，默认不再进行没有后续用途的恢复充电和 1 h 静置。

HPPC 和 EIS 通过版本稳定的协议接口预留。未实现时必须显式抛出 `NotImplementedError`，不得在 strict-W10 模式下静默跳过用户请求的诊断类型。

## 8. 软件结构

实现划分为以下独立组件：

1. 配置和参数：集中定义电芯、模型、协议、求解器和输出参数。
2. UDDS 提取器：读取 W10 MAT 数据、生成 1 Hz 波形并验证守恒量。
3. DFN 工厂：构建 `OKane2022` DFN、老化选项、参数覆盖、网格和求解器。
4. 协议状态机：执行标准循环、virtual RPT和 strict-W10 特殊循环。
5. 运行器：管理 350 cycles、检查点、恢复、日志和状态分类。
6. 诊断模块：容量 RPT，以及 HPPC/EIS 的预留接口。
7. 导出与绘图：生成 CSV、JSON、图形和代表性时序。

组件之间以显式配置和结果对象传递数据，不依赖可变全局变量。模式差异必须封装在协议状态机内，不能复制两套老化求解代码。

## 9. 求解、内存与检查点

优先使用当前 PyBaMM 环境支持的 IDAKLU 求解器，并为 DFN、老化和热耦合设置明确的相对误差、绝对误差及最大步长。求解器配置必须写入运行元数据。

每 5 个完整 aging cycles 保存可恢复检查点，在所有 RPT 节点额外保存。检查点至少包含：

- 完整 DFN `last_state`
- aging-cycle 编号
- 主老化累计时间和日历时间
- RPT 模式和协议状态
- 最近一次成功 RPT 的 `Q_ref`、其诊断节点、当前批次的20%阶段目标、80%累计目标和当前循环的 UDDS 剩余目标
- 配置指纹及 UDDS 文件指纹
- PyBaMM 及求解器版本
- 已写出结果的最后行号或事务编号

恢复时必须验证配置和 UDDS 指纹；不匹配时拒绝继续，避免把不同模型状态拼接到同一结果中。

完整时序仅保留第 1、25、75、175、350 个 aging cycle以及全部容量 RPT。其他循环只保存汇总指标，避免累计 Solution 对象导致内存持续增长。

## 10. 输出

每次运行写入独立目录，并至少包含：

- `run_config.json`
- `environment.json`
- `udds_profile.csv`
- `udds_validation.json`
- `cycle_summary.csv`
- `rpt_summary.csv`
- `degradation_summary.csv`
- `run_status.json`
- `run.log`
- `checkpoints/`
- `timeseries/`
- `figures/`

循环汇总至少记录：循环编号、模式、起止时间、`Q_ref` 及其来源 RPT 节点、20%阶段目标、80%累计目标、`Delta_Q_5,actual`、UDDS 剩余目标、Step 5–6 实际累计净移出 Ah、各阶段持续时间、充放电 Ah、端电压、最高温度、最低温度、SEI 容量损失、析锂容量、死锂容量、正负极 LAM、关键孔隙率及终止原因。

RPT 汇总至少记录：诊断节点、`q_rpt_start`、`q_rpt_end`、两者差值得到的0.24 A容量、相对初始容量 SOH、标称 SOH、诊断模式、诊断开始和结束时间、诊断是否改变主状态、该容量是否已成为新 `Q_ref`；若存在后续批次，还要记录由它派生的20%阶段目标和80%累计目标。cycle-350 记录最终容量与 SOH，但不生成后续控制目标。

图形至少包括：

- 容量和 SOH 随 aging-cycle 的变化，并叠加未经拟合的 W10 实测容量
- 温度随循环的变化
- SEI、析锂、死锂和 LAM 的退化贡献
- 代表性循环的电流、电压、温度时序
- virtual 与 strict-W10 结果的可比视图，但仅在两种模式均有完整输出时生成

## 11. 运行状态与验收

正式运行必须无未分类异常地结束。`run_status.json` 必须使用以下互斥终态之一：

### 11.1 COMPLETED

模型完成 W10 工况的 350 个完整 aging cycles，并成功执行 cycle-350 容量 RPT。

### 11.2 PHYSICAL_PROTOCOL_FAILURE

模型在达到完整验收条件前因合法物理终止事件无法继续完成规定 aging protocol 或计划内容量 RPT。该范围包括 aging-cycle 0 至350的主协议、所有中间 RPT，以及350个完整 aging cycles 结束后的 cycle-350 最终容量 RPT。示例包括：

- 在目标 Ah 尚未移出前提前达到 2.5 V
- 充电阶段因模型电压或可行电流限制无法完成
- 任一计划内 RPT 因非预期物理边界无法完成；RPT容量放电按计划到达2.5 V是成功终止，不属于失败
- 孔隙率、活性材料体积分数或其他有明确物理含义的 PyBaMM 事件到达合法边界

该终态是有效模型结果，不属于数值失败。必须保存最后有效状态、已完成循环数、失败阶段、触发的物理事件、事件值及相关电芯状态。

### 11.3 NUMERICAL_FAILURE

求解器异常、无法收敛、NaN/Inf、非法状态向量、无法分类的表达式错误或输出损坏均归入该终态。必须记录异常类型、堆栈、最后有效检查点和失败阶段。

不得把求解器错误重新标记为物理终止，也不得把合法 PyBaMM 物理事件误报为数值失败。任何未能映射到上述三种终态的退出均视为实现缺陷。

## 12. 验证与测试

### 12.1 单元测试

- 电芯几何体积与散热面积计算
- 3C、C/4及由 `Q_ref` 计算20%阶段目标、80%累计目标和 UDDS 剩余目标
- W10 电流符号转换
- 2600 s 重复单元识别和 1 Hz 相位平均
- UDDS 净 Ah、回馈 Ah、RMS和峰值验证
- RPT 节点调度
- `Q_ref` 仅在成功 RPT 后更新并在批内冻结
- Step 5 和 Step 6 容量事件共享 `q_window_start`，分别在累计20%和80%触发，UDDS 回馈电流正确抵消净移出量
- RPT容量严格等于 `q_rpt_end - q_rpt_start`，不受此前预处理充电影响
- 运行终态分类

### 12.2 状态机测试

- 标准 3C aging cycle 只在完成 UDDS 后增加编号
- virtual RPT 前后主 `last_state`、主时间和循环编号不变
- virtual RPT 丢弃诊断 DFN 状态但保留新 `Q_ref`，下一批目标随之更新
- strict-W10 诊断改变主状态但不增加循环编号
- strict-W10 post-RPT 1C CC-CV 和 1 h 静置属于下一特殊循环
- 特殊循环完成 C/4和UDDS后只增加一次编号
- Step 6 不重置容量基准，并在 Step 5–6 累计达到 `0.80 * Q_ref` 后结束
- aging阶段先触发2.5 V、未完成当前累计目标时分类为 `PHYSICAL_PROTOCOL_FAILURE`
- RPT容量放电按计划到达2.5 V视为成功事件
- cycle-350 strict-W10 RPT 后不执行恢复充电
- HPPC/EIS 未实现接口显式失败

### 12.3 集成测试

- DFN 构建和参数处理冒烟测试
- 一个标准 aging cycle
- 一个 virtual RPT 分支
- 一个 strict-W10 诊断及特殊循环
- 两个相邻 RPT 批次，验证第二次 RPT 后目标更新且批内保持不变
- 人工使 cycle-350 最终 RPT 触发阻断流程的合法物理事件，并验证分类为 `PHYSICAL_PROTOCOL_FAILURE`
- 检查点保存、恢复与连续运行结果对比
- 人工触发物理事件并验证 `PHYSICAL_PROTOCOL_FAILURE`
- 人工注入 NaN或求解器异常并验证 `NUMERICAL_FAILURE`

### 12.4 正式运行验收

正式运行必须无未分类异常地结束。

若模型能够完成 W10 工况，则以 350 个完整 aging cycles 及 cycle-350 容量 RPT 成功作为完整仿真验收。

若在达到完整验收条件前，包括350个 aging cycles 完成后的 cycle-350 最终容量 RPT，因合法物理终止事件无法完成规定 aging protocol 或计划内 RPT，则记录为 `PHYSICAL_PROTOCOL_FAILURE`，视为有效模型结果而非数值失败。容量 RPT 按计划放电至2.5 V属于成功终止；只有阻止诊断按定义完成的非预期物理事件才属于失败。

求解器异常、NaN或非法状态记录为 `NUMERICAL_FAILURE`。

## 13. 非目标

- 不拟合 `OKane2022` 老化参数
- 不把 virtual 模式宣称为完整 W10 实验复现
- 本次不实现 HPPC 和 EIS 物理过程
- 不使用通用 UDDS 文件替代 W10 实测波形
- 不保存全部 350 cycles 的高频完整时序
- 不以经验容量衰减曲线替代 DFN 状态演化
