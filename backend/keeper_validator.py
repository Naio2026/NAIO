from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from dotenv import load_dotenv
from eth_account import Account
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def _log_level() -> int:
    s = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, s, logging.INFO)

logging.basicConfig(level=_log_level(), format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("keeper_validator")

DEPOSIT_TOPIC = Web3.keccak(text="DepositFromTransfer(address,uint256,bytes32)").hex()
REFUND_TOPIC = Web3.keccak(text="DepositRefunded(address,uint256,bytes32,uint8)").hex()
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()

if not DEPOSIT_TOPIC.startswith("0x"):
    DEPOSIT_TOPIC = "0x" + DEPOSIT_TOPIC
if not REFUND_TOPIC.startswith("0x"):
    REFUND_TOPIC = "0x" + REFUND_TOPIC
if not TRANSFER_TOPIC.startswith("0x"):
    TRANSFER_TOPIC = "0x" + TRANSFER_TOPIC

CONTROLLER_ABI = [
    {
        "inputs": [],
        "name": "keeperAccountingPaused",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "validatorVetoPause",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

SUPPORTED_LANGS = {"zh-CN", "zh-TW", "en-US", "ko", "ja", "th"}

I18N = {
    "zh-CN": {
        "anomaly_title": "🚨 Keeper 验证异常",
        "block": "区块",
        "event": "事件",
        "user": "用户",
        "amount": "金额",
        "event_tx_hash": "事件txHash",
        "tx": "交易",
        "reason": "原因",
        "reason_no_match": "事件txHash对应交易中未找到匹配的 USDT Transfer(from=user,to=controller,amount)",
        "event_deposit": "DepositFromTransfer",
        "event_refund": "DepositRefunded",
        "veto_attempt_title": "🛑 验证者否决尝试",
        "ok": "成功",
        "detail": "详情",
        "veto_failed_title": "❌ 验证者否决失败",
        "lang_usage": "用法: /lang <zh-CN|zh-TW|en-US|ko|ja|th>\n当前: {current}",
        "lang_set_ok": "已将本聊天语言设置为 {lang}",
        "lang_set_invalid": "不支持的语言: {lang}\n支持: zh-CN, zh-TW, en-US, ko, ja, th",
        "lang_menu_title": "🌐 语言设置\n当前: {current}\n点击按钮切换",
        "lang_need_mention": "群聊请使用 /lang@{bot} 才会生效",
        "gas_low_title": "⛽️ 手续费余额告警",
        "wallet": "钱包",
        "balance": "当前余额",
        "threshold": "告警阈值",
        "gas_hint": "该地址余额低于阈值，可能导致 keeper/validator 交易失败。",
    },
    "zh-TW": {
        "anomaly_title": "🚨 Keeper 驗證異常",
        "block": "區塊",
        "event": "事件",
        "user": "使用者",
        "amount": "金額",
        "event_tx_hash": "事件txHash",
        "tx": "交易",
        "reason": "原因",
        "reason_no_match": "事件txHash對應交易中未找到匹配的 USDT Transfer(from=user,to=controller,amount)",
        "event_deposit": "DepositFromTransfer",
        "event_refund": "DepositRefunded",
        "veto_attempt_title": "🛑 驗證者否決嘗試",
        "ok": "成功",
        "detail": "詳情",
        "veto_failed_title": "❌ 驗證者否決失敗",
        "lang_usage": "用法: /lang <zh-CN|zh-TW|en-US|ko|ja|th>\n目前: {current}",
        "lang_set_ok": "已將本聊天語言設定為 {lang}",
        "lang_set_invalid": "不支援的語言: {lang}\n支援: zh-CN, zh-TW, en-US, ko, ja, th",
        "lang_menu_title": "🌐 語言設定\n目前: {current}\n點擊按鈕切換",
        "lang_need_mention": "群組請使用 /lang@{bot} 才會生效",
        "gas_low_title": "⛽️ 手續費餘額告警",
        "wallet": "錢包",
        "balance": "目前餘額",
        "threshold": "告警閾值",
        "gas_hint": "該地址餘額低於閾值，可能導致 keeper/validator 交易失敗。",
    },
    "en-US": {
        "anomaly_title": "🚨 Keeper validator anomaly",
        "block": "block",
        "event": "event",
        "user": "user",
        "amount": "amount",
        "event_tx_hash": "eventTxHash",
        "tx": "tx",
        "reason": "reason",
        "reason_no_match": "no matching USDT Transfer(from=user,to=controller,amount) in event txHash receipt",
        "event_deposit": "DepositFromTransfer",
        "event_refund": "DepositRefunded",
        "veto_attempt_title": "🛑 validator veto attempt",
        "ok": "ok",
        "detail": "detail",
        "veto_failed_title": "❌ validator veto failed",
        "lang_usage": "Usage: /lang <zh-CN|zh-TW|en-US|ko|ja|th>\nCurrent: {current}",
        "lang_set_ok": "Language updated to {lang} for this chat.",
        "lang_set_invalid": "Unsupported language: {lang}\nSupported: zh-CN, zh-TW, en-US, ko, ja, th",
        "lang_menu_title": "🌐 Language settings\nCurrent: {current}\nTap a button to change",
        "lang_need_mention": "In groups, use /lang@{bot} to apply changes",
        "gas_low_title": "⛽️ Low gas balance alert",
        "wallet": "wallet",
        "balance": "current balance",
        "threshold": "alert threshold",
        "gas_hint": "This wallet is below threshold and may fail keeper/validator transactions.",
    },
    "ko": {
        "anomaly_title": "🚨 Keeper 검증 이상",
        "block": "블록",
        "event": "이벤트",
        "user": "사용자",
        "amount": "금액",
        "event_tx_hash": "이벤트 txHash",
        "tx": "트랜잭션",
        "reason": "원인",
        "reason_no_match": "event txHash 영수증에서 일치하는 USDT Transfer(from=user,to=controller,amount)를 찾지 못함",
        "event_deposit": "DepositFromTransfer",
        "event_refund": "DepositRefunded",
        "veto_attempt_title": "🛑 검증자 거부 시도",
        "ok": "성공",
        "detail": "상세",
        "veto_failed_title": "❌ 검증자 거부 실패",
        "lang_usage": "사용법: /lang <zh-CN|zh-TW|en-US|ko|ja|th>\n현재: {current}",
        "lang_set_ok": "이 채팅의 언어를 {lang}(으)로 설정했습니다.",
        "lang_set_invalid": "지원하지 않는 언어: {lang}\n지원: zh-CN, zh-TW, en-US, ko, ja, th",
        "lang_menu_title": "🌐 언어 설정\n현재: {current}\n버튼으로 변경",
        "lang_need_mention": "그룹에서는 /lang@{bot} 형식으로 사용하세요",
        "gas_low_title": "⛽️ 가스 잔액 경고",
        "wallet": "지갑",
        "balance": "현재 잔액",
        "threshold": "경고 임계값",
        "gas_hint": "해당 지갑 잔액이 임계값보다 낮아 keeper/validator 거래가 실패할 수 있습니다.",
    },
    "ja": {
        "anomaly_title": "🚨 Keeper 検証異常",
        "block": "ブロック",
        "event": "イベント",
        "user": "ユーザー",
        "amount": "数量",
        "event_tx_hash": "イベント txHash",
        "tx": "トランザクション",
        "reason": "理由",
        "reason_no_match": "event txHash のレシート内に一致する USDT Transfer(from=user,to=controller,amount) がありません",
        "event_deposit": "DepositFromTransfer",
        "event_refund": "DepositRefunded",
        "veto_attempt_title": "🛑 バリデータ veto 試行",
        "ok": "成功",
        "detail": "詳細",
        "veto_failed_title": "❌ バリデータ veto 失敗",
        "lang_usage": "使い方: /lang <zh-CN|zh-TW|en-US|ko|ja|th>\n現在: {current}",
        "lang_set_ok": "このチャットの言語を {lang} に設定しました。",
        "lang_set_invalid": "未対応の言語です: {lang}\n対応: zh-CN, zh-TW, en-US, ko, ja, th",
        "lang_menu_title": "🌐 言語設定\n現在: {current}\nボタンで変更",
        "lang_need_mention": "グループでは /lang@{bot} で実行してください",
        "gas_low_title": "⛽️ ガス残高アラート",
        "wallet": "ウォレット",
        "balance": "現在残高",
        "threshold": "アラート閾値",
        "gas_hint": "このウォレットは閾値未満のため、keeper/validator の取引が失敗する可能性があります。",
    },
    "th": {
        "anomaly_title": "🚨 ความผิดปกติของ Keeper validator",
        "block": "บล็อก",
        "event": "อีเวนต์",
        "user": "ผู้ใช้",
        "amount": "จำนวน",
        "event_tx_hash": "eventTxHash",
        "tx": "ธุรกรรม",
        "reason": "สาเหตุ",
        "reason_no_match": "ไม่พบ USDT Transfer(from=user,to=controller,amount) ที่ตรงกันใน receipt ของ event txHash",
        "event_deposit": "DepositFromTransfer",
        "event_refund": "DepositRefunded",
        "veto_attempt_title": "🛑 พยายาม veto โดย validator",
        "ok": "ผลลัพธ์",
        "detail": "รายละเอียด",
        "veto_failed_title": "❌ validator veto ล้มเหลว",
        "lang_usage": "วิธีใช้: /lang <zh-CN|zh-TW|en-US|ko|ja|th>\nปัจจุบัน: {current}",
        "lang_set_ok": "ตั้งค่าภาษาของแชตนี้เป็น {lang} แล้ว",
        "lang_set_invalid": "ไม่รองรับภาษา: {lang}\nรองรับ: zh-CN, zh-TW, en-US, ko, ja, th",
        "lang_menu_title": "🌐 ตั้งค่าภาษา\nปัจจุบัน: {current}\nแตะปุ่มเพื่อเปลี่ยน",
        "lang_need_mention": "ในกลุ่มให้ใช้ /lang@{bot} เพื่อให้มีผล",
        "gas_low_title": "⛽️ แจ้งเตือนยอดแก๊สต่ำ",
        "wallet": "กระเป๋า",
        "balance": "ยอดปัจจุบัน",
        "threshold": "เกณฑ์แจ้งเตือน",
        "gas_hint": "ยอดต่ำกว่าเกณฑ์ อาจทำให้ธุรกรรม keeper/validator ล้มเหลว",
    },
}

def _topic_addr(topic_hex: str) -> str:
    return Web3.to_checksum_address("0x" + topic_hex[-40:])

def _load_state(path: str) -> dict:
    default_state = {
        "last_block": 0,
        "telegram_update_offset": 0,
        "telegram_chat_lang_map": {},
        "gas_alert_last_day": {},
    }
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
            if not isinstance(raw, dict):
                return default_state
            st = dict(default_state)
            st.update(raw)
            if not isinstance(st.get("telegram_chat_lang_map"), dict):
                st["telegram_chat_lang_map"] = {}
            if not isinstance(st.get("gas_alert_last_day"), dict):
                st["gas_alert_last_day"] = {}
            st["telegram_update_offset"] = int(st.get("telegram_update_offset") or 0)
            st["last_block"] = int(st.get("last_block") or 0)
            return st
    except FileNotFoundError:
        return default_state
    except Exception:
        return default_state

def _save_state(path: str, st: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)

def _send_telegram(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text})
    data = payload.encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10):
        return

def _send_telegram_with_markup(token: str, chat_id: str, text: str, reply_markup: dict) -> None:
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": json.dumps(reply_markup, ensure_ascii=False),
        }
    )
    req = urllib.request.Request(url, data=payload.encode("utf-8"), method="POST")
    with urllib.request.urlopen(req, timeout=10):
        return

def _parse_telegram_chat_ids() -> list[str]:
    raw = os.getenv("VALIDATOR_TELEGRAM_CHAT_IDS", "").strip()
    if raw:

        try:
            val = json.loads(raw)
            if isinstance(val, list):
                out = [str(x).strip() for x in val if str(x).strip()]
                if out:
                    return out
        except Exception:
            pass
        out = [x.strip() for x in raw.replace("\n", ",").split(",") if x.strip()]
        if out:
            return out
    single = os.getenv("VALIDATOR_TELEGRAM_CHAT_ID", "").strip()
    return [single] if single else []

def _parse_gas_watch_addresses() -> list[str]:
    raw = os.getenv("VALIDATOR_GAS_WATCH_ADDRESSES", "").strip()
    if not raw:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def _push_addr(v) -> None:
        s = str(v).strip()
        if not s:
            return
        try:
            addr = Web3.to_checksum_address(s)
        except Exception:
            logger.warning("ignore invalid VALIDATOR_GAS_WATCH_ADDRESSES item: %s", s)
            return
        low = addr.lower()
        if low in seen:
            return
        seen.add(low)
        out.append(addr)

    try:
        val = json.loads(raw)
        if isinstance(val, list):
            for x in val:
                _push_addr(x)
            return out
    except Exception:
        pass

    for x in raw.replace("\n", ",").split(","):
        _push_addr(x)
    return out

def _parse_witness_signer_addresses() -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def _push_addr(v, source: str) -> None:
        s = str(v).strip()
        if not s:
            return
        try:
            addr = Web3.to_checksum_address(s)
        except Exception:
            logger.warning("ignore invalid %s item: %s", source, s)
            return
        low = addr.lower()
        if low in seen:
            return
        seen.add(low)
        out.append(addr)

    for i in range(1, 4):
        _push_addr(os.getenv(f"WITNESS_SIGNER_{i}", "").strip(), f"WITNESS_SIGNER_{i}")

    raw = os.getenv("WITNESS_SIGNER_ADDRESSES", "").strip()
    if raw:
        try:
            val = json.loads(raw)
            if isinstance(val, list):
                for x in val:
                    _push_addr(x, "WITNESS_SIGNER_ADDRESSES")
            else:
                for x in raw.replace("\n", ",").split(","):
                    _push_addr(x, "WITNESS_SIGNER_ADDRESSES")
        except Exception:
            for x in raw.replace("\n", ",").split(","):
                _push_addr(x, "WITNESS_SIGNER_ADDRESSES")

    return out

def _normalize_lang(lang: str) -> str:
    if not lang:
        return "zh-CN"
    l = lang.strip()
    if l in SUPPORTED_LANGS:
        return l
    lower = l.lower()
    if lower in ("zh-tw", "zhtw", "zh_hk", "zh-hk"):
        return "zh-TW"
    if lower.startswith("zh"):
        return "zh-CN"
    if lower.startswith("en"):
        return "en-US"
    if lower.startswith("ko"):
        return "ko"
    if lower.startswith("ja"):
        return "ja"
    if lower.startswith("th"):
        return "th"
    return "zh-CN"

def _parse_chat_lang_map() -> dict[str, str]:
    raw = os.getenv("VALIDATOR_TELEGRAM_CHAT_LANG_MAP", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in data.items():
            chat_id = str(k).strip()
            if not chat_id:
                continue
            out[chat_id] = _normalize_lang(str(v))
        return out
    except Exception:
        return {}

def _t(lang: str, key: str) -> str:
    l = _normalize_lang(lang)
    if key in I18N.get(l, {}):
        return I18N[l][key]
    return I18N["zh-CN"].get(key, key)

def _format_anomaly_msg(
    lang: str,
    block_number: int,
    is_deposit: bool,
    user: str,
    amount: int,
    event_tx_hash: str,
    tx_hash: str,
) -> str:
    event_name = _t(lang, "event_deposit") if is_deposit else _t(lang, "event_refund")
    return (
        f"{_t(lang, 'anomaly_title')}\n"
        f"{_t(lang, 'block')}: {block_number}\n"
        f"{_t(lang, 'event')}: {event_name}\n"
        f"{_t(lang, 'user')}: {user}\n"
        f"{_t(lang, 'amount')}: {amount}\n"
        f"{_t(lang, 'event_tx_hash')}: {event_tx_hash}\n"
        f"{_t(lang, 'tx')}: {tx_hash}\n"
        f"{_t(lang, 'reason')}: {_t(lang, 'reason_no_match')}"
    )

def _format_veto_attempt_msg(lang: str, ok_veto: bool, detail: str) -> str:
    return f"{_t(lang, 'veto_attempt_title')}\n{_t(lang, 'ok')}={ok_veto}\n{_t(lang, 'detail')}={detail}"

def _format_veto_failed_msg(lang: str, err_msg: str) -> str:
    return f"{_t(lang, 'veto_failed_title')}: {err_msg}"

def _fmt_bnb(wei: int) -> str:
    return f"{wei / 1e18:.6f} BNB"

def _format_gas_low_msg(lang: str, wallet: str, balance_wei: int, threshold_wei: int) -> str:
    return (
        f"{_t(lang, 'gas_low_title')}\n"
        f"{_t(lang, 'wallet')}: {wallet}\n"
        f"{_t(lang, 'balance')}: {_fmt_bnb(balance_wei)}\n"
        f"{_t(lang, 'threshold')}: {_fmt_bnb(threshold_wei)}\n"
        f"{_t(lang, 'gas_hint')}"
    )

def _today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())

def _poll_gas_balance_alerts(
    cfg,
    w3: Web3,
    state: dict,
    runtime_chat_lang_map: dict[str, str],
) -> bool:
    if not cfg.telegram_token or not cfg.telegram_chat_ids or not cfg.gas_watch_addresses:
        return False
    changed = False
    day = _today_utc()
    sent_map = state.get("gas_alert_last_day") or {}
    if not isinstance(sent_map, dict):
        sent_map = {}

    for addr in cfg.gas_watch_addresses:
        key = addr.lower()
        try:
            bal = int(w3.eth.get_balance(addr))
        except Exception as e:
            logger.debug("check gas balance failed addr=%s err=%s", addr, e)
            continue

        if bal < cfg.gas_alert_threshold_wei:
            if sent_map.get(key) == day:
                continue
            try:
                _send_telegram_many_localized(
                    cfg.telegram_token,
                    cfg.telegram_chat_ids,
                    cfg.telegram_default_lang,
                    runtime_chat_lang_map,
                    lambda lang: _format_gas_low_msg(lang, addr, bal, cfg.gas_alert_threshold_wei),
                )
            except Exception as e:
                logger.debug("send gas low alert failed addr=%s err=%s", addr, e)
            sent_map[key] = day
            changed = True
        else:

            if key in sent_map:
                sent_map.pop(key, None)
                changed = True

    if changed:
        state["gas_alert_last_day"] = sent_map
    return changed

def _telegram_api_json(token: str, method: str, params: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    payload = urllib.parse.urlencode(params)
    data = payload.encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
    obj = json.loads(raw)
    if isinstance(obj, dict):
        return obj
    return {"ok": False, "result": []}

def _cmd_lang_from_text(text: str) -> tuple[str, str] | None:
    if not text:
        return None
    s = text.strip()
    m = re.search(r"/lang(?:@([A-Za-z0-9_]+))?(?:\s+([^\s]+))?", s, flags=re.IGNORECASE)
    if not m:
        return None
    target = (m.group(1) or "").strip()
    arg = (m.group(2) or "").strip()
    return (arg, target)

def _lang_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "简体中文", "callback_data": "lang:set:zh-CN"},
                {"text": "繁體中文", "callback_data": "lang:set:zh-TW"},
            ],
            [
                {"text": "English", "callback_data": "lang:set:en-US"},
                {"text": "한국어", "callback_data": "lang:set:ko"},
            ],
            [
                {"text": "日本語", "callback_data": "lang:set:ja"},
                {"text": "ไทย", "callback_data": "lang:set:th"},
            ],
        ]
    }

def _fetch_bot_username(token: str) -> str:
    try:
        resp = _telegram_api_json(token, "getMe", {})
        if not resp.get("ok"):
            return ""
        result = resp.get("result") or {}
        return str(result.get("username") or "").strip()
    except Exception:
        return ""

def _poll_lang_commands(
    cfg,
    state: dict,
    runtime_chat_lang_map: dict[str, str],
    bot_username: str,
) -> bool:
    if not cfg.telegram_enable_commands or not cfg.telegram_token:
        return False
    changed = False
    offset = int(state.get("telegram_update_offset") or 0)
    try:
        resp = _telegram_api_json(
            cfg.telegram_token,
            "getUpdates",
            {"offset": offset, "timeout": 0, "allowed_updates": json.dumps(["message", "callback_query"])},
        )
        if not resp.get("ok"):
            return False
        for upd in resp.get("result") or []:
            if not isinstance(upd, dict):
                continue
            upd_id = int(upd.get("update_id") or 0)
            if upd_id >= offset:
                offset = upd_id + 1

            cb = upd.get("callback_query") or {}
            cb_id = str(cb.get("id") or "").strip()
            cb_data = str(cb.get("data") or "").strip()
            cb_msg = cb.get("message") or {}
            cb_chat = cb_msg.get("chat") or {}
            cb_chat_id = str(cb_chat.get("id") or "").strip()
            if cb_id and cb_chat_id and cb_data.startswith("lang:set:"):
                picked = cb_data.split("lang:set:", 1)[1].strip()
                current_lang = runtime_chat_lang_map.get(cb_chat_id, cfg.telegram_default_lang)
                accepted = picked in SUPPORTED_LANGS or picked.lower() in ("zh", "en", "ko", "ja", "th")
                if accepted:
                    new_lang = _normalize_lang(picked)
                    runtime_chat_lang_map[cb_chat_id] = new_lang
                    changed = True
                    _send_telegram_with_markup(
                        cfg.telegram_token,
                        cb_chat_id,
                        _t(new_lang, "lang_set_ok").format(lang=new_lang) + "\n" + _t(new_lang, "lang_menu_title").format(current=new_lang),
                        _lang_keyboard(),
                    )
                    _telegram_api_json(cfg.telegram_token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "OK"})
                else:
                    _send_telegram(
                        cfg.telegram_token,
                        cb_chat_id,
                        _t(current_lang, "lang_set_invalid").format(lang=picked),
                    )
                    _telegram_api_json(cfg.telegram_token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": "Invalid"})
                continue

            msg = upd.get("message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id") or "").strip()
            text = str(msg.get("text") or "").strip()
            chat_type = str(chat.get("type") or "").strip().lower()
            if not chat_id:
                continue

            parsed = _cmd_lang_from_text(text)
            if parsed is None:
                continue
            lang_arg, target = parsed
            current_lang = runtime_chat_lang_map.get(chat_id, cfg.telegram_default_lang)

            if chat_type in ("group", "supergroup"):
                if not bot_username:
                    continue
                mentioned = f"@{bot_username.lower()}" in text.lower()
                if target:
                    if target.lower() != bot_username.lower():
                        continue
                elif not mentioned:
                    continue

            if lang_arg == "":
                _send_telegram_with_markup(
                    cfg.telegram_token,
                    chat_id,
                    _t(current_lang, "lang_usage").format(current=current_lang) + "\n" + _t(current_lang, "lang_menu_title").format(current=current_lang),
                    _lang_keyboard(),
                )
                continue

            accepted = lang_arg in SUPPORTED_LANGS or lang_arg.lower() in ("zh", "zh-cn", "zh-tw", "en", "en-us", "ko", "ja", "th")
            if not accepted:
                _send_telegram(
                    cfg.telegram_token,
                    chat_id,
                    _t(current_lang, "lang_set_invalid").format(lang=lang_arg),
                )
                continue

            new_lang = _normalize_lang(lang_arg)
            runtime_chat_lang_map[chat_id] = new_lang
            _send_telegram_with_markup(
                cfg.telegram_token,
                chat_id,
                _t(new_lang, "lang_set_ok").format(lang=new_lang) + "\n" + _t(new_lang, "lang_menu_title").format(current=new_lang),
                _lang_keyboard(),
            )
            changed = True

        if offset != int(state.get("telegram_update_offset") or 0):
            state["telegram_update_offset"] = offset
            changed = True
    except Exception as e:
        logger.debug("poll /lang commands failed: %s", e)
    return changed

def _send_telegram_many_localized(
    token: str,
    chat_ids: list[str],
    default_lang: str,
    chat_lang_map: dict[str, str],
    text_builder,
) -> None:
    if not token or not chat_ids:
        return
    for chat_id in chat_ids:
        lang = chat_lang_map.get(chat_id, default_lang)
        _send_telegram(token, chat_id, text_builder(lang))

@dataclass
class Cfg:
    rpc_url: str
    controller: str
    usdt: str
    validator_pk: str
    confirmations: int
    poll_interval: int
    state_file: str
    veto_enabled: bool
    telegram_token: str
    telegram_chat_ids: list[str]
    telegram_default_lang: str
    telegram_chat_lang_map: dict[str, str]
    telegram_enable_commands: bool
    telegram_bot_username: str
    gas_watch_addresses: list[str]
    gas_alert_threshold_wei: int
    gas_check_interval: int

def _pick_rpc_url() -> str:
    use_quicknode = _env_bool("USE_QUICKNODE", False)
    if use_quicknode:
        url = os.getenv("QUICKNODE_HTTP_URL", "").strip()
        if url:
            return url
    return os.getenv("BSC_RPC_URL", "").strip() or "https://data-seed-prebsc-1-s1.binance.org:8545/"

def _load_cfg() -> Cfg:
    controller = os.getenv("CONTROLLER_ADDRESS", "").strip()
    usdt = os.getenv("USDT_ADDRESS", "").strip()
    validator_pk = os.getenv("VALIDATOR_PRIVATE_KEY", "").strip()
    keeper_pk = os.getenv("KEEPER_PRIVATE_KEY", "").strip()
    if validator_pk and not validator_pk.startswith("0x"):
        validator_pk = "0x" + validator_pk
    if keeper_pk and not keeper_pk.startswith("0x"):
        keeper_pk = "0x" + keeper_pk
    if not controller or not usdt:
        raise RuntimeError("Missing CONTROLLER_ADDRESS/USDT_ADDRESS")
    if not validator_pk:
        raise RuntimeError("Missing VALIDATOR_PRIVATE_KEY")

    gas_watch_addresses: list[str] = []
    try:
        gas_watch_addresses.append(Web3.to_checksum_address(Account.from_key(validator_pk).address))
    except Exception:
        pass
    if keeper_pk:
        try:
            gas_watch_addresses.append(Web3.to_checksum_address(Account.from_key(keeper_pk).address))
        except Exception:
            pass

    for i in range(1, 6):
        m = os.getenv(f"KEEPER_COUNCIL_MEMBER_{i}", "").strip()
        if not m:
            continue
        try:
            gas_watch_addresses.append(Web3.to_checksum_address(m))
        except Exception:
            logger.warning("ignore invalid KEEPER_COUNCIL_MEMBER_%s: %s", i, m)

    gas_watch_addresses.extend(_parse_witness_signer_addresses())

    gas_watch_addresses.extend(_parse_gas_watch_addresses())

    seen = set()
    gas_watch_addresses = [a for a in gas_watch_addresses if not (a in seen or seen.add(a))]

    threshold_bnb = float(os.getenv("VALIDATOR_GAS_ALERT_THRESHOLD_BNB", "0.01"))
    if threshold_bnb < 0:
        threshold_bnb = 0
    gas_alert_threshold_wei = int(threshold_bnb * (10**18))
    gas_check_interval = max(10, int(os.getenv("VALIDATOR_GAS_CHECK_INTERVAL", "300")))

    return Cfg(
        rpc_url=_pick_rpc_url(),
        controller=Web3.to_checksum_address(controller),
        usdt=Web3.to_checksum_address(usdt),
        validator_pk=validator_pk,
        confirmations=max(0, int(os.getenv("VALIDATOR_CONFIRMATIONS", "3"))),
        poll_interval=max(3, int(os.getenv("VALIDATOR_POLL_INTERVAL", "12"))),
        state_file=os.getenv("VALIDATOR_STATE_FILE", "keeper_validator_state.json").strip() or "keeper_validator_state.json",
        veto_enabled=_env_bool("VALIDATOR_VETO_ENABLED", True),
        telegram_token=os.getenv("VALIDATOR_TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_ids=_parse_telegram_chat_ids(),
        telegram_default_lang=_normalize_lang(os.getenv("VALIDATOR_TELEGRAM_LANG", "zh-CN")),
        telegram_chat_lang_map=_parse_chat_lang_map(),
        telegram_enable_commands=_env_bool("VALIDATOR_TELEGRAM_ENABLE_COMMANDS", True),
        telegram_bot_username=os.getenv("VALIDATOR_TELEGRAM_BOT_USERNAME", "").strip().lstrip("@"),
        gas_watch_addresses=gas_watch_addresses,
        gas_alert_threshold_wei=gas_alert_threshold_wei,
        gas_check_interval=gas_check_interval,
    )

def _decode_amount(data_hex: str) -> int:
    if isinstance(data_hex, bytes):
        if len(data_hex) == 0:
            return 0
        if len(data_hex) >= 32:
            return int.from_bytes(data_hex[:32], "big")
        return int.from_bytes(data_hex, "big")
    h = data_hex[2:] if isinstance(data_hex, str) and data_hex.startswith("0x") else str(data_hex)
    if len(h) == 0:
        return 0

    if len(h) > 64:
        h = h[:64]
    return int(h, 16)

def _has_matching_transfer_in_receipt(receipt, usdt: str, user: str, controller: str, amount: int) -> bool:
    user_t = user.lower()
    ctrl_t = controller.lower()
    for lg in receipt["logs"]:
        if str(lg.get("address", "")).lower() != usdt.lower():
            continue
        topics = lg.get("topics") or []
        if len(topics) < 3:
            continue
        t0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
        if not t0.startswith("0x"):
            t0 = "0x" + t0
        if t0.lower() != TRANSFER_TOPIC.lower():
            continue
        t1 = topics[1].hex() if hasattr(topics[1], "hex") else str(topics[1])
        t2 = topics[2].hex() if hasattr(topics[2], "hex") else str(topics[2])
        from_addr = _topic_addr(t1)
        to_addr = _topic_addr(t2)
        amt = _decode_amount(lg.get("data"))
        if from_addr.lower() == user_t and to_addr.lower() == ctrl_t and amt == amount:
            return True
    return False

def _norm_hex32(v) -> str:
    if v is None:
        return ""
    s = v.hex() if hasattr(v, "hex") else str(v)
    if not isinstance(s, str):
        s = str(s)
    if not s.startswith("0x"):
        s = "0x" + s
    return s.lower()

def _get_receipt_with_retry(w3: Web3, tx_hash: str, retries: int = 3, delay_s: float = 0.5):
    if not tx_hash:
        return None
    for i in range(max(1, retries)):
        try:
            return w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            if i + 1 < retries:
                time.sleep(delay_s)
    return None

def _build_controller(w3: Web3, controller: str):
    return w3.eth.contract(address=controller, abi=CONTROLLER_ABI)

def _attempt_veto(w3: Web3, controller_contract, validator) -> tuple[bool, str]:
    paused = bool(controller_contract.functions.keeperAccountingPaused().call())
    if paused:
        return True, "already_paused"

    fn = controller_contract.functions.validatorVetoPause()
    nonce = w3.eth.get_transaction_count(validator.address)
    try:
        gas_est = fn.estimate_gas({"from": validator.address})
        gas = int(gas_est * 12 // 10)
    except Exception:
        gas = 200000
    tx = fn.build_transaction(
        {
            "from": validator.address,
            "nonce": nonce,
            "gas": gas,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = validator.sign_transaction(tx)
    raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
    if raw is None:
        return False, "raw_tx_missing"
    sent = w3.eth.send_raw_transaction(raw)
    tx_hash = sent.hex()
    try:
        receipt = w3.eth.wait_for_transaction_receipt(sent, timeout=120)
    except Exception as e:
        return False, f"{tx_hash}:receipt_timeout:{e}"
    status = getattr(receipt, "status", None)
    if status is None and isinstance(receipt, dict):
        status = receipt.get("status")
    if int(status or 0) != 1:
        return False, f"{tx_hash}:status=0"
    return True, tx_hash

def main() -> int:
    load_dotenv(override=True)
    cfg = _load_cfg()

    w3 = Web3(Web3.HTTPProvider(cfg.rpc_url))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        raise RuntimeError(f"RPC not connected: {cfg.rpc_url}")

    validator = Account.from_key(cfg.validator_pk)
    controller_contract = _build_controller(w3, cfg.controller)
    state = _load_state(cfg.state_file)
    last_block = int(state.get("last_block") or 0)
    runtime_chat_lang_map = dict(cfg.telegram_chat_lang_map)
    persisted_map = state.get("telegram_chat_lang_map") or {}
    if isinstance(persisted_map, dict):
        for chat_id, lang in persisted_map.items():
            runtime_chat_lang_map[str(chat_id)] = _normalize_lang(str(lang))
    bot_username = cfg.telegram_bot_username or _fetch_bot_username(cfg.telegram_token)
    if bot_username:
        logger.info("validator telegram bot username: @%s", bot_username)
    logger.info(
        "gas monitor enabled. threshold=%s wallets=%s interval=%ss",
        _fmt_bnb(cfg.gas_alert_threshold_wei),
        ",".join(cfg.gas_watch_addresses) if cfg.gas_watch_addresses else "-",
        cfg.gas_check_interval,
    )
    next_gas_check_at = 0.0

    logger.info("validator started. controller=%s usdt=%s validator=%s", cfg.controller, cfg.usdt, validator.address)

    while True:
        try:
            if _poll_lang_commands(cfg, state, runtime_chat_lang_map, bot_username):
                state["telegram_chat_lang_map"] = runtime_chat_lang_map
                _save_state(cfg.state_file, state)

            now = time.time()
            if now >= next_gas_check_at:
                if _poll_gas_balance_alerts(cfg, w3, state, runtime_chat_lang_map):
                    _save_state(cfg.state_file, state)
                next_gas_check_at = now + cfg.gas_check_interval

            head = w3.eth.block_number
            safe = max(0, head - cfg.confirmations)
            if last_block == 0:
                last_block = safe
            if last_block >= safe:
                time.sleep(cfg.poll_interval)
                continue

            frm = last_block + 1
            to = safe
            logs = w3.eth.get_logs(
                {
                    "fromBlock": frm,
                    "toBlock": to,
                    "address": cfg.controller,
                    "topics": [[DEPOSIT_TOPIC, REFUND_TOPIC]],
                }
            )
            logs = sorted(logs, key=lambda x: (x.get("blockNumber", 0), x.get("logIndex", 0)))

            for lg in logs:
                topics = lg.get("topics") or []
                if len(topics) < 3:
                    continue
                t0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
                if not t0.startswith("0x"):
                    t0 = "0x" + t0
                user = _topic_addr(_norm_hex32(topics[1]))
                txh = _norm_hex32(topics[2])
                amount = _decode_amount(lg.get("data"))
                bookkeeping_tx_hash = _norm_hex32(lg.get("transactionHash"))

                receipt = _get_receipt_with_retry(w3, txh)
                ok = bool(receipt) and _has_matching_transfer_in_receipt(receipt, cfg.usdt, user, cfg.controller, amount)
                if ok:
                    continue

                msg = _format_anomaly_msg(
                    cfg.telegram_default_lang,
                    int(lg.get("blockNumber") or 0),
                    t0.lower() == DEPOSIT_TOPIC.lower(),
                    user,
                    amount,
                    txh,
                    bookkeeping_tx_hash,
                )
                logger.error(msg)
                try:
                    _send_telegram_many_localized(
                        cfg.telegram_token,
                        cfg.telegram_chat_ids,
                        cfg.telegram_default_lang,
                        runtime_chat_lang_map,
                        lambda lang: _format_anomaly_msg(
                            lang,
                            int(lg.get("blockNumber") or 0),
                            t0.lower() == DEPOSIT_TOPIC.lower(),
                            user,
                            amount,
                            txh,
                            bookkeeping_tx_hash,
                        ),
                    )
                except Exception as e:
                    logger.error("telegram send failed: %s", e)

                if cfg.veto_enabled:
                    try:
                        paused_now = bool(controller_contract.functions.keeperAccountingPaused().call())
                        if paused_now:
                            logger.error("anomaly detected but keeper already paused; skip repeated veto")
                        else:
                            ok_veto, detail = _attempt_veto(w3, controller_contract, validator)
                            logger.error("veto result ok=%s detail=%s", ok_veto, detail)
                            _send_telegram_many_localized(
                                cfg.telegram_token,
                                cfg.telegram_chat_ids,
                                cfg.telegram_default_lang,
                                runtime_chat_lang_map,
                                lambda lang: _format_veto_attempt_msg(lang, ok_veto, detail),
                            )
                    except Exception as e:
                        logger.error("veto failed: %s", e)
                        _send_telegram_many_localized(
                            cfg.telegram_token,
                            cfg.telegram_chat_ids,
                            cfg.telegram_default_lang,
                            runtime_chat_lang_map,
                            lambda lang: _format_veto_failed_msg(lang, str(e)),
                        )

            last_block = to
            state["last_block"] = last_block
            state["telegram_chat_lang_map"] = runtime_chat_lang_map
            _save_state(cfg.state_file, state)
            time.sleep(cfg.poll_interval)
        except Exception as e:
            logger.exception("validator loop error: %s", e)
            time.sleep(max(3, cfg.poll_interval))

if __name__ == "__main__":
    raise SystemExit(main())
