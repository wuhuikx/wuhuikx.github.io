# Compass 仿真与实测对比

Compass 是 ATOM 推理引擎的虚拟时钟仿真器。这里放各 Cell 实测与仿真的 Prometheus 对比图，任务跟踪见 [#592](https://github.com/wuhuikx/LLM-Booster/issues/592)。

## 多配置看板（按模型 / 部署方式 / 并行配置 / 并发切换）

**交互式对比图：<https://wuhuikx.github.io/LLM-Booster/compass/dashboard.html>**

直接打开即可，不需要登录。

- **页面结构：** 顶部依次选模型、部署方式（PD 分离 / aggregated）、并行配置、并发。最上面的总览给出所有配置 × 并发的误差热力图，选一个指标（逐请求的 TTFT / TPOT / 吞吐，或任一 Prometheus 面板）即可横向比较；下面是选中配置的误差矩阵、逐请求表和面板曲线（实线实测，虚线仿真）。误差 = 仿真 / 实测 − 1。
- **当前数据：** DeepSeek-V4.1-Flash 1P1D（P TP2 EP2，D TP4），Mooncake Store 开，pit2-p03-g27，c = 1 / 8 / 32 / 64 / 128，每档 1800 s agentic 实跑；仿真为每档自身记录拟合的定律（in-sample）。每档只展示一条仿真，记作 sim：标准发送顺序（不设发送顺序种子）的那次运行。

  | 指标（sim） | c=1 | c=8 | c=32 | c=64 | c=128 |
  |---|--:|--:|--:|--:|--:|
  | TTFT P50 | −7.0% | −1.7% | −2.8% | −3.7% | −6.2% |
  | TPOT P50 | 0.0% | −0.5% | −0.7% | −1.0% | −1.1% |
  | 吞吐/卡 | +0.6% | +0.1% | +0.6% | 0.0% | +0.5% |

  PD 部署的仿真报告不含 mesh 入口的 Overall TTFT 面板，所以总览里该面板指标为空；端到端 TTFT 请看逐请求的 TTFT 指标。
- **生成方式：** jgong5/ATOM `scripts/compass/compare_dashboard.py` 的多配置版本，见 [jgong5/ATOM#3939](https://github.com/jgong5/ATOM/pull/3939)（尚未合入；合入前请用该 PR 的分支 `compass/dashboard-multi-config` 生成，`dsv41/dev` 上的旧版不支持 `--scan` 和多配置）。新结果按目录约定放好即可自动出现在页面上，不用手写清单：

  ```
  ROOT/<模型>/<pd|aggregated>/<并行配置>/c<并发>/
      real/  prom/report-data.json  sims/<label>/prometheus.json  [one_real.txt]  [cell.json]
  ```

  `python3 scripts/compass/compare_dashboard.py --scan ROOT --out dashboard.html`，再把生成的页面替换本目录下的 `dashboard.html`。每个 cell 只放一个仿真目录 `sims/sim/`（标准发送顺序）。

  当前页面由 `python3 scripts/compass/compare_dashboard.py /share_nfs/xingjche/compass_dashboard/1p1d_sweep_sim/manifest.json --title 'Compass · DeepSeek-V4.1-Flash 1P1D 真实 vs 仿真' --out dashboard.html` 生成（jgong5/ATOM#3939 的分支，`e20e4a06b`）。

## Cell A：单机 DPA4-EP4（DeepSeek-V4.1-Flash）

**交互式对比图：<https://wuhuikx.github.io/LLM-Booster/compass/cellA_dashboard.html>**

直接打开即可，不需要登录。

- **对象：** DeepSeek-V4.1-Flash，MI355X 单机 4 卡，DPA4-EP4（4 个 DP attention rank + 专家并行），MTP 投机解码（5 个 draft，强制接受长度 3.51），bf16 KV，decode CUDA graph 档位 1–64，max-num-seqs 64。
- **实测：** 每个并发（c = 1 / 8 / 32 / 64 / 128 / 256 / 512）一次预热后的 30 min agentic 多轮真跑，aiperf 窗口相同。c=512 超过容量（64 条 × 4 rank），以吞吐和 P90 为主看。
- **仿真：** Compass 用同一负载、同一请求序列（发送顺序 seed 0）。实线是实测，虚线是仿真，横轴是从第一个正式请求开始的分钟数，取 1800 s 计分窗口。仿真代码为上游 jgong5/ATOM `dsv41/dev` 2fdaae22（含 #3742 突发接收顺序修复）加本地校准提交，digest 与实测一致。
- **定律，即当前最优方案：理论构造的校准负载 + PG2 定律。**
  - 在真机上录一份校准记录，共 11 段客户端，32 GPU min；只用这份记录拟合，拟合 0.8 min。
  - decode 为 F1：高档位按全组真实 MoE token 计价。
  - prefill 为 PG2：增加 token graph 分桶的 `gather_rows` 列。
  - host law 用平稳段和 decode drain 窗口拟合。
  - 校准方法详解：[#739](https://github.com/wuhuikx/LLM-Booster/issues/739)；优化全过程：[#666 评论](https://github.com/wuhuikx/LLM-Booster/issues/666#issuecomment-7093204062)。

主要看 P90、吞吐和前缀命中率；P50、P99 是次要指标，在汇总表里排在后面并淡化显示。误差 = 仿真 / 实测 − 1（发送顺序 seed 0）。

| 指标 | c=1 | c=8 | c=32 | c=64 | c=128 | c=256 | c=512 |
|---|--:|--:|--:|--:|--:|--:|--:|
| TTFT P90 | +0.4% | −2.5% | −4.7% | +1.2% | +12.5% | −3.3% | −0.0% |
| TPOT P90 | −1.8% | −4.8% | +1.0% | −12.2% | +0.4% | −1.9% | −7.6% |
| 交互度 P90 | +1.8% | +5.1% | −1.0% | +13.9% | −0.4% | +2.0% | +8.3% |
| 吞吐/卡 | +0.5% | +2.0% | +1.3% | +1.2% | −0.3% | −1.6% | +4.3% |
| GPU 前缀命中率 | −0.1% | +1.5% | −0.0% | +0.0% | −0.4% | −1.6% | +1.0% |

- 三种发送顺序取平均后，各项尾部指标都在实测自身的 95% 区间内；唯一系统性的偏差是 c=64 的 TPOT P90（三种顺序平均 −9.6% ± 2.8%），原因是多 rank 同时 decode 时每步定价略低，下一次校准会补上这类负载。
- c=128 的 TTFT P90 受突发时请求落到哪个 rank 的随机性影响大：同一并发的两次实测之间就差 23%。

### 各指标误差一览

前五列是主要指标，带 (2nd) 的是次要指标。

![overview](png/dsv41_cellA_overview.png)

### 各并发的时间曲线（静态截图，交互版见上面的链接）

| c | 图 |
|---|---|
| 1 | [dsv41_cellA_c1.png](png/dsv41_cellA_c1.png) |
| 8 | [dsv41_cellA_c8.png](png/dsv41_cellA_c8.png) |
| 32 | [dsv41_cellA_c32.png](png/dsv41_cellA_c32.png) |
| 64 | [dsv41_cellA_c64.png](png/dsv41_cellA_c64.png) |
| 128 | [dsv41_cellA_c128.png](png/dsv41_cellA_c128.png) |
| 256 | [dsv41_cellA_c256.png](png/dsv41_cellA_c256.png) |
| 512 | [dsv41_cellA_c512.png](png/dsv41_cellA_c512.png) |

### 数据来源

- 实测记录 `real_pw_c*` 与 Prometheus 报告 `prom_real_pw_c*`，校准记录 `cal_theory1`，定律 `store_cal_theory1_pg2`，都在 MI355X 节点 005 的 `~xingjche/perzhang/data/dsv41-mi355/` 下。
- 对比图由 `build_dash.py` 生成：实测与仿真都取同一 Prometheus 导出器的面板，按第一个正式请求对齐时间。

## 单机 cell A：其他模型（进行中）

按同一套校准流程（step law 的校准方式与 DeepSeek-V4.1-Flash 完全一致），按 recipe 配置，aiperf 窗口不变：

| 模型 | 部署 | 状态 |
|---|---|---|
| MiniMax-M3.1-Flash | 单机 4 卡 TP4，DSpark 7 个 draft（接受长度强制为实测均值 3.13），fp8 KV，前缀缓存开，最大长度 1048576 | 冒烟测试与内存读数已完成；7 个并发的实测和校准排在 005 的队列中 |
| Kimi-K3.1 | 单机 8 卡 TP8 + DCP8，bf16 KV，KDA 循环状态，前缀缓存开，最大长度 1048576 | 权重已下载；排在 M3.1 之后 |
