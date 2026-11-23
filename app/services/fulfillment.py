"""DFD 5.0 fulfillment service: Traveler KYC, league-specific tickets, 4-category vouchers & exceptions."""

from datetime import date, datetime, timedelta, timezone
import json
import re
import secrets
from typing import Any

from sqlalchemy.orm import Session

from app.models import BookingException, BookingVoucher, GapItineraryItem, OrderQuote, PaymentTxn, utc_now
from app.services.payment import process_mock_refund
from app.services.planner import _get_league_preset, parse_draft_json

_TAIPEI_TZ = timezone(timedelta(hours=8))

_PASSPORT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z\s,./-]{1,79}$")
_PASSPORT_NO_RE = re.compile(r"^[A-Za-z0-9]{6,12}$")
_PNR_RE = re.compile(r"^[A-Za-z0-9]{5,8}$")
_IATA_RE = re.compile(r"^[A-Za-z]{3}$")

VOUCHER_CATEGORIES: dict[str, dict[str, str]] = {
    "MATCH": {"label": "賽事電子門票", "prefix": "AGD-MAT", "badge": "賽事門票"},
    "FLIGHT": {"label": "國際機票行程收據", "prefix": "AGD-FLT", "badge": "來回機票"},
    "HOTEL": {"label": "飯店入住確認單", "prefix": "AGD-HTL", "badge": "飯店住宿"},
    "SPOT": {"label": "景點門票電子憑證", "prefix": "AGD-SPT", "badge": "景點門票"},
}

LEAGUE_TICKET_FORMATS: dict[str, dict[str, Any]] = {
    "MLB": {
        "label": "MLB（MLB Ballpark App 官方電子票）",
        "delivery": "MLB Ballpark App（SafeTix 動態條碼／Apple & Google Wallet NFC）",
        "seat_mode": "US",
        "requires_id_match": False,
        "anti_screenshot": True,
        "rule_note": "入場須開啟 MLB Ballpark App 動態條碼或 NFC 感應，嚴禁使用靜態截圖或紙本影印。",
    },
    "NBA": {
        "label": "NBA（Ticketmaster SafeTix 官方電子票）",
        "delivery": "Ticketmaster App 轉票（SafeTix 動態藍線條碼／NFC）",
        "seat_mode": "US",
        "requires_id_match": False,
        "anti_screenshot": True,
        "rule_note": "請以領票信箱登入 Ticketmaster App 接收轉票，動態藍線條碼嚴禁截圖入場。",
    },
    "EU_LEAGUE": {
        "label": "歐洲五大聯賽（Stand / Block / Turnstile 數位票）",
        "delivery": "球會官方 App / NFC Mobile Pass（義甲強制實名制）",
        "seat_mode": "EU",
        "requires_id_match": True,
        "anti_screenshot": True,
        "rule_note": "請依票面 Stand 看台、Block 區塊與 Turnstile 旋轉門入場；義甲強制實名制、英超／西甲得抽驗護照正本。",
    },
    "UEFA_UCL": {
        "label": "UEFA 歐冠（UEFA Mobile Tickets App 強制實名）",
        "delivery": "UEFA Mobile Tickets App 專屬（單一裝置綁定，不接受 PDF／Wallet／截圖）",
        "seat_mode": "UEFA",
        "requires_id_match": True,
        "anti_screenshot": True,
        "rule_note": "強制使用 UEFA Mobile Tickets App（開賽前啟用動態 QR），入場須攜帶與綁定姓名相符之實體護照正本查驗。",
    },
}

EXCEPTION_REASON_PRESETS: tuple[str, ...] = (
    "原訂賽事下層看台座位已售罄",
    "原指定航班票價大幅調漲超出報價預算",
    "原訂市區精選飯店該區間滿房",
    "官方賽程開賽時間異動需調整場次與住宿",
)


def mask_passport_number(passport_number: str) -> str:
    """Return masked passport number (e.g. '312***678') safe for printed vouchers."""
    cleaned = (passport_number or "").strip().upper()
    if len(cleaned) <= 4:
        return "*" * len(cleaned)
    return f"{cleaned[:3]}{'*' * max(len(cleaned) - 6, 3)}{cleaned[-3:]}"


def get_traveler_profile(draft: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return stored traveler passport profile dict from llm_draft_json if present."""
    if not isinstance(draft, dict):
        return None
    profile = draft.get("traveler_profile")
    return profile if isinstance(profile, dict) and profile.get("passport_number") else None


def validate_traveler_profile(
    raw: dict[str, str],
    trip_end_date: date,
) -> tuple[dict[str, Any] | None, str | None]:
    """Strictly validate mandatory traveler passport KYC data before mock payment."""
    passport_name = " ".join((raw.get("passport_name") or "").split()).upper()
    passport_number = (raw.get("passport_number") or "").replace(" ", "").strip().upper()
    nationality = (raw.get("nationality") or "").strip()
    birth_raw = (raw.get("birth_date") or "").strip()
    expiry_raw = (raw.get("passport_expiry") or "").strip()
    contact_phone = (raw.get("contact_phone") or "").strip()
    ticket_email = (raw.get("ticket_email") or "").strip().lower()

    if not passport_name or not _PASSPORT_NAME_RE.match(passport_name):
        return None, "請輸入正確的護照英文拼音姓名（限英文字母、空白或逗號，例如 CHEN, DAMING）。"
    if not passport_number or not _PASSPORT_NO_RE.match(passport_number):
        return None, "請輸入有效的護照號碼（6～12 碼英數字，不可含特殊符號）。"
    if not nationality:
        return None, "請填寫旅客國籍（例如 Taiwan (TWN)）。"

    try:
        birth_date = date.fromisoformat(birth_raw)
    except ValueError:
        return None, "請輸入有效的出生日期格式（YYYY-MM-DD）。"
    if birth_date >= date.today():
        return None, "出生日期不可晚於或等於今日。"

    try:
        expiry_date = date.fromisoformat(expiry_raw)
    except ValueError:
        return None, "請輸入有效的護照效期截止日（YYYY-MM-DD）。"
    if expiry_date <= trip_end_date:
        return (
            None,
            f"護照效期（{expiry_date.isoformat()}）已過期或早於回程日（{trip_end_date.isoformat()}），請更換有效護照後再訂購。",
        )

    digits_only = re.sub(r"\D", "", contact_phone)
    if len(digits_only) < 8:
        return None, "請輸入有效的聯絡電話（至少 8 碼數字）。"

    if "@" not in ticket_email or "." not in ticket_email.split("@")[-1]:
        return None, "請輸入有效的電子票接收信箱格式（用於 Ticketmaster / UEFA App 轉票）。"

    return (
        {
            "passport_name": passport_name,
            "passport_number": passport_number,
            "passport_masked": mask_passport_number(passport_number),
            "nationality": nationality,
            "birth_date": birth_date.isoformat(),
            "passport_expiry": expiry_date.isoformat(),
            "expiry_under_6m": (expiry_date - trip_end_date).days < 180,
            "contact_phone": contact_phone,
            "ticket_email": ticket_email,
        },
        None,
    )


def infer_league_ticket_format(league_type: str, context_text: str = "") -> str:
    """Resolve league ticketing standard key: MLB / NBA / EU_LEAGUE / UEFA_UCL."""
    combined = f"{league_type} {context_text}".lower()
    if any(k in combined for k in ("歐冠", "uefa", "champions league")):
        return "UEFA_UCL"
    if any(
        k in combined
        for k in (
            "英超",
            "西甲",
            "義甲",
            "德甲",
            "法甲",
            "歐足",
            "premier",
            "la liga",
            "serie a",
            "bundesliga",
            "ligue 1",
        )
    ):
        return "EU_LEAGUE"
    if "nba" in combined:
        return "NBA"
    return "MLB"


def generate_booking_code(category: str) -> str:
    """Generate an Agoda-style alphanumeric confirmation code."""
    meta = VOUCHER_CATEGORIES.get(category)
    prefix = meta["prefix"] if meta else "AGD-VCH"
    date_part = datetime.now(_TAIPEI_TZ).strftime("%Y%m%d")
    token = secrets.token_hex(2).upper()
    return f"{prefix}-{date_part}-{token}"


def parse_voucher_detail(raw_json: str | None) -> dict[str, Any]:
    """Safely deserialize BookingVoucher.detail_json into a dictionary."""
    if not raw_json:
        return {}
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def has_pending_exception(db: Session, order_id: int) -> bool:
    """Return True when the order has an unresolved PENDING booking exception."""
    return (
        db.query(BookingException)
        .filter(
            BookingException.order_id == order_id,
            BookingException.user_decision == "PENDING",
        )
        .first()
        is not None
    )


def compute_fulfillment_checklist(db: Session, order: OrderQuote) -> dict[str, Any]:
    """Compute required vs issued vouchers by category; BOOKED only when 100% satisfied."""
    vouchers = (
        db.query(BookingVoucher)
        .filter(BookingVoucher.order_id == order.order_id)
        .all()
    )
    counts: dict[str, int] = {"MATCH": 0, "FLIGHT": 0, "HOTEL": 0, "SPOT": 0}
    for v in vouchers:
        counts[v.category] = counts.get(v.category, 0) + 1

    trip = order.trip_plan
    draft = parse_draft_json(trip.llm_draft_json) if trip else None
    items: list[dict[str, Any]] = []

    if order.order_type == "ADDON":
        req_spot = max(len(order.addon_gap_items), 1)
        items.append(
            {
                "category": "SPOT",
                "label": "景點門票加購憑證",
                "required": req_spot,
                "issued": counts.get("SPOT", 0),
                "done": counts.get("SPOT", 0) >= req_spot,
            }
        )
    elif trip is not None:
        matches = (
            draft.get("matches")
            if isinstance(draft, dict) and isinstance(draft.get("matches"), list)
            else []
        )
        req_match = max(len(matches), 1)
        items.append(
            {
                "category": "MATCH",
                "label": f"賽事電子門票（共 {req_match} 場）",
                "required": req_match,
                "issued": counts.get("MATCH", 0),
                "done": counts.get("MATCH", 0) >= req_match,
            }
        )
        if trip.need_flight:
            items.append(
                {
                    "category": "FLIGHT",
                    "label": "國際來回電子機票 (E-Ticket)",
                    "required": 1,
                    "issued": counts.get("FLIGHT", 0),
                    "done": counts.get("FLIGHT", 0) >= 1,
                }
            )
        if trip.need_hotel:
            items.append(
                {
                    "category": "HOTEL",
                    "label": "飯店入住確認單 (Prepaid)",
                    "required": 1,
                    "issued": counts.get("HOTEL", 0),
                    "done": counts.get("HOTEL", 0) >= 1,
                }
            )

    required_total = sum(it["required"] for it in items)
    issued_required = sum(min(it["issued"], it["required"]) for it in items)
    missing_labels = [it["label"] for it in items if not it["done"]]
    is_complete = bool(items) and not missing_labels

    return {
        "required_total": required_total,
        "issued_required": issued_required,
        "is_complete": is_complete,
        "missing_labels": missing_labels,
        "items": items,
    }


def _build_validated_match_detail(
    fields: dict[str, str],
    order: OrderQuote,
    traveler: dict[str, Any] | None,
) -> dict[str, Any]:
    trip = order.trip_plan
    matchup = (fields.get("matchup") or fields.get("title") or "").strip()
    event_date = (fields.get("event_date") or "").strip()
    event_time = (fields.get("event_time") or "").strip() or "19:30（當地時間）"
    venue = (fields.get("venue") or fields.get("location") or "").strip()
    fmt_key = (fields.get("league_format") or "").strip().upper()
    if fmt_key not in LEAGUE_TICKET_FORMATS:
        fmt_key = infer_league_ticket_format(
            trip.league_type if trip else "",
            f"{matchup} {venue}",
        )
    fmt_meta = LEAGUE_TICKET_FORMATS[fmt_key]
    is_eu = fmt_meta["seat_mode"] in ("EU", "UEFA")

    stand = (fields.get("stand") or "").strip()
    section = (fields.get("section") or "").strip()
    block = (fields.get("block") or "").strip()
    row = (fields.get("row") or "").strip()
    seat = (fields.get("seat") or "").strip()
    gate = (fields.get("gate_or_turnstile") or "").strip()

    if not matchup:
        raise ValueError("開立賽事門票時，請填寫賽事對戰組合名稱。")
    if not event_date:
        raise ValueError("開立賽事門票時，請填寫開賽日期。")
    if not venue:
        raise ValueError("開立賽事門票時，請填寫官方球場／場館全名。")
    if is_eu and not block:
        raise ValueError("歐洲足球／歐冠門票必須填寫看台區塊（Block，例如 Block 112）。")
    if not is_eu and not section:
        raise ValueError("北美 MLB／NBA 門票必須填寫區域（Section，例如 Section 108）。")
    if not row or not seat:
        raise ValueError("開立賽事門票時，請完整填寫 Row（排）與 Seat（座號）。")

    holder_name = (
        (fields.get("holder_name") or "").strip().upper()
        or (traveler.get("passport_name") if traveler else "")
        or "TRAVELER"
    )
    passport_masked = (
        (fields.get("holder_passport_masked") or "").strip()
        or (traveler.get("passport_masked") if traveler else "")
    )
    ticket_email = (
        (fields.get("ticket_email") or "").strip()
        or (traveler.get("ticket_email") if traveler else "")
    )
    delivery = (fields.get("delivery_method") or "").strip() or fmt_meta["delivery"]
    default_gate = "Turnstile E / Gate 4" if is_eu else "Main South Gate"
    gate_val = gate or default_gate

    if is_eu:
        seat_summary = f"{(stand + ' · ') if stand else ''}Block {block} · Row {row} · Seat {seat}"
    else:
        seat_summary = f"Section {section} · Row {row} · Seat {seat}"

    return {
        "title": matchup,
        "subtitle": fmt_meta["label"],
        "event_date": f"{event_date} {event_time}".strip(),
        "location": venue,
        "seat_or_room": seat_summary,
        "league_format": fmt_key,
        "seat_mode": fmt_meta["seat_mode"],
        "matchup": matchup,
        "match_date": event_date,
        "event_time": event_time,
        "venue": venue,
        "stand": stand or ("主看台區" if is_eu else ""),
        "section": section,
        "block": block,
        "row": row,
        "seat": seat,
        "gate_or_turnstile": gate_val,
        "delivery_method": delivery,
        "holder_name": holder_name,
        "holder_passport_masked": passport_masked,
        "ticket_email": ticket_email,
        "requires_id_match": fmt_meta["requires_id_match"],
        "anti_screenshot": fmt_meta["anti_screenshot"],
        "rule_note": fmt_meta["rule_note"],
        "notes": (fields.get("notes") or "").strip() or fmt_meta["rule_note"],
    }


def _build_validated_flight_detail(
    fields: dict[str, str],
    traveler: dict[str, Any] | None,
) -> dict[str, Any]:
    airline = (fields.get("airline") or "").strip()
    pnr = (fields.get("pnr") or "").strip().upper()
    eticket = (fields.get("eticket_number") or "").strip()
    pax_name = (
        (fields.get("passenger_name") or "").strip().upper()
        or (traveler.get("passport_name") if traveler else "")
    )
    out_flt = (fields.get("outbound_flight_no") or "").strip().upper()
    orig = (fields.get("origin_airport") or "").strip().upper()
    dest = (fields.get("dest_airport") or "").strip().upper()
    out_dt = (fields.get("outbound_datetime") or "").strip()
    ret_flt = (fields.get("return_flight_no") or "").strip().upper()
    ret_dt = (fields.get("return_datetime") or "").strip()
    cabin = (fields.get("cabin_class") or "").strip() or "經濟艙 Economy (Q)"
    baggage = (fields.get("baggage_allowance") or "").strip() or "託運 1 件 23kg ＋ 手提 7kg"
    seat_no = (fields.get("seat_assignment") or "").strip() or "已預選靠窗／走道連號（34A / 34B）"

    if not airline:
        raise ValueError("開立電子機票時，請填寫執飛航空公司名稱。")
    if not pnr or not _PNR_RE.match(pnr):
        raise ValueError("開立電子機票時，請填寫正確的 5～8 碼訂位代號 (PNR，例如 K8M9P2)。")
    if len(re.sub(r"\D", "", eticket)) < 10:
        raise ValueError("開立電子機票時，請填寫正確的電子票號 (E-Ticket Number，至少 10 碼數字)。")
    if not pax_name:
        raise ValueError("開立電子機票時，旅客護照英文姓名不可空白。")
    if not out_flt:
        raise ValueError("開立電子機票時，請填寫去程航班編號（例如 BR08 / CX880）。")
    if not _IATA_RE.match(orig) or not _IATA_RE.match(dest):
        raise ValueError("出發與抵達機場請填寫 3 碼國際 IATA 代碼（例如 TPE 與 SFO／LHR）。")
    if not out_dt:
        raise ValueError("開立電子機票時，請填寫去程起降時間資訊。")

    passport_masked = (traveler.get("passport_masked") if traveler else "") or "已核對護照正本"
    return {
        "title": f"{airline} 國際來回電子機票（{orig} ↔ {dest}）",
        "subtitle": f"PNR: {pnr} · E-Ticket: {eticket}",
        "event_date": out_dt,
        "location": f"{orig} → {dest}",
        "seat_or_room": f"{cabin} · {baggage}",
        "airline": airline,
        "pnr": pnr,
        "eticket_number": eticket,
        "passenger_name": pax_name,
        "passport_masked": passport_masked,
        "origin_airport": orig,
        "dest_airport": dest,
        "outbound_flight_no": out_flt,
        "outbound_datetime": out_dt,
        "return_flight_no": ret_flt or f"{out_flt[:2]}回程班機",
        "return_datetime": ret_dt or "依開票聯航段回程時間",
        "cabin_class": cabin,
        "baggage_allowance": baggage,
        "seat_assignment": seat_no,
        "notes": (fields.get("notes") or "").strip()
        or "請於航班起飛前 3 小時持有效護照正本與本電子機票收據至櫃檯辦理報到託運。",
    }


def _build_validated_hotel_detail(
    fields: dict[str, str],
    traveler: dict[str, Any] | None,
) -> dict[str, Any]:
    hotel_name = (fields.get("hotel_name") or "").strip()
    hotel_address = (fields.get("hotel_address") or "").strip()
    guest_name = (
        (fields.get("guest_name") or "").strip().upper()
        or (traveler.get("passport_name") if traveler else "")
    )
    checkin_raw = (fields.get("checkin_date") or "").strip()
    checkout_raw = (fields.get("checkout_date") or "").strip()
    room_type = (fields.get("room_type") or "").strip()
    guests_count = (fields.get("guests_count") or "").strip() or "2 位成人 (2 Adults)"
    meal_plan = (fields.get("meal_plan") or "").strip() or "含每日雙人自助早餐 (Breakfast Included)"

    if not hotel_name:
        raise ValueError("開立飯店入住確認單時，請填寫飯店全名。")
    if not hotel_address:
        raise ValueError("開立飯店入住確認單時，請填寫飯店完整地址。")
    if not guest_name:
        raise ValueError("開立飯店入住確認單時，代表住客護照英文姓名不可空白。")
    try:
        ci_date = date.fromisoformat(checkin_raw)
        co_date = date.fromisoformat(checkout_raw)
    except ValueError:
        raise ValueError("開立飯店入住確認單時，入住與退房日期請使用 YYYY-MM-DD 格式。")
    if co_date <= ci_date:
        raise ValueError("飯店退房日期必須晚於入住日期（至少住宿 1 晚）。")
    if not room_type:
        raise ValueError("開立飯店入住確認單時，請填寫確認房型（例如 Standard Twin Room）。")

    nights = (co_date - ci_date).days
    payment_terms = (
        (fields.get("payment_terms") or "").strip()
        or "PREPAID — 房費已由旅行社全額預付，辦理入住時無須再向住客收取房費（現場僅依飯店規定收押金）。"
    )
    return {
        "title": hotel_name,
        "subtitle": f"{nights} 晚住宿確認 · {guests_count}",
        "event_date": f"{ci_date.isoformat()} ~ {co_date.isoformat()}（{nights} 晚）",
        "location": hotel_address,
        "seat_or_room": f"{room_type} · {meal_plan}",
        "hotel_name": hotel_name,
        "hotel_address": hotel_address,
        "guest_name": guest_name,
        "checkin_date": ci_date.isoformat(),
        "checkin_time": "15:00 後",
        "checkout_date": co_date.isoformat(),
        "checkout_time": "11:00 前",
        "nights": nights,
        "guests_count": guests_count,
        "room_type": room_type,
        "meal_plan": meal_plan,
        "payment_terms": payment_terms,
        "notes": (fields.get("notes") or "").strip()
        or "辦理入住手續時，請代表住客出示與訂單相符之護照正本及本確認單。",
    }


def _build_validated_spot_detail(
    fields: dict[str, str],
    traveler: dict[str, Any] | None,
) -> dict[str, Any]:
    spot_name = (fields.get("spot_name") or fields.get("title") or "").strip()
    visit_date = (fields.get("visit_date") or fields.get("event_date") or "").strip()
    time_slot = (fields.get("time_slot") or "").strip() or "全日適用／預約時段入場"
    location = (fields.get("location") or "").strip()
    ticket_type = (fields.get("ticket_type") or "").strip()
    redemption = (
        (fields.get("redemption_method") or "").strip()
        or "於現場指定入口直接出示電子 QR Code 掃碼入場"
    )
    guest_name = (
        (fields.get("guest_name") or "").strip().upper()
        or (traveler.get("passport_name") if traveler else "")
        or "TRAVELER"
    )

    if not spot_name:
        raise ValueError("開立景點憑證時，請填寫景點／活動名稱。")
    if not visit_date:
        raise ValueError("開立景點憑證時，請填寫預約使用日期。")
    if not location:
        raise ValueError("開立景點憑證時，請填寫入場或集合地點。")
    if not ticket_type:
        raise ValueError("開立景點憑證時，請填寫票種與數量（例如 成人快速通關雙人券）。")

    return {
        "title": spot_name,
        "subtitle": f"{ticket_type} · {time_slot}",
        "event_date": f"{visit_date}（{time_slot}）",
        "location": location,
        "seat_or_room": ticket_type,
        "spot_name": spot_name,
        "visit_date": visit_date,
        "time_slot": time_slot,
        "ticket_type": ticket_type,
        "redemption_method": redemption,
        "guest_name": guest_name,
        "notes": (fields.get("notes") or "").strip()
        or "請提前 15 分鐘抵達指定集合或驗票入口並備妥電子憑證。",
    }


def issue_voucher(
    db: Session,
    order: OrderQuote,
    category: str,
    fields: dict[str, str],
    booking_code: str = "",
) -> BookingVoucher:
    """Validate category-specific fields, persist BookingVoucher, and promote BOOKED if complete."""
    clean_cat = (category or "").strip().upper()
    if clean_cat not in VOUCHER_CATEGORIES:
        raise ValueError(f"不支援的憑證類別：{category}")
    if order.order_status not in ("PAID", "BOOKED"):
        raise ValueError(f"訂單目前狀態為 {order.order_status}，僅限已付款訂單可核發憑證。")
    if has_pending_exception(db, order.order_id):
        raise ValueError("此訂單尚有未解決的異常紀錄（PENDING），請待會員抉擇後再核發憑證。")

    draft = parse_draft_json(order.trip_plan.llm_draft_json) if order.trip_plan else None
    traveler = get_traveler_profile(draft)

    if clean_cat == "MATCH":
        detail = _build_validated_match_detail(fields, order, traveler)
    elif clean_cat == "FLIGHT":
        detail = _build_validated_flight_detail(fields, traveler)
    elif clean_cat == "HOTEL":
        detail = _build_validated_hotel_detail(fields, traveler)
    else:
        detail = _build_validated_spot_detail(fields, traveler)

    code = (booking_code or "").strip().upper() or generate_booking_code(clean_cat)
    voucher = BookingVoucher(
        order_id=order.order_id,
        category=clean_cat,
        booking_code=code,
        detail_json=json.dumps(detail, ensure_ascii=False),
    )
    db.add(voucher)
    db.flush()

    checklist = compute_fulfillment_checklist(db, order)
    if checklist["is_complete"]:
        order.order_status = "BOOKED"

    db.commit()
    db.refresh(voucher)
    db.refresh(order)
    return voucher


def _infer_flight_route_defaults(league_type: str, matches: list[dict[str, Any]]) -> dict[str, str]:
    """Choose realistic airline, IATA codes, and flight numbers matching trip league/city."""
    text = f"{league_type} " + " ".join(
        f"{m.get('matchup', '')} {m.get('venue', '')}"
        for m in matches
        if isinstance(m, dict)
    )
    low = text.lower()
    if "歐冠" in low or "real madrid" in low or "bernabeu" in low or "barcelona" in low:
        return {"airline": "Emirates 阿聯酋航空 / Iberia", "orig": "TPE", "dest": "MAD", "out": "EK367", "ret": "EK366"}
    if "德甲" in low or "bayern" in low or "cologne" in low or "allianz arena" in low:
        return {"airline": "EVA Air 長榮航空", "orig": "TPE", "dest": "MUC", "out": "BR071", "ret": "BR072"}
    if "義甲" in low or "milan" in low or "juventus" in low:
        return {"airline": "EVA Air 長榮航空", "orig": "TPE", "dest": "MXP", "out": "BR095", "ret": "BR096"}
    if "法甲" in low or "psg" in low or "paris" in low:
        return {"airline": "EVA Air 長榮航空", "orig": "TPE", "dest": "CDG", "out": "BR087", "ret": "BR088"}
    if "英超" in low or "歐足" in low or "arsenal" in low or "liverpool" in low or "chelsea" in low:
        return {"airline": "China Airlines 中華航空", "orig": "TPE", "dest": "LHR", "out": "CI081", "ret": "CI082"}
    if "warriors" in low or "giants" in low or "chase center" in low:
        return {"airline": "EVA Air 長榮航空", "orig": "TPE", "dest": "SFO", "out": "BR028", "ret": "BR027"}
    if "yankees" in low or "mets" in low or "knicks" in low or "madison square" in low:
        return {"airline": "EVA Air 長榮航空", "orig": "TPE", "dest": "JFK", "out": "BR032", "ret": "BR031"}
    return {"airline": "STARLUX 星宇航空", "orig": "TPE", "dest": "LAX", "out": "JX002", "ret": "JX001"}


def auto_issue_trip_vouchers(db: Session, order: OrderQuote) -> list[BookingVoucher]:
    """One-click generate industry-standard vouchers fulfilling all missing required items."""
    if order.order_status not in ("PAID", "BOOKED"):
        raise ValueError(f"訂單目前狀態為 {order.order_status}，無法批次核發憑證。")
    if has_pending_exception(db, order.order_id):
        raise ValueError("此訂單尚有待會員決策之異常紀錄，無法核發憑證。")
    if order.trip_plan is None:
        raise ValueError("找不到關聯行程規劃資料。")

    trip = order.trip_plan
    draft = parse_draft_json(trip.llm_draft_json) or {}
    traveler = get_traveler_profile(draft)

    existing = (
        db.query(BookingVoucher)
        .filter(BookingVoucher.order_id == order.order_id)
        .all()
    )
    existing_match_titles = {
        str(parse_voucher_detail(v.detail_json).get("title") or "")
        for v in existing
        if v.category == "MATCH"
    }
    has_flight = any(v.category == "FLIGHT" for v in existing)
    has_hotel = any(v.category == "HOTEL" for v in existing)

    created: list[BookingVoucher] = []

    if order.order_type == "ADDON":
        existing_spot_titles = {
            str(parse_voucher_detail(v.detail_json).get("title") or "")
            for v in existing
            if v.category == "SPOT"
        }
        addon_items = (
            db.query(GapItineraryItem)
            .filter(GapItineraryItem.addon_order_id == order.order_id)
            .order_by(GapItineraryItem.day_number.asc(), GapItineraryItem.item_id.asc())
            .all()
        )
        default_city = str(_get_league_preset(trip.league_type).get("city") or "主辦城市")
        day_cities = {
            int(d.get("day_number")): str(d.get("city") or default_city)
            for d in (draft.get("days") or [])
            if isinstance(d, dict) and d.get("day_number")
        }
        if addon_items:
            for item in addon_items:
                if item.activity_name in existing_spot_titles:
                    continue
                existing_spot_titles.add(item.activity_name)
                visit_dt = trip.start_date + timedelta(days=max(item.day_number - 1, 0))
                city = day_cities.get(item.day_number, default_city)
                spot_fields = {
                    "spot_name": item.activity_name,
                    "visit_date": visit_dt.isoformat(),
                    "time_slot": item.time_slot,
                    "location": f"{city} · {item.activity_name} 官方電子票入口",
                    "ticket_type": "加購雙人電子門票／快速通關券 ×2",
                    "redemption_method": "於現場指定入口直接出示電子 QR Code 掃碼入場",
                    "notes": f"Day {item.day_number}（{item.time_slot}）空檔景點門票代訂加購憑證",
                }
                spot_detail = _build_validated_spot_detail(spot_fields, traveler)
                v = BookingVoucher(
                    order_id=order.order_id,
                    category="SPOT",
                    booking_code=generate_booking_code("SPOT"),
                    detail_json=json.dumps(spot_detail, ensure_ascii=False),
                )
                db.add(v)
                created.append(v)
        elif not existing_spot_titles:
            spot_fields = {
                "spot_name": f"{trip.league_type} 精選加購景點快速通關門票",
                "visit_date": trip.start_date.isoformat(),
                "time_slot": "下午預約梯次",
                "location": f"{default_city} 指定景點快速通關入口",
                "ticket_type": "加購雙人官方電子門票 ×2",
                "redemption_method": "出示類 Agoda 訂票憑證與護照英文姓名掃碼入場",
            }
            spot_detail = _build_validated_spot_detail(spot_fields, traveler)
            v = BookingVoucher(
                order_id=order.order_id,
                category="SPOT",
                booking_code=generate_booking_code("SPOT"),
                detail_json=json.dumps(spot_detail, ensure_ascii=False),
            )
            db.add(v)
            created.append(v)

        if created:
            db.flush()
            checklist = compute_fulfillment_checklist(db, order)
            if checklist["is_complete"]:
                order.order_status = "BOOKED"
            db.commit()
            for v in created:
                db.refresh(v)
            db.refresh(order)
        return created

    matches = draft.get("matches") if isinstance(draft.get("matches"), list) else []

    if matches:
        for idx, m in enumerate(matches):
            if not isinstance(m, dict):
                continue
            match_title = str(m.get("matchup") or f"{trip.league_type} 官方觀賽場次 #{idx + 1}").strip()
            if match_title in existing_match_titles:
                continue
            existing_match_titles.add(match_title)
            fmt_key = infer_league_ticket_format(
                trip.league_type,
                f"{m.get('league', '')} {match_title} {m.get('venue', '')}",
            )
            is_eu = LEAGUE_TICKET_FORMATS[fmt_key]["seat_mode"] in ("EU", "UEFA")
            match_fields = {
                "league_format": fmt_key,
                "matchup": match_title,
                "event_date": str(m.get("date") or trip.start_date.isoformat()),
                "event_time": "20:00（當地開賽）" if fmt_key == "UEFA_UCL" else "19:30（當地開賽）",
                "venue": str(m.get("venue") or "官方指定主場館"),
                "stand": "North / Main Lower Stand" if is_eu else "",
                "section": "" if is_eu else f"{108 + idx}",
                "block": f"Block {110 + idx}" if is_eu else "",
                "row": "F",
                "seat": "11-12",
                "gate_or_turnstile": "Entrance C · Turnstile 14" if is_eu else "South Main Gate",
                "notes": str(m.get("note") or ""),
            }
            detail = _build_validated_match_detail(match_fields, order, traveler)
            v = BookingVoucher(
                order_id=order.order_id,
                category="MATCH",
                booking_code=generate_booking_code("MATCH"),
                detail_json=json.dumps(detail, ensure_ascii=False),
            )
            db.add(v)
            created.append(v)
    elif not existing_match_titles:
        fmt_key = infer_league_ticket_format(trip.league_type)
        is_eu = LEAGUE_TICKET_FORMATS[fmt_key]["seat_mode"] in ("EU", "UEFA")
        fallback_fields = {
            "league_format": fmt_key,
            "matchup": f"{trip.league_type} 官方焦點賽事雙人觀賽券",
            "event_date": trip.start_date.isoformat(),
            "event_time": "19:30（當地開賽）",
            "venue": "官方主辦球場",
            "stand": "Main Lower Stand" if is_eu else "",
            "section": "" if is_eu else "112",
            "block": "Block 112" if is_eu else "",
            "row": "G",
            "seat": "5-6",
            "gate_or_turnstile": "Entrance B · Turnstile 08" if is_eu else "Gate A",
        }
        detail = _build_validated_match_detail(fallback_fields, order, traveler)
        v = BookingVoucher(
            order_id=order.order_id,
            category="MATCH",
            booking_code=generate_booking_code("MATCH"),
            detail_json=json.dumps(detail, ensure_ascii=False),
        )
        db.add(v)
        created.append(v)

    if trip.need_flight and not has_flight:
        route = _infer_flight_route_defaults(trip.league_type, matches)
        pnr = secrets.token_hex(3).upper()
        eticket = f"695-{secrets.randbelow(9000000000) + 1000000000}"
        flight_fields = {
            "airline": route["airline"],
            "pnr": pnr,
            "eticket_number": eticket,
            "passenger_name": (traveler.get("passport_name") if traveler else "") or "TRAVELER, DEMO",
            "origin_airport": route["orig"],
            "dest_airport": route["dest"],
            "outbound_flight_no": route["out"],
            "outbound_datetime": f"{trip.start_date.isoformat()} 23:30 ({route['orig']} T2) → 隔日當地抵達 ({route['dest']})",
            "return_flight_no": route["ret"],
            "return_datetime": f"{trip.end_date.isoformat()} 11:45 ({route['dest']}) → 隔日返抵 {route['orig']}",
            "cabin_class": "經濟艙 Economy (M)",
            "baggage_allowance": "託運 1 件 23kg ＋ 手提 7kg",
            "seat_assignment": "34A / 34B（靠窗相鄰）",
            "notes": str(draft.get("flight_suggestion") or ""),
        }
        flight_detail = _build_validated_flight_detail(flight_fields, traveler)
        v = BookingVoucher(
            order_id=order.order_id,
            category="FLIGHT",
            booking_code=f"{pnr} ({generate_booking_code('FLIGHT')})",
            detail_json=json.dumps(flight_detail, ensure_ascii=False),
        )
        db.add(v)
        created.append(v)

    if trip.need_hotel and not has_hotel:
        hotel_fields = {
            "hotel_name": f"{trip.league_type} Grand Central Premier Hotel",
            "hotel_address": "100 Stadium Transit Blvd, City Center (鄰近球場捷運直達站)",
            "guest_name": (traveler.get("passport_name") if traveler else "") or "TRAVELER, DEMO",
            "checkin_date": trip.start_date.isoformat(),
            "checkout_date": trip.end_date.isoformat(),
            "room_type": "Deluxe Twin Room（豪華雙床禁菸房）",
            "guests_count": "2 位成人 (2 Adults)",
            "meal_plan": "含每日雙人自助早餐 (Breakfast Included)",
            "notes": str(draft.get("hotel_suggestion") or ""),
        }
        hotel_detail = _build_validated_hotel_detail(hotel_fields, traveler)
        v = BookingVoucher(
            order_id=order.order_id,
            category="HOTEL",
            booking_code=generate_booking_code("HOTEL"),
            detail_json=json.dumps(hotel_detail, ensure_ascii=False),
        )
        db.add(v)
        created.append(v)

    if created:
        db.flush()
        checklist = compute_fulfillment_checklist(db, order)
        if checklist["is_complete"]:
            order.order_status = "BOOKED"
        db.commit()
        for v in created:
            db.refresh(v)
        db.refresh(order)

    return created


def delete_voucher(db: Session, order: OrderQuote, voucher_id: int) -> str:
    """Revoke an erroneously issued voucher and revert BOOKED -> PAID if checklist is incomplete."""
    voucher = db.get(BookingVoucher, voucher_id)
    if voucher is None or voucher.order_id != order.order_id:
        raise ValueError("找不到指定的訂票憑證。")

    code = voucher.booking_code
    db.delete(voucher)
    db.flush()

    checklist = compute_fulfillment_checklist(db, order)
    if order.order_status == "BOOKED" and not checklist["is_complete"]:
        order.order_status = "PAID"

    db.commit()
    db.refresh(order)
    return code


def report_booking_exception(
    db: Session,
    order: OrderQuote,
    reason: str,
    alternative_proposal: str,
) -> BookingException:
    """Create a PENDING BookingException and switch order_status to EXCEPTION."""
    if order.order_status not in ("PAID", "BOOKED"):
        raise ValueError(f"訂單目前狀態為 {order.order_status}，僅限已付款待代訂訂單回報異常。")
    if has_pending_exception(db, order.order_id):
        raise ValueError("此訂單已有尚未處理的異常回報，請待會員決策完成。")

    clean_reason = (reason or "").strip()
    clean_alt = (alternative_proposal or "").strip()
    if not clean_reason or not clean_alt:
        raise ValueError("請同時填寫異常原因與給會員的替代方案說明。")

    exc = BookingException(
        order_id=order.order_id,
        reason=clean_reason,
        alternative_proposal=clean_alt,
        user_decision="PENDING",
        resolved_at=None,
    )
    db.add(exc)
    order.order_status = "EXCEPTION"
    db.commit()
    db.refresh(exc)
    db.refresh(order)
    return exc


def resolve_booking_exception(
    db: Session,
    order: OrderQuote,
    exc: BookingException,
    decision: str,
) -> tuple[BookingException, PaymentTxn | None]:
    """Apply member exception decision: ALT (resume PAID) or REFUND (D4 refund & CANCELLED)."""
    if exc.order_id != order.order_id:
        raise ValueError("異常紀錄與訂單不相符。")
    if exc.user_decision != "PENDING":
        raise ValueError("此筆異常紀錄已完成決策。")

    clean_decision = (decision or "").strip().upper()
    if clean_decision not in ("ALT", "REFUND"):
        raise ValueError(f"不支援的異常處理決定：{decision}")

    exc.user_decision = clean_decision
    exc.resolved_at = utc_now()

    if clean_decision == "ALT":
        order.order_status = "PAID"
        db.commit()
        db.refresh(exc)
        db.refresh(order)
        return exc, None

    refund_txn = process_mock_refund(db, order)
    if order.order_type == "MAIN_TRIP" and order.trip_plan is not None:
        order.trip_plan.status = "CANCELLED"
        db.commit()
        db.refresh(order.trip_plan)
    db.refresh(exc)
    db.refresh(order)
    return exc, refund_txn


def build_voucher_view_items(vouchers: list[BookingVoucher]) -> list[dict[str, Any]]:
    """Pair each BookingVoucher ORM row with parsed detail_json and category display metadata."""
    items: list[dict[str, Any]] = []
    for v in vouchers:
        meta = VOUCHER_CATEGORIES.get(
            v.category,
            {"label": "代訂確認憑證", "prefix": "AGD", "badge": v.category},
        )
        items.append(
            {
                "voucher": v,
                "meta": meta,
                "detail": parse_voucher_detail(v.detail_json),
            }
        )
    return items
