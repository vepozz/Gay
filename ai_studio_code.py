#!/usr/bin/env python3
"""
BCR Telegram Bot v3.5 PROMAX REAL-TIME — NO-KEY GEMINI INTEGRATED (SHERLOCK)
- HARDCODED BOT TOKEN TRỰC TIẾP: 7746793519:AAF35UiQOaxRA7yMxTnJ3bwn2-lhM8Ze6lg (KHÔNG DÙNG ENV)
- TÍCH HỢP TRỰC TIẾP GEMINI NO-KEY (CURL_CFFI CHROME131 REVERSE-ENGINEERED)
- KHÔNG CẦN GEMINI API KEY
- TỰ ĐỘNG CHECK KẾT QUẢ MỖI 1.5S & DỰ ĐOÁN TAY MỚI MỖI 3S
- CÓ DÒNG "🧠 Y/k của AI:" TRÊN MỌI THÔNG BÁO DỰ ĐOÁN
"""

import asyncio
import json
import math
import os
import re
import sys
import uuid
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import List, Dict, Tuple, Optional, Any

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

import requests
from flask import Flask
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ======================== CONFIG (HARDCODED TRỰC TIẾP KHÔNG DÙNG ENV) ========================
BOT_TOKEN = "7746793519:AAF35UiQOaxRA7yMxTnJ3bwn2-lhM8Ze6lg"
API_BCR = "https://construct-vacuum-bosnia-travel.trycloudflare.com/api/bcr"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bcr_bot_data.json")
PORT = 10000
MAX_PATTERNS = 10000
MAX_HISTORY = 500
WINRATE_WINDOW = 30
HISTORY_SHOW = 20

# Cấu hình chu kỳ tự động
REALTIME_POLL_INTERVAL = 1.5
AUTO_SCAN_INTERVAL = 60
AUTO_SCAN_MIN_PCT = 70

_table_last_results: Dict[str, str] = {}
_pending_predictions: Dict[str, dict] = {}
_background_tasks = set()
_last_alerted_keys = {}


# ======================== PERSISTENT BOT DATA (THREAD-SAFE RLOCK) ========================
class BotData:
    def __init__(self, filename: str = DATA_FILE):
        self.filename = filename
        self.lock = threading.RLock()
        self.patterns: Dict[str, dict] = {}
        self.predictions: List[dict] = []
        self.auto_scan_chats = set()
        self.auto_live_subs: Dict[int, List[str]] = {}
        self.load()

    def load(self):
        with self.lock:
            if not os.path.exists(self.filename):
                return
            try:
                with open(self.filename, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.patterns = data.get("patterns", {})
                    self.predictions = data.get("predictions", [])
                    self.auto_scan_chats = set(data.get("auto_scan_chats", []))
                    raw_subs = data.get("auto_live_subs", {})
                    self.auto_live_subs = {int(k): v for k, v in raw_subs.items()}
                print(f"[Data] Loaded {len(self.patterns)} patterns, {len(self.predictions)} preds, "
                      f"{len(self.auto_scan_chats)} scan_chats, {len(self.auto_live_subs)} live_subs")
            except Exception as e:
                print(f"[Data] Load error: {e}")

    def save(self):
        with self.lock:
            try:
                data = {
                    "patterns": self.patterns,
                    "predictions": self.predictions[-MAX_HISTORY:],
                    "auto_scan_chats": list(self.auto_scan_chats),
                    "auto_live_subs": {str(k): v for k, v in self.auto_live_subs.items()},
                }
                tmp_file = f"{self.filename}.tmp"
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_file, self.filename)
            except Exception as e:
                print(f"[Data] Save error: {e}")

    def add_live_sub(self, chat_id: int, ban: str):
        with self.lock:
            subs = self.auto_live_subs.get(chat_id, [])
            if ban not in subs:
                subs.append(ban)
                self.auto_live_subs[chat_id] = subs
                self.save()

    def remove_live_sub(self, chat_id: int, ban: Optional[str] = None):
        with self.lock:
            if chat_id not in self.auto_live_subs:
                return
            if ban is None or ban.upper() == "ALL":
                del self.auto_live_subs[chat_id]
            else:
                subs = [b for b in self.auto_live_subs[chat_id] if b != ban]
                if subs:
                    self.auto_live_subs[chat_id] = subs
                else:
                    del self.auto_live_subs[chat_id]
            self.save()

    def record_prediction(self, ban: str, pred: str, actual: Optional[str], pct: int, pattern: str, mode: str = "auto"):
        with self.lock:
            self.predictions.append({
                "ban": ban,
                "pred": pred,
                "actual": actual,
                "correct": (pred == actual) if actual is not None else None,
                "pct": pct,
                "pattern": pattern,
                "mode": mode,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S")
            })
            if len(self.predictions) > MAX_HISTORY:
                self.predictions = self.predictions[-MAX_HISTORY:]
            self.save()

    def add_pattern(self, pattern: str, next_val: str, correct: bool):
        if not pattern or len(pattern) < 6:
            return
        with self.lock:
            if pattern not in self.patterns:
                self.patterns[pattern] = {"P": 0, "B": 0, "win": 0, "loss": 0}
            if next_val in ("P", "B"):
                self.patterns[pattern][next_val] += 1
            if correct:
                self.patterns[pattern]["win"] += 1
            else:
                self.patterns[pattern]["loss"] += 1
            self.save()

    def get_pattern_boost(self, pattern: str) -> Tuple[float, float, float]:
        with self.lock:
            data = self.patterns.get(pattern)
            if not data:
                return 0.0, 0.0, 0.5
            p_cnt = data.get("P", 0)
            b_cnt = data.get("B", 0)
            tot = p_cnt + b_cnt
            if tot < 2:
                return 0.0, 0.0, 0.5
            p_ratio = p_cnt / tot
            conf = abs(p_ratio - 0.5) * 2
            boost_p = (p_ratio - 0.5) * 3.0 if p_ratio > 0.55 else 0.0
            boost_b = (0.5 - p_ratio) * 3.0 if p_ratio < 0.45 else 0.0
            return max(0.0, boost_p), max(0.0, boost_b), conf

    def get_winrate(self, window: int = WINRATE_WINDOW) -> Tuple[int, int, float]:
        with self.lock:
            checked = [p for p in self.predictions if p.get("correct") is not None]
            recent = checked[-window:] if len(checked) > window else checked
            if not recent:
                return 0, 0, 0.0
            correct = sum(1 for p in recent if p["correct"])
            total = len(recent)
            return correct, total, (correct / total) * 100.0

    def get_history(self, limit: int = HISTORY_SHOW) -> List[dict]:
        with self.lock:
            return list(self.predictions[-limit:])


db = BotData()

_session = requests.Session()
_session.headers.update({"Cache-Control": "no-cache", "User-Agent": "BCRBot/3.5"})


# ======================== SAFE SEND & REPLY ========================
async def safe_reply(update: Update, text: str, parse_mode="Markdown"):
    try:
        if update.message:
            await update.message.reply_text(text, parse_mode=parse_mode)
    except Exception:
        try:
            if update.message:
                await update.message.reply_text(text, parse_mode=None)
        except Exception as e:
            print(f"[SafeReply] error: {e}")


async def safe_send(bot, chat_id: int, text: str, parse_mode="Markdown"):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    except Exception:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=None)
        except Exception as e:
            print(f"[SafeSend] error: {e}")


def normalize_ban_name(ban_input: str) -> str:
    s = ban_input.strip().upper()
    if s.isdigit():
        return f"C{int(s):02d}"
    m = re.match(r"^C(\d+)$", s)
    if m:
        return f"C{int(m.group(1)):02d}"
    return s


# ======================== LIVE API FETCHER ========================
_api_cache = {"data": None, "ts": 0.0}
_api_cache_lock = threading.Lock()


def fetch_tables():
    global _api_cache
    now = time.time()
    with _api_cache_lock:
        if _api_cache["data"] is not None and (now - _api_cache["ts"]) < 1.2:
            return _api_cache["data"]

    try:
        res = _session.get(API_BCR, timeout=(4.0, 7.0))
        if res.status_code == 200:
            data = res.json()
            if data.get("code") == 0:
                tables = data.get("data", [])
                with _api_cache_lock:
                    _api_cache["data"] = tables
                    _api_cache["ts"] = now
                return tables
    except Exception as e:
        print(f"[API Fetch] error: {e}")

    with _api_cache_lock:
        return _api_cache["data"] or []


def get_table(ban: str):
    tables = fetch_tables()
    norm = normalize_ban_name(ban)
    for t in tables:
        curr = str(t.get("ban", "")).strip().upper()
        if curr == norm or normalize_ban_name(curr) == norm:
            return t
    return None


# ======================== PROMAX PREDICTION ENGINE ========================
def build_big_road(clean_seq: str) -> List[List[str]]:
    cols = []
    curr_col = []
    curr_side = None
    for c in clean_seq:
        if c == curr_side:
            curr_col.append(c)
        else:
            if curr_col:
                cols.append(curr_col)
            curr_col = [c]
            curr_side = c
    if curr_col:
        cols.append(curr_col)
    return cols


def analyze_derived_roads(clean_seq: str) -> Tuple[float, float, str]:
    if len(clean_seq) < 14:
        return 0.0, 0.0, ""
    cols = build_big_road(clean_seq)
    if len(cols) < 3:
        return 0.0, 0.0, ""

    scoreP = 0.0
    scoreB = 0.0
    tags = []
    last_col = cols[-1]
    last_side = last_col[0]

    if len(cols) >= 3:
        prev1 = len(cols[-2])
        prev2 = len(cols[-3])
        if prev1 == prev2:
            tags.append("ĐạiNhãn(Đỏ)")
            if last_side == "P":
                scoreP += 1.4
            else:
                scoreB += 1.4
        else:
            tags.append("ĐạiNhãn(Xanh-Lệch)")
            if last_side == "P":
                scoreB += 1.2
            else:
                scoreP += 1.2

    if len(cols) >= 4:
        p1 = len(cols[-2])
        p3 = len(cols[-4])
        if p1 == p3:
            tags.append("TiểuLộ(Đỏ)")
            if last_side == "P":
                scoreP += 1.2
            else:
                scoreB += 1.2

    if len(cols) >= 5:
        p1 = len(cols[-2])
        p4 = len(cols[-5])
        if p1 == p4:
            tags.append("PhổBàn(Đỏ)")
            if last_side == "P":
                scoreP += 1.0
            else:
                scoreB += 1.0

    tag_str = "Tam Lộ: " + " • ".join(tags) if tags else ""
    return scoreP, scoreB, tag_str


def predict_next(results: str):
    clean = [c for c in results if c in ("P", "B")]
    if len(clean) < 8:
        return None, 50, "Chưa đủ dữ liệu (tối thiểu 8 ván)", "", "Quan sát"

    seq = clean
    n = len(seq)
    last = seq[-1]
    pattern8 = "".join(seq[-8:])

    streak = 0
    for c in reversed(seq):
        if c == last:
            streak += 1
        else:
            break

    pingpong_len = 0
    for i in range(n - 1, 0, -1):
        if seq[i] != seq[i - 1]:
            pingpong_len += 1
        else:
            break
    is_pingpong = (pingpong_len >= 3)

    scoreP = 0.0
    scoreB = 0.0
    reasons = []

    # 1. Bệt & Pingpong
    if streak >= 2:
        mult = 2.0 + min(streak * 0.7, 4.5)
        if last == "P":
            scoreP += mult
            reasons.append(f"Cầu Bệt Con ({streak} tay)")
        else:
            scoreB += mult
            reasons.append(f"Cầu Bệt Cái ({streak} tay)")

    if is_pingpong:
        next_pingpong = "B" if last == "P" else "P"
        mult = 2.2 + min(pingpong_len * 0.6, 3.5)
        if next_pingpong == "P":
            scoreP += mult
        else:
            scoreB += mult
        reasons.append(f"Cầu 1-1 ({pingpong_len + 1} nhịp)")

    # Cầu 2-2
    if n >= 6:
        if seq[-1] == seq[-2] and seq[-3] == seq[-4] and seq[-1] != seq[-3]:
            predict_22 = seq[-1]
            if predict_22 == "P":
                scoreP += 2.2
            else:
                scoreB += 2.2
            reasons.append("Nhịp cầu đôi 2-2")

    # 2. N-Gram Pattern Weighting
    for gram_len, weight in [(5, 4.0), (4, 3.0), (3, 2.0), (2, 1.2)]:
        if n >= gram_len + 3:
            sub = "".join(seq[-gram_len:])
            p_next = 0
            b_next = 0
            full_str = "".join(seq)
            for i in range(n - gram_len):
                if full_str[i:i + gram_len] == sub:
                    nxt = full_str[i + gram_len]
                    if nxt == "P":
                        p_next += 1
                    elif nxt == "B":
                        b_next += 1
            tot = p_next + b_next
            if tot >= 2:
                p_ratio = p_next / tot
                b_ratio = b_next / tot
                if p_ratio >= 0.65:
                    scoreP += weight * p_ratio
                    reasons.append(f"{gram_len}g→P({int(p_ratio * 100)}%)")
                elif b_ratio >= 0.65:
                    scoreB += weight * b_ratio
                    reasons.append(f"{gram_len}g→B({int(b_ratio * 100)}%)")

    # 3. Markov Chain
    if len(seq) >= 12:
        last2 = "".join(seq[-2:])
        m_counts = {"P": 0, "B": 0}
        for i in range(len(seq) - 2):
            if "".join(seq[i:i + 2]) == last2:
                nxt = seq[i + 2]
                m_counts[nxt] += 1
        m_total = m_counts["P"] + m_counts["B"]
        if m_total >= 3:
            diff = (m_counts["P"] - m_counts["B"]) / m_total
            if abs(diff) > 0.3:
                m_side = "P" if diff > 0 else "B"
                if m_side == "P":
                    scoreP += 1.8
                else:
                    scoreB += 1.8
                reasons.append(f"Markov({m_side} {max(m_counts['P'], m_counts['B'])}/{m_total})")

    # 4. Tam Lộ
    tl_P, tl_B, tl_tag = analyze_derived_roads(seq)
    scoreP += tl_P
    scoreB += tl_B
    if tl_P > 0 or tl_B > 0:
        reasons.append(tl_tag)

    # 5. Cầu nghiêng
    recent = seq[-12:]
    rP = rB = 0.0
    for i, v in enumerate(recent):
        w = 0.6 + (i / max(len(recent), 1)) * 1.5
        if v == "P":
            rP += w
        else:
            rB += w
    if rP > rB + 2.0:
        scoreP += 1.8
        reasons.append("Cầu nghiêng Con")
    elif rB > rP + 2.0:
        scoreB += 1.8
        reasons.append("Cầu nghiêng Cái")

    # 6. AI Pattern Memory
    bp, bb, conf = db.get_pattern_boost(pattern8)
    if bp > 0 or bb > 0:
        scoreP += bp
        scoreB += bb
        reasons.append(f"AI-Học({conf:.0%})")

    totP = seq.count("P")
    totB = seq.count("B")
    if abs(totP - totB) / len(seq) > 0.15:
        if totP > totB:
            scoreP += 0.8
        else:
            scoreB += 0.8

    diff = scoreP - scoreB
    pPct = int(round(1.0 / (1.0 + math.exp(-diff * 0.42)) * 100))
    pPct = max(20, min(85, pPct))
    bPct = 100 - pPct

    if pPct > bPct + 2:
        pred = "P"
        pct = pPct
    elif bPct > pPct + 2:
        pred = "B"
        pct = bPct
    else:
        pred = last
        pct = max(pPct, bPct)

    if pct >= 72 or streak >= 4:
        bet_advice = "🔥 Cược Mạnh (2 Units) - Cầu đang rất nét"
    elif pct >= 60:
        bet_advice = "🎯 Cược Tiêu Chuẩn (1 Unit) - Nhịp chuẩn"
    else:
        bet_advice = "⚠️ Nhẹ tay / Quan sát (0.5 Unit) - Cầu đang giằng co"

    detail = " • ".join(reasons[:5]) if reasons else "Đa nhân tố Promax"
    return pred, pct, detail, pattern8, bet_advice


# ======================== GEMINI NO-KEY ENGINE (SHERLOCK) ========================
BASE = "https://gemini.google.com"
PATH = "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate"
Dev = None
BL = None
FSID = None
_gemini_init_lock = threading.Lock()


def init_gemini_nokey():
    """Khởi tạo session Gemini không cần API Key sử dụng curl_cffi (impersonate chrome131)"""
    global Dev, BL, FSID
    if Dev is not None and BL and FSID:
        return True
    with _gemini_init_lock:
        if Dev is not None and BL and FSID:
            return True
        if cffi_requests is None:
            return False
        try:
            Dev = cffi_requests.Session(impersonate="chrome131")
            html = Dev.get(BASE + "/", timeout=6).text
            m_bl = re.search(r'"cfb2h":"([^"]+)"', html)
            m_fs = re.search(r'"FdrFJe":"(-?\d+)"', html)
            if m_bl and m_fs:
                BL = m_bl.group(1)
                FSID = m_fs.group(1)
                print(f"[Gemini No-Key] Kết nối thành công! BL={BL[:6]}... FSID={FSID[:6]}...")
                return True
        except Exception as e:
            print(f"[Gemini No-Key] Khởi tạo session thất bại: {e}")
            Dev = None
    return False


def dig(cands):
    out, stack = "", [cands]
    while stack:
        n = stack.pop()
        if isinstance(n, list):
            if (len(n) > 1 and isinstance(n[0], str) and n[0].startswith("rc_")
                    and isinstance(n[1], list) and n[1] and isinstance(n[1][0], str)
                    and len(n[1][0]) >= len(out)):
                out = n[1][0]
            stack.extend(n)
    return out


def Sherlock(msg: str, cid=None, rid=None, lang="vi"):
    """Truy vấn trực tiếp Gemini Google không cần API Key"""
    if not init_gemini_nokey():
        return "", None, None
    try:
        fresh = ["", "", "", None, None, None, None, None, None, ""]
        p = [None] * 99
        p[0] = [msg, 0, None, None, None, None, 0]
        p[1] = [lang]
        p[2] = [cid, rid, "", None, None, None, None, None, None, ""] if cid else fresh
        p[6], p[7], p[10], p[11] = [1], 1, 1, 0
        p[17], p[18] = [[0]], 0
        p[27], p[30] = 1, [4]
        p[41], p[53] = [2], 0
        p[59], p[61] = str(uuid.uuid4()).upper(), []
        p[68], p[79] = 2, 6
        p[91], p[96], p[98] = 0, 0, 1
        url = f"{BASE}{PATH}?bl={BL}&f.sid={FSID}&hl={lang}&_reqid=100000&rt=c"
        res = Dev.post(url, data={"f.req": json.dumps([None, json.dumps(p)])}, stream=True, timeout=5)
        text, ids = "", None
        for line in res.iter_lines():
            if not line:
                continue
            line = line.decode() if isinstance(line, bytes) else line
            if line.startswith(")]}'") or line.isdigit():
                continue
            try:
                arr = json.loads(line)
            except Exception:
                continue
            for row in arr:
                if not (isinstance(row, list) and len(row) > 2 and row[0] == "wrb.fr"):
                    continue
                try:
                    d = json.loads(row[2])
                except Exception:
                    continue
                if (isinstance(d, list) and len(d) > 1 and isinstance(d[1], list)
                        and d[1] and str(d[1][0]).startswith("c_")):
                    ids = d[1]
                if len(d) > 4 and isinstance(d[4], list):
                    t = dig(d[4])
                    if len(t) > len(text):
                        text = t
        return text, (ids[0] if ids else None), (ids[1] if ids else None)
    except Exception as e:
        print(f"[Gemini No-Key] Query error: {e}")
        return "", None, None


def generate_ai_opinion_sync(ban: str, results: str, pred: str, pct: int, detail: str, good_road: str) -> str:
    """Thuật toán AI Promax sinh nhận định chiến thuật thực chiến cực bén và tự nhiên 100%"""
    side_vn = "PLAYER (Con)" if pred == "P" else "BANKER (Cái)"
    side_short = "Con" if pred == "P" else "Cái"
    other_short = "Cái" if pred == "P" else "Con"
    clean = [c for c in results if c in ("P", "B")]

    streak = 0
    for c in reversed(clean):
        if c == pred:
            streak += 1
        else:
            break

    last_hand = clean[-1] if clean else ""
    is_streak = streak >= 2 and last_hand == pred

    points = []
    # 1. Nhịp cầu bệt / đu rồng
    if is_streak:
        if streak >= 4:
            points.append(f"Dòng tiền bám cầu bệt {side_short} đang cực mạnh ({streak} tay liên tiếp). Khuyến nghị tiếp tục đu cầu theo xu hướng, tuyệt đối không bẻ.")
        else:
            points.append(f"Chân cầu bệt {side_short} đang hình thành rõ nét ({streak} tay). Tỷ lệ tiếp diễn vượt trội so với xác suất đảo cửa.")
    # 2. Cầu 1-1 / Pingpong
    elif "1-1" in detail or "PingPong" in detail:
        points.append(f"Nhịp đối xứng 1-1 đang chạy chuẩn khuôn mẫu. Sau tay {other_short}, điểm rơi xác suất chuyển dịch dứt khoát về {side_short}.")
    # 3. Cầu đôi 2-2
    elif "2-2" in detail:
        points.append(f"Thế cầu chạy theo khuôn mẫu song song 2-2. Điểm cân bằng chỉ định tiếp tục vào {side_short} để bảo toàn nhịp cầu.")

    # 4. Tam Lộ
    if "Tam Lộ" in detail or "ĐạiNhãn" in detail or "TiểuLộ" in detail:
        points.append(f"Chỉ số Tam Lộ (Đại Nhãn / Tiểu Lộ) đang đồng thuận hướng về {side_short}, áp lực dòng tiền dồn mạnh.")

    # 5. N-Gram & Markov
    if "100%" in detail:
        points.append(f"Tổ hợp lịch sử N-gram lặp lại với xác suất 100% xuất hiện {side_short}. Đây là tay có cơ sở dữ liệu mạnh nhất phiên.")
    elif "Markov" in detail:
        points.append(f"Mô hình chuỗi Markov chỉ ra biên độ lệch kỳ vọng nghiêng hẳn về {side_short}.")
    elif "Cầu nghiêng" in detail:
        points.append(f"Mật độ thống kê bàn {ban} đang có xu hướng lệch về {side_short}, biên độ an toàn cao.")

    # 6. Lời khuyên vào lệnh & quản lý vốn
    if pct >= 80:
        advice = f"Xác suất đạt đỉnh ({pct}%), tự tin vào lệnh mạnh theo đúng khuyến nghị."
    elif pct >= 70:
        advice = f"Độ tin cậy cao ({pct}%), ưu tiên vào lệnh dứt khoát và giữ vững kỷ luật vốn."
    else:
        advice = f"Độ tin cậy {pct}%, giữ tâm lý vững, vào lệnh vừa phải theo đúng nhịp."

    if points:
        main_idea = points[0]
        if len(points) > 1 and len(main_idea) < 70:
            main_idea += f" Đồng thời {points[1].lower()}"
        return f"{main_idea} {advice}"
    else:
        return f"Nhịp cầu bàn {ban} đang ủng hộ cửa {side_vn} với độ tin cậy {pct}%. Vào lệnh theo đúng quản lý vốn."


async def generate_ai_analysis_safe(ban: str, results: str, pred: str, pct: int, detail: str, good_road: str) -> str:
    """Sinh phân tích AI: Gọi trực tiếp Gemini No-Key (Sherlock), nếu bận hoặc chờ lâu thì tự động mix AI Promax tức thì"""
    side_text = "PLAYER (Con)" if pred == "P" else "BANKER (Cái)"
    try:
        prompt = (
            f"Bạn là chuyên gia Baccarat AI. Bàn {ban}, 20 ván gần: {results[-20:]}. "
            f"Dự đoán {side_text} ({pct}%), thế cầu: {detail}. "
            f"Hãy đưa ra đúng 1 câu nhận định chiến thuật ngắn gọn (tối đa 25 từ tiếng Việt) giải thích dứt khoát lý do vào cửa này."
        )
        res_tuple = await asyncio.wait_for(
            asyncio.to_thread(Sherlock, prompt, None, None, "vi"),
            timeout=2.0
        )
        if res_tuple and res_tuple[0]:
            ans = res_tuple[0].strip().replace("\n", " ")
            if len(ans) >= 8:
                if len(ans) > 180:
                    ans = ans[:177] + "..."
                return ans
    except Exception:
        pass

    return generate_ai_opinion_sync(ban, results, pred, pct, detail, good_road)


# ======================== MULTI-TABLE SCANNER ENGINE ========================
def scan_all_tables_sync():
    """Quét toàn bộ các bàn live, tính toán dự đoán Promax cho từng bàn"""
    tables = fetch_tables()
    if not tables:
        return []

    results_list = []
    for t in tables:
        ban = str(t.get("ban", "")).strip().upper()
        res_str = t.get("results", "")
        good_road = t.get("good_road", "") or "—"
        pb_count = len([c for c in res_str if c in ("P", "B")])

        if pb_count < 8:
            continue

        pred_res = predict_next(res_str)
        if not pred_res or pred_res[0] is None:
            continue

        pred, pct, detail, pattern8, bet_advice = pred_res
        results_list.append({
            "ban": ban,
            "pred": pred,
            "pct": pct,
            "detail": detail,
            "pattern8": pattern8,
            "bet_advice": bet_advice,
            "good_road": good_road,
            "total_hands": len(res_str),
            "pb_count": pb_count,
            "results": res_str,
        })

    results_list.sort(key=lambda x: x["pct"], reverse=True)
    return results_list


# ======================== REALTIME AUTO-CHECK & PREDICT ENGINE ========================
async def realtime_auto_monitor_task(app: Application):
    """
    TỰ ĐỘNG THỜI GIAN THỰC (REAL-TIME 100%):
    - Quét API liên tục mỗi 1.5s
    - Khi bàn ra ván mới:
      1. Tự động đối soát Thắng / Thua, nạp AI pattern memory (không cần check tay)
      2. Gửi thông báo kết quả ván vừa xong tới chat
      3. Đợi 2.5s (sau 3s): Tự động tính toán & gửi dự đoán ván tiếp theo kèm Ý kiến của AI!
    """
    print("[RealTime Engine] ⚡ Auto-Check (1.5s) & Auto-Predict (3s) Task Started!")
    while True:
        try:
            await asyncio.sleep(REALTIME_POLL_INTERVAL)

            with db.lock:
                if not db.auto_live_subs:
                    continue
                active_subs = {cid: set(bans) for cid, bans in db.auto_live_subs.items()}

            monitored_tables = set()
            for bans in active_subs.values():
                monitored_tables.update(bans)

            if not monitored_tables:
                continue

            tables = await asyncio.to_thread(fetch_tables)
            if not tables:
                continue

            for t in tables:
                ban_raw = str(t.get("ban", "")).strip().upper()
                norm_ban = normalize_ban_name(ban_raw)
                curr_results = t.get("results", "")
                good_road = t.get("good_road", "") or "—"

                is_sub = (norm_ban in monitored_tables) or (ban_raw in monitored_tables) or ("ALL" in monitored_tables)
                if not is_sub:
                    continue

                target_chats = [
                    cid for cid, bans in active_subs.items()
                    if norm_ban in bans or ban_raw in bans or "ALL" in bans
                ]
                if not target_chats:
                    continue

                prev_results = _table_last_results.get(norm_ban)
                if prev_results is None:
                    _table_last_results[norm_ban] = curr_results
                    continue

                if len(curr_results) > len(prev_results):
                    new_hands = curr_results[len(prev_results):]
                    _table_last_results[norm_ban] = curr_results

                    # 1. BƯỚC 1: TỰ ĐỘNG CHECK KẾT QUẢ VÁN VỪA XONG (AUTO CHECK 100%)
                    for actual in new_hands:
                        if actual not in ("P", "B", "T"):
                            continue

                        last_pred = _pending_predictions.get(norm_ban)
                        if last_pred:
                            pred = last_pred["pred"]
                            pattern8 = last_pred.get("pattern8", "")

                            if actual == "T":
                                st_text = "🤝 HÒA (Tie - Hoàn tiền / Giữ nguyên vốn)"
                                is_win = None
                            elif actual == pred:
                                st_text = "🎉 ✅ THẮNG (WIN!)"
                                is_win = True
                            else:
                                st_text = "⚠️ ❌ THUA (LOSS)"
                                is_win = False

                            if is_win is not None:
                                db.record_prediction(norm_ban, pred, actual, last_pred.get("pct", 50), pattern8, "auto_real")
                                if pattern8:
                                    db.add_pattern(pattern8, actual, is_win)

                            c_win, c_tot, c_rate = db.get_winrate(WINRATE_WINDOW)
                            wr_str = f"\n📊 Tỉ lệ thắng gần nhất: *{c_rate:.1f}%* ({c_win}/{c_tot} phiên)" if c_tot > 0 else ""

                            actual_side = "PLAYER (P - Con)" if actual == "P" else ("BANKER (B - Cái)" if actual == "B" else "TIE (Hòa)")
                            actual_emoji = "🔵" if actual == "P" else ("🔴" if actual == "B" else "🟢")

                            result_msg = (
                                f"🔔 *BÀN `{norm_ban}` — KẾT QUẢ VÁN #{len(curr_results)}*\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🏁 Kết quả ra: {actual_emoji} *{actual_side}*\n"
                                f"🎯 Dự đoán trước đó: *{pred}* ({last_pred.get('pct', 50)}%)\n"
                                f"👉 Trạng thái: *{st_text}*{wr_str}\n"
                                f"📝 _AI Promax đã tự động nạp kết quả vào bộ nhớ!_\n"
                                f"⏳ _Đang tự động phân tích ván tiếp theo sau 3 giây..._"
                            )

                            for cid in target_chats:
                                await safe_send(app.bot, cid, result_msg)

                    # 2. BƯỚC 2: TỰ ĐỘNG GỬI DỰ ĐOÁN VÁN TIẾP THEO (SAU 2.5 - 3 GIÂY)
                    await asyncio.sleep(2.5)

                    pred_res = predict_next(curr_results)
                    if pred_res and pred_res[0] is not None:
                        nxt_pred, nxt_pct, nxt_detail, nxt_pattern8, nxt_bet_advice = pred_res
                        _pending_predictions[norm_ban] = {
                            "pred": nxt_pred,
                            "pct": nxt_pct,
                            "detail": nxt_detail,
                            "pattern8": nxt_pattern8,
                            "bet_advice": nxt_bet_advice,
                            "ts": time.time(),
                        }

                        side_text = "PLAYER (P - Con)" if nxt_pred == "P" else "BANKER (B - Cái)"
                        emoji = "🔵" if nxt_pred == "P" else "🔴"
                        road_str = f"🛣 Thế cầu: `{good_road}`\n" if good_road != "—" else ""

                        # TỰ ĐỘNG CHẠY AI Ý KIẾN TRÊN MỖI LẦN DỰ ĐOÁN (MIX AI 100%)
                        ai_opinion = await generate_ai_analysis_safe(
                            norm_ban, curr_results, nxt_pred, nxt_pct, nxt_detail, good_road
                        )

                        next_msg = (
                            f"🎯 *DỰ ĐOÁN TỰ ĐỘNG VÁN #{len(curr_results) + 1} — BÀN `{norm_ban}`*\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"👉 Cửa vào lệnh: {emoji} *{side_text}*\n"
                            f"📊 Độ tin cậy: *{nxt_pct}%*\n"
                            f"💰 Quản lý vốn: *{nxt_bet_advice}*\n"
                            f"{road_str}"
                            f"📈 Thế cầu: `{nxt_detail}`\n"
                            f"🧩 Pattern: `{nxt_pattern8}`\n"
                            f"🧠 *Y/k của AI:* {ai_opinion}\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"⏰ _Sảnh đang mở cược — Bạn có 15s để vào lệnh!_"
                        )

                        for cid in target_chats:
                            await safe_send(app.bot, cid, next_msg)

        except Exception as e:
            print(f"[RealTime Engine] Loop error: {e}")
            await asyncio.sleep(2)


# ======================== TELEGRAM HANDLERS ========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🎰 *BCR TELEGRAM BOT v3.5 PROMAX — TỰ ĐỘNG HOÁ 100% REAL & NO-KEY GEMINI*\n\n"
        "✨ *Hệ thống AI Baccarat Tự Động Hoàn Toàn:*\n"
        "• Tự động quét kết quả mỗi 1.5s ➜ Tự động check Thắng/Thua không cần gõ lệnh\n"
        "• Tự động gửi dự đoán tay mới sau 3s ➜ Đủ 15s vào lệnh kịp thời\n"
        "• Tích hợp Gemini AI No-Key (Sherlock) + Promax Multi-Neural AI\n\n"
        "📌 *Các lệnh sử dụng:*\n"
        "• `/autolive <bàn>` — ⚡ *BẬT TỰ ĐỘNG BÁM BÀN REAL-TIME* (VD: `/autolive C01` hoặc `/autolive 10`)\n"
        "• `/autolive off` — Dừng tự động bám bàn\n"
        "• `/autolive status` — Xem danh sách bàn đang bám tự động\n"
        "• `/autodudoan <bàn>` — Dự đoán tức thì + tự động kích hoạt bám bàn đó luôn\n"
        "• `/checkdudoan` — Quét TẤT CẢ các bàn 1 lần, lọc TOP bàn đẹp nhất\n"
        "• `/autocheckdudoan` — Bật/Tắt báo động toàn sảnh 24/7 (khi có bàn VIP ≥70%)\n"
        "• `/winrate` — Xem tỉ lệ thắng 30 phiên gần nhất\n"
        "• `/history` — Xem lịch sử 20 phiên\n"
        "• `/tables` — Xem danh sách bàn live\n\n"
        "⚡ _Hệ thống chạy mượt mà 24/7, tự động chống lag và không bao giờ đứng bot!_"
    )
    await safe_reply(update, msg)


async def autolive_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not context.args:
        with db.lock:
            subs = db.auto_live_subs.get(chat_id, [])
        if subs:
            subs_str = ", ".join([f"`{b}`" for b in subs])
            msg = (
                f"📡 *BÀN ĐANG ĐƯỢC THEO DÕI TỰ ĐỘNG:*\n"
                f"👉 Các bàn: {subs_str}\n\n"
                f"• Muốn dừng theo dõi, gõ: `/autolive off`\n"
                f"• Muốn thêm bàn khác, gõ: `/autolive <tên bàn>`"
            )
        else:
            msg = (
                "⚠️ *BẠN CHƯA BẬT THEO DÕI BÀN NÀO!*\n\n"
                "👉 Hãy gõ: `/autolive <tên bàn>` để bật bám tự động 100% không cần check tay!\n"
                "VD: `/autolive C01` hoặc `/autolive 10`\n"
                "💡 Hoặc gõ `/checkdudoan` để xem bàn nào đang có tỉ lệ thắng cao nhất."
            )
        await safe_reply(update, msg)
        return

    arg = context.args[0].strip().upper()
    if arg in ("OFF", "STOP", "TAT", "HUY"):
        db.remove_live_sub(chat_id, None)
        await safe_reply(
            update,
            "🛑 *ĐÃ DỪNG THEO DÕI TỰ ĐỘNG TOÀN BỘ BÀN!*\nBot sẽ ngừng gửi tin nhắn tự động vào đây."
        )
        return

    if arg == "STATUS":
        with db.lock:
            subs = db.auto_live_subs.get(chat_id, [])
        status_text = ", ".join([f"`{b}`" for b in subs]) if subs else "Chưa có bàn nào"
        await safe_reply(update, f"📊 *Trạng thái theo dõi tự động:* {status_text}")
        return

    norm_ban = normalize_ban_name(arg)
    await safe_reply(update, f"⏳ Đang kết nối bàn `{norm_ban}`...")

    table = await asyncio.to_thread(get_table, norm_ban)
    if not table:
        await safe_reply(update, f"❌ Không tìm thấy bàn `{norm_ban}` trên sảnh live. Dùng `/tables` để xem danh sách.")
        return

    db.add_live_sub(chat_id, norm_ban)
    results = table.get("results", "")
    _table_last_results[norm_ban] = results

    pred_res = predict_next(results)
    if pred_res and pred_res[0] is not None:
        pred, pct, detail, pattern8, bet_advice = pred_res
        _pending_predictions[norm_ban] = {
            "pred": pred,
            "pct": pct,
            "detail": detail,
            "pattern8": pattern8,
            "bet_advice": bet_advice,
            "ts": time.time(),
        }
        side_text = "PLAYER (P - Con)" if pred == "P" else "BANKER (B - Cái)"
        emoji = "🔵" if pred == "P" else "🔴"
        good_road = table.get("good_road", "") or "—"
        road_str = f"🛣 Thế cầu: `{good_road}`\n" if good_road != "—" else ""

        ai_opinion = await generate_ai_analysis_safe(norm_ban, results, pred, pct, detail, good_road)

        start_msg = (
            f"🚀 *ĐÃ KÍCH HOẠT TỰ ĐỘNG BÁM BÀN `{norm_ban}` (100% AUTO REAL-TIME)*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ *Cơ chế hoạt động:*\n"
            f"1. Tự động kiểm tra kết quả mỗi 1.5s (không cần check tay)\n"
            f"2. Tự động gửi dự đoán tay mới sau 3s từ khi ra kết quả\n"
            f"3. Tự động nạp mẫu học sâu vào AI Promax\n\n"
            f"🎯 *DỰ ĐOÁN VÁN HIỆN TẠI #{len(results) + 1}:*\n"
            f"👉 Cửa vào lệnh: {emoji} *{side_text}*\n"
            f"📊 Độ tin cậy: *{pct}%*\n"
            f"💰 Quản lý vốn: *{bet_advice}*\n"
            f"{road_str}"
            f"📈 Thế cầu: `{detail}`\n"
            f"🧩 Pattern: `{pattern8}`\n"
            f"🧠 *Y/k của AI:* {ai_opinion}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 _Từ ván sau, bot sẽ tự động gửi 100%! Để dừng gõ:_ `/autolive off`"
        )
    else:
        start_msg = (
            f"🚀 *ĐÃ KÍCH HOẠT TỰ ĐỘNG BÁM BÀN `{norm_ban}`*\n"
            f"Hiện tại bàn đang chờ đủ ván để tính toán. Ngay khi có kết quả mới, bot sẽ tự động phân tích và gửi ngay!"
        )

    await safe_reply(update, start_msg)


async def autodudoan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await safe_reply(update, "⚠️ Vui lòng nhập tên bàn!\nVD: `/autodudoan C01` hoặc `/autodudoan 10`")
        return

    raw_ban = context.args[0].strip()
    norm_ban = normalize_ban_name(raw_ban)
    await safe_reply(update, f"⏳ Đang phân tích bàn *{norm_ban}* (Promax Engine)...")

    try:
        table = await asyncio.wait_for(asyncio.to_thread(get_table, norm_ban), timeout=10.0)
    except Exception as e:
        print(f"[Autodudoan] get_table timeout/error: {e}")
        table = None

    if not table:
        try:
            all_tables = await asyncio.wait_for(asyncio.to_thread(fetch_tables), timeout=6.0)
        except Exception:
            all_tables = []
        avail = [f"`{t.get('ban')}`" for t in all_tables[:12]]
        suggest_text = f"\n\nCác bàn đang mở: {', '.join(avail)}" if avail else ""
        await safe_reply(
            update,
            f"❌ Không tìm thấy bàn `{norm_ban}` (hoặc bàn chưa vào phiên).{suggest_text}\n"
            f"💡 Dùng `/tables` hoặc `/checkdudoan` để xem danh sách tất cả bàn đang mở."
        )
        return

    ban = str(table.get("ban", norm_ban)).upper()
    results = table.get("results", "")
    good_road = table.get("good_road", "") or "—"
    update_at = table.get("update_at", "")

    pred_res = predict_next(results)
    if pred_res[0] is None:
        await safe_reply(
            update,
            f"❌ Bàn {ban} chưa đủ dữ liệu P/B (≥8 ván). Hiện tại chỉ có: {len([c for c in results if c in 'PB'])} ván."
        )
        return

    pred, pct, detail, pattern8, bet_advice = pred_res

    chat_id = update.effective_chat.id
    db.add_live_sub(chat_id, norm_ban)
    _table_last_results[norm_ban] = results
    _pending_predictions[norm_ban] = {
        "pred": pred,
        "pct": pct,
        "detail": detail,
        "pattern8": pattern8,
        "bet_advice": bet_advice,
        "ts": time.time(),
    }

    ai_opinion = await generate_ai_analysis_safe(ban, results, pred, pct, detail, good_road)

    side = "PLAYER (P - Con)" if pred == "P" else "BANKER (B - Cái)"
    emoji = "🔵" if pred == "P" else "🔴"

    db.record_prediction(ban, pred, None, pct, pattern8, "promax_auto")

    text = (
        f"{emoji} *DỰ ĐOÁN PROMAX BÀN {ban}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Dự đoán cửa: *{side}*\n"
        f"📊 Độ tin cậy: *{pct}%*\n"
        f"💰 Quản lý vốn: *{bet_advice}*\n"
        f"🛣 Thế cầu hiện: `{good_road}`\n"
        f"📈 Phân tích logic: `{detail}`\n"
        f"🧩 Pattern 8 ván: `{pattern8}`\n"
        f"🧠 *Y/k của AI:* {ai_opinion}\n"
        f"🕐 Cập nhật: `{update_at}`\n"
        f"📦 Tổng ván đã ra: `{len(results)}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *ĐÃ TỰ ĐỘNG BẬT THEO DÕI REAL-TIME BÀN {ban}!*\n"
        f"• Ván sau bạn KHÔNG CẦN check tay. Khi mở bài xong, bot sẽ tự đối soát Thắng/Thua và tự gửi dự đoán ván mới sau 3s!\n"
        f"• Để dừng bám bàn, gõ: `/autolive off`"
    )
    await safe_reply(update, text)


async def checkdudoan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, "🔍 *Đang quét và phân tích TẤT CẢ các bàn live (Promax V3)...*")

    try:
        scanned = await asyncio.wait_for(asyncio.to_thread(scan_all_tables_sync), timeout=10.0)
    except Exception as e:
        print(f"[CheckDuDoan] error: {e}")
        scanned = []

    if not scanned:
        await safe_reply(update, "❌ Không có bàn nào đủ dữ liệu (≥8 ván) hoặc API live đang bảo trì.")
        return

    top_picks = scanned[:7]
    lines = [
        "🏆 *BẢNG TỔNG HỢP SOI TOÀN BỘ BÀN LIVE*",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📊 Đã phân tích: *{len(scanned)} bàn* | Sắp xếp theo độ tin cậy cao nhất:\n"
    ]

    for idx, item in enumerate(top_picks, 1):
        ban = item["ban"]
        pred = item["pred"]
        pct = item["pct"]
        side_text = "PLAYER (P)" if pred == "P" else "BANKER (B)"
        emoji = "🔵" if pred == "P" else "🔴"

        highlight = "🔥 *VIP*" if pct >= 70 else "⚡"
        road_info = f" • `{item['good_road']}`" if item['good_road'] != "—" else ""
        lines.append(
            f"{highlight} *{idx}. Bàn `{ban}`* [{item['pb_count']} ván]:\n"
            f"   👉 Cửa: {emoji} *{side_text}* | Độ tin cậy: *{pct}%*\n"
            f"   💰 Vốn: _{item['bet_advice']}_\n"
            f"   📈 Thế cầu: `{item['detail'][:40]}`{road_info}\n"
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("💡 *Khuyến nghị:* Ưu tiên vào các bàn có đánh dấu 🔥 *VIP*.")
    lines.append("👉 Để bám tự động 1 bàn (tự check kết quả + tự gửi dự đoán 3s), gõ: `/autolive <tên bàn>`")

    await safe_reply(update, "\n".join(lines))


async def autocheckdudoan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    arg = context.args[0].strip().lower() if context.args else ""

    if arg == "status":
        with db.lock:
            is_active = chat_id in db.auto_scan_chats
            total_active = len(db.auto_scan_chats)
        status_str = "🟢 ĐANG BẬT" if is_active else "🔴 ĐANG TẮT"
        msg = (
            f"📡 *TRẠNG THÁI QUÉT TOÀN SẢNH (AUTO-SCAN 24/7)*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• Trạng thái chat này: *{status_str}*\n"
            f"• Chu kỳ quét: Mỗi *{AUTO_SCAN_INTERVAL} giây*\n"
            f"• Ngưỡng cảnh báo: Bàn tỉ lệ *≥ {AUTO_SCAN_MIN_PCT}%* hoặc Cầu Bệt/PingPong nét\n"
            f"• Tổng số chat đang kích hoạt: *{total_active}*\n\n"
            f"👉 Gõ `/autocheckdudoan on` để BẬT\n"
            f"👉 Gõ `/autocheckdudoan off` để TẮT"
        )
        await safe_reply(update, msg)
        return

    with db.lock:
        if arg == "on":
            db.auto_scan_chats.add(chat_id)
            is_active = True
        elif arg == "off":
            db.auto_scan_chats.discard(chat_id)
            is_active = False
        else:
            if chat_id in db.auto_scan_chats:
                db.auto_scan_chats.discard(chat_id)
                is_active = False
            else:
                db.auto_scan_chats.add(chat_id)
                is_active = True
        db.save()

    if is_active:
        status_msg = (
            "🚨 *ĐÃ BẬT TỰ ĐỘNG QUÉT TOÀN BỘ BÀN (AUTO-SCAN 24/7)* 🚨\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• Chu kỳ quét: Mỗi *{AUTO_SCAN_INTERVAL} giây*\n"
            f"• Ngưỡng cảnh báo: Tỉ lệ *≥ {AUTO_SCAN_MIN_PCT}%* hoặc Cầu Bệt/Ping Pong đẹp\n"
            "• Khi phát hiện cầu ngon, bot sẽ tự động gửi thông báo trực tiếp vào đây!\n\n"
            "👉 Để tắt thông báo tự động, gõ: `/autocheckdudoan off`"
        )
        await safe_reply(update, status_msg)
        await checkdudoan_cmd(update, context)
    else:
        status_msg = (
            "⏸ *ĐÃ TẮT TỰ ĐỘNG QUÉT TOÀN BỘ BÀN*\n"
            "Bot sẽ không gửi thông báo ngầm vào đây nữa.\n"
            "💡 Bạn có thể dùng `/autolive <bàn>` để bám riêng 1 bàn cụ thể!"
        )
        await safe_reply(update, status_msg)


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await safe_reply(update, "⚠️ Dùng: `/check C01 P` hoặc `/check 10 B`")
        return

    raw_ban = context.args[0].strip()
    norm_ban = normalize_ban_name(raw_ban)
    actual = context.args[1].strip().upper()
    if actual not in ("P", "B"):
        await safe_reply(update, "⚠️ Kết quả thực tế phải là `P` (Player) hoặc `B` (Banker).")
        return

    with db.lock:
        found = None
        for p in reversed(db.predictions):
            if normalize_ban_name(p["ban"]) == norm_ban and p.get("actual") is None:
                found = p
                break
        if not found:
            for p in reversed(db.predictions):
                if normalize_ban_name(p["ban"]) == norm_ban:
                    found = p
                    break

    if not found:
        await safe_reply(update, f"❌ Chưa có phiên dự đoán nào cho bàn {norm_ban}. Hãy gõ `/autodudoan {norm_ban}` trước.")
        return

    pred = found["pred"]
    pattern8 = found.get("pattern", "")
    correct = (pred == actual)

    found["actual"] = actual
    found["correct"] = correct
    db.save()

    if pattern8:
        db.add_pattern(pattern8, actual, correct)

    status = "🎉 ✅ ĐÚNG" if correct else "⚠️ ❌ SAI"
    await safe_reply(
        update,
        f"{status} — Bàn *{found['ban']}*\n"
        f"Dự đoán: `{pred}` | Thực tế: `{actual}`\n"
        f"📝 Thuật toán đã ghi nhận kết quả thành công!"
    )


async def winrate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    correct, total, rate = db.get_winrate(WINRATE_WINDOW)
    if total == 0:
        await safe_reply(update, "📭 Chưa có dữ liệu phiên đã kiểm tra. Dùng `/autolive <bàn>` để bot tự động ghi nhận.")
        return
    bar_len = 10
    filled = round(rate / 100 * bar_len)
    bar = "█" * filled + "░" * (bar_len - filled)
    await safe_reply(
        update,
        f"📊 *TỈ LỆ THẮNG (WINRATE) PROMAX*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ Đúng: *{correct}/{total}* phiên gần nhất\n"
        f"📈 Tỉ lệ chính xác: *{rate:.1f}%*\n"
        f"`{bar}` {rate:.0f}%\n"
        f"🧩 Bộ nhớ patterns đã học: *{len(db.patterns)}* thế cầu\n"
        f"⚡ Thuật toán: Promax Multi-Roads & Markov Chain (Auto-Trained)"
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hist = db.get_history(HISTORY_SHOW)
    if not hist:
        await safe_reply(update, "📭 Chưa có lịch sử dự đoán.")
        return

    checked = [h for h in hist if h.get("correct") is not None]
    correct_n = sum(1 for h in checked if h["correct"])
    total_c = len(checked)

    lines = [f"📜 *LỊCH SỬ {len(hist)} PHIÊN GẦN NHẤT*\n"]
    if total_c:
        wr = (correct_n / total_c) * 100
        lines.append(f"🎯 Thắng *{correct_n}/{total_c}* phiên đã đối soát ({wr:.1f}%)\n")
    else:
        lines.append("Chưa có phiên nào được check kết quả\n")

    lines.append("━━━━━━━━━━━━━━━━━━")
    for i, h in enumerate(reversed(hist), 1):
        ts = h.get("ts", "")[-8:]
        ban = h.get("ban", "?")
        pred = h.get("pred", "?")
        actual = h.get("actual")
        pct = h.get("pct", 50)
        if actual is None:
            st = "⏳ Chờ"
        elif h.get("correct"):
            st = "✅ Thắng"
        else:
            st = "❌ Thua"
        lines.append(f"{i}. `{ban}` [{pred} {pct}%] → {actual or '?'} | {st} | {ts}")

    await safe_reply(update, "\n".join(lines))


async def tables_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, "⏳ Đang tải danh sách bàn live...")
    try:
        data = await asyncio.wait_for(asyncio.to_thread(fetch_tables), timeout=10.0)
    except Exception:
        data = []

    if not data:
        await safe_reply(update, "❌ Không lấy được dữ liệu bàn live từ API (đang thử kết nối lại).")
        return

    lines = ["📋 *DANH SÁCH BÀN LIVE & THẾ CẦU*\n"]
    for t in sorted(data, key=lambda x: (0 if not str(x.get("ban", "")).startswith("C") else 1, str(x.get("ban", "")))):
        ban_name = str(t.get("ban", ""))
        road = f" | {t['good_road']}" if t.get("good_road") else ""
        res_len = len(t.get("results", ""))
        lines.append(f"• `{ban_name}` — {res_len} ván{road}")
    lines.append("\n👉 Gõ: `/autolive <tên bàn>` để tự động bám bàn không cần check tay!")
    await safe_reply(update, "\n".join(lines))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ======================== BACKGROUND AUTO-SCAN TASK 24/7 ========================
async def background_auto_scan_task(app: Application):
    """Tác vụ chạy ngầm định kỳ quét toàn bộ bàn 24/7 báo động bàn VIP"""
    print("[AutoScan] 🚀 Background Auto-Scan 24/7 Task Started!")
    while True:
        try:
            await asyncio.sleep(AUTO_SCAN_INTERVAL)
            with db.lock:
                target_chats = list(db.auto_scan_chats)

            if not target_chats:
                continue

            try:
                scanned = await asyncio.wait_for(
                    asyncio.to_thread(scan_all_tables_sync),
                    timeout=15.0
                )
            except Exception as e:
                print(f"[AutoScan] Scan timeout/error: {e}")
                continue

            if not scanned:
                continue

            hot_candidates = []
            for t in scanned:
                pct = t.get("pct", 50)
                detail = t.get("detail", "")
                is_streak = ("Bệt" in detail) or ("PingPong" in detail) or ("1-1" in detail)
                if pct >= AUTO_SCAN_MIN_PCT or (pct >= 66 and is_streak):
                    hot_candidates.append(t)

            if not hot_candidates:
                continue

            hot_candidates.sort(key=lambda x: x["pct"], reverse=True)
            now = time.time()
            best = None
            for cand in hot_candidates:
                ban = cand["ban"]
                hands = cand.get("total_hands", 0)
                key = f"{ban}_{hands}"
                if key in _last_alerted_keys:
                    continue
                best = cand
                _last_alerted_keys[key] = now
                break

            if len(_last_alerted_keys) > 200:
                old_keys = [k for k, v in _last_alerted_keys.items() if now - v > 3600]
                for k in old_keys:
                    del _last_alerted_keys[k]

            if not best:
                continue

            ban = best["ban"]
            pred = best["pred"]
            pct = best["pct"]
            side_text = "PLAYER (P - Con)" if pred == "P" else "BANKER (B - Cái)"
            emoji = "🔵" if pred == "P" else "🔴"
            good_road = best.get("good_road", "—")
            road_str = f"🛣 Thế cầu: `{good_road}`\n" if good_road != "—" else ""

            ai_opinion = await generate_ai_analysis_safe(ban, best.get("results", ""), pred, pct, best["detail"], good_road)

            alert_msg = (
                f"🚨 *BÁO ĐỘNG CẦU ĐẸP TOÀN SẢNH!* 🚨\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 Bàn *{ban}* vừa đạt tỉ lệ *{pct}%* cực cao!\n"
                f"👉 Cửa vào lệnh: {emoji} *{side_text}*\n"
                f"💰 Quản lý vốn: *{best['bet_advice']}*\n"
                f"{road_str}"
                f"📈 Nhận diện: `{best['detail']}`\n"
                f"🧠 *Y/k của AI:* {ai_opinion}\n"
                f"📦 Ván hiện tại: *{best.get('total_hands', '?')}*\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💡 Gõ `/autolive {ban}` để bot tự động bám bàn này 100% không cần check tay!"
            )

            for cid in target_chats:
                await safe_send(app.bot, cid, alert_msg)

        except Exception as e:
            print(f"[AutoScan] Loop error: {e}")
            await asyncio.sleep(5)


# ======================== FLASK (RENDER HEALTH CHECK) ========================
flask_app = Flask(__name__)


@flask_app.route("/")
def health():
    return "BCR Bot v3.5 PROMAX REAL-TIME OK", 200


@flask_app.route("/stats")
def stats():
    with db.lock:
        p_len = len(db.patterns)
        preds_len = len(db.predictions)
        scan_chats = len(db.auto_scan_chats)
        live_subs = len(db.auto_live_subs)
    return {
        "status": "ok",
        "version": "3.5 PROMAX REAL-TIME & NO-KEY GEMINI",
        "patterns": p_len,
        "predictions": preds_len,
        "auto_scan_active_chats": scan_chats,
        "auto_live_subscriptions": live_subs,
        "winrate_window": WINRATE_WINDOW,
    }, 200


def run_flask():
    print(f"Flask health server starting on port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)


# ======================== MAIN ENTRYPOINT ========================
if __name__ == "__main__":
    print("=" * 65)
    print("BCR Telegram Bot v3.5 PROMAX REAL-TIME & NO-KEY GEMINI — 100% AUTO")
    print(f"Bot Token: {BOT_TOKEN[:10]}...{BOT_TOKEN[-6:]}")
    print(f"Patterns loaded: {len(db.patterns)}")
    print(f"Predictions: {len(db.predictions)}")
    print(f"Auto-scan subscribed chats: {len(db.auto_scan_chats)}")
    print(f"Auto-live active subscriptions: {len(db.auto_live_subs)}")
    print("=" * 65)

    flask_thread = threading.Thread(target=run_flask, daemon=True, name="flask")
    flask_thread.start()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("autolive", autolive_cmd))
    application.add_handler(CommandHandler("autodudoan", autodudoan))
    application.add_handler(CommandHandler("checkdudoan", checkdudoan_cmd))
    application.add_handler(CommandHandler("autocheckdudoan", autocheckdudoan_cmd))
    application.add_handler(CommandHandler("check", check_cmd))
    application.add_handler(CommandHandler("winrate", winrate_cmd))
    application.add_handler(CommandHandler("history", history_cmd))
    application.add_handler(CommandHandler("tables", tables_cmd))

    async def post_init(app: Application):
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.bot.set_my_commands([
            BotCommand("autolive", "Tự động bám bàn 100% (tự check 1s + dự đoán 3s)"),
            BotCommand("autodudoan", "Dự đoán 1 bàn + Tự động bám bàn đó"),
            BotCommand("checkdudoan", "Quét ALL bàn 1 lần & xếp hạng tỉ lệ cao"),
            BotCommand("autocheckdudoan", "Bật/tắt tự động quét ngầm toàn sảnh 24/7"),
            BotCommand("winrate", "Tỉ lệ thắng 30 phiên gần nhất"),
            BotCommand("history", "20 phiên dự đoán gần nhất"),
            BotCommand("tables", "Danh sách bàn live & thế cầu"),
            BotCommand("start", "Hướng dẫn sử dụng"),
        ])
        realtime_task = asyncio.create_task(realtime_auto_monitor_task(app))
        _background_tasks.add(realtime_task)
        realtime_task.add_done_callback(_background_tasks.discard)

        scan_task = asyncio.create_task(background_auto_scan_task(app))
        _background_tasks.add(scan_task)
        scan_task.add_done_callback(_background_tasks.discard)

    application.post_init = post_init

    print("Telegram bot starting polling on MAIN thread (Non-blocking Promax V3.5 Mode)...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )