# API参考

启动后访问 `/docs` 获取真实OpenAPI Schema。所有接口JSON编码，时间ISO 8601且带时区，金额为人民币整数分。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | /health | 进程状态、数据模式、版本 |
| POST | /api/v1/chat | 规划、政策问答或偏好提取 |
| POST | /api/v1/sessions | 创建会话 |
| GET | /api/v1/sessions | 最近100个会话 |
| GET | /api/v1/sessions/{sid} | 会话最近100条消息 |
| DELETE | /api/v1/sessions/{sid} | 删除会话及关联消息与方案 |
| GET | /api/v1/preferences | 读取本地用户长期偏好 |
| PUT | /api/v1/preferences | 全量替换偏好；未提供字段使用默认值 |
| DELETE | /api/v1/preferences | 恢复默认偏好 |

## 对话请求

```json
{"message":"明天晚上从北京海淀到天津滨海新区，预算50元，不要打车"}
```

省略session_id会创建新会话；后续同会话传回返回的session_id。未知会话返回404。消息最大3000字符，空串返回422。

`constraints`可选，用于无法可靠解析的复杂条件。传入时为**完整权威约束**，不再合并保存偏好。范例：

```json
{
  "message":"请按结构化条件规划",
  "constraints": {
    "origin":"夜间起点", "destination":"夜间终点",
    "depart_after":"2026-09-15T22:30:00+08:00",
    "depart_before":"2026-09-15T23:00:00+08:00",
    "arrive_by":"2026-09-16T01:30:00+08:00",
    "budget_cents":10000, "cycling_acceptance":2,
    "excluded_modes":["taxi"], "max_walk_m":3000
  }
}
```

响应字段：session_id、status、answer、plans、constraints、metadata、sources。status为ok/no_results/clarification/degraded/policy/memory之一；无可行候选时plans为空，不制造“兜底班次”。所有业务状态返回200；Schema失败422、鉴权失败401、会话不存在404。

Plan包含id、label、legs、total_cost_cents、total_minutes、transfers、score、risks；score越低越优。Leg包含起终点、模式、服务号、发车/到达、金额、距离、来源与库存状态。风险同时出现在结构化方案和自然语言答案里。

## 多站活动请求

`itinerary` 与 `constraints` 互斥。自然语言复杂行程会保存在 `metadata.itinerary_draft`；同一 `session_id` 补充日期、活动时长或门店后继续更新草稿。缺字段时返回 `clarification`，不会降为单一起终点或仅保存偏好。

```json
{
  "message": "按顺序规划这些活动",
  "itinerary": {
    "origin": "haidian",
    "depart_after": "2026-09-26T18:00:00+08:00",
    "arrive_by": "2026-09-26T23:00:00+08:00",
    "allow_overnight": false,
    "budget_scope": "transport",
    "budget_cents": 20000,
    "preferences": {"excluded_modes": ["taxi"]},
    "stops": [
      {"label": "用餐", "locations": ["beijing"], "duration_min": 30},
      {"label": "到达", "locations": ["binhai"], "duration_min": 0}
    ]
  }
}
```

这是使用演示节点的测试输入。活动可设置固定 `start_at`（含时区）、`requires_start_time`、`duration_min` 和 `cost_cents`。单站 `locations` 为可选地点集合，`stops` 才是顺序访问；不确定的时间允许为 null，补齐后进入搜索。上限为 8 站、总行程 7 天，每次段间查询限 48 小时。

返回 Plan 新增 `activities`，包含地点、开始/结束时间和用户提供的费用；交通费用与总费用按 `budget_scope` 区分。`metadata.place_candidates` 为高德真实 POI 候选，展示来源但不代表已核验营业时间或可用交通。

metadata包含迭代轮数、保守Token预留、API报告的实际Token、节点轨迹、搜索扩展数/截断、错误码和耗时。不能用demo零Token消耗来宣称LLM节省比例。

## 时间角色与硬限制

`Constraints` 和 `ItineraryDraft` 新增 `arrival_priority`：自然语言只给到达期限时启用，避免把“4点到”当作“4点出发”。自然语言单程会重新查询倒推的出发候选；结构化请求仍使用调用者明确提供的时间窗口。

二者均支持 `max_walk_m`（默认5000米）、`max_bike_m`（默认12000米）和 `max_transfers`（默认null，无显式硬上限）。限制累计到全程；换乘计算为交通工具乘坐段数减一，不计步行和骑行。`transfer_tolerance` 仍为软偏好。

`metadata.pending_time_query` 保存有歧义的到达时间请求；同会话回复“上午”或“下午”会恢复原始请求。`metadata.intelligent_plan` 包含到达期限、候选、淘汰原因和数据缺口，地图结果仍标记为参考数据。

## 验证示例

```powershell
$body = @{message='明天晚上从北京海淀到天津滨海新区，预算50元，不要打车'} | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/api/v1/chat -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

配置API_ACCESS_TOKEN后，业务接口需要 `Authorization: Bearer <token>`。这仅保护同一个本地用户的数据，不是多租户登录系统。本版本没有SSE流式响应、方案收藏和实际购票接口。
