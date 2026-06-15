"""Flask + flask-sock phone server.

Serves the single-page phone UI, exposes REST control endpoints and a WebSocket
that pushes transcript / answer / provider events. All pushes go through a
thread-safe fan-out so any daemon thread can call server.push(...) safely.

Security: a secret key must be supplied as ?key=XXXX on both the page load and
the WebSocket handshake.
"""

import json
import queue
import socket
import threading
import time

from flask import Flask, Response, jsonify, request

try:
    from flask_sock import Sock
except Exception:  # pragma: no cover
    Sock = None

import config
from logutil import log


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class PhoneServer:
    def __init__(self, key, host=None, port=None):
        self.key = key
        self.host = host or config.SERVER_HOST
        self.port = port or config.SERVER_PORT
        self.app = Flask(__name__)
        self.sock = Sock(self.app) if Sock is not None else None

        self._clients = set()  # set[queue.Queue]
        self._clients_lock = threading.Lock()

        # In-memory state for reconnecting phones.
        self._state_lock = threading.Lock()
        self._provider = "gemini-live"
        self._paused = False
        self._state = "listening"
        self._fallback_ai = config.DEFAULT_FALLBACK_AI
        self._transcript = []  # last 10 chunk dicts
        self._answer = ""
        self._start_time = time.time()

        # Control callbacks wired by the daemon.
        self.on_pause = None
        self.on_resume = None
        self.on_clear = None
        self.on_switch_ai = None
        self.on_switch_provider = None
        self.on_stop = None
        self.get_devices = None

        self._register_routes()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def url(self):
        return f"http://{get_lan_ip()}:{self.port}/?key={self.key}"

    def start(self):
        t = threading.Thread(target=self._serve, daemon=True)
        t.start()
        log(f"server: listening on {self.host}:{self.port}")

    def _serve(self):
        try:
            self.app.run(
                host=self.host,
                port=self.port,
                threaded=True,
                debug=False,
                use_reloader=False,
            )
        except Exception as e:
            log(f"server: failed to run: {e}")

    def push(self, msg):
        """Thread-safe broadcast + state update."""
        self._update_state(msg)
        data = json.dumps(msg)
        with self._clients_lock:
            dead = []
            for q in self._clients:
                try:
                    q.put_nowait(data)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._clients.discard(q)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _update_state(self, msg):
        t = msg.get("type")
        with self._state_lock:
            if t == "chunk":
                self._transcript.append(msg)
                self._transcript = self._transcript[-10:]
            elif t == "ai_start":
                self._answer = ""
            elif t == "ai_chunk":
                self._answer += msg.get("text", "")
            elif t == "provider":
                self._provider = msg.get("name", self._provider)
            elif t == "status":
                self._state = msg.get("state", self._state)
                self._paused = msg.get("state") == "paused"

    def _snapshot(self):
        with self._state_lock:
            msgs = [{"type": "provider", "name": self._provider}]
            msgs.append(
                {
                    "type": "status",
                    "state": "paused" if self._paused else self._state,
                }
            )
            for c in self._transcript:
                msgs.append(c)
            if self._answer:
                msgs.append({"type": "ai_start", "provider": self._provider})
                msgs.append({"type": "ai_chunk", "text": self._answer})
                msgs.append({"type": "ai_done"})
            return msgs

    def set_fallback_ai(self, ai):
        with self._state_lock:
            self._fallback_ai = ai

    def set_paused(self, paused):
        with self._state_lock:
            self._paused = paused
            self._state = "paused" if paused else "listening"

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    def _check_key(self):
        return request.args.get("key") == self.key

    def _register_routes(self):
        app = self.app

        @app.route("/")
        def index():
            if not self._check_key():
                return Response("Unauthorized", status=401)
            return Response(PHONE_HTML, mimetype="text/html")

        @app.route("/pause", methods=["POST"])
        def pause():
            if not self._check_key():
                return Response(status=401)
            self.set_paused(True)
            if self.on_pause:
                self.on_pause()
            self.push({"type": "status", "state": "paused"})
            return jsonify(ok=True)

        @app.route("/resume", methods=["POST"])
        def resume():
            if not self._check_key():
                return Response(status=401)
            self.set_paused(False)
            if self.on_resume:
                self.on_resume()
            self.push({"type": "status", "state": "listening"})
            return jsonify(ok=True)

        @app.route("/clear", methods=["POST"])
        def clear():
            if not self._check_key():
                return Response(status=401)
            with self._state_lock:
                self._answer = ""
            if self.on_clear:
                self.on_clear()
            self.push({"type": "clear"})
            return jsonify(ok=True)

        @app.route("/switch-ai", methods=["POST"])
        def switch_ai():
            if not self._check_key():
                return Response(status=401)
            ai = (request.get_json(silent=True) or {}).get("ai", "groq")
            self.set_fallback_ai(ai)
            if self.on_switch_ai:
                self.on_switch_ai(ai)
            self.push({"type": "ai_provider", "ai": ai})
            return jsonify(ok=True, ai=ai)

        @app.route("/switch-provider", methods=["POST"])
        def switch_provider():
            if not self._check_key():
                return Response(status=401)
            prov = (request.get_json(silent=True) or {}).get("provider", "gemini")
            if self.on_switch_provider:
                self.on_switch_provider(prov)
            return jsonify(ok=True, provider=prov)

        @app.route("/stop", methods=["POST"])
        def stop():
            if not self._check_key():
                return Response(status=401)
            if self.on_stop:
                threading.Thread(target=self.on_stop, daemon=True).start()
            return jsonify(ok=True)

        @app.route("/status")
        def status():
            if not self._check_key():
                return Response(status=401)
            with self._state_lock:
                return jsonify(
                    provider=self._provider,
                    paused=self._paused,
                    uptime=int(time.time() - self._start_time),
                    fallback_ai=self._fallback_ai,
                )

        @app.route("/devices")
        def devices():
            if not self._check_key():
                return Response(status=401)
            devs = self.get_devices() if self.get_devices else []
            return jsonify(devices=devs)

        if self.sock is not None:

            @self.sock.route("/ws")
            def ws_handler(ws):
                key = None
                try:
                    key = request.args.get("key")
                except Exception:
                    key = None
                if key != self.key:
                    try:
                        ws.send(json.dumps({"type": "error", "message": "unauthorized"}))
                    except Exception:
                        pass
                    return

                q = queue.Queue(maxsize=1000)
                with self._clients_lock:
                    self._clients.add(q)

                # Send snapshot so the phone never shows a blank screen.
                try:
                    for m in self._snapshot():
                        ws.send(json.dumps(m))
                except Exception:
                    pass

                try:
                    while True:
                        try:
                            data = q.get(timeout=15)
                            ws.send(data)
                        except queue.Empty:
                            ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    pass
                finally:
                    with self._clients_lock:
                        self._clients.discard(q)


PHONE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no" />
<title>Interview Assistant</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; }
  body {
    background: #080810;
    color: #e8e8ff;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    display: flex; flex-direction: column; height: 100vh; overflow: hidden;
  }
  #bar {
    height: 44px; min-height: 44px; background: #0a0a18;
    display: flex; align-items: center; gap: 10px; padding: 0 12px;
    border-bottom: 1px solid #14142a;
  }
  #dot { width: 10px; height: 10px; border-radius: 50%; background: #7a5cff; box-shadow: 0 0 8px #7a5cff; }
  #dot.fallback { background: #ff9a3c; box-shadow: 0 0 8px #ff9a3c; }
  #dot.off { background: #ff3c5c; box-shadow: 0 0 8px #ff3c5c; }
  #pname { font-size: 13px; color: #b8b8e8; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .btn {
    background: #14142a; color: #c8c8f0; border: none; border-radius: 8px;
    height: 30px; min-width: 34px; padding: 0 8px; font-size: 14px;
  }
  .btn:active { background: #20204a; }
  select.btn { height: 30px; }
  select:disabled { opacity: 0.4; }

  #content { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
  #qwrap { padding: 8px 14px; border-bottom: 1px solid #101024; }
  #qlabel { color: #1e1e33; font-size: 10px; text-transform: uppercase; letter-spacing: 2px; }
  #question { color: #3a3aaa; font-size: 15px; margin-top: 2px; }

  #answer {
    flex: 1; overflow-y: auto; padding: 16px 18px;
    color: #e8e8ff; font-family: "Courier New", monospace; font-size: 22px;
    line-height: 1.7; white-space: pre-wrap; word-break: break-word;
    transition: box-shadow 0.2s, border-color 0.2s;
    border: 2px solid transparent;
  }
  #answer.flash { border-color: #3cff8c; box-shadow: inset 0 0 20px rgba(60,255,140,0.1); }

  #footer { border-top: 1px solid #101024; padding: 8px 14px; max-height: 30vh; overflow-y: auto; }
  .you { color: #6a6a9a; font-size: 12px; font-style: italic; margin: 2px 0; }
  #interim { color: #252540; font-size: 12px; font-style: italic; margin-top: 4px; }
</style>
</head>
<body>
  <div id="bar">
    <span id="dot"></span>
    <span id="pname">Connecting...</span>
    <button class="btn" id="pauseBtn">&#10073;&#10073;</button>
    <button class="btn" id="clearBtn">&#9003;</button>
    <select class="btn" id="aiSel" title="Fallback AI">
      <option value="groq">Groq</option>
      <option value="claude">Claude</option>
      <option value="chatgpt">ChatGPT</option>
    </select>
    <button class="btn" id="provBtn" title="Toggle provider">&#8644;</button>
  </div>

  <div id="content">
    <div id="qwrap">
      <div id="qlabel">Interviewer</div>
      <div id="question">Waiting for the interview to start...</div>
    </div>
    <div id="answer"></div>
    <div id="footer">
      <div id="you"></div>
      <div id="interim"></div>
    </div>
  </div>

<script>
(function () {
  const params = new URLSearchParams(location.search);
  const key = params.get("key") || "";
  const dot = document.getElementById("dot");
  const pname = document.getElementById("pname");
  const question = document.getElementById("question");
  const answerEl = document.getElementById("answer");
  const youEl = document.getElementById("you");
  const interimEl = document.getElementById("interim");
  const aiSel = document.getElementById("aiSel");

  let ws = null;
  let provider = "gemini-live";

  function setProvider(name) {
    provider = name;
    dot.classList.remove("fallback", "off");
    if (name === "gemini-live") {
      pname.textContent = "Gemini Live";
      aiSel.disabled = true;
    } else if (name === "deepgram-fallback") {
      pname.textContent = "Deepgram";
      dot.classList.add("fallback");
      aiSel.disabled = false;
    } else {
      pname.textContent = name;
    }
  }

  function post(path, body) {
    return fetch(path + "?key=" + encodeURIComponent(key), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : null,
    }).catch(function () {});
  }

  document.getElementById("pauseBtn").onclick = function () {
    if (this.dataset.paused === "1") {
      post("/resume"); this.dataset.paused = "0"; this.style.opacity = "1";
    } else {
      post("/pause"); this.dataset.paused = "1"; this.style.opacity = "0.5";
    }
  };
  document.getElementById("clearBtn").onclick = function () {
    post("/clear"); answerEl.textContent = "";
  };
  document.getElementById("provBtn").onclick = function () {
    const target = provider === "gemini-live" ? "fallback" : "gemini";
    post("/switch-provider", { provider: target });
  };
  aiSel.onchange = function () { post("/switch-ai", { ai: this.value }); };

  function atBottom() {
    return answerEl.scrollHeight - answerEl.scrollTop - answerEl.clientHeight < 60;
  }

  function handle(msg) {
    switch (msg.type) {
      case "provider": setProvider(msg.name); break;
      case "status":
        if (msg.state === "paused") { dot.classList.add("off"); }
        else { dot.classList.remove("off"); }
        break;
      case "interim":
        interimEl.textContent = "[" + msg.speaker + "]: " + msg.text;
        break;
      case "chunk":
        if (msg.speaker === "INTERVIEWER") {
          question.textContent = msg.text;
          interimEl.textContent = "";
        } else {
          youEl.textContent = "YOU: " + msg.text;
        }
        break;
      case "ai_start":
        answerEl.textContent = "";
        break;
      case "ai_chunk": {
        const stick = atBottom();
        answerEl.textContent += msg.text;
        if (stick) answerEl.scrollTop = answerEl.scrollHeight;
        break;
      }
      case "ai_done":
        answerEl.classList.add("flash");
        setTimeout(function () { answerEl.classList.remove("flash"); }, 600);
        break;
      case "clear":
        answerEl.textContent = "";
        break;
      case "ai_provider":
        aiSel.value = msg.ai;
        break;
      case "error":
        console.warn("error", msg.message);
        break;
      case "ping": break;
    }
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(proto + "://" + location.host + "/ws?key=" + encodeURIComponent(key));
    ws.onopen = function () { dot.classList.remove("off"); };
    ws.onmessage = function (e) {
      try { handle(JSON.parse(e.data)); } catch (err) {}
    };
    ws.onclose = function () {
      dot.classList.add("off");
      pname.textContent = "Reconnecting...";
      setTimeout(connect, 2000);
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }

  async function wakeLock() {
    try {
      if ("wakeLock" in navigator) {
        let lock = await navigator.wakeLock.request("screen");
        document.addEventListener("visibilitychange", async function () {
          if (document.visibilityState === "visible") {
            try { lock = await navigator.wakeLock.request("screen"); } catch (e) {}
          }
        });
      }
    } catch (e) {}
  }

  connect();
  wakeLock();
})();
</script>
</body>
</html>
"""
