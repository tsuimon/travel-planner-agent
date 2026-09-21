# Phase 0–7 交付与验证

先阅读 `architecture.md` 的可行性评审。每阶段完整源码（带文件路径）集中在 `phase-code.md`，由 `python -m scripts.export_phase_code` 从工作区生成，避免手工复制失真。实际源码始终是最终权威。

## Phase 0：项目初始化

目标：建立可安装、可配置、可追溯的Python工程。

代码：requirements.txt、requirements-dev.txt、pyproject.toml、environment.yml、.env.example、src/config.py、src/errors.py、src/logging_config.py、README.md。

验证：`conda env create -f environment.yml`；本机已完成命名环境创建及依赖安装，`python -m pip check`。配置值通过Pydantic校验。

衔接：后续模块从Settings读取数据模式、数据库地址、超时与Token预算。

## Phase 1：数据层

目标：六表持久化、事务保存会话、TTL缓存。

代码：src/data/models.py、repository.py、cache.py；scripts/init_db.py。

验证：`python -m scripts.init_db`；`python -m pytest tests/test_tools_data.py -q`，包括重开数据库后偏好仍在、TTL过期和缓存副本隔离。

衔接：提供工具缓存、偏好和会话存储；当前本地单用户，不能把user表理解成已实现多租户认证。

## Phase 2：工具层

目标：天气、车次、地理编码、网页线索、偏好工具具备统一接口和function calling Schema。

代码：src/tools/base.py、providers.py，供应商契约见providers.md。

验证：`python -m pytest tests/test_tools_data.py tests/test_api_llm.py -q`，检查格式验证、首次+2次重试、超时、有效缓存回退、未知工具和live模式不混入演示数据。

衔接：工具返回结构化Network供搜索器使用；网页摘要不直接成为班次。

## Phase 3：搜索层

目标：时间依赖多标签搜索、主干/接驳分层组合、夜间专用后缀扩展与硬约束校验。

代码：src/domain.py；src/search/algorithm.py、planner.py、sample.py；scripts/load_sample_data.py。

验证：`python -m scripts.load_sample_data`；`python -m pytest tests/test_search.py -q`，包括较贵但更早的可行标签、跨午夜、末班错过、累计骑行距离、预算与禁用方式。

衔接：搜索器只接受验证后的Constraints和Network，返回0–N个可比较候选给Agent，LLM不能篡改班次计算结果。

## Phase 4：Agent核心

目标：LangGraph六节点、有变化的最多6轮策略扩展、Token预留与30秒deadline。

代码：src/agent/state.py、parser.py、budget.py、llm.py、nodes.py、graph.py。

验证：`python -m pytest tests/test_parser_memory.py tests/test_api_llm.py tests/test_e2e.py -q`；LLM非法JSON与格式修复通过模拟响应测试，真实LLM联调需配置授权端点。

衔接：政策分支和记忆注入复用同一请求生命周期；最终生成器只使用校验后的计划。

## Phase 5：RAG与记忆

目标：本地可复现向量检索、来源与复核日期、明确偏好的持久化和当前覆盖规则。

代码：src/rag/knowledge.py、documents/policies.json；src/memory.py；scripts/build_knowledge_base.py。

验证：`python -m scripts.build_knowledge_base`；E2E政策问答和偏好记忆测试验证引用、铁路/航空领域隔离和临时偏好不污染长期存储。

衔接：政策答复进入统一ChatResponse，风险提示展示在UI。当前Embeddings是字符n-gram词法基线，答案是检索片段确定性拼接；预训练语义模型和开放式生成不作为已实现能力。

## Phase 6：风险、API与前端

目标：风险标签与结果一致，HTTP接口和Gradio可以完成交互。

代码：src/risk.py；src/api/app.py；frontend/app.py。

验证：`python -m pytest tests/test_api_llm.py tests/test_e2e.py -q`；启动uvicorn后浏览器测试夜间方案、政策问答、偏好保存。

衔接：实际结果和元数据用于最终回归与性能报告；UI和API共用服务，避免不同端各有一套计算逻辑。

## Phase 7：测试、优化、文档与部署

目标：用真实运行结果证明原型行为，提供可重建环境与部署文件。

代码：tests/*、scripts/demo.py、benchmark.py、export_phase_code.py、Dockerfile、docker-compose.yml、.github/workflows/ci.yml。

验证：`python -m pytest -q --cov=src`；`python -m ruff check src frontend scripts tests`；`python -m pip check`；`python -m scripts.benchmark`。

衔接：下一阶段是供应商授权与真实数据联调，再做固定真实OD对比实验。Docker启动验证和真实LLM/交通API验证未在本机完成；本项目不以合成测试代替这些验收。
