"""Sports schedule retrieval (ESPN live API + ddgs) and Gemini itinerary planning service."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import json
import re
import time
from typing import Any
import unicodedata
from urllib.parse import urlparse

import httpx

import app.config as config

MAX_TRIP_DAYS = 30
LOCKED_GAP_TITLE = "自由空檔時段（付款後解鎖）"
LOCKED_GAP_DETAIL = (
    "全額付款完成後，系統將自動解鎖免費空檔旅遊建議與景點門票加購代訂。"
)

_ESPN_LEAGUE_PATHS: dict[str, list[tuple[str, str]]] = {
    "MLB": [("MLB", "baseball/mlb")],
    "NBA": [("NBA", "basketball/nba")],
    "歐足五大": [
        ("英超", "soccer/eng.1"),
        ("西甲", "soccer/esp.1"),
        ("義甲", "soccer/ita.1"),
        ("德甲", "soccer/ger.1"),
        ("法甲", "soccer/fra.1"),
    ],
    "歐冠": [("歐冠", "soccer/uefa.champions")],
}

_LEAGUE_SEARCH_TERMS: dict[str, str] = {
    "MLB": "MLB schedule",
    "NBA": "NBA schedule",
    "歐足五大": "Premier League La Liga fixtures",
    "歐冠": "Champions League fixtures",
}

LEAGUE_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "espn_path": "baseball/mlb",
        "league_type": "MLB",
        "sub_league": "MLB",
        "label": "MLB（美國職棒大聯盟）",
        "group": "北美職業聯賽",
        "region": "NA",
        "region_label": "北美",
    },
    {
        "espn_path": "basketball/nba",
        "league_type": "NBA",
        "sub_league": "NBA",
        "label": "NBA（美國職業籃球聯賽）",
        "group": "北美職業聯賽",
        "region": "NA",
        "region_label": "北美",
    },
    {
        "espn_path": "soccer/eng.1",
        "league_type": "歐足五大",
        "sub_league": "英超",
        "label": "英超（Premier League）",
        "group": "歐洲足球五大聯賽",
        "region": "EU",
        "region_label": "歐洲",
    },
    {
        "espn_path": "soccer/esp.1",
        "league_type": "歐足五大",
        "sub_league": "西甲",
        "label": "西甲（La Liga）",
        "group": "歐洲足球五大聯賽",
        "region": "EU",
        "region_label": "歐洲",
    },
    {
        "espn_path": "soccer/ita.1",
        "league_type": "歐足五大",
        "sub_league": "義甲",
        "label": "義甲（Serie A）",
        "group": "歐洲足球五大聯賽",
        "region": "EU",
        "region_label": "歐洲",
    },
    {
        "espn_path": "soccer/ger.1",
        "league_type": "歐足五大",
        "sub_league": "德甲",
        "label": "德甲（Bundesliga）",
        "group": "歐洲足球五大聯賽",
        "region": "EU",
        "region_label": "歐洲",
    },
    {
        "espn_path": "soccer/fra.1",
        "league_type": "歐足五大",
        "sub_league": "法甲",
        "label": "法甲（Ligue 1）",
        "group": "歐洲足球五大聯賽",
        "region": "EU",
        "region_label": "歐洲",
    },
    {
        "espn_path": "soccer/uefa.champions",
        "league_type": "歐冠",
        "sub_league": "歐冠",
        "label": "歐冠（UEFA Champions League）",
        "group": "歐洲冠軍聯賽",
        "region": "EU",
        "region_label": "歐洲",
    },
)

LEAGUE_OPTION_BY_PATH: dict[str, dict[str, str]] = {
    opt["espn_path"]: opt for opt in LEAGUE_OPTIONS
}

_EN_TEAM_ZH_LABELS: dict[str, str] = {
    "Los Angeles Dodgers": "洛杉磯道奇",
    "New York Yankees": "紐約洋基",
    "San Francisco Giants": "舊金山巨人",
    "Boston Red Sox": "波士頓紅襪",
    "Texas Rangers": "德州遊騎兵",
    "Seattle Mariners": "西雅圖水手",
    "San Diego Padres": "聖地牙哥教士",
    "Houston Astros": "休士頓太空人",
    "New York Mets": "紐約大都會",
    "Chicago Cubs": "芝加哥小熊",
    "Atlanta Braves": "亞特蘭大勇士",
    "Philadelphia Phillies": "費城費城人",
    "Golden State Warriors": "金州勇士",
    "Los Angeles Lakers": "洛杉磯湖人",
    "Boston Celtics": "波士頓塞爾提克",
    "New York Knicks": "紐約尼克",
    "Denver Nuggets": "丹佛金塊",
    "Dallas Mavericks": "達拉斯獨行俠",
    "Miami Heat": "邁阿密熱火",
    "Milwaukee Bucks": "密爾瓦基公鹿",
    "Philadelphia 76ers": "費城76人",
    "LA Clippers": "洛杉磯快艇",
    "Oklahoma City Thunder": "奧克拉荷馬雷霆",
    "Minnesota Timberwolves": "明尼蘇達灰狼",
    "Phoenix Suns": "鳳凰城太陽",
    "San Antonio Spurs": "聖安東尼奧馬刺",
    "Houston Rockets": "休士頓火箭",
    "Everton": "艾佛頓",
    "Liverpool": "利物浦",
    "Arsenal": "兵工廠",
    "Chelsea": "切爾西",
    "Manchester City": "曼城",
    "Manchester United": "曼聯",
    "Tottenham Hotspur": "熱刺",
    "Newcastle United": "紐卡索",
    "Aston Villa": "阿斯頓維拉",
    "Brighton & Hove Albion": "布萊頓",
    "West Ham United": "西漢姆聯",
    "Real Madrid": "皇家馬德里",
    "Barcelona": "巴塞隆納",
    "Atletico Madrid": "馬德里競技",
    "Inter Milan": "國際米蘭",
    "AC Milan": "AC米蘭",
    "Juventus": "尤文圖斯",
    "Napoli": "拿坡里",
    "AS Roma": "羅馬",
    "Bayern Munich": "拜仁慕尼黑",
    "Borussia Dortmund": "多特蒙德",
    "Bayer Leverkusen": "勒沃庫森",
    "RB Leipzig": "RB萊比錫",
    "VfB Stuttgart": "斯圖加特",
    "Eintracht Frankfurt": "法蘭克福",
    "FC Cologne": "科隆",
    "SC Freiburg": "弗萊堡",
    "Paris Saint-Germain": "巴黎聖日耳曼",
    "Marseille": "馬賽",
    "Lyon": "里昂",
    "AS Monaco": "摩納哥",
}

_FALLBACK_TEAMS_BY_PATH: dict[str, list[dict[str, str]]] = {
    "baseball/mlb": [
        {"id": "19", "name": "Los Angeles Dodgers"},
        {"id": "10", "name": "New York Yankees"},
        {"id": "2", "name": "Boston Red Sox"},
        {"id": "26", "name": "San Francisco Giants"},
        {"id": "13", "name": "Texas Rangers"},
    ],
    "basketball/nba": [
        {"id": "9", "name": "Golden State Warriors"},
        {"id": "13", "name": "Los Angeles Lakers"},
        {"id": "2", "name": "Boston Celtics"},
        {"id": "18", "name": "New York Knicks"},
        {"id": "7", "name": "Denver Nuggets"},
    ],
    "soccer/eng.1": [
        {"id": "368", "name": "Everton"},
        {"id": "364", "name": "Liverpool"},
        {"id": "359", "name": "Arsenal"},
        {"id": "363", "name": "Chelsea"},
        {"id": "382", "name": "Manchester City"},
        {"id": "360", "name": "Manchester United"},
    ],
    "soccer/esp.1": [
        {"id": "86", "name": "Real Madrid"},
        {"id": "83", "name": "Barcelona"},
        {"id": "1068", "name": "Atletico Madrid"},
    ],
    "soccer/ita.1": [
        {"id": "110", "name": "Inter Milan"},
        {"id": "103", "name": "AC Milan"},
        {"id": "111", "name": "Juventus"},
        {"id": "114", "name": "Napoli"},
    ],
    "soccer/ger.1": [
        {"id": "132", "name": "Bayern Munich"},
        {"id": "124", "name": "Borussia Dortmund"},
        {"id": "122", "name": "FC Cologne"},
        {"id": "131", "name": "Bayer Leverkusen"},
    ],
    "soccer/fra.1": [
        {"id": "160", "name": "Paris Saint-Germain"},
        {"id": "176", "name": "Marseille"},
        {"id": "167", "name": "Lyon"},
    ],
    "soccer/uefa.champions": [
        {"id": "86", "name": "Real Madrid"},
        {"id": "83", "name": "Barcelona"},
        {"id": "132", "name": "Bayern Munich"},
        {"id": "364", "name": "Liverpool"},
        {"id": "382", "name": "Manchester City"},
        {"id": "359", "name": "Arsenal"},
        {"id": "110", "name": "Inter Milan"},
        {"id": "160", "name": "Paris Saint-Germain"},
    ],
}

# (zh_or_alias_keyword, en_team_keyword, optional_espn_path)
_TEAM_ZH_TO_EN: tuple[tuple[str, str, str | None], ...] = (
    # Premier League (soccer/eng.1)
    ("艾佛頓", "Everton", "soccer/eng.1"),
    ("利物浦", "Liverpool", "soccer/eng.1"),
    ("薩拉赫", "Liverpool", "soccer/eng.1"),
    ("salah", "Liverpool", "soccer/eng.1"),
    ("anfield", "Liverpool", "soccer/eng.1"),
    ("安菲爾德", "Liverpool", "soccer/eng.1"),
    ("兵工廠", "Arsenal", "soccer/eng.1"),
    ("阿森納", "Arsenal", "soccer/eng.1"),
    ("emirates", "Arsenal", "soccer/eng.1"),
    ("酋長球場", "Arsenal", "soccer/eng.1"),
    ("切爾西", "Chelsea", "soccer/eng.1"),
    ("stamford bridge", "Chelsea", "soccer/eng.1"),
    ("史丹福橋", "Chelsea", "soccer/eng.1"),
    ("曼城", "Manchester City", "soccer/eng.1"),
    ("哈蘭德", "Manchester City", "soccer/eng.1"),
    ("haaland", "Manchester City", "soccer/eng.1"),
    ("曼聯", "Manchester United", "soccer/eng.1"),
    ("old trafford", "Manchester United", "soccer/eng.1"),
    ("老特拉福德", "Manchester United", "soccer/eng.1"),
    ("熱刺", "Tottenham", "soccer/eng.1"),
    ("紐卡索", "Newcastle", "soccer/eng.1"),
    ("阿斯頓維拉", "Aston Villa", "soccer/eng.1"),
    ("維拉", "Aston Villa", "soccer/eng.1"),
    ("布萊頓", "Brighton", "soccer/eng.1"),
    ("西漢姆", "West Ham", "soccer/eng.1"),
    ("狼隊", "Wolves", "soccer/eng.1"),
    ("水晶宮", "Crystal Palace", "soccer/eng.1"),
    ("富勒姆", "Fulham", "soccer/eng.1"),
    ("布倫特福德", "Brentford", "soccer/eng.1"),
    ("伯恩茅斯", "Bournemouth", "soccer/eng.1"),
    ("諾丁漢森林", "Nottingham Forest", "soccer/eng.1"),
    ("萊斯特", "Leicester", "soccer/eng.1"),
    ("伊普斯威奇", "Ipswich", "soccer/eng.1"),
    ("南安普敦", "Southampton", "soccer/eng.1"),
    ("里茲聯", "Leeds", "soccer/eng.1"),
    # La Liga (soccer/esp.1)
    ("皇家馬德里", "Real Madrid", "soccer/esp.1"),
    ("皇馬", "Real Madrid", "soccer/esp.1"),
    ("姆巴佩", "Real Madrid", "soccer/esp.1"),
    ("mbappe", "Real Madrid", "soccer/esp.1"),
    ("bernabeu", "Real Madrid", "soccer/esp.1"),
    ("伯納烏", "Real Madrid", "soccer/esp.1"),
    ("巴塞隆納", "Barcelona", "soccer/esp.1"),
    ("巴薩", "Barcelona", "soccer/esp.1"),
    ("梅西", "Barcelona", "soccer/esp.1"),
    ("messi", "Barcelona", "soccer/esp.1"),
    ("馬德里競技", "Atletico Madrid", "soccer/esp.1"),
    ("馬競", "Atletico Madrid", "soccer/esp.1"),
    ("畢爾巴鄂", "Athletic Club", "soccer/esp.1"),
    ("皇家社會", "Real Sociedad", "soccer/esp.1"),
    ("塞維利亞", "Sevilla", "soccer/esp.1"),
    ("比亞雷亞爾", "Villarreal", "soccer/esp.1"),
    ("赫羅納", "Girona", "soccer/esp.1"),
    ("瓦倫西亞", "Valencia", "soccer/esp.1"),
    ("貝提斯", "Real Betis", "soccer/esp.1"),
    # Serie A (soccer/ita.1)
    ("國際米蘭", "Inter Milan", "soccer/ita.1"),
    ("國米", "Inter Milan", "soccer/ita.1"),
    ("AC米蘭", "AC Milan", "soccer/ita.1"),
    ("米蘭", "AC Milan", "soccer/ita.1"),
    ("尤文圖斯", "Juventus", "soccer/ita.1"),
    ("祖文圖斯", "Juventus", "soccer/ita.1"),
    ("拿坡里", "Napoli", "soccer/ita.1"),
    ("羅馬", "Roma", "soccer/ita.1"),
    ("拉齊奧", "Lazio", "soccer/ita.1"),
    ("亞特蘭大", "Atalanta", "soccer/ita.1"),
    ("佛羅倫薩", "Fiorentina", "soccer/ita.1"),
    # Bundesliga (soccer/ger.1)
    ("拜仁慕尼黑", "Bayern Munich", "soccer/ger.1"),
    ("拜仁", "Bayern Munich", "soccer/ger.1"),
    ("凱恩", "Bayern Munich", "soccer/ger.1"),
    ("kane", "Bayern Munich", "soccer/ger.1"),
    ("allianz arena", "Bayern Munich", "soccer/ger.1"),
    ("安聯球場", "Bayern Munich", "soccer/ger.1"),
    ("多特蒙德", "Borussia Dortmund", "soccer/ger.1"),
    ("勒沃庫森", "Bayer Leverkusen", "soccer/ger.1"),
    ("萊比錫", "RB Leipzig", "soccer/ger.1"),
    ("斯圖加特", "Stuttgart", "soccer/ger.1"),
    ("法蘭克福", "Eintracht Frankfurt", "soccer/ger.1"),
    ("科隆第一足球俱樂部", "FC Cologne", "soccer/ger.1"),
    ("科隆第一足球", "FC Cologne", "soccer/ger.1"),
    ("科隆", "FC Cologne", "soccer/ger.1"),
    ("fc koln", "FC Cologne", "soccer/ger.1"),
    ("koln", "FC Cologne", "soccer/ger.1"),
    ("cologne", "FC Cologne", "soccer/ger.1"),
    ("rheinenergiestadion", "FC Cologne", "soccer/ger.1"),
    ("弗萊堡", "SC Freiburg", "soccer/ger.1"),
    ("門興", "Borussia Monchengladbach", "soccer/ger.1"),
    ("柏林聯", "1. FC Union Berlin", "soccer/ger.1"),
    ("奧格斯堡", "FC Augsburg", "soccer/ger.1"),
    ("梅因斯", "Mainz", "soccer/ger.1"),
    ("漢堡", "Hamburg SV", "soccer/ger.1"),
    ("沙爾克", "Schalke 04", "soccer/ger.1"),
    ("霍芬海姆", "TSG Hoffenheim", "soccer/ger.1"),
    ("不來梅", "Werder Bremen", "soccer/ger.1"),
    # Ligue 1 (soccer/fra.1)
    ("巴黎聖日耳曼", "Paris Saint-Germain", "soccer/fra.1"),
    ("大巴黎", "Paris Saint-Germain", "soccer/fra.1"),
    ("馬賽", "Marseille", "soccer/fra.1"),
    ("里昂", "Lyon", "soccer/fra.1"),
    ("摩納哥", "Monaco", "soccer/fra.1"),
    ("里爾", "Lille", "soccer/fra.1"),
    # MLB (baseball/mlb)
    ("洛杉磯道奇", "Dodgers", "baseball/mlb"),
    ("道奇球場", "Dodgers", "baseball/mlb"),
    ("dodger stadium", "Dodgers", "baseball/mlb"),
    ("道奇", "Dodgers", "baseball/mlb"),
    ("大谷翔平", "Dodgers", "baseball/mlb"),
    ("山本由伸", "Dodgers", "baseball/mlb"),
    ("紐約洋基", "Yankees", "baseball/mlb"),
    ("洋基球場", "Yankees", "baseball/mlb"),
    ("yankee stadium", "Yankees", "baseball/mlb"),
    ("洋基", "Yankees", "baseball/mlb"),
    ("法官", "Yankees", "baseball/mlb"),
    ("judge", "Yankees", "baseball/mlb"),
    ("舊金山巨人", "Giants", "baseball/mlb"),
    ("巨人", "Giants", "baseball/mlb"),
    ("波士頓紅襪", "Red Sox", "baseball/mlb"),
    ("紅襪", "Red Sox", "baseball/mlb"),
    ("德州遊騎兵", "Rangers", "baseball/mlb"),
    ("遊騎兵", "Rangers", "baseball/mlb"),
    ("洛杉磯天使", "Angels", "baseball/mlb"),
    ("天使", "Angels", "baseball/mlb"),
    ("西雅圖水手", "Mariners", "baseball/mlb"),
    ("水手", "Mariners", "baseball/mlb"),
    ("聖地牙哥教士", "Padres", "baseball/mlb"),
    ("教士", "Padres", "baseball/mlb"),
    ("休士頓太空人", "Astros", "baseball/mlb"),
    ("太空人", "Astros", "baseball/mlb"),
    ("紐約大都會", "Mets", "baseball/mlb"),
    ("大都會", "Mets", "baseball/mlb"),
    ("芝加哥小熊", "Cubs", "baseball/mlb"),
    ("小熊", "Cubs", "baseball/mlb"),
    ("亞特蘭大勇士", "Braves", "baseball/mlb"),
    ("費城費城人", "Phillies", "baseball/mlb"),
    ("費城人", "Phillies", "baseball/mlb"),
    # NBA (basketball/nba)
    ("金州勇士", "Warriors", "basketball/nba"),
    ("大通中心", "Warriors", "basketball/nba"),
    ("chase center", "Warriors", "basketball/nba"),
    ("柯瑞", "Warriors", "basketball/nba"),
    ("curry", "Warriors", "basketball/nba"),
    ("勇士", "Warriors", "basketball/nba"),
    ("洛杉磯湖人", "Lakers", "basketball/nba"),
    ("湖人", "Lakers", "basketball/nba"),
    ("詹姆斯", "Lakers", "basketball/nba"),
    ("lebron", "Lakers", "basketball/nba"),
    ("東契奇", "Lakers", "basketball/nba"),
    ("doncic", "Lakers", "basketball/nba"),
    ("波士頓塞爾提克", "Celtics", "basketball/nba"),
    ("塞爾提克", "Celtics", "basketball/nba"),
    ("紐約尼克", "Knicks", "basketball/nba"),
    ("麥迪遜花園", "Knicks", "basketball/nba"),
    ("madison square garden", "Knicks", "basketball/nba"),
    ("尼克", "Knicks", "basketball/nba"),
    ("丹佛金塊", "Nuggets", "basketball/nba"),
    ("金塊", "Nuggets", "basketball/nba"),
    ("約基奇", "Nuggets", "basketball/nba"),
    ("jokic", "Nuggets", "basketball/nba"),
    ("達拉斯獨行俠", "Mavericks", "basketball/nba"),
    ("獨行俠", "Mavericks", "basketball/nba"),
    ("邁阿密熱火", "Heat", "basketball/nba"),
    ("熱火", "Heat", "basketball/nba"),
    ("密爾瓦基公鹿", "Bucks", "basketball/nba"),
    ("公鹿", "Bucks", "basketball/nba"),
    ("費城76人", "76ers", "basketball/nba"),
    ("76人", "76ers", "basketball/nba"),
    ("洛杉磯快艇", "Clippers", "basketball/nba"),
    ("快艇", "Clippers", "basketball/nba"),
    ("奧克拉荷馬雷霆", "Thunder", "basketball/nba"),
    ("雷霆", "Thunder", "basketball/nba"),
    ("明尼蘇達灰狼", "Timberwolves", "basketball/nba"),
    ("灰狼", "Timberwolves", "basketball/nba"),
    ("鳳凰城太陽", "Suns", "basketball/nba"),
    ("太陽", "Suns", "basketball/nba"),
    ("聖安東尼奧馬刺", "Spurs", "basketball/nba"),
    ("馬刺", "Spurs", "basketball/nba"),
    ("休士頓火箭", "Rockets", "basketball/nba"),
    ("火箭", "Rockets", "basketball/nba"),
)

_SUBLEAGUE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("英超", "soccer/eng.1"),
    ("premier league", "soccer/eng.1"),
    ("epl", "soccer/eng.1"),
    ("西甲", "soccer/esp.1"),
    ("la liga", "soccer/esp.1"),
    ("laliga", "soccer/esp.1"),
    ("義甲", "soccer/ita.1"),
    ("意甲", "soccer/ita.1"),
    ("serie a", "soccer/ita.1"),
    ("德甲", "soccer/ger.1"),
    ("bundesliga", "soccer/ger.1"),
    ("法甲", "soccer/fra.1"),
    ("ligue 1", "soccer/fra.1"),
    ("ligue1", "soccer/fra.1"),
)

_ESPN_TEAMS_CACHE: dict[str, list[dict[str, str]]] = {}

_MOCK_LEAGUE_PRESETS: dict[str, dict[str, Any]] = {
    "MLB": {
        "city": "洛杉磯",
        "flight_text": "桃園 (TPE) ↔ 洛杉磯 (LAX) 直飛來回經濟艙參考航班（含 2 件托運行李建議）",
        "hotel_text": "洛杉磯市中心 / 小東京精選商務飯店（鄰近 Union Station，直達球場接駁線）",
        "base_ticket_budget": 14000,
        "matches": [
            (
                "洛杉磯道奇 vs 紐約洋基",
                "Dodger Stadium（道奇體育場）",
                "建議座位：內野下層一壘側 Field Box，視野佳且鄰近餐飲區",
            ),
            (
                "洛杉磯道奇 vs 舊金山巨人",
                "Dodger Stadium（道奇體育場）",
                "經典國聯西區世仇對戰，建議提前 90 分鐘進場參觀紀念品商店",
            ),
        ],
        "default_sources": [
            {
                "title": "MLB Official Schedule & Scores",
                "url": "https://www.mlb.com/schedule",
                "snippet": "美國職棒大聯盟官方賽程與對戰場次資訊（mlb.com）。",
            },
            {
                "title": "ESPN MLB Schedule & Standings",
                "url": "https://www.espn.com/mlb/schedule",
                "snippet": "ESPN 賽事轉播時刻表與先發投手焦點前瞻（espn.com）。",
            },
        ],
        "activity_pool": [
            "Union Station 歷史建築巡禮與 MLB 官方周邊旗艦店採購",
            "道奇體育場賽前導覽與主場觀賽體驗",
            "聖塔莫尼卡海灘與好萊塢星光大道",
            "小東京美食探索與格里菲斯天文台夕陽行程",
        ],
    },
    "NBA": {
        "city": "舊金山／洛杉磯",
        "flight_text": "桃園 (TPE) ↔ 舊金山 (SFO) 晚去早回直飛建議航班，動線順暢免轉機",
        "hotel_text": "舊金山聯合廣場 / Mission Bay 捷運沿線四星級飯店（步行可達或輕軌直達球館）",
        "base_ticket_budget": 16000,
        "matches": [
            (
                "金州勇士 vs 洛杉磯湖人",
                "Chase Center（大通中心）",
                "建議座位：下層 100 區邊線視角，近距離感受 NBA 球星攻防節奏",
            ),
            (
                "金州勇士 vs 波士頓塞爾提克",
                "Chase Center（大通中心）",
                "焦點強權對決，建議提早入場觀看賽前熱身與燈光秀",
            ),
        ],
        "default_sources": [
            {
                "title": "NBA Official Schedule",
                "url": "https://www.nba.com/schedule",
                "snippet": "NBA 美國職籃官方例行賽與季後賽賽程表（nba.com）。",
            },
            {
                "title": "ESPN NBA Daily Schedule",
                "url": "https://www.espn.com/nba/schedule",
                "snippet": "ESPN NBA 賽程、場館資訊與傷兵名單摘要（espn.com）。",
            },
        ],
        "activity_pool": [
            "大通中心 Thrive City 廣場與 Warriors Team Store 朝聖",
            "NBA 主場高張力晚場觀賽",
            "漁人碼頭 39 號碼頭與金門大橋觀光",
            "惡魔島或納帕酒莊半日文化體驗",
        ],
    },
    "歐足五大": {
        "city": "倫敦／歐洲主場城市",
        "flight_text": "桃園 (TPE) ↔ 歐洲主要樞紐機場精品航空去程過夜班機（抵達即可銜接市區鐵路）",
        "hotel_text": "市中心主要火車站周邊精品旅宿（地鐵交會且往返球場與跨城交通便利）",
        "base_ticket_budget": 15000,
        "matches": [
            (
                "兵工廠 vs 利物浦（英超焦點戰）",
                "Emirates Stadium（酋長球場）",
                "建議席次：Club Level 或東看台下層，含官方電子票入場動線指引",
            ),
            (
                "切爾西 vs 熱刺（倫敦德比）",
                "Stamford Bridge（史丹福橋球場）",
                "高人氣倫敦德比，建議提前 2 小時至球場周邊酒吧感受賽前氛圍",
            ),
        ],
        "default_sources": [
            {
                "title": "Premier League Official Fixtures",
                "url": "https://www.premierleague.com/fixtures",
                "snippet": "英格蘭足球超級聯賽官方賽程與開球時間（premierleague.com）。",
            },
            {
                "title": "ESPN Football Scores & Fixtures",
                "url": "https://www.espn.com/soccer/fixtures",
                "snippet": "歐洲五大聯賽（英超、西甲、義甲、德甲、法甲）賽程彙整（espn.com）。",
            },
        ],
        "activity_pool": [
            "主場球場幕後導覽（球員通道與更衣室參觀）及球衣印字",
            "聯賽主場 90 分鐘全場觀賽與賽後球迷街區散步",
            "市中心經典博物館、舊城區與河岸遊船巡禮",
            "當地特色市集與商圈自由購物散策",
        ],
    },
    "歐冠": {
        "city": "馬德里／慕尼黑",
        "flight_text": "桃園 (TPE) ↔ 馬德里巴拉哈斯 (MAD) 建議航班（轉機一次，含早鳥接送建議）",
        "hotel_text": "馬德里卡斯蒂利亞大道 / 太陽門廣場精選飯店（地鐵 10 號線直達伯納烏球場）",
        "base_ticket_budget": 19000,
        "matches": [
            (
                "皇家馬德里 vs 拜仁慕尼黑（歐冠之夜）",
                "Santiago Bernabéu（聖地牙哥·伯納烏球場）",
                "歐冠晚場 21:00 開球，建議選擇中層側翼看台兼顧全景與臨場震撼",
            ),
            (
                "巴塞隆納 vs 巴黎聖日耳曼（歐冠焦點）",
                "Estadi Olímpic / 巴塞隆納主場",
                "歐冠淘汰賽氛圍濃厚，建議預留散場地鐵人潮緩衝時間",
            ),
        ],
        "default_sources": [
            {
                "title": "UEFA Champions League Fixtures & Results",
                "url": "https://www.uefa.com/uefachampionsleague/fixtures-results/",
                "snippet": "歐洲冠軍聯賽官方賽程、分組與淘汰賽對戰樹（uefa.com）。",
            },
            {
                "title": "ESPN UEFA Champions League Schedule",
                "url": "https://www.espn.com/soccer/league/_/name/uefa.champions",
                "snippet": "歐冠賽事焦點報導與開球時間表（espn.com）。",
            },
        ],
        "activity_pool": [
            "伯納烏球場博物館巡禮與歐冠官方紀念品挑選",
            "歐冠聯賽之夜旗艦觀賽（體驗歐冠主題曲全場大合唱）",
            "普拉多美術館、馬約爾廣場與當地美食市集",
            "城市公園散步與格蘭大道城市探索",
        ],
    },
}


def _normalize_text(text: str) -> str:
    """Lowercase and strip accents/diacritics (e.g. 'FC Köln' -> 'fc koln')."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def translate_preference_to_english(
    preference: str,
    league_type: str | None = None,
) -> tuple[str, str | None]:
    """Translate Chinese/accented team, player, venue, or sub-league names to English."""
    cleaned = preference.strip()
    if not cleaned:
        return "", None

    cleaned_norm = _normalize_text(cleaned)
    allowed_paths = {
        path for _, path in _ESPN_LEAGUE_PATHS.get(league_type or "", [])
    }

    matches: list[tuple[int, str, str | None]] = []
    for zh_kw, en_kw, espn_path in _TEAM_ZH_TO_EN:
        kw_norm = _normalize_text(zh_kw)
        if kw_norm and kw_norm in cleaned_norm:
            path_bonus = 1 if (not allowed_paths or espn_path in allowed_paths) else 0
            matches.append((path_bonus * 100 + len(kw_norm), en_kw, espn_path))

    if matches:
        matches.sort(key=lambda item: item[0], reverse=True)
        _, best_en, best_path = matches[0]
        return best_en, best_path

    for sub_kw, sub_path in _SUBLEAGUE_KEYWORDS:
        if sub_kw in cleaned_norm and (not allowed_paths or sub_path in allowed_paths):
            return cleaned, sub_path

    return cleaned, None


def parse_preference_targets(
    preference: str,
    league_type: str | None = None,
) -> list[tuple[str, str | None, str]]:
    """Split multi-condition preferences into unique (en_keyword, espn_path, raw_chunk) tuples."""
    cleaned = preference.strip()
    if not cleaned:
        return []

    raw_chunks = [
        part.strip()
        for part in re.split(r"[、,，/+&]+|\s+[與和]\s+", cleaned)
        if part and part.strip()
    ]
    if not raw_chunks:
        raw_chunks = [cleaned]

    targets: list[tuple[str, str | None, str]] = []
    seen: set[tuple[str, str | None]] = set()
    for chunk in raw_chunks:
        en_kw, espn_path = translate_preference_to_english(chunk, league_type)
        key = (_normalize_text(en_kw), espn_path)
        if key not in seen:
            seen.add(key)
            targets.append((en_kw, espn_path, chunk))
    return targets


def _is_authoritative_url(url: str, allowed_domains: tuple[str, ...]) -> bool:
    """Return True only when URL belongs to an authoritative domain (filtering out ads)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in allowed_domains
    )


def validate_trip_request(
    league_type: str,
    start_date: date,
    end_date: date,
) -> str | None:
    """Validate supported league scope and trip date boundaries."""
    if league_type not in config.SUPPORTED_LEAGUES:
        allowed = "、".join(config.SUPPORTED_LEAGUES)
        return f"不支援的賽事聯賽「{league_type}」，系統僅支援：{allowed}。"
    if end_date < start_date:
        return "回程日期不可早於出發日期，請重新確認旅遊區間。"
    duration_days = (end_date - start_date).days + 1
    if duration_days > MAX_TRIP_DAYS:
        return f"單筆行程規劃天數不可超過 {MAX_TRIP_DAYS} 天（目前選擇 {duration_days} 天）。"
    return None


def _parse_espn_event(
    event: dict[str, Any],
    label: str,
    date_str: str,
    default_url: str,
) -> dict[str, str] | None:
    """Extract normalized match fields from an ESPN scoreboard or team schedule event."""
    matchup = str(event.get("name") or event.get("shortName") or "").strip()
    if not matchup:
        return None
    kickoff_utc = str(event.get("date") or "").strip()
    competitions = event.get("competitions") or []
    venue = ""
    if competitions and isinstance(competitions[0], dict):
        venue_obj = competitions[0].get("venue") or {}
        if isinstance(venue_obj, dict):
            venue = str(venue_obj.get("fullName") or "").strip()
    links = event.get("links") or []
    event_url = default_url
    if links and isinstance(links[0], dict) and links[0].get("href"):
        event_url = str(links[0]["href"])
    return {
        "date": date_str,
        "kickoff_utc": kickoff_utc,
        "league": label,
        "matchup": matchup,
        "venue": venue or "官方指定球場",
        "url": event_url,
    }


def _fetch_espn_teams(client: httpx.Client, path: str) -> list[dict[str, str]]:
    """Fetch and cache the ESPN team directory for a league path."""
    if path in _ESPN_TEAMS_CACHE:
        return _ESPN_TEAMS_CACHE[path]

    api_url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/teams"
    try:
        resp = client.get(api_url, params={"limit": "50"})
        if resp.status_code != 200:
            return []
        payload = resp.json()
        sports = payload.get("sports") or []
        leagues = sports[0].get("leagues") or [] if sports else []
        raw_teams = leagues[0].get("teams") or [] if leagues else []
    except Exception:
        return []

    teams: list[dict[str, str]] = []
    for entry in raw_teams:
        team_obj = entry.get("team") if isinstance(entry, dict) else None
        if not isinstance(team_obj, dict):
            continue
        team_id = str(team_obj.get("id") or "").strip()
        display_name = str(team_obj.get("displayName") or "").strip()
        if not team_id or not display_name:
            continue
        teams.append(
            {
                "id": team_id,
                "name": display_name,
                "short_name": str(team_obj.get("shortDisplayName") or "").strip(),
                "nickname": str(team_obj.get("nickname") or "").strip(),
            }
        )

    if teams:
        _ESPN_TEAMS_CACHE[path] = teams
    return teams


def get_league_teams(espn_path: str) -> list[dict[str, str]]:
    """Return formatted official teams for a valid ESPN league path (cached + offline fallback)."""
    if espn_path not in LEAGUE_OPTION_BY_PATH:
        raise ValueError(f"Unsupported league espn_path: {espn_path}")

    teams = _ESPN_TEAMS_CACHE.get(espn_path)
    if not teams:
        try:
            with httpx.Client(timeout=5.0, follow_redirects=True) as client:
                teams = _fetch_espn_teams(client, espn_path)
        except Exception:
            teams = []

    if not teams:
        teams = _FALLBACK_TEAMS_BY_PATH.get(espn_path, [])

    formatted: list[dict[str, str]] = []
    for item in teams:
        name = str(item.get("name") or "").strip()
        team_id = str(item.get("id") or "").strip()
        if not name or not team_id:
            continue
        zh_name = _EN_TEAM_ZH_LABELS.get(name, "")
        formatted.append(
            {
                "id": team_id,
                "name": name,
                "zh_name": zh_name,
                "label": f"{name}（{zh_name}）" if zh_name else name,
            }
        )
    formatted.sort(key=lambda t: t["name"].lower())
    return formatted


def validate_structured_targets(
    raw_targets: list[dict[str, Any]],
    start_date: date,
    end_date: date,
) -> tuple[list[dict[str, str]], str | None]:
    """Validate structured multi-league/cross-continent targets and trip date bounds."""
    if end_date < start_date:
        return [], "回程日期不可早於出發日期，請重新確認旅遊區間。"
    duration_days = (end_date - start_date).days + 1
    if duration_days > MAX_TRIP_DAYS:
        return [], f"單筆行程規劃天數不可超過 {MAX_TRIP_DAYS} 天（目前選擇 {duration_days} 天）。"

    if not isinstance(raw_targets, list) or not raw_targets:
        return [], "請至少加入 1 筆觀賽賽事目標。"
    if len(raw_targets) > 8:
        return [], "單筆行程最多支援加入 8 筆觀賽目標。"

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_targets:
        if not isinstance(item, dict):
            continue
        espn_path = str(item.get("espn_path") or "").strip()
        opt = LEAGUE_OPTION_BY_PATH.get(espn_path)
        if opt is None:
            return (
                [],
                f"不支援的賽事聯賽代碼「{espn_path}」，系統僅支援北美（MLB、NBA）與歐洲（五大聯賽、歐冠）。",
            )
        team_id = str(item.get("team_id") or "").strip()
        team_name = str(item.get("team_name") or "").strip()
        key = (espn_path, team_id)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "espn_path": espn_path,
                "league_type": opt["league_type"],
                "sub_league": opt["sub_league"],
                "region": opt["region"],
                "region_label": opt["region_label"],
                "team_id": team_id,
                "team_name": team_name,
            }
        )

    if not normalized:
        return [], "請至少加入 1 筆有效的觀賽賽事目標。"

    regions = {t["region"] for t in normalized if t.get("region")}
    if len(regions) > 1 and duration_days < 6:
        return (
            [],
            f"跨洲行程（同時包含北美與歐洲賽事）須預留跨洋長程飛行與時差緩衝，旅遊總天數至少須 6 天以上（目前為 {duration_days} 天）。",
        )
    return normalized, None


def summarize_targets_league_type(targets: list[dict[str, str]]) -> str:
    """Summarize structured targets into a compact TripPlan.league_type label."""
    ordered: list[str] = []
    for t in targets:
        lt = t["league_type"]
        if lt not in ordered:
            ordered.append(lt)
    return "+".join(ordered) if ordered else "MLB"


def fetch_espn_schedules_for_targets(
    targets: list[dict[str, str]],
    start_date: date,
    end_date: date,
    meta: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Fetch live ESPN schedules across structured targets (supporting cross-league & cross-continent)."""
    if not targets:
        return []

    total_days = min((end_date - start_date).days + 1, MAX_TRIP_DAYS)
    raw_events: list[dict[str, str]] = []
    unique_leagues: list[tuple[str, str]] = []
    seen_leagues: set[str] = set()
    team_labels: list[str] = []
    team_keywords: list[str] = []

    for tgt in targets:
        path = tgt["espn_path"]
        sub_league = tgt["sub_league"]
        if path not in seen_leagues:
            seen_leagues.add(path)
            unique_leagues.append((sub_league, path))
        if tgt.get("team_name"):
            team_labels.append(f"{tgt['team_name']} ({sub_league})")
            base_en = tgt["team_name"].split("（")[0].strip()
            team_keywords.append(_normalize_text(base_en))

    def _fetch_scoreboard_day(
        client: httpx.Client, label: str, path: str, target_date: date
    ) -> list[dict[str, str]]:
        date_compact = target_date.strftime("%Y%m%d")
        api_url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
        try:
            resp = client.get(api_url, params={"dates": date_compact})
            if resp.status_code != 200:
                return []
            payload = resp.json()
        except Exception:
            return []

        items: list[dict[str, str]] = []
        default_url = f"https://www.espn.com/{path}/scoreboard/_/date/{date_compact}"
        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                continue
            parsed = _parse_espn_event(
                event=event,
                label=label,
                date_str=target_date.isoformat(),
                default_url=default_url,
            )
            if parsed is not None:
                items.append(parsed)
        return items

    try:
        with httpx.Client(timeout=5.0, follow_redirects=True) as client:
            for tgt in targets:
                if tgt.get("team_id"):
                    raw_events.extend(
                        _fetch_team_season_schedule(
                            client,
                            tgt["sub_league"],
                            tgt["espn_path"],
                            tgt["team_id"],
                            start_date,
                            end_date,
                        )
                    )

            if len(unique_leagues) == 1 or total_days <= 10:
                query_dates = [
                    start_date + timedelta(days=i) for i in range(total_days)
                ]
            else:
                sample_count = 10
                indices = sorted(
                    {
                        round(i * (total_days - 1) / (sample_count - 1))
                        for i in range(sample_count)
                    }
                )
                query_dates = [start_date + timedelta(days=idx) for idx in indices]

            tasks = [
                (label, path, d) for label, path in unique_leagues for d in query_dates
            ]
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [
                    executor.submit(_fetch_scoreboard_day, client, label, path, d)
                    for label, path, d in tasks
                ]
                for fut in as_completed(futures):
                    try:
                        raw_events.extend(fut.result())
                    except Exception:
                        continue
    except Exception:
        return []

    seen_ev: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []
    for ev in raw_events:
        key = (ev["date"], ev["matchup"])
        if key not in seen_ev:
            seen_ev.add(key)
            deduped.append(ev)

    kw_candidates = [kw for kw in team_keywords if len(kw) >= 2]
    selected = _select_balanced_schedules(deduped, kw_candidates, limit=25)

    regions = {t["region"] for t in targets}
    if meta is not None:
        meta["strategy"] = "structured_targets"
        meta["matched_team"] = ", ".join(dict.fromkeys(team_labels))
        meta["total_found"] = len(deduped)
        meta["covered_range"] = f"{start_date.isoformat()} ~ {end_date.isoformat()}"
        meta["queried_leagues"] = [lbl for lbl, _ in unique_leagues]
        meta["is_cross_league"] = len(unique_leagues) > 1
        meta["is_cross_continent"] = len(regions) > 1

    return selected


def _find_matching_teams(
    client: httpx.Client,
    target_leagues: list[tuple[str, str]],
    targets: list[tuple[str, str | None, str]],
    max_teams: int = 3,
) -> list[tuple[str, str, str, str]]:
    """Locate up to max_teams ESPN teams matching multi-condition user preferences."""
    ignored = {"fc", "sc", "sv", "club", "city", "united", "real", "inter", "ac"}
    matched_teams: list[tuple[str, str, str, str]] = []
    seen_ids: set[tuple[str, str]] = set()

    for en_kw, espn_path, raw_chunk in targets:
        candidates: list[str] = []
        for term in (en_kw, raw_chunk):
            norm = _normalize_text(term)
            if len(norm) >= 3 and norm not in ignored and norm not in candidates:
                candidates.append(norm)
        if not candidates:
            continue

        search_leagues = (
            [item for item in target_leagues if item[1] == espn_path]
            if espn_path
            else target_leagues
        ) or target_leagues

        best_score = 0
        best_match: tuple[str, str, str, str] | None = None
        for label, path in search_leagues:
            for team in _fetch_espn_teams(client, path):
                names = [
                    _normalize_text(team["name"]),
                    _normalize_text(team["short_name"]),
                    _normalize_text(team["nickname"]),
                ]
                for cand in candidates:
                    for t_name in names:
                        if not t_name:
                            continue
                        if cand == t_name:
                            score = 1000 + len(cand)
                        elif len(cand) >= 4 and (cand in t_name or t_name in cand):
                            score = 500 + min(len(cand), len(t_name))
                        else:
                            continue
                        if score > best_score:
                            best_score = score
                            best_match = (label, path, team["id"], team["name"])

        if best_match is not None:
            id_key = (best_match[1], best_match[2])
            if id_key not in seen_ids:
                seen_ids.add(id_key)
                matched_teams.append(best_match)
                if len(matched_teams) >= max_teams:
                    break

    return matched_teams


def _fetch_team_season_schedule(
    client: httpx.Client,
    label: str,
    path: str,
    team_id: str,
    start_date: date,
    end_date: date,
) -> list[dict[str, str]]:
    """Fetch full-season team schedule in 1-2 requests and filter to [start_date, end_date]."""
    api_url = (
        f"https://site.api.espn.com/apis/site/v2/sports/{path}/teams/{team_id}/schedule"
    )
    param_sets: list[dict[str, str]] = (
        [{"fixture": "true"}] if path.startswith("soccer/") else [{}]
    )
    if path.startswith("soccer/") and start_date <= date.today():
        param_sets.append({})

    start_iso = start_date.isoformat()
    end_iso = end_date.isoformat()
    results: list[dict[str, str]] = []

    for params in param_sets:
        try:
            resp = client.get(api_url, params=params or None)
            if resp.status_code != 200:
                continue
            payload = resp.json()
        except Exception:
            continue

        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                continue
            raw_date = str(event.get("date") or "").strip()
            event_iso = raw_date[:10]
            if len(event_iso) != 10 or not (start_iso <= event_iso <= end_iso):
                continue
            parsed = _parse_espn_event(
                event=event,
                label=label,
                date_str=event_iso,
                default_url=f"https://www.espn.com/{path}/team/_/id/{team_id}",
            )
            if parsed is not None:
                results.append(parsed)

    return results


def _select_balanced_schedules(
    events: list[dict[str, str]],
    kw_candidates: list[str],
    limit: int = 25,
) -> list[dict[str, str]]:
    """Keep all preferred-team/venue matches first, then distribute remaining slots across all dates."""
    preferred: list[dict[str, str]] = []
    others: list[dict[str, str]] = []

    for ev in events:
        haystack = _normalize_text(f"{ev['matchup']} {ev['venue']}")
        if kw_candidates and any(kw in haystack for kw in kw_candidates):
            preferred.append(ev)
        else:
            others.append(ev)

    preferred.sort(key=lambda ev: (ev["date"], ev["matchup"]))
    others.sort(key=lambda ev: (ev["date"], ev["matchup"]))

    selected = list(preferred[:limit])
    remaining_slots = limit - len(selected)
    if remaining_slots <= 0 or not others:
        return selected

    by_date: dict[str, list[dict[str, str]]] = {}
    for ev in others:
        by_date.setdefault(ev["date"], []).append(ev)

    ordered_dates = sorted(by_date.keys())
    balanced_others: list[dict[str, str]] = []
    idx = 0
    while len(balanced_others) < remaining_slots:
        added_this_round = False
        for d in ordered_dates:
            bucket = by_date[d]
            if idx < len(bucket):
                balanced_others.append(bucket[idx])
                added_this_round = True
                if len(balanced_others) >= remaining_slots:
                    break
        if not added_this_round:
            break
        idx += 1

    balanced_others.sort(key=lambda ev: (ev["date"], ev["matchup"]))
    return selected + balanced_others


def fetch_espn_schedules(
    league_type: str,
    start_date: date,
    end_date: date,
    preference: str = "",
    meta: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Fetch real match schedules via ESPN multi-team season and date-scoreboard APIs."""
    target_leagues = _ESPN_LEAGUE_PATHS.get(league_type, [])
    if not target_leagues:
        return []

    targets = parse_preference_targets(preference, league_type)
    detected_paths = {path for _, path, _ in targets if path}
    if league_type == "歐足五大" and detected_paths:
        narrowed = [item for item in target_leagues if item[1] in detected_paths]
        if narrowed:
            target_leagues = narrowed

    total_days = min((end_date - start_date).days + 1, MAX_TRIP_DAYS)
    raw_events: list[dict[str, str]] = []
    strategy = "league_scoreboard"
    matched_teams: list[tuple[str, str, str, str]] = []

    def _fetch_scoreboard_day(
        client: httpx.Client, label: str, path: str, target_date: date
    ) -> list[dict[str, str]]:
        date_compact = target_date.strftime("%Y%m%d")
        api_url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
        try:
            resp = client.get(api_url, params={"dates": date_compact})
            if resp.status_code != 200:
                return []
            payload = resp.json()
        except Exception:
            return []

        items: list[dict[str, str]] = []
        default_url = f"https://www.espn.com/{path}/scoreboard/_/date/{date_compact}"
        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                continue
            parsed = _parse_espn_event(
                event=event,
                label=label,
                date_str=target_date.isoformat(),
                default_url=default_url,
            )
            if parsed is not None:
                items.append(parsed)
        return items

    try:
        with httpx.Client(timeout=5.0, follow_redirects=True) as client:
            if targets:
                matched_teams = _find_matching_teams(client, target_leagues, targets)
                if matched_teams:
                    strategy = "team_schedule"
                    for m_label, m_path, m_id, _ in matched_teams:
                        raw_events.extend(
                            _fetch_team_season_schedule(
                                client, m_label, m_path, m_id, start_date, end_date
                            )
                        )
                    matched_league_set = {(m[0], m[1]) for m in matched_teams}
                    target_leagues = sorted(matched_league_set)

            if len(target_leagues) == 1 or total_days <= 14:
                query_dates = [
                    start_date + timedelta(days=i) for i in range(total_days)
                ]
            else:
                sample_count = 14
                indices = sorted(
                    {
                        round(i * (total_days - 1) / (sample_count - 1))
                        for i in range(sample_count)
                    }
                )
                query_dates = [start_date + timedelta(days=idx) for idx in indices]

            tasks = [
                (label, path, d) for label, path in target_leagues for d in query_dates
            ]
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [
                    executor.submit(_fetch_scoreboard_day, client, label, path, d)
                    for label, path, d in tasks
                ]
                for fut in as_completed(futures):
                    try:
                        raw_events.extend(fut.result())
                    except Exception:
                        continue
    except Exception:
        return []

    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []
    for ev in raw_events:
        key = (ev["date"], ev["matchup"])
        if key not in seen:
            seen.add(key)
            deduped.append(ev)

    kw_candidates: list[str] = []
    for kw in (
        *[m[3] for m in matched_teams],
        *[t[0] for t in targets],
        *[t[2] for t in targets],
    ):
        norm = _normalize_text(kw)
        if len(norm) >= 2 and norm not in kw_candidates:
            kw_candidates.append(norm)

    selected = _select_balanced_schedules(deduped, kw_candidates, limit=25)
    if meta is not None:
        meta["strategy"] = strategy
        meta["matched_team"] = ", ".join(
            f"{m[3]} ({m[0]})" for m in matched_teams
        )
        meta["total_found"] = len(deduped)
        meta["covered_range"] = f"{start_date.isoformat()} ~ {end_date.isoformat()}"
        meta["queried_leagues"] = [lbl for lbl, _ in target_leagues]
    return selected


def search_authoritative_schedules(
    league_type: str,
    start_date: date,
    end_date: date,
    preference: str = "",
    max_results: int = 4,
) -> list[dict[str, str]]:
    """Search official/authoritative sports sites via ddgs without raising on failure."""
    domain_list: list[str] = []
    for part in league_type.split("+"):
        for dom in config.LEAGUE_AUTHORITATIVE_DOMAINS.get(part.strip(), ()):
            if dom not in domain_list:
                domain_list.append(dom)
    domains = tuple(domain_list)
    if not domains:
        return []

    targets = parse_preference_targets(preference, league_type)
    en_terms = " ".join(t[0] for t in targets[:2]) if targets else ""
    site_filter = " OR ".join(f"site:{domain}" for domain in domains[:2])
    base_terms = (
        f"{en_terms} schedule fixtures"
        if en_terms
        else _LEAGUE_SEARCH_TERMS.get(league_type, league_type)
    )
    month_hint = start_date.strftime("%Y %B")
    query = f"({site_filter}) {base_terms} {month_hint}".strip()

    try:
        from ddgs import DDGS

        raw_results = DDGS(timeout=6).text(query, max_results=max_results * 2)
        sources: list[dict[str, str]] = []
        for item in raw_results or []:
            title = str(item.get("title") or "").strip()
            url = str(item.get("href") or item.get("url") or "").strip()
            snippet = str(item.get("body") or item.get("snippet") or "").strip()
            if title and url and _is_authoritative_url(url, domains):
                sources.append(
                    {
                        "title": title[:120],
                        "url": url,
                        "snippet": snippet[:220],
                    }
                )
            if len(sources) >= max_results:
                break
        return sources
    except Exception:
        return []


def _get_league_preset(league_type: str) -> dict[str, Any]:
    """Resolve _MOCK_LEAGUE_PRESETS safely even for combined league labels like MLB+歐冠."""
    if league_type in _MOCK_LEAGUE_PRESETS:
        return _MOCK_LEAGUE_PRESETS[league_type]
    for part in league_type.split("+"):
        cleaned = part.strip()
        if cleaned in _MOCK_LEAGUE_PRESETS:
            return _MOCK_LEAGUE_PRESETS[cleaned]
    return _MOCK_LEAGUE_PRESETS["MLB"]


def _estimate_budget_twd(
    league_type: str,
    num_days: int,
    match_count: int,
    need_flight: bool,
    need_hotel: bool,
    is_cross_continent: bool = False,
) -> int:
    """Compute a consistent indicative TWD budget from trip parameters."""
    preset = _get_league_preset(league_type)
    estimated = preset["base_ticket_budget"] * max(match_count, 1)
    if need_flight:
        estimated += 38000
        if is_cross_continent:
            estimated += 28000
    if need_hotel:
        estimated += 6500 * max(num_days - 1, 1)
    return estimated


_NA_REGION_KEYWORDS: tuple[str, ...] = (
    "mlb",
    "nba",
    "baseball",
    "basketball",
    "dodgers",
    "yankees",
    "giants",
    "red sox",
    "cubs",
    "mets",
    "phillies",
    "padres",
    "braves",
    "astros",
    "warriors",
    "lakers",
    "celtics",
    "knicks",
    "nuggets",
    "miami heat",
    "phoenix suns",
    "spurs",
    "chase center",
    "dodger stadium",
    "yankee stadium",
    "oracle park",
    "madison square garden",
    "fenway park",
    "crypto.com arena",
    "td garden",
    "紐約洋基",
    "洛杉磯道奇",
    "金州勇士",
    "洛杉磯湖人",
    "波士頓塞爾提克",
)

_EU_REGION_KEYWORDS: tuple[str, ...] = (
    "英超",
    "西甲",
    "義甲",
    "德甲",
    "法甲",
    "歐冠",
    "歐足",
    "premier",
    "la liga",
    "laliga",
    "serie a",
    "bundesliga",
    "ligue 1",
    "uefa",
    "champions",
    "soccer",
    "arsenal",
    "liverpool",
    "everton",
    "chelsea",
    "manchester",
    "tottenham",
    "real madrid",
    "barcelona",
    "atletico",
    "bayern",
    "dortmund",
    "cologne",
    "koln",
    "psg",
    "paris saint",
    "inter milan",
    "internazionale",
    "ac milan",
    "juventus",
    "emirates",
    "anfield",
    "stamford bridge",
    "etihad",
    "old trafford",
    "bernabeu",
    "allianz arena",
    "camp nou",
    "hill dickinson",
    "兵工廠",
    "利物浦",
    "艾佛頓",
    "切爾西",
    "熱刺",
    "皇家馬德里",
    "巴塞隆納",
    "拜仁慕尼黑",
    "巴黎聖日耳曼",
    "伯納烏",
    "酋長球場",
)


def infer_match_region(
    match: dict[str, Any], default_league_type: str = ""
) -> str:
    """Infer continent code ('NA' or 'EU') from match metadata and fallback league_type."""
    explicit = str(match.get("region") or "").strip().upper()
    if explicit in ("NA", "EU"):
        return explicit

    league_text = _normalize_text(str(match.get("league") or ""))
    if any(kw in league_text for kw in ("mlb", "nba", "baseball", "basketball")):
        return "NA"
    if any(
        kw in league_text
        for kw in (
            "英超",
            "西甲",
            "義甲",
            "德甲",
            "法甲",
            "歐冠",
            "歐足",
            "premier",
            "la liga",
            "serie a",
            "bundesliga",
            "ligue 1",
            "uefa",
            "champions",
            "soccer",
        )
    ):
        return "EU"

    combined = _normalize_text(
        f"{match.get('league', '')} {match.get('matchup', '')} {match.get('venue', '')}"
    )
    if any(kw in combined for kw in _EU_REGION_KEYWORDS):
        return "EU"
    if any(kw in combined for kw in _NA_REGION_KEYWORDS):
        return "NA"

    if "歐" in (default_league_type or ""):
        return "EU"
    return "NA"


def _parse_kickoff_dt(kickoff_str: str) -> datetime | None:
    """Parse ESPN ISO-8601 kickoff timestamp into an aware UTC datetime when available."""
    raw = str(kickoff_str or "").strip()
    if not raw:
        return None
    normalized = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def validate_schedule_feasibility(
    matches: list[dict[str, Any]],
    start_date: date,
    end_date: date,
    need_flight: bool,
    league_type: str = "",
    is_cross_continent: bool = False,
) -> dict[str, list[str]]:
    """Validate same-day overlap, flight-day conflicts, and cross-continent transit buffers."""
    errors: list[str] = []
    warnings: list[str] = []
    num_days = (end_date - start_date).days + 1

    if is_cross_continent and num_days < 6:
        errors.append(
            f"跨洲行程（北美 ↔ 歐洲）需預留往返與跨洋飛行緩衝，旅遊總天數至少須 6 天以上（目前為 {num_days} 天）。"
        )

    parsed_items: list[dict[str, Any]] = []
    for m in matches:
        if not isinstance(m, dict):
            continue
        date_str = str(m.get("date") or "").strip()[:10]
        matchup = str(m.get("matchup") or "未命名賽事").strip()
        venue_raw = str(m.get("venue") or "").strip()
        kickoff_raw = str(m.get("kickoff_utc") or "").strip()
        try:
            m_date = date.fromisoformat(date_str)
        except ValueError:
            errors.append(f"賽事「{matchup}」日期格式無效（{date_str}）。")
            continue

        parsed_items.append(
            {
                "date_str": m_date.isoformat(),
                "date_obj": m_date,
                "matchup": matchup,
                "venue_raw": venue_raw or "官方指定球場",
                "venue_norm": _normalize_text(venue_raw),
                "kickoff_dt": _parse_kickoff_dt(kickoff_raw),
                "kickoff_raw": kickoff_raw,
                "region": infer_match_region(m, league_type),
            }
        )

    parsed_items.sort(
        key=lambda item: (item["date_str"], item["kickoff_raw"], item["matchup"])
    )

    for item in parsed_items:
        m_date = item["date_obj"]
        if m_date < start_date or m_date > end_date:
            errors.append(
                f"賽事「{item['matchup']}」（{item['date_str']}）超出旅遊日期區間（{start_date.isoformat()} ~ {end_date.isoformat()}）。"
            )
            continue
        if need_flight and num_days >= 3 and (m_date == start_date or m_date == end_date):
            role_label = "出發日" if m_date == start_date else "回程日"
            errors.append(
                f"已勾選代訂國際機票，{role_label}（{item['date_str']}）須預留長程飛行與通關時間，請勿排入正賽「{item['matchup']}」（或改為機票自理）。"
            )

    by_date: dict[str, list[dict[str, Any]]] = {}
    for item in parsed_items:
        by_date.setdefault(item["date_str"], []).append(item)

    for d_str, day_list in by_date.items():
        if len(day_list) < 2:
            continue
        has_conflict = False
        conflict_names: list[str] = []
        for i in range(len(day_list)):
            for j in range(i + 1, len(day_list)):
                a = day_list[i]
                b = day_list[j]
                diff_venue = (
                    not a["venue_norm"]
                    or not b["venue_norm"]
                    or a["venue_norm"] != b["venue_norm"]
                )
                dt_a = a["kickoff_dt"]
                dt_b = b["kickoff_dt"]
                pair_conflict = (
                    diff_venue
                    or dt_a is None
                    or dt_b is None
                    or abs((dt_b - dt_a).total_seconds()) < 5 * 3600
                )
                if pair_conflict:
                    has_conflict = True
                    for nm in (a["matchup"], b["matchup"]):
                        if nm not in conflict_names:
                            conflict_names.append(nm)
        if has_conflict:
            errors.append(
                f"同一天（{d_str}）排入多場時間或場館衝突賽事（{'、'.join(conflict_names)}），無法同時觀賽，請取消其中一場。"
            )

    for idx in range(len(parsed_items) - 1):
        prev = parsed_items[idx]
        curr = parsed_items[idx + 1]
        gap_days = (curr["date_obj"] - prev["date_obj"]).days
        if prev["region"] != curr["region"] and gap_days < 2:
            errors.append(
                f"跨洲賽事（北美 ↔ 歐洲）間隔不足 2 天（{prev['date_str']}「{prev['matchup']}」與 {curr['date_str']}「{curr['matchup']}」），無法完成跨洋長程飛行與時差銜接，請至少間隔 2 天或調整場次。"
            )
        elif (
            prev["region"] == curr["region"]
            and gap_days == 1
            and prev["venue_norm"]
            and curr["venue_norm"]
            and prev["venue_norm"] != curr["venue_norm"]
        ):
            warnings.append(
                f"連日跨場館提醒：{prev['date_str']}（{prev['venue_raw']}）與 {curr['date_str']}（{curr['venue_raw']}）為連續兩天不同場館賽事，請預留城際交通或國內線移動時間。"
            )

    return {"errors": errors, "warnings": warnings}


def _pick_feasible_live_matches(
    live_list: list[dict[str, str]],
    target_count: int,
    start_date: date,
    end_date: date,
    need_flight: bool,
    league_type: str,
    is_cross_continent: bool,
) -> list[dict[str, str]]:
    """Select up to target_count live matches satisfying all hard feasibility rules."""
    chosen: list[dict[str, str]] = []
    for ev in live_list:
        kickoff_note = f"（UTC {ev['kickoff_utc']}）" if ev.get("kickoff_utc") else ""
        candidate = {
            "date": ev["date"],
            "kickoff_utc": ev.get("kickoff_utc", ""),
            "league": ev.get("league", ""),
            "region": infer_match_region(ev, league_type),
            "matchup": (
                f"{ev['matchup']}（{ev['league']}）"
                if ev.get("league")
                else ev["matchup"]
            ),
            "venue": ev.get("venue") or "官方指定球場",
            "note": f"來自 ESPN 即時賽程查詢{kickoff_note}，目前由展示保底模式自動整合真實對戰。",
        }
        check = validate_schedule_feasibility(
            chosen + [candidate],
            start_date=start_date,
            end_date=end_date,
            need_flight=need_flight,
            league_type=league_type,
            is_cross_continent=is_cross_continent,
        )
        if not check["errors"]:
            chosen.append(candidate)
            if len(chosen) >= target_count:
                break
    return chosen


def _build_structured_days(
    league_type: str,
    start_date: date,
    end_date: date,
    need_flight: bool,
    need_hotel: bool,
    matches: list[dict[str, str]],
    custom_instruction: str = "",
    is_cross_continent: bool = False,
) -> list[dict[str, Any]]:
    """Build pre-payment skeleton days containing ONLY core blocks (賽事/交通/住宿/空檔)."""
    preset = _get_league_preset(league_type)
    num_days = (end_date - start_date).days + 1

    matches_by_date: dict[str, list[dict[str, str]]] = {}
    for m in matches:
        matches_by_date.setdefault(str(m.get("date") or ""), []).append(m)

    transit_day_idx = num_days // 2 if (is_cross_continent and num_days >= 5) else None

    days: list[dict[str, Any]] = []
    for idx in range(num_days):
        current_day = start_date + timedelta(days=idx)
        day_iso = current_day.isoformat()
        day_matches = matches_by_date.get(day_iso, [])

        blocks: list[dict[str, str]] = []
        if day_matches:
            day_type = "MATCH_DAY"
            title = f"觀賽重頭戲：{day_matches[0]['matchup']}"
            if idx == 0:
                blocks.append(
                    {
                        "period": "上午",
                        "category": "交通",
                        "title": "抵達觀賽城市與寄放行李" if need_hotel else "抵達並領取交通票卡",
                        "detail": "建議優先將行李寄放於飯店或車站置物櫃後再前往場館。",
                    }
                )
            else:
                blocks.append(
                    {
                        "period": "上午",
                        "category": "空檔",
                        "title": LOCKED_GAP_TITLE,
                        "detail": LOCKED_GAP_DETAIL,
                    }
                )

            for dm in day_matches:
                blocks.append(
                    {
                        "period": "下午 / 晚間",
                        "category": "賽事",
                        "title": dm["matchup"],
                        "detail": f"場館：{dm['venue']}｜{dm['note']}",
                    }
                )
            blocks.append(
                {
                    "period": "賽後",
                    "category": "空檔",
                    "title": "賽後自由空檔（付款後解鎖）",
                    "detail": LOCKED_GAP_DETAIL,
                }
            )
        elif idx == 0:
            day_type = "ARRIVAL"
            title = f"抵達 {preset['city']} 與機場市區接駁"
            blocks.extend(
                [
                    {
                        "period": "上午 / 抵達",
                        "category": "交通",
                        "title": "抵達當地國際機場與入境通關" if need_flight else "抵達觀賽城市",
                        "detail": preset["flight_text"] if need_flight else "自理航班與機場進市區交通。",
                    },
                    {
                        "period": "下午",
                        "category": "住宿",
                        "title": "市區飯店辦理入住" if need_hotel else "市區住宿與場館接駁確認",
                        "detail": preset["hotel_text"] if need_hotel else "建議選擇鄰近車站或球場接駁站之住宿。",
                    },
                    {
                        "period": "晚間",
                        "category": "空檔",
                        "title": LOCKED_GAP_TITLE,
                        "detail": LOCKED_GAP_DETAIL,
                    },
                ]
            )
        elif idx == num_days - 1:
            day_type = "DEPARTURE"
            title = f"退房整理與搭機返程（{preset['city']}）"
            blocks.extend(
                [
                    {
                        "period": "上午",
                        "category": "住宿" if need_hotel else "交通",
                        "title": "飯店退房與行李整理" if need_hotel else "整理行李與核對返程電子憑證",
                        "detail": "退房後確認隨身證件、電子票券與往返機場交通路線。",
                    },
                    {
                        "period": "下午 / 返程",
                        "category": "交通",
                        "title": "前往機場搭機返台" if need_flight else "結束本次跨國觀賽旅程",
                        "detail": "建議提前 3 小時抵達機場辦理國際航班報到與通關。",
                    },
                ]
            )
        elif transit_day_idx is not None and idx == transit_day_idx:
            day_type = "EXPLORE"
            title = "跨洲長程航段與時差調整緩衝日"
            blocks.extend(
                [
                    {
                        "period": "全天",
                        "category": "交通",
                        "title": "跨洲航班轉乘（北美 ↔ 歐洲）",
                        "detail": "預留跨洋長程飛行與時差適應緩衝，抵達後就近飯店休息。",
                    },
                    {
                        "period": "晚間",
                        "category": "住宿",
                        "title": "入住下一段賽區市區飯店" if need_hotel else "抵達下一段賽區住宿點",
                        "detail": "辦理入住並確認隔日觀賽場館交通路線。",
                    },
                ]
            )
        else:
            day_type = "EXPLORE"
            title = f"{preset['city']} 自由空檔日（付款後解鎖詳細行程）"
            blocks.extend(
                [
                    {
                        "period": "上午",
                        "category": "空檔",
                        "title": LOCKED_GAP_TITLE,
                        "detail": custom_instruction.strip() or LOCKED_GAP_DETAIL,
                    },
                    {
                        "period": "下午",
                        "category": "空檔",
                        "title": LOCKED_GAP_TITLE,
                        "detail": LOCKED_GAP_DETAIL,
                    },
                ]
            )

        highlights = [f"[{b['period']}] {b['title']}：{b['detail']}" for b in blocks]
        days.append(
            {
                "day_number": idx + 1,
                "date": day_iso,
                "day_type": day_type,
                "city": preset["city"],
                "title": title,
                "blocks": blocks,
                "highlights": highlights,
            }
        )

    return days


def build_mock_draft(
    league_type: str,
    start_date: date,
    end_date: date,
    need_flight: bool,
    need_hotel: bool,
    preference: str = "",
    search_sources: list[dict[str, str]] | None = None,
    live_schedules: list[dict[str, str]] | None = None,
    diagnostics: dict[str, Any] | None = None,
    fallback_reason: str = "",
    desired_match_count: int = 2,
    locked_matches: list[dict[str, str]] | None = None,
    custom_instruction: str = "",
    structured_targets: list[dict[str, str]] | None = None,
    is_cross_continent: bool = False,
) -> dict[str, Any]:
    """Generate a structured Traditional Chinese itinerary draft as safe fallback."""
    preset = _get_league_preset(league_type)
    num_days = (end_date - start_date).days + 1
    live_list = live_schedules or []
    target_count = max(1, min(int(desired_match_count or 2), 6))

    matches: list[dict[str, str]] = []
    if locked_matches is not None:
        matches = [
            {
                **m,
                "region": m.get("region") or infer_match_region(m, league_type),
            }
            for m in locked_matches
        ]
    elif live_list:
        matches = _pick_feasible_live_matches(
            live_list=live_list,
            target_count=target_count,
            start_date=start_date,
            end_date=end_date,
            need_flight=need_flight,
            league_type=league_type,
            is_cross_continent=is_cross_continent,
        )
    else:
        match_day_index = min(1, num_days - 1)
        second_match_index = min(num_days - 2, 3) if num_days >= 4 else None
        primary_match = preset["matches"][0]
        matchup_title = (
            f"{primary_match[0]}（結合偏好：{preference}）"
            if preference
            else primary_match[0]
        )
        matches.append(
            {
                "date": (start_date + timedelta(days=match_day_index)).isoformat(),
                "region": infer_match_region({"matchup": matchup_title, "venue": primary_match[1]}, league_type),
                "matchup": matchup_title,
                "venue": primary_match[1],
                "note": primary_match[2],
            }
        )
        if (
            target_count >= 2
            and second_match_index is not None
            and second_match_index != match_day_index
        ):
            secondary_match = preset["matches"][1]
            matches.append(
                {
                    "date": (start_date + timedelta(days=second_match_index)).isoformat(),
                    "region": infer_match_region({"matchup": secondary_match[0], "venue": secondary_match[1]}, league_type),
                    "matchup": secondary_match[0],
                    "venue": secondary_match[1],
                    "note": secondary_match[2],
                }
            )

    feasibility = validate_schedule_feasibility(
        matches=matches,
        start_date=start_date,
        end_date=end_date,
        need_flight=need_flight,
        league_type=league_type,
        is_cross_continent=is_cross_continent,
    )
    enriched_live = [
        {**ev, "region": ev.get("region") or infer_match_region(ev, league_type)}
        for ev in live_list[:25]
    ]

    days = _build_structured_days(
        league_type=league_type,
        start_date=start_date,
        end_date=end_date,
        need_flight=need_flight,
        need_hotel=need_hotel,
        matches=matches,
        custom_instruction=custom_instruction,
        is_cross_continent=is_cross_continent,
    )

    flight_suggestion = (
        preset["flight_text"]
        if need_flight
        else "會員選擇機票自理，系統僅安排賽程核心動線與場館交通建議。"
    )
    if need_flight and is_cross_continent:
        flight_suggestion = (
            "跨洲長程機票配置建議：採「台灣 → 北美賽區 → 歐洲賽區 → 台灣」或反向順飛環球動線，"
            "於北美與歐洲之間預留跨洋直飛與 1～2 天時差調整日。"
        )
    hotel_suggestion = (
        preset["hotel_text"]
        if need_hotel
        else "會員選擇住宿自理，建議優先挑選鄰近球場地鐵／捷運轉乘站之區域。"
    )
    estimated_budget = _estimate_budget_twd(
        league_type=league_type,
        num_days=num_days,
        match_count=len(matches),
        need_flight=need_flight,
        need_hotel=need_hotel,
        is_cross_continent=is_cross_continent,
    )

    pref_note = f"（偏好：{preference}）" if preference else ""
    summary = (
        f"為您規劃 {start_date.isoformat()} 至 {end_date.isoformat()}（共 {num_days} 天）"
        f"的【{league_type}】觀賽旅遊大架構{pref_note}。"
        f"主軸鎖定 {len(matches)} 場賽事，確認架構後將送交訂票專員 AG001 開立一口價報價單（無訂金）。"
    )

    return {
        "league_type": league_type,
        "preference": preference,
        "targets": structured_targets or [],
        "is_cross_continent": is_cross_continent,
        "desired_match_count": target_count,
        "custom_instruction": custom_instruction.strip(),
        "summary": summary,
        "matches": matches,
        "feasibility": feasibility,
        "flight_suggestion": flight_suggestion,
        "hotel_suggestion": hotel_suggestion,
        "days": days,
        "estimated_budget_twd": estimated_budget,
        "search_sources": search_sources if search_sources else preset["default_sources"],
        "live_schedules": enriched_live,
        "diagnostics": diagnostics or {},
        "mode": "mock",
        "mode_label": "展示保底模式 (Mock)",
        "fallback_reason": fallback_reason,
    }


def _normalize_gemini_draft(
    raw_data: dict[str, Any],
    league_type: str,
    start_date: date,
    end_date: date,
    need_flight: bool,
    need_hotel: bool,
    preference: str,
    search_sources: list[dict[str, str]],
    live_schedules: list[dict[str, str]],
    diagnostics: dict[str, Any],
    used_model: str,
    desired_match_count: int = 2,
    locked_matches: list[dict[str, str]] | None = None,
    custom_instruction: str = "",
    structured_targets: list[dict[str, str]] | None = None,
    is_cross_continent: bool = False,
) -> dict[str, Any]:
    """Validate and normalize Gemini JSON output into a structured draft dictionary."""
    summary = str(raw_data.get("summary") or "").strip()
    matches = raw_data.get("matches")
    days = raw_data.get("days")

    if not summary or not isinstance(matches, list) or not isinstance(days, list) or not days:
        raise ValueError("Gemini output missing required summary, matches, or days")

    max_matches = max(desired_match_count, len(locked_matches or []), 6)
    live_lookup: dict[tuple[str, str], dict[str, str]] = {}
    for ev in live_schedules:
        if isinstance(ev, dict):
            ev_date = str(ev.get("date") or "")
            ev_match = _normalize_text(str(ev.get("matchup") or ""))
            live_lookup[(ev_date, ev_match)] = ev

    normalized_matches: list[dict[str, str]] = []
    if locked_matches is not None:
        normalized_matches = [
            {
                **m,
                "region": m.get("region") or infer_match_region(m, league_type),
            }
            for m in locked_matches
        ]
    else:
        for item in matches[:max_matches]:
            if not isinstance(item, dict):
                continue
            m_date = str(item.get("date") or start_date.isoformat())
            m_matchup = str(item.get("matchup") or f"{league_type} 焦點賽事")
            m_venue = str(item.get("venue") or "官方指定主場館")
            matched_ev = live_lookup.get((m_date, _normalize_text(m_matchup)))
            if matched_ev is None:
                norm_m = _normalize_text(m_matchup)
                for (ev_date, ev_norm), ev_obj in live_lookup.items():
                    if ev_date == m_date and ev_norm and (ev_norm in norm_m or norm_m in ev_norm):
                        matched_ev = ev_obj
                        break
            match_dict = {
                "date": m_date,
                "matchup": m_matchup,
                "venue": m_venue,
                "note": str(item.get("note") or "建議提前入場並確認電子票資訊"),
                "kickoff_utc": str(
                    item.get("kickoff_utc")
                    or (matched_ev.get("kickoff_utc") if matched_ev else "")
                    or ""
                ),
                "league": str(
                    item.get("league")
                    or (matched_ev.get("league") if matched_ev else "")
                    or ""
                ),
            }
            match_dict["region"] = infer_match_region(match_dict, league_type)
            normalized_matches.append(match_dict)

    match_dates = {m["date"] for m in normalized_matches}
    preset = _get_league_preset(league_type)
    valid_day_types = {"MATCH_DAY", "ARRIVAL", "EXPLORE", "DEPARTURE"}
    valid_categories = {"賽事", "交通", "住宿", "空檔"}

    normalized_days: list[dict[str, Any]] = []
    total_days_in_resp = len(days)
    for idx, item in enumerate(days):
        if not isinstance(item, dict):
            continue
        day_date = str(
            item.get("date") or (start_date + timedelta(days=idx)).isoformat()
        )
        raw_day_type = str(item.get("day_type") or "").strip().upper()
        if raw_day_type not in valid_day_types:
            if day_date in match_dates:
                raw_day_type = "MATCH_DAY"
            elif idx == 0:
                raw_day_type = "ARRIVAL"
            elif idx == total_days_in_resp - 1:
                raw_day_type = "DEPARTURE"
            else:
                raw_day_type = "EXPLORE"

        raw_blocks = item.get("blocks")
        normalized_blocks: list[dict[str, str]] = []
        if isinstance(raw_blocks, list):
            for blk in raw_blocks:
                if not isinstance(blk, dict):
                    continue
                cat = str(blk.get("category") or "空檔").strip()
                period = str(blk.get("period") or "全天").strip()
                b_title = str(blk.get("title") or "").strip()
                b_detail = str(blk.get("detail") or "").strip()
                is_gap_text = (
                    "付款後解鎖" in b_title
                    or "付款後解鎖" in b_detail
                    or LOCKED_GAP_DETAIL in b_detail
                )
                if cat not in valid_categories or cat == "空檔" or is_gap_text:
                    gap_title = (
                        "賽後自由空檔（付款後解鎖）"
                        if "賽後" in period
                        else LOCKED_GAP_TITLE
                    )
                    normalized_blocks.append(
                        {
                            "period": period,
                            "category": "空檔",
                            "title": gap_title,
                            "detail": LOCKED_GAP_DETAIL,
                        }
                    )
                elif b_title or b_detail:
                    normalized_blocks.append(
                        {
                            "period": period,
                            "category": cat,
                            "title": b_title or b_detail[:24],
                            "detail": b_detail or b_title,
                        }
                    )

        if not normalized_blocks:
            highlights_raw = item.get("highlights")
            hl_list = (
                [str(h) for h in highlights_raw if str(h).strip()]
                if isinstance(highlights_raw, list) and highlights_raw
                else [LOCKED_GAP_TITLE]
            )
            for b_idx, hl in enumerate(hl_list):
                is_match_slot = raw_day_type == "MATCH_DAY" and b_idx > 0
                is_transit_slot = raw_day_type in ("ARRIVAL", "DEPARTURE") and b_idx == 0
                if is_match_slot:
                    cat = "賽事"
                    slot_title = hl[:28]
                    slot_detail = hl
                elif is_transit_slot:
                    cat = "交通"
                    slot_title = hl[:28]
                    slot_detail = hl
                else:
                    cat = "空檔"
                    slot_title = LOCKED_GAP_TITLE
                    slot_detail = LOCKED_GAP_DETAIL
                normalized_blocks.append(
                    {
                        "period": "上午" if b_idx == 0 else ("下午" if b_idx == 1 else "晚間"),
                        "category": cat,
                        "title": slot_title,
                        "detail": slot_detail,
                    }
                )

        highlights = [
            f"[{b['period']}] {b['title']}：{b['detail']}"
            for b in normalized_blocks
        ]

        normalized_days.append(
            {
                "day_number": int(item.get("day_number") or (idx + 1)),
                "date": day_date,
                "day_type": raw_day_type,
                "city": str(item.get("city") or preset["city"]),
                "title": str(item.get("title") or f"Day {idx + 1} 行程規劃"),
                "blocks": normalized_blocks,
                "highlights": highlights,
            }
        )
    if not normalized_days:
        raise ValueError("Gemini days list contained no valid items")

    budget_raw = raw_data.get("estimated_budget_twd")
    try:
        estimated_budget = max(int(budget_raw), 10000)
    except (TypeError, ValueError):
        estimated_budget = _estimate_budget_twd(
            league_type,
            (end_date - start_date).days + 1,
            len(normalized_matches),
            need_flight,
            need_hotel,
            is_cross_continent=is_cross_continent,
        )

    flight_default = (
        preset["flight_text"] if need_flight else "會員自理機票，僅規劃賽事與當地行程。"
    )
    hotel_default = (
        preset["hotel_text"] if need_hotel else "會員自理住宿，建議入住球場捷運沿線。"
    )
    feasibility = validate_schedule_feasibility(
        matches=normalized_matches,
        start_date=start_date,
        end_date=end_date,
        need_flight=need_flight,
        league_type=league_type,
        is_cross_continent=is_cross_continent,
    )
    enriched_live = [
        {**ev, "region": ev.get("region") or infer_match_region(ev, league_type)}
        for ev in live_schedules[:25]
    ]

    return {
        "league_type": league_type,
        "preference": preference,
        "targets": structured_targets or [],
        "is_cross_continent": is_cross_continent,
        "desired_match_count": desired_match_count,
        "custom_instruction": custom_instruction.strip(),
        "summary": summary,
        "matches": normalized_matches,
        "feasibility": feasibility,
        "flight_suggestion": str(raw_data.get("flight_suggestion") or flight_default),
        "hotel_suggestion": str(raw_data.get("hotel_suggestion") or hotel_default),
        "days": normalized_days,
        "estimated_budget_twd": estimated_budget,
        "search_sources": search_sources if search_sources else preset["default_sources"],
        "live_schedules": enriched_live,
        "diagnostics": diagnostics,
        "mode": "gemini",
        "mode_label": f"Gemini 連網規劃 ({used_model})",
        "fallback_reason": "",
    }


def _get_candidate_models() -> list[str]:
    """Return deduplicated primary + fallback Gemini model list in priority order."""
    seen: set[str] = set()
    candidates: list[str] = []
    for name in (config.GEMINI_MODEL, *config.GEMINI_FALLBACK_MODELS):
        cleaned = (name or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            candidates.append(cleaned)
    return candidates or ["gemini-3.5-flash-lite"]


def _summarize_gemini_error(exc: Exception) -> str:
    """Build a short diagnostic error label without exposing sensitive payloads."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    if code:
        parts = [str(code)]
        if status:
            parts.append(str(status))
        return f"{type(exc).__name__} ({' '.join(parts)})"
    msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    if msg:
        return f"{type(exc).__name__}: {msg[:48]}"
    return type(exc).__name__


def apply_match_selection_instantly(
    existing_draft: dict[str, Any],
    selected_matches: list[dict[str, str]],
    league_type: str,
    start_date: date,
    end_date: date,
    need_flight: bool,
    need_hotel: bool,
    desired_match_count: int = 2,
    custom_instruction: str = "",
) -> dict[str, Any]:
    """Instantly rebuild structured days and budget from user-selected matches (0s, no AI call)."""
    updated = dict(existing_draft)
    num_days = (end_date - start_date).days + 1

    updated["matches"] = [
        {**m, "region": m.get("region") or infer_match_region(m, league_type)}
        for m in selected_matches
    ]
    updated["desired_match_count"] = max(1, min(int(desired_match_count or 2), 6))
    updated["custom_instruction"] = custom_instruction.strip()
    is_cross_continent = bool(updated.get("is_cross_continent")) or (
        len({m["region"] for m in updated["matches"] if m.get("region")}) > 1
    )
    updated["is_cross_continent"] = is_cross_continent
    updated["feasibility"] = validate_schedule_feasibility(
        matches=updated["matches"],
        start_date=start_date,
        end_date=end_date,
        need_flight=need_flight,
        league_type=league_type,
        is_cross_continent=is_cross_continent,
    )
    updated["days"] = _build_structured_days(
        league_type=league_type,
        start_date=start_date,
        end_date=end_date,
        need_flight=need_flight,
        need_hotel=need_hotel,
        matches=selected_matches,
        custom_instruction=custom_instruction,
        is_cross_continent=is_cross_continent,
    )
    updated["estimated_budget_twd"] = _estimate_budget_twd(
        league_type=league_type,
        num_days=num_days,
        match_count=len(selected_matches),
        need_flight=need_flight,
        need_hotel=need_hotel,
        is_cross_continent=is_cross_continent,
    )

    if selected_matches:
        match_names = "、".join(m["matchup"] for m in selected_matches[:3])
        extra = f" 等共 {len(selected_matches)} 場" if len(selected_matches) > 3 else ""
        updated["summary"] = (
            f"已依您的即時調整鎖定 {len(selected_matches)} 場賽事（{match_names}{extra}），"
            f"行程涵蓋 {start_date.isoformat()} 至 {end_date.isoformat()}（共 {num_days} 天）。"
        )
    else:
        updated["summary"] = (
            f"目前尚未保留任何觀賽場次（共 {num_days} 天純自由行大架構），"
            "您可以隨時從下方賽程庫重新勾選賽事或送請 AI 重新規劃。"
        )

    tz_taipei = timezone(timedelta(hours=8))
    diag = dict(updated.get("diagnostics") or {})
    diag["instant_update"] = True
    diag["generated_at"] = datetime.now(tz_taipei).strftime("%Y-%m-%d %H:%M:%S (UTC+8)")
    updated["diagnostics"] = diag
    return updated


def generate_itinerary_draft(
    league_type: str,
    start_date: date,
    end_date: date,
    need_flight: bool,
    need_hotel: bool,
    preference: str = "",
    desired_match_count: int = 2,
    locked_matches: list[dict[str, str]] | None = None,
    custom_instruction: str = "",
    existing_live_schedules: list[dict[str, str]] | None = None,
    existing_search_sources: list[dict[str, str]] | None = None,
    existing_espn_meta: dict[str, Any] | None = None,
    structured_targets: list[dict[str, str]] | None = None,
    use_llm: bool = False,
) -> dict[str, Any]:
    """Fetch live ESPN schedules + ddgs results and build draft via rule engine or Gemini."""
    target_count = max(1, min(int(desired_match_count or 2), 6))
    is_cross_continent = bool(
        structured_targets and len({t.get("region") for t in structured_targets if t.get("region")}) > 1
    )

    if existing_live_schedules is not None:
        live_schedules = list(existing_live_schedules)
        espn_meta = dict(existing_espn_meta or {})
        espn_meta.setdefault("total_found", len(live_schedules))
        espn_meta.setdefault(
            "covered_range", f"{start_date.isoformat()} ~ {end_date.isoformat()}"
        )
    elif structured_targets:
        espn_meta = {}
        live_schedules = fetch_espn_schedules_for_targets(
            targets=structured_targets,
            start_date=start_date,
            end_date=end_date,
            meta=espn_meta,
        )
    else:
        espn_meta = {}
        live_schedules = fetch_espn_schedules(
            league_type=league_type,
            start_date=start_date,
            end_date=end_date,
            preference=preference,
            meta=espn_meta,
        )

    if existing_search_sources is not None:
        search_sources = list(existing_search_sources)
    else:
        search_sources = search_authoritative_schedules(
            league_type=league_type,
            start_date=start_date,
            end_date=end_date,
            preference=preference,
        )

    if structured_targets:
        translated_display = ", ".join(
            dict.fromkeys(
                f"{t['sub_league']}:{t.get('team_name') or '全聯賽'}"
                for t in structured_targets
            )
        )
    else:
        targets = parse_preference_targets(preference, league_type)
        translated_display = ", ".join(
            dict.fromkeys(t[0] for t in targets if t[0] and t[0] != t[2])
        )
    tz_taipei = timezone(timedelta(hours=8))

    diagnostics: dict[str, Any] = {
        "planning_engine": "gemini" if use_llm else "rule_engine",
        "gemini_called": False,
        "model": config.GEMINI_MODEL,
        "latency_ms": 0,
        "prompt_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "espn_events_count": len(live_schedules),
        "espn_total_found": espn_meta.get("total_found", len(live_schedules)),
        "espn_strategy": espn_meta.get("strategy", "league_scoreboard"),
        "espn_matched_team": espn_meta.get("matched_team", ""),
        "espn_covered_range": espn_meta.get(
            "covered_range", f"{start_date.isoformat()} ~ {end_date.isoformat()}"
        ),
        "ddgs_results_count": len(search_sources),
        "used_default_sources": len(search_sources) == 0,
        "translated_preference": translated_display,
        "attempted_models": [],
        "instant_update": False,
        "is_cross_continent": is_cross_continent,
        "generated_at": datetime.now(tz_taipei).strftime("%Y-%m-%d %H:%M:%S (UTC+8)"),
    }

    if not use_llm:
        rule_draft = build_mock_draft(
            league_type=league_type,
            start_date=start_date,
            end_date=end_date,
            need_flight=need_flight,
            need_hotel=need_hotel,
            preference=preference,
            search_sources=search_sources,
            live_schedules=live_schedules,
            diagnostics=diagnostics,
            fallback_reason="",
            desired_match_count=target_count,
            locked_matches=locked_matches,
            custom_instruction=custom_instruction,
            structured_targets=structured_targets,
            is_cross_continent=is_cross_continent,
        )
        rule_draft["mode"] = "rule_engine"
        rule_draft["mode_label"] = "規則引擎即時排程 (ESPN)"
        rule_draft["fallback_reason"] = ""
        return rule_draft

    api_key = config.GEMINI_API_KEY
    if not api_key:
        return build_mock_draft(
            league_type=league_type,
            start_date=start_date,
            end_date=end_date,
            need_flight=need_flight,
            need_hotel=need_hotel,
            preference=preference,
            search_sources=search_sources,
            live_schedules=live_schedules,
            diagnostics=diagnostics,
            fallback_reason="未設定 GEMINI_API_KEY，已自動切換展示保底模式",
            desired_match_count=target_count,
            locked_matches=locked_matches,
            custom_instruction=custom_instruction,
            structured_targets=structured_targets,
            is_cross_continent=is_cross_continent,
        )

    num_days = (end_date - start_date).days + 1
    schedules_json = json.dumps(live_schedules[:25], ensure_ascii=False)
    sources_json = json.dumps(search_sources, ensure_ascii=False)

    if locked_matches is not None:
        locked_json = json.dumps(locked_matches, ensure_ascii=False)
        match_rule = (
            f"1. 使用者已親自勾選鎖定以下 {len(locked_matches)} 場賽事：{locked_json}。"
            "輸出的 matches 必須嚴格與這批鎖定場次完全一致（不可擅自增刪或替換成別場），"
            "並將整段每日行程繞著這幾場比賽的日期與球場動線重新安排。\n"
        )
    else:
        match_rule = (
            f"1. 使用者希望安排約 {target_count} 場賽事。若「ESPN 即時官方賽程資料」非空，"
            "matches 必須嚴格採用上述真實資料中的日期、對戰隊伍與球場名稱，優先挑選符合使用者偏好（球隊/球星/球場）的場次，"
            "並盡量滿足目標場數（若真實賽程總數不足則依實際數量安排，禁止編造假比賽）。\n"
        )

    extra_rule = (
        f"3. 使用者補充微調指令（請務必落實於每日時段或機酒建議中）：{custom_instruction.strip()}\n"
        if custom_instruction.strip()
        else ""
    )
    cross_continent_rule = (
        "5. 本次為「跨洲行程」（橫跨北美與歐洲），請分段依序先安排同一洲賽事，並在兩洲之間明確排入跨洋長程移動與時差緩衝日（category 標為 交通）。\n"
        if is_cross_continent
        else ""
    )

    prompt = (
        "你是專業的跨國體育賽事旅遊規劃師，請使用繁體中文輸出嚴格合法的 JSON 物件，"
        "不得包含任何 Markdown 標記或額外說明文字。\n"
        f"需求條件：聯賽={league_type}、出發日期={start_date.isoformat()}、"
        f"回程日期={end_date.isoformat()}（共 {num_days} 天）、"
        f"預計觀看場數={target_count} 場、"
        f"代訂機票={'需要' if need_flight else '自理'}、"
        f"代訂飯店={'需要' if need_hotel else '自理'}、"
        f"球隊/球星/球場偏好={preference or '無特定偏好'}（英文解析關鍵字：{translated_display or '無'}）。\n"
        f"ESPN 即時官方賽程資料（真實抓取共 {len(live_schedules)} 場）：{schedules_json}\n"
        f"權威網站搜尋摘要（共 {len(search_sources)} 筆）：{sources_json}\n"
        "重要規劃與防幻覺規範：\n"
        f"{match_rule}"
        "2. 若「ESPN 即時官方賽程資料」為空（例如休賽季），請於 summary 明確告知查無該區間官方賽程，matches 回傳 []，嚴禁編造假對戰。\n"
        f"{extra_rule}"
        f"{cross_continent_rule}"
        "4. 【未付款前核心架構規範（嚴格遵守）】本階段僅提供「賽事、交通、住宿、空檔」四大類別骨架，嚴禁安排任何觀光景點、博物館、餐廳或購物推薦。"
        "每一天的 blocks 請依「上午 / 下午 / 晚間 / 賽後」拆分 2~3 個結構化時段，"
        "day_type 限用 MATCH_DAY / ARRIVAL / EXPLORE / DEPARTURE，"
        "block 的 category 嚴格限用 賽事 / 交通 / 住宿 / 空檔（禁止輸出「景點」類別）。"
        f"非賽事／非機場交通／非飯店入住的自由時段，title 一律填「{LOCKED_GAP_TITLE}」，detail 一律填「{LOCKED_GAP_DETAIL}」。\n"
        "6. 【排程可行性與防衝突規範（嚴格遵守）】同一天不可排入時間或場館衝突的多場賽事；"
        "若 代訂機票=需要 且旅遊總天數 >= 3 天，出發日與回程日不可排入正賽（保留給國際長程飛行與通關時間）；"
        "若為跨洲行程（北美 ↔ 歐洲），兩洲賽事之間至少須間隔 2 天以上。\n"
        "請輸出包含以下鍵值的 JSON：\n"
        "{\n"
        '  "summary": "整體旅遊大架構說明（繁體中文）",\n'
        '  "matches": [{"date": "YYYY-MM-DD", "matchup": "對戰組合", "venue": "球場名稱", "note": "席次與入場建議"}],\n'
        '  "flight_suggestion": "航班配置建議",\n'
        '  "hotel_suggestion": "住宿區域建議",\n'
        '  "days": [{\n'
        '    "day_number": 1,\n'
        '    "date": "YYYY-MM-DD",\n'
        '    "day_type": "ARRIVAL",\n'
        '    "city": "城市名稱",\n'
        '    "title": "當日主題標題",\n'
        '    "blocks": [{"period": "上午", "category": "交通", "title": "項目名稱", "detail": "簡短說明"}]\n'
        "  }],\n"
        '  "estimated_budget_twd": 75000\n'
        "}"
    )

    try:
        from google import genai
        from google.genai import types

        http_kwargs: dict[str, Any] = {"timeout": config.GEMINI_TIMEOUT_MS}
        if hasattr(types, "HttpRetryOptions"):
            http_kwargs["retry_options"] = types.HttpRetryOptions(attempts=1)
        client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(**http_kwargs),
        )
    except Exception as exc:
        return build_mock_draft(
            league_type=league_type,
            start_date=start_date,
            end_date=end_date,
            need_flight=need_flight,
            need_hotel=need_hotel,
            preference=preference,
            search_sources=search_sources,
            live_schedules=live_schedules,
            diagnostics=diagnostics,
            fallback_reason=f"AI 連網服務初始化失敗，已自動切換高擬真 Mock 保底資料 ({_summarize_gemini_error(exc)})",
            desired_match_count=target_count,
            locked_matches=locked_matches,
            custom_instruction=custom_instruction,
            structured_targets=structured_targets,
            is_cross_continent=is_cross_continent,
        )

    attempted: list[dict[str, str]] = []
    for model_name in _get_candidate_models():
        t0 = time.perf_counter()
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            elapsed_ms = round((time.perf_counter() - t0) * 1000)

            raw_text = (response.text or "").strip()
            if raw_text.startswith("```"):
                raw_text = raw_text.strip("`")
                if raw_text.lower().startswith("json"):
                    raw_text = raw_text[4:].strip()
            parsed = json.loads(raw_text)
            if not isinstance(parsed, dict):
                raise ValueError("Gemini JSON response is not an object")

            attempted.append({"model": model_name, "status": "ok"})
            diagnostics["attempted_models"] = attempted
            diagnostics["gemini_called"] = True
            diagnostics["model"] = model_name
            diagnostics["latency_ms"] = elapsed_ms

            usage = getattr(response, "usage_metadata", None)
            if usage is not None:
                diagnostics["prompt_tokens"] = getattr(usage, "prompt_token_count", None)
                diagnostics["output_tokens"] = getattr(usage, "candidates_token_count", None)
                diagnostics["total_tokens"] = getattr(usage, "total_token_count", None)

            return _normalize_gemini_draft(
                raw_data=parsed,
                league_type=league_type,
                start_date=start_date,
                end_date=end_date,
                need_flight=need_flight,
                need_hotel=need_hotel,
                preference=preference,
                search_sources=search_sources,
                live_schedules=live_schedules,
                diagnostics=diagnostics,
                used_model=model_name,
                desired_match_count=target_count,
                locked_matches=locked_matches,
                custom_instruction=custom_instruction,
                structured_targets=structured_targets,
                is_cross_continent=is_cross_continent,
            )
        except Exception as exc:
            attempted.append(
                {"model": model_name, "status": _summarize_gemini_error(exc)}
            )

    diagnostics["attempted_models"] = attempted
    error_summary = "；".join(f"{a['model']}: {a['status']}" for a in attempted)
    return build_mock_draft(
        league_type=league_type,
        start_date=start_date,
        end_date=end_date,
        need_flight=need_flight,
        need_hotel=need_hotel,
        preference=preference,
        search_sources=search_sources,
        live_schedules=live_schedules,
        diagnostics=diagnostics,
        fallback_reason=f"AI 連網服務暫時無法使用（{error_summary}），已自動切換高擬真 Mock 保底資料",
        desired_match_count=target_count,
        locked_matches=locked_matches,
        custom_instruction=custom_instruction,
        structured_targets=structured_targets,
        is_cross_continent=is_cross_continent,
    )


def parse_draft_json(raw_json: str | None) -> dict[str, Any] | None:
    """Safely deserialize stored llm_draft_json from TripPlan."""
    if not raw_json:
        return None
    try:
        data = json.loads(raw_json)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None
