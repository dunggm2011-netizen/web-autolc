const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const { io: ioClient } = require('socket.io-client');
const crypto = require('crypto');

const PORT = process.env.PORT || 3394;

const app = express();
const server = http.createServer(app);
const io = new Server(server, {
    cors: { origin: "*" }
});

// Quản lý bot chạy ngầm theo username (giữ kết nối 24/7 ngay cả khi đóng tab)
const activeBots = {}; // username -> { gameSio, state, currentSocket }

// ==========================================
// THUẬT TOÁN DỰ ĐOÁN ENSEMBLE AI
// ==========================================
function makePredictionVip(history) {
    if (history.length < 5) {
        return Math.random() < 0.5 ? "TAI" : "XIU";
    }

    let scoreTai = 0;
    let scoreXiu = 0;

    const recent10 = history.slice(-10);
    const tai10 = recent10.filter(x => x === "TAI").length;
    const xiu10 = recent10.filter(x => x === "XIU").length;
    if (tai10 > xiu10) scoreTai += 1;
    else if (xiu10 > tai10) scoreXiu += 1;

    const recent5 = history.slice(-5).map(x => x === "TAI" ? "T" : "X").join("");
    if (recent5.endsWith("TTTT")) scoreTai += 3;
    else if (recent5.endsWith("XXXX")) scoreXiu += 3;
    else if (recent5.endsWith("TXTX")) scoreTai += 2;
    else if (recent5.endsWith("XTXT")) scoreXiu += 2;
    else if (recent5.endsWith("TTXX")) scoreTai += 2;
    else if (recent5.endsWith("XXTT")) scoreXiu += 2;
    else if (recent5.endsWith("TXX")) scoreTai += 1;
    else if (recent5.endsWith("XTT")) scoreXiu += 1;

    if (scoreTai > scoreXiu) return "TAI";
    if (scoreXiu > scoreTai) return "XIU";
    return history[history.length - 1];
}

// ==========================================
// API ĐĂNG NHẬP & XÁC THỰC
// ==========================================
function md5Hash(text) {
    return crypto.createHash('md5').update(text).digest('hex');
}

async function loginAndGetToken(username, password) {
    const pwMd5 = md5Hash(password);
    const url = `https://apifo88daigia.tele68.com/api?c=3&un=${username}&pw=${pwMd5}&cp=R&cl=R&pf=web&at=`;
    try {
        const r = await fetch(url);
        const data = await r.json();
        if (!data.success) {
            return { _error: `Lỗi Game: ${data.message || 'Sai thông tin đăng nhập'}` };
        }

        let sk = data.sessionKey;
        sk += "=".repeat((4 - (sk.length % 4)) % 4);
        const sd = JSON.parse(Buffer.from(sk, 'base64').toString('utf-8'));
        const nickname = sd.nickname || sd.nickName;

        const headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": "https://lc79b.bet",
            "referer": "https://lc79b.bet/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
        };

        const r2 = await fetch("https://wlb.tele68.com/v1/lobby/auth/login?cp=R&cl=R&pf=web&at=", {
            method: "POST",
            headers,
            body: JSON.stringify({ nickName: nickname, accessToken: data.accessToken })
        });
        const lobby = await r2.json();
        const token = lobby.token;
        if (!token) return { _error: "Lobby không trả về JWT token hợp lệ." };

        return {
            token,
            nickname,
            money: lobby.remoteLoginResp?.money || 0,
        };
    } catch (e) {
        return { _error: `Lỗi kết nối server: ${e.message}` };
    }
}

// ==========================================
// QUẢN LÝ BOT CHẠY NGẦM (BACKGROUND WORKER)
// ==========================================
function getOrCreateBot(username, token) {
    if (activeBots[username]) {
        return activeBots[username];
    }

    const st = {
        history: [],
        profitLoss: 0,
        autoBetEnabled: false,
        betAmount: 10000,
        currentPrediction: null,
        waitingForResult: false,
        hasBetThisSession: false,
        sessionId: null,
        balance: 0,
        nickname: username,
        logs: []
    };

    const gameSio = ioClient('https://wtxmd52.tele68.com', {
        path: '/txmd5/',
        transports: ['websocket'],
        auth: { token },
        extraHeaders: {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
            "Origin": "https://lc79b.bet",
            "Referer": "https://lc79b.bet/"
        }
    });

    function pushLog(level, msg) {
        const timeStr = new Date().toLocaleTimeString('vi-VN');
        const logItem = { level, msg: `[${timeStr}] ${msg}` };
        st.logs.push(logItem);
        if (st.logs.length > 150) st.logs.shift();

        if (bot.currentSocket) {
            bot.currentSocket.emit('log', { level, msg });
        }
    }

    gameSio.on('connect', () => {
        pushLog("ok", "🟢 Bot đã kết nối ngầm với máy chủ Game thành công!");
    });

    gameSio.on('disconnect', () => {
        pushLog("err", "🔴 Mất kết nối ngầm tới Game Server! Đang thử lại...");
    });

    gameSio.on('new-session', (data) => {
        st.sessionId = data.id || 'N/A';
        st.hasBetThisSession = false;
        const histLen = st.history.length;

        if (histLen >= 5) {
            st.currentPrediction = makePredictionVip(st.history);
        } else {
            st.currentPrediction = null;
        }

        if (bot.currentSocket) {
            bot.currentSocket.emit('new_session', {
                session_id: st.sessionId,
                prediction: st.currentPrediction,
                history_len: histLen,
            });
        }
    });

    gameSio.on('tick-update', (data) => {
        const gs = data.state;
        if (bot.currentSocket) {
            bot.currentSocket.emit('tick', { state: gs, remain: data.remainTime });
        }

        // TỰ ĐỘNG CƯỢC NGẦM KHI ĐÓNG TAB TRÌNH DUYỆT
        if (gs === 'BETTING' && st.autoBetEnabled && st.currentPrediction) {
            if (!st.hasBetThisSession) {
                gameSio.emit('bet', { type: st.currentPrediction, amount: st.betAmount });
                st.hasBetThisSession = true;
                st.waitingForResult = true;
                pushLog("info", `⏳ [Auto Ngầm] Đã đặt cược ${st.betAmount.toLocaleString()} vào ${st.currentPrediction}`);
            }
        }
    });

    gameSio.on('bet-result', (data) => {
        if (data.postBalance !== undefined) {
            st.balance = data.postBalance;
        }
        if (bot.currentSocket) {
            bot.currentSocket.emit('bet_result', { balance: st.balance });
        }
        pushLog("ok", `✅ Đặt cược thành công. Số dư: ${st.balance.toLocaleString()}`);
        gameSio.emit('get-current-my-info', null);
    });

    gameSio.on('session-result', (data) => {
        const dices = data.dices || [0, 0, 0];
        const result = data.resultTruyenThong || 'N/A';
        const sumDices = dices.reduce((a, b) => a + b, 0);

        if (result === "TAI" || result === "XIU") {
            st.history.push(result);
            if (st.history.length > 100) st.history.shift();
        }

        let correct = null;
        let profitDelta = 0;
        if (st.currentPrediction) {
            if (st.currentPrediction === result) {
                correct = true;
                if (st.waitingForResult) {
                    let win = Math.floor(st.betAmount * 0.98);
                    st.profitLoss += win;
                    profitDelta = win;
                }
            } else {
                correct = false;
                if (st.waitingForResult) {
                    st.profitLoss -= st.betAmount;
                    profitDelta = -st.betAmount;
                }
            }
            st.waitingForResult = false;
        }

        const emoji = result === 'TAI' ? '🔵 TÀI' : result === 'XIU' ? '🔴 XỈU' : '⚪';
        let line = `🎲 ${emoji} (${sumDices})`;
        if (correct === true) line += ` ✅ Chuẩn! (+${profitDelta.toLocaleString()})`;
        else if (correct === false) line += ` ❌ Gãy (${profitDelta.toLocaleString()})`;
        pushLog(correct === true ? 'ok' : correct === false ? 'err' : 'info', line);

        if (bot.currentSocket) {
            bot.currentSocket.emit('session_result', {
                result: result,
                dices: dices,
                sum: sumDices,
                correct: correct,
                profit_delta: profitDelta,
                profit_loss: st.profitLoss,
                history: st.history.slice(-20),
            });
        }
    });

    const bot = {
        gameSio,
        state: st,
        currentSocket: null,
        username
    };

    activeBots[username] = bot;
    return bot;
}

// ==========================================
// WEB SOCKET SERVER CHO GIAO DIỆN BROWSER
// ==========================================
io.on('connection', (socket) => {
    let currentUsername = null;
    console.log(`Browser connected: ${socket.id}`);

    socket.on('do_login', async (data) => {
        const username = (data?.username || "").trim();
        const password = data?.password || "";
        if (!username || !password) {
            socket.emit('login_result', { ok: false, error: "Thiếu tài khoản/mật khẩu" });
            return;
        }

        socket.emit('login_result', { pending: true, msg: "🔄 Đang xác thực với hệ thống..." });
        const result = await loginAndGetToken(username, password);

        if (result._error) {
            socket.emit('login_result', { ok: false, error: result._error });
            return;
        }

        currentUsername = username;
        const bot = getOrCreateBot(username, result.token);
        bot.currentSocket = socket;

        bot.state.balance = result.money;

        socket.emit('login_result', { ok: true, nickname: result.nickname, money: bot.state.balance });
        socket.emit('autobet_state', { enabled: bot.state.autoBetEnabled, amount: bot.state.betAmount });
        
        // Gửi lại nhật ký log và số dư hiện tại
        bot.state.logs.slice(-50).forEach(l => socket.emit('log', l));
    });

    socket.on('set_autobet', (data) => {
        if (!currentUsername || !activeBots[currentUsername]) {
            socket.emit('log', { level: "err", msg: "Chưa đăng nhập!" });
            return;
        }
        const bot = activeBots[currentUsername];
        const enabled = Boolean(data?.enabled);
        let amount = parseInt(data?.amount) || bot.state.betAmount;
        if (amount <= 0) amount = 10000;

        bot.state.autoBetEnabled = enabled;
        bot.state.betAmount = amount;

        socket.emit('autobet_state', { enabled, amount });
        bot.currentSocket.emit('log', { level: enabled ? "ok" : "info", msg: `${enabled ? '✅ ĐÃ BẬT' : '🔴 ĐÃ TẮT'} Auto Bet Ngầm @ ${amount.toLocaleString()}` });
    });

    socket.on('stop', () => {
        if (currentUsername && activeBots[currentUsername]) {
            try {
                activeBots[currentUsername].gameSio.disconnect();
            } catch (e) {}
            delete activeBots[currentUsername];
            socket.emit('log', { level: "info", msg: "🔌 Đã ngắt hoàn toàn Bot ngầm." });
        }
    });

    socket.on('disconnect', () => {
        if (currentUsername && activeBots[currentUsername]) {
            activeBots[currentUsername].currentSocket = null;
            console.log(`Trình duyệt của ${currentUsername} đã đóng, bot vẫn chạy ngầm 24/7.`);
        }
    });
});

// ==========================================
// GIAO DIỆN HTML WEB UI
// ==========================================
const HTML = `
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<title>TX MD5 - AI Auto Bet (24/7 Background)</title>
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
  <h1>🤖 TX MD5 — AI AUTO BET (CHẠY NGẦM 24/7)</h1>
  <div class="grid">
    <div class="card">
      <h2>🔐 Đăng nhập</h2>
      <input id="username" placeholder="Tên đăng nhập" autocomplete="off">
      <input id="password" type="password" placeholder="Mật khẩu" autocomplete="off">
      <button class="btn-primary" id="btnLogin" onclick="doLogin()">ĐĂNG NHẬP & KÍCH HOẠT BOT</button>
      <div style="margin-top:12px">
        <div class="stat"><span class="label">Nhân vật</span><span class="value" id="nickname">—</span></div>
        <div class="stat"><span class="label">Số dư</span><span class="value" id="balance">0</span></div>
        <div class="stat"><span class="label">Trạng thái</span><span class="value"><span id="connBadge" class="badge badge-none">OFFLINE</span></span></div>
      </div>
    </div>

    <div class="card">
      <h2>⚡ Auto Bet Ngầm</h2>
      <input id="betAmount" type="number" value="10000" min="1" placeholder="Mức cược">
      <div class="btn-row">
        <button class="btn-green" onclick="toggleAuto(true)">BẬT AUTO</button>
        <button class="btn-red" onclick="toggleAuto(false)">TẮT AUTO</button>
        <button class="btn-red" onclick="stopAll()" style="background:#64748b">DỪNG BOT</button>
      </div>
      <div style="margin-top:12px">
        <div class="stat"><span class="label">Trạng thái Auto</span><span class="value"><span id="autoBadge" class="badge badge-none">OFF</span></span></div>
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
      <h2>📜 Log hệ thống 24/7</h2>
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
  div.textContent = msg;
  el.appendChild(div);
  el.scrollTop = el.scrollHeight;
  while (el.children.length > 250) el.removeChild(el.firstChild);
}

function setConn(ok) {
  const b = document.getElementById('connBadge');
  b.className = 'badge ' + (ok ? 'badge-ok' : 'badge-none');
  b.textContent = ok ? 'ONLINE (24/7)' : 'OFFLINE';
}

function renderPrediction(pred, histLen) {
  const box = document.getElementById('predBox');
  if (!pred) {
    box.className = 'pred-box pred-none';
    box.textContent = \`Chưa đủ dữ liệu (\${histLen}/5)\`;
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
  addLog('ok', \`🎉 Đăng nhập thành công bot ngầm cho: \${d.nickname}\`);
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
  document.getElementById('pl').textContent = fmt(d.profit_loss);
  renderHistory(d.history);
});

socket.on('autobet_state', (d) => {
  const b = document.getElementById('autoBadge');
  b.className = 'badge ' + (d.enabled ? 'badge-ok' : 'badge-none');
  b.textContent = d.enabled ? \`ON (\${fmt(d.amount)})\` : 'OFF';
  document.getElementById('betAmount').value = d.amount;
});

socket.on('log', (d) => addLog(d.level || 'info', d.msg));
</script>
</body>
</html>
`;

app.get('/', (req, res) => {
    res.send(HTML);
});

server.listen(PORT, '0.0.0.0', () => {
    console.log("==================================================");
    console.log(`🤖 TX-MD5-AI 24/7 (Node.js) | Web UI chạy tại cổng: ${PORT}`);
    console.log("==================================================");
});
