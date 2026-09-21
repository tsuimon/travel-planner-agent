"""Deterministic explicit preference extraction; request overrides are separate from memory."""

import re
from src.domain import Mode, Preferences


def preference_patch(text: str, previous: Preferences) -> dict:
    patch: dict = {}
    excluded = set(previous.excluded_modes)
    for pattern, mode in [
        (r"(?:不要|不想|不坐|不乘|不|禁止)(?:再)?(?:打车|出租车|网约车)", Mode.taxi),
        (r"(?:不要|不想|不坐|不乘)(?:飞机)", Mode.flight),
        (r"(?:不要|不想|不坐|不乘)(?:地铁)", Mode.metro),
        (r"(?:不要|不想|不坐|不乘)(?:大巴|长途客车)", Mode.coach),
        (r"(?:不要|不想|不坐|不乘)(?:公交)", Mode.bus),
        (r"(?:不要|不想|不坐|不乘)(?:高铁)", Mode.high_speed_rail),
        (r"(?:不要|不想|不坐|不乘)(?:普速)", Mode.normal_rail),
        (r"(?:不要|不想|不坐|不乘)(?:轮渡)", Mode.ferry),
        (r"(?:不要|不想|不坐|不乘)(?:县域班车)", Mode.county_bus),
        (r"(?:不要|不想|不能|禁止)(?:步行|走路)", Mode.walk),
    ]:
        if re.search(pattern, text):
            excluded.add(mode)
            patch["excluded_modes"] = list(excluded)
    if re.search(r"(?:可以|允许|愿意|接受|然后|再)(?:坐)?(?:打车|网约车)", text):
        excluded.discard(Mode.taxi)
        patch["excluded_modes"] = list(excluded)
    if re.search(r"(?:不骑|不要骑|不能骑|不想骑)", text):
        patch["cycling_acceptance"] = 0
    elif re.search(r"(?:能|可以|接受|愿意)骑|骑共享单车|接受骑车", text):
        patch["cycling_acceptance"] = 2
    if "少换乘" in text:
        patch["transfer_tolerance"] = 1
    if "不换乘" in text:
        patch["transfer_tolerance"] = 0
    if any(s in text for s in ("尽量便宜", "省钱", "经济优先", "花费最低", "费用最低", "最便宜")):
        patch["budget_preference"] = "economy"
    if any(s in text for s in ("省时", "尽快", "时间优先")):
        patch["budget_preference"] = "fast"
    if "舒适" in text:
        patch["comfort_priority"] = "high"
    return patch


def merge_preferences(previous: Preferences, patch: dict) -> Preferences:
    return Preferences.model_validate({**previous.model_dump(), **patch})


def is_preference_only(text: str) -> bool:
    """Recognize complete preference utterances, rather than missing route keywords.

    Unknown content remains a planning/clarification request. A preference embedded
    in a journey must never swallow its places, dates or activities.
    """
    clauses = re.split(r"[，,。；;！!\n]+", text.strip())
    preference = (
        r"(?:不想|不要|不|不坐|不乘|禁止)(?:再)?(?:打车|网约车|出租车|飞机|地铁|公交|高铁|普速|大巴|轮渡|县域班车)"
        r"|(?:可以|允许|愿意|接受)(?:坐)?(?:打车|网约车)"
        r"|(?:不骑|不要骑|不能骑|不想骑)(?:车|自行车|共享单车)?"
        r"|(?:能|可以|接受|愿意)骑(?:车|自行车|共享单车)"
        r"|(?:尽量)?(?:少换乘|不换乘|便宜|省钱|舒适)"
        r"|经济优先|时间优先|省时|尽快"
    )
    found = False
    for clause in clauses:
        clause = re.sub(r"\s+", "", clause)
        clause = re.sub(
            r"^(?:(?:请)?记住|我的偏好是|我|以后|今后|长期|默认|平时|希望|想要|想|请)+", "", clause
        )
        if not clause:
            continue
        if re.fullmatch(preference, clause) is None:
            return False
        found = True
    return found


def should_remember(text: str) -> bool:
    """Persist explicit memory requests or standalone preferences, never incidental trip preferences."""
    return "记住" in text or (
        is_preference_only(text)
        and not any(word in text for word in ("这次", "本次", "今天", "明天", "暂时"))
    )
