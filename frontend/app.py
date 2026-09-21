"""Embedded Gradio UI, sharing the API's service and database."""

from typing import Callable
import gradio as gr
from src.domain import ChatRequest, Mode, Preferences
from src.risk import MODE_NAMES
from frontend.cards import render_cards, render_intelligent


def build_ui(get_service: Callable) -> gr.Blocks:
    with gr.Blocks(title="混合交通出行规划") as ui:
        gr.Markdown(
            "# 混合交通出行规划\n告诉我出行要求，程序自动选择接驳站点、安排活动与交通，并给出推荐理由。"
        )
        connection = gr.Markdown()
        session = gr.State(None)
        with gr.Row():
            with gr.Column(scale=3):
                chat = gr.Chatbot(label="出行对话", height=460)
                text = gr.Textbox(
                    label="出行需求", placeholder="明天晚上从北京海淀到天津滨海新区，预算100以内，不要打车"
                )
                with gr.Row():
                    submit = gr.Button("规划行程", variant="primary")
                    reset = gr.Button("新对话")
                gr.Examples(
                    examples=[
                        "2026年9月26日15点从北京大兴清源路出发，19点半在天津奥体看演唱会，预计22点散场，吃一小时海底捞后去广州。交通预算500元，允许过夜，餐厅帮我选。",
                        "明天晚上从北京海淀到天津滨海新区，预算100以内，不要打车",
                        "今天晚上10点半从鸟巢出发，去北京印刷学院，四号线赶不上了，先坐地铁，然后打车，给我花费最低的方案",
                        "高铁能带充电宝吗",
                    ],
                    inputs=text,
                )
            with gr.Column(scale=1):
                gr.Markdown("### 长期偏好")
                bike = gr.Radio(
                    choices=[("不骑行", 0), ("短途骑行", 1), ("可以骑行", 2)], value=0, label="骑行接受度"
                )
                transfer = gr.Slider(0, 3, value=2, step=1, label="换乘容忍度（软偏好）")
                economy = gr.Dropdown(
                    [("综合", "balanced"), ("省钱", "economy"), ("省时", "fast")],
                    value="balanced",
                    label="排序偏好",
                )
                excluded = gr.CheckboxGroup([(MODE_NAMES[m.value], m.value) for m in Mode], label="禁用方式")
                save = gr.Button("保存偏好")
                saved = gr.Markdown()
        cards = gr.HTML(label="方案对比")
        with gr.Accordion("结构化结果与数据来源", open=False):
            details = gr.JSON(label="方案详情与数据来源")

        async def respond(message, history, sid):
            if not message or not message.strip():
                return history, sid, {}, message, ""
            result = await get_service().chat(ChatRequest(message=message, session_id=sid))
            updated = [
                *(history or []),
                {"role": "user", "content": message},
                {"role": "assistant", "content": result.answer},
            ]
            cards_html = render_cards(result.plans) + render_intelligent(
                result.metadata.get("intelligent_plan")
            )
            return updated, result.session_id, result.model_dump(mode="json"), "", cards_html

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
            return "已保存，下次规划自动应用。"

        def load_prefs():
            p = get_service().repo.preferences()
            return (
                p.cycling_acceptance,
                p.transfer_tolerance,
                p.budget_preference,
                [m.value for m in p.excluded_modes],
            )

        submit.click(
            respond, [text, chat, session], [chat, session, details, text, cards], concurrency_limit=4
        )
        text.submit(
            respond, [text, chat, session], [chat, session, details, text, cards], concurrency_limit=4
        )
        save.click(persist, [bike, transfer, economy, excluded], saved)
        reset.click(lambda: ([], None, {}, ""), outputs=[chat, session, details, cards])
        ui.load(load_prefs, outputs=[bike, transfer, economy, excluded])

        def connection_status():
            settings = get_service().settings
            if settings.amap_api_key.get_secret_value():
                return "**已连接高德：支持真实地点查询和地铁转打车的自动选站。** 打车为参考估价；车票价格与余票仍需售票方核验。"
            return "**当前为演示数据。** 配置高德后可启用复杂行程的真实路线比较。"

        ui.load(connection_status, outputs=connection)
    return ui
