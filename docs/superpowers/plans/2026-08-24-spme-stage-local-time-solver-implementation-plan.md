# SPMe全流程阶段局部时间求解实施计划

- 文档日期：2026-08-24
- 上位规范：`E:\SPMe\docs\superpowers\specs\2026-08-24-spme-stage-local-time-solver-design.md`
- 状态：局部时间主体已实现；阶段固定双档书面修订待用户复核后实施；未授权启动350循环
- 数据保护：不得修改`outputs/pybamm_spme/w10-350-solver-resilience-v1`

## 1. 建立审计基线

对将修改的源码、测试、README和规范生成SHA-256清单，写入独立`tmp`目录。
记录正式旧运行的状态、进度和cycle 22检查点哈希。

## 2. 版本与检查点测试

先修改测试，要求：

- `solver_execution_version=stage-local-time-v1`；
- `checkpoint_schema_version=6`；
- schema 5不能由新配置正式恢复；
- 输出schema和协议算法版本不变。

随后最小修改`config.py`和检查点兼容逻辑使测试通过。

## 3. 局部时间核心与后端时间账本

新增后端内部类型和纯函数：

- 终端状态无损归一化到局部时间0；
- 已提交局部求解片段到全局时间的映射；
- 外部累计实验时间；
- 快照保存归一化状态和累计时间。

测试覆盖状态/导数逐位保持、累计时间、restore/fork/compact和失败不变性。

## 4. 单阶段求解迁移

把rest、恒流、恒压、容量终止和UDDS统一迁移为：

```text
normalize start -> solve local candidate -> validate -> commit segment
```

终止时间向协议层返回全局时间。测试固定协议值、阶段时长、UDDS本地波形时间和
候选失败原子性。

## 5. 连续标准充电迁移

四步Experiment从局部0开始。将四个step的终止时间和轨迹时间映射到全局时间；
重试仍从同一归一化状态哈希开始。分析完成前不提交主后端候选。

测试覆盖四步顺序、终止事件、轨迹映射、重试、部分cycle和分析失败原子性。

## 6. 跨阶段时序与摘要

`timeseries_since()`从已提交片段生成全局单调时序，正确去重共享边界。
`summary_metrics()`使用全局片段计算电量和温度极值。保留周期CSV语义不变；
`compact_state()`在结果提交后清除片段但保留终端状态和累计时间。

## 7. 自治性保护

增加基础模型显式时间依赖检查。固定CC/CV/rest模型必须自治；UDDS只允许协议
电流插值使用阶段局部时间。检测到未授权绝对时间物理项时拒绝正式运行。

## 8. 阶段固定双档配置测试

先新增失败测试，固定以下配置和映射：

- `solver_profile_policy_version=phase-fixed-v1`；
- `general_protocol`为`rtol=1e-5`、`atol=1e-7`、`dt_max=1.0 s`；
- `certified_charge`为`rtol=3e-7`、`atol=3e-9`、`dt_max=0.1 s`；
- 连续四段标准充电以及RPT CC/CV充电只能选择`certified_charge`；
- rest、Step 5、UDDS和RPT容量放电只能选择`general_protocol`；
- 档位选择不得读取cycle编号、状态值或首次失败结果；
- 连续四段标准充电仍构建一个四步Experiment；
- 配置指纹、guard指纹和JSON审计同时包含两个档位及策略版本。

测试必须证明保守IDAKLU重试继承所属阶段的`rtol`、`atol`和`dt_max`。
未经认证的不同容差或步长结果不得提交。

## 9. 阶段固定双档实现

在`config.py`保留现有通用求解字段，并增加明确命名的充电认证字段；不要把
运行时字典作为隐式配置。`model.py`提供纯档位构造函数，`backend.py`只根据
协议阶段身份选择档位。

标准充电继续使用一次四步Experiment和现有候选后提交边界。RPT充电选择严格
档，但不得改变其电流、电压、截止事件或主状态分支语义。其他单阶段求解继续
使用通用档。输出和heartbeat写入实际档位名称，配置指纹变化使旧配置检查点
不能混用。

## 10. 跨模块回归

运行局部目标测试，再运行全量测试。静态核对所有协议常量、顺序、RPT节点、
Step 5、UDDS和科学输出字段未变化。

## 11. 有界真实求解与精度验证

运行不包含老化循环的真实PyBaMM smoke；在隔离目录只读重放旧cycle 22状态的
cycle 23标准充电。不得写入旧正式目录。验证顺序为：

1. 初始健康状态以及只读旧cycle 14--22检查点作为cycles 15--23初态的连续
   四段`certified_charge`相对严格参考门槛；
2. cycle 22压力状态上的完整静置、完整Step 5和一个完整UDDS基础波形周期；
3. 一次最终配置的真实RPT/smoke；
4. cycle 23最终配置只读重放，要求一次成功且不触发保守重试。

探针只向stdout或隔离临时目录写结果。结论记录到规范或审计摘要后删除一次性
验证代码和临时输出。不得运行0--25回归或350循环。

通用阶段的收紧档固定为`rtol=1e-6`、`atol=1e-8`、`dt_max=0.5 s`，参考档
固定为`rtol=1e-7`、`atol=1e-9`、`dt_max=0.1 s`。一个UDDS基础波形周期只
比较同一最终时间的数值响应；完整容量终止事件由生产档smoke覆盖。

## 12. 性能验收

记录cycle 23标准充电参考档、认证档以及smoke中各阶段墙钟时间。交付时明确：

- 分阶段方案只承诺避免全局`0.1 s`造成的UDDS成本放大；
- cycle 22实测分布推算普通cycle额外成本约`4--5 s`，不是350循环完成时间保证；
- 若通用档精度失败，只局部收紧失败阶段类别并重新认证。

## 13. 文档与运行命令

更新README，说明阶段局部时间、阶段固定双档、schema 6、新目录、从cycle 0
启动和旧检查点不兼容。核验CLI帮助后提供：

- 用户自行启动350循环的PowerShell命令；
- 只读查看进度和错误的PowerShell命令。

不得执行350循环启动命令。
