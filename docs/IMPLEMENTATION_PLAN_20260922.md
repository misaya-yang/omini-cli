# Omni HomeVlog 后续开发与功能测试计划

> **后续更新（2026-09-22）：** 用户已将重点调整为容易操作的本地网页工作台，取消本轮 30 秒人物验收要求。网页选图、生成、预览、修改、版本与下载已实现，见 [STUDIO.md](STUDIO.md)。下文为之前阶段的计划/验收快照；旧的 GET 阻塞已解决：Vertex `stream=true` 返回视频位于 `step.delta`，已通过真实远端恢复及异步参考图生成恢复。新增视频 POST 仅 1 次（3 秒参考图），网页验收全部用本地替身，不再消耗模型调用。


日期：2026-09-22。代码基线：`2ac1ac9`，另含本文记录的本轮改动。

原始需求：`/Users/misaya.yanghejazfs.com.au/Downloads/GEMINI_OMNI_HOMEVLOG_AGENT_CODEX_PLAN_20260921.md`。

## 执行更新（2026-09-22，后续轮次）

用户已授权继续实现和简单真实验收。最新实现、测试及未通过项以 [ACCEPTANCE_20260922.md](ACCEPTANCE_20260922.md) 为准；下文保留制定计划时的基线，不能当成当前状态。

- M1–M3：已落地主要代码及回归测试，覆盖能力持久化、严格判断、版本评审、预算恢复、审批导出、任务锁及真实媒体输入。
- M4：限定成本验证了 3→6→9 秒原生链和 9 秒编辑，共 5 次视频请求（包含 1 次参数失败）。另做 1 次 Critic 调用。
- M4 阻塞：两次只读查询恢复均遇到服务端错误，没有新生成兜底。
- M5：更新依赖锁、安装/恢复/限制文档及验收证据；未运行五组人物质量 A/B。
- 30 秒人物创作、另一 provider、上传素材/首尾帧等付费矩阵尚未验收；本次按用户“简单测试”要求控制调用，不标记为完成。

## 1. 结论与本轮范围

当前仓库已经有 CLI、两种 provider、规划/评审、状态机、预算、存储、恢复及测试。
后续应在现有架构上补齐真实闭环，不能再按“从零实现 Phase 0–3”安排工作。
完成标准是：通过 CLI 从素材进入同一条 30 秒原生视频链，发生中断后能恢复，
评审与人工决策生效，最终视频和谱系可导出，调用与预算可核对。

本轮完成代码核查、离线测试基线及两个小范围修复；后面的里程碑是待执行任务，
不代表已经实现或经过真实生成验证。当前没有运行付费 API 测试。

### 本轮已完成

- 修复离线测试读取本机 `.env` 和业务环境变量的问题；显式 dotenv 用例仍可指定测试文件。
- live 测试保留自身的凭据与 opt-in 控制，不再被离线 fixture 清空配置。
- 修复累计视频时长统计：10 → 20 → 30 秒的原生链，应显示 30 秒，不是 60 秒。
- Mock provider 改为返回累计视频；编辑保留父视频长度。
- 集成回归检查最终 MP4 时长、状态摘要和请求秒数，防止以三个独立短片模拟原生续接。

## 2. 实测基线与证据等级

| 项目 | 当前结果 | 能证明什么 |
|---|---|---|
| 修改前 pytest | 344 passed / 3 failed / 4 skipped | 三处恢复用例受开发机配置污染 |
| 修改后 pytest | 347 passed / 4 skipped | 离线单元与 Mock 集成通过；不是模型质量验收 |
| Ruff | 通过 | 静态规则通过 |
| mypy | 58 个源文件通过 | 类型检查通过 |
| Python | 3.12.12 | 当前虚拟环境可用 |
| ffmpeg / ffprobe | 当前 PATH 未发现 | 关键帧和完整 Critic 评审仍有环境缺口 |
| 根目录 CAPABILITY_REPORT.md | 9 月 21 日鉴权/路由 PASS，生成项 SKIPPED | 不能据此宣称 edit、reference、30 秒链已验证 |
| README / LIMITATIONS | 记载 3.008 → 6.016 秒续接及多次历史实验 | 属于历史记录；本轮未逐份核验原始响应及视频 |
| Gemini API | 有适配器，未见完整验收证据 | 保持“实现但未验收” |

证据分开标识：官方声明、历史实测、当次真实实测、Mock 测试、未验证。
不要让“已实现”或 HTTP 200 自动变成“用户功能可用”。

## 3. 已发现的重点缺口

以下为代码阅读确认的实现行为或明确待验证点，不将静态推断包装成 live 结果。

| 优先级 | 缺口 | 代码位置 | 影响 / 验证方式 |
|---|---|---|---|
| P0 | 无能力记录时默认允许 edit/extend，Vertex 默认策略 A | `pipeline/review.py`, `providers/vertex_enterprise.py` | 与“未探测不视为支持”不一致；用未知能力用例验证是否提前阻断 |
| P0 | 续接增长检测结果未参与能力布尔值推导 | `providers/capability_probe.py::_capabilities_from` | 接口成功但视频不增长仍可能标记可续接；构造 HTTP 成功、时长不增长样本 |
| P0 | 免费 doctor 与付费 probe 的结果语义混在同一最新快照中 | `providers/factory.py::latest_capabilities`, `storage/database.py` | 测试先写付费证据再跑免费探测，确认不会以 SKIPPED 抹掉已有测量 |
| P0 | 最终评审遍历全部历史报告 | `pipeline/review.py::review_final` | 已修好的废弃版本仍可能拖住最终验收；按当前接受 artifact 对应评审选取 |
| P0 | export 可直接 finalize；derived 拒绝发生在复制之后 | `cli.py::export`, `pipeline/finalize.py::export` | 可能绕过最终验收，且拒绝后文件已写出；加入 CLI 级失败用例 |
| P0 | 价格默认零值，金额上限可能无实际保护 | `costing.py`, `pricing.yaml`, `budget.py` | 必须区分价格未知和免费；覆盖缺失价格、零值、指定金额上限 |
| P0 | 环境金额上限创建时使用，但需核对恢复后的保存语义 | `pipeline/context.py` | 重启后预算不能放宽；测试环境改变/删除后的恢复 |
| P1 | provider 校验以单次请求时长比较累计视频，仅记录警告 | `providers/base.py::_verify_output` | 明确 seed/edit/extend 的期望长度，缩短或不增长不能当作完成 |
| P1 | 最新视频时长已修复，但 metrics 的 generated_seconds 仍累加版本长度 | `observability/metrics.py` | 区分最终时长、请求秒数、累计输出秒数，避免费用口径混淆 |
| P1 | 恢复需要真实 transport 的 inline/URI/异步响应契约测试 | `pipeline/resume.py`, `providers/base.py` | Mock 恢复不能代替下载、轮询、谱系恢复验证 |
| P1 | 导出清单函数有定义，未找到调用点 | `pipeline/finalize.py::write_manifest_export` | CLI 打印的 final/manifest.json 应实际存在且状态最新 |
| P1 | live smoke 的三个测试各自调用一次生成 | `tests/live/test_smoke_render.py` | 共享同一生成样本完成多项检查，降低重复花费 |
| P1 | 文档互相冲突 | `README.md`, `docs/LIMITATIONS.md` | 有的段落仍说默认策略 C、未连接能力查询，代码已改变；需统一 |

CLI `create` 已调用 `latest_capabilities`，不需要重复实现这一功能。
需要测试的是跨进程持久化、能力证据有效性以及实际使用路径是否一致。

## 4. 开发顺序与里程碑

按 M0 → M1 → M2 → M3 → M4 → M5 执行。每个里程碑一个可独立验证的改动集合。
保持 Python + Typer + SQLite + provider adapter；暂不扩展到网页、队列或部署平台。

### M0：建立可复现基线（本轮已完成主要项）

开发：隔离开发机配置，修正累计时长及 Mock 模型，保留现有凭据文件不动。

验收：默认 pytest 不产生生成请求；全部离线用例通过；Ruff、mypy 通过。
后续补充离线 HTTP 请求意外外发的测试拦截，以及可复现的依赖安装记录。

### M1：能力证据与 provider 契约（第一开发批次）

1. 将“未测量 / 已测成功 / 已测失败 / 配额阻塞”作为能力证据保存，避免只用 bool 丢失原因。
2. 免费诊断不覆盖付费实验的历史证据；保留来源、测量时间、provider/project/model/location。
3. 分离 documented limit 与 measured maximum；不能把 3→6 秒实验写成 30 秒已验证。
4. previous_interaction_id 的 PASS 必须同时满足响应可解析、媒体可用及续接长度增长。
5. 将同一 capability snapshot 传到创建、恢复、决策及 adapter；未知能力给出具体缺口。
6. doctor 增加可选择的探测项、素材输入和总调用上限；一次 seed 可供续接/编辑测试复用。
7. 对真实脱敏 REST 响应做契约测试：JSON/SSE、多 model_output、inline/URI、pending/completed。
8. 保留 Vertex 已测的“previous_interaction_id 不与显式 video task 同传”适配，不全局套用到 Gemini API。

验收：不同项目/模型证据不串用；免费检查不伪造失败或擦除测量；增长失败不能选择策略 A；
CLI 新进程能读取正确快照；未验证能力不通过隐式回退触发生产调用。

交付：能力记录/报告修订、provider 契约测试、更新的 doctor 使用说明。

### M2：恢复、评审、审批和导出闭环（第二开发批次）

1. 按 interaction ID、segment index、attempt index 关联评审，仅当前接受链参与最终判定。
2. 分别测试 seed 编辑、extension 编辑、extension 重生成后的谱系；允许编辑版本追溯到被替代版本。
3. 对 10/20/30/40 秒链校验实际累计长度及完整性，不能只有 parent ID 连上就认为完成。
4. 统一导出前置条件：正式导出需完成规定评审/审批；中间视频仍可由 review 查看。
5. 所有拒绝在写目标文件之前完成；成功输出 final.mp4 和最新 manifest，保留原始字节及哈希。
6. 覆盖请求前、请求已发未回、已获 ID 未下载、下载完成未入账、manifest 已写 DB 未写等中断点。
7. 恢复优先查状态/补下载；未知结果不再生成；恢复时预算、provider/project/model、素材和链不改变。
8. 补 CLI 黑盒测试：create/run/status/review/approve/retry/resume/export；验证退出码、文件及 JSON 输出。
9. 同一任务的两个进程同时 resume 时只允许一个调度付费操作，采用本地任务锁而非引入队列。

验收：CLI 可以从模拟创建走到批准和导出；修复成功不被旧报告阻塞；坏链/未通过评审无法伪装成成片；
每个中断点恢复不重复计费，任务数据可重建。

### M3：预算与评审输入完整性（第三开发批次）

1. 将任务实际生效的预算完整保存，重启、环境变化及手工 retry 均不重置。
2. 价格未知输出 unknown，而非 $0；若设置美元硬上限但无可用估价，调用前给出阻断原因。
3. 区分请求秒数、返回累计时长和费用依据；统计不要把旧版视频长度当新增秒数。
4. 盘点 Director、Sanitizer、Critic 的文本/视觉调用和有限重试成本，单独记录用量与上限。
5. 安装/配置 ffmpeg 与 ffprobe 后检查实际路径；缺失时保留现有降级判定。
6. extension 的评审聚焦新增片段和连接处，并提供上一版本末尾、参考图、连续性约束及音频证据。
7. 验证 Critic 真正收到视频/关键帧，而非只收到文件路径或文本；JSON 损坏、模型拒绝均不能放行。
8. 补参考图角色、排序、重复、拼图/时间码和不合格输入用例；不扩大既有产品安全边界。

验收：所有允许支出均能从账本重建；上限不会因恢复放宽；缺媒体证据时不会产生自动 ACCEPT。

### M4：最小真实 API 验收（离线契约通过后）

先沿用显式配置的单个 Vertex 项目；Gemini API 独立验收，不自动切换 provider 或项目。
真实测试采用确定的调用上限；价格未核实前不提供虚假的美元预算估计。

| 批次 | 场景 | 视频调用计划 | 要验收的事实 |
|---|---|---:|---|
| A | 无人物静物 3s/360p seed + 两次 3s 原生续接 | 3 次，不自动修复 | 约 3→6→9 秒、第三链、下载、媒体音轨、ID 谱系 |
| B | 复用 A 的可编辑版本做一次局部 edit | 1 次 | 编辑有效，时长未意外丢失，未要求改变的区域可比较 |
| C | 干净合成或已授权人物素材，10s/720p + 10s + 10s | 基础 3 次；可选最多 1 edit + 1 regenerate | 同一人物与空间、两处连接、真实 30 秒成片 |
| D | C 的恢复与导出 | 预期不新增视频生成 | 重启后查询/下载/审批/导出不重复生成 |
| E | Gemini API 同类最低成本 smoke | 独立最多 3 次 | 单独证明该 surface；不复用 Vertex 结论 |

批次 C 的人物素材及生成预算需由用户指定；没有素材时只能完成无人物技术验收，不能声明人物连续性通过。
批次 B 的 1 次调用预算独立于 A；不把 3 次授权扩展为 4 次。
若服务端状态丢失，需要新 seed，先记录原因并重新纳入预算，不隐含追加调用。
如果本轮总上限选择 8 次，则 A+B+C 的 7 次基础调用后仅剩 1 次可选修复；E 留到独立批次。

所有真实测试保存：脱敏请求/响应、模型和 transport 版本、UTC 时间、interaction 谱系、视频 SHA256、
请求/实测时长、尺寸、音轨、调用次数、usage、估算费用状态、C2PA 检测结果和失败原因。
原始素材及详细运行证据留在私有运行目录，不提交凭据、带签名 URL 或人物素材到仓库。

验收不能只看自动评分：检查开头、两处衔接前后、结尾，并观看完整视频和听取音频。
对静态画面增长但动作重复、跳切、人物重置、字幕/UI 污染均记录失败时间点。

### M5：质量回归与交付文档

1. 保留原计划五类固定场景：喝杯子、起身、短距离走路、餐桌到卧室、手遮镜头。
2. 固定参考素材哈希、角色顺序、prompt、模型、参数及阈值版本，用于 SDK/模型更新前后比较。
3. 单独保存人工观察和 Critic 输出；不把一次抽样通过写成稳定成功率。
4. 统一 README、LIMITATIONS、RECOVERY 和能力报告：删除与当前代码冲突的状态描述。
5. 交付一个无真人隐私的示例任务，包含配置、命令、最终视频和脱敏完整 manifest。
6. 在干净环境验证安装、CLI --help、离线测试和示例重放。

完成 M5 后再评估是否值得加网页审批；界面不作为当前 CLI 验收的替代品。

## 5. 功能测试矩阵

| 层级 | 必测场景 | 判定 |
|---|---|---|
| 单元 | 能力状态/选择、增长检测、预算恢复、当前评审选择、时长语义、输入与 prompt | 正常和拒绝路径均有断言 |
| transport 契约 | REST JSON/SSE、多输出、无视频、错误响应、URI/inline、分页或轮询状态 | 字段来自实际样本/官方契约，不凭空假设 SDK 等价 |
| Mock 集成 | 10/20/30/40 秒、edit/regenerate、预算耗尽、鉴权/429/5xx、Critic 失败 | 状态、谱系、调用次数、文件、账本一致 |
| 崩溃恢复 | 已请求未获 ID、已获 ID 未下载、写盘/DB 中断、并发 resume | 不产生重复生成；未知结果明确停留在可诊断状态 |
| CLI | 创建→状态→审片→批准→恢复→导出，非法参数、丢失任务、--json | 用户命令真正执行所选操作，机器输出可解析 |
| 真实 smoke | 批次 A/B | 真 MP4、长度增长、第三链、局部编辑，无隐式重复调用 |
| 创作验收 | 批次 C/D | 原生 30 秒，同一人物、动作空间连续、审批有效、导出完整 |
| 回归 | 五类固定场景 | 对照素材与旧输出评判，记录失败片段与人工观察 |

## 6. 当前可直接运行的离线验证

在项目根目录：

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/omni-vlog --help
```

不要将现有 `pytest tests/live` 视为已具备全部验收能力：目前 smoke 会重复生成，且未覆盖完整人物链。
M1/M4 补齐后，再写入已经实际执行成功的 live 命令和具体输出位置。

## 7. 依赖、节奏与完成定义

建议按三个离线开发批次加两个真实验收批次推进；每批修复、测试、记录结果后进入下一批。
这是工作拆分，不是已完成承诺，也不保证外部配额、模型响应时间或视频质量。

当前外部条件：ffmpeg/ffprobe 未在 PATH 发现；人物参考素材未指定；真实调用范围待选择；
付费调用前还需确认当前项目访问权及配额，昨天的文档记录不能代替今天的结果。

最终 DoD：

- [x] 离线基线恢复为全通过。
- [x] Mock 与状态摘要使用累计原生视频时长。
- [ ] 能力证据可跨进程使用，UNKNOWN 不变成 PASS。
- [ ] 评审、修复、审批、导出和恢复经过 CLI 级验收。
- [ ] 金额未知与零成本分开，预算在恢复后仍有效。
- [ ] 完成真实 seed、edit、至少 20 秒链及中断恢复的原计划 Phase 0 退出条件。
- [ ] 完成真实 30 秒人物创作与完整观看验收。
- [ ] 最终 MP4、最新 manifest、评审、调用账本及示例可交付。
- [ ] Gemini API 独立标明验收结果，不能随 Vertex 一起宣称通过。

## 8. 当日官方资料核对

2026-09-22 已查阅：

- [Gemini API Omni 文档](https://ai.google.dev/gemini-api/docs/omni)：说明 previous_interaction_id 状态编辑及原生尾部续接，REST 视频输出在 steps 内；store=false 不能用于后续依赖该状态的编辑。
- [Cloud Omni 1.1 Flash 模型卡](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/omni-1-1-flash)：列出的 Cloud 模型 ID 为 gemini-omni-1.1-flash-preview；不支持 system instructions、structured output 及 agentic video understanding。

原计划中的计费/配额描述不能视为固定事实：当日 Cloud 模型卡消费选项列出 Fixed quota、Provisioned Throughput，Pay-as-you-go 标记不支持；需结合实际项目权限确认。
开发继续使用独立规划/评审模型；保留目前 REST 路径，除非兼容性验证证明有必要，不强制迁移 SDK。
