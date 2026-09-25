"""Local visual assets and Gradio theme configuration."""

from pathlib import Path
import gradio as gr

CSS_PATH = Path(__file__).with_name("styles.css")
THEME = gr.themes.Soft(
    primary_hue="emerald",
    secondary_hue="teal",
    neutral_hue="slate",
    font=["Segoe UI", "Microsoft YaHei", "sans-serif"],
    font_mono=["Consolas", "monospace"],
)

COMPASS = """<svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.5"/>
<path d="m16 8-2.5 5.5L8 16l2.5-5.5L16 8Z" fill="currentColor"/></svg>"""
HEADER = f"""<header class="trip-nav"><div class="trip-brand"><span class="trip-logo">{COMPASS}</span>
行间<span style="font-size:11px;font-weight:400;letter-spacing:2px">出行规划</span></div>
<span class="trip-nav-note">每一段路，都有安排</span></header>"""
HERO = """<div class="trip-hero"><div class="trip-eyebrow">YOUR NEXT JOURNEY</div>
<h1>把复杂行程，安排明白。</h1><p>说说你要去哪里、几点到，还有那些不能将就的要求。</p></div>"""
WELCOME = f"""<div class="trip-welcome"><div class="trip-welcome-icon">{COMPASS}</div>
<h3>这次，想怎么出发？</h3><p>赶一场演唱会，接上最后一班地铁，<br>或在预算内安排一趟跨城旅行。<br>把需求告诉我，我们一起把路线理清。</p>
<div class="trip-welcome-tags"><span>多站行程</span><span>到达时间倒推</span><span>夜间接驳</span></div></div>"""
EMPTY = """<div class="trip-empty"><div class="trip-empty-art">
<svg width="240" height="110" viewBox="0 0 240 110" fill="none" aria-hidden="true">
<path d="M18 80H68Q95 80 95 52V45Q95 25 125 25H166Q188 25 188 53V62Q188 80 220 80" stroke="#afc7b3" stroke-width="2" stroke-dasharray="5 6"/>
<circle cx="18" cy="80" r="8" fill="#eaf3e7" stroke="#588568" stroke-width="2"/>
<circle cx="126" cy="25" r="6" fill="#739d7b"/>
<path d="M220 57c-12 0-16 15 0 29 16-14 12-29 0-29Z" fill="#176651"/>
<circle cx="220" cy="68" r="4" fill="#ecf3e9"/>
<path d="m47 33 9-4 9 4-9 4-9-4Z" fill="#cad9bd"/>
<path d="M153 72h15m-7-7v14" stroke="#becfb8" stroke-width="2"/>
</svg></div><h3>你的行程，即将在这里展开</h3>
<p>先聊聊目的地和时间。规划完成后，<br>在这里查看费用、接驳站点和每一段安排。</p>
<div class="trip-empty-steps"><span><b>01 / 描述</b>说出出行需求</span><span><b>02 / 规划</b>检查时间与衔接</span><span><b>03 / 查看</b>选择合适的路线</span></div></div>"""
