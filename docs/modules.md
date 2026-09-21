# 模块设计

## 领域模型与金额时间

`src/domain.py` 定义 Constraints、Preferences、Node、Edge、Network、Leg、Plan 和API模型。Pydantic禁止额外字段、拒绝无时区时间，预算以整数分表示。11种方式均有枚举和演示图示例，真实覆盖取决于供应商。出发时间窗限制首段实际发车；恰好某时刻出发是零宽窗口。若允许等待，应明确提供时间窗。

`max_walk_m`、`max_bike_m`是全行程累计上限；不骑车以及禁用方式是硬排除；换乘容忍度为评分偏好。报价未知不能构造交通边，估价必须单独标注，预算过滤只针对数据中的报价。

## 搜索模块

`algorithm.py` 用堆实现有界、多标签、时间依赖的简单路径枚举。每个状态保存节点、抵达时刻、路径与已访问节点集合，路径含成本、服务和距离资源。不采用原文错误的单标签visited剪枝。当前为保留可解释替代方案，没有实施跨路线前缀的Pareto支配删除。

```text
push(origin, depart_after, empty_path, {origin})
while heap is not empty and fewer than K destinations:
    check shared deadline and expansion limit
    pop by cost / arrival / transfers
    if node == target: retain path
    for outgoing edge allowed by hard constraints:
        ready = arrival + transfer_buffer(previous_service, edge)
        departure = next boardable scheduled/frequency departure >= ready
        reject if first departure outside departure window
        reject if arrival exceeds arrive_by
        reject sold-out, forbidden mode, excess cost or cumulative distance
        reject a revisited node or path beyond hop bound
        push updated label
return retained paths and truncation metadata
```

边费用和时长必须非负/正数；频率服务的运营结束时刻限制发车，不禁止末班在结束后到站。跨午夜通过完整日期表示。固定班次找到下一班可乘车；不同票价或行程时间的车次应表示为不同Edge，而非合并到同一departures列表。

最坏情况仍是路径枚举的指数复杂度。本实现依靠主干/本地子图、默认Top-5/Top-3、每段最多8跳、共享15000次扩展及30秒deadline限制计算量。遇到截断会设置`search_truncated`，不能宣称精确Pareto前沿或全局最优。

`planner.py` 先找起点可达枢纽，再以到站时间搜索主干，然后按主干实际到达时间搜索终点接驳。首末地点不需要接驳时使用空路径；不把所有城市的边放到同一次路径搜索中。最终`make_plan`重新校验时刻、连续性、缓冲、预算、距离和排除方式。总耗时按末段抵达减首段出发计算，因此包含全部等待。

夜间 `local_search` 在常规时刻搜索之外，枚举公共交通可达终点，再搜索taxi/bike/walk尾段，并在满足限制时加入直达打车。这里使用可达性与费用比较，不以直线距离最近站替代可达性。排序保留省钱、省时、少换乘代表，再按偏好分排序；同一方案可同时代表多个目标。

## 工具与供应商

`BaseTool` 在调用前验证参数，默认3次总尝试（首次+2次重试），指数退避，每次受工具超时和请求deadline双重限制。成功结果写入有界TTL内存缓存和SQLite缓存；仅未过期缓存可回退。失败错误不包含HTTP原始响应或密钥。

注册表提供function calling JSON Schema并校验工具白名单。网页线索不会自动转为可乘交通边。供应商网关需完成数据授权与归一化，详见 `providers.md`。

## Agent 与资源预算

`RequestNodes` 每请求一个实例。LangGraph依次执行需求解析、策略决策、工具调用、结果整合、反思、输出。前三轮尝试常见方式的费用/时间/换乘目标；后三轮扩展到县域班车与轮渡，禁止无变化的无限循环。

LLM启用后使用JSON验证与一次格式修复，仍失败走规则解析或澄清；HTTP调用本身遵守首次+2次重试。function calling只开放当前允许的只读工具；记忆写入由明确偏好提取控制。LLM不产生车次或价格。

每次模型调用前按请求UTF-8字节数、协议余量与输出上限做保守Token预留，预算不够时不发起调用；返回usage计入`token_actual`。预留值不冒充实际消耗。对不遵守max_tokens/usage合同的第三方模型不能保证计费上限，应在供应商侧另设限额。

`asyncio.wait_for`包住图运行；网络调用可取消，搜索每次展开检查绝对deadline。同步搜索在工作线程内执行，超时后最多继续到下一次检查；无法中断任意卡死的第三方C扩展。数据库初始化、进程启动与HTTP传输时间不计入30秒规划预算。RAG在启动时建库，查询在线程内执行。

## RAG、记忆和数据层

`knowledge.py` 加载本地JSON政策条目，480字符分块、80字符重叠，使用LangChain Embeddings接口的512维字符哈希向量写入Chroma。领域标签限制铁路/航空政策串用。检索后按原条目内容输出，附来源、施行日期、复核日期；180天未复核提示可能过时。没有收录的航空充电宝规则明确回答缺数据。

`memory.py` 提取明确偏好；本次或明天等临时措辞不自动永久保存，“记住”优先。当前输入覆盖保存值，API结构化constraints作为完整请求使用。会话上下文只使用同一session最近6条消息，不跨会话继承行程。

SQLAlchemy六表：users、preferences、sessions、messages、plan_records、cache。外键启用、SQLite WAL与busy_timeout；一轮用户消息、助手消息及方案记录在同一事务保存。会话删除级联删除消息与方案，长期偏好保留，可单独重置。

## UI 与API

Gradio挂在FastAPI的 `/ui`，聊天结果就是API的结构化方案和中文解释，偏好面板读写同一个仓库。显式设置API_ACCESS_TOKEN时禁用匿名UI，仅开放Bearer保护的API。服务默认本地单用户，未实现多用户鉴权隔离；不要将默认配置暴露公网。

## 多站活动补充设计（2026-09-15）

复杂请求先进入 `ItineraryDraft`，活动顺序、地点候选、固定开场时间、停留时长、最终到达期限、过夜意愿和费用口径独立建模。每次澄清后的草稿存入会话响应，后续补充基于最近草稿更新。含“省钱”的出行请求不会仅保存成长期偏好。

多站求解器逐站传播候选状态 `(地点, 可再次出发时间, 累计费用, 全部交通段, 已完成活动)`，每段复用城市间主干与本地接驳搜索。到达晚于活动开场、活动结束晚于终点期限、累计超预算或违反过夜约束的分支被过滤。下一站的搜索起点来自活动结束，保留较早与较便宜的多个标签，避免只贪心选择第一段最低价而错过后续班次。

单站最多 5 个候选地点，总计最多 8 站、7 天；每次路线查询最多覆盖 48 小时，中间候选数量受限，不保证全局最优。活动和餐饮费用由用户提供，交通预算默认不含这些费用。营业时间、排队、住宿和演出实际延迟尚未建模；标注风险而不假设已知。

可选 DeepSeek 负责草稿解析；LLM 输出须通过 Pydantic 验证，不能丢掉规则已识别的站点。高德 POI 工具提供真实候选场馆或门店；候选本身不会自动成为可乘坐的交通边。真实交通数据接入状态见 `api-setup.md`。
