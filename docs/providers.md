# 真实数据接入契约

本项目没有假定可直接调用的“12306开放API”。`DATA_MODE=live` 时要求 `PROVIDER_URL` 指向用户已授权的供应商适配网关。无供应商配置或返回失败时，报告缺数据，不使用演示图兜底。

所有请求通过POST JSON，Header `Authorization: Bearer ${PROVIDER_API_KEY}`；超时、最多2次重试和缓存由工具基类统一处理。URL由部署配置控制，LLM不得改写。

| 端点 | 输入 | 输出Pydantic模型 |
|---|---|---|
| /geocode | address, at（ISO时间） | Node |
| /transit_query | origin, destination, depart_after, arrive_by | Network |
| /weather_query | location, date | Weather |
| /web_search | query | WebResults |

准确Schema由 `src/domain.py` 与 `src/tools/providers.py` 定义。使用 `Model.model_json_schema()` 可导出。geocode.id必须与网络中的Node.id一致；网络节点city用于确定本地接驳范围，hub标记车站/机场/客运枢纽。

## 数据要求

- 每条边需要独立id、service_id、起终节点id、模式、整数分票价、正数分钟时长、来源、观测时间。计价不同的同线路车次拆成不同边。
- 固定班次用带时区datetime的departures；频率服务用operating_start/operating_end与headway_min。跨午夜end必须为次日日期。headway=0表示运营区间内可随时出发，仅用于步行等连接。
- 库存未知用null，售罄用0；不能把未知写成有票。网约车估价需fare_estimated=true。
- live响应每条边必须demo=false；来源为真实URL/供应商标识。官方来源设置official=true须有事实依据。
- 网关必须提供请求时窗内所需的本地与主干边，不应返回全国全量图。
- 各车站换乘、跨站步行、机场提前到达要求需要网关明确建模。当前默认15分钟换乘和90分钟登机缓冲只是原型基线。
- 网页搜索只作为线索返回，未经服务日期、票价、上下车点与班次验证的内容不能加入网络。

## 接入验收

使用已授权沙箱对照固定日期OD核查站点ID、方向、首末班、售罄、跨日、票价、步行距离及退款/政策来源。使用httpx MockTransport可在无网络情况下回放响应；live模式集成合同测试不能替代供应商联调。

LLM独立配置 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`，需要兼容 `/chat/completions`、JSON object输出、tools/function calling、max_tokens和usage。模型失败仍可运行保守规则解析；无法确认的约束进入澄清。配置文件不包含真实密钥。
