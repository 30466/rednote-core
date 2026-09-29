# rednote-core 阶段进度与测试清单

本文档记录当前两阶段改造的运行命令、已经验证的功能和仍需实测的问题。只有经过真实小红书页面运行验证的功能才标记为完成。

## 环境与 CLI

```bash
# 安装并同步依赖
uv sync

# 验证已有登录态；失效时打开项目隔离浏览器扫码
uv run python rednote.py login

# 默认让项目隔离浏览器直连，不继承终端代理
uv run python rednote.py export '小红书号'

# 如需显式允许使用终端或系统代理
REDNOTE_DIRECT=0 uv run python rednote.py export '小红书号'
```

项目只使用 `Volume/playwright-profile/` 中的隔离 Playwright Chrome for Testing profile，不连接用户日常 Chrome profile。

## 第一阶段：作品数据爬取

### 运行命令

```bash
# 默认模式：导出完整作品摘要清单，不逐篇打开详情
uv run python rednote.py export '谭思慧'
uv run python rednote.py export '819540154'
uv run python rednote.py export 'https://www.xiaohongshu.com/user/profile/作者ID?xsec_token=...'

# 逐篇抓取标题、正文、标签、发布时间和互动统计等详情
uv run python rednote.py export '819540154' --details

# 限量验证；0 或不传 --limit 表示不限制
uv run python rednote.py export '819540154' --limit 10 --summary-only
uv run python rednote.py export '819540154' --limit 3 --details

# 自定义隔离 profile 或输出目录
uv run python rednote.py export '819540154' \
  --profile-dir Volume/playwright-profile \
  --data-dir data
```

输出文件：

```text
data/{作者昵称}/{作者内部ID}.json
data/{作者昵称}/{作者内部ID}.csv
```

### 已完成并验证

- [x] 支持完整主页 URL、精确昵称和唯一小红书号；遇到完全同名账号时停止并列出候选小红书号。
- [x] 验证服务端登录态，失效时使用项目隔离浏览器扫码登录。
- [x] 抓取首屏和 `user_posted` 分页结构化数据，按作品 ID 去除置顶重复项。
- [x] 根据 `hasMore` 自主决定是否继续分页；只有3篇作品且没有分页请求的账号可正确结束。
- [x] 页面水合后只解包所需 Vue Ref 字段，不再序列化完整响应式依赖图。
- [x] 默认摘要清单与显式 `--details` 详情抓取分离。
- [x] JSON/CSV 不保存容易过期的 `xsec_token` 和 CDN 媒体地址。
- [x] 显示账号解析、分页数量、浏览器切换、文件写入和总耗时。
- [x] Playwright 清理操作带超时保护，避免浏览器窗口关闭后 Python 进程无限等待。
- [x] 小红书号 `819540154`：完整清单154条；完整详情154/154成功、失败0、未执行0，总耗时20分32秒。
- [x] 小红书号 `11519961936`：完整清单3条；完整详情3/3成功、失败0、未执行0，总耗时30秒。

### 唯一待测试问题

- [ ] 在逐篇详情过程中真实触发登录失效或验证码，验证程序能否保持当前页面且不主动刷新；人工扫码/验证后，应稳定识别恢复后的登录态、重试当前作品并继续剩余清单。当前等待上限为300秒。最近的完整回归均未再次触发风控，因此这条路径尚未完成真实验证。

复现时记录：触发的作品序号、页面 URL、页面显示的验证类型、人工操作前后等待时间、是否自动继续、最终各 `detail_status` 数量和总耗时。

## 第二阶段：媒体下载（尚未测试）

第二阶段复用第一阶段的隔离浏览器读取单篇结构化详情，再把媒体地址交给上游下载器。当前代码入口已经存在，但图文、视频和 Live Photo 尚未进行完整真实下载测试。

```bash
# 只解析并打印一篇作品详情，不下载
uv run python rednote.py detail '完整作品URL'

# 下载一篇作品的图片、视频或 Live Photo
uv run python rednote.py download '完整作品URL'

# 指定下载目录
uv run python rednote.py download '完整作品URL' --output downloads

# 可选：显式提供网页版 Cookie 给媒体下载器
uv run python rednote.py download '完整作品URL' \
  --cookie 'a1=...; web_session=...' \
  --output downloads
```

### 待测试

- [ ] 单篇图文作品的全部图片下载。
- [ ] 单篇视频作品的视频、封面以及可获得的最高画质/音质。
- [ ] 单篇 Live Photo 的图片与动态文件下载。
- [ ] 登录态与游客态媒体质量是否一致。
- [ ] 下载失败、重试、已有文件跳过和断点续传行为。
- [ ] 第一、二阶段串联：从 JSON/CSV 筛选作品，再逐篇调用下载命令。

第二阶段单篇能力验证稳定后，再编写 Agent Skill 负责筛选规则、并发数量、失败重试和批量下载报告；core 不固化特定批量筛选规则。
