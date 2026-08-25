# 阶段 K 实施与验收报告

日期：2026-08-18  
工作区：`E:\battery\new`  
最终状态：通过（cycle-0 容量标定）；未执行任何 aging cycle。

## K1–K3 门槛

- K1：最终全量 pytest 通过，`72 passed`。
- K2：只构建检查通过。初始 SOC 为 20%，W10 周期为 2600 s，识别到 175 个完整单元；求解器容差、最大步长和最大步数未修改。
- K3：短真实 smoke 在 `outputs/pybamm_w10/event-remediation-smoke-v2-k3-retry3` 通过。Step 6 容量事件发生在 23.7809035 s，早于 profile 终点 62.5901620 s；容量相对误差为 `3.39084e-13`；恢复状态哈希一致；锁、rollback、heartbeat 与无 aging 断言均通过。

## K4 cycle-0 容量校准

成功工件目录：`outputs/pybamm_w10_calibration/m50t-w10-v1-retry3`。

- 参数集：`OKane2022-M50T-W10-v1`
- `capacity_scale_factor`：`0.95630859375`
- 目标容量：`4.865884391243259 Ah`
- 独立 DFN RPT 容量：`4.86592335159038 Ah`
- 容量相对误差：`8.00684e-06`（阈值 `0.002`）
- 二分区间宽度：`5.859375e-05`（阈值 `1e-4`）
- 总评估数：14（含独立复算，预算上限 16）
- 独立复算差：`0.0`（阈值 `0.0002`）
- 全区间电压 RMSE：`0.04546479 V`，状态 `CAPACITY_MATCHED_VOLTAGE_PASSED`
- 三个退化倍率均为 1，状态为 `not_calibrated`；`full_dfn_confirmed=false`。

校准状态按数据门槛停在 `AGING_DATA_INCOMPLETE / MISSING_W10_HPPC_EIS`。该状态不允许把本参数伪装成正式 aging 参数。

## K5 只读与安全审计

- 原工程基线清单差异：0。
- `outputs/pybamm_w10/virtual-formal-001` 哈希/大小差异：0。
- 成功校准输出中的 `cycle_summary.csv`、`degradation_summary.csv`：0 个。
- 成功校准输出中的非 cycle-0 aging checkpoint：0 个。
- `capacity_calibration.json` 与 `calibrated_parameters.json` 的 `holdout_accessed` 都为 `false`。
- 参数指纹：`9c8aead9f4bf05af9e660ec7a734405445f12b1f2118e021ac363435c88b5c40`。
- 有效参数审计指纹：`507d7a3eb8ee41cf49e1507808b25e0f1cbb5fdb65e4488681e43b4080482baa`。

## 保留的失败证据

以下目录未被覆盖，保留用于取证：

- `outputs/pybamm_w10_calibration/m50t-w10-v1`：历史拼接状态哈希边界。
- `outputs/pybamm_w10_calibration/m50t-w10-v1-retry1`：混合 RPT trace 的曲线段选择边界。
- `outputs/pybamm_w10_calibration/m50t-w10-v1-retry2`：cycle-0 文件名零填充边界。

三项边界均已增加回归测试，最终成功运行不复用任何失败候选状态。
