from __future__ import annotations

import os
import logging
import json
import asyncio
import time
import sqlite3
import io
import re
import random
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Set, List

from dotenv import load_dotenv
from web3 import Web3
import web3
from web3.middleware import ExtraDataToPOAMiddleware

try:
    from query_stats import query_downline_deposits
except ImportError:

    query_downline_deposits = None

try:
    from telegram import ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        ChatMemberHandler,
        CommandHandler,
        MessageHandler,
        ContextTypes,
        filters,
    )
except Exception as e:
    raise SystemExit(
        "Missing optional dependency for Telegram bot.\n"
        "Install it with:\n"
        "  pip install -r requirements.txt\n"
        f"Original import error: {e}"
    )

def _get_log_level() -> int:
    s = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    return getattr(logging, s, logging.INFO)

logging.basicConfig(
    level=_get_log_level(),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

BOT_BUILD = "2026-02-05.telegram-i18n.v1"

def _inject_poa_middleware(w3: Web3) -> None:
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def _pick_rpc_url() -> str:
    use_quicknode = _env_bool("USE_QUICKNODE", False)
    if use_quicknode:
        url = os.getenv("QUICKNODE_HTTP_URL", "").strip()
        if not url:
            raise RuntimeError("USE_QUICKNODE=true but QUICKNODE_HTTP_URL is empty")
        return url
    url = os.getenv("BSC_RPC_URL", "").strip()
    if not url:

        url = "https://data-seed-prebsc-1-s1.binance.org:8545/"
    return url

def _fmt_decimal_human(d: Decimal, decimals: int) -> str:
    if decimals < 0:
        decimals = 0
    s = f"{d:,.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s

def _fmt_amount(value_wei: int, token_decimals: int, display_decimals: int) -> str:
    if token_decimals < 0:
        token_decimals = 18
    d = Decimal(int(value_wei)) / (Decimal(10) ** Decimal(token_decimals))
    return _fmt_decimal_human(d, display_decimals)

def _as_code(s: str) -> str:
    return f"`{s}`"

def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _as_pre(s: str) -> str:
    return f"<pre>{_html_escape(s)}</pre>"

def _fmt_ts_local(ts: int, lang: str) -> str:
    if ts <= 0:
        return "-"

    if lang in ("zh-CN", "zh-TW"):
        offset = 8 * 3600
        label = "UTC+8"
        fmt = "%Y-%m-%d %H:%M:%S"
    elif lang == "en-US":
        offset = -5 * 3600
        label = "ET(UTC-5)"
        fmt = "%m/%d/%Y %H:%M:%S"
    elif lang == "ko":
        offset = 9 * 3600
        label = "UTC+9"
        fmt = "%Y.%m.%d %H:%M:%S"
    elif lang == "ja":
        offset = 9 * 3600
        label = "UTC+9"
        fmt = "%Y/%m/%d %H:%M:%S"
    elif lang == "th":
        offset = 7 * 3600
        label = "UTC+7"
        fmt = "%d/%m/%Y %H:%M:%S"
    else:
        offset = 0
        label = "UTC"
        fmt = "%Y-%m-%d %H:%M:%S"

    t = time.gmtime(ts + offset)
    return f"{time.strftime(fmt, t)} {label}"

def _draw_candles_png(
    candles: list[tuple[int, int, int, int, int]],
    volumes: list[int],
    interval_secs: int,
) -> bytes:
    if not candles:
        return b""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from matplotlib.patches import Rectangle
        from matplotlib.ticker import ScalarFormatter
        from matplotlib.ticker import FuncFormatter
    except Exception as e:
        logger.error("matplotlib import failed: %s", e)
        return b""

    fig, (ax, ax_vol) = plt.subplots(
        2,
        1,
        figsize=(8, 5),
        dpi=120,
        sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )
    width = (interval_secs / 86400.0) * 0.7

    denom = 1e18
    xs: list[float] = []
    closes: list[float] = []
    colors: list[str] = []
    for ts, o, h, l, c in candles:
        x = mdates.date2num(datetime.fromtimestamp(ts, tz=timezone.utc))
        o = o / denom
        h = h / denom
        l = l / denom
        c = c / denom
        xs.append(x)
        closes.append(c)
        color = "#2ecc71" if c >= o else "#e74c3c"
        colors.append(color)
        ax.plot([x, x], [l, h], color=color, linewidth=1)
        rect = Rectangle(
            (x - width / 2, min(o, c)),
            width,
            max(1e-12, abs(c - o)),
            facecolor=color,
            edgecolor=color,
            linewidth=0.5,
        )
        ax.add_patch(rect)

    if len(closes) >= 2:
        pct = ((closes[-1] - closes[0]) / closes[0]) * 100 if closes[0] != 0 else 0
    else:
        pct = 0

    def _ma(series: list[float], window: int) -> list[float]:
        if len(series) < window:
            return []
        out = []
        s = 0.0
        for i, v in enumerate(series):
            s += v
            if i >= window:
                s -= series[i - window]
            if i >= window - 1:
                out.append(s / window)
        return out

    ma5 = _ma(closes, 5)
    if ma5:
        ax.plot(xs[4:], ma5, color="#f5a623", linewidth=1.0, label="MA5")
    ma10 = _ma(closes, 10)
    if ma10:
        ax.plot(xs[9:], ma10, color="#7f8c8d", linewidth=1.0, label="MA10")

    candle_count = len(candles)

    max_ticks = min(12, max(5, candle_count // 5))

    if interval_secs < 3600:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        base_interval = max(1, interval_secs // 60)

        tick_interval = max(1, (candle_count + max_ticks - 1) // max_ticks)
        ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=base_interval * tick_interval))
    elif interval_secs < 86400:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        base_interval = max(1, interval_secs // 3600)
        tick_interval = max(1, (candle_count + max_ticks - 1) // max_ticks)
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=base_interval * tick_interval))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        base_interval = max(1, interval_secs // 86400)
        tick_interval = max(1, (candle_count + max_ticks - 1) // max_ticks)
        ax.xaxis.set_major_locator(mdates.DayLocator(interval=base_interval * tick_interval))

    ax.yaxis.set_major_formatter(
        FuncFormatter(lambda y, _pos: _fmt_decimal_human(Decimal(str(y)), 8))
    )
    ax.grid(True, alpha=0.2)
    sign = "+" if pct >= 0 else ""
    ax.set_title(f"NAIO Candles (UTC)  {sign}{pct:.2f}%")
    ax.set_ylabel("Price")
    ax.legend(loc="upper left", frameon=False, fontsize=8)

    plt.setp(ax.xaxis.get_majorticklabels(), visible=False)

    if volumes:
        vols = [v / denom for v in volumes]
        ax_vol.bar(xs[: len(vols)], vols, width=width, color=colors[: len(vols)], alpha=0.6)
        ax_vol.set_ylabel("Vol")
        ax_vol.yaxis.set_major_formatter(
            FuncFormatter(lambda y, _pos: _fmt_decimal_human(Decimal(str(y)), 2))
        )
        ax_vol.grid(True, alpha=0.2)

    plt.setp(ax_vol.xaxis.get_majorticklabels(), rotation=45, ha="right")

    buf = io.BytesIO()
    plt.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()

def _format_reply_text(update: Update, text: str) -> tuple[str, str]:
    if not update.effective_chat or not update.effective_user:
        return text, None

    if update.effective_chat.type in ("group", "supergroup"):
        user = update.effective_user
        user_name = user.full_name or user.first_name or "User"
        user_id = user.id

        formatted = f'<a href="tg://user?id={user_id}">{_html_escape(user_name)}</a>\n{text}'
        return formatted, "HTML"

    return text, None

async def _reply_text_with_mention(update: Update, text: str, reply_markup=None, parse_mode=None):
    if not update.message:
        return

    formatted_text, mention_parse_mode = _format_reply_text(update, text)

    final_parse_mode = parse_mode or mention_parse_mode

    await update.message.reply_text(
        formatted_text,
        reply_markup=reply_markup,
        parse_mode=final_parse_mode,
    )

async def _safe_edit_or_reply(q, text: str, reply_markup=None, update: Update = None) -> None:

    if update:
        text, parse_mode = _format_reply_text(update, text)
    else:
        parse_mode = None

    try:
        if parse_mode:
            await q.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        else:
            await q.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        try:
            if q.message:
                if parse_mode:
                    await q.message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
                else:
                    await q.message.reply_text(text, reply_markup=reply_markup)
        except Exception:
            return

async def _send_loading_msg(q, loading_text: str, reply_markup=None, update: Update = None):

    if update:
        loading_text, parse_mode = _format_reply_text(update, loading_text)
    else:
        parse_mode = None

    try:

        try:
            if parse_mode:
                await q.edit_message_text(loading_text, parse_mode=parse_mode, reply_markup=reply_markup)
            else:
                await q.edit_message_text(loading_text, reply_markup=reply_markup)

            return q.message
        except Exception:

            if q.message:
                if parse_mode:
                    return await q.message.reply_text(loading_text, parse_mode=parse_mode, reply_markup=reply_markup)
                else:
                    return await q.message.reply_text(loading_text, reply_markup=reply_markup)
    except Exception:
        pass
    return None

DEFAULT_LANG = "zh-CN"
LANG_LABELS = {
    "zh-CN": "简体中文",
    "zh-TW": "繁體中文",
    "ko": "한국어",
    "en-US": "English (US)",
    "ja": "日本語",
    "th": "ไทย",
}
TRANSLATIONS: dict[str, dict[str, str]] = {
    "menu_select": {
        "zh-CN": "请选择查询项：",
        "zh-TW": "請選擇查詢項：",
        "ko": "조회 항목을 선택하세요:",
        "en-US": "Select an item:",
        "ja": "項目を選択してください:",
        "th": "กรุณาเลือกเมนู:",
    },
    "menu_chain_info": {
        "zh-CN": "链上信息",
        "zh-TW": "鏈上資訊",
        "ko": "온체인 정보",
        "en-US": "On-chain Info",
        "ja": "オンチェーン情報",
        "th": "ข้อมูลออนเชน",
    },
    "menu_ref_query": {
        "zh-CN": "查询推荐关系",
        "zh-TW": "查詢推薦關係",
        "ko": "추천 관계 조회",
        "en-US": "Referral Query",
        "ja": "紹介関係の照会",
        "th": "ตรวจสอบผู้แนะนำ",
    },
    "menu_earn_query": {
        "zh-CN": "查询地址收益",
        "zh-TW": "查詢地址收益",
        "ko": "수익 조회",
        "en-US": "Earnings Query",
        "ja": "収益照会",
        "th": "ตรวจสอบรายได้",
    },
    "menu_info_query": {
        "zh-CN": "查询地址信息",
        "zh-TW": "查詢地址資訊",
        "ko": "주소 정보 조회",
        "en-US": "Address Info",
        "ja": "アドレス情報照会",
        "th": "ตรวจสอบข้อมูลที่อยู่",
    },
    "menu_help_ops": {
        "zh-CN": "链上操作说明",
        "zh-TW": "鏈上操作說明",
        "ko": "온체인 작업 안내",
        "en-US": "On-chain Guide",
        "ja": "オンチェーン操作説明",
        "th": "คู่มือออนเชน",
    },
    "menu_addr_list": {
        "zh-CN": "合约地址列表",
        "zh-TW": "合約地址清單",
        "ko": "컨트랙트 주소 목록",
        "en-US": "Contract Addresses",
        "ja": "コントラクト住所一覧",
        "th": "รายการที่อยู่สัญญา",
    },
    "menu_more": {
        "zh-CN": "更多",
        "zh-TW": "更多",
        "ko": "더보기",
        "en-US": "More",
        "ja": "もっと見る",
        "th": "เพิ่มเติม",
    },
    "menu_back": {
        "zh-CN": "返回",
        "zh-TW": "返回",
        "ko": "돌아가기",
        "en-US": "Back",
        "ja": "戻る",
        "th": "กลับ",
    },
    "menu_cmd_list": {
        "zh-CN": "命令列表",
        "zh-TW": "命令列表",
        "ko": "명령어 목록",
        "en-US": "Command List",
        "ja": "コマンド一覧",
        "th": "รายการคำสั่ง",
    },
    "menu_chain_roles": {
        "zh-CN": "链上角色",
        "zh-TW": "鏈上角色",
        "ko": "온체인 역할",
        "en-US": "Chain Roles",
        "ja": "オンチェーン役割",
        "th": "บทบาทออนเชน",
    },
    "chain_info_title": {
        "zh-CN": "📈 链上信息",
        "zh-TW": "📈 鏈上資訊",
        "ko": "📈 온체인 정보",
        "en-US": "📈 On-chain Info",
        "ja": "📈 オンチェーン情報",
        "th": "📈 ข้อมูลออนเชน",
    },
    "chain_info_section_price": {
        "zh-CN": "💹 价格与底池",
        "zh-TW": "💹 價格與底池",
        "ko": "💹 가격·풀",
        "en-US": "💹 Price & Pool",
        "ja": "💹 価格・プール",
        "th": "💹 ราคาและพูล",
    },
    "chain_info_section_epoch": {
        "zh-CN": "⏱ 通缩与当期",
        "zh-TW": "⏱ 通縮與當期",
        "ko": "⏱ 통축·당기",
        "en-US": "⏱ Deflation & Period",
        "ja": "⏱ 通縮・当期",
        "th": "⏱ Deflation และช่วง",
    },
    "chain_info_section_withdraw": {
        "zh-CN": "📤 撤本额度与队列",
        "zh-TW": "📤 撤本額度與佇列",
        "ko": "📤 출금 한도·큐",
        "en-US": "📤 Withdraw & Queue",
        "ja": "📤 撤本枠・キュー",
        "th": "📤 ถอนและคิว",
    },
    "chain_info_section_deflation": {
        "zh-CN": "🔥 最近通缩快照",
        "zh-TW": "🔥 最近通縮快照",
        "ko": "🔥 최근 통축 스냅",
        "en-US": "🔥 Last Deflation Snapshot",
        "ja": "🔥 最新通縮スナップ",
        "th": "🔥 สแนป Deflation ล่าสุด",
    },
    "chain_info_section_time": {
        "zh-CN": "🕐 当期时间",
        "zh-TW": "🕐 當期時間",
        "ko": "🕐 당기 시간",
        "en-US": "🕐 Period Time",
        "ja": "🕐 当期時間",
        "th": "🕐 เวลาช่วง",
    },
    "addr_list_title": {
        "zh-CN": "合约地址列表",
        "zh-TW": "合約地址清單",
        "ko": "컨트랙트 주소 목록",
        "en-US": "Contract Address List",
        "ja": "コントラクト住所一覧",
        "th": "รายการที่อยู่สัญญา",
    },
    "chain_roles_title": {
        "zh-CN": "🔐 链上角色",
        "zh-TW": "🔐 鏈上角色",
        "ko": "🔐 온체인 역할",
        "en-US": "🔐 Chain Roles",
        "ja": "🔐 オンチェーン役割",
        "th": "🔐 บทบาทออนเชน",
    },
    "chain_roles_desc_keeper": {
        "zh-CN": "记账员，调用 depositFromTransferWitness 将用户 USDT 入金记入链上",
        "zh-TW": "記賬員，調用 depositFromTransferWitness 將用戶 USDT 入金記入鏈上",
        "ko": "기록원, depositFromTransferWitness 호출로 USDT 입금을 온체인에 기록",
        "en-US": "Bookkeeper; calls depositFromTransferWitness to record USDT deposits",
        "ja": "記帳員、depositFromTransferWitness を呼び出して USDT 入金を記録",
        "th": "ผู้บันทึกบัญชี เรียก depositFromTransferWitness เพื่อบันทึกการฝาก USDT",
    },
    "chain_roles_desc_witness": {
        "zh-CN": "3/3 见证人，入金需三者均签名才能通过规则引擎",
        "zh-TW": "3/3 見證人，入金需三者均簽名才能通過規則引擎",
        "ko": "3/3 증인, 입금 시 3명 모두 서명해야 규칙 엔진 통과",
        "en-US": "3/3 witnesses; all three must sign for deposits to pass rule engine",
        "ja": "3/3 証人、入金には3者全員の署名が必要",
        "th": "พยาน 3/3 ต้องลงนามทั้ง 3 คนเพื่อฝากเงินผ่าน rule engine",
    },
    "chain_roles_desc_owner": {
        "zh-CN": "主合约池 owner，拥有最高治理权（通常为 KeeperCouncil 多签）",
        "zh-TW": "主合約池 owner，擁有最高治理權（通常為 KeeperCouncil 多簽）",
        "ko": "메인 컨트랙트 owner, 최고 거버넌스 권한 (보통 KeeperCouncil 멀티시그)",
        "en-US": "Controller owner; highest governance (usually KeeperCouncil multisig)",
        "ja": "Controller owner、最高のガバナンス権（通常 KeeperCouncil マルチシグ）",
        "th": "Controller owner สิทธิ์กำกับสูงสุด (มักเป็น KeeperCouncil มัลติซิก)",
    },
    "chain_roles_desc_council": {
        "zh-CN": "5/5 委员会，keeper 管理 3/5、成员/见证人变更 5/5",
        "zh-TW": "5/5 委員會，keeper 管理 3/5、成員/見證人變更 5/5",
        "ko": "5/5 위원회, keeper 관리 3/5, 멤버/증인 변경 5/5",
        "en-US": "5/5 council; keeper ops 3/5, member/witness change 5/5",
        "ja": "5/5 委員会、keeper 操作 3/5、メンバー/証人変更 5/5",
        "th": "คณะกรรมการ 5/5 keeper 3/5 สมาชิก/พยาน 5/5",
    },
    "chain_roles_desc_validator": {
        "zh-CN": "唯一验证者，可一票暂停 keeper 记账（紧急风控）",
        "zh-TW": "唯一驗證者，可一票暫停 keeper 記賬（緊急風控）",
        "ko": "유일 검증자, keeper 기록 일시정지 가능 (긴급 리스크 관리)",
        "en-US": "Sole validator; can pause keeper accounting with one vote (emergency)",
        "ja": "唯一の検証者、1票で keeper 記帳を一時停止可能（緊急時）",
        "th": "ผู้ตรวจสอบคนเดียว สามารถหยุด keeper ด้วย 1 คะแนน (ฉุกเฉิน)",
    },
    "chain_roles_desc_fixed": {
        "zh-CN": "5 个固定地址：节点池、市场池、运营池、生态池、独立奖励池",
        "zh-TW": "5 個固定地址：節點池、市場池、營運池、生態池、獨立獎勵池",
        "ko": "5개 고정 주소: 노드풀, 마켓풀, 운영풀, 생태풀, 독립보상풀",
        "en-US": "5 fixed pools: node, market, ops, eco, independent",
        "ja": "5つの固定アドレス: ノード、マーケット、運用、エコ、独立報酬",
        "th": "5 ที่อยู่คงที่: node, market, ops, eco, independent",
    },
    "chain_roles_label_witness": {
        "zh-CN": "3/3 见证",
        "zh-TW": "3/3 見證",
        "ko": "3/3 증인",
        "en-US": "3/3 Witnesses",
        "ja": "3/3 証人",
        "th": "พยาน 3/3",
    },
    "chain_roles_label_council": {
        "zh-CN": "5/5 委员会",
        "zh-TW": "5/5 委員會",
        "ko": "5/5 위원회",
        "en-US": "5/5 Council",
        "ja": "5/5 委員会",
        "th": "คณะกรรมการ 5/5",
    },
    "chain_roles_label_validator": {
        "zh-CN": "验证者",
        "zh-TW": "驗證者",
        "ko": "검증자",
        "en-US": "Validator",
        "ja": "検証者",
        "th": "ผู้ตรวจสอบ",
    },
    "chain_roles_label_fixed": {
        "zh-CN": "5 固定地址",
        "zh-TW": "5 個固定地址",
        "ko": "고정 주소 5개",
        "en-US": "5 Fixed Pools",
        "ja": "固定アドレス5つ",
        "th": "ที่อยู่คงที่ 5 รายการ",
    },
    "chain_roles_nodes_intro": {
        "zh-CN": "节点席位共 1000 个，持有者可领取通缩日释放中的节点分红。",
        "zh-TW": "節點席位共 1000 個，持有者可領取通縮日釋放中的節點分紅。",
        "ko": "노드 1000석; 보유자는 디플레이션 일일 분배에서 노드 몫을 청구합니다.",
        "en-US": "1000 node seats; holders claim node share from daily deflation.",
        "ja": "ノード席位1000。保有者はデフレ日次配分からノード配当を請求できます。",
        "th": "โหนด 1000 ที่ ผู้ถือรับส่วนแบ่งจากการปล่อยรายวันตาม deflation",
    },
    "label_controller": {
        "zh-CN": "池子地址",
        "zh-TW": "池子地址",
        "ko": "풀 주소",
        "en-US": "Controller",
        "ja": "コントローラ",
        "th": "ที่อยู่ Pool",
    },
    "label_naio": {
        "zh-CN": "NAIO 地址",
        "zh-TW": "NAIO 地址",
        "ko": "NAIO 주소",
        "en-US": "NAIO",
        "ja": "NAIO",
        "th": "NAIO",
    },
    "label_usdt": {
        "zh-CN": "USDT 地址",
        "zh-TW": "USDT 地址",
        "ko": "USDT 주소",
        "en-US": "USDT",
        "ja": "USDT",
        "th": "USDT",
    },
    "label_node": {
        "zh-CN": "席位地址",
        "zh-TW": "席位地址",
        "ko": "노드 풀 주소",
        "en-US": "Node Seat Pool",
        "ja": "ノード席プール",
        "th": "พูลที่นั่งโหนด",
    },
    "label_ref_bootstrap": {
        "zh-CN": "推荐冷启动地址",
        "zh-TW": "推薦冷啟動地址",
        "ko": "추천 부트스트랩 주소",
        "en-US": "Referral Bootstrap",
        "ja": "紹介ブートストラップ",
        "th": "ที่อยู่บูตสแตรปการแนะนำ",
    },
    "label_pool_seeder": {
        "zh-CN": "初始注资地址",
        "zh-TW": "初始注資地址",
        "ko": "초기 자금 주소",
        "en-US": "Initial Pool Seeder",
        "ja": "初期注資アドレス",
        "th": "ที่อยู่เติมสภาพคล่องเริ่มต้น",
    },
    "label_burn": {
        "zh-CN": "黑洞地址",
        "zh-TW": "黑洞地址",
        "ko": "소각 주소",
        "en-US": "Burn Address",
        "ja": "バーンアドレス",
        "th": "ที่อยู่เผา",
    },
    "label_price": {
        "zh-CN": "当前价格",
        "zh-TW": "當前價格",
        "ko": "현재 가격",
        "en-US": "Price",
        "ja": "現在価格",
        "th": "ราคา",
    },
    "label_pool_usdt": {
        "zh-CN": "底池USDT",
        "zh-TW": "底池USDT",
        "ko": "풀 USDT",
        "en-US": "Pool USDT",
        "ja": "プールUSDT",
        "th": "USDT ในพูล",
    },
    "label_pool_naio": {
        "zh-CN": "底池NAIO",
        "zh-TW": "底池NAIO",
        "ko": "풀 NAIO",
        "en-US": "Pool NAIO",
        "ja": "プールNAIO",
        "th": "NAIO ในพูล",
    },
    "label_burned_naio": {
        "zh-CN": "已销毁NAIO",
        "zh-TW": "已銷毀NAIO",
        "ko": "소각 누적 NAIO",
        "en-US": "Burned NAIO",
        "ja": "焼却済みNAIO",
        "th": "NAIO ที่ถูกเผา",
    },
    "label_reserved_usdt": {
        "zh-CN": "未领取USDT",
        "zh-TW": "未領取USDT",
        "ko": "미수령 USDT",
        "en-US": "Unclaimed USDT",
        "ja": "未受領USDT",
        "th": "USDT ที่ยังไม่รับ",
    },
    "label_reserved_naio": {
        "zh-CN": "未领取NAIO",
        "zh-TW": "未領取NAIO",
        "ko": "미수령 NAIO",
        "en-US": "Unclaimed NAIO",
        "ja": "未受領NAIO",
        "th": "NAIO ที่ยังไม่รับ",
    },
    "label_next_poke": {
        "zh-CN": "下一次通缩时间",
        "zh-TW": "下一次通縮時間",
        "ko": "다음 통축 시간",
        "en-US": "Next Deflation",
        "ja": "次の通縮時間",
        "th": "เวลาทำ Deflation ครั้งถัดไป",
    },
    "label_epoch_deposit": {
        "zh-CN": "当期总入金(USDT)",
        "zh-TW": "當期總入金(USDT)",
        "ko": "당기 총 입금(USDT)",
        "en-US": "Current Period Deposits (USDT)",
        "ja": "当期入金(USDT)",
        "th": "ยอดฝากช่วงปัจจุบัน (USDT)",
    },
    "label_epoch_sell": {
        "zh-CN": "当期已卖出估值(USDT)",
        "zh-TW": "當期已賣出估值(USDT)",
        "ko": "당기 매도 추정(USDT)",
        "en-US": "Current Period Sells (USDT)",
        "ja": "当期売却(USDT)",
        "th": "ยอดขายช่วงปัจจุบัน (USDT)",
    },
    "label_withdraw_limit": {
        "zh-CN": "当期可撤额度",
        "zh-TW": "當期可撤額度",
        "ko": "당기 출금 가능 한도",
        "en-US": "Current Period Withdraw Limit",
        "ja": "当期撤本可能額",
        "th": "วงเงินถอนช่วงปัจจุบัน",
    },
    "label_withdraw_limit_initial": {
        "zh-CN": "当期可销毁额度",
        "zh-TW": "當期可銷毀額度",
        "ko": "당기 소각 한도",
        "en-US": "Current Period Burn Quota",
        "ja": "当期焼却枠",
        "th": "โควต้าเผาช่วงปัจจุบัน",
    },
    "label_withdraw_limit_used": {
        "zh-CN": "当期已用撤本额度",
        "zh-TW": "當期已用撤本額度",
        "ko": "당기 사용된 출금 한도",
        "en-US": "Current Period Withdraw Used",
        "ja": "当期使用済み撤本額",
        "th": "วงเงินถอนที่ใช้แล้วช่วงปัจจุบัน",
    },
    "label_withdraw_queue_pending": {
        "zh-CN": "撤本队列总待处理",
        "zh-TW": "撤本佇列總待處理",
        "ko": "출금 큐 총 대기금액",
        "en-US": "Withdraw Queue Pending Total",
        "ja": "撤本キュー総待機額",
        "th": "ยอดคิวถอนรอดำเนินการรวม",
    },
    "label_withdraw_queue_users": {
        "zh-CN": "撤本队列用户数",
        "zh-TW": "撤本佇列用戶數",
        "ko": "출금 큐 사용자 수",
        "en-US": "Withdraw Queue Users",
        "ja": "撤本キュー人数",
        "th": "จำนวนผู้ใช้ในคิวถอน",
    },
    "label_poke_ready": {
        "zh-CN": "当前可执行通缩",
        "zh-TW": "當前可執行通縮",
        "ko": "현재 통축 실행 가능",
        "en-US": "Poke Ready",
        "ja": "現在通縮実行可能",
        "th": "พร้อมทำ Deflation ตอนนี้",
    },
    "label_catchup_epochs": {
        "zh-CN": "待补发Epoch数",
        "zh-TW": "待補發Epoch數",
        "ko": "미정산 Epoch 수",
        "en-US": "Catch-up Epochs",
        "ja": "未補発行Epoch数",
        "th": "จำนวน Epoch ที่ต้องตามทัน",
    },
    "label_deflation_last_epoch": {
        "zh-CN": "最近通缩Epoch",
        "zh-TW": "最近通縮Epoch",
        "ko": "최근 통축 Epoch",
        "en-US": "Last Deflation Epoch",
        "ja": "最新通縮Epoch",
        "th": "Epoch Deflation ล่าสุด",
    },
    "label_deflation_total": {
        "zh-CN": "最近通缩总量",
        "zh-TW": "最近通縮總量",
        "ko": "최근 통축 총량",
        "en-US": "Last Deflation Total",
        "ja": "最新通縮総量",
        "th": "ยอด Deflation รวมล่าสุด",
    },
    "label_deflation_burn_value": {
        "zh-CN": "黑洞价值(USDT)",
        "zh-TW": "黑洞價值(USDT)",
        "ko": "소각 가치(USDT)",
        "en-US": "Burn Value (USDT)",
        "ja": "バーン価値(USDT)",
        "th": "มูลค่าที่เผา (USDT)",
    },
    "label_deflation_split": {
        "zh-CN": "分配(生态/复投/节点/独立/推荐/静态)",
        "zh-TW": "分配(生態/復投/節點/獨立/推薦/靜態)",
        "ko": "분배(에코/재투자/노드/독립/추천/정적)",
        "en-US": "Split (eco/reinvest/node/ind/ref/static)",
        "ja": "配分(エコ/再投資/ノード/独立/紹介/静的)",
        "th": "สัดส่วน (eco/reinvest/node/ind/ref/static)",
    },
    "label_referral_pool": {
        "zh-CN": "推荐奖励池",
        "zh-TW": "推薦獎勵池",
        "ko": "추천 보상 풀",
        "en-US": "Referral Reward Pool",
        "ja": "紹介報酬プール",
        "th": "พูลรางวัลแนะนำ",
    },
    "label_current_release_rate": {
        "zh-CN": "当期释放率",
        "zh-TW": "當期釋放率",
        "ko": "당기 방출률",
        "en-US": "Current Period Release Rate",
        "ja": "当期放出率",
        "th": "อัตราปล่อยช่วงปัจจุบัน",
    },
    "label_burn_quota_remaining": {
        "zh-CN": "当期剩余可销毁额度",
        "zh-TW": "當期剩餘可銷毀額度",
        "ko": "당기 남은 소각 한도",
        "en-US": "Current Period Remaining Burn Quota",
        "ja": "当期残り焼却枠",
        "th": "โควต้าเผาคงเหลือช่วงปัจจุบัน",
    },
    "label_withdraw_limit_add": {
        "zh-CN": "最近通缩撤本代烧量",
        "zh-TW": "最近通縮撤本代燒量",
        "ko": "최근 통축 시 출금 대행 소각량",
        "en-US": "Withdraw Burn Consumed (Last Deflation)",
        "ja": "最新通縮での撤本代行焼却量",
        "th": "จำนวนเผาจากถอนใน Deflation ล่าสุด",
    },
    "label_withdraw_queue": {
        "zh-CN": "撤本队列待处理",
        "zh-TW": "撤本佇列待處理",
        "ko": "출금 대기 중",
        "en-US": "Withdraw Queue Pending",
        "ja": "撤本キュー待機中",
        "th": "คิวถอนรอการดำเนินการ",
    },
    "label_epoch_window": {
        "zh-CN": "当期Epoch时间起止",
        "zh-TW": "當期Epoch時間起止",
        "ko": "당기 Epoch 시간 구간",
        "en-US": "Current Period Epoch time range",
        "ja": "当期Epoch時間範囲",
        "th": "ช่วงเวลา Epoch ช่วงปัจจุบัน",
    },
    "prompt_need_format": {
        "zh-CN": "请按格式发送：\n{example}",
        "zh-TW": "請按格式發送：\n{example}",
        "ko": "다음 형식으로 보내주세요:\n{example}",
        "en-US": "Send in this format:\n{example}",
        "ja": "次の形式で送信してください:\n{example}",
        "th": "กรุณาส่งตามรูปแบบนี้:\n{example}",
    },
    "invalid_address": {
        "zh-CN": "地址格式不正确，请重新发送 0x 开头的地址。",
        "zh-TW": "地址格式不正確，請重新發送 0x 開頭的地址。",
        "ko": "주소 형식이 올바르지 않습니다. 0x로 시작하는 주소를 보내주세요.",
        "en-US": "Invalid address. Send a 0x-prefixed address.",
        "ja": "アドレス形式が正しくありません。0xから始まるアドレスを送ってください。",
        "th": "รูปแบบที่อยู่ไม่ถูกต้อง กรุณาส่งที่อยู่ที่ขึ้นต้นด้วย 0x",
    },
    "ref_query_no_ref": {
        "zh-CN": "查询地址：{addr}\n上级地址：无（0x0）",
        "zh-TW": "查詢地址：{addr}\n上級地址：無（0x0）",
        "ko": "조회 주소: {addr}\n상위: 없음(0x0)",
        "en-US": "Address: {addr}\nReferrer: none (0x0)",
        "ja": "照会アドレス: {addr}\n上位: なし(0x0)",
        "th": "ที่อยู่: {addr}\nผู้แนะนำ: ไม่มี (0x0)",
    },
    "ref_query_with_ref": {
        "zh-CN": "查询地址：{addr}\n上级地址：{ref}",
        "zh-TW": "查詢地址：{addr}\n上級地址：{ref}",
        "ko": "조회 주소: {addr}\n상위: {ref}",
        "en-US": "Address: {addr}\nReferrer: {ref}",
        "ja": "照会アドレス: {addr}\n上位: {ref}",
        "th": "ที่อยู่: {addr}\nผู้แนะนำ: {ref}",
    },
    "ops_help_title": {
        "zh-CN": "📘 链上操作说明",
        "zh-TW": "📘 鏈上操作說明",
        "ko": "📘 온체인 작업 안내",
        "en-US": "📘 On-chain Guide",
        "ja": "📘 オンチェーン操作説明",
        "th": "📘 คู่มือออนเชน",
    },
    "ops_help_desc": {
        "zh-CN": "说明：\n1）以下 OP 都是「向池子地址转一笔很小的 BNB」触发，BNB 会原路退回，你实际消耗的是 Gas；\n2）初始注资请向“初始注资地址”转 USDT，并通过 0.001 BNB OP 触发，不走记账口径；\n3）特别说明：本项目不支持使用多签名地址，只支持普通地址。",
        "zh-TW": "說明：\n1）以下 OP 都是「向池子地址轉一筆很小的 BNB」觸發，BNB 會原路退回，你實際消耗的是 Gas；\n2）初始注資請向「初始注資地址」轉 USDT，並透過 0.001 BNB OP 觸發，不走記帳口徑；\n3）特別說明：本項目不支援使用多簽地址，只支援普通地址。",
        "ko": "안내:\n1) 아래 OP 들은 풀 주소로 소량의 BNB 를 전송하면 실행되며, BNB 는 되돌려지고 실제 비용은 가스입니다.\n2) 초기 자금 주입은 \"초기 자금 주소\"로 USDT 를 송금한 뒤, 0.001 BNB OP 로 트리거하며, 별도의 회계 경로를 사용하지 않습니다.\n3) 중요: 본 프로젝트는 멀티시그 주소를 지원하지 않으며, 일반 주소만 지원합니다.",
        "en-US": "Notes:\n1) The OP actions below are triggered by sending a tiny amount of BNB to the pool; BNB is refunded and you only pay gas.\n2) For initial pool funding, send USDT to the \"Initial Pool Seeder\" address and trigger it via the 0.001 BNB OP; this does not go through the keeper accounting path.\n3) Special note: This project does not support multisig addresses; only regular addresses are supported.",
        "ja": "説明：\n1）以下の OP はすべて「プールアドレスへ少額のBNBを送る」ことでトリガーされ、BNB は元のアドレスに返金され、実際のコストはガスのみです。\n2）初期注資は「初期注資アドレス」に USDT を送金し、0.001 BNB の OP でトリガーし、記帳用の経路は通りません。\n3）特別説明：本プロジェクトはマルチシグアドレスをサポートしておらず、通常アドレスのみ対応しています。",
        "th": "คำอธิบาย:\n1) OP ด้านล่างทั้งหมดถูกเรียกใช้โดยการส่ง BNB จำนวนน้อยไปยังที่อยู่ Pool โดย BNB จะถูกส่งคืน คุณจ่ายเฉพาะค่าแก๊สเท่านั้น\n2) การเติมสภาพคล่องเริ่มต้น ให้โอน USDT ไปยัง “ที่อยู่เติมสภาพคล่องเริ่มต้น” แล้วใช้ OP 0.001 BNB ในการทริกเกอร์ โดยไม่ผ่านเส้นทาง accounting ของ keeper\n3) หมายเหตุพิเศษ: โปรเจกต์นี้ไม่รองรับที่อยู่แบบมัลติซิก รองรับเฉพาะที่อยู่ปกติเท่านั้น",
    },
    "ops_help_other_ops": {
        "zh-CN": "其他常用操作（非 OP）：\n- 入金：把 USDT 直接转到池子地址（金额不合规会自动退款）\n- 卖出：把 NAIO 直接转到池子地址（转多少=卖多少）\n- 绑定推荐：推荐人给你转 ≥ 0.001 NAIO（只绑定一次）",
        "zh-TW": "其他常用操作（非 OP）：\n- 入金：把 USDT 直接轉到池子地址（金額不合規會自動退款）\n- 賣出：把 NAIO 直接轉到池子地址（轉多少=賣多少）\n- 綁定推薦：推薦人給你轉 ≥ 0.001 NAIO（只綁定一次）",
        "ko": "기타 작업(비 OP):\n- 입금: USDT를 풀 주소로 직접 전송(부적합 금액 자동 환불)\n- 매도: NAIO를 풀 주소로 직접 전송(보낸 만큼 매도)\n- 추천 바인딩: 추천인이 0.001 이상 NAIO 전송(1회만)",
        "en-US": "Other actions (non-OP):\n- Deposit: send USDT to the pool (invalid amounts auto-refund)\n- Sell: send NAIO to the pool (amount sent = amount sold)\n- Bind referral: referrer sends >= 0.001 NAIO (one-time)",
        "ja": "その他(非OP):\n- 入金: USDTをプールへ直接送金(不適合は自動返金)\n- 売却: NAIOをプールへ直接送金(送った分だけ売却)\n- 紹介バインド: 紹介者が0.001以上のNAIOを送付(1回のみ)",
        "th": "การทำงานอื่นๆ (ไม่ใช่ OP):\n- ฝาก: ส่ง USDT ไปยังพูล (จำนวนไม่ถูกต้องจะคืนอัตโนมัติ)\n- ขาย: ส่ง NAIO ไปยังพูล (ส่งเท่าไรก็ขายเท่านั้น)\n- ผูกผู้แนะนำ: ผู้แนะนำส่ง NAIO >= 0.001 (ได้ครั้งเดียว)",
    },
    "op_poke": {
        "zh-CN": "通缩/日释放",
        "zh-TW": "通縮/日釋放",
        "ko": "통축/일 방출",
        "en-US": "Deflation",
        "ja": "通縮/日放出",
        "th": "Deflation",
    },
    "op_claim_newuser": {
        "zh-CN": "领取复投奖励",
        "zh-TW": "領取復投獎勵",
        "ko": "재투자 보상 수령",
        "en-US": "Claim re-invest reward",
        "ja": "再投資報酬受領",
        "th": "รับรางวัลรีอินเวสต์",
    },
    "op_claim_fixed_usdt": {
        "zh-CN": "领取平台/市场USDT",
        "zh-TW": "領取平台/市場USDT",
        "ko": "플랫폼/마켓 USDT 수령",
        "en-US": "Claim platform/market USDT",
        "ja": "プラットフォーム/市場USDT受領",
        "th": "รับ USDT แพลตฟอร์ม/มาร์เก็ต",
    },
    "op_claim_static": {
        "zh-CN": "领取静态",
        "zh-TW": "領取靜態",
        "ko": "정적 수령",
        "en-US": "Claim static",
        "ja": "静的受領",
        "th": "รับสเตติก",
    },
    "op_claim_dynamic": {
        "zh-CN": "领取推荐/动态",
        "zh-TW": "領取推薦/動態",
        "ko": "추천/동적 수령",
        "en-US": "Claim referral/dynamic",
        "ja": "紹介/動的受領",
        "th": "รับแนะนำ/ไดนามิก",
    },
    "op_claim_node": {
        "zh-CN": "领取节点分红",
        "zh-TW": "領取節點分紅",
        "ko": "노드 분배 수령",
        "en-US": "Claim node dividend",
        "ja": "ノード配当受領",
        "th": "รับปันผลโหนด",
    },
    "op_withdraw": {
        "zh-CN": "申请撤本",
        "zh-TW": "申請撤本",
        "ko": "출금 신청",
        "en-US": "Withdraw request",
        "ja": "撤本申請",
        "th": "ขอถอน",
    },
    "op_claim_all": {
        "zh-CN": "一键领取",
        "zh-TW": "一鍵領取",
        "ko": "원클릭 수령",
        "en-US": "Claim all",
        "ja": "一括受領",
        "th": "รับทั้งหมด",
    },
    "lang_set_ok": {
        "zh-CN": "语言已设置为：{lang_name}",
        "zh-TW": "語言已設定為：{lang_name}",
        "ko": "언어가 설정되었습니다: {lang_name}",
        "en-US": "Language set to: {lang_name}",
        "ja": "言語を設定しました: {lang_name}",
        "th": "ตั้งค่าภาษาเป็น: {lang_name}",
    },
    "lang_admin_only": {
        "zh-CN": "只有群管理员可以设置语言。",
        "zh-TW": "只有群管理員可以設定語言。",
        "ko": "그룹 관리자만 언어를 설정할 수 있습니다.",
        "en-US": "Only group admins can set the language.",
        "ja": "言語設定は管理者のみ可能です。",
        "th": "เฉพาะแอดมินกลุ่มเท่านั้นที่ตั้งค่าภาษาได้",
    },
    "hash_admin_only": {
        "zh-CN": "只有群管理员可以在群聊中使用 /hash 补录入金。",
        "zh-TW": "只有群管理員可以在群聊中使用 /hash 補錄入金。",
        "ko": "그룹 관리자만 그룹 채팅에서 /hash로 입금을 수동 보정할 수 있습니다.",
        "en-US": "Only group admins can use /hash in this group to backfill a deposit.",
        "ja": "このグループでは管理者のみが /hash で入金を手動補完できます。",
        "th": "ในกลุ่มนี้เฉพาะแอดมินเท่านั้นที่ใช้ /hash เติมรายการฝากได้",
    },
    "lang_choose": {
        "zh-CN": "请选择语言：",
        "zh-TW": "請選擇語言：",
        "ko": "언어를 선택하세요:",
        "en-US": "Choose a language:",
        "ja": "言語を選択してください:",
        "th": "เลือกภาษา:",
    },
    "price_pic_usage": {
        "zh-CN": "用法：/price_pic_2min（每根K线=2分钟）或 /price_pic_1h /price_pic_1d",
        "zh-TW": "用法：/price_pic_2min（每根K線=2分鐘）或 /price_pic_1h /price_pic_1d",
        "ko": "사용법: /price_pic_2min(각 봉=2분) 또는 /price_pic_1h /price_pic_1d",
        "en-US": "Usage: /price_pic_2min (each candle=2min) or /price_pic_1h /price_pic_1d",
        "ja": "使い方: /price_pic_2min（1本=2分）または /price_pic_1h /price_pic_1d",
        "th": "วิธีใช้: /price_pic_2min (แท่งละ 2 นาที) หรือ /price_pic_1h /price_pic_1d",
    },
    "price_pic_no_data": {
        "zh-CN": "时间范围内没有足够数据。",
        "zh-TW": "時間範圍內沒有足夠資料。",
        "ko": "해당 기간 데이터가 없습니다.",
        "en-US": "Not enough data in this range.",
        "ja": "この範囲のデータがありません。",
        "th": "ไม่มีข้อมูลเพียงพอในช่วงนี้",
    },
    "price_pic_failed": {
        "zh-CN": "生成图表失败：{err}",
        "zh-TW": "生成圖表失敗：{err}",
        "ko": "차트 생성 실패: {err}",
        "en-US": "Chart generation failed: {err}",
        "ja": "チャート生成失敗: {err}",
        "th": "สร้างกราฟล้มเหลว: {err}",
    },
    "help_title": {
        "zh-CN": "📖 可用命令列表",
        "zh-TW": "📖 可用命令列表",
        "ko": "📖 사용 가능한 명령어 목록",
        "en-US": "📖 Available Commands",
        "ja": "📖 利用可能なコマンド一覧",
        "th": "📖 รายการคำสั่งที่ใช้ได้",
    },
    "help_start": {
        "zh-CN": "/start - 显示主菜单",
        "zh-TW": "/start - 顯示主選單",
        "ko": "/start - 메인 메뉴 표시",
        "en-US": "/start - Show main menu",
        "ja": "/start - メインメニューを表示",
        "th": "/start - แสดงเมนูหลัก",
    },
    "help_status": {
        "zh-CN": "/status - 查询链上信息（价格、底池、销毁量等）",
        "zh-TW": "/status - 查詢鏈上資訊（價格、底池、銷毀量等）",
        "ko": "/status - 온체인 정보 조회 (가격, 풀, 소각량 등)",
        "en-US": "/status - Query on-chain info (price, pool, burned amount, etc.)",
        "ja": "/status - オンチェーン情報を照会（価格、プール、焼却量など）",
        "th": "/status - ตรวจสอบข้อมูลออนเชน (ราคา, พูล, ปริมาณเผา ฯลฯ)",
    },
    "help_lang": {
        "zh-CN": "/lang - 设置语言（群聊仅管理员可用）",
        "zh-TW": "/lang - 設定語言（群聊僅管理員可用）",
        "ko": "/lang - 언어 설정 (그룹에서는 관리자만 가능)",
        "en-US": "/lang - Set language (admin only in groups)",
        "ja": "/lang - 言語設定（グループでは管理者のみ）",
        "th": "/lang - ตั้งค่าภาษา (ในกลุ่มเฉพาะแอดมิน)",
    },
    "help_subscribe": {
        "zh-CN": "/subscribe - 订阅播报（私聊时启用）",
        "zh-TW": "/subscribe - 訂閱播報（私聊時啟用）",
        "ko": "/subscribe - 방송 구독 (개인 채팅에서 활성화)",
        "en-US": "/subscribe - Subscribe to broadcasts (enable in private chat)",
        "ja": "/subscribe - ブロードキャスト購読（プライベートチャットで有効化）",
        "th": "/subscribe - สมัครรับการประกาศ (เปิดใช้ในการแชทส่วนตัว)",
    },
    "help_unsubscribe": {
        "zh-CN": "/unsubscribe - 取消订阅播报",
        "zh-TW": "/unsubscribe - 取消訂閱播報",
        "ko": "/unsubscribe - 방송 구독 취소",
        "en-US": "/unsubscribe - Unsubscribe from broadcasts",
        "ja": "/unsubscribe - ブロードキャスト購読解除",
        "th": "/unsubscribe - ยกเลิกการสมัครรับการประกาศ",
    },
    "help_price_pic": {
        "zh-CN": "/price_pic_Xmin/h/d - 生成价格图表\n例如：/price_pic_15min（每根K线=15分钟）\n/price_pic_2h（每根K线=2小时）\n/price_pic_1d（每根K线=1天）",
        "zh-TW": "/price_pic_Xmin/h/d - 生成價格圖表\n例如：/price_pic_15min（每根K線=15分鐘）\n/price_pic_2h（每根K線=2小時）\n/price_pic_1d（每根K線=1天）",
        "ko": "/price_pic_Xmin/h/d - 가격 차트 생성\n예: /price_pic_15min (각 봉=15분)\n/price_pic_2h (각 봉=2시간)\n/price_pic_1d (각 봉=1일)",
        "en-US": "/price_pic_Xmin/h/d - Generate price chart\nExample: /price_pic_15min (each candle=15min)\n/price_pic_2h (each candle=2h)\n/price_pic_1d (each candle=1d)",
        "ja": "/price_pic_Xmin/h/d - 価格チャート生成\n例: /price_pic_15min（1本=15分）\n/price_pic_2h（1本=2時間）\n/price_pic_1d（1本=1日）",
        "th": "/price_pic_Xmin/h/d - สร้างกราฟราคา\nตัวอย่าง: /price_pic_15min (แท่งละ 15 นาที)\n/price_pic_2h (แท่งละ 2 ชั่วโมง)\n/price_pic_1d (แท่งละ 1 วัน)",
    },
    "help_price_pic2": {
        "zh-CN": "/price_pic2_Xmin/h/d - 去重价格横盘，仅保留价格变化的拐点来画图\n示例：/price_pic2_15min（每根K线≈15分钟的拐点走势）",
        "zh-TW": "/price_pic2_Xmin/h/d - 去重價格橫盤，只保留價格變化的拐點來畫圖\n示例：/price_pic2_15min（每根K線≈15分鐘的拐點走勢）",
        "ko": "/price_pic2_Xmin/h/d - 가격이 변한 지점만 사용하여 차트를 그립니다(횡보 구간 dedup)\n예: /price_pic2_15min",
        "en-US": "/price_pic2_Xmin/h/d - Plot only price-change pivots (dedup flat segments)\nExample: /price_pic2_15min",
        "ja": "/price_pic2_Xmin/h/d - 価格が変化したポイントのみを使ってチャート描画（横ばい区間を間引き）\n例: /price_pic2_15min",
        "th": "/price_pic2_Xmin/h/d - วาดกราฟเฉพาะจุดที่ราคาเปลี่ยน (ตัดช่วงราคาคงที่ออก)\nตัวอย่าง: /price_pic2_15min",
    },
    "help_price_pic3": {
        "zh-CN": "/price_pic3 或 /price_pic3_N - 不看时间窗口，从现在往前按价格变动分段画图；N 为空表示显示所有价格变动，N>0 表示只取最近 N 个价格变动。",
        "zh-TW": "/price_pic3 或 /price_pic3_N - 不看時間窗口，從現在往前按價格變動分段畫圖；N 為空表示顯示所有價格變動，N>0 表示只取最近 N 個價格變動。",
        "ko": "/price_pic3 또는 /price_pic3_N - 시간 구간 없이 지금부터 거꾸로 가격이 바뀐 구간만 이어서 그립니다. N 이 없으면 전체, N>0 이면 최근 N번 변동만 표시합니다.",
        "en-US": "/price_pic3 or /price_pic3_N - Ignore time window and plot segments by distinct price changes from now backwards; empty N shows all changes, N>0 shows the latest N changes.",
        "ja": "/price_pic3 または /price_pic3_N - 時間ウィンドウを見ず、現在からさかのぼって価格変動ごとに区切って描画します。N なし=すべて、N>0=直近 N 回の価格変動のみ。",
        "th": "/price_pic3 หรือ /price_pic3_N - ไม่สนช่วงเวลา วาดกราฟตามช่วงที่ราคามีการเปลี่ยนแปลงจากตอนนี้ย้อนกลับไป; ถ้าไม่ใส่ N จะแสดงทุกช่วง ถ้า N>0 จะแสดงเฉพาะ N ช่วงล่าสุด",
    },
    "help_list_miss": {
        "zh-CN": "/list_miss - 列出有入金但没有推荐关系的地址（用于排查团队业绩缺失）",
        "zh-TW": "/list_miss - 列出有入金但沒有推薦關係的地址（用於排查團隊業績缺失）",
        "ko": "/list_miss - 입금 기록은 있으나 추천 관계가 없는 주소 목록을 표시합니다.",
        "en-US": "/list_miss - List addresses that have deposits but no referral relation (for troubleshooting team stats).",
        "ja": "/list_miss - 入金履歴はあるが紹介関係がないアドレスを一覧表示（チーム業績の欠落確認用）",
        "th": "/list_miss - แสดงที่อยู่ที่มีการฝากแต่ไม่มีความสัมพันธ์ผู้แนะนำ (ใช้ตรวจสอบยอดทีมที่หายไป)",
    },
    "help_hash": {
        "zh-CN": "/hash 0x交易哈希 - 手动补录一笔 USDT 入金转账（补入见证队列）",
        "zh-TW": "/hash 0x交易哈希 - 手動補錄一筆 USDT 入金轉賬（補入見證隊列）",
        "ko": "/hash 0xTxHash - USDT 입금 이체를 수동 보정 등록(증인 큐에 추가)",
        "en-US": "/hash 0xTxHash - Manually backfill a USDT deposit transfer (enqueue witness task).",
        "ja": "/hash 0xTxHash - USDT 入金トランザクションを手動補完（証人キューに追加）",
        "th": "/hash 0xTxHash - เติมข้อมูลโอนฝาก USDT แบบแมนนวล (เพิ่มเข้าคิวพยาน)",
    },
    "hash_usage": {
        "zh-CN": "用法：/hash 0x<64位交易哈希>",
        "zh-TW": "用法：/hash 0x<64位交易哈希>",
        "ko": "사용법: /hash 0x<64자리 트랜잭션 해시>",
        "en-US": "Usage: /hash 0x<64-hex transaction hash>",
        "ja": "使い方: /hash 0x<64桁トランザクションハッシュ>",
        "th": "วิธีใช้: /hash 0x<แฮชธุรกรรม 64 หลัก>",
    },
    "hash_usage_example": {
        "zh-CN": "示例：/hash 0x3f00005e2958d185ddac9bf3a8f445ed9070801f127d0941f8177a3ac28913de",
        "zh-TW": "示例：/hash 0x3f00005e2958d185ddac9bf3a8f445ed9070801f127d0941f8177a3ac28913de",
        "ko": "예시: /hash 0x3f00005e2958d185ddac9bf3a8f445ed9070801f127d0941f8177a3ac28913de",
        "en-US": "Example: /hash 0x3f00005e2958d185ddac9bf3a8f445ed9070801f127d0941f8177a3ac28913de",
        "ja": "例: /hash 0x3f00005e2958d185ddac9bf3a8f445ed9070801f127d0941f8177a3ac28913de",
        "th": "ตัวอย่าง: /hash 0x3f00005e2958d185ddac9bf3a8f445ed9070801f127d0941f8177a3ac28913de",
    },
    "hash_submit_ok": {
        "zh-CN": "✅ 补录请求已提交\n交易哈希：{txh}\n状态：{detail}{extra}",
        "zh-TW": "✅ 補錄請求已提交\n交易哈希：{txh}\n狀態：{detail}{extra}",
        "en-US": "✅ Backfill request submitted\nTxHash: {txh}\nStatus: {detail}{extra}",
    },
    "hash_submit_fail": {
        "zh-CN": "❌ 补录失败\n交易哈希：{txh}\n原因：{detail}",
        "zh-TW": "❌ 補錄失敗\n交易哈希：{txh}\n原因：{detail}",
        "en-US": "❌ Backfill failed\nTxHash: {txh}\nReason: {detail}",
    },
    "hash_extra_user": {
        "zh-CN": "\n用户：{user}",
        "zh-TW": "\n用戶：{user}",
        "en-US": "\nUser: {user}",
    },
    "hash_extra_amount": {
        "zh-CN": "\n金额：{amt} USDT",
        "zh-TW": "\n金額：{amt} USDT",
        "en-US": "\nAmount: {amt} USDT",
    },
    "hash_detail_queued": {
        "zh-CN": "已入见证队列，等待签名与提交",
        "zh-TW": "已入見證隊列，等待簽名與提交",
        "en-US": "Queued for witness signatures and submit",
    },
    "hash_detail_already_queued": {
        "zh-CN": "该交易已在见证队列中",
        "zh-TW": "該交易已在見證隊列中",
        "en-US": "Already in witness queue",
    },
    "hash_detail_already_processed": {
        "zh-CN": "该交易链上已处理，无需补录",
        "zh-TW": "該交易鏈上已處理，無需補錄",
        "en-US": "Already processed on-chain",
    },
    "hash_detail_bad_tx_hash": {
        "zh-CN": "交易哈希格式不正确",
        "zh-TW": "交易哈希格式不正確",
        "en-US": "Invalid transaction hash format",
    },
    "hash_detail_hub_not_configured": {
        "zh-CN": "未配置 WITNESS_HUB_SERVER_URL，无法提交补录",
        "zh-TW": "未配置 WITNESS_HUB_SERVER_URL，無法提交補錄",
        "en-US": "WITNESS_HUB_SERVER_URL is not configured",
    },
    "hash_detail_tx_not_found": {
        "zh-CN": "链上未找到该交易回执",
        "zh-TW": "鏈上未找到該交易回執",
        "en-US": "Transaction receipt not found on-chain",
    },
    "hash_detail_transfer_not_found": {
        "zh-CN": "该交易中未找到“USDT -> Controller”的 Transfer 日志",
        "zh-TW": "該交易中未找到「USDT -> Controller」的 Transfer 日誌",
        "en-US": "No USDT->Controller transfer log found in this tx",
    },
    "hash_detail_multiple_transfer_logs": {
        "zh-CN": "该交易包含多条候选入金日志，无法自动判定",
        "zh-TW": "該交易包含多條候選入金日誌，無法自動判定",
        "en-US": "Multiple candidate transfer logs found in this tx",
    },
    "hash_detail_witness_disabled": {
        "zh-CN": "见证模式未启用，当前无法补录入队",
        "zh-TW": "見證模式未啟用，當前無法補錄入隊",
        "en-US": "Witness mode is disabled",
    },
    "hash_detail_enqueue_failed": {
        "zh-CN": "补录请求已发送，但入队失败",
        "zh-TW": "補錄請求已發送，但入隊失敗",
        "en-US": "Backfill request sent but enqueue failed",
    },
    "hash_detail_enqueue_exception": {
        "zh-CN": "补录入队执行异常，请查看监听器日志",
        "zh-TW": "補錄入隊執行異常，請查看監聽器日誌",
        "en-US": "Enqueue execution exception, check listener logs",
    },
    "hash_detail_hub_internal_error": {
        "zh-CN": "见证 Hub 内部错误，请查看监听器日志",
        "zh-TW": "見證 Hub 內部錯誤，請查看監聽器日誌",
        "en-US": "Witness hub internal error, check listener logs",
    },
    "hash_detail_hub_request_failed": {
        "zh-CN": "请求见证 Hub 失败（连接中断/超时）",
        "zh-TW": "請求見證 Hub 失敗（連接中斷/超時）",
        "en-US": "Failed to reach witness hub (connection closed/timeout)",
    },
    "hash_detail_bad_hub_response": {
        "zh-CN": "见证 Hub 返回了无效响应",
        "zh-TW": "見證 Hub 返回了無效響應",
        "en-US": "Witness hub returned invalid response",
    },
    "list_miss_scope": {
        "zh-CN": "全网范围内：",
        "zh-TW": "全網範圍內：",
        "ko": "전체 네트워크 기준:",
        "en-US": "Global scope:",
        "ja": "全ネットワーク範囲：",
        "th": "ช่วงข้อมูลทั้งระบบ:",
    },
    "list_miss_total_deposit_users": {
        "zh-CN": "- 总入金地址数: {count}",
        "zh-TW": "- 總入金地址數: {count}",
        "ko": "- 입금 기록이 있는 주소 수: {count}",
        "en-US": "- Addresses with deposits: {count}",
        "ja": "- 入金履歴のあるアドレス数: {count}",
        "th": "- จำนวนที่อยู่ที่มีการฝาก: {count}",
    },
    "list_miss_total_ref_users": {
        "zh-CN": "- 有推荐关系的地址数: {count}",
        "zh-TW": "- 有推薦關係的地址數: {count}",
        "ko": "- 추천 관계가 있는 주소 수: {count}",
        "en-US": "- Addresses with referral relation: {count}",
        "ja": "- 紹介関係のあるアドレス数: {count}",
        "th": "- จำนวนที่อยู่ที่มีความสัมพันธ์ผู้แนะนำ: {count}",
    },
    "list_miss_scope_users": {
        "zh-CN": "- 本次检查范围内的入金地址数: {count}",
        "zh-TW": "- 本次檢查範圍內的入金地址數: {count}",
        "ko": "- 이번 검사 범위 내 입금 주소 수: {count}",
        "en-US": "- Deposit addresses in this check scope: {count}",
        "ja": "- 今回のチェック範囲内の入金アドレス数: {count}",
        "th": "- จำนวนที่อยู่ที่มีการฝากในช่วงตรวจสอบนี้: {count}",
    },
    "list_miss_missing_count": {
        "zh-CN": "- 其中缺少推荐关系的地址数: {count}",
        "zh-TW": "- 其中缺少推薦關係的地址數: {count}",
        "ko": "- 이 중 추천 관계가 없는 주소 수: {count}",
        "en-US": "- Addresses without referral relation: {count}",
        "ja": "- このうち紹介関係がないアドレス数: {count}",
        "th": "- จำนวนที่อยู่ที่ไม่มีความสัมพันธ์ผู้แนะนำ: {count}",
    },
    "list_miss_none": {
        "zh-CN": "没有发现“有入金但没有推荐关系”的地址。",
        "zh-TW": "沒有發現「有入金但沒有推薦關係」的地址。",
        "ko": "\"입금 기록은 있으나 추천 관계가 없는\" 주소는 발견되지 않았습니다.",
        "en-US": "No addresses found that have deposits but no referral relation.",
        "ja": "「入金履歴はあるが紹介関係がない」アドレスは見つかりませんでした。",
        "th": "ไม่พบที่อยู่ที่ “มีการฝากแต่ไม่มีความสัมพันธ์ผู้แนะนำ”",
    },
    "list_miss_intro": {
        "zh-CN": "以下地址在 deposit_records 中有入金记录，但在 referral_relations 中没有对应推荐关系：",
        "zh-TW": "以下地址在 deposit_records 中有入金記錄，但在 referral_relations 中沒有對應推薦關係：",
        "ko": "다음 주소들은 deposit_records 에 입금 기록은 있으나 referral_relations 에 추천 관계가 없습니다:",
        "en-US": "The following addresses have deposits in deposit_records but no referral relation in referral_relations:",
        "ja": "以下のアドレスは deposit_records に入金履歴はありますが、referral_relations に対応する紹介関係がありません：",
        "th": "ที่อยู่ต่อไปนี้มีการฝากใน deposit_records แต่ไม่มีความสัมพันธ์ผู้แนะนำใน referral_relations:",
    },
    "help_menu": {
        "zh-CN": "\n\n💡 提示：使用 /start 可打开主菜单，通过按钮快速访问各项功能。",
        "zh-TW": "\n\n💡 提示：使用 /start 可打開主選單，通過按鈕快速訪問各項功能。",
        "ko": "\n\n💡 팁: /start를 사용하여 메인 메뉴를 열고 버튼으로 빠르게 기능에 접근할 수 있습니다.",
        "en-US": "\n\n💡 Tip: Use /start to open the main menu and quickly access features via buttons.",
        "ja": "\n\n💡 ヒント: /startを使用してメインメニューを開き、ボタンで機能に素早くアクセスできます。",
        "th": "\n\n💡 เคล็ดลับ: ใช้ /start เพื่อเปิดเมนูหลักและเข้าถึงฟีเจอร์ต่างๆ ผ่านปุ่มได้อย่างรวดเร็ว",
    },
    "earn_title": {
        "zh-CN": "📊 地址收益（可领取/待结算）",
        "zh-TW": "📊 地址收益（可領取/待結算）",
        "ko": "📊 주소 수익(수령 가능/미정산)",
        "en-US": "📊 Address Earnings (claimable/estimated)",
        "ja": "📊 アドレス収益（受領可能/未精算）",
        "th": "📊 รายได้ของที่อยู่ (รับได้/คาดการณ์)",
    },
    "earn_address": {
        "zh-CN": "地址：{addr}",
        "zh-TW": "地址：{addr}",
        "ko": "주소: {addr}",
        "en-US": "Address: {addr}",
        "ja": "アドレス: {addr}",
        "th": "ที่อยู่: {addr}",
    },
    "earn_claimable": {
        "zh-CN": "可直接领取：",
        "zh-TW": "可直接領取：",
        "ko": "즉시 수령 가능:",
        "en-US": "Claimable:",
        "ja": "受領可能:",
        "th": "รับได้ทันที:",
    },
    "earn_static": {
        "zh-CN": "- 静态（OP 0.0005）：{amt} NAIO",
        "zh-TW": "- 靜態（OP 0.0005）：{amt} NAIO",
        "ko": "- 정적(0.0005 OP): {amt} NAIO",
        "en-US": "- Static (OP 0.0005): {amt} NAIO",
        "ja": "- 静的（OP 0.0005）：{amt} NAIO",
        "th": "- คงที่ (OP 0.0005): {amt} NAIO",
    },
    "earn_dynamic": {
        "zh-CN": "- 推荐/动态（OP 0.0006）：{amt} NAIO",
        "zh-TW": "- 推薦/動態（OP 0.0006）：{amt} NAIO",
        "ko": "- 추천/동적(0.0006 OP): {amt} NAIO",
        "en-US": "- Referral/Dynamic (OP 0.0006): {amt} NAIO",
        "ja": "- 紹介/動的（OP 0.0006）：{amt} NAIO",
        "th": "- แนะนำ/ไดนามิก (OP 0.0006): {amt} NAIO",
    },
    "earn_node": {
        "zh-CN": "- 节点分红（OP 0.0007）：{usdt} USDT + {naio} NAIO",
        "zh-TW": "- 節點分紅（OP 0.0007）：{usdt} USDT + {naio} NAIO",
        "ko": "- 노드 분배(0.0007 OP): {usdt} USDT + {naio} NAIO",
        "en-US": "- Node dividend (OP 0.0007): {usdt} USDT + {naio} NAIO",
        "ja": "- ノード配当（OP 0.0007）：{usdt} USDT + {naio} NAIO",
        "th": "- ปันผลโหนด (OP 0.0007): {usdt} USDT + {naio} NAIO",
    },
    "earn_fixed_usdt": {
        "zh-CN": "- 固定地址 USDT：{amt} USDT",
        "zh-TW": "- 固定地址 USDT：{amt} USDT",
        "ko": "- 고정 주소 USDT: {amt} USDT",
        "en-US": "- Fixed-address USDT: {amt} USDT",
        "ja": "- 固定アドレス USDT: {amt} USDT",
        "th": "- USDT ที่อยู่คงที่: {amt} USDT",
    },
    "earn_estimated": {
        "zh-CN": "需计算（估算）：",
        "zh-TW": "需計算（估算）：",
        "ko": "추정 계산:",
        "en-US": "Estimated:",
        "ja": "推定:",
        "th": "คาดการณ์:",
    },
    "earn_unsettled": {
        "zh-CN": "- 未结算静态：{amt} NAIO",
        "zh-TW": "- 未結算靜態：{amt} NAIO",
        "ko": "- 미정산 정적: {amt} NAIO",
        "en-US": "- Unsettled static: {amt} NAIO",
        "ja": "- 未精算静的: {amt} NAIO",
        "th": "- สเตติกที่ยังไม่สรุป: {amt} NAIO",
    },
    "earn_unsettled_note": {
        "zh-CN": "说明：这部分会在下次入金/撤本/领取静态时结算进「静态可直接领取」。",
        "zh-TW": "說明：這部分會在下次入金/撤本/領取靜態時結算進「靜態可直接領取」。",
        "ko": "안내: 다음 입금/출금/정적 수령 시 정산되어 '정적 수령 가능'에 합산됩니다.",
        "en-US": "Note: This is settled into claimable static on next deposit/withdraw/claim.",
        "ja": "説明: 次回の入金/撤本/静的受領時に精算されます。",
        "th": "หมายเหตุ: จะถูกปรับเข้ารับได้เมื่อมีการฝาก/ถอน/รับครั้งถัดไป",
    },
    "info_title": {
        "zh-CN": "📊 地址信息",
        "zh-TW": "📊 地址資訊",
        "ko": "📊 주소 정보",
        "en-US": "📊 Address Info",
        "ja": "📊 アドレス情報",
        "th": "📊 ข้อมูลที่อยู่",
    },
    "info_address": {
        "zh-CN": "地址：{addr}",
        "zh-TW": "地址：{addr}",
        "ko": "주소: {addr}",
        "en-US": "Address: {addr}",
        "ja": "アドレス: {addr}",
        "th": "ที่อยู่: {addr}",
    },
    "info_basic": {
        "zh-CN": "💼 基本信息",
        "zh-TW": "💼 基本資訊",
        "ko": "💼 기본 정보",
        "en-US": "💼 Basic Info",
        "ja": "💼 基本情報",
        "th": "💼 ข้อมูลพื้นฐาน",
    },
    "info_principal": {
        "zh-CN": "累计入金：{amt} USDT",
        "zh-TW": "累計入金：{amt} USDT",
        "ko": "누적 입금: {amt} USDT",
        "en-US": "Total Deposits: {amt} USDT",
        "ja": "累計入金: {amt} USDT",
        "th": "ฝากสะสม: {amt} USDT",
    },
    "info_total_claimed_earnings": {
        "zh-CN": "累计已领取收益(USDT等价)：{amt} USDT",
        "zh-TW": "累計已領取收益(USDT等價)：{amt} USDT",
        "ko": "누적 수령 수익(USDT 상당): {amt} USDT",
        "en-US": "Total Claimed Earnings (USDT equiv.): {amt} USDT",
        "ja": "累計受取済み収益(USDT換算): {amt} USDT",
        "th": "รายได้รับแล้วสะสม (เทียบ USDT): {amt} USDT",
    },
    "info_power": {
        "zh-CN": "算力：{amt}",
        "zh-TW": "算力：{amt}",
        "ko": "파워: {amt}",
        "en-US": "Power: {amt}",
        "ja": "パワー: {amt}",
        "th": "พลัง: {amt}",
    },
    "info_referrer": {
        "zh-CN": "推荐人：{addr}",
        "zh-TW": "推薦人：{addr}",
        "ko": "추천인: {addr}",
        "en-US": "Referrer: {addr}",
        "ja": "紹介者: {addr}",
        "th": "ผู้แนะนำ: {addr}",
    },
    "info_referrer_none": {
        "zh-CN": "推荐人：无",
        "zh-TW": "推薦人：無",
        "ko": "추천인: 없음",
        "en-US": "Referrer: None",
        "ja": "紹介者: なし",
        "th": "ผู้แนะนำ: ไม่มี",
    },
    "info_address_type": {
        "zh-CN": "地址类型：{kind}",
        "zh-TW": "地址類型：{kind}",
        "ko": "주소 유형: {kind}",
        "en-US": "Address Type: {kind}",
        "ja": "アドレスタイプ: {kind}",
        "th": "ประเภทที่อยู่: {kind}",
    },
    "info_address_type_eoa": {
        "zh-CN": "EOA",
        "zh-TW": "EOA",
        "ko": "EOA",
        "en-US": "EOA",
        "ja": "EOA",
        "th": "EOA",
    },
    "info_address_type_non_eoa": {
        "zh-CN": "非EOA",
        "zh-TW": "非EOA",
        "ko": "비 EOA",
        "en-US": "Non-EOA",
        "ja": "非EOA",
        "th": "ไม่ใช่ EOA",
    },
    "info_address_type_note_code": {
        "zh-CN": "说明：检测到链上合约代码，因此这不是传统无代码 EOA 地址。",
        "zh-TW": "說明：檢測到鏈上合約代碼，因此這不是傳統無代碼 EOA 地址。",
        "ko": "설명: 온체인 코드가 감지되어 이 주소는 전통적인 무코드 EOA 주소가 아닙니다.",
        "en-US": "Note: On-chain code was detected, so this is not a traditional code-free EOA address.",
        "ja": "説明: オンチェーンコードが検出されたため、このアドレスは従来のコードを持たない EOA ではありません。",
        "th": "หมายเหตุ: ตรวจพบโค้ดบนเชน ดังนั้นที่อยู่นี้จึงไม่ใช่ EOA แบบดั้งเดิมที่ไม่มีโค้ด",
    },
    "info_address_type_note_7702": {
        "zh-CN": "说明：检测到 EIP-7702 委托代码，因此这不是传统无代码 EOA 地址。委托目标：{target}",
        "zh-TW": "說明：檢測到 EIP-7702 委託代碼，因此這不是傳統無代碼 EOA 地址。委託目標：{target}",
        "ko": "설명: EIP-7702 위임 코드가 감지되어 이 주소는 전통적인 무코드 EOA 주소가 아닙니다. 위임 대상: {target}",
        "en-US": "Note: EIP-7702 delegation code was detected, so this is not a traditional code-free EOA address. Delegated target: {target}",
        "ja": "説明: EIP-7702 の委任コードが検出されたため、このアドレスは従来のコードを持たない EOA ではありません。委任先: {target}",
        "th": "หมายเหตุ: ตรวจพบโค้ดมอบหมาย EIP-7702 ดังนั้นที่อยู่นี้จึงไม่ใช่ EOA แบบดั้งเดิมที่ไม่มีโค้ด ปลายทางที่มอบหมาย: {target}",
    },
    "info_address_type_note_inactive": {
        "zh-CN": "说明：此地址没有任何链上活动，有可能误判。",
        "zh-TW": "說明：此地址沒有任何鏈上活動，有可能誤判。",
        "ko": "설명: 이 주소는 온체인 활동이 전혀 없어 오판 가능성이 있습니다.",
        "en-US": "Note: This address has no visible on-chain activity, so misclassification is possible.",
        "ja": "説明: このアドレスには確認できるオンチェーン活動がないため、誤判定の可能性があります。",
        "th": "หมายเหตุ: ที่อยู่นี้ไม่มีความเคลื่อนไหวบนเชนที่มองเห็นได้ จึงอาจตัดสินคลาดเคลื่อนได้",
    },
    "info_direct_count": {
        "zh-CN": "直推有效人数：{count}",
        "zh-TW": "直推有效人數：{count}",
        "ko": "직접 추천 유효 인원: {count}",
        "en-US": "Direct Referrals: {count}",
        "ja": "直接紹介有効人数: {count}",
        "th": "ผู้แนะนำโดยตรง: {count}",
    },
    "ev_deposit_hash_inflow": {
        "zh-CN": "入金哈希",
        "zh-TW": "入金哈希",
        "ko": "입금 해시",
        "en-US": "Deposit TxHash",
        "ja": "入金ハッシュ",
        "th": "แฮชการฝาก",
    },
    "ev_deposit_hash_bookkeeping": {
        "zh-CN": "记账哈希",
        "zh-TW": "記帳哈希",
        "ko": "기장 해시",
        "en-US": "Bookkeeping TxHash",
        "ja": "記帳ハッシュ",
        "th": "แฮชการบันทึก",
    },
    "ev_deposit_block_inflow": {
        "zh-CN": "入金区块高度",
        "zh-TW": "入金區塊高度",
        "ko": "입금 블록",
        "en-US": "Deposit Block",
        "ja": "入金ブロック",
        "th": "บล็อกการฝาก",
    },
    "ev_deposit_block_bookkeeping": {
        "zh-CN": "记账区块高度",
        "zh-TW": "記帳區塊高度",
        "ko": "기장 블록",
        "en-US": "Bookkeeping Block",
        "ja": "記帳ブロック",
        "th": "บล็อกการบันทึก",
    },
    "info_price_suffix": {
        "zh-CN": "此时 NAIO 价格：{price} USDT",
        "zh-TW": "此時 NAIO 價格：{price} USDT",
        "ko": "현재 NAIO 가격: {price} USDT",
        "en-US": "Current NAIO price: {price} USDT",
        "ja": "現在の NAIO 価格: {price} USDT",
        "th": "ราคา NAIO ขณะนี้: {price} USDT",
    },
    "info_downline_stats": {
        "zh-CN": "📈 团队总业绩",
        "zh-TW": "📈 團隊總業績",
        "ko": "📈 팀 총 실적",
        "en-US": "📈 Team Total Performance",
        "ja": "📈 チーム総業績",
        "th": "📈 ผลงานรวมของทีม",
    },
    "info_downline_last_week": {
        "zh-CN": "上周：{amt} USDT",
        "zh-TW": "上週：{amt} USDT",
        "ko": "지난주: {amt} USDT",
        "en-US": "Last Week: {amt} USDT",
        "ja": "先週: {amt} USDT",
        "th": "สัปดาห์ที่แล้ว: {amt} USDT",
    },
    "info_downline_this_week": {
        "zh-CN": "本周：{amt} USDT",
        "zh-TW": "本週：{amt} USDT",
        "ko": "이번주: {amt} USDT",
        "en-US": "This Week: {amt} USDT",
        "ja": "今週: {amt} USDT",
        "th": "สัปดาห์นี้: {amt} USDT",
    },
    "info_downline_this_month": {
        "zh-CN": "本月：{amt} USDT",
        "zh-TW": "本月：{amt} USDT",
        "ko": "이번달: {amt} USDT",
        "en-US": "This Month: {amt} USDT",
        "ja": "今月: {amt} USDT",
        "th": "เดือนนี้: {amt} USDT",
    },
    "info_downline_total": {
        "zh-CN": "总计：{amt} USDT",
        "zh-TW": "總計：{amt} USDT",
        "ko": "총계: {amt} USDT",
        "en-US": "Total: {amt} USDT",
        "ja": "合計: {amt} USDT",
        "th": "รวม: {amt} USDT",
    },
    "info_downline_query_failed": {
        "zh-CN": "团队业绩查询失败：{err}",
        "zh-TW": "團隊業績查詢失敗：{err}",
        "ko": "팀 실적 조회 실패: {err}",
        "en-US": "Team performance query failed: {err}",
        "ja": "チーム業績の照会に失敗: {err}",
        "th": "ดึงข้อมูลผลงานทีมล้มเหลว: {err}",
    },
    "info_new_user_reward": {
        "zh-CN": "🔄 复投奖励（上一已结算日）",
        "zh-TW": "🔄 復投獎勵（上一已結算日）",
        "ko": "🔄 재투자 보상(직전 정산일)",
        "en-US": "🔄 Re-invest Reward (Last Settled Day)",
        "ja": "🔄 再投資報酬（直近確定日）",
        "th": "🔄 รางวัลรีอินเวสต์ (วันปิดรอบล่าสุด)",
    },
    "info_new_user_day": {
        "zh-CN": "查询日(epoch)：{day}",
        "zh-TW": "查詢日(epoch)：{day}",
        "ko": "조회일(epoch): {day}",
        "en-US": "Query Day (epoch): {day}",
        "ja": "照会日(epoch): {day}",
        "th": "วันที่ตรวจสอบ (epoch): {day}",
    },
    "info_new_user_pool": {
        "zh-CN": "复投奖励总池：{amt} NAIO",
        "zh-TW": "復投獎勵總池：{amt} NAIO",
        "ko": "재투자 보상 총 풀: {amt} NAIO",
        "en-US": "Re-invest Reward Pool: {amt} NAIO",
        "ja": "再投資報酬総プール: {amt} NAIO",
        "th": "พูลรางวัลรีอินเวสต์รวม: {amt} NAIO",
    },
    "info_new_user_total_power": {
        "zh-CN": "复投总权重：{amt}",
        "zh-TW": "復投總權重：{amt}",
        "ko": "재투자 총 가중치: {amt}",
        "en-US": "Re-invest Total Weight: {amt}",
        "ja": "再投資総ウェイト: {amt}",
        "th": "น้ำหนักรวมรีอินเวสต์: {amt}",
    },
    "info_new_user_user_power": {
        "zh-CN": "该用户权重：{amt}",
        "zh-TW": "該用戶權重：{amt}",
        "ko": "해당 사용자 가중치: {amt}",
        "en-US": "This User Weight: {amt}",
        "ja": "当該ユーザーのウェイト: {amt}",
        "th": "น้ำหนักของผู้ใช้นี้: {amt}",
    },
    "info_new_user_estimated": {
        "zh-CN": "估算可领：{amt} NAIO",
        "zh-TW": "估算可領：{amt} NAIO",
        "ko": "예상 수령 가능: {amt} NAIO",
        "en-US": "Estimated Claimable: {amt} NAIO",
        "ja": "推定受取可能: {amt} NAIO",
        "th": "ประมาณการรับได้: {amt} NAIO",
    },
    "info_earnings": {
        "zh-CN": "💰 收益信息",
        "zh-TW": "💰 收益資訊",
        "ko": "💰 수익 정보",
        "en-US": "💰 Earnings",
        "ja": "💰 収益情報",
        "th": "💰 รายได้",
    },
    "info_withdraw": {
        "zh-CN": "📤 撤本信息",
        "zh-TW": "📤 撤本資訊",
        "ko": "📤 출금 정보",
        "en-US": "📤 Withdrawal Info",
        "ja": "📤 撤本情報",
        "th": "📤 ข้อมูลการถอน",
    },
    "info_first_deposit": {
        "zh-CN": "首次入金时间：{ts}",
        "zh-TW": "首次入金時間：{ts}",
        "ko": "최초 입금 시간: {ts}",
        "en-US": "First Deposit: {ts}",
        "ja": "初回入金時刻: {ts}",
        "th": "เวลาฝากครั้งแรก: {ts}",
    },
    "info_first_deposit_none": {
        "zh-CN": "首次入金时间：未入金",
        "zh-TW": "首次入金時間：未入金",
        "ko": "최초 입금 시간: 입금 없음",
        "en-US": "First Deposit: None",
        "ja": "初回入金時刻: 未入金",
        "th": "เวลาฝากครั้งแรก: ยังไม่ฝาก",
    },
    "info_locked": {
        "zh-CN": "锁定本金：{amt} USDT",
        "zh-TW": "鎖定本金：{amt} USDT",
        "ko": "잠금 원금: {amt} USDT",
        "en-US": "Locked Principal: {amt} USDT",
        "ja": "ロック元本: {amt} USDT",
        "th": "เงินต้นที่ล็อค: {amt} USDT",
    },
    "info_withdrawn": {
        "zh-CN": "已撤回：{amt} USDT",
        "zh-TW": "已撤回：{amt} USDT",
        "ko": "출금 완료: {amt} USDT",
        "en-US": "Withdrawn: {amt} USDT",
        "ja": "撤本済み: {amt} USDT",
        "th": "ถอนแล้ว: {amt} USDT",
    },
    "info_unlock_rate": {
        "zh-CN": "可撤比例：{rate}%",
        "zh-TW": "可撤比例：{rate}%",
        "ko": "출금 가능 비율: {rate}%",
        "en-US": "Unlock Rate: {rate}%",
        "ja": "撤本可能率: {rate}%",
        "th": "อัตราถอนได้: {rate}%",
    },
    "info_unlocked_by_time": {
        "zh-CN": "按时间解锁上限：{amt} USDT",
        "zh-TW": "按時間解鎖上限：{amt} USDT",
        "ko": "시간 기준 해제 상한: {amt} USDT",
        "en-US": "Time-unlocked cap: {amt} USDT",
        "ja": "時間基準アンロック上限: {amt} USDT",
        "th": "เพดานที่ปลดล็อกตามเวลา: {amt} USDT",
    },
    "info_withdrawable_now": {
        "zh-CN": "当前可申请撤本：{amt} USDT",
        "zh-TW": "當前可申請撤本：{amt} USDT",
        "ko": "현재 출금 신청 가능: {amt} USDT",
        "en-US": "Withdrawable now: {amt} USDT",
        "ja": "現在申請可能な撤本: {amt} USDT",
        "th": "ถอนได้ตอนนี้: {amt} USDT",
    },
    "info_queue": {
        "zh-CN": "撤本队列：{amt} USDT",
        "zh-TW": "撤本佇列：{amt} USDT",
        "ko": "출금 대기: {amt} USDT",
        "en-US": "Withdrawal Queue: {amt} USDT",
        "ja": "撤本キュー: {amt} USDT",
        "th": "คิวถอน: {amt} USDT",
    },
    "info_queue_none": {
        "zh-CN": "撤本队列：无",
        "zh-TW": "撤本佇列：無",
        "ko": "출금 대기: 없음",
        "en-US": "Withdrawal Queue: None",
        "ja": "撤本キュー: なし",
        "th": "คิวถอน: ไม่มี",
    },
    "info_trading": {
        "zh-CN": "📈 交易信息",
        "zh-TW": "📈 交易資訊",
        "ko": "📈 거래 정보",
        "en-US": "📈 Trading Info",
        "ja": "📈 取引情報",
        "th": "📈 ข้อมูลการซื้อขาย",
    },
    "info_total_sold": {
        "zh-CN": "累计卖出：{amt} USDT",
        "zh-TW": "累計賣出：{amt} USDT",
        "ko": "누적 판매: {amt} USDT",
        "en-US": "Total Sold: {amt} USDT",
        "ja": "累計売却: {amt} USDT",
        "th": "ขายสะสม: {amt} USDT",
    },
    "info_sell_multiple": {
        "zh-CN": "卖出倍数：{multiple}倍",
        "zh-TW": "賣出倍數：{multiple}倍",
        "ko": "판매 배수: {multiple}배",
        "en-US": "Sell Multiple: {multiple}x",
        "ja": "売却倍率: {multiple}倍",
        "th": "อัตราการขาย: {multiple} เท่า",
    },
    "info_sell_multiple_none": {
        "zh-CN": "卖出倍数：-",
        "zh-TW": "賣出倍數：-",
        "ko": "판매 배수: -",
        "en-US": "Sell Multiple: -",
        "ja": "売却倍率: -",
        "th": "อัตราการขาย: -",
    },
    "info_node": {
        "zh-CN": "🪙 节点信息",
        "zh-TW": "🪙 節點資訊",
        "ko": "🪙 노드 정보",
        "en-US": "🪙 Node Info",
        "ja": "🪙 ノード情報",
        "th": "🪙 ข้อมูลโหนด",
    },
    "info_node_seats": {
        "zh-CN": "节点席位：{count}",
        "zh-TW": "節點席位：{count}",
        "ko": "노드 좌석: {count}",
        "en-US": "Node Seats: {count}",
        "ja": "ノードシート: {count}",
        "th": "ที่นั่งโหนด: {count}",
    },
    "info_node_seats_none": {
        "zh-CN": "节点席位：无",
        "zh-TW": "節點席位：無",
        "ko": "노드 좌석: 없음",
        "en-US": "Node Seats: None",
        "ja": "ノードシート: なし",
        "th": "ที่นั่งโหนด: ไม่มี",
    },
    "help_info_query": {
        "zh-CN": "查询地址信息：\n群聊：@机器人 0x地址\n私聊：0x地址\n例如：@机器人 0x1234...5678\n（包含推荐关系、收益、基本信息等）",
        "zh-TW": "查詢地址資訊：\n群聊：@機器人 0x地址\n私聊：0x地址\n例如：@機器人 0x1234...5678\n（包含推薦關係、收益、基本信息等）",
        "ko": "주소 정보 조회:\n그룹: @봇 0x주소\n개인: 0x주소\n예: @봇 0x1234...5678\n(추천 관계, 수익, 기본 정보 포함)",
        "en-US": "Query address info:\nGroup: @botname 0xaddress\nPrivate: 0xaddress\nExample: @botname 0x1234...5678\n(Includes referral relation, earnings, basic info, etc.)",
        "ja": "アドレス情報照会:\nグループ: @ボット 0xアドレス\n個人: 0xアドレス\n例: @ボット 0x1234...5678\n(紹介関係、収益、基本情報を含む)",
        "th": "ตรวจสอบข้อมูลที่อยู่:\nกลุ่ม: @บอท 0xที่อยู่\nส่วนตัว: 0xที่อยู่\nตัวอย่าง: @บอท 0x1234...5678\n(รวมความสัมพันธ์ผู้แนะนำ รายได้ ข้อมูลพื้นฐาน)",
    },
    "ev_deposit": {
        "zh-CN": "✅ 新入金记账\n用户：`{user}`\n金额：{amt} USDT{block_str}{suffix}",
        "zh-TW": "✅ 新入金記帳\n用戶：`{user}`\n金額：{amt} USDT{block_str}{suffix}",
        "ko": "✅ 신규 입금 기록\n사용자: `{user}`\n금액: {amt} USDT{block_str}{suffix}",
        "en-US": "✅ Deposit recorded\nUser: `{user}`\nAmount: {amt} USDT{block_str}{suffix}",
        "ja": "✅ 入金記録\nユーザー: `{user}`\n金額: {amt} USDT{block_str}{suffix}",
        "th": "✅ บันทึกการฝาก\nผู้ใช้: `{user}`\nจำนวน: {amt} USDT{block_str}{suffix}",
    },
    "ev_refund": {
        "zh-CN": "⚠️ 入金已退款\n用户：`{user}`\n金额：{amt} USDT\n原因：{reason}（code={code}）\ntxHash：`{txh}`{block_str}{suffix}",
        "zh-TW": "⚠️ 入金已退款\n用戶：`{user}`\n金額：{amt} USDT\n原因：{reason}（code={code}）\ntxHash：`{txh}`{block_str}{suffix}",
        "ko": "⚠️ 입금 환불\n사용자: `{user}`\n금액: {amt} USDT\n사유: {reason} (code={code})\ntxHash: `{txh}`{block_str}{suffix}",
        "en-US": "⚠️ Deposit refunded\nUser: `{user}`\nAmount: {amt} USDT\nReason: {reason} (code={code})\ntxHash: `{txh}`{block_str}{suffix}",
        "ja": "⚠️ 入金返金\nユーザー: `{user}`\n金額: {amt} USDT\n理由: {reason} (code={code})\ntxHash: `{txh}`{block_str}{suffix}",
        "th": "⚠️ คืนเงินฝาก\nผู้ใช้: `{user}`\nจำนวน: {amt} USDT\nเหตุผล: {reason} (code={code})\ntxHash: `{txh}`{block_str}{suffix}",
    },
    "ev_deflation": {
        "zh-CN": "🔥 通缩执行\n时间：{ts}\nEpoch：{epoch}\n释放率：{rate_pct}（{rate_bps} bps）\n通缩总量：{deflation} NAIO\n黑洞：{burned} NAIO（价值 {burn_value} USDT）\n分配：生态 {eco} / 复投 {new_user} / 节点 {node} / 独立 {independent} / 推荐 {dynamic} / 静态 {static}\n本期撤本代烧：{withdraw_add} NAIO{block_str}{txline}{suffix}",
        "zh-TW": "🔥 通縮執行\n時間：{ts}\nEpoch：{epoch}\n釋放率：{rate_pct}（{rate_bps} bps）\n通縮總量：{deflation} NAIO\n黑洞：{burned} NAIO（價值 {burn_value} USDT）\n分配：生態 {eco} / 復投 {new_user} / 節點 {node} / 獨立 {independent} / 推薦 {dynamic} / 靜態 {static}\n本期撤本代燒：{withdraw_add} NAIO{block_str}{txline}{suffix}",
        "ko": "🔥 통축 실행\n시간: {ts}\nEpoch: {epoch}\n비율: {rate_pct} ({rate_bps} bps)\n총 통축량: {deflation} NAIO\n소각: {burned} NAIO (가치 {burn_value} USDT)\n분배: 에코 {eco} / 재투자 {new_user} / 노드 {node} / 독립 {independent} / 추천 {dynamic} / 정적 {static}\n이번 회차 출금 대행 소각: {withdraw_add} NAIO{block_str}{txline}{suffix}",
        "en-US": "🔥 Deflation executed\nTime: {ts}\nEpoch: {epoch}\nRate: {rate_pct} ({rate_bps} bps)\nDeflation total: {deflation} NAIO\nBurn: {burned} NAIO (value {burn_value} USDT)\nSplit: eco {eco} / reinvest {new_user} / node {node} / indep {independent} / referral {dynamic} / static {static}\nWithdraw-assisted burn: {withdraw_add} NAIO{block_str}{txline}{suffix}",
        "ja": "🔥 通縮実行\n時間: {ts}\nEpoch: {epoch}\n比率: {rate_pct}（{rate_bps} bps）\n通縮総量: {deflation} NAIO\nバーン: {burned} NAIO（価値 {burn_value} USDT）\n配分: エコ {eco} / 再投資 {new_user} / ノード {node} / 独立 {independent} / 紹介 {dynamic} / 静的 {static}\n本回の撤本代行バーン: {withdraw_add} NAIO{block_str}{txline}{suffix}",
        "th": "🔥 ดำเนินการ Deflation\nเวลา: {ts}\nEpoch: {epoch}\nอัตรา: {rate_pct} ({rate_bps} bps)\nยอด Deflation รวม: {deflation} NAIO\nเผา: {burned} NAIO (มูลค่า {burn_value} USDT)\nการกระจาย: eco {eco} / reinvest {new_user} / node {node} / indep {independent} / referral {dynamic} / static {static}\nการเผาแทนจากการถอน: {withdraw_add} NAIO{block_str}{txline}{suffix}",
    },
    "ev_static_claim": {
        "zh-CN": "🎁 领取静态成功\n用户：`{user}`\n数量：{amt} NAIO{block_str}{suffix}",
        "zh-TW": "🎁 領取靜態成功\n用戶：`{user}`\n數量：{amt} NAIO{block_str}{suffix}",
        "ko": "🎁 정적 수령 성공\n사용자: `{user}`\n수량: {amt} NAIO{block_str}{suffix}",
        "en-US": "🎁 Static claimed\nUser: `{user}`\nAmount: {amt} NAIO{block_str}{suffix}",
        "ja": "🎁 静的受領\nユーザー: `{user}`\n数量: {amt} NAIO{block_str}{suffix}",
        "th": "🎁 รับสเตติกสำเร็จ\nผู้ใช้: `{user}`\nจำนวน: {amt} NAIO{block_str}{suffix}",
    },
    "ev_dynamic_claim": {
        "zh-CN": "🎁 领取推荐/动态成功\n用户：`{user}`\n数量：{amt} NAIO{block_str}{suffix}",
        "zh-TW": "🎁 領取推薦/動態成功\n用戶：`{user}`\n數量：{amt} NAIO{block_str}{suffix}",
        "ko": "🎁 추천/동적 수령 성공\n사용자: `{user}`\n수량: {amt} NAIO{block_str}{suffix}",
        "en-US": "🎁 Referral/Dynamic claimed\nUser: `{user}`\nAmount: {amt} NAIO{block_str}{suffix}",
        "ja": "🎁 紹介/動的受領\nユーザー: `{user}`\n数量: {amt} NAIO{block_str}{suffix}",
        "th": "🎁 รับแนะนำ/ไดนามิกสำเร็จ\nผู้ใช้: `{user}`\nจำนวน: {amt} NAIO{block_str}{suffix}",
    },
    "ev_new_user_claim": {
        "zh-CN": "🎁 领取复投奖励成功\n用户：`{user}`\nEpoch：{day}\n数量：{amt} NAIO{block_str}{suffix}",
        "zh-TW": "🎁 領取復投獎勵成功\n用戶：`{user}`\nEpoch：{day}\n數量：{amt} NAIO{block_str}{suffix}",
        "ko": "🎁 재투자 보상 수령 성공\n사용자: `{user}`\nEpoch: {day}\n수량: {amt} NAIO{block_str}{suffix}",
        "en-US": "🎁 Reinvest reward claimed\nUser: `{user}`\nEpoch: {day}\nAmount: {amt} NAIO{block_str}{suffix}",
        "ja": "🎁 再投資報酬受領成功\nユーザー: `{user}`\nEpoch: {day}\n数量: {amt} NAIO{block_str}{suffix}",
        "th": "🎁 รับรางวัลรีอินเวสต์สำเร็จ\nผู้ใช้: `{user}`\nEpoch: {day}\nจำนวน: {amt} NAIO{block_str}{suffix}",
    },
    "ev_withdraw_queued": {
        "zh-CN": "📤 撤本申请入队\n用户：`{user}`\n申请金额：{amt} USDT{block_str}{suffix}",
        "zh-TW": "📤 撤本申請入隊\n用戶：`{user}`\n申請金額：{amt} USDT{block_str}{suffix}",
        "ko": "📤 출금 대기 등록\n사용자: `{user}`\n신청 금액: {amt} USDT{block_str}{suffix}",
        "en-US": "📤 Withdraw queued\nUser: `{user}`\nAmount: {amt} USDT{block_str}{suffix}",
        "ja": "📤 撤本キュー登録\nユーザー: `{user}`\n申請金額: {amt} USDT{block_str}{suffix}",
        "th": "📤 เข้าคิวถอน\nผู้ใช้: `{user}`\nจำนวน: {amt} USDT{block_str}{suffix}",
    },
    "ev_withdraw_processed": {
        "zh-CN": "✅ 撤本执行\n用户：`{user}`\n返还：{usdt} USDT\n黑洞：{burn} NAIO\n当期已用代烧：{daily_used} NAIO\n当期剩余代烧：{daily_remain} NAIO{block_str}{suffix}",
        "zh-TW": "✅ 撤本執行\n用戶：`{user}`\n返還：{usdt} USDT\n黑洞：{burn} NAIO\n當期已用代燒：{daily_used} NAIO\n當期剩餘代燒：{daily_remain} NAIO{block_str}{suffix}",
        "ko": "✅ 출금 처리\n사용자: `{user}`\n환급: {usdt} USDT\n소각: {burn} NAIO\n당기 사용 소각량: {daily_used} NAIO\n당기 남은 소각량: {daily_remain} NAIO{block_str}{suffix}",
        "en-US": "✅ Withdraw processed\nUser: `{user}`\nReturned: {usdt} USDT\nBurn: {burn} NAIO\nCurrent period burn used: {daily_used} NAIO\nCurrent period burn remaining: {daily_remain} NAIO{block_str}{suffix}",
        "ja": "✅ 撤本実行\nユーザー: `{user}`\n返還: {usdt} USDT\nバーン: {burn} NAIO\n当期使用済み代行バーン: {daily_used} NAIO\n当期残り代行バーン: {daily_remain} NAIO{block_str}{suffix}",
        "th": "✅ ถอนสำเร็จ\nผู้ใช้: `{user}`\nคืน: {usdt} USDT\nเผา: {burn} NAIO\nเผาแทนที่ใช้แล้วช่วงนี้: {daily_used} NAIO\nเผาแทนคงเหลือช่วงนี้: {daily_remain} NAIO{block_str}{suffix}",
    },
    "ev_sell": {
        "zh-CN": "💱 卖出成功\n用户：`{user}`\n卖出：{sold} NAIO\n到账：{usdt} USDT\n黑洞：{burn} NAIO{block_str}{suffix}",
        "zh-TW": "💱 賣出成功\n用戶：`{user}`\n賣出：{sold} NAIO\n到帳：{usdt} USDT\n黑洞：{burn} NAIO{block_str}{suffix}",
        "ko": "💱 매도 성공\n사용자: `{user}`\n매도: {sold} NAIO\n수령: {usdt} USDT\n소각: {burn} NAIO{block_str}{suffix}",
        "en-US": "💱 Sell executed\nUser: `{user}`\nSold: {sold} NAIO\nReceived: {usdt} USDT\nBurn: {burn} NAIO{block_str}{suffix}",
        "ja": "💱 売却成功\nユーザー: `{user}`\n売却: {sold} NAIO\n受取: {usdt} USDT\nバーン: {burn} NAIO{block_str}{suffix}",
        "th": "💱 ขายสำเร็จ\nผู้ใช้: `{user}`\nขาย: {sold} NAIO\nได้รับ: {usdt} USDT\nเผา: {burn} NAIO{block_str}{suffix}",
    },
    "ev_referral_bound": {
        "zh-CN": "🔗 绑定推荐成功\n用户：`{user}`\n上级：`{inviter}`{block_str}{suffix}",
        "zh-TW": "🔗 綁定推薦成功\n用戶：`{user}`\n上級：`{inviter}`{block_str}{suffix}",
        "ko": "🔗 추천 바인딩 성공\n사용자: `{user}`\n상위: `{inviter}`{block_str}{suffix}",
        "en-US": "🔗 Referral bound\nUser: `{user}`\nReferrer: `{inviter}`{block_str}{suffix}",
        "ja": "🔗 紹介バインド成功\nユーザー: `{user}`\n上位: `{inviter}`{block_str}{suffix}",
        "th": "🔗 ผูกผู้แนะนำสำเร็จ\nผู้ใช้: `{user}`\nผู้แนะนำ: `{inviter}`{block_str}{suffix}",
    },
    "ev_node_claim": {
        "zh-CN": "🏆 节点分红领取\n用户：`{user}`\nUSDT：{usdt}\nNAIO：{naio}{block_str}{suffix}",
        "zh-TW": "🏆 節點分紅領取\n用戶：`{user}`\nUSDT：{usdt}\nNAIO：{naio}{block_str}{suffix}",
        "ko": "🏆 노드 분배 수령\n사용자: `{user}`\nUSDT: {usdt}\nNAIO: {naio}{block_str}{suffix}",
        "en-US": "🏆 Node dividend claimed\nUser: `{user}`\nUSDT: {usdt}\nNAIO: {naio}{block_str}{suffix}",
        "ja": "🏆 ノード配当受領\nユーザー: `{user}`\nUSDT: {usdt}\nNAIO: {naio}{block_str}{suffix}",
        "th": "🏆 รับปันผลโหนด\nผู้ใช้: `{user}`\nUSDT: {usdt}\nNAIO: {naio}{block_str}{suffix}",
    },
    "ev_tx_failed_out_of_gas": {
        "zh-CN": "⚠️ 主合约交易失败：手续费不足（gas 给低了）\n发起方：`{from_addr}`\n用户设置 gas limit：{gas_limit_tx}\n已消耗 gas：{gas_used}（{gas_pct}%）{block_str}\n操作建议：请将 gas limit 提高到 **{gas_limit}** 以上后重试。",
        "zh-TW": "⚠️ 主合約交易失敗：手續費不足（gas 給低了）\n發起方：`{from_addr}`\n用戶設置 gas limit：{gas_limit_tx}\n已消耗 gas：{gas_used}（{gas_pct}%）{block_str}\n操作建議：請將 gas limit 提高到 **{gas_limit}** 以上後重試。",
        "en-US": "⚠️ Controller tx failed: insufficient gas (gas limit too low)\nFrom: `{from_addr}`\nUser gas limit: {gas_limit_tx}\nGas used: {gas_used} ({gas_pct}%){block_str}\nSuggestion: increase gas limit to **{gas_limit}** or higher and retry.",
        "ko": "⚠️ 컨트롤러 트랜잭션 실패: 가스 부족 (gas limit이 너무 낮음)\n발신: `{from_addr}`\n사용자 gas limit: {gas_limit_tx}\n사용된 gas: {gas_used} ({gas_pct}%){block_str}\n권장: gas limit을 **{gas_limit}** 이상으로 올린 후 다시 시도하세요.",
        "ja": "⚠️ コントローラー取引失敗：ガス不足（gas limit が低すぎます）\n送信元：`{from_addr}`\nユーザー gas limit: {gas_limit_tx}\n消費 gas: {gas_used} ({gas_pct}%){block_str}\n推奨: gas limit を **{gas_limit}** 以上に設定して再試行してください。",
        "th": "⚠️ ธุรกรรมคอนโทรลเลอร์ล้มเหลว: gas ไม่เพียงพอ (gas limit ต่ำเกินไป)\nผู้ส่ง: `{from_addr}`\ngas limit ผู้ใช้: {gas_limit_tx}\ngas ที่ใช้: {gas_used} ({gas_pct}%){block_str}\nคำแนะนำ: เพิ่ม gas limit เป็น **{gas_limit}** ขึ้นไปแล้วลองใหม่",
    },
    "broadcast_started": {
        "zh-CN": "📣 播报服务已启动（自动向所有已加入的群播报）。",
        "zh-TW": "📣 播報服務已啟動（自動向所有已加入的群播報）。",
        "ko": "📣 방송 서비스가 시작되었습니다(등록된 그룹에 자동 알림).",
        "en-US": "📣 Broadcast service started (auto posting to joined groups).",
        "ja": "📣 ブロードキャスト開始（参加済みグループに自動投稿）。",
        "th": "📣 เริ่มระบบประกาศแล้ว (ส่งอัตโนมัติไปยังกลุ่มที่เข้าร่วม)",
    },
    "bot_init_failed": {
        "zh-CN": "Bot 初始化失败",
        "zh-TW": "Bot 初始化失敗",
        "ko": "봇 초기화 실패",
        "en-US": "Bot initialization failed",
        "ja": "Bot 初期化に失敗しました",
        "th": "การเริ่มต้นบอทล้มเหลว",
    },
    "broadcast_disabled": {
        "zh-CN": "入金播报未启用（TELEGRAM_BOT_BROADCAST_DEPOSITS=false）",
        "zh-TW": "入金播報未啟用（TELEGRAM_BOT_BROADCAST_DEPOSITS=false）",
        "ko": "입금 알림이 비활성화됨 (TELEGRAM_BOT_BROADCAST_DEPOSITS=false)",
        "en-US": "Deposit broadcast disabled (TELEGRAM_BOT_BROADCAST_DEPOSITS=false)",
        "ja": "入金通知が無効です（TELEGRAM_BOT_BROADCAST_DEPOSITS=false）",
        "th": "ปิดการประกาศฝาก (TELEGRAM_BOT_BROADCAST_DEPOSITS=false)",
    },
    "subscribe_ok": {
        "zh-CN": "已订阅入金播报 ✅",
        "zh-TW": "已訂閱入金播報 ✅",
        "ko": "입금 알림 구독 완료 ✅",
        "en-US": "Subscribed to deposit broadcast ✅",
        "ja": "入金通知を購読しました ✅",
        "th": "สมัครรับประกาศการฝากแล้ว ✅",
    },
    "subscribe_already": {
        "zh-CN": "你已订阅过入金播报",
        "zh-TW": "你已訂閱過入金播報",
        "ko": "이미 입금 알림을 구독했습니다",
        "en-US": "Already subscribed to deposit broadcast",
        "ja": "既に入金通知を購読しています",
        "th": "สมัครรับประกาศการฝากแล้ว",
    },
    "unsubscribe_ok": {
        "zh-CN": "已取消订阅入金播报",
        "zh-TW": "已取消訂閱入金播報",
        "ko": "입금 알림 구독 해제",
        "en-US": "Unsubscribed from deposit broadcast",
        "ja": "入金通知の購読を解除しました",
        "th": "ยกเลิกการสมัครประกาศการฝากแล้ว",
    },
    "unsubscribe_none": {
        "zh-CN": "你未订阅入金播报",
        "zh-TW": "你未訂閱入金播報",
        "ko": "입금 알림을 구독하지 않았습니다",
        "en-US": "You are not subscribed to deposit broadcast",
        "ja": "入金通知を購読していません",
        "th": "คุณยังไม่ได้สมัครประกาศการฝาก",
    },
    "unknown_cmd": {
        "zh-CN": "未知指令",
        "zh-TW": "未知指令",
        "ko": "알 수 없는 명령",
        "en-US": "Unknown command",
        "ja": "不明なコマンド",
        "th": "คำสั่งไม่รู้จัก",
    },
    "query_failed": {
        "zh-CN": "查询失败：{err}",
        "zh-TW": "查詢失敗：{err}",
        "ko": "조회 실패: {err}",
        "en-US": "Query failed: {err}",
        "ja": "照会失敗: {err}",
        "th": "ค้นหาล้มเหลว: {err}",
    },
    "querying": {
        "zh-CN": "查询中，请稍候…",
        "zh-TW": "查詢中，請稍候…",
        "ko": "조회 중입니다. 잠시만 기다려 주세요…",
        "en-US": "Querying, please wait…",
        "ja": "照会中です。しばらくお待ちください…",
        "th": "กำลังค้นหา กรุณารอสักครู่…",
    },
    "query_timeout": {
        "zh-CN": "查询超时，请稍后再试。",
        "zh-TW": "查詢超時，請稍後再試。",
        "ko": "조회 시간이 초과되었습니다. 잠시 후 다시 시도해주세요.",
        "en-US": "Query timed out. Please try again later.",
        "ja": "照会がタイムアウトしました。後で再試行してください。",
        "th": "หมดเวลาในการค้นหา โปรดลองอีกครั้งภายหลัง",
    },
    "nodes_unavailable": {
        "zh-CN": "节点列表文件暂不可用，请联系管理员。",
        "zh-TW": "節點列表文件暫不可用，請聯繫管理員。",
        "ko": "노드 목록 파일을 사용할 수 없습니다. 관리자에게 문의하세요.",
        "en-US": "Node list file is not available. Contact admin.",
        "ja": "ノード一覧ファイルが利用できません。管理者に連絡してください。",
        "th": "ไฟล์รายการโหนดไม่พร้อมใช้งาน ติดต่อผู้ดูแล",
    },
    "nodes_sent": {
        "zh-CN": "已发送 1000 节点地址列表。",
        "zh-TW": "已發送 1000 節點地址列表。",
        "ko": "1000개 노드 주소 목록을 전송했습니다.",
        "en-US": "Sent 1000 node address list.",
        "ja": "1000ノードアドレス一覧を送信しました。",
        "th": "ส่งรายการที่อยู่โหนด 1000 รายการแล้ว",
    },
    "refund_reason_lt100": {
        "zh-CN": "金额 < 100 USDT（不合规，自动原路退回）",
        "zh-TW": "金額 < 100 USDT（不合規，自動原路退回）",
        "ko": "금액 < 100 USDT (부적합, 자동 환불)",
        "en-US": "Amount < 100 USDT (invalid, auto-refund)",
        "ja": "金額 < 100 USDT（不適合、自動返金）",
        "th": "จำนวน < 100 USDT (ไม่ถูกต้อง คืนอัตโนมัติ)",
    },
    "refund_reason_gt1000": {
        "zh-CN": "底池 < 100万 且金额 > 1000 USDT（不合规，自动原路退回）",
        "zh-TW": "底池 < 100萬 且金額 > 1000 USDT（不合規，自動原路退回）",
        "ko": "풀 < 100만 이고 금액 > 1000 USDT (부적합, 자동 환불)",
        "en-US": "Pool < 1,000,000 and amount > 1,000 USDT (invalid, auto-refund)",
        "ja": "プール < 100万 かつ 金額 > 1000 USDT（不適合、自動返金）",
        "th": "พูล < 1,000,000 และจำนวน > 1,000 USDT (ไม่ถูกต้อง คืนอัตโนมัติ)",
    },
    "refund_reason_paused": {
        "zh-CN": "已禁止入金，等待恢复（自动原路退回）",
        "zh-TW": "已禁止入金，等待恢復（自動原路退回）",
        "ko": "입금이 중지되어 있습니다. 복구를 기다려 주세요 (자동 환불)",
        "en-US": "Deposits are disabled after veto. Please wait for recovery (auto-refund).",
        "ja": "入金は禁止中です。再開までお待ちください（自動返金）",
        "th": "ปิดรับฝากหลังการยับยั้ง โปรดรอการกู้คืน (คืนอัตโนมัติ)",
    },
    "refund_reason_unknown": {
        "zh-CN": "未知原因（reason code 未识别）",
        "zh-TW": "未知原因（reason code 未識別）",
        "ko": "알 수 없는 사유 (code 미인식)",
        "en-US": "Unknown reason (code not recognized)",
        "ja": "不明な理由（code未認識）",
        "th": "เหตุผลไม่ทราบ (ไม่รู้จักรหัส)",
    },
}

def _t(lang: str, key: str, **kwargs) -> str:
    table = TRANSLATIONS.get(key) or {}
    text = table.get(lang) or table.get(DEFAULT_LANG) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

ERC20_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "from", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "to", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
]

NODE_SEAT_POOL_ABI = ERC20_ABI + [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "uint16", "name": "seatId", "type": "uint16"},
            {"indexed": True, "internalType": "address", "name": "owner", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "usdtAmount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "naioAmount", "type": "uint256"},
        ],
        "name": "Claimed",
        "type": "event",
    },
    {
        "inputs": [{"internalType": "address", "name": "owner_", "type": "address"}],
        "name": "pendingUsdt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "owner_", "type": "address"}],
        "name": "pendingNaio",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

NAIO_ABI = ERC20_ABI + [
    {
        "inputs": [],
        "name": "burnAddress",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]

RULE_ENGINE_ABI = [
    {
        "inputs": [],
        "name": "rulePoolUsdt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

KEEPER_COUNCIL_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "members",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

CONTROLLER_ABI = [
    {
        "inputs": [],
        "name": "getPrice",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "naio",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "systemStartTs",
        "outputs": [{"internalType": "uint64", "name": "", "type": "uint64"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "lastPokeEpoch",
        "outputs": [{"internalType": "uint32", "name": "", "type": "uint32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getCurrentEpoch",
        "outputs": [{"internalType": "uint32", "name": "", "type": "uint32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "lastDeflationSnapshotEpoch",
        "outputs": [{"internalType": "uint32", "name": "", "type": "uint32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint32", "name": "", "type": "uint32"}],
        "name": "deflationSnapshots",
        "outputs": [
            {"internalType": "uint32", "name": "epoch", "type": "uint32"},
            {"internalType": "uint64", "name": "timestamp", "type": "uint64"},
            {"internalType": "uint256", "name": "rateBps", "type": "uint256"},
            {"internalType": "uint256", "name": "priceBefore", "type": "uint256"},
            {"internalType": "uint256", "name": "poolTokenAmount", "type": "uint256"},
            {"internalType": "uint256", "name": "deflationAmount", "type": "uint256"},
            {"internalType": "uint256", "name": "burnAmount", "type": "uint256"},
            {"internalType": "uint256", "name": "ecoAmount", "type": "uint256"},
            {"internalType": "uint256", "name": "newUserAmount", "type": "uint256"},
            {"internalType": "uint256", "name": "nodeAmount", "type": "uint256"},
            {"internalType": "uint256", "name": "independentAmount", "type": "uint256"},
            {"internalType": "uint256", "name": "referralAmount", "type": "uint256"},
            {"internalType": "uint256", "name": "staticAmount", "type": "uint256"},
            {"internalType": "uint256", "name": "withdrawBurnConsumed", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "referralRewardExcluded",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "depositRuleEngine",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {"inputs": [], "name": "owner", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "keeper", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "nodePool", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "marketPool", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "opsPool", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "ecoPool", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "independentPool", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "validatorGuardian", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [],
        "name": "reservedUsdt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "reservedNaio",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "referralPoolNaio",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "withdrawBurnEpoch",
        "outputs": [{"internalType": "uint32", "name": "", "type": "uint32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "withdrawBurnQuotaToken",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "withdrawBurnUsedToken",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "withdrawQueuedAmount",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "totalSoldUsdt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },

    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "pendingStaticNaio",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "pendingNaio",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "pendingUsdt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "accRewardPerPower",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "users",
        "outputs": [
            {"internalType": "uint256", "name": "principalUsdt", "type": "uint256"},
            {"internalType": "uint256", "name": "power", "type": "uint256"},
            {"internalType": "address", "name": "referrer", "type": "address"},
            {"internalType": "uint16", "name": "directCount", "type": "uint16"},
            {"internalType": "uint64", "name": "lastClaimTs", "type": "uint64"},
            {"internalType": "uint64", "name": "firstDepositTs", "type": "uint64"},
            {"internalType": "uint256", "name": "rewardDebt", "type": "uint256"},
            {"internalType": "uint32", "name": "lastDepositEpoch", "type": "uint32"},
            {"internalType": "uint32", "name": "powerSnapEpoch", "type": "uint32"},
            {"internalType": "uint256", "name": "powerSnapAtDayStart", "type": "uint256"},
            {"internalType": "uint256", "name": "withdrawnUsdt", "type": "uint256"},
            {"internalType": "uint256", "name": "lockedUsdt", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "totalClaimedEarningsUsdt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint32", "name": "", "type": "uint32"}],
        "name": "newUserRewardNaioByDay",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint32", "name": "", "type": "uint32"}],
        "name": "newUserTotalPowerByDay",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint32", "name": "", "type": "uint32"},
            {"internalType": "address", "name": "", "type": "address"},
        ],
        "name": "newUserEligiblePower",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },

    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "user", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "usdtAmount", "type": "uint256"},
            {"indexed": True, "internalType": "bytes32", "name": "txHash", "type": "bytes32"},
        ],
        "name": "DepositFromTransfer",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "user", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "usdtAmount", "type": "uint256"},
            {"indexed": True, "internalType": "bytes32", "name": "txHash", "type": "bytes32"},
            {"indexed": False, "internalType": "uint8", "name": "reason", "type": "uint8"},
        ],
        "name": "DepositRefunded",
        "type": "event",
    },

    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "user", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "StaticRewardClaimed",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "user", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "DynamicRewardClaimed",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "user", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "WithdrawQueued",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "user", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "usdtReturned", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "naioBurned", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "dailyBurnUsed", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "dailyBurnRemaining", "type": "uint256"},
        ],
        "name": "WithdrawProcessed",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "user", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "lpAmount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "usdtReturned", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "tokenBurned", "type": "uint256"},
        ],
        "name": "LPWithdrawn",
        "type": "event",
    },

    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "uint256", "name": "timestamp", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "rateBps", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "tokenBurned", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "staticReward", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "dynamicReward", "type": "uint256"},
        ],
        "name": "DeflationExecuted",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "uint32", "name": "epoch", "type": "uint32"},
            {"indexed": True, "internalType": "uint256", "name": "timestamp", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "rateBps", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "priceBefore", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "poolTokenAmount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "deflationAmount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "burnAmount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "ecoAmount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "newUserAmount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "nodeAmount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "independentAmount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "referralAmount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "staticAmount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "withdrawBurnConsumed", "type": "uint256"},
        ],
        "name": "DeflationExecutedDetailed",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "user", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "inviter", "type": "address"},
        ],
        "name": "ReferralBound",
        "type": "event",
    },
]

@dataclass
class PersistState:

    chats: dict[str, dict]

    subscribers: list[int]
    cursor_block: int
    burn_cursor_block: int
    burned_naio_wei: int

    burn_start_block: int
    naio_address: str
    burn_address: str

    epoch_id: int
    epoch_start_block: int
    epoch_deposit_usdt: int
    epoch_sell_usdt: int
    epoch_last_block: int

    queue_cursor_block: int
    queue_pending_usdt: int
    queue_users_count: int
    queue_user_remaining: dict[str, int]

def _load_state(path: str) -> PersistState:
    try:
        with open(path, "r", encoding="utf-8") as f:
            j = json.load(f)
        subs = j.get("subscribers") or []
        cursor = int(j.get("cursor_block") or 0)
        burn_cursor = int(j.get("burn_cursor_block") or 0)
        burned_wei = int(j.get("burned_naio_wei") or 0)
        burn_start_block = int(j.get("burn_start_block") or 0)
        naio_address = str(j.get("naio_address") or "")
        burn_address = str(j.get("burn_address") or "")
        epoch_id = int(j.get("epoch_id") or 0)
        epoch_start_block = int(j.get("epoch_start_block") or 0)
        epoch_deposit_usdt = int(j.get("epoch_deposit_usdt") or 0)
        epoch_sell_usdt = int(j.get("epoch_sell_usdt") or 0)
        epoch_last_block = int(j.get("epoch_last_block") or 0)
        queue_cursor_block = int(j.get("queue_cursor_block") or 0)
        queue_pending_usdt = int(j.get("queue_pending_usdt") or 0)
        queue_users_count = int(j.get("queue_users_count") or 0)
        queue_user_remaining_raw = j.get("queue_user_remaining") or {}
        if not isinstance(queue_user_remaining_raw, dict):
            queue_user_remaining_raw = {}
        queue_user_remaining: dict[str, int] = {}
        for k, v in queue_user_remaining_raw.items():
            try:
                vv = int(v)
                if vv > 0:
                    queue_user_remaining[str(k).lower()] = vv
            except Exception:
                continue
        chats = j.get("chats") or {}
        subs2: list[int] = []
        for x in subs:
            try:
                subs2.append(int(x))
            except Exception:
                pass

        if not isinstance(chats, dict):
            chats = {}
        for cid in subs2:
            k = str(int(cid))
            chats.setdefault(k, {"type": "unknown", "title": "", "broadcast": True})
        return PersistState(
            chats=chats,
            subscribers=sorted(set(subs2)),
            cursor_block=cursor,
            burn_cursor_block=burn_cursor,
            burned_naio_wei=burned_wei,
            burn_start_block=burn_start_block,
            naio_address=naio_address,
            burn_address=burn_address,
            epoch_id=epoch_id,
            epoch_start_block=epoch_start_block,
            epoch_deposit_usdt=epoch_deposit_usdt,
            epoch_sell_usdt=epoch_sell_usdt,
            epoch_last_block=epoch_last_block,
            queue_cursor_block=queue_cursor_block,
            queue_pending_usdt=queue_pending_usdt,
            queue_users_count=queue_users_count,
            queue_user_remaining=queue_user_remaining,
        )
    except FileNotFoundError:
        return PersistState(
            chats={},
            subscribers=[],
            cursor_block=0,
            burn_cursor_block=0,
            burned_naio_wei=0,
            burn_start_block=0,
            naio_address="",
            burn_address="",
            epoch_id=0,
            epoch_start_block=0,
            epoch_deposit_usdt=0,
            epoch_sell_usdt=0,
            epoch_last_block=0,
            queue_cursor_block=0,
            queue_pending_usdt=0,
            queue_users_count=0,
            queue_user_remaining={},
        )
    except Exception:
        return PersistState(
            chats={},
            subscribers=[],
            cursor_block=0,
            burn_cursor_block=0,
            burned_naio_wei=0,
            burn_start_block=0,
            naio_address="",
            burn_address="",
            epoch_id=0,
            epoch_start_block=0,
            epoch_deposit_usdt=0,
            epoch_sell_usdt=0,
            epoch_last_block=0,
            queue_cursor_block=0,
            queue_pending_usdt=0,
            queue_users_count=0,
            queue_user_remaining={},
        )

def _save_state(path: str, st: PersistState) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "chats": st.chats,
                "subscribers": st.subscribers,
                "cursor_block": st.cursor_block,
                "burn_cursor_block": st.burn_cursor_block,
                "burned_naio_wei": st.burned_naio_wei,
                "burn_start_block": st.burn_start_block,
                "naio_address": st.naio_address,
                "burn_address": st.burn_address,
                "epoch_id": st.epoch_id,
                "epoch_start_block": st.epoch_start_block,
                "epoch_deposit_usdt": st.epoch_deposit_usdt,
                "epoch_sell_usdt": st.epoch_sell_usdt,
                "epoch_last_block": st.epoch_last_block,
                "queue_cursor_block": st.queue_cursor_block,
                "queue_pending_usdt": st.queue_pending_usdt,
                "queue_users_count": st.queue_users_count,
                "queue_user_remaining": st.queue_user_remaining,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

class BotState:
    def __init__(self) -> None:
        load_dotenv(override=True)
        logger.info("web3.py version: %s", web3.__version__)

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")
        self.telegram_token = token

        controller_addr = os.getenv("CONTROLLER_ADDRESS", "").strip()
        usdt_addr = os.getenv("USDT_ADDRESS", "").strip()
        node_seat_pool_addr = os.getenv("NODE_SEAT_POOL_ADDRESS", "").strip()
        pool_seeder_addr = os.getenv("INITIAL_POOL_SEEDER_ADDRESS", "").strip()
        if not controller_addr:
            raise RuntimeError("Missing CONTROLLER_ADDRESS in .env")
        if not usdt_addr:
            raise RuntimeError("Missing USDT_ADDRESS in .env")

        self.controller = Web3.to_checksum_address(controller_addr)
        self.usdt = Web3.to_checksum_address(usdt_addr)
        self.node_seat_pool = Web3.to_checksum_address(node_seat_pool_addr) if node_seat_pool_addr else None
        self.pool_seeder = Web3.to_checksum_address(pool_seeder_addr) if pool_seeder_addr else None

        self.state_file = os.getenv("TELEGRAM_BOT_STATE_FILE", "telegram_bot_state.json").strip() or "telegram_bot_state.json"
        self.broadcast_deposits_enabled = _env_bool("TELEGRAM_BOT_BROADCAST_DEPOSITS", True)

        self.broadcast_deflation_enabled = _env_bool("TELEGRAM_BOT_BROADCAST_DEFLATION", self.broadcast_deposits_enabled)
        self.broadcast_ops_enabled = _env_bool("TELEGRAM_BOT_BROADCAST_OPS", self.broadcast_deposits_enabled)
        self.broadcast_failed_out_of_gas = _env_bool("TELEGRAM_BOT_BROADCAST_FAILED_OOG", True)
        self.broadcast_any_enabled = bool(
            self.broadcast_deposits_enabled
            or self.broadcast_deflation_enabled
            or self.broadcast_ops_enabled
            or self.broadcast_failed_out_of_gas
        )
        self.broadcast_confirmations = int(os.getenv("CONFIRMATIONS", "3"))
        if self.broadcast_confirmations < 0:
            self.broadcast_confirmations = 0
        self.poll_interval = int(os.getenv("TELEGRAM_BOT_POLL_INTERVAL", "8"))
        self.max_blocks_per_scan = int(os.getenv("TELEGRAM_BOT_MAX_BLOCKS_PER_SCAN", "2000"))
        self.witness_hub_server_url = (os.getenv("WITNESS_HUB_SERVER_URL", "").strip() or "").rstrip("/")
        self.witness_hub_api_key = os.getenv("WITNESS_HUB_API_KEY", "").strip()
        self.witness_hub_timeout_seconds = max(2, int(os.getenv("WITNESS_HUB_TIMEOUT_SECONDS", "8")))

        rpc_url = _pick_rpc_url()
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        _inject_poa_middleware(self.w3)
        if not self.w3.is_connected():
            raise RuntimeError(f"RPC not connected: {rpc_url}")
        logger.info("RPC: %s", rpc_url)
        logger.info("Controller: %s", self.controller)
        logger.info("USDT: %s", self.usdt)
        if self.pool_seeder:
            logger.info("InitialPoolSeeder: %s", self.pool_seeder)

        self.controller_contract = self.w3.eth.contract(address=self.controller, abi=CONTROLLER_ABI)
        self.usdt_contract = self.w3.eth.contract(address=self.usdt, abi=ERC20_ABI)
        self.node_seat_pool_contract = (
            self.w3.eth.contract(address=self.node_seat_pool, abi=NODE_SEAT_POOL_ABI) if self.node_seat_pool else None
        )

        naio_env = os.getenv("NAIO_TOKEN_ADDRESS", "").strip()
        if naio_env:
            self.naio = Web3.to_checksum_address(naio_env)
        else:
            self.naio = Web3.to_checksum_address(self.controller_contract.functions.naio().call())
        self.naio_contract = self.w3.eth.contract(address=self.naio, abi=NAIO_ABI)
        logger.info("NAIO: %s", self.naio)

        self.rule_engine_contract = None
        try:
            engine_addr = self.controller_contract.functions.depositRuleEngine().call()
            if engine_addr and int(engine_addr, 16) != 0:
                engine_addr = Web3.to_checksum_address(engine_addr)
                self.rule_engine_contract = self.w3.eth.contract(address=engine_addr, abi=RULE_ENGINE_ABI)
                logger.info("RuleEngine: %s", engine_addr)
        except Exception:
            self.rule_engine_contract = None

        try:
            excluded = self.controller_contract.functions.referralRewardExcluded().call()
            self.referral_bootstrap_address = (
                Web3.to_checksum_address(excluded)
                if excluded and int(excluded, 16) != 0
                else None
            )
        except Exception:
            self.referral_bootstrap_address = None
        if self.referral_bootstrap_address:
            logger.info("Referral bootstrap(excluded) address: %s", self.referral_bootstrap_address)

        try:
            self.burn_address = Web3.to_checksum_address(self.naio_contract.functions.burnAddress().call())
        except Exception:
            self.burn_address = Web3.to_checksum_address("0x0000000000000000000000000000000000000000")
        logger.info("Burn address: %s", self.burn_address)

        self.usdt_decimals = self._safe_decimals(self.usdt_contract, default=18)
        self.naio_decimals = self._safe_decimals(self.naio_contract, default=18)

        self.display_usdt_decimals = int(os.getenv("TELEGRAM_BOT_DISPLAY_USDT_DECIMALS", "2"))
        self.display_naio_decimals = int(os.getenv("TELEGRAM_BOT_DISPLAY_NAIO_DECIMALS", "4"))
        self.display_price_decimals = int(os.getenv("TELEGRAM_BOT_DISPLAY_PRICE_DECIMALS", "8"))

        self.price_db_path = os.getenv("PRICE_DB_PATH", "price_history.db").strip() or "price_history.db"
        self.price_retention_days = int(os.getenv("PRICE_RETENTION_DAYS", "7"))
        self.price_record_interval = max(1, int(os.getenv("PRICE_RECORD_INTERVAL", "1")))
        self.price_candle_interval = max(60, int(os.getenv("PRICE_CANDLE_INTERVAL", "60")))

        self.epoch_seconds = int(os.getenv("EPOCH_SECONDS", "86400"))
        if self.epoch_seconds < 60:
            self.epoch_seconds = 86400
        self._init_price_db()

        self._persist = _load_state(self.state_file)
        if self._persist.cursor_block <= 0:
            start = os.getenv("TELEGRAM_BOT_START_BLOCK", "latest").strip().lower()
            if start == "latest":
                head = self.w3.eth.block_number
                safe_head = max(0, head - self.broadcast_confirmations)
                self._persist.cursor_block = safe_head
            else:
                try:
                    self._persist.cursor_block = int(start)
                except Exception:
                    self._persist.cursor_block = 0
            _save_state(self.state_file, self._persist)

        if self._persist.burn_cursor_block <= 0:
            start = os.getenv("TELEGRAM_BOT_BURN_START_BLOCK", "latest").strip().lower()
            if start == "latest":
                head = self.w3.eth.block_number
                safe_head = max(0, head - self.broadcast_confirmations)
                self._persist.burn_cursor_block = safe_head
                self._persist.burn_start_block = 0
            else:
                try:
                    self._persist.burn_cursor_block = int(start)
                    self._persist.burn_start_block = int(start)
                except Exception:
                    self._persist.burn_cursor_block = 0
                    self._persist.burn_start_block = 0
            _save_state(self.state_file, self._persist)
        else:

            desired_start_raw = os.getenv("TELEGRAM_BOT_BURN_START_BLOCK", "latest").strip().lower()
            desired_start = 0
            if desired_start_raw != "latest":
                try:
                    desired_start = int(desired_start_raw)
                except Exception:
                    desired_start = 0

            naio_changed = (self._persist.naio_address or "").lower() != (self.naio or "").lower()
            burn_changed = (self._persist.burn_address or "").lower() != (self.burn_address or "").lower()
            start_changed = desired_start > 0 and int(self._persist.burn_start_block or 0) != desired_start
            if naio_changed or burn_changed or start_changed:
                logger.info(
                    "Reset burn scan state (naio_changed=%s burn_changed=%s start_changed=%s): old_naio=%s new_naio=%s old_burn=%s new_burn=%s old_start=%s new_start=%s",
                    naio_changed,
                    burn_changed,
                    start_changed,
                    self._persist.naio_address,
                    self.naio,
                    self._persist.burn_address,
                    self.burn_address,
                    self._persist.burn_start_block,
                    desired_start,
                )
                self._persist.burned_naio_wei = 0
                self._persist.burn_cursor_block = desired_start if desired_start > 0 else self._persist.burn_cursor_block
                self._persist.burn_start_block = desired_start
                self._persist.burn_address = self.burn_address
                self._persist.naio_address = self.naio
                _save_state(self.state_file, self._persist)

        if (self._persist.naio_address or "").lower() != (self.naio or "").lower():
            self._persist.naio_address = self.naio
            _save_state(self.state_file, self._persist)

    @staticmethod
    def _fmt_bps_percent(rate_bps: int) -> str:

        try:
            d = Decimal(int(rate_bps)) / Decimal(100)
        except Exception:
            d = Decimal(0)
        return _fmt_decimal_human(d, 2) + "%"

    @staticmethod
    def _fmt_ts_utc(ts: int) -> str:
        try:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            return str(ts)

    @staticmethod
    def _safe_decimals(token_contract, default: int) -> int:
        try:
            return int(token_contract.functions.decimals().call())
        except Exception:
            return default

    @staticmethod
    def _normalize_tx_hash(tx_hash: str) -> str:
        txh = str(tx_hash or "").strip()
        if not txh:
            return ""
        if not txh.startswith("0x"):
            txh = "0x" + txh
        try:
            raw = Web3.to_bytes(hexstr=txh)
        except Exception:
            return ""
        if len(raw) != 32:
            return ""
        return "0x" + raw.hex()

    def enqueue_hash_backfill(self, tx_hash: str) -> tuple[bool, str, Optional[dict]]:
        txh = self._normalize_tx_hash(tx_hash)
        if not txh:
            return False, "bad_tx_hash", None
        if not self.witness_hub_server_url:
            return False, "hub_not_configured", None

        url = f"{self.witness_hub_server_url}/v1/enqueue_hash"
        body = json.dumps({"txHash": txh}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.witness_hub_api_key:
            headers["X-Api-Key"] = self.witness_hub_api_key
        req = urllib.request.Request(url=url, data=body, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.witness_hub_timeout_seconds) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read() or b""
            except Exception:
                raw = b""
            try:
                payload = json.loads(raw.decode("utf-8") if raw else "{}")
            except Exception:
                payload = {}
            detail = str(payload.get("detail") or payload.get("error") or f"http_{getattr(e, 'code', 0)}")
            transfer = payload.get("transfer") if isinstance(payload.get("transfer"), dict) else None
            return False, detail, transfer
        except Exception as e:
            return False, f"hub_request_failed:{e}", None

        try:
            payload = json.loads(raw.decode("utf-8") if raw else "{}")
        except Exception:
            return False, "bad_hub_response", None

        ok = bool(payload.get("ok"))
        detail = str(payload.get("detail") or payload.get("error") or ("ok" if ok else "unknown"))
        transfer = payload.get("transfer") if isinstance(payload.get("transfer"), dict) else None
        return ok, detail, transfer

    def _init_price_db(self) -> None:
        conn = sqlite3.connect(self.price_db_path, timeout=30)
        with conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS price_points ("
                "ts INTEGER PRIMARY KEY, "
                "price_wei INTEGER NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS deposit_points ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts INTEGER NOT NULL, "
                "amount_wei INTEGER NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_deposit_points_ts ON deposit_points(ts)")
        conn.close()

    def _purge_old_points(self, conn: sqlite3.Connection, now_ts: int) -> None:
        if self.price_retention_days <= 0:
            return
        cutoff = now_ts - (self.price_retention_days * 86400)
        conn.execute("DELETE FROM price_points WHERE ts < ?", (cutoff,))

    def record_price_point(self) -> None:
        ts = int(time.time())
        price = int(self.controller_contract.functions.getPrice().call())
        conn = sqlite3.connect(self.price_db_path, timeout=30)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO price_points(ts, price_wei) VALUES(?, ?)",
                (ts, price),
            )

            if ts % 3600 == 0:
                self._purge_old_points(conn, ts)
        conn.close()

    def get_price_candles(self, duration_secs: int, interval_secs: int) -> list[tuple[int, int, int, int, int]]:
        end_ts = int(time.time())
        start_ts = end_ts - duration_secs
        if _env_bool("PRICE_PIC_SIMULATE", False):
            return self._simulate_candles(duration_secs, interval_secs)
        conn = sqlite3.connect(self.price_db_path, timeout=30)
        rows = conn.execute(
            "SELECT ts, price_wei FROM price_points "
            "WHERE ts BETWEEN ? AND ? ORDER BY ts ASC",
            (start_ts, end_ts),
        ).fetchall()
        conn.close()
        if not rows:
            return []
        buckets: dict[int, list[int]] = {}
        for ts, price in rows:
            b = ts - (ts % interval_secs)
            buckets.setdefault(b, []).append(int(price))
        candles: list[tuple[int, int, int, int, int]] = []
        for b in sorted(buckets.keys()):
            prices = buckets[b]
            o = prices[0]
            h = max(prices)
            l = min(prices)
            c = prices[-1]
            candles.append((b, o, h, l, c))
        return candles

    def _get_deposit_vols(self, start_ts: int, end_ts: int, interval_secs: int) -> dict[int, int]:
        conn = sqlite3.connect(self.price_db_path, timeout=30)
        rows = conn.execute(
            "SELECT ts, amount_wei FROM deposit_points "
            "WHERE ts BETWEEN ? AND ? ORDER BY ts ASC",
            (start_ts, end_ts),
        ).fetchall()
        conn.close()
        vols: dict[int, int] = {}
        for ts, amt in rows:
            b = ts - (ts % interval_secs)
            vols[b] = vols.get(b, 0) + int(amt)
        return vols

    def _load_all_deposit_users(self) -> Set[str]:
        users: Set[str] = set()
        conn = sqlite3.connect(self.price_db_path, timeout=30)
        try:
            cursor = conn.execute("SELECT DISTINCT user_address FROM deposit_records")
            for row in cursor:
                addr = (row[0] or "").strip().lower()
                if addr:
                    users.add(addr)
        finally:
            conn.close()
        return users

    def _load_all_referral_users(self) -> Set[str]:
        users: Set[str] = set()
        conn = sqlite3.connect(self.price_db_path, timeout=30)
        try:
            cursor = conn.execute("SELECT DISTINCT user_address FROM referral_relations")
            for row in cursor:
                addr = (row[0] or "").strip().lower()
                if addr:
                    users.add(addr)
        finally:
            conn.close()
        return users

    def get_missing_referral_users(self) -> tuple[int, int, int, list[str]]:
        all_deposit_users = self._load_all_deposit_users()
        all_referral_users = self._load_all_referral_users()
        target_users = all_deposit_users
        missing = sorted([u for u in target_users if u not in all_referral_users])
        return len(all_deposit_users), len(all_referral_users), len(target_users), missing

    def get_price_candles_with_vol(
        self, duration_secs: int, interval_secs: int
    ) -> tuple[list[tuple[int, int, int, int, int]], list[int]]:
        end_ts = int(time.time())
        start_ts = end_ts - duration_secs
        if _env_bool("PRICE_PIC_SIMULATE", False):
            return self._simulate_candles_with_vol(duration_secs, interval_secs)

        candles = self.get_price_candles(duration_secs, interval_secs)
        if not candles:
            return [], []
        vol_map = self._get_deposit_vols(start_ts, end_ts, interval_secs)
        vols = [vol_map.get(c[0], 0) for c in candles]
        return candles, vols

    def get_price_candles_with_vol_dedup(
        self, duration_secs: int, interval_secs: int
    ) -> tuple[list[tuple[int, int, int, int, int]], list[int]]:
        end_ts = int(time.time())

        max_window_days = max(1, int(getattr(self, "price_retention_days", 7)))
        start_ts = end_ts - max_window_days * 86400
        if _env_bool("PRICE_PIC_SIMULATE", False):
            return self._simulate_candles_with_vol(duration_secs, interval_secs)

        conn = sqlite3.connect(self.price_db_path, timeout=30)
        rows = conn.execute(
            "SELECT ts, price_wei FROM price_points "
            "WHERE ts BETWEEN ? AND ? ORDER BY ts ASC",
            (start_ts, end_ts),
        ).fetchall()
        conn.close()
        if not rows:
            return [], []

        bucket_prices: dict[int, list[int]] = {}
        for ts, price in rows:
            p = int(price)
            b = ts - (ts % interval_secs)
            bucket_prices.setdefault(b, []).append(p)

        if not bucket_prices:
            return [], []

        bucket_ohlc: list[tuple[int, int, int, int, int]] = []
        for b in sorted(bucket_prices.keys()):
            prices = bucket_prices[b]
            o = prices[0]
            h = max(prices)
            l = min(prices)
            c = prices[-1]
            bucket_ohlc.append((b, o, h, l, c))

        target_count = max(1, duration_secs // interval_secs)
        selected_idx_desc: list[int] = []
        last_close: Optional[int] = None

        for i in range(len(bucket_ohlc) - 1, -1, -1):
            c = bucket_ohlc[i][4]
            if last_close is None or c != last_close:
                selected_idx_desc.append(i)
                last_close = c
                if len(selected_idx_desc) >= target_count:
                    break

        if not selected_idx_desc:
            return [], []

        selected_idx = sorted(selected_idx_desc)

        selected: list[tuple[int, int, int, int, int]] = []
        for k, idx in enumerate(selected_idx):
            b, o0, _h0, _l0, c = bucket_ohlc[idx]
            if k > 0:
                prev_idx = selected_idx[k - 1]
                o = bucket_ohlc[prev_idx][4]
            else:
                prev_idx = idx
                o = o0

            segment = bucket_ohlc[prev_idx: idx + 1]
            hi = max(max(x[2], x[4], x[1]) for x in segment)
            lo = min(min(x[3], x[4], x[1]) for x in segment)
            selected.append((b, o, hi, lo, c))

        vol_map = self._get_deposit_vols(start_ts, end_ts, interval_secs)
        vols: list[int] = [vol_map.get(c[0], 0) for c in selected]

        compressed_candles: list[tuple[int, int, int, int, int]] = []
        if selected:

            anchor_ts = end_ts - (end_ts % interval_secs)
            n = len(selected)
            for idx, (_orig_ts, o, h, l, c) in enumerate(selected):

                new_ts = anchor_ts - (n - 1 - idx) * interval_secs
                compressed_candles.append((new_ts, o, h, l, c))

        return compressed_candles, vols

    def get_price_changes_with_vol(
        self, max_points: int, interval_secs: int
    ) -> tuple[list[tuple[int, int, int, int, int]], list[int]]:
        end_ts = int(time.time())
        max_window_days = max(1, int(getattr(self, "price_retention_days", 7)))
        start_ts = end_ts - max_window_days * 86400
        if _env_bool("PRICE_PIC_SIMULATE", False):
            return self._simulate_candles_with_vol(max_points * interval_secs, interval_secs)

        conn = sqlite3.connect(self.price_db_path, timeout=30)
        rows = conn.execute(
            "SELECT ts, price_wei FROM price_points "
            "WHERE ts BETWEEN ? AND ? ORDER BY ts ASC",
            (start_ts, end_ts),
        ).fetchall()
        conn.close()
        if not rows:
            return [], []

        changes: list[tuple[int, int, int]] = []
        last_price: Optional[int] = None
        for row_idx, (ts, price) in enumerate(rows):
            p = int(price)
            if last_price is not None and p == last_price:
                continue
            changes.append((int(ts), p, row_idx))
            last_price = p

        if not changes:
            return [], []

        start_idx = max(0, len(changes) - max_points) if max_points > 0 else 0
        selected = changes[start_idx:]

        vol_map = self._get_deposit_vols(start_ts, end_ts, interval_secs)
        vols: list[int] = []
        for ts_orig, _p, _row_idx in selected:
            b = ts_orig - (ts_orig % interval_secs)
            vols.append(vol_map.get(b, 0))

        compressed_candles: list[tuple[int, int, int, int, int]] = []
        anchor_ts = end_ts - (end_ts % interval_secs)
        n = len(selected)
        for idx, (_ts_orig, price, row_idx) in enumerate(selected):
            new_ts = anchor_ts - (n - 1 - idx) * interval_secs
            global_idx = start_idx + idx
            if global_idx > 0:
                prev_price = changes[global_idx - 1][1]
                prev_row_idx = changes[global_idx - 1][2]
            else:
                prev_price = price
                prev_row_idx = row_idx

            o = int(prev_price)
            c = int(price)
            lo = min(o, c)
            hi = max(o, c)

            seg = rows[prev_row_idx: row_idx + 1]
            if seg:
                seg_prices = [int(pv) for (_ts, pv) in seg]
                lo = min(lo, min(seg_prices))
                hi = max(hi, max(seg_prices))
            l = int(lo)
            h = int(hi)
            compressed_candles.append((new_ts, o, h, l, c))

        return compressed_candles, vols

    def _simulate_candles_with_vol(
        self, duration_secs: int, interval_secs: int
    ) -> tuple[list[tuple[int, int, int, int, int]], list[int]]:
        candles = self._simulate_candles(duration_secs, interval_secs)
        vols: list[int] = []
        base_vol = 10_000 * 10**18
        for _ts, o, h, l, c in candles:
            spread = max(1, h - l)
            v = base_vol + (spread // 50) + random.randint(0, 2_000) * 10**18
            vols.append(max(0, int(v)))
        return candles, vols

    def _simulate_candles(self, duration_secs: int, interval_secs: int) -> list[tuple[int, int, int, int, int]]:

        now = int(time.time())
        start = now - duration_secs
        count = max(1, duration_secs // interval_secs)
        base = 1_000_000_000_000_000_000
        seed = (start // interval_secs) ^ (duration_secs << 8) ^ (interval_secs << 4)
        rng = random.Random(seed)

        candles: list[tuple[int, int, int, int, int]] = []
        last_close = base
        trend = 0.0
        for i in range(int(count)):
            ts = start + i * interval_secs
            trend = 0.85 * trend + rng.uniform(-1.0, 1.0)
            shock = rng.gauss(0, 1.0)
            drift = (trend * 6_000_000_000_000_0) + (shock * 3_000_000_000_000_0)

            o = last_close
            c = int(max(1, o + drift))
            wick = abs(rng.gauss(0, 1.0)) * 7_000_000_000_000_0
            hi = int(max(o, c) + wick)
            lo = int(max(1, min(o, c) - wick))
            candles.append((ts, o, hi, lo, c))
            last_close = c
        return candles

    def get_price_text(self) -> str:
        head = int(self.w3.eth.block_number)
        try:
            p = int(self.controller_contract.functions.getPrice().call(block_identifier=head))
        except Exception:
            p = int(self.controller_contract.functions.getPrice().call())

        val = Decimal(p) / Decimal(10**18)
        return f"当前价格：1 NAIO = {_fmt_decimal_human(val, self.display_price_decimals)} USDT ({head})"

    def _get_price_suffix(
        self, block_no: Optional[int] = None, cache: Optional[dict[int, str]] = None
    ) -> str:
        if block_no is not None and block_no > 0 and cache is not None and block_no in cache:
            return cache[block_no]

        try:
            if block_no is not None and block_no > 0:

                p = int(self.controller_contract.functions.getPrice().call(block_identifier=block_no))
            else:
                p = int(self.controller_contract.functions.getPrice().call())
        except Exception:
            p = 0

        if p == 0:
            suffix = ""
        else:
            val = Decimal(p) / Decimal(10**18)
            price_str = _fmt_decimal_human(val, self.display_price_decimals)

            from_lang = DEFAULT_LANG

            base = _t(from_lang, "info_price_suffix", price=price_str)
            if block_no is not None and block_no > 0:
                suffix = f"\n\n{base} ({block_no})"
            else:
                suffix = f"\n\n{base}"

        if block_no is not None and block_no > 0 and cache is not None:
            cache[block_no] = suffix
        return suffix

    def _get_rule_pool_line(
        self, block_no: Optional[int] = None, cache: Optional[dict[int, str]] = None
    ) -> str:
        if block_no is not None and block_no > 0 and cache is not None and block_no in cache:
            return cache[block_no]
        if self.rule_engine_contract is None:
            return ""
        try:
            if block_no is not None and block_no > 0:
                pool_wei = int(self.rule_engine_contract.functions.rulePoolUsdt().call(block_identifier=block_no))
            else:
                pool_wei = int(self.rule_engine_contract.functions.rulePoolUsdt().call())
            pool_s = _fmt_amount(pool_wei, self.usdt_decimals, self.display_usdt_decimals)
            line = f"\n底池USDT：{pool_s} USDT"
        except Exception:
            line = ""
        if block_no is not None and block_no > 0 and cache is not None:
            cache[block_no] = line
        return line

    def _get_deposit_rule_pool_and_price(
        self, block_no: int, usdt_amount: int, bookkeeping_block: Optional[int] = None
    ) -> tuple[str, str]:
        rule_pool_line = ""
        suffix = ""
        if self.rule_engine_contract is None:
            return (rule_pool_line, suffix)
        blk = block_no if block_no > 0 else "latest"
        try:
            pool_wei = int(self.rule_engine_contract.functions.rulePoolUsdt().call(block_identifier=blk))

            pool_after = pool_wei if (bookkeeping_block and block_no == bookkeeping_block) else (pool_wei + usdt_amount)
            pool_s = _fmt_amount(pool_after, self.usdt_decimals, self.display_usdt_decimals)
            rule_pool_line = f"\n底池USDT：{pool_s} USDT"

            supply = int(self.naio_contract.functions.totalSupply().call(block_identifier=blk))
            if self.burn_address:
                burned = int(self.naio_contract.functions.balanceOf(self.burn_address).call(block_identifier=blk))
                supply = supply - burned if supply > burned else 0
            if supply > 0 and pool_after > 0:
                price_wei = (pool_after * 10**18) // supply
                val = Decimal(price_wei) / Decimal(10**18)
                price_str = _fmt_decimal_human(val, self.display_price_decimals)
                from_lang = DEFAULT_LANG
                base = _t(from_lang, "info_price_suffix", price=price_str)
                suffix = f"\n\n{base} ({block_no})" if block_no > 0 else f"\n\n{base}"
        except Exception:
            pass
        return (rule_pool_line, suffix)

    def get_pool_usdt_text(self) -> str:
        bal = int(self.usdt_contract.functions.balanceOf(self.controller).call())
        reserved = int(self.controller_contract.functions.reservedUsdt().call())
        pool = bal - reserved if bal > reserved else 0
        return f"底池 USDT 余额（扣除待领）：{_fmt_amount(pool, self.usdt_decimals, self.display_usdt_decimals)} USDT"

    def get_pool_naio_text(self) -> str:
        supply = int(self.naio_contract.functions.totalSupply().call())
        burned = int(self.naio_contract.functions.balanceOf(self.burn_address).call())
        denom = supply - burned if supply > burned else 0
        return f"底池 NAIO（计价分母=总量-黑洞）：{_fmt_amount(denom, self.naio_decimals, self.display_naio_decimals)} NAIO"

    def get_copy_addr_text(self, which: str) -> str:
        which = which.lower()
        if which == "naio":
            a = Web3.to_checksum_address(self.naio)
            return f"NAIO 地址：\n{_as_pre(a)}"
        if which == "usdt":
            a = Web3.to_checksum_address(self.usdt)
            return f"USDT 地址：\n{_as_pre(a)}"
        if which == "pool":
            a = Web3.to_checksum_address(self.controller)
            return f"池子地址：\n{_as_pre(a)}"
        if which == "node":
            if not self.node_seat_pool:
                return "席位地址：未配置（请在 .env 填 NODE_SEAT_POOL_ADDRESS）"
            a = Web3.to_checksum_address(self.node_seat_pool)
            return f"席位地址：\n{_as_pre(a)}"
        return "未知地址类型"

    def _scan_burn_events_sync(self) -> None:
        head = self.w3.eth.block_number
        safe_head = max(0, head - self.broadcast_confirmations)
        if safe_head < self._persist.burn_cursor_block:
            return

        from_block = self._persist.burn_cursor_block
        to_block = min(safe_head, from_block + max(1, self.max_blocks_per_scan))

        sig = Web3.keccak(text="Transfer(address,address,uint256)").hex()
        if isinstance(sig, str) and not sig.startswith("0x"):
            sig = "0x" + sig

        burn_addr = self.burn_address or "0x0000000000000000000000000000000000000000"
        burn_addr = Web3.to_checksum_address(burn_addr)
        to_topic0 = "0x" + burn_addr[2:].lower().rjust(64, "0")

        logs = self.w3.eth.get_logs(
            {
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": self.naio,
                "topics": [sig, None, to_topic0],
            }
        )
        total = self._persist.burned_naio_wei
        for lg in logs:
            try:
                ev = self.naio_contract.events.Transfer().process_log(lg)
                v = int(ev["args"]["value"])
                if v > 0:
                    total += v
            except Exception:

                try:
                    data_hex = lg.get("data")
                    if isinstance(data_hex, str):
                        v = int(data_hex, 16)
                        total += v
                except Exception:
                    pass

        self._persist.burned_naio_wei = total
        self._persist.burn_cursor_block = to_block + 1
        _save_state(self.state_file, self._persist)

    def get_burned_naio_text(self) -> str:

        self._scan_burn_events_sync()
        burned = self._persist.burned_naio_wei
        try:
            burn_balance = int(self.naio_contract.functions.balanceOf(self.burn_address).call())
        except Exception:
            burn_balance = 0
        supply = int(self.naio_contract.functions.totalSupply().call())
        circulating = supply - burn_balance if burn_balance <= supply else 0
        burned_s = _fmt_amount(burned, self.naio_decimals, self.display_naio_decimals)
        burn_balance_s = _fmt_amount(burn_balance, self.naio_decimals, self.display_naio_decimals)
        circ_s = _fmt_amount(circulating, self.naio_decimals, self.display_naio_decimals)
        return f"黑洞累计：{burn_balance_s} NAIO\n扫描累计：{burned_s} NAIO\n当前流通：{circ_s} NAIO"

    def _find_block_by_ts(self, target_ts: int) -> int:
        if target_ts <= 0:
            return 0
        head = self.w3.eth.block_number
        lo = 0
        hi = head
        while lo < hi:
            mid = (lo + hi) // 2
            try:
                ts = int(self.w3.eth.get_block(mid)["timestamp"])
            except Exception:
                ts = 0
            if ts < target_ts:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _calc_business_tax_bps(self, seller: str) -> int:
        try:
            info = self.controller_contract.functions.users(seller).call()
            principal = int(info[0]) if info and len(info) > 0 else 0
        except Exception:
            principal = 0
        if principal == 0:
            return 1000
        try:
            total_sold = int(self.controller_contract.functions.totalSoldUsdt(seller).call())
        except Exception:
            total_sold = 0
        multiple = (total_sold * 1e18) // principal
        return 2000 if multiple > 20e18 else 1000

    def _get_epoch_stats(self) -> tuple[int, int]:
        try:
            system_start = int(self.controller_contract.functions.systemStartTs().call())
        except Exception:
            return (0, 0)
        if system_start == 0:
            return (0, 0)
        now_ts = int(time.time())
        epoch_id = max(0, (now_ts - system_start) // self.epoch_seconds)
        epoch_start_ts = system_start + (epoch_id * self.epoch_seconds)

        if self._persist.epoch_id != epoch_id or self._persist.epoch_start_block == 0:
            start_block = self._find_block_by_ts(epoch_start_ts)
            self._persist.epoch_id = int(epoch_id)
            self._persist.epoch_start_block = int(start_block)
            self._persist.epoch_deposit_usdt = 0
            self._persist.epoch_sell_usdt = 0
            self._persist.epoch_last_block = int(start_block) - 1

        head = self.w3.eth.block_number
        from_block = max(self._persist.epoch_start_block, self._persist.epoch_last_block + 1)
        if from_block > head:
            return (self._persist.epoch_deposit_usdt, self._persist.epoch_sell_usdt)

        sig_dep = Web3.keccak(text="DepositFromTransfer(address,uint256,bytes32)").hex()
        sig_lp = Web3.keccak(text="LPWithdrawn(address,uint256,uint256,uint256)").hex()
        if isinstance(sig_dep, str) and not sig_dep.startswith("0x"):
            sig_dep = "0x" + sig_dep
        if isinstance(sig_lp, str) and not sig_lp.startswith("0x"):
            sig_lp = "0x" + sig_lp

        total_dep = self._persist.epoch_deposit_usdt
        total_sell = self._persist.epoch_sell_usdt

        cur = from_block
        while cur <= head:
            to_block = min(head, cur + max(1, self.max_blocks_per_scan))
            logs = self.w3.eth.get_logs(
                {
                    "fromBlock": cur,
                    "toBlock": to_block,
                    "address": self.controller,
                    "topics": [[sig_dep, sig_lp]],
                }
            )
            logs = sorted(logs, key=lambda x: (x.get("blockNumber", 0), x.get("logIndex", 0)))
            for lg in logs:
                topic0 = lg["topics"][0].hex()
                if not topic0.startswith("0x"):
                    topic0 = "0x" + topic0
                if topic0.lower() == sig_dep.lower():
                    ev = self.controller_contract.events.DepositFromTransfer().process_log(lg)
                    total_dep += int(ev["args"]["usdtAmount"])
                elif topic0.lower() == sig_lp.lower():
                    ev = self.controller_contract.events.LPWithdrawn().process_log(lg)
                    lp_amount = int(ev["args"]["lpAmount"])
                    if lp_amount > 0:
                        user = ev["args"]["user"]
                        net = int(ev["args"]["usdtReturned"])
                        bps = self._calc_business_tax_bps(user)
                        if bps > 0 and bps < 10000:
                            gross = (net * 10000) // (10000 - bps)
                        else:
                            gross = net
                        total_sell += gross
            cur = to_block + 1

        self._persist.epoch_deposit_usdt = total_dep
        self._persist.epoch_sell_usdt = total_sell
        self._persist.epoch_last_block = head
        _save_state(self.state_file, self._persist)
        return (total_dep, total_sell)

    def _get_withdraw_queue_stats(self) -> tuple[int, int]:
        if self._persist.queue_cursor_block <= 0:
            start = os.getenv("TELEGRAM_BOT_START_BLOCK", "latest").strip().lower()
            if start == "latest":
                head = self.w3.eth.block_number
                safe_head = max(0, head - self.broadcast_confirmations)
                self._persist.queue_cursor_block = safe_head
            else:
                try:
                    self._persist.queue_cursor_block = int(start)
                except Exception:
                    self._persist.queue_cursor_block = 0

        head = self.w3.eth.block_number
        from_block = self._persist.queue_cursor_block
        if from_block > head:
            return (self._persist.queue_pending_usdt, self._persist.queue_users_count)

        sig_wq = Web3.keccak(text="WithdrawQueued(address,uint256)").hex()
        sig_wp = Web3.keccak(text="WithdrawProcessed(address,uint256,uint256,uint256,uint256)").hex()
        if isinstance(sig_wq, str) and not sig_wq.startswith("0x"):
            sig_wq = "0x" + sig_wq
        if isinstance(sig_wp, str) and not sig_wp.startswith("0x"):
            sig_wp = "0x" + sig_wp

        queue_map = dict(self._persist.queue_user_remaining or {})
        pending = int(self._persist.queue_pending_usdt or 0)
        users = int(self._persist.queue_users_count or 0)

        cur = from_block
        while cur <= head:
            to_block = min(head, cur + max(1, self.max_blocks_per_scan))
            logs = self.w3.eth.get_logs(
                {
                    "fromBlock": cur,
                    "toBlock": to_block,
                    "address": self.controller,
                    "topics": [[sig_wq, sig_wp]],
                }
            )
            logs = sorted(logs, key=lambda x: (x.get("blockNumber", 0), x.get("logIndex", 0)))
            for lg in logs:
                topic0 = lg["topics"][0].hex()
                if not topic0.startswith("0x"):
                    topic0 = "0x" + topic0

                if topic0.lower() == sig_wq.lower():
                    ev = self.controller_contract.events.WithdrawQueued().process_log(lg)
                    user = str(ev["args"]["user"]).lower()
                    amount = int(ev["args"]["amount"])
                    prev = int(queue_map.get(user, 0))
                    newv = prev + amount
                    queue_map[user] = newv
                    pending += amount
                    if prev == 0 and newv > 0:
                        users += 1
                elif topic0.lower() == sig_wp.lower():
                    ev = self.controller_contract.events.WithdrawProcessed().process_log(lg)
                    user = str(ev["args"]["user"]).lower()
                    paid = int(ev["args"]["usdtReturned"])
                    prev = int(queue_map.get(user, 0))
                    if prev <= paid:
                        if prev > 0:
                            pending -= prev
                            users -= 1
                        queue_map.pop(user, None)
                    else:
                        newv = prev - paid
                        queue_map[user] = newv
                        pending -= paid
            cur = to_block + 1

        if pending < 0:
            pending = 0
        if users < 0:
            users = 0

        self._persist.queue_cursor_block = head + 1
        self._persist.queue_pending_usdt = pending
        self._persist.queue_users_count = users
        self._persist.queue_user_remaining = queue_map
        _save_state(self.state_file, self._persist)
        return (pending, users)

    def get_chain_info_text(self, lang: str) -> str:
        def _call_with_retry(fn, default=0, retries: int = 2, delay: float = 0.3):
            for i in range(retries + 1):
                try:
                    return fn()
                except Exception as e:
                    if i >= retries:
                        logger.debug("chain info call failed after retry: %s", e)
                        return default
                    time.sleep(delay * (i + 1))

        head = int(self.w3.eth.block_number)

        with ThreadPoolExecutor(max_workers=10) as exe:
            f_price = exe.submit(lambda: int(self.controller_contract.functions.getPrice().call(block_identifier=head)))
            f_usdt_bal = exe.submit(lambda: int(self.usdt_contract.functions.balanceOf(self.controller).call()))
            f_reserved_usdt = exe.submit(lambda: int(self.controller_contract.functions.reservedUsdt().call()))
            f_rule_pool_usdt = exe.submit(
                lambda: int(self.rule_engine_contract.functions.rulePoolUsdt().call())
                if self.rule_engine_contract is not None
                else 0
            )
            f_naio_supply = exe.submit(lambda: int(self.naio_contract.functions.totalSupply().call()))
            f_reserved_naio = exe.submit(lambda: int(self.controller_contract.functions.reservedNaio().call()))
            f_referral_pool = exe.submit(lambda: int(self.controller_contract.functions.referralPoolNaio().call()))
            f_burn = exe.submit(lambda: int(self.naio_contract.functions.balanceOf(self.burn_address).call()))
            f_system_start = exe.submit(lambda: int(self.controller_contract.functions.systemStartTs().call()))
            f_current_epoch = exe.submit(lambda: int(self.controller_contract.functions.getCurrentEpoch().call()))
            f_last_epoch = exe.submit(lambda: int(self.controller_contract.functions.lastPokeEpoch().call()))
            f_deflation_snapshot = exe.submit(
                lambda: self.controller_contract.functions.deflationSnapshots(
                    int(self.controller_contract.functions.lastDeflationSnapshotEpoch().call())
                ).call()
            )
            f_epoch_stats = exe.submit(self._get_epoch_stats)
            f_withdraw_burn_epoch = exe.submit(lambda: int(self.controller_contract.functions.withdrawBurnEpoch().call()))
            f_withdraw_burn_quota = exe.submit(lambda: int(self.controller_contract.functions.withdrawBurnQuotaToken().call()))
            f_withdraw_burn_used = exe.submit(lambda: int(self.controller_contract.functions.withdrawBurnUsedToken().call()))
            f_queue_stats = exe.submit(self._get_withdraw_queue_stats)

            price = _call_with_retry(f_price.result)
            usdt_bal = _call_with_retry(f_usdt_bal.result)
            reserved_usdt = _call_with_retry(f_reserved_usdt.result)
            rule_pool_usdt = _call_with_retry(f_rule_pool_usdt.result, default=0)
            naio_supply = _call_with_retry(f_naio_supply.result)
            reserved_naio = _call_with_retry(f_reserved_naio.result)
            referral_pool = _call_with_retry(f_referral_pool.result)
            burn_balance = _call_with_retry(f_burn.result)
            system_start = _call_with_retry(f_system_start.result)
            current_epoch = _call_with_retry(f_current_epoch.result, default=0)
            last_epoch = _call_with_retry(f_last_epoch.result)
            snap = _call_with_retry(f_deflation_snapshot.result, default=(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
            dep_sum, sell_sum = _call_with_retry(f_epoch_stats.result, (0, 0))
            withdraw_burn_epoch = _call_with_retry(f_withdraw_burn_epoch.result, default=0)
            withdraw_burn_quota = _call_with_retry(f_withdraw_burn_quota.result, default=0)
            withdraw_burn_used = _call_with_retry(f_withdraw_burn_used.result, default=0)
            queue_pending, queue_users = _call_with_retry(f_queue_stats.result, (0, 0))

        price_s = _fmt_amount(price, 18, self.display_price_decimals)

        pool_usdt_s = _fmt_amount(rule_pool_usdt, self.usdt_decimals, self.display_usdt_decimals)
        reserved_usdt_s = _fmt_amount(reserved_usdt, self.usdt_decimals, self.display_usdt_decimals)

        price_denom_naio = naio_supply - burn_balance if naio_supply > burn_balance else 0
        pool_naio_s = _fmt_amount(price_denom_naio, self.naio_decimals, self.display_naio_decimals)
        reserved_naio_s = _fmt_amount(reserved_naio, self.naio_decimals, self.display_naio_decimals)
        referral_pool_s = _fmt_amount(referral_pool, self.naio_decimals, self.display_naio_decimals)

        burn_s = _fmt_amount(burn_balance, self.naio_decimals, self.display_naio_decimals)

        if int(last_epoch) == 4294967295:
            last_epoch = -1
        next_ts = system_start + (last_epoch + 2) * self.epoch_seconds if system_start > 0 else 0
        next_time = _fmt_ts_local(next_ts, lang) if next_ts > 0 else "-"

        dep_s = _fmt_amount(dep_sum, self.usdt_decimals, self.display_usdt_decimals)
        sell_s = _fmt_amount(sell_sum, self.usdt_decimals, self.display_usdt_decimals)

        burn_used = withdraw_burn_used if withdraw_burn_used <= withdraw_burn_quota else withdraw_burn_quota
        burn_remaining = withdraw_burn_quota - burn_used
        burn_quota_s = _fmt_amount(withdraw_burn_quota, self.naio_decimals, self.display_naio_decimals)
        burn_used_s = _fmt_amount(burn_used, self.naio_decimals, self.display_naio_decimals)
        burn_remaining_s = _fmt_amount(burn_remaining, self.naio_decimals, self.display_naio_decimals)
        queue_pending_s = _fmt_amount(queue_pending, self.usdt_decimals, self.display_usdt_decimals)

        poke_ready = (current_epoch > 0 and (current_epoch - 1) > last_epoch)
        catchup_epochs = ((current_epoch - 1) - last_epoch) if poke_ready else 0

        epochs_per_month = max(1, int(os.getenv("EPOCHS_PER_MONTH", "30")))
        month_index = int(current_epoch) // epochs_per_month
        steps = min(month_index, 10)
        current_rate_bps = 200 + (steps * 10)

        burn_epoch_label = f"{withdraw_burn_epoch}"

        snap_epoch = int(snap[0]) if len(snap) > 0 else 0
        snap_deflation = int(snap[5]) if len(snap) > 5 else 0
        snap_burn = int(snap[6]) if len(snap) > 6 else 0
        snap_eco = int(snap[7]) if len(snap) > 7 else 0
        snap_new_user = int(snap[8]) if len(snap) > 8 else 0
        snap_node = int(snap[9]) if len(snap) > 9 else 0
        snap_independent = int(snap[10]) if len(snap) > 10 else 0
        snap_ref = int(snap[11]) if len(snap) > 11 else 0
        snap_static = int(snap[12]) if len(snap) > 12 else 0
        snap_withdraw_burn_consumed = int(snap[13]) if len(snap) > 13 else 0
        snap_price_before = int(snap[3]) if len(snap) > 3 else 0
        burn_value_usdt = (snap_burn * snap_price_before) // (10**18) if snap_burn > 0 and snap_price_before > 0 else 0
        snap_deflation_s = _fmt_amount(snap_deflation, self.naio_decimals, self.display_naio_decimals)
        burn_value_s = _fmt_amount(burn_value_usdt, self.usdt_decimals, self.display_usdt_decimals)
        split_s = " / ".join(
            [
                _fmt_amount(snap_eco, self.naio_decimals, self.display_naio_decimals),
                _fmt_amount(snap_new_user, self.naio_decimals, self.display_naio_decimals),
                _fmt_amount(snap_node, self.naio_decimals, self.display_naio_decimals),
                _fmt_amount(snap_independent, self.naio_decimals, self.display_naio_decimals),
                _fmt_amount(snap_ref, self.naio_decimals, self.display_naio_decimals),
                _fmt_amount(snap_static, self.naio_decimals, self.display_naio_decimals),
            ]
        )
        snap_withdraw_burn_s = _fmt_amount(snap_withdraw_burn_consumed, self.naio_decimals, self.display_naio_decimals)

        if system_start > 0 and current_epoch >= 0:
            epoch_start_ts = system_start + (current_epoch * self.epoch_seconds)
            epoch_end_ts = epoch_start_ts + self.epoch_seconds
            epoch_start_s = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch_start_ts + 8 * 3600))
            epoch_end_s = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch_end_ts + 8 * 3600))
            epoch_window_s = f"{epoch_start_s} —— {epoch_end_s} （UTC+8）"
        else:
            epoch_window_s = "- —— - （UTC+8）"

        lines = [
            _t(lang, "chain_info_title"),
            "",
            _t(lang, "chain_info_section_price"),
            f"  {_t(lang, 'label_price')}: {price_s} USDT (#{head})",
            f"  {_t(lang, 'label_pool_usdt')}: {pool_usdt_s} USDT",
            f"  {_t(lang, 'label_pool_naio')}: {pool_naio_s} NAIO",
            f"  {_t(lang, 'label_burned_naio')}: {burn_s} NAIO",
            f"  {_t(lang, 'label_reserved_usdt')}: {reserved_usdt_s} USDT",
            f"  {_t(lang, 'label_reserved_naio')}: {reserved_naio_s} NAIO",
            f"  {_t(lang, 'label_referral_pool')}: {referral_pool_s} NAIO",
            "",
            _t(lang, "chain_info_section_epoch"),
            f"  {_t(lang, 'label_next_poke')}: {next_time}",
            f"  {_t(lang, 'label_current_release_rate')}: {self._fmt_bps_percent(current_rate_bps)} ({current_rate_bps} bps)",
            f"  {_t(lang, 'label_epoch_deposit')}: {dep_s} USDT",
            f"  {_t(lang, 'label_epoch_sell')}: {sell_s} USDT",
            f"  {_t(lang, 'label_poke_ready')}: {'YES' if poke_ready else 'NO'} (epoch {current_epoch})",
            f"  {_t(lang, 'label_catchup_epochs')}: {catchup_epochs}",
            "",
            _t(lang, "chain_info_section_withdraw"),
            f"  {_t(lang, 'label_withdraw_limit')}: {burn_remaining_s} NAIO (epoch {burn_epoch_label})",
            f"  {_t(lang, 'label_withdraw_limit_initial')}: {burn_quota_s} NAIO",
            f"  {_t(lang, 'label_withdraw_limit_used')}: {burn_used_s} NAIO",
            f"  {_t(lang, 'label_withdraw_queue_pending')}: {queue_pending_s} USDT",
            f"  {_t(lang, 'label_withdraw_queue_users')}: {queue_users}",
            "",
            _t(lang, "chain_info_section_deflation"),
            f"  {_t(lang, 'label_deflation_last_epoch')}: {snap_epoch}",
            f"  {_t(lang, 'label_deflation_total')}: {snap_deflation_s} NAIO",
            f"  {_t(lang, 'label_deflation_burn_value')}: {burn_value_s} USDT",
            f"  {_t(lang, 'label_deflation_split')}: {split_s} NAIO",
            f"  {_t(lang, 'label_withdraw_limit_add')}: {snap_withdraw_burn_s} NAIO",
            "",
            _t(lang, "chain_info_section_time"),
            f"  {_t(lang, 'label_epoch_window')}: {epoch_window_s}",
        ]
        return "\n".join(lines)

    def get_contract_addresses_text(self, lang: str) -> str:
        lines: list[str] = []
        lines.append(_t(lang, "addr_list_title"))
        lines.append(f"{_t(lang, 'label_controller')}:\n{_as_pre(self.controller)}")
        lines.append(f"{_t(lang, 'label_naio')}:\n{_as_pre(self.naio)}")
        lines.append(f"{_t(lang, 'label_usdt')}:\n{_as_pre(self.usdt)}")
        if self.pool_seeder:
            lines.append(f"{_t(lang, 'label_pool_seeder')}:\n{_as_pre(self.pool_seeder)}")
        if self.node_seat_pool:
            lines.append(f"{_t(lang, 'label_node')}:\n{_as_pre(self.node_seat_pool)}")
        if self.referral_bootstrap_address:
            lines.append(f"{_t(lang, 'label_ref_bootstrap')}:\n{_as_pre(self.referral_bootstrap_address)}")
        lines.append(f"{_t(lang, 'label_burn')}:\n{_as_pre(self.burn_address)}")
        return "\n".join(lines)

    def get_ops_help_text(self, lang: str) -> str:
        ctrl = Web3.to_checksum_address(self.controller)
        op_amounts = "\n".join(
            [
                f"0.0001 BNB  -> {_t(lang, 'op_claim_newuser')}",
                f"0.0003 BNB  -> {_t(lang, 'op_poke')} (poke)",
                f"0.0004 BNB  -> {_t(lang, 'op_claim_fixed_usdt')}",
                f"0.0005 BNB  -> {_t(lang, 'op_claim_static')} (claimStatic)",
                f"0.0006 BNB  -> {_t(lang, 'op_claim_dynamic')} (claimDynamic)",
                f"0.0007 BNB  -> {_t(lang, 'op_claim_node')}",
                f"0.000888 BNB -> {_t(lang, 'op_withdraw')}",
                f"0.0009 BNB  -> {_t(lang, 'op_claim_all')} (claimAll)",
            ]
        )
        return (
            f"{_t(lang, 'ops_help_title')}\n\n"
            f"{_t(lang, 'ops_help_desc')}\n\n"
            f"{_t(lang, 'label_controller')}:\n{_as_pre(ctrl)}\n"
            + "OP:\n"
            f"{_as_pre(op_amounts)}\n"
            + _t(lang, "ops_help_other_ops")
        )

    def get_chain_roles_text(self, lang: str) -> str:
        lines: list[str] = []
        lines.append(_t(lang, "chain_roles_title"))
        lines.append("")

        def _addr(a: str) -> str:
            if not a or not a.strip():
                return "-"
            return _as_pre(Web3.to_checksum_address(a.strip()))

        try:
            keeper = self.controller_contract.functions.keeper().call()
            lines.append(f"<b>Keeper</b> {_t(lang, 'chain_roles_desc_keeper')}")
            lines.append(_addr(keeper))
            lines.append("")
        except Exception:
            lines.append("<b>Keeper</b> " + _t(lang, "chain_roles_desc_keeper"))
            lines.append("-")
            lines.append("")

        witness_addrs: list[str] = []
        for i in range(1, 4):
            a = os.getenv(f"WITNESS_SIGNER_{i}", "").strip()
            if a:
                witness_addrs.append(a)
        raw = os.getenv("WITNESS_SIGNER_ADDRESSES", "").strip()
        if raw and not witness_addrs:
            for x in raw.replace("\n", ",").split(","):
                x = x.strip()
                if x and x.startswith("0x"):
                    witness_addrs.append(x)
        lines.append(f"<b>{_t(lang, 'chain_roles_label_witness')}</b> {_t(lang, 'chain_roles_desc_witness')}")
        if witness_addrs:
            for a in witness_addrs:
                lines.append(_addr(a))
        else:
            lines.append("-")
        lines.append("")

        try:
            owner = self.controller_contract.functions.owner().call()
            lines.append(f"<b>Owner</b> {_t(lang, 'chain_roles_desc_owner')}")
            lines.append(_addr(owner))
            lines.append("")
        except Exception:
            lines.append("<b>Owner</b> " + _t(lang, "chain_roles_desc_owner"))
            lines.append("-")
            lines.append("")

        council_addr = os.getenv("KEEPER_COUNCIL_ADDRESS", "").strip()
        if council_addr:
            try:
                council = self.w3.eth.contract(
                    address=Web3.to_checksum_address(council_addr), abi=KEEPER_COUNCIL_ABI
                )
                lines.append(f"<b>{_t(lang, 'chain_roles_label_council')}</b> {_t(lang, 'chain_roles_desc_council')}")
                for i in range(5):
                    m = council.functions.members(i).call()
                    lines.append(_addr(m))
                lines.append("")
            except Exception:
                lines.append(f"<b>{_t(lang, 'chain_roles_label_council')}</b> " + _t(lang, "chain_roles_desc_council"))
                lines.append("-")
                lines.append("")
        else:
            lines.append(f"<b>{_t(lang, 'chain_roles_label_council')}</b> {_t(lang, 'chain_roles_desc_council')}")
            lines.append("-")
            lines.append("")

        try:
            vg = self.controller_contract.functions.validatorGuardian().call()
            lines.append(f"<b>{_t(lang, 'chain_roles_label_validator')}</b> {_t(lang, 'chain_roles_desc_validator')}")
            lines.append(_addr(vg))
            lines.append("")
        except Exception:
            lines.append(f"<b>{_t(lang, 'chain_roles_label_validator')}</b> " + _t(lang, "chain_roles_desc_validator"))
            lines.append("-")
            lines.append("")

        lines.append(f"<b>{_t(lang, 'chain_roles_label_fixed')}</b> {_t(lang, 'chain_roles_desc_fixed')}")
        for name, fn in [
            ("nodePool", "nodePool"),
            ("marketPool", "marketPool"),
            ("opsPool", "opsPool"),
            ("ecoPool", "ecoPool"),
            ("independentPool", "independentPool"),
        ]:
            try:
                a = getattr(self.controller_contract.functions, fn)().call()
                lines.append(f"{name}: {_addr(a)}")
            except Exception:
                lines.append(f"{name}: -")
        lines.append("")

        lines.append(_t(lang, "chain_roles_nodes_intro"))
        return "\n".join(lines)

    def format_event(self, lang: str, ev: dict) -> str:
        t = ev.get("type")
        block_no = ev.get("block", 0)
        txh = str(ev.get("txh") or "").strip()
        block_parts: list[str] = []
        if t == "deposit":
            deposit_txh = str(ev.get("txh") or "").strip()
            bookkeeping_txh = str(ev.get("bookkeeping_txh") or "").strip()
            deposit_block = ev.get("block", 0)
            bookkeeping_block = ev.get("bookkeeping_block", 0)
            if deposit_txh:
                block_parts.append(f"\n{_t(lang, 'ev_deposit_hash_inflow')}：`{deposit_txh}`")
            if bookkeeping_txh:
                block_parts.append(f"\n{_t(lang, 'ev_deposit_hash_bookkeeping')}：`{bookkeeping_txh}`")
            if deposit_block > 0:
                block_parts.append(f"\n{_t(lang, 'ev_deposit_block_inflow')}：{deposit_block}")
            if bookkeeping_block > 0:
                block_parts.append(f"\n{_t(lang, 'ev_deposit_block_bookkeeping')}：{bookkeeping_block}")
        else:
            if block_no > 0:
                block_parts.append(f"\n区块：`{block_no}`")
            if txh:
                block_parts.append(f"\n哈希：`{txh}`")
        block_str = "".join(block_parts)

        if t == "deposit":
            ev2 = dict(ev)
            ev2["block_str"] = block_str + str(ev.get("rule_pool_line") or "")
            return _t(lang, "ev_deposit", **ev2)
        if t == "refund":
            ev2 = dict(ev)
            ev2["reason"] = _t(lang, ev.get("reason"))
            ev2["block_str"] = block_str + str(ev.get("rule_pool_line") or "")
            return _t(lang, "ev_refund", **ev2)
        if t == "deflation":
            ev2 = dict(ev)
            ev2["ts"] = _fmt_ts_local(int(ev.get("ts") or 0), lang)
            ev2["txline"] = ""
            ev2["block_str"] = block_str
            return _t(lang, "ev_deflation", **ev2)
        if t == "static_claim":
            ev2 = dict(ev)
            ev2["block_str"] = block_str
            return _t(lang, "ev_static_claim", **ev2)
        if t == "dynamic_claim":
            ev2 = dict(ev)
            ev2["block_str"] = block_str
            return _t(lang, "ev_dynamic_claim", **ev2)
        if t == "new_user_claim":
            ev2 = dict(ev)
            ev2["block_str"] = block_str
            return _t(lang, "ev_new_user_claim", **ev2)
        if t == "withdraw_queued":
            ev2 = dict(ev)
            ev2["block_str"] = block_str
            return _t(lang, "ev_withdraw_queued", **ev2)
        if t == "withdraw_processed":
            ev2 = dict(ev)
            ev2["block_str"] = block_str
            return _t(lang, "ev_withdraw_processed", **ev2)
        if t == "sell":
            ev2 = dict(ev)
            ev2["block_str"] = block_str
            return _t(lang, "ev_sell", **ev2)
        if t == "referral_bound":
            ev2 = dict(ev)
            ev2["block_str"] = block_str
            return _t(lang, "ev_referral_bound", **ev2)
        if t == "node_claim":
            ev2 = dict(ev)
            ev2["block_str"] = block_str
            return _t(lang, "ev_node_claim", **ev2)
        if t == "tx_failed_out_of_gas":
            ev2 = dict(ev)
            ev2["block_str"] = block_str
            ev2["gas_limit"] = _recommended_gas_limit_op()
            ev2["from_addr"] = ev.get("from_addr") or ""
            ev2["gas_limit_tx"] = ev.get("gas_limit_tx", "")
            ev2["gas_used"] = ev.get("gas_used", "")
            ev2["gas_pct"] = ev.get("gas_pct", "")
            return _t(lang, "ev_tx_failed_out_of_gas", **ev2)
        return ""

    def get_address_earnings_text(self, addr: str, lang: str = DEFAULT_LANG) -> str:
        a = Web3.to_checksum_address(addr)

        pending_static = int(self.controller_contract.functions.pendingStaticNaio(a).call())
        pending_naio = int(self.controller_contract.functions.pendingNaio(a).call())
        pending_usdt = int(self.controller_contract.functions.pendingUsdt(a).call())

        unsettled = 0
        try:
            acc = int(self.controller_contract.functions.accRewardPerPower().call())
            u = self.controller_contract.functions.users(a).call()

            power = int(u[1])
            reward_debt = int(u[6])
            accumulated = (power * acc) // (10**18)
            if accumulated > reward_debt:
                unsettled = accumulated - reward_debt
        except Exception:
            unsettled = 0

        node_usdt = 0
        node_naio = 0
        if self.node_seat_pool_contract is not None:
            try:
                node_usdt = int(self.node_seat_pool_contract.functions.pendingUsdt(a).call())
                node_naio = int(self.node_seat_pool_contract.functions.pendingNaio(a).call())
            except Exception:
                node_usdt = 0
                node_naio = 0

        ps = _fmt_amount(pending_static, self.naio_decimals, self.display_naio_decimals)
        pa = _fmt_amount(pending_naio, self.naio_decimals, self.display_naio_decimals)
        pu = _fmt_amount(pending_usdt, self.usdt_decimals, self.display_usdt_decimals)
        us = _fmt_amount(unsettled, self.naio_decimals, self.display_naio_decimals)
        nu = _fmt_amount(node_usdt, self.usdt_decimals, self.display_usdt_decimals)
        na = _fmt_amount(node_naio, self.naio_decimals, self.display_naio_decimals)

        lines: list[str] = []
        lines.append(_t(lang, "earn_title"))
        lines.append(_t(lang, "earn_address", addr=f"`{a}`"))
        lines.append("")
        lines.append(_t(lang, "earn_claimable"))
        lines.append(_t(lang, "earn_static", amt=ps))
        lines.append(_t(lang, "earn_dynamic", amt=pa))
        if node_usdt > 0 or node_naio > 0:
            lines.append(_t(lang, "earn_node", usdt=nu, naio=na))
        else:
            lines.append(_t(lang, "earn_node", usdt=nu, naio=na))
        if pending_usdt > 0:
            lines.append(_t(lang, "earn_fixed_usdt", amt=pu))

        lines.append("")
        lines.append(_t(lang, "earn_estimated"))
        lines.append(_t(lang, "earn_unsettled", amt=us))
        lines.append(_t(lang, "earn_unsettled_note"))
        return "\n".join(lines)

    def get_address_info_text(self, addr: str, lang: str = DEFAULT_LANG) -> str:
        a = Web3.to_checksum_address(addr)

        def _call_with_retry(fn, default=None, retries: int = 2, delay: float = 0.3):
            for i in range(retries + 1):
                try:
                    return fn()
                except Exception as e:
                    if i >= retries:
                        logger.debug("address info call failed after retry: %s", e)
                        return default
                    time.sleep(delay * (i + 1))

        with ThreadPoolExecutor(max_workers=15) as exe:

            f_user_info = exe.submit(lambda: self.controller_contract.functions.users(a).call())

            f_pending_static = exe.submit(lambda: int(self.controller_contract.functions.pendingStaticNaio(a).call()))
            f_pending_naio = exe.submit(lambda: int(self.controller_contract.functions.pendingNaio(a).call()))
            f_pending_usdt = exe.submit(lambda: int(self.controller_contract.functions.pendingUsdt(a).call()))
            f_queue = exe.submit(lambda: int(self.controller_contract.functions.withdrawQueuedAmount(a).call()))

            f_total_sold = exe.submit(lambda: int(self.controller_contract.functions.totalSoldUsdt(a).call()))

            f_acc_reward = exe.submit(lambda: int(self.controller_contract.functions.accRewardPerPower().call()))
            f_current_epoch = exe.submit(lambda: int(self.controller_contract.functions.getCurrentEpoch().call()))
            f_last_poke_epoch = exe.submit(lambda: int(self.controller_contract.functions.lastPokeEpoch().call()))

            f_node_seats = exe.submit(lambda: int(self.node_seat_pool_contract.functions.balanceOf(a).call()) if self.node_seat_pool_contract else 0)
            f_node_usdt = exe.submit(lambda: int(self.node_seat_pool_contract.functions.pendingUsdt(a).call()) if self.node_seat_pool_contract else 0)
            f_node_naio = exe.submit(lambda: int(self.node_seat_pool_contract.functions.pendingNaio(a).call()) if self.node_seat_pool_contract else 0)
            f_total_claimed_earnings = exe.submit(lambda: int(self.controller_contract.functions.totalClaimedEarningsUsdt(a).call()))
            f_addr_code = exe.submit(lambda: bytes(self.w3.eth.get_code(a)))
            f_addr_nonce = exe.submit(lambda: int(self.w3.eth.get_transaction_count(a)))
            f_addr_native_balance = exe.submit(lambda: int(self.w3.eth.get_balance(a)))

            user_info = _call_with_retry(f_user_info.result, default=())
            pending_static = _call_with_retry(f_pending_static.result, default=0)
            pending_naio = _call_with_retry(f_pending_naio.result, default=0)
            pending_usdt = _call_with_retry(f_pending_usdt.result, default=0)
            queue_amount = _call_with_retry(f_queue.result, default=0)
            total_sold = _call_with_retry(f_total_sold.result, default=0)
            acc_reward = _call_with_retry(f_acc_reward.result, default=0)
            current_epoch = _call_with_retry(f_current_epoch.result, default=0)
            last_poke_epoch = _call_with_retry(f_last_poke_epoch.result, default=0)
            if int(last_poke_epoch) == 4294967295:
                last_poke_epoch = -1
            node_seats = _call_with_retry(f_node_seats.result, default=0)
            node_usdt = _call_with_retry(f_node_usdt.result, default=0)
            node_naio = _call_with_retry(f_node_naio.result, default=0)
            total_claimed_earnings = _call_with_retry(f_total_claimed_earnings.result, default=0)
            addr_code = _call_with_retry(f_addr_code.result, default=None)
            addr_nonce = _call_with_retry(f_addr_nonce.result, default=None)
            addr_native_balance = _call_with_retry(f_addr_native_balance.result, default=None)

        principal = int(user_info[0]) if len(user_info) > 0 else 0
        power = int(user_info[1]) if len(user_info) > 1 else 0
        referrer = user_info[2] if len(user_info) > 2 else "0x0000000000000000000000000000000000000000"
        direct_count = int(user_info[3]) if len(user_info) > 3 else 0
        first_deposit_ts = int(user_info[5]) if len(user_info) > 5 else 0
        reward_debt = int(user_info[6]) if len(user_info) > 6 else 0
        withdrawn = int(user_info[10]) if len(user_info) > 10 else 0
        locked = int(user_info[11]) if len(user_info) > 11 else 0

        unsettled = 0
        if power > 0 and acc_reward > 0:
            accumulated = (power * acc_reward) // (10**18)
            if accumulated > reward_debt:
                unsettled = accumulated - reward_debt

        now_ts = int(time.time())
        time_since_first = max(0, now_ts - first_deposit_ts) if first_deposit_ts > 0 else 0

        month_epochs_raw = (os.getenv("WITHDRAW_MONTH_EPOCHS", "").strip() or "")
        if month_epochs_raw:
            month_epochs_i = max(1, int(month_epochs_raw))
        else:
            month_epochs_i = max(1, int(os.getenv("EPOCHS_PER_MONTH", "30")))
        month_secs = int(self.epoch_seconds) * month_epochs_i
        if first_deposit_ts == 0:
            unlock_bps = 0
        elif time_since_first > 2 * month_secs:
            unlock_bps = 8000
        elif time_since_first > 1 * month_secs:
            unlock_bps = 6000
        else:
            unlock_bps = 4000

        unlocked_by_time = (principal * unlock_bps) // 10000
        if unlocked_by_time > locked:
            unlocked_by_time = locked
        withdrawable_now = unlocked_by_time - withdrawn if unlocked_by_time > withdrawn else 0

        if principal > 0 and total_claimed_earnings >= principal * 2:
            withdrawable_now = 0
        unlock_rate = unlock_bps // 100

        sell_multiple = None
        if principal > 0 and total_sold > 0:

            sell_multiple_raw = (total_sold * 1e18) // principal
            sell_multiple = sell_multiple_raw / 1e18

        referrer_str = str(referrer) if referrer else "0x0000000000000000000000000000000000000000"
        try:

            if referrer_str.lower() in ("0x0000000000000000000000000000000000000000", "0x0", ""):
                referrer_addr = None
            else:
                referrer_addr = Web3.to_checksum_address(referrer_str)
        except Exception:
            referrer_addr = None

        address_type_line = None
        address_type_note = None
        if addr_code is not None:
            code_bytes = bytes(addr_code)
            if len(code_bytes) == 0:
                address_type_line = _t(lang, "info_address_type", kind=_t(lang, "info_address_type_eoa"))
                if int(addr_nonce or 0) == 0 and int(addr_native_balance or 0) == 0:
                    address_type_note = _t(lang, "info_address_type_note_inactive")
            else:
                address_type_line = _t(lang, "info_address_type", kind=_t(lang, "info_address_type_non_eoa"))
                if len(code_bytes) == 23 and code_bytes[:3] == b"\xef\x01\x00":
                    try:
                        delegate_target = Web3.to_checksum_address("0x" + code_bytes[3:].hex())
                    except Exception:
                        delegate_target = "0x" + code_bytes[3:].hex()
                    address_type_note = _t(
                        lang, "info_address_type_note_7702", target=_as_code(delegate_target)
                    )
                else:
                    address_type_note = _t(lang, "info_address_type_note_code")

        first_deposit_str = _fmt_ts_local(first_deposit_ts, lang) if first_deposit_ts > 0 else None

        principal_s = _fmt_amount(principal, self.usdt_decimals, self.display_usdt_decimals)
        power_s = _fmt_amount(power, 18, 2)
        locked_s = _fmt_amount(locked, self.usdt_decimals, self.display_usdt_decimals)
        withdrawn_s = _fmt_amount(withdrawn, self.usdt_decimals, self.display_usdt_decimals)
        queue_s = _fmt_amount(queue_amount, self.usdt_decimals, self.display_usdt_decimals)
        unlocked_by_time_s = _fmt_amount(unlocked_by_time, self.usdt_decimals, self.display_usdt_decimals)
        withdrawable_now_s = _fmt_amount(withdrawable_now, self.usdt_decimals, self.display_usdt_decimals)
        total_sold_s = _fmt_amount(total_sold, self.usdt_decimals, self.display_usdt_decimals)
        total_claimed_earnings_s = _fmt_amount(total_claimed_earnings, self.usdt_decimals, self.display_usdt_decimals)
        pending_static_s = _fmt_amount(pending_static, self.naio_decimals, self.display_naio_decimals)
        pending_naio_s = _fmt_amount(pending_naio, self.naio_decimals, self.display_naio_decimals)
        pending_usdt_s = _fmt_amount(pending_usdt, self.usdt_decimals, self.display_usdt_decimals)
        unsettled_s = _fmt_amount(unsettled, self.naio_decimals, self.display_naio_decimals)
        node_usdt_s = _fmt_amount(node_usdt, self.usdt_decimals, self.display_usdt_decimals)
        node_naio_s = _fmt_amount(node_naio, self.naio_decimals, self.display_naio_decimals)

        query_day = int(last_poke_epoch) if int(last_poke_epoch) >= 0 else (int(current_epoch) - 1 if int(current_epoch) > 0 else 0)
        new_user_pool = 0
        new_user_total_power = 0
        new_user_user_power = 0
        if query_day >= 0:
            new_user_pool = _call_with_retry(
                lambda: int(self.controller_contract.functions.newUserRewardNaioByDay(query_day).call()), default=0
            )
            new_user_total_power = _call_with_retry(
                lambda: int(self.controller_contract.functions.newUserTotalPowerByDay(query_day).call()), default=0
            )
            new_user_user_power = _call_with_retry(
                lambda: int(self.controller_contract.functions.newUserEligiblePower(query_day, a).call()), default=0
            )
        new_user_estimated = 0
        if new_user_total_power > 0 and new_user_user_power > 0 and new_user_pool > 0:
            new_user_estimated = (new_user_pool * new_user_user_power) // new_user_total_power
        new_user_pool_s = _fmt_amount(new_user_pool, self.naio_decimals, self.display_naio_decimals)
        new_user_total_power_s = _fmt_amount(new_user_total_power, 18, 2)
        new_user_user_power_s = _fmt_amount(new_user_user_power, 18, 2)
        new_user_estimated_s = _fmt_amount(new_user_estimated, self.naio_decimals, self.display_naio_decimals)

        lines: list[str] = []
        lines.append(_t(lang, "info_title"))
        lines.append(_t(lang, "info_address", addr=_as_code(a)))
        if address_type_line:
            lines.append(address_type_line)
        if address_type_note:
            lines.append(address_type_note)
        lines.append("")

        lines.append(_t(lang, "info_basic"))
        lines.append(_t(lang, "info_principal", amt=principal_s))
        lines.append(_t(lang, "info_total_claimed_earnings", amt=total_claimed_earnings_s))
        lines.append(_t(lang, "info_power", amt=power_s))
        if referrer_addr:
            lines.append(_t(lang, "info_referrer", addr=_as_code(referrer_addr)))
        else:
            lines.append(_t(lang, "info_referrer_none"))
        lines.append(_t(lang, "info_direct_count", count=direct_count))

        if query_downline_deposits is not None:
            try:

                system_start_ts = 0
                try:
                    system_start_ts = int(self.controller_contract.functions.systemStartTs().call())
                except Exception:
                    pass

                downline_last_week = query_downline_deposits(self.price_db_path, a, "last_week", system_start_ts)
                downline_this_week = query_downline_deposits(self.price_db_path, a, "week", system_start_ts)
                downline_this_month = query_downline_deposits(self.price_db_path, a, "month", system_start_ts)
                downline_total = query_downline_deposits(self.price_db_path, a, "all", system_start_ts)
                lines.append("")

                timezone_offset = int(downline_total.get("timezone_offset", 0) or 0)
                if timezone_offset != 0:
                    tz_sign = "+" if timezone_offset >= 0 else ""
                    tz_text = f" (UTC{tz_sign}{timezone_offset})"
                else:
                    tz_text = " (UTC)"
                lines.append(_t(lang, "info_downline_stats") + tz_text)

                last_week_amt = int(downline_last_week.get("total_deposit_wei", 0) or 0)
                this_week_amt = int(downline_this_week.get("total_deposit_wei", 0) or 0)
                this_month_amt = int(downline_this_month.get("total_deposit_wei", 0) or 0)
                total_amt = int(downline_total.get("total_deposit_wei", 0) or 0)
                lines.append(
                    _t(lang, "info_downline_last_week", amt=_fmt_amount(last_week_amt, self.usdt_decimals, self.display_usdt_decimals))
                )
                lines.append(
                    _t(lang, "info_downline_this_week", amt=_fmt_amount(this_week_amt, self.usdt_decimals, self.display_usdt_decimals))
                )
                lines.append(
                    _t(lang, "info_downline_this_month", amt=_fmt_amount(this_month_amt, self.usdt_decimals, self.display_usdt_decimals))
                )
                lines.append(
                    _t(lang, "info_downline_total", amt=_fmt_amount(total_amt, self.usdt_decimals, self.display_usdt_decimals))
                )
            except Exception as e:
                lines.append("")
                lines.append(_t(lang, "info_downline_stats"))
                lines.append(_t(lang, "info_downline_query_failed", err=str(e)))
                logger.debug(f"Failed to query team performance: {e}")

        lines.append("")

        lines.append(_t(lang, "info_new_user_reward"))
        lines.append(_t(lang, "info_new_user_day", day=query_day))
        lines.append(_t(lang, "info_new_user_pool", amt=new_user_pool_s))
        lines.append(_t(lang, "info_new_user_total_power", amt=new_user_total_power_s))
        lines.append(_t(lang, "info_new_user_user_power", amt=new_user_user_power_s))
        lines.append(_t(lang, "info_new_user_estimated", amt=new_user_estimated_s))
        lines.append("")

        lines.append(_t(lang, "info_earnings"))
        lines.append(_t(lang, "earn_static", amt=pending_static_s))
        lines.append(_t(lang, "earn_dynamic", amt=pending_naio_s))
        if pending_usdt > 0:
            lines.append(_t(lang, "earn_fixed_usdt", amt=pending_usdt_s))
        lines.append(_t(lang, "earn_unsettled", amt=unsettled_s))
        lines.append("")

        lines.append(_t(lang, "info_withdraw"))
        if first_deposit_str:
            lines.append(_t(lang, "info_first_deposit", ts=first_deposit_str))
        else:
            lines.append(_t(lang, "info_first_deposit_none"))
        lines.append(_t(lang, "info_locked", amt=locked_s))
        lines.append(_t(lang, "info_withdrawn", amt=withdrawn_s))
        lines.append(_t(lang, "info_unlock_rate", rate=unlock_rate))
        lines.append(_t(lang, "info_unlocked_by_time", amt=unlocked_by_time_s))
        lines.append(_t(lang, "info_withdrawable_now", amt=withdrawable_now_s))
        if queue_amount > 0:
            lines.append(_t(lang, "info_queue", amt=queue_s))
        else:
            lines.append(_t(lang, "info_queue_none"))
        lines.append("")

        lines.append(_t(lang, "info_trading"))
        lines.append(_t(lang, "info_total_sold", amt=total_sold_s))
        if sell_multiple is not None:

            multiple_display = f"{sell_multiple:.2f}" if sell_multiple >= 1.0 else f"{sell_multiple:.4f}"
            lines.append(_t(lang, "info_sell_multiple", multiple=multiple_display))
        else:
            lines.append(_t(lang, "info_sell_multiple_none"))

        if self.node_seat_pool_contract is not None:
            lines.append("")
            lines.append(_t(lang, "info_node"))
            if node_seats > 0:
                lines.append(_t(lang, "info_node_seats", count=node_seats))
                if node_usdt > 0 or node_naio > 0:
                    lines.append(_t(lang, "earn_node", usdt=node_usdt_s, naio=node_naio_s))
            else:
                lines.append(_t(lang, "info_node_seats_none"))

        return "\n".join(lines)

    def subscribe(self, chat_id: int) -> bool:
        cid = int(chat_id)
        subs = set(self._persist.subscribers)
        if cid in subs:
            return False
        subs.add(cid)
        self._persist.subscribers = sorted(subs)
        _save_state(self.state_file, self._persist)
        return True

    def unsubscribe(self, chat_id: int) -> bool:
        cid = int(chat_id)
        subs = set(self._persist.subscribers)
        if cid not in subs:
            return False
        subs.remove(cid)
        self._persist.subscribers = sorted(subs)
        _save_state(self.state_file, self._persist)
        return True

    def register_chat(self, chat) -> None:
        try:
            cid = str(int(chat.id))
        except Exception:
            return
        chat_type = getattr(chat, "type", "unknown")
        title = getattr(chat, "title", "") or ""
        existing = self._persist.chats.get(cid) or {}
        broadcast = existing.get("broadcast")
        if broadcast is None:

            broadcast = chat_type in ("group", "supergroup")
        lang = existing.get("lang") or DEFAULT_LANG
        self._persist.chats[cid] = {
            "type": chat_type,
            "title": title,
            "broadcast": bool(broadcast),
            "lang": lang,
        }
        _save_state(self.state_file, self._persist)

    def get_lang(self, chat_id: int) -> str:
        try:
            cid = str(int(chat_id))
        except Exception:
            return DEFAULT_LANG
        meta = self._persist.chats.get(cid) or {}
        lang = meta.get("lang") or DEFAULT_LANG
        if lang not in LANG_LABELS:
            return DEFAULT_LANG
        return lang

    def set_lang(self, chat_id: int, lang: str) -> None:
        if lang not in LANG_LABELS:
            return
        try:
            cid = str(int(chat_id))
        except Exception:
            return
        meta = self._persist.chats.get(cid) or {}
        meta["lang"] = lang
        self._persist.chats[cid] = meta
        _save_state(self.state_file, self._persist)

    def unregister_chat(self, chat_id: int) -> None:
        cid = str(int(chat_id))
        if cid in self._persist.chats:
            self._persist.chats.pop(cid, None)
            _save_state(self.state_file, self._persist)

    def get_broadcast_chat_ids(self) -> list[int]:
        ids: list[int] = []
        for cid, meta in (self._persist.chats or {}).items():
            try:
                if not meta.get("broadcast"):
                    continue
                ctype = meta.get("type", "")
                if ctype not in ("group", "supergroup"):
                    continue
                ids.append(int(cid))
            except Exception:
                continue
        return sorted(set(ids))

    def _scan_broadcast_events_sync(self) -> list[str]:
        if not self.broadcast_any_enabled:
            return []
        if not self.get_broadcast_chat_ids():
            return []

        head = self.w3.eth.block_number
        safe_head = max(0, head - self.broadcast_confirmations)
        if safe_head < self._persist.cursor_block:
            return []

        from_block = self._persist.cursor_block
        to_block = min(safe_head, from_block + max(1, self.max_blocks_per_scan))

        sig_deposit = Web3.keccak(text="DepositFromTransfer(address,uint256,bytes32)").hex()
        if isinstance(sig_deposit, str) and not sig_deposit.startswith("0x"):
            sig_deposit = "0x" + sig_deposit
        sig_refund = Web3.keccak(text="DepositRefunded(address,uint256,bytes32,uint8)").hex()
        if isinstance(sig_refund, str) and not sig_refund.startswith("0x"):
            sig_refund = "0x" + sig_refund
        sig_deflation_detail = Web3.keccak(
            text="DeflationExecutedDetailed(uint32,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256)"
        ).hex()
        if isinstance(sig_deflation_detail, str) and not sig_deflation_detail.startswith("0x"):
            sig_deflation_detail = "0x" + sig_deflation_detail
        sig_static = Web3.keccak(text="StaticRewardClaimed(address,uint256)").hex()
        if isinstance(sig_static, str) and not sig_static.startswith("0x"):
            sig_static = "0x" + sig_static
        sig_dynamic = Web3.keccak(text="DynamicRewardClaimed(address,uint256)").hex()
        if isinstance(sig_dynamic, str) and not sig_dynamic.startswith("0x"):
            sig_dynamic = "0x" + sig_dynamic
        sig_new_user = Web3.keccak(text="NewUserRewardClaimed(address,uint32,uint256)").hex()
        if isinstance(sig_new_user, str) and not sig_new_user.startswith("0x"):
            sig_new_user = "0x" + sig_new_user
        sig_wq = Web3.keccak(text="WithdrawQueued(address,uint256)").hex()
        if isinstance(sig_wq, str) and not sig_wq.startswith("0x"):
            sig_wq = "0x" + sig_wq
        sig_wp = Web3.keccak(text="WithdrawProcessed(address,uint256,uint256,uint256,uint256)").hex()
        if isinstance(sig_wp, str) and not sig_wp.startswith("0x"):
            sig_wp = "0x" + sig_wp
        sig_node_claim = Web3.keccak(text="Claimed(uint16,address,uint256,uint256)").hex()
        if isinstance(sig_node_claim, str) and not sig_node_claim.startswith("0x"):
            sig_node_claim = "0x" + sig_node_claim
        sig_referral_bound = Web3.keccak(text="ReferralBound(address,address)").hex()
        if isinstance(sig_referral_bound, str) and not sig_referral_bound.startswith("0x"):
            sig_referral_bound = "0x" + sig_referral_bound
        sig_lp_withdrawn = Web3.keccak(text="LPWithdrawn(address,uint256,uint256,uint256)").hex()
        if isinstance(sig_lp_withdrawn, str) and not sig_lp_withdrawn.startswith("0x"):
            sig_lp_withdrawn = "0x" + sig_lp_withdrawn

        topics0: list[str] = []
        if self.broadcast_deposits_enabled:
            topics0.extend([sig_deposit, sig_refund])
        if self.broadcast_deflation_enabled:
            topics0.append(sig_deflation_detail)
        if self.broadcast_ops_enabled:
            topics0.extend([sig_static, sig_dynamic, sig_new_user, sig_wq, sig_wp, sig_lp_withdrawn, sig_referral_bound])
            if self.node_seat_pool_contract is not None:
                topics0.append(sig_node_claim)
        events: list[dict] = []
        if topics0:
            logs = self.w3.eth.get_logs(
                {
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "address": [x for x in [self.controller, self.node_seat_pool, self.naio] if x],
                    "topics": [topics0],
                }
            )
            logs = sorted(logs, key=lambda x: (x.get("blockNumber", 0), x.get("logIndex", 0)))
            price_suffix_cache: dict[int, str] = {}
            rule_pool_cache: dict[int, str] = {}
            deposit_tx_block_cache: dict[str, int] = {}
            for lg in logs:
                try:
                    topic0 = lg["topics"][0].hex()
                    if not topic0.startswith("0x"):
                        topic0 = "0x" + topic0
                    tx_hash = lg.get("transactionHash")
                    event_txh = ""
                    try:
                        if tx_hash is not None:
                            event_txh = tx_hash.hex()
                    except Exception:
                        event_txh = ""
                    if self.broadcast_deposits_enabled and topic0.lower() == sig_deposit.lower():
                        ev = self.controller_contract.events.DepositFromTransfer().process_log(lg)
                        user = ev["args"]["user"]
                        usdt_amount = int(ev["args"]["usdtAmount"])
                        orig_txh = ev["args"]["txHash"].hex()
                        block_no = int(lg.get("blockNumber") or 0)
                        orig_block_no = deposit_tx_block_cache.get(orig_txh, -1)
                        if orig_block_no < 0:
                            try:
                                rcpt = self.w3.eth.get_transaction_receipt(orig_txh)
                                orig_block_no = int(rcpt.get("blockNumber") or 0)
                            except Exception:
                                orig_block_no = 0
                            deposit_tx_block_cache[orig_txh] = orig_block_no
                        display_block_no = orig_block_no

                        pool_block = display_block_no if display_block_no > 0 else block_no
                        rule_pool_line, price_suffix = self._get_deposit_rule_pool_and_price(
                            pool_block, usdt_amount, bookkeeping_block=block_no
                        )
                        amt = _fmt_amount(usdt_amount, self.usdt_decimals, self.display_usdt_decimals)
                        events.append(
                            {
                                "type": "deposit",
                                "user": user,
                                "amt": amt,
                                "txh": orig_txh,
                                "block": display_block_no,
                                "bookkeeping_txh": event_txh,
                                "bookkeeping_block": block_no,
                                "rule_pool_line": rule_pool_line,
                                "suffix": price_suffix,
                            }
                        )
                    elif self.broadcast_deposits_enabled and topic0.lower() == sig_refund.lower():
                        ev = self.controller_contract.events.DepositRefunded().process_log(lg)
                        user = ev["args"]["user"]
                        usdt_amount = int(ev["args"]["usdtAmount"])
                        reason = int(ev["args"]["reason"])
                        orig_txh = ev["args"]["txHash"].hex()
                        block_no = int(lg.get("blockNumber") or 0)
                        orig_block_no = deposit_tx_block_cache.get(orig_txh, -1)
                        if orig_block_no < 0:
                            try:
                                rcpt = self.w3.eth.get_transaction_receipt(orig_txh)
                                orig_block_no = int(rcpt.get("blockNumber") or 0)
                            except Exception:
                                orig_block_no = 0
                            deposit_tx_block_cache[orig_txh] = orig_block_no
                        display_block_no = orig_block_no
                        price_block_no = display_block_no if display_block_no > 0 else block_no
                        amt = _fmt_amount(usdt_amount, self.usdt_decimals, self.display_usdt_decimals)
                        if reason == 1:
                            reason_text = "refund_reason_lt100"
                        elif reason == 2:
                            reason_text = "refund_reason_gt1000"
                        elif reason == 3:
                            reason_text = "refund_reason_paused"
                        else:
                            reason_text = "refund_reason_unknown"
                        events.append(
                            {
                                "type": "refund",
                                "user": user,
                                "amt": amt,
                                "reason": reason_text,
                                "code": reason,
                                "txh": orig_txh,
                                "block": display_block_no,
                                "rule_pool_line": self._get_rule_pool_line(price_block_no, rule_pool_cache),
                                "suffix": self._get_price_suffix(price_block_no, price_suffix_cache),
                            }
                        )
                    elif self.broadcast_deflation_enabled and topic0.lower() == sig_deflation_detail.lower():
                        ev = self.controller_contract.events.DeflationExecutedDetailed().process_log(lg)
                        ts = int(ev["args"]["timestamp"])
                        epoch = int(ev["args"]["epoch"])
                        rate_bps = int(ev["args"]["rateBps"])
                        burn_amount = int(ev["args"]["burnAmount"])
                        eco_amount = int(ev["args"]["ecoAmount"])
                        new_user_amount = int(ev["args"]["newUserAmount"])
                        node_amount = int(ev["args"]["nodeAmount"])
                        independent_amount = int(ev["args"]["independentAmount"])
                        referral_amount = int(ev["args"]["referralAmount"])
                        static_amount = int(ev["args"]["staticAmount"])
                        deflation_amount = int(ev["args"]["deflationAmount"])
                        price_before = int(ev["args"]["priceBefore"])
                        withdraw_add = int(ev["args"]["withdrawBurnConsumed"])

                        burn_value_usdt = (burn_amount * price_before) // (10**18) if burn_amount > 0 and price_before > 0 else 0

                        block_no = int(lg.get("blockNumber") or 0)
                        events.append(
                            {
                                "type": "deflation",
                                "ts": ts,
                                "epoch": epoch,
                                "rate_bps": rate_bps,
                                "rate_pct": self._fmt_bps_percent(rate_bps),
                                "deflation": _fmt_amount(deflation_amount, self.naio_decimals, self.display_naio_decimals),
                                "burned": _fmt_amount(burn_amount, self.naio_decimals, self.display_naio_decimals),
                                "burn_value": _fmt_amount(burn_value_usdt, self.usdt_decimals, self.display_usdt_decimals),
                                "eco": _fmt_amount(eco_amount, self.naio_decimals, self.display_naio_decimals),
                                "new_user": _fmt_amount(new_user_amount, self.naio_decimals, self.display_naio_decimals),
                                "node": _fmt_amount(node_amount, self.naio_decimals, self.display_naio_decimals),
                                "independent": _fmt_amount(independent_amount, self.naio_decimals, self.display_naio_decimals),
                                "dynamic": _fmt_amount(referral_amount, self.naio_decimals, self.display_naio_decimals),
                                "static": _fmt_amount(static_amount, self.naio_decimals, self.display_naio_decimals),
                                "withdraw_add": _fmt_amount(withdraw_add, self.naio_decimals, self.display_naio_decimals),
                                "block": block_no,
                                "txh": event_txh,
                                "suffix": self._get_price_suffix(block_no, price_suffix_cache),
                            }
                        )
                    elif self.broadcast_ops_enabled and topic0.lower() == sig_static.lower():
                        ev = self.controller_contract.events.StaticRewardClaimed().process_log(lg)
                        user = ev["args"]["user"]
                        amount = int(ev["args"]["amount"])
                        block_no = int(lg.get("blockNumber") or 0)
                        amt = _fmt_amount(amount, self.naio_decimals, self.display_naio_decimals)
                        events.append(
                            {
                                "type": "static_claim",
                                "user": user,
                                "amt": amt,
                                "block": block_no,
                                "txh": event_txh,
                                "suffix": self._get_price_suffix(block_no, price_suffix_cache),
                            }
                        )
                    elif self.broadcast_ops_enabled and topic0.lower() == sig_dynamic.lower():
                        ev = self.controller_contract.events.DynamicRewardClaimed().process_log(lg)
                        user = ev["args"]["user"]
                        amount = int(ev["args"]["amount"])
                        block_no = int(lg.get("blockNumber") or 0)
                        amt = _fmt_amount(amount, self.naio_decimals, self.display_naio_decimals)
                        events.append(
                            {
                                "type": "dynamic_claim",
                                "user": user,
                                "amt": amt,
                                "block": block_no,
                                "txh": event_txh,
                                "suffix": self._get_price_suffix(block_no, price_suffix_cache),
                            }
                        )
                    elif self.broadcast_ops_enabled and topic0.lower() == sig_new_user.lower():

                        topics = lg.get("topics") or []
                        if len(topics) < 3:
                            continue
                        user = Web3.to_checksum_address("0x" + topics[1].hex()[-40:])
                        day = int(topics[2].hex(), 16)
                        data_raw = lg.get("data", b"")
                        if isinstance(data_raw, str):
                            data_raw = bytes.fromhex(data_raw[2:] if data_raw.startswith("0x") else data_raw)
                        amount = int.from_bytes(data_raw, byteorder="big", signed=False)
                        block_no = int(lg.get("blockNumber") or 0)
                        amt = _fmt_amount(amount, self.naio_decimals, self.display_naio_decimals)
                        events.append(
                            {
                                "type": "new_user_claim",
                                "user": user,
                                "day": day,
                                "amt": amt,
                                "block": block_no,
                                "txh": event_txh,
                                "suffix": self._get_price_suffix(block_no, price_suffix_cache),
                            }
                        )
                    elif self.broadcast_ops_enabled and topic0.lower() == sig_wq.lower():
                        ev = self.controller_contract.events.WithdrawQueued().process_log(lg)
                        user = ev["args"]["user"]
                        amount = int(ev["args"]["amount"])
                        block_no = int(lg.get("blockNumber") or 0)
                        amt = _fmt_amount(amount, self.usdt_decimals, self.display_usdt_decimals)
                        events.append(
                            {
                                "type": "withdraw_queued",
                                "user": user,
                                "amt": amt,
                                "block": block_no,
                                "txh": event_txh,
                                "suffix": self._get_price_suffix(block_no, price_suffix_cache),
                            }
                        )
                    elif self.broadcast_ops_enabled and topic0.lower() == sig_wp.lower():
                        ev = self.controller_contract.events.WithdrawProcessed().process_log(lg)
                        user = ev["args"]["user"]
                        usdt_returned = int(ev["args"]["usdtReturned"])
                        naio_burned = int(ev["args"]["naioBurned"])
                        burn_used = int(ev["args"]["dailyBurnUsed"])
                        burn_remain = int(ev["args"]["dailyBurnRemaining"])
                        block_no = int(lg.get("blockNumber") or 0)
                        usdt_s = _fmt_amount(usdt_returned, self.usdt_decimals, self.display_usdt_decimals)
                        burn_s = _fmt_amount(naio_burned, self.naio_decimals, self.display_naio_decimals)
                        used_s = _fmt_amount(burn_used, self.naio_decimals, self.display_naio_decimals)
                        remain_s = _fmt_amount(burn_remain, self.naio_decimals, self.display_naio_decimals)
                        events.append(
                            {
                                "type": "withdraw_processed",
                                "user": user,
                                "usdt": usdt_s,
                                "burn": burn_s,
                                "daily_used": used_s,
                                "daily_remain": remain_s,
                                "block": block_no,
                                "txh": event_txh,
                                "suffix": self._get_price_suffix(block_no, price_suffix_cache),
                            }
                        )
                    elif self.broadcast_ops_enabled and topic0.lower() == sig_lp_withdrawn.lower():

                        ev = self.controller_contract.events.LPWithdrawn().process_log(lg)
                        user = ev["args"]["user"]
                        lp_amount = int(ev["args"]["lpAmount"])
                        usdt_returned = int(ev["args"]["usdtReturned"])
                        token_burned = int(ev["args"]["tokenBurned"])
                        block_no = int(lg.get("blockNumber") or 0)
                        if lp_amount > 0:
                            sold_s = _fmt_amount(lp_amount, self.naio_decimals, self.display_naio_decimals)
                            usdt_s = _fmt_amount(usdt_returned, self.usdt_decimals, self.display_usdt_decimals)
                            burn_s = _fmt_amount(token_burned, self.naio_decimals, self.display_naio_decimals)
                            events.append(
                                {
                                    "type": "sell",
                                    "user": user,
                                    "sold": sold_s,
                                    "usdt": usdt_s,
                                    "burn": burn_s,
                                    "block": block_no,
                                    "txh": event_txh,
                                    "suffix": self._get_price_suffix(block_no, price_suffix_cache),
                                }
                            )
                    elif self.broadcast_ops_enabled and topic0.lower() == sig_referral_bound.lower():
                        ev = self.controller_contract.events.ReferralBound().process_log(lg)
                        user = ev["args"]["user"]
                        inviter = ev["args"]["inviter"]
                        block_no = int(lg.get("blockNumber") or 0)
                        events.append(
                            {
                                "type": "referral_bound",
                                "user": user,
                                "inviter": inviter,
                                "block": block_no,
                                "txh": event_txh,
                                "suffix": self._get_price_suffix(block_no, price_suffix_cache),
                            }
                        )
                    elif (
                        self.broadcast_ops_enabled
                        and self.node_seat_pool_contract is not None
                        and topic0.lower() == sig_node_claim.lower()
                    ):
                        ev = self.node_seat_pool_contract.events.Claimed().process_log(lg)
                        owner = ev["args"]["owner"]
                        usdt_amount = int(ev["args"]["usdtAmount"])
                        naio_amount = int(ev["args"]["naioAmount"])
                        block_no = int(lg.get("blockNumber") or 0)
                        usdt_s = _fmt_amount(usdt_amount, self.usdt_decimals, self.display_usdt_decimals)
                        naio_s = _fmt_amount(naio_amount, self.naio_decimals, self.display_naio_decimals)
                        events.append(
                            {
                                "type": "node_claim",
                                "user": owner,
                                "usdt": usdt_s,
                                "naio": naio_s,
                                "block": block_no,
                                "txh": event_txh,
                                "suffix": self._get_price_suffix(block_no, price_suffix_cache),
                            }
                        )
                except Exception as e:
                    logger.debug("process log failed: %s", e)

        if self.broadcast_failed_out_of_gas and self.controller:
            for block_num in range(from_block, to_block + 1):
                try:
                    block = self.w3.eth.get_block(block_num, full_transactions=True)
                except Exception as e:
                    logger.debug("get_block failed block=%s: %s", block_num, e)
                    continue
                txs = getattr(block, "transactions", None) or []
                for tx in txs:
                    to_addr = tx.get("to")
                    if to_addr is None:
                        continue
                    try:
                        to_checksum = Web3.to_checksum_address(to_addr)
                    except Exception:
                        continue
                    if to_checksum != self.controller:
                        continue
                    tx_gas = int(tx.get("gas") or 0)
                    if tx_gas <= 0:
                        continue
                    tx_hash = tx.get("hash")
                    try:
                        txh_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else (tx_hash if isinstance(tx_hash, str) else "")
                    except Exception:
                        txh_hex = ""
                    if not txh_hex or not txh_hex.startswith("0x"):
                        txh_hex = "0x" + (tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash))
                    try:
                        receipt = self.w3.eth.get_transaction_receipt(tx_hash)
                    except Exception as e:
                        logger.debug("get_transaction_receipt failed tx=%s: %s", txh_hex, e)
                        continue
                    if receipt is None:
                        continue
                    status = int(receipt.get("status", 1))
                    if status != 0:
                        continue
                    gas_used = int(receipt.get("gasUsed") or 0)

                    if gas_used >= (tx_gas * 99 // 100):
                        from_addr = tx.get("from")
                        try:
                            from_hex = (
                                Web3.to_checksum_address(from_addr)
                                if from_addr is not None
                                else ""
                            )
                        except Exception:
                            from_hex = str(from_addr) if from_addr is not None else ""
                        gas_pct = round(gas_used * 100.0 / tx_gas, 1) if tx_gas > 0 else 100.0
                        events.append(
                            {
                                "type": "tx_failed_out_of_gas",
                                "txh": txh_hex,
                                "block": block_num,
                                "from_addr": from_hex,
                                "gas_limit_tx": tx_gas,
                                "gas_used": gas_used,
                                "gas_pct": gas_pct,
                            }
                        )

        self._persist.cursor_block = to_block + 1
        _save_state(self.state_file, self._persist)
        return events

STATE: Optional[BotState] = None

def _menu_kb(lang: str = DEFAULT_LANG) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(_t(lang, "menu_info_query"), callback_data="info_query")],
            [InlineKeyboardButton(_t(lang, "menu_chain_info"), callback_data="chain_info"),
             InlineKeyboardButton(_t(lang, "menu_more"), callback_data="more_menu")],
        ]
    )

def _more_menu_kb(lang: str = DEFAULT_LANG) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(_t(lang, "menu_help_ops"), callback_data="help_ops")],
            [InlineKeyboardButton(_t(lang, "menu_addr_list"), callback_data="menu_addrs")],
            [InlineKeyboardButton(_t(lang, "menu_cmd_list"), callback_data="cmd_list")],
            [InlineKeyboardButton(_t(lang, "menu_back"), callback_data="back_to_main")],
        ]
    )

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    s = STATE
    if s is not None and update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if s and update.effective_chat else DEFAULT_LANG
    await update.message.reply_text(_t(lang, "menu_select"), reply_markup=_menu_kb(lang))

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    s = STATE
    if s is None:
        await _reply_text_with_mention(update, _t(DEFAULT_LANG, "bot_init_failed"))
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG
    try:
        msg = s.get_chain_info_text(lang)
    except Exception as e:
        msg = _t(lang, "query_failed", err=e)
    await _reply_text_with_mention(update, msg, reply_markup=_menu_kb(lang))

async def subscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    s = STATE
    if s is None:
        await update.message.reply_text(_t(DEFAULT_LANG, "bot_init_failed"))
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG
    if not s.broadcast_deposits_enabled:
        await update.message.reply_text(_t(lang, "broadcast_disabled"))
        return
    ok = s.subscribe(update.message.chat_id)
    if ok:
        await update.message.reply_text(_t(lang, "subscribe_ok"), reply_markup=_menu_kb(lang))
    else:
        await update.message.reply_text(_t(lang, "subscribe_already"), reply_markup=_menu_kb(lang))

async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    s = STATE
    if s is None:
        await update.message.reply_text(_t(DEFAULT_LANG, "bot_init_failed"))
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG
    ok = s.unsubscribe(update.message.chat_id)
    if ok:
        await update.message.reply_text(_t(lang, "unsubscribe_ok"), reply_markup=_menu_kb(lang))
    else:
        await update.message.reply_text(_t(lang, "unsubscribe_none"), reply_markup=_menu_kb(lang))

def _get_nodes_list_path() -> str:
    p = os.getenv("NODES_LIST_PATH", "").strip()
    if p:
        return p
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "contracts", "nodes_list.txt")

async def nodes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    s = STATE
    if s is None:
        await _reply_text_with_mention(update, _t(DEFAULT_LANG, "bot_init_failed"))
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG

    path = _get_nodes_list_path()
    if not os.path.isfile(path):
        await _reply_text_with_mention(update, _t(lang, "nodes_unavailable"), reply_markup=_menu_kb(lang))
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.warning("Failed to read nodes_list: %s", e)
        await _reply_text_with_mention(update, _t(lang, "nodes_unavailable"), reply_markup=_menu_kb(lang))
        return

    doc = InputFile(io.BytesIO(content.encode("utf-8")), filename="nodes_list.txt")
    try:
        await update.message.reply_document(document=doc, reply_markup=_menu_kb(lang))
        await update.message.reply_text(_t(lang, "nodes_sent"), reply_markup=_menu_kb(lang))
    except Exception as e:
        logger.warning("Failed to send nodes document: %s", e)
        await _reply_text_with_mention(update, _t(lang, "nodes_unavailable"), reply_markup=_menu_kb(lang))

async def _is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return False
    if getattr(chat, "type", "") == "private":
        return True
    try:
        m = await context.bot.get_chat_member(chat.id, user.id)
        return m.status in ("administrator", "creator", "owner")
    except Exception:
        return False

async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    s = STATE
    if s is None:
        await _reply_text_with_mention(update, _t(DEFAULT_LANG, "bot_init_failed"))
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG

    if not await _is_group_admin(update, context):
        await _reply_text_with_mention(update, _t(lang, "lang_admin_only"), reply_markup=_menu_kb(lang))
        return

    buttons = []
    codes = list(LANG_LABELS.keys())
    for i in range(0, len(codes), 2):
        row = []
        for code in codes[i : i + 2]:
            label = LANG_LABELS.get(code, code)
            if code == lang:
                label = f"✅ {label}"
            row.append(InlineKeyboardButton(label, callback_data=f"lang:{code}"))
        buttons.append(row)
    kb = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(_t(lang, "lang_choose"), reply_markup=kb)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    s = STATE
    if s is None:
        await _reply_text_with_mention(update, _t(DEFAULT_LANG, "bot_init_failed"))
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG

    help_text = (
        _t(lang, "help_title") + "\n\n"
        + _t(lang, "help_start") + "\n"
        + _t(lang, "help_status") + "\n"
        + _t(lang, "help_lang") + "\n"
        + _t(lang, "help_subscribe") + "\n"
        + _t(lang, "help_unsubscribe") + "\n"
        + _t(lang, "help_hash") + "\n"
        + _t(lang, "hash_usage") + "\n"
        + _t(lang, "hash_usage_example") + "\n"
        + _t(lang, "help_price_pic") + "\n"
        + _t(lang, "help_price_pic2") + "\n"
        + _t(lang, "help_price_pic3") + "\n"
        + _t(lang, "help_info_query") + "\n"
        + _t(lang, "help_list_miss") + "\n"
        + _t(lang, "help_menu")
    )

    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        help_text = _html_escape(help_text)
    await _reply_text_with_mention(update, help_text, reply_markup=_menu_kb(lang))

async def price_pic_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    s = STATE
    if s is None:
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG

    text = (update.message.text or "").strip()
    cmd = text.split()[0]
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]

    m = re.match(r"^/price_pic_(\d+)(min|h|d)?$", cmd)
    if not m:
        await _reply_text_with_mention(update, _t(lang, "price_pic_usage"), reply_markup=_menu_kb(lang))
        return

    num = int(m.group(1))
    unit = m.group(2) or "min"
    candle_count = max(10, int(os.getenv("PRICE_PIC_CANDLE_COUNT", "60")))
    if unit == "min":
        if num < 1 or num > 60:
            await _reply_text_with_mention(update, _t(lang, "price_pic_usage"), reply_markup=_menu_kb(lang))
            return
        interval_secs = num * 60
    elif unit == "h":
        if num < 1 or num > 24:
            await _reply_text_with_mention(update, _t(lang, "price_pic_usage"), reply_markup=_menu_kb(lang))
            return
        interval_secs = num * 3600
    else:
        max_days = max(1, int(s.price_retention_days))
        if num < 1 or num > max_days:
            await _reply_text_with_mention(update, _t(lang, "price_pic_usage"), reply_markup=_menu_kb(lang))
            return
        interval_secs = num * 86400

    duration_secs = interval_secs * candle_count
    max_duration = max(1, int(s.price_retention_days)) * 86400
    if duration_secs > max_duration:
        duration_secs = max_duration

    try:
        candles, vols = await asyncio.to_thread(s.get_price_candles_with_vol, duration_secs, interval_secs)
        if not candles:
            await _reply_text_with_mention(update, _t(lang, "price_pic_no_data"), reply_markup=_menu_kb(lang))
            return
        png = await asyncio.to_thread(_draw_candles_png, candles, vols, interval_secs)
        if not png:
            await _reply_text_with_mention(update, _t(lang, "price_pic_no_data"), reply_markup=_menu_kb(lang))
            return

        await update.message.reply_photo(photo=png, reply_markup=_menu_kb(lang))
    except Exception as e:
        await _reply_text_with_mention(update, _t(lang, "price_pic_failed", err=e), reply_markup=_menu_kb(lang))

async def price_pic2_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    s = STATE
    if s is None:
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG

    text = (update.message.text or "").strip()
    cmd = text.split()[0]
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]

    m = re.match(r"^/price_pic2_(\d+)(min|h|d)?$", cmd)
    if not m:
        await _reply_text_with_mention(update, _t(lang, "price_pic_usage"), reply_markup=_menu_kb(lang))
        return

    num = int(m.group(1))
    unit = m.group(2) or "min"
    candle_count = max(10, int(os.getenv("PRICE_PIC_CANDLE_COUNT", "60")))
    if unit == "min":
        if num < 1 or num > 60:
            await _reply_text_with_mention(update, _t(lang, "price_pic_usage"), reply_markup=_menu_kb(lang))
            return
        interval_secs = num * 60
    elif unit == "h":
        if num < 1 or num > 24:
            await _reply_text_with_mention(update, _t(lang, "price_pic_usage"), reply_markup=_menu_kb(lang))
            return
        interval_secs = num * 3600
    else:
        max_days = max(1, int(s.price_retention_days))
        if num < 1 or num > max_days:
            await _reply_text_with_mention(update, _t(lang, "price_pic_usage"), reply_markup=_menu_kb(lang))
            return
        interval_secs = num * 86400

    duration_secs = interval_secs * candle_count
    max_duration = max(1, int(s.price_retention_days)) * 86400
    if duration_secs > max_duration:
        duration_secs = max_duration

    try:
        candles, vols = await asyncio.to_thread(s.get_price_candles_with_vol_dedup, duration_secs, interval_secs)
        if not candles:
            await _reply_text_with_mention(update, _t(lang, "price_pic_no_data"), reply_markup=_menu_kb(lang))
            return
        png = await asyncio.to_thread(_draw_candles_png, candles, vols, interval_secs)
        if not png:
            await _reply_text_with_mention(update, _t(lang, "price_pic_no_data"), reply_markup=_menu_kb(lang))
            return
        await update.message.reply_photo(photo=png, reply_markup=_menu_kb(lang))
    except Exception as e:
        await _reply_text_with_mention(update, _t(lang, "price_pic_failed", err=e), reply_markup=_menu_kb(lang))

async def price_pic3_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    s = STATE
    if s is None:
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG

    text = (update.message.text or "").strip()
    cmd = text.split()[0]
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]

    m = re.match(r"^/price_pic3(?:_(\d+))?$", cmd)
    if not m:
        await _reply_text_with_mention(update, _t(lang, "price_pic_usage"), reply_markup=_menu_kb(lang))
        return

    max_points = -1
    if m.group(1):
        try:
            max_points = max(1, int(m.group(1)))
        except Exception:
            await _reply_text_with_mention(update, _t(lang, "price_pic_usage"), reply_markup=_menu_kb(lang))
            return

    interval_secs = int(os.getenv("PRICE_PIC3_INTERVAL_SECS", "900"))

    try:
        candles, vols = await asyncio.to_thread(s.get_price_changes_with_vol, max_points, interval_secs)
        if not candles:
            await _reply_text_with_mention(update, _t(lang, "price_pic_no_data"), reply_markup=_menu_kb(lang))
            return
        png = await asyncio.to_thread(_draw_candles_png, candles, vols, interval_secs)
        if not png:
            await _reply_text_with_mention(update, _t(lang, "price_pic_no_data"), reply_markup=_menu_kb(lang))
            return
        await update.message.reply_photo(photo=png, reply_markup=_menu_kb(lang))
    except Exception as e:
        await _reply_text_with_mention(update, _t(lang, "price_pic_failed", err=e), reply_markup=_menu_kb(lang))

async def list_miss_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    s = STATE
    if s is None:
        await _reply_text_with_mention(update, _t(DEFAULT_LANG, "bot_init_failed"))
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG

    total_deposit_users, total_ref_users, scope_users, missing = s.get_missing_referral_users()

    lines: List[str] = []
    lines.append(_t(lang, "list_miss_scope"))
    lines.append(_t(lang, "list_miss_total_deposit_users", count=total_deposit_users))
    lines.append(_t(lang, "list_miss_total_ref_users", count=total_ref_users))
    lines.append(_t(lang, "list_miss_scope_users", count=scope_users))
    lines.append(_t(lang, "list_miss_missing_count", count=len(missing)))
    lines.append("")
    if not missing:
        lines.append(_t(lang, "list_miss_none"))
    else:
        lines.append(_t(lang, "list_miss_intro"))
        lines.extend(missing)

    msg = "\n".join(lines)
    await _reply_text_with_mention(update, msg, reply_markup=_menu_kb(lang))

def _hash_detail_text(lang: str, detail: str) -> str:
    if detail.startswith("hub_request_failed:"):
        return _t(lang, "hash_detail_hub_request_failed")
    key = f"hash_detail_{detail}"
    if key in TRANSLATIONS:
        return _t(lang, key)
    return detail

def _extract_first_tx_hash(text: str) -> Optional[str]:
    m = re.search(r"0x[a-fA-F0-9]{64}", text or "")
    if not m:
        return None
    return m.group(0)

async def hash_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message
    s = STATE
    if s is None:
        await _reply_text_with_mention(update, _t(DEFAULT_LANG, "bot_init_failed"))
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG

    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        if not await _is_group_admin(update, context):
            await _reply_text_with_mention(update, _t(lang, "hash_admin_only"), reply_markup=_menu_kb(lang))
            return

    tx_arg = context.args[0] if context.args else ""
    tx_from_text = _extract_first_tx_hash(update.message.text or "")
    txh = s._normalize_tx_hash(tx_arg or tx_from_text or "")
    if not txh:
        await _reply_text_with_mention(update, _t(lang, "hash_usage"), reply_markup=_menu_kb(lang))
        return

    ok, detail, transfer = await asyncio.to_thread(s.enqueue_hash_backfill, txh)
    detail_text = _hash_detail_text(lang, detail)
    if ok:
        extra = ""
        if transfer:
            user = str(transfer.get("from") or transfer.get("user") or "").strip()
            if user:
                extra += _t(lang, "hash_extra_user", user=user)
            try:
                amount_wei = int(transfer.get("amount") or 0)
            except Exception:
                amount_wei = 0
            if amount_wei > 0:
                extra += _t(
                    lang,
                    "hash_extra_amount",
                    amt=_fmt_amount(amount_wei, s.usdt_decimals, s.display_usdt_decimals),
                )
        msg = _t(lang, "hash_submit_ok", txh=txh, detail=detail_text, extra=extra)
    else:
        msg = _t(lang, "hash_submit_fail", txh=txh, detail=detail_text)
    await _reply_text_with_mention(update, msg, reply_markup=_menu_kb(lang))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q:
        return
    await q.answer()

    s = STATE
    if s is None:
        await _safe_edit_or_reply(q, _t(DEFAULT_LANG, "bot_init_failed"), reply_markup=_menu_kb(DEFAULT_LANG), update=update)
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)
    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG

    try:

        if q.data == "more_menu":
            try:
                await q.edit_message_reply_markup(reply_markup=_more_menu_kb(lang))
            except Exception:

                pass
            return

        if q.data == "back_to_main":
            try:
                await q.edit_message_reply_markup(reply_markup=_menu_kb(lang))
            except Exception:

                pass
            return

        if q.data and q.data.startswith("lang:"):
            code = q.data.split(":", 1)[1]

            if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
                if not await _is_group_admin(update, context):

                    error_text = _t(lang, "lang_admin_only")
                    formatted_text, mention_parse_mode = _format_reply_text(update, error_text)
                    if formatted_text and mention_parse_mode:
                        await q.message.reply_text(formatted_text, parse_mode=mention_parse_mode, reply_markup=_menu_kb(lang))
                    else:
                        await q.message.reply_text(error_text, reply_markup=_menu_kb(lang))
                    return
            s.set_lang(update.effective_chat.id if update.effective_chat else q.message.chat_id, code)
            lang = s.get_lang(update.effective_chat.id if update.effective_chat else q.message.chat_id)

            success_text = _t(lang, "lang_set_ok", lang_name=LANG_LABELS.get(lang, lang))
            formatted_text, mention_parse_mode = _format_reply_text(update, success_text)
            if formatted_text and mention_parse_mode:
                await q.message.reply_text(formatted_text, parse_mode=mention_parse_mode, reply_markup=_menu_kb(lang))
            else:
                await q.message.reply_text(success_text, reply_markup=_menu_kb(lang))
            return

        if q.data == "menu_addrs":
            text = s.get_contract_addresses_text(lang)

            formatted_text, mention_parse_mode = _format_reply_text(update, "")
            if formatted_text and mention_parse_mode:

                final_text = formatted_text + "\n" + text
                await q.message.reply_text(final_text, parse_mode="HTML", reply_markup=_menu_kb(lang))
            else:
                await q.message.reply_text(text, parse_mode="HTML", reply_markup=_menu_kb(lang))
            return
        if q.data == "chain_info":

            loading_msg = None
            try:
                if q.message:
                    loading_msg = await q.message.reply_text(_t(lang, "querying"), reply_markup=_menu_kb(lang))
            except Exception:
                loading_msg = None

            try:
                text = await asyncio.wait_for(
                    asyncio.to_thread(s.get_chain_info_text, lang),
                    timeout=15,
                )

                formatted_text, parse_mode = _format_reply_text(update, text)
                if loading_msg:
                    try:
                        if parse_mode:
                            await loading_msg.edit_text(formatted_text, parse_mode=parse_mode, reply_markup=_menu_kb(lang))
                        else:
                            await loading_msg.edit_text(formatted_text, reply_markup=_menu_kb(lang))
                    except Exception:

                        if q.message:
                            await _reply_text_with_mention(update, text, reply_markup=_menu_kb(lang))
                else:

                    if q.message:
                        await _reply_text_with_mention(update, text, reply_markup=_menu_kb(lang))
            except asyncio.TimeoutError:
                error_text = _t(lang, "query_timeout")
                formatted_text, parse_mode = _format_reply_text(update, error_text)
                if loading_msg:
                    try:
                        if parse_mode:
                            await loading_msg.edit_text(formatted_text, parse_mode=parse_mode, reply_markup=_menu_kb(lang))
                        else:
                            await loading_msg.edit_text(formatted_text, reply_markup=_menu_kb(lang))
                    except Exception:
                        if q.message:
                            await _reply_text_with_mention(update, error_text, reply_markup=_menu_kb(lang))
                else:
                    if q.message:
                        await _reply_text_with_mention(update, error_text, reply_markup=_menu_kb(lang))
            except Exception as e:
                error_text = _t(lang, "query_failed", err=e)
                formatted_text, parse_mode = _format_reply_text(update, error_text)
                if loading_msg:
                    try:
                        if parse_mode:
                            await loading_msg.edit_text(formatted_text, parse_mode=parse_mode, reply_markup=_menu_kb(lang))
                        else:
                            await loading_msg.edit_text(formatted_text, reply_markup=_menu_kb(lang))
                    except Exception:
                        if q.message:
                            await _reply_text_with_mention(update, error_text, reply_markup=_menu_kb(lang))
                else:
                    if q.message:
                        await _reply_text_with_mention(update, error_text, reply_markup=_menu_kb(lang))
            return
        elif q.data == "help_ops":

            text = s.get_ops_help_text(lang)
            formatted_text, mention_parse_mode = _format_reply_text(update, "")
            if formatted_text and mention_parse_mode:

                final_text = formatted_text + "\n" + text
                await q.message.reply_text(final_text, parse_mode="HTML", reply_markup=_menu_kb(lang))
            else:
                await q.message.reply_text(text, parse_mode="HTML", reply_markup=_menu_kb(lang))
            return
        elif q.data == "chain_roles":
            loading_msg = None
            try:
                if q.message:
                    loading_msg = await q.message.reply_text(_t(lang, "querying"), reply_markup=_menu_kb(lang))
            except Exception:
                loading_msg = None
            try:
                text = await asyncio.wait_for(
                    asyncio.to_thread(s.get_chain_roles_text, lang),
                    timeout=15,
                )
                formatted_text, parse_mode = _format_reply_text(update, text)
                if loading_msg:
                    try:
                        await loading_msg.edit_text(formatted_text, parse_mode="HTML", reply_markup=_menu_kb(lang))
                    except Exception:
                        if q.message:
                            await _reply_text_with_mention(update, text, parse_mode="HTML", reply_markup=_menu_kb(lang))
                else:
                    if q.message:
                        await _reply_text_with_mention(update, text, parse_mode="HTML", reply_markup=_menu_kb(lang))
            except asyncio.TimeoutError:
                error_text = _t(lang, "query_timeout")
                formatted_text, pm = _format_reply_text(update, error_text)
                if loading_msg:
                    try:
                        await loading_msg.edit_text(formatted_text, parse_mode=pm, reply_markup=_menu_kb(lang))
                    except Exception:
                        if q.message:
                            await _reply_text_with_mention(update, error_text, reply_markup=_menu_kb(lang))
                else:
                    if q.message:
                        await _reply_text_with_mention(update, error_text, reply_markup=_menu_kb(lang))
            except Exception as e:
                error_text = _t(lang, "query_failed", err=e)
                formatted_text, pm = _format_reply_text(update, error_text)
                if loading_msg:
                    try:
                        await loading_msg.edit_text(formatted_text, parse_mode=pm, reply_markup=_menu_kb(lang))
                    except Exception:
                        if q.message:
                            await _reply_text_with_mention(update, error_text, reply_markup=_menu_kb(lang))
                else:
                    if q.message:
                        await _reply_text_with_mention(update, error_text, reply_markup=_menu_kb(lang))
            return
        elif q.data == "cmd_list":

            help_text = (
                _t(lang, "help_title") + "\n\n"
                + _t(lang, "help_start") + "\n"
                + _t(lang, "help_status") + "\n"
                + _t(lang, "help_lang") + "\n"
                + _t(lang, "help_subscribe") + "\n"
                + _t(lang, "help_unsubscribe") + "\n"
                + _t(lang, "help_hash") + "\n"
                + _t(lang, "hash_usage") + "\n"
                + _t(lang, "hash_usage_example") + "\n"
                + _t(lang, "help_price_pic") + "\n"
                + _t(lang, "help_info_query") + "\n"
                + _t(lang, "help_menu")
            )

            if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
                help_text = _html_escape(help_text)
            formatted_text, mention_parse_mode = _format_reply_text(update, help_text)
            if formatted_text and mention_parse_mode:
                await q.message.reply_text(formatted_text, parse_mode=mention_parse_mode, reply_markup=_menu_kb(lang))
            else:
                await q.message.reply_text(help_text, reply_markup=_menu_kb(lang))
            return
        elif q.data == "info_query":
            try:
                bot_username = (context.bot.username or "").strip()
            except Exception:
                bot_username = ""
            if update.effective_chat and update.effective_chat.type == "private":
                mention = ""
            else:
                mention = ("@" + bot_username) if bot_username else "@机器人"

            user = q.from_user
            u_name = user.full_name if user else "你"
            u_id = user.id if user else None
            example = f"0x..." if not mention else f"{mention} 0x..."
            prompt = _t(lang, "prompt_need_format", example=_as_pre(example))
            if u_id is not None:
                prompt = f'<a href="tg://user?id={u_id}">{_html_escape(u_name)}</a>\n{prompt}'
            await q.message.reply_text(prompt, parse_mode="HTML", reply_markup=_menu_kb(lang))
            return
        else:
            text = _t(lang, "unknown_cmd")
    except Exception as e:
        text = _t(lang, "query_failed", err=e)

    await _safe_edit_or_reply(q, text, reply_markup=_menu_kb(lang), update=update)

def _extract_first_address(text: str) -> Optional[str]:
    import re

    m = re.search(r"0x[a-fA-F0-9]{40}", text)
    if not m:
        return None
    return m.group(0)

def _is_bot_mentioned(update: Update, bot_username: str) -> bool:
    chat = update.effective_chat
    if chat and getattr(chat, "type", "") == "private":
        return True
    if not bot_username:

        txt = (update.message.text or "") if update.message else ""
        return "@" in txt

    txt = (update.message.text or "") if update.message else ""
    if f"@{bot_username}".lower() in txt.lower():
        return True

    msg = update.message
    if not msg or not msg.entities:
        return False
    for ent in msg.entities:
        try:
            if ent.type == "mention":
                frag = txt[ent.offset : ent.offset + ent.length]
                if frag.lower() == f"@{bot_username}".lower():
                    return True
        except Exception:
            continue
    return False

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    s = STATE
    if s is None:
        return
    if update.effective_chat:
        s.register_chat(update.effective_chat)

    bot_username = (context.bot.username or "").strip()
    if update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        if not _is_bot_mentioned(update, bot_username):
            return

    raw = (update.message.text or "").strip()
    addr0 = _extract_first_address(raw)
    if not addr0:
        return

    lang = s.get_lang(update.effective_chat.id) if update.effective_chat else DEFAULT_LANG

    try:
        addr = Web3.to_checksum_address(addr0)
    except Exception:
        await update.message.reply_text(_t(lang, "invalid_address"), reply_markup=_menu_kb(lang))
        return

    try:
        msg = s.get_address_info_text(addr, lang)
    except Exception as e:
        await update.message.reply_text(_t(lang, "query_failed", err=e), reply_markup=_menu_kb(lang))
        return
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=_menu_kb(lang))

async def _poll_broadcast_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    s = STATE
    if s is None:
        return
    try:
        events = await asyncio.to_thread(s._scan_broadcast_events_sync)
        if not events:
            return
        subs = s.get_broadcast_chat_ids()
        for chat_id in subs:
            lang = s.get_lang(chat_id)
            for ev in events:
                m = s.format_event(lang, ev)
                if not m:
                    continue
                try:
                    await context.bot.send_message(chat_id=chat_id, text=m, parse_mode="Markdown", reply_markup=_menu_kb(lang))
                except Exception as e:
                    logger.debug("send_message failed chat_id=%s: %s", chat_id, e)
    except Exception as e:
        logger.warning("broadcast poll failed: %s", e)

async def _post_init(app: Application) -> None:
    s = STATE
    if s is None or not s.broadcast_any_enabled:
        return
    subs = s.get_broadcast_chat_ids()
    if not subs:
        return
    for chat_id in subs:
        try:
            lang = s.get_lang(chat_id)
            msg = _t(lang, "broadcast_started")
            await app.bot.send_message(chat_id=chat_id, text=msg, reply_markup=_menu_kb(lang))
        except Exception as e:
            logger.debug("post_init send_message failed chat_id=%s: %s", chat_id, e)

async def _on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = STATE
    if s is None:
        return
    cmu: ChatMemberUpdated = update.my_chat_member
    if cmu is None:
        return
    chat = cmu.chat
    new_status = cmu.new_chat_member.status
    if new_status in ("member", "administrator"):
        s.register_chat(chat)
    elif new_status in ("left", "kicked"):
        s.unregister_chat(chat.id)

def main() -> None:
    global STATE
    STATE = BotState()

    app = Application.builder().token(STATE.telegram_token).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(CommandHandler("subscribe", subscribe_cmd))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))
    app.add_handler(CommandHandler("hash", hash_cmd))
    app.add_handler(CommandHandler("list_miss", list_miss_cmd))
    app.add_handler(CommandHandler("nodes", nodes_cmd))
    app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r"^/price_pic3"), price_pic3_cmd))
    app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r"^/price_pic2_"), price_pic2_cmd))
    app.add_handler(MessageHandler(filters.COMMAND & filters.Regex(r"^/price_pic_"), price_pic_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(ChatMemberHandler(_on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    if STATE.broadcast_any_enabled:
        if app.job_queue is None:
            logger.warning(
                "Broadcast is enabled but PTB JobQueue is unavailable. "
                "Install optional dependency: python-telegram-bot[job-queue]. "
                "Continuing without periodic broadcast worker."
            )
        else:
            app.job_queue.run_repeating(_poll_broadcast_job, interval=max(3, STATE.poll_interval), first=3)
            logger.info(
                "Broadcast enabled. poll_interval=%ss confirmations=%s subs=%s state_file=%s",
                STATE.poll_interval,
                STATE.broadcast_confirmations,
                len(STATE._persist.subscribers),
                STATE.state_file,
            )

    logger.info("Telegram bot started.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
