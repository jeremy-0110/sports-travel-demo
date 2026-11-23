"""Post-payment free gap-time itinerary planning (DFD 6.0) & attraction addon service."""

from datetime import datetime, timezone
import json
import re
from typing import Any

from sqlalchemy.orm import Session

import app.config as config
from app.models import Agent, GapItineraryItem, OrderQuote, TripPlan
from app.services.planner import (
    _get_candidate_models,
    _get_league_preset,
    _summarize_gemini_error,
    parse_draft_json,
)

# Curated realistic sightseeing / food / experience pools per destination keyword
_CITY_GAP_OPTION_POOLS: tuple[tuple[tuple[str, ...], list[dict[str, Any]]], ...] = (
    (
        ("洛杉磯", "los angeles", "la", "道奇", "dodger"),
        [
            {
                "activity_name": "道奇體育場幕後VIP球場導覽（Dodger Stadium Tour）",
                "tag": "球場朝聖 · 需門票",
                "description": "走訪球員休息區、場邊草皮與冠軍獎盃廊道，感受道奇隊百年主場魅力。",
                "ticket_required": True,
                "estimated_ticket_twd": 1200,
            },
            {
                "activity_name": "好萊塢環球影城快速通關一日暢遊",
                "tag": "主題樂園 · 需門票",
                "description": "經典影城片場導覽、超級任天堂世界與哈利波特園區快速通關體驗。",
                "ticket_required": True,
                "estimated_ticket_twd": 4800,
            },
            {
                "activity_name": "格里菲斯天文台夕陽＆城市夜景漫步",
                "tag": "地標夜景 · 免費漫遊",
                "description": "遠眺好萊塢標誌與洛杉磯百萬夜景，搭配日落黃昏拍照最愜意。",
                "ticket_required": False,
                "estimated_ticket_twd": 0,
            },
            {
                "activity_name": "中央市場（Grand Central Market）＆小東京拉麵巡禮",
                "tag": "在地美食 · 免費漫遊",
                "description": "一次品嚐 Eggslut、墨西哥玉米餅與正宗日式豚骨拉麵，交通緊鄰聯合車站。",
                "ticket_required": False,
                "estimated_ticket_twd": 0,
            },
            {
                "activity_name": "蓋蒂中心藝術博物館（Getty Center）建築與藝術半日遊",
                "tag": "藝文博物館 · 需預約",
                "description": "搭乘免費山頂電車欣賞名畫與南加州全景庭園，建議預先保留時段憑證。",
                "ticket_required": True,
                "estimated_ticket_twd": 450,
            },
            {
                "activity_name": "聖塔莫尼卡海灘日落騎行＆第 3 街徒步區",
                "tag": "濱海放鬆 · 免費漫遊",
                "description": "吹海風逛露天商店街、Pacific Park 摩天輪旁享用海景晚餐。",
                "ticket_required": False,
                "estimated_ticket_twd": 0,
            },
        ],
    ),
    (
        ("舊金山", "san francisco", "sf", "勇士", "warriors", "chase"),
        [
            {
                "activity_name": "惡魔島（Alcatraz Island）官方渡輪暨語音導覽門票",
                "tag": "必訪地標 · 需門票",
                "description": "含 33 號碼頭官方往返船票與中文語音導覽，探訪傳奇聯邦監獄遺址。",
                "ticket_required": True,
                "estimated_ticket_twd": 1650,
            },
            {
                "activity_name": "金門大橋單車跨海＆索薩利托小鎮海灣渡輪",
                "tag": "經典地標 · 需租借/船票",
                "description": "從漁人碼頭騎行跨過金門大橋至藝術海濱小鎮 Sausalito，搭渡輪輕鬆返程。",
                "ticket_required": True,
                "estimated_ticket_twd": 1100,
            },
            {
                "activity_name": "漁人碼頭 39 號碼頭酸麵包濃湯＆海獅觀景漫步",
                "tag": "海港美食 · 免費漫遊",
                "description": "品嚐 Boudin 經典蛤蜊巧達酸麵包湯與鮮甜鄧杰內斯蟹，欣賞港灣日落。",
                "ticket_required": False,
                "estimated_ticket_twd": 0,
            },
            {
                "activity_name": "加州科學院（California Academy of Sciences）全館通票",
                "tag": "科學博物館 · 需門票",
                "description": "位於金門公園內，集水族館、熱帶雨林圓頂與天文館於一身。",
                "ticket_required": True,
                "estimated_ticket_twd": 1550,
            },
            {
                "activity_name": "Mission 區壁畫街＆藍瓶咖啡慢活散步",
                "tag": "文青散步 · 免費漫遊",
                "description": "探索色彩繽紛的 Clarion Alley 街頭藝術與舊金山最強墨西哥捲餅。",
                "ticket_required": False,
                "estimated_ticket_twd": 0,
            },
        ],
    ),
    (
        ("倫敦", "london", "利物浦", "liverpool", "曼徹斯特", "manchester", "英超"),
        [
            {
                "activity_name": "倫敦眼摩天輪（London Eye）快速通關優先登艙門票",
                "tag": "經典地標 · 需門票",
                "description": "免排長隊直接搭乘泰晤士河畔巨型摩天輪，360 度俯瞰大笨鐘與國會大廈。",
                "ticket_required": True,
                "estimated_ticket_twd": 1850,
            },
            {
                "activity_name": "倫敦塔橋＆皇家珠寶館聯合參觀門票",
                "tag": "歷史古蹟 · 需門票",
                "description": "登高步行透明玻璃步道俯瞰泰晤士河，並入內欣賞英國皇室王冠珠寶。",
                "ticket_required": True,
                "estimated_ticket_twd": 1450,
            },
            {
                "activity_name": "博羅市場（Borough Market）百年歐陸美食掃街之旅",
                "tag": "美食市集 · 免費漫遊",
                "description": "品嚐現煎干貝、西班牙海鮮燉飯與倫敦最紅草莓巧克力杯，美食迷必朝聖。",
                "ticket_required": False,
                "estimated_ticket_twd": 0,
            },
            {
                "activity_name": "大英博物館（British Museum）中文深度導覽＆熱門時段預約",
                "tag": "世界級博物館 · 需導覽票",
                "description": "專人串聯羅塞塔石碑與希臘帕德嫩神殿雕塑精華，兩小時不迷路看懂典藏。",
                "ticket_required": True,
                "estimated_ticket_twd": 950,
            },
            {
                "activity_name": "牛津街＆柯芬園街頭藝人廣場購物漫步",
                "tag": "購物逛街 · 免費漫遊",
                "description": "採買英倫伴手禮 Fortnum & Mason 茶葉、球衣旗艦店並欣賞街頭表演。",
                "ticket_required": False,
                "estimated_ticket_twd": 0,
            },
        ],
    ),
    (
        ("馬德里", "madrid", "巴塞隆納", "barcelona", "西甲", "歐冠", "伯納烏"),
        [
            {
                "activity_name": "伯納烏球場（Bernabéu Tour）皇家馬德里榮耀博物館門票",
                "tag": "球場朝聖 · 需門票",
                "description": "全視野俯瞰全新伸縮草皮主場，近距離與 15 座歐冠大耳朵獎盃合影。",
                "ticket_required": True,
                "estimated_ticket_twd": 1350,
            },
            {
                "activity_name": "普拉多美術館（Museo del Prado）官方快速入場票",
                "tag": "藝術殿堂 · 需門票",
                "description": "免去現場購票人潮，欣賞哥雅、維拉斯奎茲與波希經典巨作。",
                "ticket_required": True,
                "estimated_ticket_twd": 850,
            },
            {
                "activity_name": "聖米格爾市場（Mercado de San Miguel）西班牙 Tapas 美食之夜",
                "tag": "美食市集 · 免費漫遊",
                "description": "百年鐵藝市場內大啖伊比利火腿、蒜味蝦與冰涼 Sangria 水果酒。",
                "ticket_required": False,
                "estimated_ticket_twd": 0,
            },
            {
                "activity_name": "麗池公園（Parque del Retiro）玻璃宮＆太陽門廣場散步",
                "tag": "悠閒漫步 · 免費漫遊",
                "description": "在皇室御用湖畔公園租小船、漫步格蘭大道購買皇家馬德里官方周邊。",
                "ticket_required": False,
                "estimated_ticket_twd": 0,
            },
            {
                "activity_name": "馬德里佛朗明哥百年劇場晚餐秀 VIP 座位票",
                "tag": "文化夜秀 · 需門票",
                "description": "在歷史地窖劇院近距離體驗正統安達魯西亞吉他與熱情舞步。",
                "ticket_required": True,
                "estimated_ticket_twd": 2200,
            },
        ],
    ),
    (
        ("慕尼黑", "munich", "德國", "德甲", "安聯", "拜仁"),
        [
            {
                "activity_name": "新天鵝堡（Neuschwanstein）慕尼黑出發一日導覽與城堡入場票",
                "tag": "夢幻城堡 · 需門票",
                "description": "專車往返阿爾卑斯山麓童話城堡，保證內殿預約梯次與瑪麗安橋絕美視角。",
                "ticket_required": True,
                "estimated_ticket_twd": 2900,
            },
            {
                "activity_name": "BMW 博物館＆奧林匹克公園高塔觀景套票",
                "tag": "工藝地標 · 需門票",
                "description": "探索巴伐利亞百年造車工藝並登塔遠眺阿爾卑斯山與安聯球場全景。",
                "ticket_required": True,
                "estimated_ticket_twd": 780,
            },
            {
                "activity_name": "瑪麗恩廣場新市政廳木偶鐘＆皇家啤酒屋 (HB) 豬腳盛宴",
                "tag": "經典美食 · 免費漫遊",
                "description": "步行逛遍老城廣場與穀物市場，晚上大啖烤豬腳配慕尼黑生啤酒。",
                "ticket_required": False,
                "estimated_ticket_twd": 0,
            },
        ],
    ),
    (
        ("米蘭", "milan", "義大利", "義甲", "巴黎", "paris", "法甲"),
        [
            {
                "activity_name": "米蘭大教堂登頂電梯＋博物館官方快速通關票",
                "tag": "百年地標 · 需門票",
                "description": "搭電梯直上哥德式大理石屋頂露台，近距離欣賞金色聖母像與城市天際線。",
                "ticket_required": True,
                "estimated_ticket_twd": 1250,
            },
            {
                "activity_name": "艾曼紐二世迴廊精品逛街＆運河區 (Navigli) 開胃酒漫步",
                "tag": "時尚與美食 · 免費漫遊",
                "description": "踩一踩幸運公牛馬賽克後，傍晚到運河邊享用道地義式 Aperitivo 點心。",
                "ticket_required": False,
                "estimated_ticket_twd": 0,
            },
            {
                "activity_name": "聖西羅球場官方博物館＆賽道巡禮門票",
                "tag": "球場朝聖 · 需門票",
                "description": "同時探訪 AC 米蘭與國際米蘭雙雄更衣室、混合採訪區與球員通道。",
                "ticket_required": True,
                "estimated_ticket_twd": 1300,
            },
        ],
    ),
    (
        ("紐約", "new york", "ny", "yankees", "knicks"),
        [
            {
                "activity_name": "SUMMIT One Vanderbilt 雲端鏡面觀景台預約門票",
                "tag": "超人氣地標 · 需門票",
                "description": "曼哈頓最紅全鏡面高空觀景台，360 度捕捉帝國大廈與中央公園黃金日落。",
                "ticket_required": True,
                "estimated_ticket_twd": 1650,
            },
            {
                "activity_name": "百老匯經典音樂劇優先劃位電子門票",
                "tag": "藝文夜秀 · 需門票",
                "description": "時代廣場晚間演出，專員精選中前區 Orchestra 黃金視野座位。",
                "ticket_required": True,
                "estimated_ticket_twd": 4900,
            },
            {
                "activity_name": "高線公園（High Line）空中花園＆雀兒喜市場龍蝦美食散步",
                "tag": "都市散步 · 免費漫遊",
                "description": "從廢棄鐵道改建的空中綠廊漫步至室內美食殿堂大啖現蒸龍蝦。",
                "ticket_required": False,
                "estimated_ticket_twd": 0,
            },
        ],
    ),
)

_DEFAULT_GAP_OPTIONS: list[dict[str, Any]] = [
    {
        "activity_name": "主辦城市主場館官方 Stadium Tour 幕後導覽門票",
        "tag": "球場朝聖 · 需門票",
        "description": "專人帶逛球員更衣室、記者會大廳與 VIP 看台，非比賽日也能深入場館核心。",
        "ticket_required": True,
        "estimated_ticket_twd": 1250,
    },
    {
        "activity_name": "城市最高觀景台／文化博物館快速通關門票",
        "tag": "地標文化 · 需門票",
        "description": "免排現場購票長龍，輕鬆串聯市中心經典地標與歷史典藏展區。",
        "ticket_required": True,
        "estimated_ticket_twd": 1450,
    },
    {
        "activity_name": "市中心百年美食市集＆官方球隊旗艦店自由散策",
        "tag": "美食購物 · 免費漫遊",
        "description": "彈性品嚐在地早午餐、選購限定球衣圍巾並體驗當地咖啡文化。",
        "ticket_required": False,
        "estimated_ticket_twd": 0,
    },
]

_PREF_KEYWORD_RULES: tuple[tuple[tuple[str, ...], str, dict[str, Any]], ...] = (
    (
        ("美食", "吃", "餐廳", "小吃", "市集", "拉麵", "牛排", "咖啡", "夜市", "food", "tapas", "pub", "market"),
        "美食探索",
        {
            "activity_name": "在地老饕推薦美食市集＆排隊名店慢食巡禮",
            "tag": "依偏好推薦：美食探索",
            "description": "鎖定球場交通沿線最受歡迎的招牌料理與職人咖啡館，邊吃邊逛最對味。",
            "ticket_required": False,
            "estimated_ticket_twd": 0,
        },
    ),
    (
        ("博物館", "美術館", "藝術", "歷史", "古蹟", "展覽", "導覽", "museum", "art", "history", "gallery"),
        "藝文博物館",
        {
            "activity_name": "城市經典博物館／歷史城堡免排隊快速通關專場",
            "tag": "依偏好推薦：藝文博物館 · 需門票",
            "description": "依您的藝文偏好安排高評價展館預約入場，免去現場長時間候位。",
            "ticket_required": True,
            "estimated_ticket_twd": 1150,
        },
    ),
    (
        ("購物", "逛街", "買", "球衣", "紀念品", "百貨", "outlet", "shop", "mall", "store"),
        "購物血拚",
        {
            "activity_name": "聯賽官方旗艦店＆熱門商圈 Outlet 免稅血拚",
            "tag": "依偏好推薦：購物與球衣採購",
            "description": "一站補齊限量客製化球衣、帽飾與城市特色伴手禮。",
            "ticket_required": False,
            "estimated_ticket_twd": 0,
        },
    ),
    (
        ("夜景", "景觀", "摩天輪", "樂園", "親子", "天文台", "看海", "游船", "sunset", "view", "theme", "universal"),
        "景觀樂園",
        {
            "activity_name": "高空觀景台黃金日落時段預約／主題地標快速通關票",
            "tag": "依偏好推薦：高人氣地標 · 需門票",
            "description": "精準配合空檔時段預約黃金夕陽或夜間燈光秀門票，拍出旅途最美大片。",
            "ticket_required": True,
            "estimated_ticket_twd": 1850,
        },
    ),
    (
        ("放鬆", "休息", "慢活", "公園", "散步", "咖啡", "spa", "relax", "chill", "park", "beach"),
        "慢活紓壓",
        {
            "activity_name": "都市綠洲公園散步＆精品烘焙早午餐慢活時光",
            "tag": "依偏好推薦：慢活紓壓",
            "description": "在緊湊觀賽之間預留充分體力與緩衝空間，享受無壓力自由步調。",
            "ticket_required": False,
            "estimated_ticket_twd": 0,
        },
    ),
)


def is_trip_gap_unlocked(trip: TripPlan, db: Session) -> bool:
    """Return True strictly after full main-trip payment has unlocked DFD 6.0 gap planning."""
    if trip is None or trip.status == "CANCELLED":
        return False
    if trip.status == "LOCKED":
        return True
    paid_main = (
        db.query(OrderQuote)
        .filter(
            OrderQuote.trip_id == trip.trip_id,
            OrderQuote.order_type == "MAIN_TRIP",
            OrderQuote.order_status.in_(("PAID", "BOOKED")),
        )
        .first()
    )
    return paid_main is not None


def extract_gap_slots(draft: dict[str, Any] | None, trip: TripPlan) -> list[dict[str, Any]]:
    """Extract every unlocked gap-time slot from draft structured blocks in chronological order."""
    default_city = str(_get_league_preset(trip.league_type).get("city") or "主辦城市")
    slots: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    if isinstance(draft, dict) and isinstance(draft.get("days"), list):
        for idx, day in enumerate(draft["days"], start=1):
            if not isinstance(day, dict):
                continue
            day_num = int(day.get("day_number") or idx)
            day_date = str(day.get("date") or "")
            city = str(day.get("city") or default_city)
            day_title = str(day.get("title") or f"Day {day_num}")
            blocks = day.get("blocks") if isinstance(day.get("blocks"), list) else []
            for blk in blocks:
                if not isinstance(blk, dict):
                    continue
                if str(blk.get("category") or "").strip() != "空檔":
                    continue
                period = str(blk.get("period") or "自由時段").strip() or "自由時段"
                slot_key = f"{day_num}::{period}"
                if slot_key in seen_keys:
                    continue
                seen_keys.add(slot_key)
                slots.append(
                    {
                        "slot_key": slot_key,
                        "day_number": day_num,
                        "date": day_date,
                        "city": city,
                        "day_title": day_title,
                        "period": period,
                        "slot_title": str(blk.get("title") or "自由空檔時段"),
                    }
                )

    if not slots:
        total_days = max((trip.end_date - trip.start_date).days + 1, 1)
        fallback_day = 2 if total_days >= 3 else 1
        from datetime import timedelta

        visit_day = trip.start_date + timedelta(days=fallback_day - 1)
        slots.append(
            {
                "slot_key": f"{fallback_day}::下午",
                "day_number": fallback_day,
                "date": visit_day.isoformat(),
                "city": default_city,
                "day_title": f"Day {fallback_day} 城市自由空檔探索",
                "period": "下午",
                "slot_title": "自由空檔時段（可自由規劃景點或保留彈性休憩）",
            }
        )

    return slots


def search_gap_attractions_ddgs(
    city: str,
    preference: str = "",
    max_results: int = 4,
) -> list[dict[str, str]]:
    """Best-effort free web search for city attractions matching user preference via ddgs."""
    query_parts = [city.strip() or "sports city", "top attractions things to do", preference.strip()]
    query = " ".join(part for part in query_parts if part)
    try:
        from ddgs import DDGS

        raw_results = DDGS(timeout=5).text(query, max_results=max_results)
    except Exception:
        return []

    results: list[dict[str, str]] = []
    for row in raw_results or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("href") or row.get("url") or "").strip()
        snippet = str(row.get("body") or row.get("snippet") or "").strip()
        if title and url:
            results.append({"title": title[:110], "url": url, "snippet": snippet[:180]})
        if len(results) >= max_results:
            break
    return results


def _pick_city_pool(city: str, league_type: str) -> list[dict[str, Any]]:
    """Select curated gap attraction pool tailored to the destination city or league."""
    haystack = f"{city} {league_type}".lower()
    for keywords, pool in _CITY_GAP_OPTION_POOLS:
        if any(kw in haystack for kw in keywords):
            return pool
    return _DEFAULT_GAP_OPTIONS


def _score_option_for_slot_and_pref(
    option: dict[str, Any],
    period: str,
    pref_lower: str,
) -> int:
    """Rank options so suggestions match time of day and user preferences cleanly."""
    score = 0
    text = f"{option['activity_name']} {option['tag']} {option['description']}".lower()
    if any(k in period for k in ("晚", "傍晚", "夜")):
        if any(w in text for w in ("夜景", "日落", "夕陽", "音樂劇", "啤酒", "tapas", "夜秀", "摩天輪")):
            score += 4
    elif "早" in period or "上午" in period:
        if any(w in text for w in ("博物館", "導覽", "市場", "教堂", "公園", "科學院")):
            score += 3
    if pref_lower:
        for keywords, _, _ in _PREF_KEYWORD_RULES:
            if any(k in pref_lower for k in keywords) and any(k in text for k in keywords):
                score += 6
    return score


def _build_pref_custom_option(preference: str, slot_city: str, slot_idx: int) -> dict[str, Any] | None:
    """Synthesize one preference-aware option when the user provided custom interests."""
    clean_pref = (preference or "").strip()
    if not clean_pref:
        return None
    low = clean_pref.lower()
    for keywords, label, template in _PREF_KEYWORD_RULES:
        if any(k in low for k in keywords):
            return {
                "activity_name": template["activity_name"],
                "tag": template["tag"],
                "description": f"{template['description']}（針對您的偏好「{clean_pref[:40]}」精選）",
                "ticket_required": bool(template["ticket_required"]),
                "estimated_ticket_twd": int(template["estimated_ticket_twd"]),
            }

    # Generic fallback tailored directly to the user's exact preference wording
    if slot_idx % 2 == 0:
        return {
            "activity_name": f"{slot_city}「{clean_pref[:24]}」主題精選深度行程",
            "tag": "依偏好客製 · 免費探索",
            "description": f"依照您輸入的想法「{clean_pref[:60]}」，由系統優先安排鄰近交通圈之對應體驗路線。",
            "ticket_required": False,
            "estimated_ticket_twd": 0,
        }
    return {
        "activity_name": f"{slot_city}「{clean_pref[:24]}」熱門門票體驗（可加購代訂）",
        "tag": "依偏好客製 · 可代訂門票",
        "description": f"依據您的偏好「{clean_pref[:60]}」建議可預約之官方時段入場票券，減少排隊等候。",
        "ticket_required": True,
        "estimated_ticket_twd": 1500,
    }


def _build_slot_mock_options(
    slot: dict[str, Any],
    league_type: str,
    preference: str,
    slot_idx: int,
) -> list[dict[str, Any]]:
    """Build 3 balanced selectable options (free + ticketed) for a single gap slot."""
    pool = _pick_city_pool(slot["city"], league_type)
    pref_lower = (preference or "").strip().lower()
    ranked = sorted(
        pool,
        key=lambda opt: _score_option_for_slot_and_pref(opt, slot["period"], pref_lower),
        reverse=True,
    )

    chosen: list[dict[str, Any]] = []
    pref_opt = _build_pref_custom_option(preference, slot["city"], slot_idx)
    if pref_opt is not None:
        chosen.append(pref_opt)

    rotated = ranked[slot_idx % len(ranked) :] + ranked[: slot_idx % len(ranked)]
    for candidate in rotated:
        if any(existing["activity_name"] == candidate["activity_name"] for existing in chosen):
            continue
        chosen.append(dict(candidate))
        if len(chosen) >= 3:
            break

    # Ensure at least one ticketed option exists if all 3 happened to be free
    if chosen and not any(opt.get("ticket_required") for opt in chosen):
        for candidate in pool:
            if candidate.get("ticket_required"):
                chosen[-1] = dict(candidate)
                break

    formatted: list[dict[str, Any]] = []
    for opt_idx, opt in enumerate(chosen[:3]):
        formatted.append(
            {
                "option_id": f"{slot['slot_key']}::{opt_idx}",
                "activity_name": str(opt["activity_name"]),
                "tag": str(opt["tag"]),
                "description": str(opt["description"]),
                "ticket_required": bool(opt.get("ticket_required")),
                "estimated_ticket_twd": int(opt.get("estimated_ticket_twd") or 0),
            }
        )
    return formatted


def build_mock_gap_suggestions(
    trip: TripPlan,
    draft: dict[str, Any] | None,
    preference: str = "",
    ddgs_sources: list[dict[str, str]] | None = None,
    gemini_status: str = "MOCK",
    model_used: str = "",
    fallback_reason: str = "",
) -> dict[str, Any]:
    """Build deterministic, preference-aware mock gap suggestions ensuring zero-crash fallback."""
    slots = extract_gap_slots(draft, trip)
    slot_entries: list[dict[str, Any]] = []
    clean_pref = (preference or "").strip()

    for idx, slot in enumerate(slots):
        slot_entries.append(
            {
                **slot,
                "options": _build_slot_mock_options(slot, trip.league_type, clean_pref, idx),
            }
        )

    return {
        "preference": clean_pref,
        "source": gemini_status,
        "model_used": model_used or "built-in-concierge-mock",
        "fallback_reason": fallback_reason,
        "ddgs_sources": ddgs_sources or [],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slots": slot_entries,
    }


def _strip_markdown_json_fence(raw_text: str) -> str:
    """Remove ```json ... ``` markdown fences if Gemini wrapped its JSON output."""
    cleaned = (raw_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _normalize_gemini_gap_payload(
    raw_payload: Any,
    slots: list[dict[str, Any]],
    trip: TripPlan,
    preference: str,
    ddgs_sources: list[dict[str, str]],
    model_used: str,
) -> dict[str, Any]:
    """Validate and merge Gemini gap suggestions with curated fallback for any missing slots."""
    raw_slots = raw_payload.get("slots") if isinstance(raw_payload, dict) else raw_payload
    if not isinstance(raw_slots, list):
        raise ValueError("Gemini 回傳之空檔規劃格式非 slots 陣列。")

    by_key: dict[str, list[dict[str, Any]]] = {}
    for entry in raw_slots:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("slot_key") or "").strip()
        if not key and entry.get("day_number") and entry.get("period"):
            key = f"{entry.get('day_number')}::{str(entry.get('period')).strip()}"
        opts = entry.get("options")
        if key and isinstance(opts, list):
            by_key[key] = [o for o in opts if isinstance(o, dict)]

    normalized_slots: list[dict[str, Any]] = []
    for idx, slot in enumerate(slots):
        raw_opts = by_key.get(slot["slot_key"], [])
        clean_opts: list[dict[str, Any]] = []
        for opt in raw_opts[:3]:
            name = str(opt.get("activity_name") or "").strip()
            if not name:
                continue
            ticket_req = bool(opt.get("ticket_required"))
            est_raw = opt.get("estimated_ticket_twd")
            try:
                est_price = max(int(est_raw), 0) if est_raw is not None else (1400 if ticket_req else 0)
            except (TypeError, ValueError):
                est_price = 1400 if ticket_req else 0
            clean_opts.append(
                {
                    "option_id": f"{slot['slot_key']}::{len(clean_opts)}",
                    "activity_name": name[:80],
                    "tag": str(opt.get("tag") or ("AI 推薦 · 可代訂門票" if ticket_req else "AI 推薦 · 免費探索"))[:28],
                    "description": str(opt.get("description") or "Gemini 依賽程動線與旅遊偏好為您挑選的推薦體驗。")[:140],
                    "ticket_required": ticket_req,
                    "estimated_ticket_twd": est_price if ticket_req else 0,
                }
            )

        # Backfill up to 3 options from curated mock pool if Gemini skipped or under-filled a slot
        if len(clean_opts) < 2:
            mock_opts = _build_slot_mock_options(slot, trip.league_type, preference, idx)
            for m_opt in mock_opts:
                if any(c["activity_name"] == m_opt["activity_name"] for c in clean_opts):
                    continue
                m_copy = dict(m_opt)
                m_copy["option_id"] = f"{slot['slot_key']}::{len(clean_opts)}"
                clean_opts.append(m_copy)
                if len(clean_opts) >= 3:
                    break

        normalized_slots.append({**slot, "options": clean_opts[:3]})

    return {
        "preference": (preference or "").strip(),
        "source": "GEMINI",
        "model_used": model_used,
        "fallback_reason": "",
        "ddgs_sources": ddgs_sources,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slots": normalized_slots,
    }


def generate_gap_suggestions(
    trip: TripPlan,
    draft: dict[str, Any] | None,
    preference: str = "",
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Generate 2-3 selectable activity recommendations per unlocked gap slot via Gemini or Mock."""
    clean_pref = (preference or "").strip()
    slots = extract_gap_slots(draft, trip)
    slot_keys_expected = [s["slot_key"] for s in slots]

    if not force_refresh and isinstance(draft, dict):
        cached = draft.get("gap_suggestions")
        if (
            isinstance(cached, dict)
            and str(cached.get("preference") or "").strip() == clean_pref
            and isinstance(cached.get("slots"), list)
            and [s.get("slot_key") for s in cached["slots"] if isinstance(s, dict)] == slot_keys_expected
        ):
            return cached

    cities = list(dict.fromkeys(s["city"] for s in slots if s.get("city")))
    ddgs_sources = search_gap_attractions_ddgs(" ".join(cities[:2]), clean_pref, max_results=4)

    api_key = (config.GEMINI_API_KEY or "").strip()
    if not api_key:
        return build_mock_gap_suggestions(
            trip,
            draft,
            preference=clean_pref,
            ddgs_sources=ddgs_sources,
            gemini_status="MOCK",
            fallback_reason="未設定 GEMINI_API_KEY，使用內建觀光專家智慧推薦。",
        )

    matches_summary = ", ".join(
        f"{m.get('date', '')} {m.get('matchup', '')} @ {m.get('venue', '')}"
        for m in ((draft or {}).get("matches") or [])
        if isinstance(m, dict)
    ) or trip.league_type
    slot_prompt_lines = [
        {
            "slot_key": s["slot_key"],
            "day_number": s["day_number"],
            "date": s["date"],
            "city": s["city"],
            "period": s["period"],
            "day_title": s["day_title"],
        }
        for s in slots
    ]
    sources_snippet = "\n".join(
        f"- {src['title']}: {src['snippet']}" for src in ddgs_sources[:4]
    ) or "(無額外網路摘要，請依主辦城市經典景點與真實地標作答)"

    prompt = (
        "你是一位資深的國際體育旅遊自由行顧問（使用繁體中文回答）。\n"
        "旅客已完成以下賽事主行程全額付款，系統解鎖了免費『空檔時間觀光推薦』。\n"
        f"- 聯賽與主場次：{trip.league_type}（{matches_summary}）\n"
        f"- 旅遊期間：{trip.start_date} ~ {trip.end_date}\n"
        f"- 旅客本次輸入的自由行偏好：{clean_pref or '未指定特殊偏好，請兼顧必訪地標、球場導覽與道地美食'}\n"
        f"- 搜尋引擎參考摘要：\n{sources_snippet}\n\n"
        "請為以下每一個空檔時段（務必保留原始 slot_key）各提供 **3 個不同類型且貼合該時段（上午/下午/晚上）的具體活動選項**，其中至少 1 個是需購票景點（ticket_required=true，方便後續交由專員加購代訂）且至少 1 個是免費漫步或在地美食（ticket_required=false）。\n"
        f"空檔清單 JSON：\n{json.dumps(slot_prompt_lines, ensure_ascii=False)}\n\n"
        "請嚴格輸出純 JSON，格式為：\n"
        '{"slots": [{"slot_key": "1::下午", "options": [{"activity_name": "活動名稱", "tag": "短標籤", "description": "60字內亮點說明", "ticket_required": true, "estimated_ticket_twd": 1500}]}]}'
    )

    last_exc: Exception | None = None
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=config.GEMINI_TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        for model_name in _get_candidate_models():
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.7,
                    ),
                )
                raw_text = _strip_markdown_json_fence(getattr(response, "text", "") or "")
                parsed = json.loads(raw_text)
                return _normalize_gemini_gap_payload(
                    parsed,
                    slots=slots,
                    trip=trip,
                    preference=clean_pref,
                    ddgs_sources=ddgs_sources,
                    model_used=model_name,
                )
            except Exception as exc:
                last_exc = exc
                continue
    except Exception as exc:
        last_exc = exc

    return build_mock_gap_suggestions(
        trip,
        draft,
        preference=clean_pref,
        ddgs_sources=ddgs_sources,
        gemini_status="MOCK",
        fallback_reason=_summarize_gemini_error(last_exc) if last_exc else "Gemini 暫時無法連線",
    )


def persist_gap_suggestions_to_draft(
    db: Session,
    trip: TripPlan,
    gap_suggestions: dict[str, Any],
) -> dict[str, Any]:
    """Save generated gap_suggestions inside trip.llm_draft_json so reloads and POST /save see them."""
    draft = parse_draft_json(trip.llm_draft_json)
    if not isinstance(draft, dict):
        draft = {}
    updated_draft = dict(draft)
    updated_draft["gap_suggestions"] = gap_suggestions
    trip.llm_draft_json = json.dumps(updated_draft, ensure_ascii=False)
    db.commit()
    db.refresh(trip)
    return updated_draft


def save_gap_selections(
    db: Session,
    trip: TripPlan,
    form_data: Any,
    suggestions: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Persist user-selected gap activities to D2 GapItineraryItem while preserving addon-locked rows."""
    existing_items = (
        db.query(GapItineraryItem)
        .filter(GapItineraryItem.trip_id == trip.trip_id)
        .all()
    )
    locked_slots: set[tuple[int, str]] = set()
    for item in existing_items:
        if item.addon_order_id is not None:
            locked_slots.add((item.day_number, item.time_slot))
        else:
            db.delete(item)
    db.flush()

    draft = parse_draft_json(trip.llm_draft_json)
    active_suggestions = suggestions or (draft.get("gap_suggestions") if isinstance(draft, dict) else None)
    slot_list = (
        active_suggestions.get("slots")
        if isinstance(active_suggestions, dict) and isinstance(active_suggestions.get("slots"), list)
        else extract_gap_slots(draft, trip)
    )

    saved_count = 0
    addon_count = 0

    for slot in slot_list:
        if not isinstance(slot, dict):
            continue
        day_num = int(slot.get("day_number") or 1)
        period = str(slot.get("period") or "自由時段").strip()
        slot_key = str(slot.get("slot_key") or f"{day_num}::{period}")

        if (day_num, period) in locked_slots:
            continue

        choice = str(form_data.get(f"choice__{slot_key}") or "").strip()
        if not choice or choice == "__NONE__":
            continue

        if choice == "__CUSTOM__":
            activity_name = str(form_data.get(f"custom__{slot_key}") or "").strip()
            if not activity_name:
                raise ValueError(f"Day {day_num}（{period}）已選擇「自行輸入想去的景點」，請填寫活動或景點名稱。")
        else:
            # Resolve option label from form hidden field or cached suggestions
            activity_name = str(form_data.get(f"opt_name__{choice}") or "").strip()
            if not activity_name:
                for opt in slot.get("options") or []:
                    if isinstance(opt, dict) and str(opt.get("option_id")) == choice:
                        activity_name = str(opt.get("activity_name") or "").strip()
                        break
            if not activity_name:
                activity_name = choice

        raw_addon = str(form_data.get(f"addon__{slot_key}") or "").strip().lower()
        need_ticket_addon = raw_addon in ("1", "true", "on", "yes")

        db.add(
            GapItineraryItem(
                trip_id=trip.trip_id,
                day_number=day_num,
                time_slot=period,
                activity_name=activity_name[:120],
                need_ticket_addon=need_ticket_addon,
                addon_order_id=None,
            )
        )
        saved_count += 1
        if need_ticket_addon:
            addon_count += 1

    db.commit()
    return {
        "saved_count": saved_count,
        "addon_count": addon_count,
        "preserved_locked_count": len(locked_slots),
    }


def create_addon_order_for_gap_items(db: Session, trip: TripPlan) -> OrderQuote:
    """Create a DFD 3.0 ADDON OrderQuote linking all pending ticket-addon GapItineraryItem rows."""
    if not is_trip_gap_unlocked(trip, db):
        raise ValueError("需先完成主行程全額付款並解鎖空檔規劃後，才能提交景點加購詢價。")

    pending_items = (
        db.query(GapItineraryItem)
        .filter(
            GapItineraryItem.trip_id == trip.trip_id,
            GapItineraryItem.need_ticket_addon.is_(True),
            GapItineraryItem.addon_order_id.is_(None),
        )
        .order_by(GapItineraryItem.day_number.asc(), GapItineraryItem.item_id.asc())
        .all()
    )
    if not pending_items:
        raise ValueError("目前沒有待詢價的「需要專員代訂門票」空檔景點，請先勾選景點並點選儲存空檔規劃。")

    agent = db.get(Agent, config.DEFAULT_AGENT_ID)
    if agent is None:
        agent = Agent(
            agent_id=config.DEFAULT_AGENT_ID,
            agent_name="林專員 (運動旅遊首席顧問)",
            email="ag001@sportstravel.demo",
        )
        db.add(agent)
        db.flush()

    addon_order = OrderQuote(
        trip_id=trip.trip_id,
        agent_id=config.DEFAULT_AGENT_ID,
        order_type="ADDON",
        quoted_price=None,
        order_status="QUOTING",
    )
    db.add(addon_order)
    db.flush()

    for item in pending_items:
        item.addon_order_id = addon_order.order_id

    db.commit()
    db.refresh(addon_order)
    return addon_order
