[English](./README.en.md) · [Website](https://thinkcap.lei6393.com) · [GitHub](https://github.com/SuperMarioYL/thinkcap)

<picture>
  <source media="(max-width: 600px) and (prefers-color-scheme: dark)" srcset="./assets/presentation/hero-mobile-dark.svg">
  <source media="(max-width: 600px)" srcset="./assets/presentation/hero-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="./assets/presentation/hero-dark.svg">
  <img src="./assets/presentation/hero-light.svg" width="960" alt="Hero diagram">
</picture>

# thinkcap

**改变实时流之前，先检查推理预算。**

thinkcap 按任务类别分析可见 think 块文本，保存观测并生成供提示型流式代理使用的预算策略。

## 为什么需要它

重命名与重构可能产生不同长度的轨迹。先分别记录观测并保留计数来源，再用策略识别流何时越过所选预算。

- **区分任务类别** — 各任务类保留自己的观测分布。
- **处理跨块分隔符** — 解析器跨 chunk 跟踪不完整标记。
- **保留计数来源** — profile 区分 tokenize 结果与估计。

## 架构

<picture>
  <source media="(max-width: 600px) and (prefers-color-scheme: dark)" srcset="./assets/presentation/architecture-mobile-dark.svg">
  <source media="(max-width: 600px)" srcset="./assets/presentation/architecture-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="./assets/presentation/architecture-dark.svg">
  <img src="./assets/presentation/architecture-light.svg" width="960" alt="Architecture diagram">
</picture>

profiler 发送五项任务或使用预设响应，提取标记的 think 块并生成各类 p90 预算。ThinkStreamParser 跟踪跨块分隔符和 UTF-8 字节；FastAPI 代理分类请求、转发 SSE 并记录越界决策，目前不会改变上游生成。

| 组件 | 职责 |
| --- | --- |
| `Task profiling` | thinkcap/profiler.py |
| `Policy + observations` | policy.py; budget_store.py |
| `Incremental parser` | trace_parser.py |
| `Advisory SSE proxy` | thinkcap/proxy.py |

## 安装与快速上手

使用仓库清单声明的运行时版本。以下源码安装步骤可复现随仓示例。

```bash
git clone https://github.com/SuperMarioYL/thinkcap.git
cd thinkcap
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

支持 Python 3.10+；此处安装步骤使用 Python 3.12。先运行 mock profiling，再向真实解析器提供完整合成跨块分隔符流。

```bash
.venv/bin/python examples/presentation_demo.py
```

## 实际运行示例

<picture>
  <source media="(max-width: 600px) and (prefers-color-scheme: dark)" srcset="./assets/presentation/process-mobile-dark.svg">
  <source media="(max-width: 600px)" srcset="./assets/presentation/process-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="./assets/presentation/process-dark.svg">
  <img src="./assets/presentation/process-light.svg" width="960" alt="Process diagram">
</picture>

Mock profiling emits five policy classes; a synthetic open think block crosses the budget and the closed block does not trigger.

```text
mock policy classes: bugfix, new_file, refactor, rename, default
inside think: True
estimated threshold reached: True
after closing block: False
completed think blocks: 1
```

完整命令与输出保存在 [docs/demo-results.json](./docs/demo-results.json). 输入和复现代码均随仓提供。

![已有终端录制](./assets/demo.gif)

保留已有录制供参考；上方文字示例给出当前可复现的操作。

## 用法

安装后在仓库根目录运行以下命令；处理自己的数据时替换相应路径。

```bash
thinkcap probe --mock --policy policy.json --budgets budgets.json
# With your compatible local server:
thinkcap probe --server http://localhost:8080 --suite coding-mini --policy policy.json
thinkcap serve --upstream http://localhost:8080 --port 8088 --policy policy.json --dry-run
```

## 配置

任务类包括 rename、bugfix、refactor、new_file、default。probe 提供 --server（或 THINKCAP_SERVER）、--model、--suite、--policy、--budgets；serve 使用 --upstream（或 THINKCAP_UPSTREAM）、--port、--policy。default 回退预算为 2048 token。代理强制流式，绑定 0.0.0.0 且无认证层，应部署于适当受控网络；/health 只报告配置，不验证上游就绪。

## 集成与职责分工

<picture>
  <source media="(max-width: 600px) and (prefers-color-scheme: dark)" srcset="./assets/presentation/integrations-mobile-dark.svg">
  <source media="(max-width: 600px)" srcset="./assets/presentation/integrations-mobile-light.svg">
  <source media="(prefers-color-scheme: dark)" srcset="./assets/presentation/integrations-dark.svg">
  <img src="./assets/presentation/integrations-light.svg" width="960" alt="Integrations diagram">
</picture>

根据工作流选择输入与输出路径。本文本地示例验证其中明确说明的子流程。

| 路径 | 已实现职责 |
| --- | --- |
| Marked content deltas | im_start think / im_end |
| llama.cpp HTTP | Probe and streaming upstream |
| policy.json | Per-class budget rows |
| budgets.json | Recorded observations |
| FastAPI health | Configured upstream and classes |

## 限制与后续方向

- 当前代理不会注入 token、抑制推理或停止上游计算。--accept 只改变日志/状态，不是强制预算模式。
- 实时决策按字节除以四估算 token，离线回退按字符估算。两者均为估计，五任务 mock 观测不是标定后的模型预算。
- 解析器要求 content 中出现字面 <|im_start|>think 与 <|im_end|>；其他推理字段或模板需要独立支持。示例未验证当前 Qwen 部署。

真实上游预算控制与通过率一致性研究仍是后续工作；当前交付能力是 profiling 与提示型观测。

## 许可与贡献

许可见 [LICENSE](./LICENSE). 反馈问题时请提供最小输入、执行命令和实际输出。
