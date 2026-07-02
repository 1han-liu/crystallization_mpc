# Gsensor Hough MATLAB/Python 对照测试计划

## 目标

验证当前 Python 版 gsensor Hough 相关实现是否与源 MATLAB 计算结果一致，并用 MATLAB 导出的 `.mat` 中间数据分段定位差异来源。

当前重点链路在 `src/crystallization_mpc/apps/gsensor/detection/update_line.py`：

```python
Hs, thetas, rhos = hough(I, theta=calc_theta_range(line.theta, params_G.delta_theta))
Hs(rhos < rho_min | rhos > rho_max) = 0
peaks = houghpeaks(Hs, params_G.num_peak, nhood_size=(9, 1))
lines = houghlines(I, thetas, rhos, peaks, fill_gap=5, min_length=7)
```

注意：`H_rho_filtered` 对应的是当前 MATLAB 代码实际执行后的 `Hs`。这里要复现当前 MATLAB 语句 `Hs(rhos < rho_min | rhos > rho_max) = 0`，而不是先改成按 rho 行过滤的理想逻辑。Python 对照测试中应按 MATLAB 的列优先线性索引语义复现这一句。

因此测试应优先覆盖：

1. `hough()`
2. rho 窗口过滤
3. `houghpeaks()`
4. `houghlines()`
5. 完整 `update_line()` 选线结果

## 基本思路

从源 MATLAB 代码中运行一组真实数据，在 Hough 计算前后保存关键中间量。然后将 MATLAB 保存的 Hough 输入传给当前 Python 代码，逐层比较 Python 输出和 MATLAB 输出。

这个方案可行，且比只比较最终 `line/dist` 更适合迁移调试。原因是最终结果不一致时，分层数据可以判断差异来自 accumulator、rho 过滤、peak 选择、line 提取，还是后续 candidate 选择逻辑。

## “分层保存”的含义

“分层保存”不是必须保存成多个文件，而是在一个 `.mat` case 文件中保存多个关键检查点。

推荐一组数据保存成一个文件，例如：

```text
tests/data/gsensor/hough_debug_001.mat
tests/data/gsensor/hough_debug_002.mat
```

每个 `.mat` 顶层保存一个结构体，例如 `hough_debug`。结构体内按算法阶段保存字段。

## 推荐保存字段

### 必须字段

用于验证 `hough()`：

- `I_hough_input`: Hough 前的二值图。对应 MATLAB 调用 `hough(I, ...)` 那一刻的 `I`。
- `theta_input`: MATLAB 传给 `hough` 的 theta 数组。
- `H_raw`: MATLAB `hough` 输出的 accumulator。
- `rho_output`: MATLAB `hough` 输出的 rho 数组。

用于验证 rho 过滤：

- `rho_min_max`: `[rho_min, rho_max]`。
- `H_rho_filtered`: rho 窗口过滤后的 H。

用于验证 `houghpeaks()`：

- `peaks_matlab_1based`: MATLAB `houghpeaks` 输出。注意 MATLAB peak 索引是 1-based，Python 对比前需要减 1。

用于验证 `houghlines()`：

- `lines_hough_raw`: MATLAB `houghlines` 原始输出，包含每条线的 `point1`、`point2`、`theta`、`rho`。

用于保留上下文：

- `line_before`: Hough 前使用的 line，至少包含 `point1`、`point2`、`theta`、`rho`。
- `params_G.width`
- `params_G.width_divider`
- `params_G.num_peak`
- `params_G.len_min`

### 完整 `update_line()` 测试时再保留

如果要测试完整选线和距离计算，再保留：

- `line_after`
- `line_candidate`
- `t`
- `e`
- `n`
- `o`
- `is_opposite`
- `dist`

### 可以先删掉或暂时不保存

只测 Hough/HoughPeaks/HoughLines 时，以下字段可以先不要：

- `image_file`
- `image_path`
- `created_at`
- `params_G.ratio`
- `thetas`，如果已保存 `theta_input`，它是重复字段。
- `peak_rho`
- `peak_theta`
- `peak_value`
- `lines_processed`

这些字段不是永远没用，而是对第一阶段定位 Hough 差异不是必需。

## 命名建议

当前已查看的文件使用了这些字段：

- `I_hough`
- `theta_range`
- `H_raw`
- `H_masked`
- `thetas`
- `rhos`
- `rho_window`
- `peaks`
- `lines_raw`

建议后续改成更明确的名字：

| 当前名称 | 建议名称 | 原因 |
| --- | --- | --- |
| `I_hough` | `I_hough_input` | 明确这是 Hough 输入 |
| `theta_range` | `theta_input` | 明确这是传入 Hough 的 theta |
| `H_masked` | `H_rho_filtered` | 避免和图像 mask 混淆 |
| `rhos` | `rho_output` | 明确这是 Hough 输出 rho |
| `rho_window` | `rho_min_max` | 明确两个值含义 |
| `peaks` | `peaks_matlab_1based` | 提醒 Python 对比时需要转 0-based |
| `lines_raw` | `lines_hough_raw` | 明确来自 MATLAB `houghlines` |

## 已检查的样例文件

已检查文件：

```text
C:\Users\Jack\Desktop\MPCrystal\gsensor_data\hough_debug\hough_debug_20260626_104244648_000001.mat
```

该文件顶层只有一个结构体 `hough_debug`，Python `scipy.io.loadmat` 可以读取。

主要字段：

- `I_hough`: shape `(1024, 1500)`，`uint8`，0/1 图，非零点数 2172。
- `theta_range`: shape `(20,)`。
- `H_raw`: shape `(3631, 20)`。
- `H_masked`: shape `(3631, 20)`。
- `rhos`: `-1815:1815`。
- `rho_window`: `[679.8031890312907, 729.8031890312907]`。
- `peaks`: shape `(10, 2)`，MATLAB 1-based。
- `lines_raw`: 空。
- `lines_processed`: 空。
- `line_before` 与 `line_after` 相同，说明该帧未更新出新线。

这个样例足够验证 `hough()` 和 `houghpeaks()`，但由于 `lines_raw` 为空，不足以充分验证 `houghlines()` 的端点输出。

## 已发现的关键差异

使用该 `.mat` 中的 `I_hough/theta_range` 调当前 Python `hough()` 时，Python 输出 H shape 为：

```text
(3635, 20)
```

MATLAB 保存的 `H_raw` shape 为：

```text
(3631, 20)
```

MATLAB 的 `rho_output` 是：

```text
-1815:1815
```

当前 Python 代码计算出的 rho 范围对应：

```text
-1817:1817
```

这说明第一层差异已经出现在 `hough()`。

进一步验证显示，该 MATLAB 结果对应的 Hough 坐标语义是：

- 像素坐标使用 0-based 的 `x, y`。
- rho 最大范围使用 `ceil(hypot(height - 1, width - 1))`。

而当前 Python 代码中：

```python
diag = int(np.ceil(np.hypot(height, width)))
x = x.astype(float) + 1.0
y = y.astype(float) + 1.0
```

也就是使用了：

- `ceil(hypot(height, width))`
- `x + 1, y + 1`

这是当前已确认的首个问题点。

当使用 MATLAB 的 rho 范围和 0-based 坐标重新计算时，`H_raw` 可以与 MATLAB 完全一致。

## 类型注意事项

当前样例中：

- `H_raw` 是 `uint8`
- `H_masked` 是 `uint8`

这组数据最大值只有 15，因此没有丢信息。但后续真实图像的 Hough 投票值可能超过 255。建议 MATLAB 保存时不要强制转成 `uint8`，优先保存为：

- MATLAB 默认 `double`
- 或显式 `uint16` / `uint32`

`I_hough_input` 保存为 logical、uint8 0/1 都可以。

## Python 测试落地建议

新增测试数据目录：

```text
tests/data/gsensor/
```

将精简后的 `.mat` case 放入该目录。

新增 pytest 文件，例如：

```text
tests/test_gsensor_hough_matlab_parity.py
```

建议测试顺序：

1. 读取 `.mat`，确认字段完整。
2. 用 `I_hough_input` 和 `theta_input` 调 Python `hough()`。
3. 比较 `rho_output`。
4. 比较 `H_raw`。
5. 用 `rho_min_max` 过滤 Python H，比较 `H_rho_filtered`。
6. 用 Python `houghpeaks()` 跑 `H_rho_filtered`，比较 `peaks_matlab_1based - 1`。
7. 如果 `lines_hough_raw` 非空，用 Python `houghlines()` 比较线段输出。
8. 如果需要完整流程，再比较 `line_after` 和 `dist`。

## 对比容差建议

`hough()` 和 rho 过滤：

- 应该可以要求完全一致。

`houghpeaks()`：

- 如果 H 完全一致，理论上应尽量完全一致。
- 注意 MATLAB 1-based 和 Python 0-based 转换。
- 如果多个 peak 票数相同，可能需要确认 tie-breaking 规则。

`houghlines()`：

- 不建议一开始要求端点逐值完全一致。
- 可先比较数量、theta、rho。
- 端点允许 1 像素左右容差。
- 还要考虑 `point1` / `point2` 顺序可能相反。

完整 `update_line()`：

- 比较 `line_after.theta/rho`。
- 比较 `line_after.point1/point2`，允许小容差。
- 比较 `dist`。

## 后续行动

1. 在 MATLAB 端按推荐字段导出一份精简 `.mat`。
2. 至少准备两类 case：
   - 当前这种 `lines_hough_raw` 为空的 case，用于验证 Hough 和 peaks。
   - 一组 `lines_hough_raw` 非空的 case，用于验证 `houghlines()`。
3. 将 `.mat` 放入 `tests/data/gsensor/`。
4. 新增 pytest，对每层分别断言。
5. 先修正 Python `hough()` 的坐标和 rho 范围，使 `H_raw/rho_output` 与 MATLAB 一致。
6. 再逐层处理 `houghpeaks()` 和 `houghlines()` 的差异。
