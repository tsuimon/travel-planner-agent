"""Responsive Gradio workspace, sharing the API's service and database."""

import logging
from typing import Callable
import gradio as gr
from src.domain import ChatRequest, Mode, Preferences
from src.risk import MODE_NAMES
from frontend.cards import render_cards, render_intelligent, notice
from frontend.presentation import HEADER, HERO, WELCOME, EMPTY

SCENARIOS = [
    ("到点抵达 ↗", "明天从北京国家体育馆到北京大兴枣园地铁站，下午4点前到，预算100元，不要打车"),
    ("夜间接驳 ↗", "明天晚上10点半从鸟巢出发，去北京印刷学院，先坐地铁然后打车，帮我选择下车站，尽量省钱"),
    (
        "多站安排 ↗",
        "明天下午3点从北京大兴清源路出发，晚上7点半在天津奥体看演唱会，预计10点散场，吃一小时海底捞后去广州。交通预算500元，允许过夜，餐厅帮我选。",
    ),
]
READY = "可以继续补充条件，我会保留本次对话中的行程信息。"


def build_ui(get_service: Callable) -> gr.Blocks:
    with gr.Blocks(title="行间 · 智能出行规划", analytics_enabled=False, fill_width=True) as ui:
        gr.HTML(HEADER, elem_id="trip-header")
        gr.HTML(HERO, elem_id="trip-hero")
        connection = gr.HTML(elem_id="trip-connection")
        session = gr.State(None)
        with gr.Row(elem_id="trip-workspace"):
            with gr.Column(scale=7, min_width=320, elem_id="trip-conversation"):
                gr.HTML(
                    '<div class="trip-section-head"><h2>聊聊你的行程</h2><span>一句话，也可以开始</span></div>'
                )
                chat = gr.Chatbot(
                    label="出行对话",
                    show_label=False,
                    height=400,
                    min_height=280,
                    placeholder=WELCOME,
                    buttons=["copy"],
                    elem_id="trip-chat",
                )
                with gr.Row(elem_id="trip-scenarios"):
                    examples = [gr.Button(label, size="sm") for label, _ in SCENARIOS]
                with gr.Group(elem_id="trip-composer"):
                    text = gr.Textbox(
                        label="出行需求",
                        show_label=False,
                        lines=3,
                        max_lines=6,
                        placeholder="从哪里出发？什么时候到？\n例如：明天下午4点到天津，预算100元，不要打车。",
                        elem_id="trip-input",
                    )
                    with gr.Row(elem_id="trip-actions"):
                        reset = gr.Button("开启新行程", scale=1, min_width=100, elem_id="trip-reset")
                        submit = gr.Button("开始规划  →", variant="primary", scale=2, elem_id="trip-submit")
                status = gr.Markdown(READY, elem_id="trip-status")
                with gr.Accordion(
                    "出行偏好 · 为下次规划记住你的习惯", open=False, elem_id="trip-preferences"
                ):
                    bike = gr.Radio(
                        [("不骑行", 0), ("短途骑行", 1), ("可以骑行", 2)], value=0, label="骑行接受度"
                    )
                    transfer = gr.Slider(
                        0,
                        3,
                        value=2,
                        step=1,
                        label="换乘接受度",
                        info="0 最少 → 3 较多；硬上限请在对话中说明",
                    )
                    economy = gr.Radio(
                        [("综合考虑", "balanced"), ("省钱优先", "economy"), ("省时优先", "fast")],
                        value="balanced",
                        label="更看重什么",
                    )
                    excluded = gr.CheckboxGroup(
                        [(MODE_NAMES[m.value], m.value) for m in Mode], label="不考虑的交通方式"
                    )
                    save = gr.Button("保存出行偏好", size="sm")
                    saved = gr.Markdown()
            with gr.Column(scale=5, min_width=300, elem_id="trip-results"):
                gr.HTML(
                    '<div class="trip-section-head"><h2>行程方案</h2><span>时间 · 费用 · 每一段路</span></div>'
                )
                cards = gr.HTML(EMPTY, elem_id="trip-cards")
                with gr.Accordion("数据来源与详细结果", open=False, elem_id="trip-details"):
                    details = gr.JSON(label="完整规划结果")
        gr.HTML('<p class="trip-footer">路线与费用为参考测算。出发前请核实班次、余票及当天运营情况。</p>')

        async def respond(message, history, sid):
            if not message or not message.strip():
                yield (
                    gr.skip(),
                    gr.skip(),
                    gr.skip(),
                    gr.skip(),
                    gr.skip(),
                    "先写下你的出发地和目的地吧。",
                    gr.skip(),
                    gr.skip(),
                    *(gr.skip() for _ in examples),
                )
                return
            conversation = [*(history or []), {"role": "user", "content": message.strip()}]
            yield (
                conversation,
                sid,
                gr.skip(),
                gr.update(value="", interactive=False),
                gr.skip(),
                "正在理解需求、查询路线并检查衔接，请稍候…",
                gr.update(value="正在规划…", interactive=False),
                gr.update(interactive=False),
                *(gr.update(interactive=False) for _ in examples),
            )
            try:
                result = await get_service().chat(ChatRequest(message=message.strip(), session_id=sid))
                html = render_cards(result.plans) + render_intelligent(
                    result.metadata.get("intelligent_plan")
                )
                if not html:
                    html = notice(
                        "需要补充一些信息" if result.status == "clarification" else "本轮回复已更新",
                        ["查看对话中的说明，补充条件后可以继续规划。"],
                    )
                conversation.append({"role": "assistant", "content": result.answer})
                hint = (
                    "请在对话中补充信息，我会接着规划。"
                    if result.status == "clarification"
                    else "本轮规划已更新 · 可以继续修改时间、预算或交通方式。"
                )
                yield (
                    conversation,
                    result.session_id,
                    result.model_dump(mode="json"),
                    gr.update(value="", interactive=True),
                    html,
                    hint,
                    gr.update(value="继续规划  →", interactive=True),
                    gr.update(interactive=True),
                    *(gr.update(interactive=True) for _ in examples),
                )
            except Exception:
                logging.getLogger("travel.ui").exception("ui_request_failed")
                conversation.append(
                    {"role": "assistant", "content": "本次请求未能完成。你的输入已保留，请稍后重试。"}
                )
                yield (
                    conversation,
                    sid,
                    gr.skip(),
                    gr.update(value=message, interactive=True),
                    gr.skip(),
                    "暂时未能完成规划，可以重试。",
                    gr.update(value="重新规划  →", interactive=True),
                    gr.update(interactive=True),
                    *(gr.update(interactive=True) for _ in examples),
                )

        def persist(b, t, e, x):
            current = get_service().repo.preferences().model_dump()
            value = Preferences.model_validate(
                {
                    **current,
                    "cycling_acceptance": b,
                    "transfer_tolerance": int(t),
                    "budget_preference": e,
                    "excluded_modes": x,
                }
            )
            get_service().repo.save_preferences(value)
            return "✓ 已保存，下次规划自动应用。"

        def load_prefs():
            p = get_service().repo.preferences()
            return (
                p.cycling_acceptance,
                p.transfer_tolerance,
                p.budget_preference,
                [m.value for m in p.excluded_modes],
            )

        outputs = [chat, session, details, text, cards, status, submit, reset, *examples]
        for event in (submit.click, text.submit):
            event(
                respond,
                [text, chat, session],
                outputs,
                concurrency_limit=1,
                concurrency_id="trip-planning",
                trigger_mode="once",
                show_progress="hidden",
            )
        for button, (_, example) in zip(examples, SCENARIOS):
            button.click(lambda value=example: value, outputs=text, queue=False)
        save.click(persist, [bike, transfer, economy, excluded], saved)
        reset.click(
            lambda: ([], None, {}, "", EMPTY, READY, gr.update(value="开始规划  →")),
            outputs=[chat, session, details, text, cards, status, submit],
            queue=False,
        )
        ui.load(load_prefs, outputs=[bike, transfer, economy, excluded])

        def connection_status():
            connected = bool(get_service().settings.amap_api_key.get_secret_value())
            title = "已连接高德地图" if connected else "演示模式"
            detail = (
                "支持真实地点与交通路线查询，价格与余票仍需核验。"
                if connected
                else "使用合成路线体验规划；配置高德后可查询真实地点。"
            )
            return f'<div class="trip-connection"><span class="trip-dot"></span><span><strong>{title}</strong> · {detail}</span></div>'

        ui.load(connection_status, outputs=connection)
    return ui
