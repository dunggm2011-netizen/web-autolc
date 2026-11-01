import hashlib
import base64
import json
import logging
import threading
import random
import time
import os

import requests
import socketio
from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit

# ==========================================
# CẤU HÌNH HỆ THỐNG
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logging.getLogger('engineio').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)

# Tự động lấy cổng từ môi trường Render (nếu chạy local sẽ mặc định là 3394)
PORT = int(os.environ.get("PORT", 3394))

app = Flask(__name__)
app.config['SECRET_KEY'] = 'txmd5-secret-key-2024'
web_sio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Lưu trạng thái theo client_id (mỗi tab trình duyệt là 1 client riêng biệt)
active_sockets = {}   # client_id -> socket.io client (đến game server)
user_states = {}      # client_id -> dict state

def init_user_state(cid):
    if cid not in user_states:
        user_states[cid] = {
            "history": [],
            "profit_loss": 0,
            "auto_bet_enabled": False,
            "bet_amount": 10000,
            "current_prediction": None,
            "waiting_for_result": False,
            "has_bet_this_session": False,
            "session_id": None,
            "balance": 0,
            "nickname": None,
        }
    return user_states[cid]

def push(cid, event, data):
    """Gửi event realtime về browser của client tương ứng."""
    try:
        web_sio.emit(event, data, to=cid)
    except Exception as e:
        logger.error(f"Push error: {e}")

# ==========================================
# THUẬT TOÁN DỰ ĐOÁN ENSEMBLE AI
# ==========================================
def make_prediction_vip(history):
    if len(history) < 5:
        return random.choice(["TAI", "XIU"])

    score_tai = 0
    score_xiu = 0

    recent_10 = history[-10:]
    if recent_10.count("TAI") > recent_10.count("XIU"): score_tai += 1
    elif recent_10.count("XIU") > recent_10.count("TAI"): score_xiu += 1

    recent_5 = "".join("T" if x == "TAI" else "X" for x in history[-5:])
    if recent_5.endswith("TTTT"): score_tai += 3
    elif recent_5.endswith("XXXX"): score_xiu += 3
    elif recent_5.endswith("TXTX"): score_tai += 2
    elif recent_5.endswith("XTXT"): score_xiu += 2
    elif recent_5.endswith("TTXX"): score_tai += 2
    elif recent_5.endswith("XXTT"): score_xiu += 2
    elif recent_5.endswith("TXX"): score_tai += 1
    elif recent_5.endswith("XTT"): score_xiu += 1

    transitions_1 = {"TAI": {"TAI": 0, "XIU": 0}, "XIU": {"TAI": 0, "XIU": 0}}
    for i in range(len(history) - 1):
        if history[i] in transitions_1 and history[i+1] in transitions_1[history[i]]:
            transitions_1[history[i]][history[i+1]] += 1

    last_1 = history[-1]
    if last_1 in transitions_1:
        if transitions_1[last_1]["TAI"] > transitions_1[last_1]["XIU"]: score_tai += 2
        elif transitions_1[last_1]["XIU"] > transitions_1[last_1]["TAI"]: score_xiu += 2

    if len(history) >= 10:
        transitions_2 = {}
        for i in range(len(history) - 2):
            state = f"{history[i]}_{history[i+1]}"
            nxt = history[i+2]
            transitions_2.setdefault(state, {"TAI": 0, "XIU": 0})
            if nxt in transitions_2[state]:
                transitions_2[state][nxt] += 1
        last_2 = f"{history[-2]}_{history[-1]}"
        if last_2 in transitions_2:
            if transitions_2[last_2]["TAI"] > transitions_2[last_2]["XIU"]: score_tai += 3
            elif transitions_2[last_2]["XIU"] > transitions_2[last_2]["TAI"]: score_xiu += 3

    if score_tai > score_xiu: return "TAI"
    if score_xiu > score_tai: return "XIU"
    return history[-1]

# ==========================================
# API ĐĂNG NHẬP
# ==========================================
def md5_hash(text): return hashlib.md5(text.encode('utf-8')).hexdigest()

def login_and_get_token(username, password):
    pw_md5 = md5_hash(password)
    url = f"https://apifo88daigia.tele68.com/api?c=3&un={username}&pw={pw_md5}&cp=R&cl=R&pf=web&at="
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"_error": f"Lỗi kết nối server: {e}"}

    if not data.get("success"):
        return {"_error": f"Lỗi Game: {data.get('message', 'Sai thông tin đăng nhập')}"}

    try:
        sk = data["sessionKey"]
        sk += "=" * ((4 - len(sk) % 4) % 4)
        sd = json.loads(base64.b64decode(sk).decode('utf-8'))
        nickname = sd.get("nickname") or sd.get("nickName")
    except Exception as e:
        return {"_error": f"Lỗi giải mã nhân vật: {e}"}

    headers = {
        "accept": "*/*", "content-type": "application/json",
        "origin": "https://lc79b.bet", "referer": "https://lc79b.bet/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
    }
    try:
        r2 = requests.post(
            "https://wlb.tele68.com/v1/lobby/auth/login?cp=R&cl=R&pf=web&at=",
            headers=headers,
            json={"nickName": nickname, "accessToken": data["accessToken"]},
            timeout=12,
        )
        r2.raise_for_status()
        lobby = r2.json()
    except Exception as e:
        return {"_error": f"Lỗi kết nối Lobby: {e}"}

    token = lobby.get("token")
    if not token:
        return {"_error": "Lobby không trả về JWT token hợp lệ."}

    return {
        "token": token,
        "nickname": nickname,
        "money": lobby.get("remoteLoginResp", {}).get("money", 0),
    }

# ==========================================
# GAME SOCKET.IO
# ==========================================
def start_websocket(cid, token):
    if cid in active_sockets:
        try: active_sockets[cid].disconnect()
        except: pass

    sio = socketio.Client(reconnection=True, reconnection_attempts=5, logger=False, engineio_logger=False)
    active_sockets[cid] = sio
    init_user_state(cid)

    @sio.event(namespace='/txmd5')
    def connect():
        logger.info(f"[{cid}] Game socket connected")
        push(cid, "log", {"level": "ok", "msg": "🟢 Kết nối máy chủ Game thành công!"})

    @sio.event(namespace='/txmd5')
    def disconnect():
        logger.warning(f"[{cid}] Game socket disconnected")
        push(cid, "log", {"level": "err", "msg": "🔴 Mất kết nối tới Game Server! Đang thử lại..."})

    @sio.on('new-session', namespace='/txmd5')
    def on_new_session(data):
        st = user_states[cid]
        st["session_id"] = data.get('id', 'N/A')
        st["has_bet_this_session"] = False
        hist_len = len(st["history"])

        if hist_len >= 5:
            st["current_prediction"] = make_prediction_vip(st["history"])
        else:
            st["current_prediction"] = None

        push(cid, "new_session", {
            "session_id": st["session_id"],
            "prediction": st["current_prediction"],
            "history_len": hist_len,
        })

    @sio.on('tick-update', namespace='/txmd5')
    def on_tick_update(data):
        gs = data.get('state')
        st = user_states[cid]
        push(cid, "tick", {"state": gs, "remain": data.get('remainTime')})

        if gs == 'BETTING' and st["auto_bet_enabled"] and st["current_prediction"]:
            if not st["has_bet_this_session"]:
                sio.emit('bet', {"type": st["current_prediction"], "amount": st["bet_amount"]}, namespace='/txmd5')
                st["has_bet_this_session"] = True
                st["waiting_for_result"] = True
                push(cid, "log", {"level": "info", "msg": f"⏳ Đã gửi lệnh cược {st['bet_amount']:,} vào {st['current_prediction']}"})

    @sio.on('bet-result', namespace='/txmd5')
    def on_bet_result(data):
        st = user_states[cid]
        if "postBalance" in data:
            st["balance"] = data["postBalance"]
        push(cid, "bet_result", {"balance": st["balance"]})
        push(cid, "log", {"level": "ok", "msg": f"✅ Đặt cược thành công. Số dư: {st['balance']:,}"})
        sio.emit('get-current-my-info', None, namespace='/txmd5')

    @sio.on('session-result', namespace='/txmd5')
    def on_session_result(data):
        dices = data.get('dices', [0, 0, 0])
        result = data.get('resultTruyenThong', 'N/A')
        st = user_states[cid]

        if result in ["TAI", "XIU"]:
            st["history"].append(result)
            if len(st["history"]) > 100: st["history"].pop(0)

        correct = None
        profit_delta = 0
        if st["current_prediction"]:
            if st["current_prediction"] == result:
                correct = True
                if st["waiting_for_result"]:
                    win = int(st["bet_amount"] * 0.98)
                    st["profit_loss"] += win
                    profit_delta = win
            else:
                correct = False
                if st["waiting_for_result"]:
                    st["profit_loss"] -= st["bet_amount"]
                    profit_delta = -st["bet_amount"]
            st["waiting_for_result"] = False

        push(cid, "session_result", {
            "result": result, "dices": dices, "sum": sum(dices),
            "correct": correct, "profit_delta": profit_delta,
            "profit_loss": st["profit_loss"], "history": st["history"][-20:],
        })

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
            "Origin": "https://lc79b.bet", "Referer": "https://lc79b.bet/", "Accept": "*/*",
        }
        sio.connect(
            'https://wtxmd52.tele68.com',
            socketio_path='txmd5/',
            namespaces=['/txmd5'],
            transports=['websocket'],
            auth={"token": token},
            headers=headers,
        )
        sio.wait()
    except Exception as e:
        logger.error(f"[{cid}] WS error: {e}")
        push(cid, "log", {"level": "err", "msg": f"⚠️ Lỗi khởi tạo Game WebSocket: {e}"})

# ==========================================
# WEB SOCKET EVENTS
# ==========================================
@web_sio.on('connect')
def on_web_connect():
    cid = request.sid
    init_user_state(cid)
    logger.info(f"Browser connected: {cid}")

@web_sio.on('disconnect')
def on_web_disconnect():
    cid = request.sid
    if cid in active_sockets:
        try: active_sockets[cid].disconnect()
        except: pass
        del active_sockets[cid]
    user_states.pop(cid, None)
    logger.info(f"Browser disconnected: {cid}")

@web_sio.on('do_login')
def on_do_login(data):
    cid = request.sid
    username = (data or {}).get("username", "").strip()
    password = (data or {}).get("password", "")
    if not username or not password:
        emit('login_result', {"ok": False, "error": "Thiếu tài khoản/mật khẩu"})
        return

    emit('login_result', {"pending": True, "msg": "🔄 Đang xác thực với hệ thống..."})
    result = login_and_get_token(username, password)

    if "_error" in result:
        emit('login_result', {"ok": False, "error": result["_error"]})
        return

    st = init_user_state(cid)
    st["balance"] = result["money"]
    st["nickname"] = result["nickname"]
    st["history"] = []
    st["profit_loss"] = 0

    emit('login_result', {"ok": True, "nickname": result["nickname"], "money": result["money"]})
    threading.Thread(target=start_websocket, args=(cid, result["token"]), daemon=True).start()

@web_sio.on('set_autobet')
def on_set_autobet(data):
    cid = request.sid
    if cid not in user_states:
        emit('log', {"level": "err", "msg": "Chưa đăng nhập!"})
        return
    st = user_states[cid]
    enabled = bool(data.get("enabled"))
    try:
        amount = int(data.get("amount", st["bet_amount"]))
        if amount <= 0: raise ValueError
    except:
        emit('log', {"level": "err", "msg": "Mức cược không hợp lệ!"})
        return

    st["auto_bet_enabled"] = enabled
    st["bet_amount"] = amount
    emit('autobet_state', {"enabled": enabled, "amount": amount})
    emit('log', {"level": "ok" if enabled else "info", "msg": f"{'✅ ĐÃ BẬT' if enabled else '🔴 ĐÃ TẮT'} Auto Bet @ {amount:,}"})

@web_sio.on('stop')
def on_stop():
    cid = request.sid
    if cid in active_sockets:
        try: active_sockets[cid].disconnect()
        except: pass
        del active_sockets[cid]
    if cid in user_states:
        user_states[cid]["auto_bet_enabled"] = False
    emit('log', {"level": "info", "msg": "🔌 Đã ngắt kết nối Game."})

# ==========================================
# HTML UI
# ==========================================
HTML = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<title>TX MD5 - AI Auto Bet</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: 'Segoe UI', sans-serif; background: linear-gradient(135deg, #0f172a, #1e293b); color: #e2e8f0; min-height: 100vh; padding: 16px; }
  .container { max-width: 1000px; margin: 0 auto; }
  h1 { text-align: center; font-size: 24px; margin: 8px 0 20px; background: linear-gradient(90deg, #38bdf8, #a78bfa); -webkit-background-clip: text; background-clip: text; color: transparent; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 720px) { .grid { grid-template-columns: 1fr; } }
  .card { background: rgba(30, 41, 59, 0.8); border: 1px solid #334155; border-radius: 12px; padding: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.3); }
  .card h2 { margin: 0 0 12px; font-size: 13px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }
  input { width: 100%; padding: 10px 12px; margin-bottom: 8px; background: #0f172a; border: 1px solid #334155; border-radius: 8px; color: #e2e8f0; font-size: 14px; }
  input:focus { outline: none; border-color: #38bdf8; }
  button { padding: 10px 16px; border: none; border-radius: 8px; cursor: pointer; font-weight: 600; font-size: 14px; transition: 0.2s; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-primary { background: #38bdf8; color: #0f172a; width: 100%; }
  .btn-primary:hover:not(:disabled) { background: #0ea5e9; }
  .btn-green { background: #22c55e; color: #fff; }
  .btn-green:hover:not(:disabled) { background: #16a34a; }
  .btn-red { background: #ef4444; color: #fff; }
  .btn-red:hover:not(:disabled) { background: #dc2626; }
  .btn-row { display: flex; gap: 8px; margin-top: 8px; }
  .btn-row button { flex: 1; }
  .stat { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed #334155; font-size: 14px; }
  .stat:last-child { border: none; }
  .stat .label { color: #94a3b8; }
  .stat .value { font-weight: 600; color: #f1f5f9; }
  .badge { padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; }
  .badge-none { background: #475569; color: #cbd5e1; }
  .badge-ok { background: #22c55e; color: #fff; }
  .badge-err { background: #ef4444; color: #fff; }
  .history { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }
  .history .dot { width: 24px; height: 24px; border-radius: 6px; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; }
  .dot.tai { background: #3b82f6; color: #fff; }
  .dot.xiu { background: #ef4444; color: #fff; }
  #log { height: 220px; overflow-y: auto; font-family: 'Consolas', monospace; font-size: 12px; background: #0a0f1e; border-radius: 8px; padding: 10px; border: 1px solid #334155; }
  .log-item { padding: 3px 0; border-bottom: 1px solid #1e293b; }
  .log-ok { color: #22c55e; }
  .log-err { color: #ef4444; }
  .log-info { color: #38bdf8; }
  .pred-box { text-align: center; padding: 16px; border-radius: 10px; margin: 8px 0; font-size: 20px; font-weight: 700; }
  .pred-tai { background: linear-gradient(135deg, #1e40af, #3b82f6); color: #fff; }
  .pred-xiu { background: linear-gradient(135deg, #991b1b, #ef4444); color: #fff; }
  .pred-none { background: #334155; color: #94a3b8; font-size: 14px; }
  .timer { text-align: center; font-size: 28px; font-weight: 800; color: #facc15; font-family: 'Consolas', monospace; }
</style>
</head>
<body>
<div class="container">
  <h1>🤖 TX MD5 — AI AUTO BET</h1>
  <div class="grid">
    <div class="card">
      <h2>🔐 Đăng nhập</h2>
      <input id="username" placeholder="Tên đăng nhập" autocomplete="off">
      <input id="password" type="password" placeholder="Mật khẩu" autocomplete="off">
      <button class="btn-primary" id="btnLogin" onclick="doLogin()">ĐĂNG NHẬP</button>
      <div style="margin-top:12px">
        <div class="stat"><span class="label">Nhân vật</span><span class="value" id="nickname">—</span></div>
        <div class="stat"><span class="label">Số dư</span><span class="value" id="balance">0</span></div>
        <div class="stat"><span class="label">Trạng thái</span><span class="value"><span id="connBadge" class="badge badge-none">OFFLINE</span></span></div>
      </div>
    </div>

    <div class="card">
      <h2>⚡ Auto Bet</h2>
      <input id="betAmount" type="number" value="10000" min="1" placeholder="Mức cược">
      <div class="btn-row">
        <button class="btn-green" onclick="toggleAuto(true)">BẬT</button>
        <button class="btn-red" onclick="toggleAuto(false)">TẮT</button>
        <button class="btn-red" onclick="stopAll()" style="background:#64748b">STOP</button>
      </div>
      <div style="margin-top:12px">
        <div class="stat"><span class="label">Auto Bet</span><span class="value"><span id="autoBadge" class="badge badge-none">OFF</span></span></div>
        <div class="stat"><span class="label">Lãi/Lỗ</span><span class="value" id="pl">0</span></div>
      </div>
    </div>

    <div class="card">
      <h2>🎯 Dự đoán AI</h2>
      <div class="stat"><span class="label">Phiên</span><span class="value" id="sessionId">—</span></div>
      <div class="pred-box pred-none" id="predBox">Chưa đủ dữ liệu (0/5)</div>
      <div class="timer" id="timer">--</div>
      <div class="stat"><span class="label">Trạng thái ván</span><span class="value" id="gameState">—</span></div>
    </div>

    <div class="card">
      <h2>📊 Lịch sử (20 phiên)</h2>
      <div class="history" id="history"></div>
    </div>

    <div class="card" style="grid-column: 1 / -1;">
      <h2>📜 Log hệ thống</h2>
      <div id="log"></div>
    </div>
  </div>
</div>

<script>
const socket = io();
let loggedIn = false;

function fmt(n) { return (n||0).toLocaleString('vi-VN'); }

function addLog(level, msg) {
  const el = document.getElementById('log');
  const div = document.createElement('div');
  div.className = 'log-item log-' + (level || 'info');
  const t = new Date().toLocaleTimeString('vi-VN');
  div.textContent = `[${t}] ${msg}`;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
  while (el.children.length > 200) el.removeChild(el.firstChild);
}

function setConn(ok) {
  const b = document.getElementById('connBadge');
  b.className = 'badge ' + (ok ? 'badge-ok' : 'badge-none');
  b.textContent = ok ? 'ONLINE' : 'OFFLINE';
}

function renderPrediction(pred, histLen) {
  const box = document.getElementById('predBox');
  if (!pred) {
    box.className = 'pred-box pred-none';
    box.textContent = `Chưa đủ dữ liệu (${histLen}/5)`;
    return;
  }
  box.className = 'pred-box ' + (pred === 'TAI' ? 'pred-tai' : 'pred-xiu');
  box.textContent = (pred === 'TAI' ? '🔵 TÀI' : '🔴 XỈU');
}

function renderHistory(arr) {
  const el = document.getElementById('history');
  el.innerHTML = '';
  (arr || []).forEach(r => {
    const d = document.createElement('div');
    d.className = 'dot ' + (r === 'TAI' ? 'tai' : 'xiu');
    d.textContent = r === 'TAI' ? 'T' : 'X';
    el.appendChild(d);
  });
}

function doLogin() {
  const u = document.getElementById('username').value.trim();
  const p = document.getElementById('password').value;
  if (!u || !p) { addLog('err', 'Vui lòng nhập đủ tài khoản & mật khẩu'); return; }
  document.getElementById('btnLogin').disabled = true;
  socket.emit('do_login', { username: u, password: p });
}

function toggleAuto(enabled) {
  if (!loggedIn) { addLog('err', 'Chưa đăng nhập!'); return; }
  const amt = parseInt(document.getElementById('betAmount').value) || 10000;
  socket.emit('set_autobet', { enabled: enabled, amount: amt });
}

function stopAll() {
  socket.emit('stop');
  loggedIn = false;
  setConn(false);
  document.getElementById('btnLogin').disabled = false;
}

socket.on('connect', () => addLog('info', '🟢 Đã kết nối Web Server'));
socket.on('disconnect', () => { addLog('err', '🔴 Mất kết nối Web Server'); setConn(false); });

socket.on('login_result', (d) => {
  if (d.pending) { addLog('info', d.msg); return; }
  if (!d.ok) {
    addLog('err', '❌ ' + d.error);
    document.getElementById('btnLogin').disabled = false;
    return;
  }
  loggedIn = true;
  document.getElementById('nickname').textContent = d.nickname;
  document.getElementById('balance').textContent = fmt(d.money);
  addLog('ok', `🎉 Đăng nhập thành công: ${d.nickname} | ${fmt(d.money)} Win`);
  setConn(true);
});

socket.on('new_session', (d) => {
  document.getElementById('sessionId').textContent = d.session_id;
  renderPrediction(d.prediction, d.history_len);
});

socket.on('tick', (d) => {
  document.getElementById('gameState').textContent = d.state || '—';
  document.getElementById('timer').textContent = (d.remain != null ? d.remain : '--');
});

socket.on('bet_result', (d) => {
  if (d.balance != null) document.getElementById('balance').textContent = fmt(d.balance);
});

socket.on('session_result', (d) => {
  const emoji = d.result === 'TAI' ? '🔵 TÀI' : d.result === 'XIU' ? '🔴 XỈU' : '⚪';
  let line = `🎲 ${emoji} (${d.sum})`;
  if (d.correct === true) line += ` ✅ Chuẩn! (+${fmt(d.profit_delta)})`;
  else if (d.correct === false) line += ` ❌ Gãy (${fmt(d.profit_delta)})`;
  addLog(d.correct === true ? 'ok' : d.correct === false ? 'err' : 'info', line);
  document.getElementById('pl').textContent = fmt(d.profit_loss);
  renderHistory(d.history);
});

socket.on('autobet_state', (d) => {
  const b = document.getElementById('autoBadge');
  b.className = 'badge ' + (d.enabled ? 'badge-ok' : 'badge-none');
  b.textContent = d.enabled ? `ON (${fmt(d.amount)})` : 'OFF';
  document.getElementById('betAmount').value = d.amount;
});

socket.on('log', (d) => addLog(d.level || 'info', d.msg));
</script>
</body>
</html>
"""

# ==========================================
# ROUTE
# ==========================================
@app.route('/')
def index():
    return render_template_string(HTML)

if __name__ == '__main__':
    print("=" * 50)
    print(f"🤖 TX-MD5-AI | Web UI chạy tại cổng: {PORT}")
    print("=" * 50)
    web_sio.run(app, host='0.0.0.0', port=PORT, debug=False, allow_unsafe_werkzeug=True)
