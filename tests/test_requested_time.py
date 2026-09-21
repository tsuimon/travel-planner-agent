"""User-specified clocks must survive parsing, current-time guards and provider calls."""

from datetime import timedelta

import pytest
from pydantic import SecretStr

from src.agent.parser import parse
from src.agent.itinerary_parser import rule_draft
from src.domain import ChatRequest, Preferences
from src.errors import NeedsClarification


@pytest.mark.parametrize("clock", ["22:30", "22：30", "22： 30", "22 : 30", "２２：３０", "22点半"])
def test_precise_evening_clock_is_not_a_broad_window(now, clock):
    current = now.replace(hour=19, minute=58)
    c = parse(f"我要在今天晚上{clock}从北京国家体育馆去北京大兴枣园地铁站，规划路线", Preferences(), current)
    assert c.depart_after == current.replace(hour=22, minute=30)
    assert c.depart_before == c.depart_after


@pytest.mark.parametrize("clock", ["22：3", "22：300", "25：30", "22：99"])
def test_invalid_clock_does_not_become_current_evening(now, clock):
    with pytest.raises(NeedsClarification):
        parse(f"今天晚上{clock}从北京到天津", Preferences(), now)


def test_changing_clock_preserves_future_trip_date(now):
    first = parse("明天22：30从北京到天津", Preferences(), now)
    changed = parse("改成23： 15出发", Preferences(), now, first)
    assert changed.depart_after.date() == first.depart_after.date()
    assert (changed.depart_after.hour, changed.depart_after.minute) == (23, 15)


def test_arrival_clock_fullwidth_colon(now):
    c = parse("明天22：30从北京到天津，最晚次日01： 15到", Preferences(), now)
    assert c.arrive_by == (now + timedelta(days=2)).replace(hour=1, minute=15)


def test_multi_stop_clocks_use_same_normalization():
    draft = rule_draft("2026年9月26日15： 30从北京出发，19： 30在天津奥体看演唱会，然后去广州", Preferences())
    assert (draft.depart_after.hour, draft.depart_after.minute) == (15, 30)
    assert (draft.stops[0].start_at.hour, draft.stops[0].start_at.minute) == (19, 30)


@pytest.mark.parametrize("last_train", ["2330", "2215"])
async def test_screenshot_clock_reaches_amap_parameters_and_output(service, now, monkeypatch, last_train):
    service.settings.amap_api_key = SecretStr("test")
    sent = []

    async def geocode(args):
        return {
            "locations": [
                {
                    "location": "116.4,40.0" if "体育馆" in args.address else "116.3,39.7",
                    "citycode": "010",
                    "formatted_address": args.address,
                    "level": "兴趣点",
                }
            ]
        }

    async def route_get(path, params):
        sent.append(params)
        return {
            "status": "1",
            "route": {
                "transits": [
                    {
                        "cost": {"duration": "1200", "transit_fee": "6"},
                        "segments": [
                            {
                                "bus": {
                                    "buslines": [
                                        {
                                            "type": "地铁线路",
                                            "name": "测试线",
                                            "station_start_time": "0500",
                                            "station_end_time": last_train,
                                            "departure_stop": {"name": "甲站"},
                                            "arrival_stop": {"name": "乙站"},
                                            "cost": {"duration": "1200"},
                                        }
                                    ]
                                }
                            }
                        ],
                    }
                ]
            },
        }

    monkeypatch.setattr(service.registry.tools["amap_geocode"], "execute", geocode)
    monkeypatch.setattr(service.registry.tools["amap_route"], "get", route_get)
    result = await service.chat(
        ChatRequest(
            message="我要在今天晚上22： 30从北京国家体育馆去北京大兴枣园地铁站，规划路线",
        ),
        now.replace(hour=19, minute=58),
    )
    expected = now.replace(hour=22, minute=30)
    assert result.constraints.depart_after == expected
    assert sent and all(p["date"] == expected.strftime("%Y-%m-%d") and p["time"] == "22-30" for p in sent)
    report = result.metadata["intelligent_plan"]
    assert bool(report["options"]) == (last_train == "2330")
    assert report["departure_at"] == expected.isoformat()
    assert all(o["routes"][0]["departure"] == expected.isoformat() for o in report["options"])
    assert "22:30" in result.answer and "19:58" not in result.answer
    if not report["options"]:
        assert "餐厅" not in result.answer
