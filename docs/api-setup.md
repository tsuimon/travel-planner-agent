# 真实 API 申请、配置与接入状态

复核日期：2026-09-15。使用本机 Anaconda 环境 `travel-planner`。凭据只放在项目根目录 `.env`；该文件已被 Git 和 Docker 构建上下文排除，示例文件不包含密钥。

## 当前状态

| 数据源 | 已实现并验证 | 仍未具备 |
|---|---|---|
| DeepSeek | 兼容接口、JSON 解析、格式校验、重试和预算控制；真实连接探针通过 | 不能用模型自身知识替代班次、余票和报价 |
| 高德 Web 服务 | 场馆/门店候选、地理编码、公共交通与步行真实路线；多站候选主动比较和时序校验 | 票价和余票尚未由售票方核验，营业时间不保证演出当天有效 |
| 机票、火车票 | 已有授权供应商网关的数据契约及交通图模型 | 尚未取得同程等供应商权限、接口文档和测试响应，未完成实时机票适配 |

目前 `.env` 保持 `DATA_MODE=demo`：预设单程搜索仍用演示图；配置高德后，自然语言多站请求自动进入真实路线证据规划流程。该流程独立标注数据来源和未核验票务，详见 [主动规划说明](intelligent-planning.md)。不要仅凭两把 Key 就把完整供应商网关切换为 live。

## DeepSeek 申请与配置

1. 注册并登录 [DeepSeek 开放平台](https://platform.deepseek.com/)，进入 API Keys 创建密钥，创建时保存完整值。Tracking ID 是管理标识，带星号的遮罩值不能用于调用。
2. 在平台确认 API 账户余额和使用额度。API 按调用计费；实际费率以平台显示为准。
3. 新安装可复制 `.env.deepseek.example` 为 `.env`；已有 `.env` 时只修改对应行，保留其他配置。

```dotenv
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
LLM_API_KEY=在本机填入完整密钥
LLM_EXTRA_BODY={"thinking":{"type":"disabled"}}
TOOL_TIMEOUT=10
```

接口地址和当前模型名参见 [DeepSeek 官方文档](https://api-docs.deepseek.com/zh-cn/)。结构化抽取关闭思考模式以控制延时；参数依据 [思考模式文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)。如更换模型，需要重新验证 JSON 输出和工具调用能力。

## 高德申请与配置

1. 按 [高德创建应用与 Key 指南](https://lbs.amap.com/api/webservice/create-project-and-key) 登录控制台，创建应用。
2. 添加 Key 时选择 **Web 服务**，将其写到 `.env` 的 `AMAP_API_KEY=`。此后端接口不使用 JSAPI 的安全密钥。
3. 当前接入 [POI 关键字搜索](https://developer.amap.com/api/webservice/guide/api/search/)，用于查找真实场馆和门店候选。查询到门店并不表示演出日夜间营业、无排队或已计算绕行代价。

```dotenv
AMAP_API_KEY=在本机填入Web服务Key
```

高德[公交路径规划文档](https://developer.amap.com/api/webservice/guide/api/newroute)提供路线能力，项目已实现真实路线证据适配与时序检查；它不能替代航空或铁路实时售票库存。

## 同程机票怎么申请

可以从 [同程商旅系统集成页面](https://tmc.ly.com/zh/platform/integration) 的咨询入口联系官方。其页面提供 API 对接服务；[同程官方开放能力说明](https://tmc.ly.com/zh/resources/updates/open-mcp-skills-cli)还列出 MCP、Skills、CLI，以及航班、酒店、火车票实时资源能力。公开页面不能证明个人开发者一定能自助开通，或普通查询权限免费，需要官方确认。

也可查阅 [同程旅行开放平台](https://union.ly.com/FrontWeb/index)。该站列有机票合作迁移公告，不应直接照搬旧博客中的调用域名和签名算法。

可以将以下内容复制到官方咨询表单，根据实际情况补充个人或企业身份（这里未代你发送）：

> 我在开发一个多约束出行规划 Agent，需要查询国内机票和火车票，首期只做查询与方案比较，不下单、不支付。希望通过 HTTP API 或 MCP 查询指定日期、出发地和目的地的航班、起降机场和时间、含税总价、舱位、可售状态及退改签规则，比较天津/北京至广州等行程。请问是否接受个人开发者或原型项目？需要哪些资质，是否有沙箱、测试额度，生产计费和缓存展示要求是什么？能否提供最新接口文档、鉴权签名说明和脱敏响应示例？

申请时需要确认：

- **权限**：仅查询是否可申请，个人/企业资质要求，是否强制绑定交易合作。
- **覆盖**：国内航班、廉航、天津与北京多机场；是否同时提供火车票。
- **报价**：票面价、燃油费、机建费、服务费、币种和成人总价如何区分；报价有效期及二次验价。
- **库存**：可售、售罄和未知的字段含义；不能把未知当成有票。
- **接入**：生产/沙箱域名，App ID、Secret 或 Token，签名样例、时间戳、IP 白名单、限流及错误码。
- **展示**：来源标注、缓存 TTL、跳转购票链接以及商业展示授权。

拿到不含凭据的文档链接和脱敏查询响应后，即可根据实际协议编写适配器。项目内部 [供应商网关契约](providers.md) 的 `PROVIDER_URL` 不是同程原生接口地址，不能把同程首页直接填进去。其他票务供应商也按相同标准接入；目前没有声称任何一家已联调完成。

## 本机检查与启动

在项目目录的 Anaconda Prompt 中执行：

```bat
conda activate travel-planner
python -m scripts.check_connections
python -m scripts.check_connections --network
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

不加 `--network` 只检查配置是否存在；加上后会发起小额模型请求及高德查询，只输出成功/失败，不打印密钥。模型探针成功不等于真实路线已接入。修改 `.env` 后需要重新启动服务。

遇到模型连接失败，依次检查完整密钥、账户余额、模型权限、Base URL；高德失败则检查服务类型、配额和控制台访问限制。不要将实际密钥复制到错误报告或提交到仓库。
