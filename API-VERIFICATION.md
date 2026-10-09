# API 核验记录

核验于 2026-10-09，依据官方 `llms.txt → llms-calendar.txt / llms-docs.txt / llms-authenticate-and-authorize.txt → API .md` 文档层级，以及本机 `lark-cli schema calendar.events.patch`。文档契约核验后，已通过已登录的飞书 CLI 完成真实日历上传、更新和 GET 读回测试；完整招聘按钮链路仍未验收。部署工作区保存独立验收记录。

## 已确认的请求契约

| 环节与官方文档 | 方法 / 路径（均以 `/open-apis` 起始） | 核验结论 |
|---|---|---|
| [创建日程](https://open.feishu.cn/document/server-docs/calendar-v4/calendar-event/create) | POST `/calendar/v4/calendars/:calendar_id/events` | 有 attachments；file_token 要经素材上传生成；上传 parent_type 写 calendar；parent_node 为同一日历 ID；查询参数 idempotency_key 32–128 字符，在应用/日历维度唯一；创建接口不添加参与人 |
| [更新日程](https://open.feishu.cn/document/server-docs/calendar-v4/calendar-event/patch) | PATCH `/calendar/v4/calendars/:calendar_id/events/:event_id` | 有 attachments 与 is_deleted；请求说明 parent_type 写 calender，与创建接口/响应说明冲突；组织者可修改可编辑字段，普通参与人仅限部分个人字段 |
| [获取日程](https://open.feishu.cn/document/server-docs/calendar-v4/calendar-event/get) | GET 同一事件路径 | 返回 event、organizer_calendar_id、attachments 中 token、file_size、name、is_deleted；用于定位与验收 |
| [日历信息](https://open.feishu.cn/document/server-docs/calendar-v4/calendar/get) | GET `/calendar/v4/calendars/:calendar_id` | 返回 role 与 type；更新要求 writer/owner 且 primary/shared |
| [素材上传](https://open.feishu.cn/document/server-docs/docs/drive-v1/media/upload_all) | POST `/drive/v1/medias/upload_all`，multipart | file_name、parent_type、parent_node、size、file 二进制；普通上传 size 最大 20971520；返回 data.file_token；枚举未列出 calendar 或 calender，必须实测 |
| [素材下载](https://open.feishu.cn/document/server-docs/docs/drive-v1/media/download) | GET `/drive/v1/medias/:file_token/download` | Bitable 源 token 可用于下载；高级权限需 extra；HTTP 403 表示资源权限不足 |
| [素材概述-extra](https://open.feishu.cn/document/server-docs/docs/drive-v1/media/introduction) | 下载查询参数 extra | bitablePerm 包含 tableId，attachments 必须按 field ID → record ID → token 数组构造；通过 URL 参数编码；本服务自己构造不信任上传的链接 |
| [批量读记录](https://open.feishu.cn/document/docs/bitable-v1/app-table-record/batch_get) | POST `/bitable/v1/apps/:app_token/tables/:table_id/records/batch_get` | record_ids 1–100；返回 records、forbidden_record_ids、absent_record_ids；服务只取一个确定记录并检查 ID |
| [列字段](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-field/list) | GET `/bitable/v1/apps/:app_token/tables/:table_id/fields` | 返回字段名、field_id、type；遍历分页；附件字段 type=17 |
| [更新记录](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/update) | PUT `/bitable/v1/apps/:app_token/tables/:table_id/records/:record_id` | 支持增量更新 fields；本服务只发送配置的状态/ID 字段，成功后另读确认 |
| [添加参与人](https://open.feishu.cn/document/server-docs/calendar-v4/calendar-event-attendee/create) | POST `/calendar/v4/calendars/:calendar_id/events/:event_id/attendees` | 创建后另调用；企业用户使用 type=user、user_id=当前应用 open_id，user_id_type=open_id；need_notification 控制 Bot 通知 |
| [读取参与人](https://open.feishu.cn/document/server-docs/calendar-v4/calendar-event-attendee/list-2) | GET 同一参与人路径 | 返回 items、user_id、rsvp_status、has_more/page_token；邀请前排查已有人，邀请后读回 |
| [获取授权码](https://open.feishu.cn/document/authentication-management/access-token/obtain-oauth-code) | GET `https://accounts.feishu.cn/open-apis/authen/v1/authorize` | client_id、response_type=code、redirect_uri、scope、state；校验 state；回调 URL 必须后台配置 |
| [用户 token](https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token) / [刷新](https://open.feishu.cn/document/authentication-management/access-token/refresh-user-access-token) | POST `/authen/v2/oauth/token` | client_id/client_secret；authorization_code 或 refresh_token grant；响应 token 字段位于顶层；offline_access 才返回刷新 token；refresh token 单次使用，须保存新值 |
| [租户 token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal) | POST `/auth/v3/tenant_access_token/internal` | app_id、app_secret；返回顶层 tenant_access_token、expire；仅企业自建应用路径 |

## 不能混用的 token

源附件 `file_token` 只作为 Bitable 下载标识使用。上传到目标日历后得到新 token，才提交日程 attachments。`app_token` 是 Base 标识，不是访问 token；日历 ID 也不是素材 token。`user_access_token` / `tenant_access_token` 决定调用身份，不能因 CLI 已登录就认为部署服务已获同一身份。

下载素材的权限 scope 与资源权限两层都要满足；开启高级权限的表格可能允许记录查询而不允许附件下载。应用 scope 不会自动授予日历访问权，也不会自动给面试官访问简历的权限。

## 文档歧义与不可承诺项

1. `calendar` / `calender` 冲突，以及上传枚举缺失：实现默认采用 calendar。已在 Fitz 的主日历实测上传与 PATCH 成功；这是当前应用、身份和日历的结果，不能保证所有租户或身份的行为。无需改变源文件 token 形态，也不自动试错上传。官方 CLI patch schema 同样出现请求 calender / 响应 calendar，不能用 schema 消除矛盾。
2. 日程附件合计 25 MB 来自创建/更新接口；普通素材上传单文件 20 MB 来自上传接口。两者不是同一个限制。本版不实现分片上传。
3. 更新 attachments 对未传旧 token 的处理、重复同 token 的幂等行为、附件最大数量：没有完整明确契约。本版合并全量未删除 token、限制一次读取来源最多 20 个（这是服务自身限制，不冒充飞书限制）、真实读回并检查。必须测试旧附件保持、重复不增加和面试官能打开文件。
4. 未核验 ETag/版本比较交换机制。服务自己的单 worker 可串行，外部并发仍有窗口；发现旧 token 丢失会失败，不能声称原子保护。
5. 上传素材没有本版可用的服务端幂等键。上传成功后本地落库前断电/超时可能留下未关联素材，下次重试会重新上传。正常映射保存后不会重复上传；不会猜 token 去删除孤立素材。
6. 创建幂等键保存期限文档未明确。本版有本地创建映射与官方 key 两层；数据丢失/长期重放不能保证不重复。
7. 原生节点是否暴露返回变量、流程创建身份、服务身份权限：已读实际工作流：现有人力面和终面的 HTTP 节点分别引用原生节点 scheduleId/calendarId；初试截图选择 Fitz 日历。CLI 对该主日历为 owner；原生流程创建的具体业务日程尚未完成下载与同步验收。
8. OAuth 页面支持登录与授权，但未做真实授权联调。token 刷新网络结果未知时采取停止并重新授权；没有把单次刷新 token 当作可重复请求。
9. 本版仅添加 PDF/.doc/.docx，文件签名作基础检查，不能替代病毒扫描或确定 Word 文件内部有效性。无自动撤销、删除、简历替换、历史附件清理功能。
10. API 模式仅企业用户参会人；不实现会议室预定、外部邮箱、重复日程。原生模式拒绝重复/例外日程，避免不明确的范围修改。

## 频控及 API 成功判断

Calendar 通常 1000 次/分钟、50 次/秒，Bitable 读记录/字段 20 次/秒；素材文档标注特殊频控（素材概述常用 5 QPS/10000 次每天）。本服务串行并对素材请求限速为每秒最多 2 次；这不能突破应用级配额。429/5xx 和已知限频码采用有界任务重试。

OpenAPI 响应以 `code=0` 判断业务成功；CLI 使用独立 `ok=true` 信封，两者不混用。文件下载按 HTTP 成功、大小与基础文件签名判断，避免把错误 HTML 当成简历。

## 真实上线前必填验收记录

- 测试应用与身份：________（不填 Secret/token）
- 原生节点有完整 IDs：________
- 实测 parent_type：________，上传成功安全错误码/结果：________
- 日程组织者与编辑权限：________
- 旧附件保留，新附件 GET 回读：________
- 相同请求重放，附件无重复：________
- 面试官账号可打开 PDF/Word：________
- Base 成功状态读回（如启用）：________
- 服务重启后队列/去重保留：________

已完成 Fitz 主日历的合成 PDF 上传、附件 PATCH、旧附件合并保留、相同 token 重放无重复及其他字段不变的 GET 读回。其余真实验收尚未执行。部署工作区保存独立验收记录。
