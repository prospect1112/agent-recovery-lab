# Coding Agent Recovery Lab

**A paired recovery benchmark prototype, not a bug-fixing product.** Compare whether a stalled coding assistant should keep its context or restart, with or without rolling back its local changes. Runs without an API key in mock mode.

## 复现

```sh
python replay.py --prepare-only
python replay.py --offline --routes continue restart rollback rollback-full
```

需要 Python 3.12+ 和 Git。第一次准备下载固定提交；使用同一份 `--cache`（默认 `.runs/cache`）可离线复现。缺对象时 `--offline` 直接报错，不访问上游。默认 mock、无模型费用。可用 `--case` 选择一个或多个案例、`--budget` 调动作上限、`--timeout` 调单次测试限时、`--checkpoint` 按前缀编辑序号选回退点、`--repeat` 重复。用相同参数和 `--session .runs/pilot` 重跑会跳过已完整写入的案例轮次；中断的轮次从头重来，付费请求可能重复。不要并发写入同一 session。GitHub CI 也使用这个准备/离线两阶段流程。

| 案例 | 缺陷类型 |
|---|---|
| [boltons #563](https://github.com/mahmoud/boltons/pull/563) | 一次性迭代器与列表键的长度处理 |
| [attrs #843](https://github.com/python-attrs/attrs/pull/843) | 父类的解析缓存使子类跳过类型标注解析 |

各案例放在 `cases/<id>/`，包含固定的上游与参考提交、目标函数、前缀编辑、可见和留出测试。基线必须通过全部不应退化的测试，并在应修复的测试上失败；参考修复必须全部通过。框架不对任何候选路线规定“应通过”或“应失败”。留出测试文件随仓库发布，因此它们不是保密数据集；这是运行时输入隔离，不是防记忆或防刷分设计。

## 对照设计

| 路线 | 起始工作区 | 送给执行器的上下文 |
|---|---|---|
| `continue` | 停滞状态 | 摘要 + 可见操作记录 |
| `restart` | 停滞状态 | 同一摘要 |
| `rollback` | 较早的检查点 | 同一摘要 |
| `rollback-full` | 较早的检查点 | 摘要 + 同一可见操作记录 |

停滞前缀目前是脚本构造的，并用两次真实测试确认同一错误。它不等于真实 Agent 自然卡住，`continue` 也不是恢复真实会话句柄。修改和运行可见测试各计一个动作。评测器在干净基线重放候选补丁。路线顺序按案例与轮次轮转；恢复耗时不包含数据准备和独立验证。

## 可选：真实模型执行器

```sh
docker pull python:3.12-slim
python replay.py --case attrs-843 --executor deepseek --docker-image python:3.12-slim --timeout 30 --budget 2 --output-tokens 1024
```

这会使用环境中的 `DEEPSEEK_API_KEY` 发起付费请求。模型经 [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion) 决定替换单个函数、跑可见测试或提交；没有终端、文件浏览或网络工具。API 只接收任务、候选函数及可见信息，不接收参考补丁或留出测试。候选代码一律在固定镜像 ID 的独立、无网络、非 root、只读容器中运行，有内存、CPU、进程数和测试限时；留出测试目录只挂载到独立的最终验证容器，不挂载到可见执行环境。最终验证时测试和候选代码同进程，**不能当作恶意参赛代码的防作弊边界**。无 Docker 则不允许运行模型路线。适用范围仅限可信的内置普通 Python 维护案例，不是任意项目的通用 Agent。

每条路线硬限动作数和 API 生成 token 数，输入 token 单独记录；输入长度并不配平，因为它是研究变量之一。通过 `--prices CACHE_HIT CACHE_MISS OUTPUT` 可按 USD/百万 token 估算费用，未填或统计不全时显示未知，不假填零。模拟不发 API 请求。本项目没有用该适配器跑付费实验的结果，API 协议测试使用替身。

## 结果怎么读

`summary.json` 给出通过数/总试次、超时、错误、动作和耗时均值、成本，以及**假修复率**（在可见通过且得到评测判决的试次中，留出失败的比例）。两两比较另记“仅第一条通过 / 仅第二条通过 / 都通过 / 都未通过”，避免汇总成功率掩盖彼此抵消的结果。超时/错误仍留在成功率的总试次分母中。每次报告另含补丁、起点哈希、预算、用量和测试记录。不要直接公开原始运行日志。

两个案例、每例两次的 mock 验收：继续 0/4、重开 2/4、回退 2/4、全上下文回退 0/4。重开路线可见测试通过 4/4，其中 2/4 被留出测试否决；回退通过 boltons 但未通过 attrs。**这些都是事先写入 fixture 的行为，只证明框架能区分结果，不是策略效果数据，没有统计显著性的含义。**

下一步：采集自然停滞前缀，预先登记配对指标，用 8–12 个普通维护案例跑成本试点；按仓库设留出集，再估计策略差异。最值得验证的信号是检查点之后新出现的可见回归，能否预测“回退比保留工作区更好”。相关工作：[Fail-Fast, Restart-Smart](https://arxiv.org/abs/2608.03222)、[AgentRewind](https://arxiv.org/abs/2608.14380)、[RippleCP](https://arxiv.org/abs/2610.09088)。

项目代码采用 [0BSD](LICENSE)。不随仓库打包上游源码或补丁；运行时下载的代码仍按其 [boltons](https://github.com/mahmoud/boltons/blob/6270b5e35ed902859df6188970eec5bee5ab73d1/LICENSE) 或 [attrs](https://github.com/python-attrs/attrs/blob/f31bb2850a520993396ebdae598056fb3e3af6f8/LICENSE) 上游许可。
