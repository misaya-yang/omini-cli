# 2026-09-22 开发与低成本验收记录

> **后续更新（2026-09-22）：** 用户已将重点调整为容易操作的本地网页工作台，取消本轮 30 秒人物验收要求。网页选图、生成、预览、修改、版本与下载已实现，见 [STUDIO.md](STUDIO.md)。下文为之前阶段的计划/验收快照；旧的 GET 阻塞已解决：Vertex `stream=true` 返回视频位于 `step.delta`，已通过真实远端恢复及异步参考图生成恢复。新增视频 POST 仅 1 次（3 秒参考图），网页验收全部用本地替身，不再消耗模型调用。


## 结论

本轮完成 CLI 主流程的能力证据、预算恢复、版本评审、审批导出、任务锁和媒体输入修复，
并做了限定范围的真实验证。**不宣称整个原计划已验收：远端查询恢复遇到服务端错误；
30 秒人物连续性、reference-to-video 和 Gemini Developer API 未在本轮付费测试。**

用户授权“真实验收、简单测试、不要疯狂消耗 API”。本轮视频请求共 **5 次**：
3 次 seed/extend，1 次参数错误的 edit，修复后复用同一视频做 1 次 edit。
另有 1 次独立 Critic 调用、免费鉴权检查和 2 次只读 GET；没有重跑整条链或切换项目。
没有核实完整价格，因此费用为未知估算，不能将 0 当作账单。

## 真实观察

| 操作 | 结果 | 证据 |
|---|---|---|
| Vertex seed，3s/360p/16:9，无人物 | PASS | 3.029333s，640×360，H.264/AAC |
| previous_interaction_id 原生续接 | PASS | 6.037333s，父节点为 seed |
| 从第二轮再次续接 | PASS | 9.024s，第三链可用 |
| 首次编辑 | FAIL，已定位 | 累计视频约 9s，错误请求配置为 3s，服务端 invalid_request |
| 修复后编辑同一视频 | PASS | 配置改为源视频时长 9s，输出 9.024s |
| 局部变化 | 抽帧确认 | 木色桌面变蓝，三处抽帧中窗户、构图和房间保留 |
| 音轨和内容凭据 | 检测到 | 四个成功输出均有 AAC 音轨及 C2PA box；未验证 C2PA 签名或 SynthID |
| Critic 输入 | 调用通过 | 完整视频（含音轨）+ 5 张关键帧；JSON 可解析，未降级 |
| Critic 质量结论 | 不等于自动验收 | 模型给 accept，但静物无人物，身份/解剖分数默认 0.5；不能作为人物质量或策略 ACCEPT 的证据 |
| 服务端 GET 恢复，第 1 次 | FAIL | Internal error encountered |
| 服务端 GET 恢复，延后复查 | FAIL | Deadline expired before operation could complete |

远端恢复失败未触发任何新视频生成。带 SHA256 校验的本地完成回执支持直接恢复已落盘视频；
没有本地回执且 GET 失败时，任务保留待恢复状态，不会重新花钱。

## 已实现的修复

- doctor 记录能力到 SQLite，后续独立 CLI 进程可使用；免费探测保留已测证据。
- 未探测的 seed/edit/extend 不再默认支持；续接能力必须通过媒体长度增长验证。
- doctor 可选择 seed/extend/third/edit、参考图及最大调用数；每次付费派发先落盘。
- provider 使用任务的能力快照；原生续接不要求 GCS，上传源模式不携带冲突的 parent 请求字段。
- 编辑按累计视频长度构造请求、预算和校验；超过 10 秒编辑省略生成时长参数，仍检查输出长度，此长编辑路径未实测。
- 最终评审只读取当前接受版本的报告；被替代版本保留审计记录，不再否决修复结果。
- 编辑中间谱系可经账本追溯；最终链检查段数、父子关系和累计时长。
- 导出要求 COMPLETE；拒绝 derived 输出发生在复制前；同时导出最新 manifest。
- final 审批可在 FINAL_REVIEW 执行；retry 进入正确评审状态，不能跳过未知请求或破坏已存在的后续链。
- 请求结果 ID 在下载前落盘；inline/URI 查询响应均可恢复；已验证本地回执可避免重复 GET。
- 视频 POST 遇到 5xx/超时不自动重试，避免 transport 隐藏重试突破预算。
- run/resume/retry/approve/export 等使用同一进程锁，阻止并发重复派发。
- 预算从授权事件恢复，包含已授权但尚未入账的派发；任务金额上限不受后来的环境配置影响。
- 未知视频价格不显示为零美元；设置金额上限但价格未知时拒绝付费派发。
- Director/Sanitizer/Critic 单独记录调用与 token 用量，默认每任务最多 24 次，重试也计数。
- Critic 实际附加视频字节和音轨，累计续接评审聚焦新增片段；大于 18MB 时明确降级。
- 上传参考图保留本地路径，避免后续 Critic 丢失人物参考。
- concept 模式仅一段，避免隐式扩展；live smoke 三项检查复用一次生成。
- 安装 ffmpeg/ffprobe；离线测试隔离 `.env`、本机媒体工具和 HTTP 请求。

## 验证及产物

最终离线测试结果见本文末尾的验证记录。新增回归覆盖命令行批准/恢复/导出、指定 edit、
concept 费用范围、未知请求不重发、预算恢复、能力合并、跨进程/线程锁、媒体输入和 inline 恢复。

运行产物位于私有目录 `outputs/acceptance_20260922/`：

- `chain.mp4`：9.024 秒原生累计视频。
- `edited.mp4`：同一视频的局部编辑结果。
- `chain_frames.jpg`、`edited_frames.jpg`：抽帧对照。
- `live_report.md`、`live_capabilities.json`：保留首次 4 次请求的原始验收结果，含编辑失败。
- `edit_retest.json`、`verified_capabilities.json`：修复后的第 5 次请求证据。
- `critic_result.json`、`critic_calls.json`：唯一一次评审调用及用量。
- `recovery_result.json`、`recovery_retry.json`：两次只读失败记录。
- `acceptance_manifest.json`：本次技术验收的谱系、哈希和调用计数；它不是生产任务 COMPLETE 清单。

## 未验收或明确不支持

- 服务端查询恢复仍被当前 API 错误阻塞，不能声称“任意阶段均已真实恢复”。
- 30 秒人物成片、五类人物质量 A/B、reference-to-video、first/last-frame、steps replay、GCS delivery、Gemini API 未执行付费验收。
- 无人物短链只能证明连续调用和技术链路，不能证明人物身份稳定或长视频成功率。
- 金额限制目前针对视频估算；文本/视觉调用采用独立次数上限及 usage 账本，不宣称全栈美元硬上限。
- 网页、队列、Cloud Run 属于原计划后续可选阶段，本轮仍保持 CLI 范围。

## 最终离线验证记录

- `python -m pytest`：**367 passed, 4 skipped**。跳过的是 opt-in live pytest；本轮真实验收通过单独受限命令执行，不包含在这 367 项中。
- `ruff check src tests`：通过。
- `mypy src`：59 个源文件通过。
- `git diff --check`：通过。
- `uv build`：sdist 和 wheel 构建成功。
- `/tmp` 下独立 Python 3.12 虚拟环境按 `requirements-dev.lock` 安装 wheel，离开源码目录后导入及 `omni-vlog --help` 成功。
- 代码保留在当前工作区；本轮未提交、推送或部署。
