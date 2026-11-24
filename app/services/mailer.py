"""Email notification service via Gmail SMTP with automatic safe Mock fallback."""

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
import smtplib
from typing import Any

import app.config as config

_DEMO_DOMAINS = ("@example.com", "@sportstravel.demo")
_EMAIL_DELIVERY_LOG: dict[int, list[dict[str, Any]]] = {}
_TAIPEI_TZ = timezone(timedelta(hours=8))


def clear_email_logs() -> None:
    """Clear in-memory email audit records (primarily used by tests)."""
    _EMAIL_DELIVERY_LOG.clear()


def get_order_email_logs(order_id: int) -> list[dict[str, Any]]:
    """Return recorded email delivery entries for an order (newest first)."""
    return list(_EMAIL_DELIVERY_LOG.get(order_id, []))


def resolve_delivery_recipient(original_email: str) -> tuple[str, bool]:
    """Redirect seeded demo domains (@example.com / @sportstravel.demo) to SMTP_DEMO_TO."""
    cleaned = (original_email or "").strip()
    if not cleaned:
        fallback = config.SMTP_DEMO_TO or config.SMTP_USER
        return fallback, bool(fallback)

    lowered = cleaned.lower()
    if lowered.endswith(_DEMO_DOMAINS):
        target = config.SMTP_DEMO_TO or config.SMTP_USER or cleaned
        return target, target.lower() != lowered
    return cleaned, False


def _send_via_smtp(msg: EmailMessage) -> None:
    """Transmit an EmailMessage over Gmail SMTP SSL (465) or STARTTLS."""
    if config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(
            config.SMTP_HOST,
            config.SMTP_PORT,
            timeout=config.SMTP_TIMEOUT_SEC,
        ) as smtp:
            smtp.login(config.SMTP_USER, config.SMTP_APP_PASSWORD)
            smtp.send_message(msg)
        return

    with smtplib.SMTP(
        config.SMTP_HOST,
        config.SMTP_PORT,
        timeout=config.SMTP_TIMEOUT_SEC,
    ) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(config.SMTP_USER, config.SMTP_APP_PASSWORD)
        smtp.send_message(msg)


def send_notification_email(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    category: str,
    order_id: int | None = None,
) -> dict[str, Any]:
    """Send an HTML/text notification email via Gmail SMTP, falling back cleanly on failure."""
    actual_to, redirected = resolve_delivery_recipient(to_email)
    final_subject = (
        f"[Demo 轉寄 · 原收件人 {to_email}] {subject}"
        if redirected and to_email
        else subject
    )

    can_attempt_smtp = bool(
        config.SMTP_USER
        and config.SMTP_APP_PASSWORD
        and "@" in actual_to
        and not actual_to.lower().endswith(_DEMO_DOMAINS)
    )

    if can_attempt_smtp:
        msg = EmailMessage()
        msg["Subject"] = final_subject
        msg["From"] = f"體育賽事旅遊規劃與代訂系統 <{config.SMTP_USER}>"
        msg["To"] = actual_to
        msg.set_content(text_body, subtype="plain", charset="utf-8")
        msg.add_alternative(html_body, subtype="html", charset="utf-8")

        try:
            _send_via_smtp(msg)
            mode = "gmail_smtp"
            status = "SENT"
            note = f"已透過 Gmail SMTP 寄送通知信至 {actual_to}"
        except Exception as exc:
            mode = "mock_fallback"
            status = "MOCK_FALLBACK"
            note = (
                f"Gmail SMTP 寄送異常（{type(exc).__name__}），"
                "已自動降級為本機展示通知紀錄。"
            )
    else:
        mode = "mock_fallback"
        status = "MOCK_FALLBACK"
        note = "尚未設定 Gmail SMTP（SMTP_USER / SMTP_APP_PASSWORD），已自動使用本機防呆寄信紀錄。"

    entry = {
        "sent_at": datetime.now(_TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S (UTC+8)"),
        "category": category,
        "original_to": to_email or actual_to,
        "actual_to": actual_to,
        "redirected": redirected,
        "subject": final_subject,
        "text_preview": text_body.strip()[:280],
        "mode": mode,
        "status": status,
        "note": note,
    }

    if order_id is not None:
        bucket = _EMAIL_DELIVERY_LOG.setdefault(order_id, [])
        bucket.insert(0, entry)
        if len(bucket) > 20:
            del bucket[20:]

    return entry


def _wrap_html_card(title: str, subtitle: str, body_html: str, action_url: str, action_label: str) -> str:
    """Render a clean responsive HTML email card."""
    safe_title = escape(title)
    safe_sub = escape(subtitle)
    safe_url = escape(action_url, quote=True)
    safe_btn = escape(action_label)
    return (
        '<div style="font-family:\'Noto Sans TC\',Arial,sans-serif;max-width:600px;margin:0 auto;'
        'background:#f8fafc;padding:24px;color:#0f172a;">'
        '<div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;">'
        '<div style="background:#0f172a;color:#ffffff;padding:18px 24px;">'
        '<div style="font-size:12px;color:#93c5fd;font-weight:700;">體育賽事旅遊規劃與代訂系統</div>'
        f'<h1 style="margin:6px 0 0;font-size:20px;">{safe_title}</h1>'
        f'<p style="margin:4px 0 0;font-size:13px;color:#cbd5e1;">{safe_sub}</p>'
        "</div>"
        f'<div style="padding:22px 24px;font-size:14px;line-height:1.7;">{body_html}'
        '<div style="margin-top:20px;">'
        f'<a href="{safe_url}" style="display:inline-block;background:#0284c7;color:#ffffff;'
        'text-decoration:none;font-weight:700;padding:10px 18px;border-radius:10px;">'
        f"{safe_btn}</a></div></div>"
        '<div style="background:#f1f5f9;padding:12px 24px;font-size:12px;color:#64748b;">'
        "此信件由系統自動發送（支援一口價無訂金全額結帳與專員代訂流程）。"
        "</div></div></div>"
    )


def send_quoting_request_email(order: Any, trip: Any, user: Any, agent: Any) -> dict[str, Any]:
    """Notify booking agent when a member confirms an itinerary draft for quoting."""
    agent_id = getattr(agent, "agent_id", None) or getattr(order, "agent_id", None) or "AG001"
    agent_name = getattr(agent, "agent_name", None) or "訂票專員"
    agent_email = getattr(agent, "email", None) or ""
    member_name = getattr(user, "full_name", None) or "會員"
    member_email = getattr(user, "email", None) or ""
    order_url = f"{config.APP_BASE_URL}/orders/{order.order_id}"

    subject = f"[待報價需求] 訂單 #{order.order_id}（{trip.league_type}）請專員 {agent_id} 開立一口價"
    text_body = (
        f"{agent_name}（{agent_id}）您好：\n"
        f"會員 {member_name}（{member_email}）已確認行程大架構 #{trip.trip_id}，"
        f"系統已產生待報價詢價單 #{order.order_id}。\n"
        f"賽事聯賽：{trip.league_type}\n"
        f"旅遊區間：{trip.start_date} ~ {trip.end_date}\n"
        f"代訂需求：機票={'需要' if trip.need_flight else '自理'} / 飯店={'需要' if trip.need_hotel else '自理'}\n"
        f"請登入專員視角開立正式一口價報價單（無訂金）：{order_url}"
    )
    body_html = (
        f"<p><strong>{escape(agent_name)}（{escape(agent_id)}）您好：</strong></p>"
        f"<p>會員 <strong>{escape(member_name)}</strong> 已確認賽事旅遊大架構，請審核並開立正式一口價報價單。</p>"
        "<ul>"
        f"<li>訂單編號：<strong>#{order.order_id}</strong>（{escape(order.order_type)}）</li>"
        f"<li>賽事聯賽：{escape(trip.league_type)}</li>"
        f"<li>旅遊日期：{trip.start_date} ~ {trip.end_date}</li>"
        f"<li>代訂項目：球賽門票{' ＋ 來回機票' if trip.need_flight else ''}{' ＋ 飯店住宿' if trip.need_hotel else ''}</li>"
        "</ul>"
    )
    html_body = _wrap_html_card(
        title=f"新待報價詢價單 #{order.order_id}",
        subtitle=f"指派專員：{agent_id} {agent_name}",
        body_html=body_html,
        action_url=order_url,
        action_label="前往專員報價頁",
    )
    return send_notification_email(
        to_email=agent_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        category="QUOTING_REQUEST",
        order_id=order.order_id,
    )


def send_quote_ready_email(
    order: Any,
    trip: Any,
    user: Any,
    agent: Any,
    quote_note: str = "",
) -> dict[str, Any]:
    """Notify member with official one-price quote details and payment link."""
    agent_id = getattr(agent, "agent_id", None) or getattr(order, "agent_id", None) or "AG001"
    agent_name = getattr(agent, "agent_name", None) or "訂票專員"
    member_name = getattr(user, "full_name", None) or "會員"
    member_email = getattr(user, "email", None) or ""
    price_str = f"NT$ {order.quoted_price:,}"
    order_url = f"{config.APP_BASE_URL}/orders/{order.order_id}"

    is_addon = getattr(order, "order_type", "MAIN_TRIP") == "ADDON"
    order_kind_label = "景點門票加購" if is_addon else "賽事旅遊主行程"
    quote_work_desc = "景點門票與快速通關核算" if is_addon else "即時球票與機酒核算"
    post_pay_desc = (
        "全額付款完成後，專員將立即核發 SPOT 景點電子入場憑證。"
        if is_addon
        else "全額付款完成後，系統將立即指派專員進行代訂，並解鎖您的免費空檔旅遊行程規劃權限。"
    )

    subject = f"[一口價報價單完成] 訂單 #{order.order_id}（{trip.league_type}）{price_str}"
    note_line = f"\n專員備註：{quote_note}" if quote_note else ""
    text_body = (
        f"{member_name} 您好：\n"
        f"訂票專員 {agent_id}（{agent_name}）已為您的{order_kind_label}（行程 #{trip.trip_id}）開立正式一口價報價單（無訂金、一次付清）。\n"
        f"訂單編號：#{order.order_id}\n"
        f"正式一口價金額：{price_str}\n"
        f"旅遊區間：{trip.start_date} ~ {trip.end_date}（{trip.league_type}）"
        f"{note_line}\n"
        f"請點擊以下連結查看明細並完成全額付款（{post_pay_desc}）：\n{order_url}"
    )
    note_html = (
        f"<p style=\"background:#f8fafc;padding:10px 12px;border-radius:8px;border:1px solid #e2e8f0;\">"
        f"<strong>專員報價備註：</strong>{escape(quote_note)}</p>"
        if quote_note
        else ""
    )
    body_html = (
        f"<p><strong>{escape(member_name)} 您好：</strong></p>"
        f"<p>您的專屬訂票專員 <strong>{escape(agent_id)}（{escape(agent_name)}）</strong> 已完成{quote_work_desc}，開立正式一口價報價單：</p>"
        "<div style=\"background:#ecfdf5;border:1px solid #a7f3d0;border-radius:10px;padding:14px 16px;margin:12px 0;\">"
        "<div style=\"font-size:12px;color:#047857;font-weight:700;\">正式一口價總額（無訂金 · 一次付清）</div>"
        f"<div style=\"font-size:24px;font-weight:800;color:#064e3b;margin-top:4px;\">{price_str}</div>"
        "</div>"
        "<ul>"
        f"<li>訂單編號：#{order.order_id}（{order_kind_label}）</li>"
        f"<li>行程規劃：#{trip.trip_id} · {escape(trip.league_type)}（{trip.start_date} ~ {trip.end_date}）</li>"
        "</ul>"
        f"{note_html}"
        f"<p style=\"color:#475569;font-size:13px;\">{post_pay_desc}</p>"
    )
    html_body = _wrap_html_card(
        title=f"您的正式一口價報價單已開立（#{order.order_id}）",
        subtitle=f"承辦專員：{agent_id} {agent_name}",
        body_html=body_html,
        action_url=order_url,
        action_label="檢視報價單並一次付清",
    )
    return send_notification_email(
        to_email=member_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        category="QUOTE_READY",
        order_id=order.order_id,
    )


def send_payment_success_emails(
    order: Any,
    trip: Any,
    user: Any,
    agent: Any,
    txn: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Send full payment receipt to member and paid-order dispatch email to agent."""
    agent_id = getattr(agent, "agent_id", None) or getattr(order, "agent_id", None) or "AG001"
    agent_name = getattr(agent, "agent_name", None) or "訂票專員"
    agent_email = getattr(agent, "email", None) or ""
    member_name = getattr(user, "full_name", None) or "會員"
    member_email = getattr(user, "email", None) or ""
    price_str = f"NT$ {txn.amount:,}"
    order_url = f"{config.APP_BASE_URL}/orders/{order.order_id}"

    is_addon = getattr(order, "order_type", "MAIN_TRIP") == "ADDON"
    fulfill_target = "景點門票（SPOT 電子憑證）" if is_addon else "球賽門票／機票／飯店"
    member_status_banner = (
        "✓ 景點門票加購訂單已派單！訂票專員將盡速為您開立 SPOT 景點電子憑證。"
        if is_addon
        else "✓ 您的行程已進入鎖定代訂狀態（LOCKED），免費空檔行程規劃權限已解鎖！"
    )

    member_subject = f"[全額付款成功收據] 訂單 #{order.order_id} 已付清 {price_str}"
    member_text = (
        f"{member_name} 您好：\n"
        f"您已成功全額付清訂單 #{order.order_id}（{trip.league_type}）。\n"
        f"金流交易編號：{txn.gateway_ref}\n"
        f"實付金額：{price_str}\n"
        f"系統已通知專員 {agent_id} 進行人工代訂（{fulfill_target}）。\n查看訂單：{order_url}"
    )
    member_body_html = (
        f"<p><strong>{escape(member_name)} 您好：</strong></p>"
        "<p>感謝您完成全額付款！以下為本次電子付款收據：</p>"
        "<ul>"
        f"<li>訂單編號：<strong>#{order.order_id}</strong>（狀態：PAID）</li>"
        f"<li>交易編號：<code>{escape(txn.gateway_ref or '')}</code></li>"
        f"<li>實付金額：<strong>{price_str}</strong>（一口價無訂金）</li>"
        f"<li>承辦專員：{escape(agent_id)}（{escape(agent_name)}）</li>"
        "</ul>"
        "<p style=\"background:#eef2ff;border:1px solid #c7d2fe;border-radius:10px;padding:12px 14px;color:#312e81;\">"
        f"{member_status_banner}</p>"
    )
    member_html = _wrap_html_card(
        title=f"全額付款成功收據 · #{order.order_id}",
        subtitle=f"交易編號：{txn.gateway_ref}",
        body_html=member_body_html,
        action_url=order_url,
        action_label="檢視訂單與交易收據",
    )
    member_res = send_notification_email(
        to_email=member_email,
        subject=member_subject,
        html_body=member_html,
        text_body=member_text,
        category="PAYMENT_SUCCESS",
        order_id=order.order_id,
    )

    agent_subject = f"[已付款待代訂派單] 訂單 #{order.order_id}（{trip.league_type}）請專員 {agent_id} 核發憑證"
    agent_text = (
        f"{agent_name}（{agent_id}）您好：\n"
        f"會員 {member_name} 已完成訂單 #{order.order_id} 全額付款（{price_str}，交易編號 {txn.gateway_ref}）。\n"
        f"請盡速執行{fulfill_target}人工代訂，並核發類 Agoda 訂票憑證。\n訂單頁：{order_url}"
    )
    agent_body_html = (
        f"<p><strong>{escape(agent_name)}（{escape(agent_id)}）您好：</strong></p>"
        f"<p>會員 <strong>{escape(member_name)}</strong> 已全額付清一口價訂單 <strong>#{order.order_id}</strong>，系統已自動派單待您執行實際代訂與開立憑證。</p>"
        "<ul>"
        f"<li>實收金額：{price_str}</li>"
        f"<li>金流參考號：<code>{escape(txn.gateway_ref or '')}</code></li>"
        f"<li>行程內容：{escape(trip.league_type)}（{trip.start_date} ~ {trip.end_date}）</li>"
        "</ul>"
    )
    agent_html = _wrap_html_card(
        title=f"已付款待訂票派單 #{order.order_id}",
        subtitle=f"會員：{member_name} · 一口價 {price_str}",
        body_html=agent_body_html,
        action_url=order_url,
        action_label="前往檢視待訂票訂單",
    )
    agent_res = send_notification_email(
        to_email=agent_email,
        subject=agent_subject,
        html_body=agent_html,
        text_body=agent_text,
        category="ORDER_DISPATCHED",
        order_id=order.order_id,
    )

    return member_res, agent_res


def send_payment_failed_email(
    order: Any,
    trip: Any,
    user: Any,
    txn: Any,
) -> dict[str, Any]:
    """Notify member when a simulated payment attempt fails so they can retry."""
    member_name = getattr(user, "full_name", None) or "會員"
    member_email = getattr(user, "email", None) or ""
    price_str = f"NT$ {txn.amount:,}"
    order_url = f"{config.APP_BASE_URL}/orders/{order.order_id}"

    subject = f"[付款失敗提醒] 訂單 #{order.order_id} 交易未完成（未扣款）"
    text_body = (
        f"{member_name} 您好：\n"
        f"您於訂單 #{order.order_id}（{trip.league_type}）嘗試全額付款 {price_str} 時發生授權失敗（參考號 {txn.gateway_ref}），"
        "本次交易未扣款，您的報價單仍保持有效（QUOTED），可隨時回到訂單頁面重新付款：\n"
        f"{order_url}"
    )
    body_html = (
        f"<p><strong>{escape(member_name)} 您好：</strong></p>"
        f"<p>您的訂單 <strong>#{order.order_id}</strong>（{escape(trip.league_type)}）於嘗試全額付款 <strong>{price_str}</strong> 時授權失敗，本次交易並未扣款。</p>"
        "<ul>"
        f"<li>失敗交易參考號：<code>{escape(txn.gateway_ref or '')}</code></li>"
        "<li>目前訂單狀態：仍維持 QUOTED（報價單持續有效）</li>"
        "</ul>"
    )
    html_body = _wrap_html_card(
        title=f"付款未成功提醒 · 訂單 #{order.order_id}",
        subtitle="本次交易未扣款，可立即重新嘗試付款",
        body_html=body_html,
        action_url=order_url,
        action_label="返回訂單重新付款",
    )
    return send_notification_email(
        to_email=member_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        category="PAYMENT_FAILED",
        order_id=order.order_id,
    )


def send_voucher_issued_email(
    order: Any,
    trip: Any,
    user: Any,
    agent: Any,
    vouchers: list[Any],
) -> dict[str, Any]:
    """Notify member that Agoda-style booking confirmation vouchers have been issued."""
    agent_id = getattr(agent, "agent_id", None) or getattr(order, "agent_id", None) or "AG001"
    agent_name = getattr(agent, "agent_name", None) or "訂票專員"
    member_name = getattr(user, "full_name", None) or "會員"
    member_email = getattr(user, "email", None) or ""
    voucher_url = f"{config.APP_BASE_URL}/orders/{order.order_id}/vouchers"

    code_lines = [f"- [{v.category}] {v.booking_code}" for v in vouchers]
    subject = f"[訂票憑證已核發] 訂單 #{order.order_id}（{trip.league_type}）共 {len(vouchers)} 張類 Agoda 確認單"
    text_body = (
        f"{member_name} 您好：\n"
        f"您的專屬訂票專員 {agent_id}（{agent_name}）已完成實質代訂作業並核發最新訂票憑證：\n"
        + "\n".join(code_lines)
        + f"\n請點擊以下連結檢視完整類 Agoda 確認憑證與條碼明細：\n{voucher_url}"
    )
    list_html = "".join(
        f"<li><strong>{escape(v.category)}</strong>：<code>{escape(v.booking_code)}</code></li>"
        for v in vouchers
    )
    body_html = (
        f"<p><strong>{escape(member_name)} 您好：</strong></p>"
        f"<p>訂票專員 <strong>{escape(agent_id)}（{escape(agent_name)}）</strong> 已為您的訂單 <strong>#{order.order_id}</strong> 核發以下類 Agoda 官方確認憑證：</p>"
        f"<ul>{list_html}</ul>"
        "<p style=\"color:#065f46;background:#ecfdf5;padding:10px 12px;border-radius:8px;border:1px solid #a7f3d0;\">"
        "✓ 訂單已進入 BOOKED 完訂狀態，您可隨時線上檢視或列印 PDF 憑證。</p>"
    )
    html_body = _wrap_html_card(
        title=f"訂票成功憑證已核發 · #{order.order_id}",
        subtitle=f"承辦專員：{agent_id} {agent_name} · 共 {len(vouchers)} 張確認單",
        body_html=body_html,
        action_url=voucher_url,
        action_label="開啟類 Agoda 訂票憑證確認單",
    )
    return send_notification_email(
        to_email=member_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        category="VOUCHER_ISSUED",
        order_id=order.order_id,
    )


def send_exception_alert_email(
    order: Any,
    trip: Any,
    user: Any,
    agent: Any,
    exc: Any,
) -> dict[str, Any]:
    """Notify member of a booking exception with an alternative proposal or full refund choice."""
    agent_id = getattr(agent, "agent_id", None) or getattr(order, "agent_id", None) or "AG001"
    agent_name = getattr(agent, "agent_name", None) or "訂票專員"
    member_name = getattr(user, "full_name", None) or "會員"
    member_email = getattr(user, "email", None) or ""
    order_url = f"{config.APP_BASE_URL}/orders/{order.order_id}"

    subject = f"[訂票異常處理通知] 訂單 #{order.order_id}（{trip.league_type}）請選擇替代方案或全額退款"
    text_body = (
        f"{member_name} 您好：\n"
        f"專員 {agent_id}（{agent_name}）於代訂訂單 #{order.order_id} 時遇到以下狀況：\n"
        f"異常原因：{exc.reason}\n"
        f"專員建議替代方案：{exc.alternative_proposal}\n"
        f"請登入訂單頁面決定「接受替代方案繼續代訂」或「申請全額退款」：\n{order_url}"
    )
    body_html = (
        f"<p><strong>{escape(member_name)} 您好：</strong></p>"
        f"<p>承辦專員 <strong>{escape(agent_id)}（{escape(agent_name)}）</strong> 執行訂單 <strong>#{order.order_id}</strong> 實質代訂時回報以下異常狀況：</p>"
        "<div style=\"background:#fff1f2;border:1px solid #fecdd3;border-radius:10px;padding:12px 14px;margin:10px 0;\">"
        f"<div><strong>異常原因：</strong>{escape(exc.reason)}</div>"
        f"<div style=\"margin-top:6px;\"><strong>專員替代方案：</strong>{escape(exc.alternative_proposal)}</div>"
        "</div>"
        "<p>請立即前往系統訂單頁面選擇【接受替代方案（繼續代訂）】或【不接受，立即全額模擬退款】。</p>"
    )
    html_body = _wrap_html_card(
        title=f"訂票異常通知 · 請確認處理方式 (#{order.order_id})",
        subtitle=f"異常單 #{exc.exception_id} · 待會員抉擇",
        body_html=body_html,
        action_url=order_url,
        action_label="立即前往決定替代方案或退款",
    )
    return send_notification_email(
        to_email=member_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        category="EXCEPTION_ALERT",
        order_id=order.order_id,
    )


def send_exception_resolved_emails(
    order: Any,
    trip: Any,
    user: Any,
    agent: Any,
    exc: Any,
    refund_txn: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Send resolution emails to member & agent after ALT acceptance or REFUND completion."""
    agent_id = getattr(agent, "agent_id", None) or getattr(order, "agent_id", None) or "AG001"
    agent_name = getattr(agent, "agent_name", None) or "訂票專員"
    agent_email = getattr(agent, "email", None) or ""
    member_name = getattr(user, "full_name", None) or "會員"
    member_email = getattr(user, "email", None) or ""
    order_url = f"{config.APP_BASE_URL}/orders/{order.order_id}"

    if exc.user_decision == "ALT":
        member_res = send_notification_email(
            to_email=member_email,
            subject=f"[異常處理確認] 訂單 #{order.order_id} 已接受替代方案並恢復代訂",
            html_body=_wrap_html_card(
                title=f"已接受替代方案 · 訂單 #{order.order_id}",
                subtitle="專員將依替代方案繼續代訂並核發憑證",
                body_html=(
                    f"<p><strong>{escape(member_name)} 您好：</strong></p>"
                    f"<p>您已確認接受訂單 <strong>#{order.order_id}</strong> 之替代方案：</p>"
                    f"<p style=\"background:#f8fafc;border:1px solid #e2e8f0;padding:10px 12px;border-radius:8px;\">{escape(exc.alternative_proposal or '')}</p>"
                    f"<p>訂單已恢復為 <strong>PAID</strong>，專員 {escape(agent_id)} 將盡速為您核發類 Agoda 訂票憑證。</p>"
                ),
                action_url=order_url,
                action_label="檢視訂單進度",
            ),
            text_body=(
                f"{member_name} 您好：您已選擇接受訂單 #{order.order_id} 之替代方案，"
                f"系統已恢復訂單為 PAID 並通知專員 {agent_id} 繼續代訂。\n查看：{order_url}"
            ),
            category="EXCEPTION_ALT_ACCEPTED",
            order_id=order.order_id,
        )
        agent_res = send_notification_email(
            to_email=agent_email,
            subject=f"[會員異常處理指示] 訂單 #{order.order_id} 已同意替代方案（ALT），請繼續核發憑證",
            html_body=_wrap_html_card(
                title=f"會員已接受替代方案 · 訂單 #{order.order_id}",
                subtitle=f"異常單 #{exc.exception_id} · 指示代碼 ALT",
                body_html=(
                    f"<p><strong>{escape(agent_name)}（{escape(agent_id)}）您好：</strong></p>"
                    f"<p>會員 <strong>{escape(member_name)}</strong> 已於系統確認接受您提出的替代方案（<code>{escape(exc.alternative_proposal or '')}</code>）。</p>"
                    "<p>訂單狀態已恢復為 <strong>PAID</strong>，請依核准方案執行代訂並核發類 Agoda 訂票憑證。</p>"
                ),
                action_url=order_url,
                action_label="前往核發訂票憑證",
            ),
            text_body=(
                f"{agent_name}（{agent_id}）您好：會員 {member_name} 已接受訂單 #{order.order_id} 之替代方案，"
                f"請繼續代訂並核發憑證。\n訂單頁：{order_url}"
            ),
            category="EXCEPTION_USER_DIRECTIVE",
            order_id=order.order_id,
        )
        return member_res, agent_res

    amount_str = f"NT$ {refund_txn.amount:,}" if refund_txn else f"NT$ {order.quoted_price or 0:,}"
    ref_code = getattr(refund_txn, "gateway_ref", None) or "MOCK-REF"
    member_res = send_notification_email(
        to_email=member_email,
        subject=f"[全額退款完成通知] 訂單 #{order.order_id} 已退還 {amount_str}",
        html_body=_wrap_html_card(
            title=f"全額退款已完成 · 訂單 #{order.order_id}",
            subtitle=f"退款參考號：{ref_code}",
            body_html=(
                f"<p><strong>{escape(member_name)} 您好：</strong></p>"
                f"<p>我們已依您的異常處理決定，透過第三方金流完成訂單 <strong>#{order.order_id}</strong> 的全額模擬退款：</p>"
                "<ul>"
                f"<li>退款金額：<strong>{amount_str}</strong></li>"
                f"<li>退款參考號：<code>{escape(ref_code)}</code></li>"
                "<li>訂單狀態：REFUNDED（關聯主行程亦已關閉）</li>"
                "</ul>"
            ),
            action_url=order_url,
            action_label="檢視 D4 退款交易紀錄",
        ),
        text_body=(
            f"{member_name} 您好：訂單 #{order.order_id} 已完成全額退款 {amount_str}"
            f"（退款參考號 {ref_code}）。\n查看交易明細：{order_url}"
        ),
        category="REFUND_SUCCESS",
        order_id=order.order_id,
    )
    agent_res = send_notification_email(
        to_email=agent_email,
        subject=f"[異常結案 · 會員選擇全額退款] 訂單 #{order.order_id} 已轉為 REFUNDED",
        html_body=_wrap_html_card(
            title=f"會員申請全額退款結案 · #{order.order_id}",
            subtitle=f"異常單 #{exc.exception_id} · 指示代碼 REFUND",
            body_html=(
                f"<p><strong>{escape(agent_name)}（{escape(agent_id)}）您好：</strong></p>"
                f"<p>會員 {escape(member_name)} 針對異常單 #{exc.exception_id} 選擇申請全額退款（{amount_str}），"
                f"金流退款交易 <code>{escape(ref_code)}</code> 已完成，本訂單已結案。</p>"
            ),
            action_url=order_url,
            action_label="檢視結案訂單",
        ),
        text_body=(
            f"{agent_name}（{agent_id}）您好：會員 {member_name} 已選擇針對訂單 #{order.order_id} 全額退款，"
            f"交易 {ref_code} 完成，無需再進行代訂。"
        ),
        category="EXCEPTION_REFUND_CLOSED",
        order_id=order.order_id,
    )
    return member_res, agent_res


def send_support_refund_decision_email(
    order: Any,
    trip: Any,
    user: Any,
    agent: Any,
    *,
    approved: bool,
    refund_txn: Any = None,
    note: str = "",
) -> dict[str, Any]:
    """Notify member when live support approves or rejects a refund request."""
    agent_id = getattr(agent, "agent_id", None) or getattr(order, "agent_id", None) or "AG001"
    agent_name = getattr(agent, "agent_name", None) or "訂票專員"
    member_name = getattr(user, "full_name", None) or "會員"
    member_email = getattr(user, "email", None) or ""
    order_url = f"{config.APP_BASE_URL}/orders/{order.order_id}"
    support_url = f"{config.APP_BASE_URL}/support"

    if approved:
        amount_str = (
            f"NT$ {refund_txn.amount:,}"
            if refund_txn is not None
            else f"NT$ {order.quoted_price or 0:,}"
        )
        ref_code = getattr(refund_txn, "gateway_ref", None) or "MOCK-REF"
        note_html = (
            f"<p><strong>專員審核備註：</strong>{escape(note)}</p>" if note else ""
        )
        return send_notification_email(
            to_email=member_email,
            subject=f"[客服退款核准通知] 訂單 #{order.order_id} 已退款 {amount_str}",
            html_body=_wrap_html_card(
                title=f"客服退款申請已核准 · 訂單 #{order.order_id}",
                subtitle=f"承辦專員：{agent_id} {agent_name} · 退款編號 {ref_code}",
                body_html=(
                    f"<p><strong>{escape(member_name)} 您好：</strong></p>"
                    f"<p>您在真人客服提出之訂單 <strong>#{order.order_id}</strong>（{escape(trip.league_type)}）退款申請已獲專員審核通過並完成模擬入帳：</p>"
                    "<ul>"
                    f"<li>核准退款金額：<strong>{amount_str}</strong></li>"
                    f"<li>金流退款參考號：<code>{escape(ref_code)}</code></li>"
                    "<li>訂單狀態：REFUNDED</li>"
                    "</ul>"
                    f"{note_html}"
                ),
                action_url=order_url,
                action_label="檢視訂單與退款交易紀錄",
            ),
            text_body=(
                f"{member_name} 您好：客服專員 {agent_id} 已核准訂單 #{order.order_id} 退款 {amount_str}"
                f"（參考號 {ref_code}）。\n訂單明細：{order_url}"
            ),
            category="SUPPORT_REFUND_APPROVED",
            order_id=order.order_id,
        )

    return send_notification_email(
        to_email=member_email,
        subject=f"[客服退款審核通知] 訂單 #{order.order_id} 退款申請未通過",
        html_body=_wrap_html_card(
            title=f"客服退款申請說明 · 訂單 #{order.order_id}",
            subtitle=f"承辦專員：{agent_id} {agent_name}",
            body_html=(
                f"<p><strong>{escape(member_name)} 您好：</strong></p>"
                f"<p>客服專員 <strong>{escape(agent_id)}（{escape(agent_name)}）</strong> 已審閱您針對訂單 <strong>#{order.order_id}</strong> 的退款申請，目前暫無法核准退款，訂單維持有效狀態。</p>"
                f"<p style=\"background:#fff1f2;border:1px solid #fecdd3;border-radius:8px;padding:10px 12px;\"><strong>專員說明：</strong>{escape(note)}</p>"
            ),
            action_url=support_url,
            action_label="返回客服對話確認細節",
        ),
        text_body=(
            f"{member_name} 您好：客服專員 {agent_id} 回覆訂單 #{order.order_id} 退款申請未通過，原因：{note}\n"
            f"客服中心：{support_url}"
        ),
        category="SUPPORT_REFUND_REJECTED",
        order_id=order.order_id,
    )

