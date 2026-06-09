import urllib.request
import urllib.error
import json
import ssl
import time
import base64
import hashlib
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

BASE    = 'https://taskitos.cupiditys.lol'
OCP_KEY = 'd701a2043aa24d7ebb37e9adf60d043b'
UA      = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36'

# ─── HTTP ────────────────────────────────────────────────────────────────────

def req(url, method='GET', data=None, headers={}, cookies={}):
    body = json.dumps(data).encode() if data else None
    h = dict(headers)
    if cookies:
        h['cookie'] = '; '.join(f'{k}={v}' for k, v in cookies.items())
    r = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, context=ctx, timeout=30) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        try:    return e.code, json.loads(e.read())
        except: return e.code, {}

def headers_auth(token, captcha=None):
    h = {
        'accept': '*/*',
        'accept-language': 'pt-BR,pt;q=0.9,en;q=0.8',
        'content-type': 'application/json',
        'x-api-key': token,
        'x-api-platform': 'webclient',
        'x-api-realm': 'edusp',
        'origin': BASE,
        'referer': BASE + '/',
        'user-agent': UA,
    }
    if captcha:
        h['x-captcha-token'] = captcha
    return h

# ─── CAPTCHA ─────────────────────────────────────────────────────────────────

def solve_captcha(cookies={}):
    s, ch = req(f'{BASE}/captcha/challenge',
        headers={'accept':'*/*','origin':BASE,'referer':BASE+'/','user-agent':UA},
        cookies=cookies)
    if s != 200 or not ch.get('challenge'):
        raise Exception(f'captcha challenge falhou: {ch}')
    t0 = time.time()
    n  = 0
    while hashlib.sha256(f'{ch["salt"]}{n}'.encode()).hexdigest() != ch['challenge']:
        n += 1
    took = int((time.time() - t0) * 1000)
    payload = base64.b64encode(json.dumps({
        'algorithm': ch.get('algorithm', 'SHA-256'),
        'challenge': ch['challenge'], 'number': n,
        'salt': ch['salt'], 'signature': ch['signature'], 'took': took,
    }, separators=(',',':')).encode()).decode()
    s2, v = req(f'{BASE}/captcha/verify', method='POST',
        data={'payload': payload},
        headers={'accept':'*/*','content-type':'application/json',
                 'origin':BASE,'referer':BASE+'/','user-agent':UA},
        cookies=cookies)
    if not v.get('token'):
        raise Exception(f'captcha verify falhou: {v}')
    return v['token']

# ─── LÓGICA ──────────────────────────────────────────────────────────────────

def do_login(ra, senha, cf=None):
    cookies = {'cf_clearance': cf} if cf else {}
    captcha = solve_captcha(cookies)
    s, d = req(
        f'{BASE}/p/https://sedintegracoes.educacao.sp.gov.br/saladofuturobffapi/credenciais/api/LoginCompletoToken',
        method='POST', data={'user': ra, 'senha': senha},
        headers={
            'accept':'*/*','accept-language':'pt-BR,pt;q=0.9',
            'content-type':'application/json',
            'ocp-apim-subscription-key': OCP_KEY,
            'x-captcha-token': captcha,
            'origin': BASE, 'referer': BASE+'/', 'user-agent': UA,
        },
        cookies=cookies,
    )
    if s != 200 or not d.get('token'):
        raise Exception(d.get('message') or f'Login falhou ({s})')
    sed_token = d['token']
    nome = ''
    escola = ''
    try:
        p = sed_token.split('.')[1]; p += '=' * (4 - len(p) % 4)
        payload_data = json.loads(base64.b64decode(p))
        nome = payload_data.get('NAME', '').title()
        escola = payload_data.get('SCHOOL_NAME', '') or payload_data.get('SCHOOL', '') or 'EE Sala do Futuro'
    except: pass
    for _ in range(5):
        cap2 = solve_captcha(cookies)
        s2, d2 = req(
            f'{BASE}/p/https://edusp-api.ip.tv/registration/edusp/token',
            method='POST', data={'token': sed_token},
            headers={
                'accept':'*/*','accept-language':'pt-BR,pt;q=0.9,en;q=0.8',
                'content-type':'application/json',
                'x-api-platform':'webclient','x-api-realm':'edusp',
                'x-captcha-token': cap2,
                'origin': BASE,'referer': BASE+'/','priority':'u=1, i',
                'user-agent': UA,
            },
            cookies=cookies,
        )
        tok = d2.get('auth_token') or d2.get('token')
        if s2 == 200 and tok:
            return {'token': tok, 'nome': nome, 'escola': escola, 'captcha': cap2}
        time.sleep(2)
    raise Exception('Falha ao trocar token após 5 tentativas')

def do_get_tasks(token, captcha, cf=None):
    cookies = {'cf_clearance': cf} if cf else {}
    s, d = req(f'{BASE}/p/https://edusp-api.ip.tv/room/user',
        headers=headers_auth(token, captcha), cookies=cookies)
    targets = []
    if s == 200:
        for room in d.get('rooms', []):
            v = room.get('name')
            if v and str(v) not in targets: targets.append(str(v))
            for gc in room.get('group_categories', []):
                v2 = gc.get('id')
                if v2 and str(v2) not in targets: targets.append(str(v2))
    def fetch(expired):
        filter_exp = 'false' if expired else 'true'
        url = (f'{BASE}/p/https://edusp-api.ip.tv/tms/task/todo'
               f'?expired_only={str(expired).lower()}&limit=100&offset=0'
               f'&filter_expired={filter_exp}&is_exam=false&with_answer=true&is_essay=false'
               f'&answer_statuses=draft&answer_statuses=pending&with_apply_moment=true')
        for t in targets: url += f'&publication_target={t}'
        s2, d2 = req(url, headers=headers_auth(token, captcha), cookies=cookies)
        if isinstance(d2, list): return d2
        return d2.get('results') or d2.get('tasks') or []
    def fmt(tasks, tipo):
        return [{'id': t.get('id'),
                 'title': t.get('title', f'#{t.get("id")}'),
                 'expire_at': (t.get('expire_at','')[:10] if t.get('expire_at') else '-'),
                 'publication_target': t.get('publication_target',''),
                 'tipo': tipo} for t in tasks]
    return {'pending': fmt(fetch(False), 'pendente'),
            'expired': fmt(fetch(True),  'expirada'),
            'captcha': captcha}

def do_complete_task(token, captcha, task_id, publication_target, wait_sec, cf=None, draft=False):
    cookies = {'cf_clearance': cf} if cf else {}
    cap = solve_captcha(cookies)
    s, lesson = req(
        f'{BASE}/p/https://edusp-api.ip.tv/tms/task/{task_id}/apply/?preview_mode=false&room_code={publication_target}',
        headers=headers_auth(token, cap), cookies=cookies)
    if s not in (200, 304):
        raise Exception(f'apply falhou {s}: {lesson.get("message") or lesson}')
    wait = max(lesson.get('min_execution_time') or 60, wait_sec)
    time.sleep(wait)
    cap2 = solve_captcha(cookies)
    s2, res = req(f'{BASE}/api/complete', method='POST',
        data={
            'x_auth_key': token, 'room_code': publication_target,
            'lesson_id': task_id, 'draft': draft, 'lesson_info': lesson,
            'time_spent': wait, 'answer_id': lesson.get('answer_id') or 0,
            'target_score': 100, 'captchaToken': cap2,
        },
        headers={
            'accept':'*/*','accept-language':'pt-BR,pt;q=0.7',
            'content-type':'application/json',
            'origin': BASE,'referer': BASE+'/','priority':'u=1, i',
            'user-agent': UA,
        },
        cookies=cookies)
    if s2 == 200:
        return {'success': True, 'wait': wait, 'draft': draft}
    raise Exception(f'complete falhou {s2}: {res.get("message") or res.get("error") or res}')

# ─── MODELS ──────────────────────────────────────────────────────────────────

class LoginBody(BaseModel):
    ra: str
    senha: str
    cf: Optional[str] = None
    turnstile_token: Optional[str] = None

class TasksBody(BaseModel):
    token: str
    captcha: str
    cf: Optional[str] = None

class CompleteBody(BaseModel):
    token: str
    captcha: Optional[str] = None
    task_id: int
    publication_target: str = ''
    wait_sec: int = 90
    cf: Optional[str] = None
    draft: bool = False

# ─── ROTAS ───────────────────────────────────────────────────────────────────

TURNSTILE_SECRET = "0x4AAAAAADf8FX1DAuHNy6M-3rohj2wvMvw"

def verify_turnstile(token):
    if not token:
        return False
    try:
        data = json.dumps({"secret": TURNSTILE_SECRET, "response": token}).encode()
        r = urllib.request.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(r, context=ctx, timeout=10) as res:
            return json.loads(res.read()).get("success", False)
    except:
        return False

@app.post('/api/login')
def api_login(body: LoginBody):
    if not body.cf or len(body.cf.strip()) < 50:
        raise HTTPException(status_code=401, detail='RA ou senha inválidos')
    if not verify_turnstile(body.turnstile_token):
        raise HTTPException(status_code=403, detail='Verificação Cloudflare falhou. Recarregue a página.')
    try:
        return do_login(body.ra, body.senha, body.cf)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post('/api/tasks')
def api_tasks(body: TasksBody):
    try:
        return do_get_tasks(body.token, body.captcha, body.cf)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post('/api/complete_task')
def api_complete(body: CompleteBody):
    try:
        return do_complete_task(body.token, body.captcha, body.task_id,
                                body.publication_target, body.wait_sec, body.cf, body.draft)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get('/', response_class=HTMLResponse)
def index():
    return HTML

# ─── FRONTEND ────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NEP Solutions — Sala do Futuro</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: system-ui, sans-serif;
  background: #0d0d0d;
  color: #e0e0e0;
  min-height: 100vh;
}

/* ── TELA DE VERIFICAÇÃO ── */
#cf-screen {
  position: fixed; inset: 0; z-index: 100;
  background: #0d0d0d;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 16px;
}
#cf-screen h1 { font-size: 20px; letter-spacing: 2px; color: #fff; }
#cf-screen.hidden { opacity: 0; pointer-events: none; transition: opacity .5s; }

/* ── LAYOUT ── */
#app { display: none; height: 100vh; }
#app.visible { display: flex; }

aside {
  width: 200px; flex-shrink: 0;
  background: #161616;
  border-right: 1px solid #2a2a2a;
  display: flex; flex-direction: column;
  padding: 20px 0;
}
.logo {
  padding: 0 16px 20px;
  font-size: 13px; font-weight: 700;
  letter-spacing: 1px; color: #fff;
  border-bottom: 1px solid #2a2a2a;
}
.logo span { color: #e53935; }

nav { flex: 1; padding: 12px 0; }
.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 16px;
  font-size: 13px; color: #999;
  cursor: pointer; transition: all .15s;
  border-left: 2px solid transparent;
}
.nav-item:hover { color: #fff; background: #1e1e1e; }
.nav-item.active { color: #fff; border-left-color: #e53935; background: #1e1e1e; }
.badge {
  margin-left: auto; background: #e53935;
  color: #fff; font-size: 10px; font-weight: 700;
  padding: 1px 6px; border-radius: 10px;
}
.soon-tag {
  margin-left: auto; font-size: 9px; color: #555;
  letter-spacing: 1px;
}

.sidebar-info {
  padding: 14px 16px;
  border-top: 1px solid #2a2a2a;
  font-size: 11px; color: #555;
}
#sidebar-nome { color: #999; font-size: 12px; margin-bottom: 2px; }

/* ── MAIN ── */
main {
  flex: 1; overflow-y: auto;
  display: flex; flex-direction: column;
}
.topbar {
  padding: 12px 24px;
  border-bottom: 1px solid #2a2a2a;
  display: flex; align-items: center; justify-content: space-between;
  font-size: 12px; color: #555;
}
.topbar strong { color: #ccc; }
.status {
  font-size: 11px; padding: 3px 10px;
  border-radius: 20px; border: 1px solid #2a2a2a; color: #555;
}
.status.running { border-color: #e53935; color: #e53935; }

.content { flex: 1; padding: 28px 24px; }

/* ── PÁGINAS ── */
.page { display: none; }
.page.active { display: block; }

/* ── LOGIN ── */
#login-screen {
  position: fixed; inset: 0; z-index: 50;
  background: #0d0d0d;
  display: flex; align-items: center; justify-content: center;
}
#login-screen.hidden { display: none; }
.login-box {
  background: #161616;
  border: 1px solid #2a2a2a;
  border-radius: 12px;
  padding: 36px 32px;
  width: 100%; max-width: 400px;
}
.login-box h2 {
  font-size: 18px; margin-bottom: 6px; color: #fff;
}
.login-box h2 span { color: #e53935; }
.login-box p { font-size: 13px; color: #666; margin-bottom: 24px; }

/* ── FORMULÁRIO ── */
.field { margin-bottom: 14px; }
label { display: block; font-size: 11px; color: #666; margin-bottom: 6px; letter-spacing: .5px; }
input[type=text], input[type=password] {
  width: 100%; background: #1e1e1e;
  border: 1px solid #2a2a2a; border-radius: 8px;
  color: #e0e0e0; font-size: 14px;
  padding: 10px 12px; outline: none; transition: border .2s;
}
input:focus { border-color: #e53935; }
input::placeholder { color: #444; }
.hint { font-size: 10px; color: #555; margin-top: 4px; }
.pw-wrap { position: relative; }
.pw-wrap input { padding-right: 42px; }
.pw-btn {
  position: absolute; right: 10px; top: 50%;
  transform: translateY(-50%);
  background: none; border: none; cursor: pointer; font-size: 16px;
}

/* ── BOTÕES ── */
.btn {
  width: 100%; padding: 11px;
  border: none; border-radius: 8px;
  font-size: 12px; font-weight: 700;
  cursor: pointer; transition: all .2s;
  letter-spacing: 1px; text-transform: uppercase;
  margin-top: 6px;
}
.btn-primary { background: #e53935; color: #fff; }
.btn-primary:hover { background: #c62828; }
.btn-primary:disabled { background: #4a1515; color: #888; cursor: not-allowed; }
.btn-secondary {
  background: transparent; color: #666;
  border: 1px solid #2a2a2a;
}
.btn-secondary:hover { border-color: #555; color: #ccc; }

/* ── CARDS ── */
.card {
  background: #161616;
  border: 1px solid #2a2a2a;
  border-radius: 10px; padding: 20px;
  margin-bottom: 16px;
}
.card-title {
  font-size: 11px; color: #555;
  letter-spacing: 2px; text-transform: uppercase;
  margin-bottom: 16px;
}

/* ── TAREFAS ── */
.task-list { list-style: none; }
.task-item {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; border-radius: 8px;
  cursor: pointer; transition: background .15s;
  border: 1px solid transparent; margin-bottom: 4px;
}
.task-item:hover { background: #1e1e1e; }
.task-item.selected { background: #1e1e1e; border-color: rgba(229,57,53,.3); }
.task-check {
  width: 15px; height: 15px; flex-shrink: 0;
  border: 1px solid #444; border-radius: 4px;
  display: flex; align-items: center; justify-content: center;
}
.task-item.selected .task-check { background: #e53935; border-color: #e53935; }
.task-item.selected .task-check::after { content: '✓'; font-size: 9px; color: #fff; }
.task-name { flex: 1; font-size: 13px; }
.task-type {
  font-size: 9px; padding: 2px 7px; border-radius: 4px;
  letter-spacing: 1px; text-transform: uppercase;
}
.type-p { background: rgba(229,57,53,.1); color: #e53935; }
.type-e { background: rgba(255,100,0,.1); color: #ff6630; }
.task-date { font-size: 10px; color: #555; white-space: nowrap; }

.section-label {
  font-size: 10px; color: #555; letter-spacing: 2px;
  text-transform: uppercase; margin: 16px 0 8px;
}

/* ── OPÇÕES ── */
.opts { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.opt {
  padding: 8px 14px; border-radius: 8px;
  border: 1px solid #2a2a2a; background: transparent;
  color: #666; font-size: 11px; cursor: pointer;
  transition: all .15s;
}
.opt:hover { border-color: #555; color: #ccc; }
.opt.active { border-color: #e53935; color: #e53935; background: rgba(229,57,53,.05); }

/* ── TERMINAL ── */
.terminal {
  background: #111; border-radius: 8px;
  padding: 12px 14px; font-size: 12px;
  font-family: monospace; line-height: 1.7;
  max-height: 260px; overflow-y: auto;
  color: #777; margin-bottom: 14px;
}
.log-ok { color: #4caf50; }
.log-err { color: #e53935; }
.log-info { color: #888; }

/* ── PROGRESS ── */
.prog-wrap { background: #1e1e1e; border-radius: 4px; height: 4px; margin-bottom: 12px; }
.prog-bar { background: #e53935; height: 4px; border-radius: 4px; width: 0; transition: width .3s; }

/* ── RESULTADO ── */
.result-num {
  font-size: 48px; font-weight: 900;
  color: #e53935; text-align: center; margin-bottom: 6px;
}
.result-label { text-align: center; color: #666; font-size: 13px; margin-bottom: 16px; }

/* ── STEPS ── */
.step { display: none; }
.step.active { display: block; }

/* ── STATS MINI ── */
.stats-row { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
.stat-card {
  background: #161616; border: 1px solid #2a2a2a;
  border-radius: 10px; padding: 16px 20px; flex: 1; min-width: 120px;
}
.stat-num { font-size: 28px; font-weight: 900; color: #e53935; }
.stat-lbl { font-size: 11px; color: #555; margin-top: 2px; }

/* ── NOTIFICAÇÕES ── */
#notif-stack {
  position: fixed; top: 16px; right: 16px;
  z-index: 999; display: flex; flex-direction: column; gap: 8px;
}
.notif {
  background: #1e1e1e; border-radius: 8px;
  padding: 10px 14px; font-size: 12px; max-width: 280px;
  animation: nin .25s ease;
}
.notif-ok { border: 1px solid #4caf50; color: #4caf50; }
.notif-err { border: 1px solid #e53935; color: #e53935; }
.notif-warn { border: 1px solid #ff9800; color: #ff9800; }
@keyframes nin { from { opacity:0; transform: translateX(16px); } to { opacity:1; transform: none; } }

/* ── HOME ── */
.welcome { margin-bottom: 20px; }
.welcome h2 { font-size: 20px; color: #fff; margin-bottom: 4px; }
.welcome h2 span { color: #e53935; }
.welcome p { font-size: 13px; color: #555; }

.module-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
.module-card {
  background: #161616; border: 1px solid #2a2a2a;
  border-radius: 10px; padding: 18px;
  cursor: pointer; transition: border .2s;
}
.module-card:hover { border-color: #444; }
.module-card h3 { font-size: 13px; color: #ccc; margin-bottom: 4px; }
.module-card p { font-size: 11px; color: #555; }
.module-card .mod-soon { color: #555; font-size: 10px; margin-top: 8px; letter-spacing: 1px; }
</style>
</head>
<body>

<!-- VERIFICAÇÃO CLOUDFLARE -->
<div id="cf-screen">
  <h1>NEP <span style="color:#e53935">SOLUTIONS</span></h1>
  <p style="color:#555;font-size:13px">Verificando acesso seguro...</p>
  <div id="cf-turnstile-wrap" style="margin-top:8px">
    <div class="cf-turnstile" data-sitekey="0x4AAAAAADf8FSTL21uTKbKu" data-callback="onTurnstileSuccess"></div>
  </div>
</div>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>

<!-- NOTIFICAÇÕES -->
<div id="notif-stack"></div>

<!-- TELA DE LOGIN -->
<div id="login-screen">
  <div class="login-box">
    <h2>SALA DO <span>FUTURO</span></h2>
    <p>Insira suas credenciais CMSP para continuar</p>

    <div class="field">
      <label>RA do Aluno</label>
      <input type="text" id="login-ra" placeholder="ex: 1100000001sp" autocomplete="off">
    </div>
    <div class="field">
      <label>Senha</label>
      <div class="pw-wrap">
        <input type="password" id="login-senha" placeholder="Digite sua senha">
        <button class="pw-btn" onclick="toggleLoginPw()" id="login-pw-btn">👁</button>
      </div>
    </div>
    <div class="field">
      <label>cf_clearance (cookie de segurança)</label>
      <input type="text" id="login-cf" placeholder="Cole o valor aqui...">
      <div class="hint">F12 → Application → Cookies → cf_clearance</div>
    </div>

    <button class="btn btn-primary" id="btn-login" onclick="doLogin()">ENTRAR →</button>
    <div style="margin-top:16px;text-align:center">
      <a href="https://discord.gg/ESVB9598dt" target="_blank" style="color:#555;font-size:12px;text-decoration:none">Discord</a>
    </div>
  </div>
</div>

<!-- APP PRINCIPAL -->
<div id="app">
  <aside>
    <div class="logo">NEP <span>SOLUTIONS</span></div>

    <nav>
      <div class="nav-item active" onclick="navTo('home', this)">🏠 Home</div>
      <div class="nav-item" onclick="navTo('tasks', this)">
        ✅ Tarefa SP
        <span class="badge" id="badge-tasks">0</span>
      </div>
      <div class="nav-item" onclick="navTo('redacao', this)">
        ✏️ Redação
        <span class="soon-tag">SOON</span>
      </div>
      <div class="nav-item" onclick="navTo('provas', this)">
        📄 Provas
        <span class="soon-tag">SOON</span>
      </div>
    </nav>

    <div class="sidebar-info">
      <div id="sidebar-nome">—</div>
      <div id="sidebar-ra" style="font-size:10px;color:#444">RA: —</div>
    </div>
  </aside>

  <main>
    <div class="topbar">
      <strong id="topbar-page">HOME</strong>
      <div class="status" id="status-pill">ONLINE</div>
    </div>

    <div class="content">

      <!-- HOME -->
      <div class="page active" id="page-home">
        <div class="welcome">
          <h2>Olá, <span id="dash-nome">Estudante</span></h2>
          <p id="dash-escola">—</p>
        </div>

        <div class="stats-row">
          <div class="stat-card">
            <div class="stat-num" id="dash-pending">—</div>
            <div class="stat-lbl">Pendentes</div>
          </div>
          <div class="stat-card">
            <div class="stat-num" style="color:#4caf50" id="dash-done">—</div>
            <div class="stat-lbl">Expiradas</div>
          </div>
        </div>

        <div class="module-grid">
          <div class="module-card" onclick="navTo('tasks', document.querySelectorAll('.nav-item')[1])">
            <h3>✅ Tarefa SP</h3>
            <p>Completar atividades automaticamente</p>
          </div>
          <div class="module-card">
            <h3>✏️ Redação Paulista</h3>
            <p>Automação de redações</p>
            <div class="mod-soon">EM BREVE</div>
          </div>
          <div class="module-card">
            <h3>📄 Provas</h3>
            <p>Automatização de provas</p>
            <div class="mod-soon">EM BREVE</div>
          </div>
        </div>
      </div>

      <!-- TASKS -->
      <div class="page" id="page-tasks">

        <!-- Step: credenciais -->
        <div class="step active" id="step-login">
          <div style="max-width:440px">
            <div class="card">
              <div class="card-title">Credenciais</div>
              <div class="field">
                <label>RA do Aluno</label>
                <input type="text" id="ra" placeholder="ex: 1100000001sp" autocomplete="off">
              </div>
              <div class="field">
                <label>Senha</label>
                <div class="pw-wrap">
                  <input type="password" id="senha" placeholder="Digite sua senha" style="padding-right:42px">
                  <button class="pw-btn" onclick="togglePw()" id="pw-toggle">👁</button>
                </div>
              </div>
              <div class="field">
                <label>cf_clearance</label>
                <input type="text" id="cf" placeholder="Cole o cookie aqui...">
                <div class="hint">F12 → Application → Cookies → cf_clearance</div>
              </div>
              <button class="btn btn-primary" id="btn-fetch" onclick="doLogin()">BUSCAR ATIVIDADES →</button>
            </div>
          </div>
        </div>

        <!-- Step: lista -->
        <div class="step" id="step-tasks">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
            <strong style="color:#fff">Atividades encontradas</strong>
            <button class="opt" onclick="selectAll()">Selecionar todas</button>
          </div>

          <div class="card">
            <div id="task-section-pending" style="display:none">
              <div class="section-label">Pendentes</div>
              <ul class="task-list" id="list-pending"></ul>
            </div>
            <div id="task-section-expired" style="display:none">
              <div class="section-label">Expiradas</div>
              <ul class="task-list" id="list-expired"></ul>
            </div>

            <div class="section-label" style="margin-top:20px">Tempo por atividade</div>
            <div class="opts">
              <button class="opt" onclick="setSpeed(60,this)">Mínimo (60s)</button>
              <button class="opt active" onclick="setSpeed(90,this)">Normal (90s)</button>
              <button class="opt" onclick="setSpeed(120,this)">Longo (120s)</button>
            </div>

            <div class="section-label">Modo de envio</div>
            <div class="opts">
              <button class="opt active" id="mode-finalizar" onclick="setMode(false,this)">Finalizar</button>
              <button class="opt" id="mode-rascunho" onclick="setMode(true,this)">Rascunho</button>
            </div>

            <button class="btn btn-primary" onclick="runTasks()" id="btn-run">COMPLETAR SELECIONADAS →</button>
            <button class="btn btn-secondary" onclick="showStep('step-login')" style="margin-top:8px">← Voltar</button>
          </div>
        </div>

        <!-- Step: executando -->
        <div class="step" id="step-running">
          <div class="card">
            <div class="card-title">Executando...</div>
            <div id="running-status" style="font-size:13px;color:#ccc;margin-bottom:12px"></div>
            <div class="prog-wrap"><div class="prog-bar" id="progress"></div></div>
            <div class="terminal" id="log-run"></div>
          </div>
        </div>

        <!-- Step: concluído -->
        <div class="step" id="step-done">
          <div class="card">
            <div class="card-title">Concluído</div>
            <div class="result-num" id="res-count">0/0</div>
            <div class="result-label">atividades processadas</div>
            <div class="terminal" id="log-done"></div>
            <button class="btn btn-primary" onclick="showStep('step-tasks')" style="margin-top:8px">EXECUTAR NOVAMENTE →</button>
            <button class="btn btn-secondary" onclick="navTo('home', document.querySelector('.nav-item'))" style="margin-top:8px">← Voltar ao início</button>
          </div>
        </div>
      </div>

      <!-- REDAÇÃO -->
      <div class="page" id="page-redacao">
        <div style="text-align:center;padding:60px 0;color:#555">
          <div style="font-size:40px;margin-bottom:16px">✏️</div>
          <div style="font-size:14px;letter-spacing:2px">EM BREVE</div>
          <div style="margin-top:8px;font-size:13px">Redação Paulista — disponível em breve</div>
        </div>
      </div>

      <!-- PROVAS -->
      <div class="page" id="page-provas">
        <div style="text-align:center;padding:60px 0;color:#555">
          <div style="font-size:40px;margin-bottom:16px">📄</div>
          <div style="font-size:14px;letter-spacing:2px">EM BREVE</div>
          <div style="margin-top:8px;font-size:13px">Provas — disponível em breve</div>
        </div>
      </div>

    </div><!-- /content -->
  </main>
</div><!-- /app -->

<script>
// ── ESTADO ──
let state = {
  token:'', captcha:'', cf:'',
  nome:'', ra:'', escola:'',
  tasks:[], selected: new Set(),
  waitSec:90, draft:false,
  loggedIn:false,
};

// ── TURNSTILE ──
let turnstileToken = null;
function onTurnstileSuccess(token) {
  turnstileToken = token;
  const s = document.getElementById('cf-screen');
  s.classList.add('hidden');
  setTimeout(() => s.style.display='none', 600);
}

// ── NAVEGAÇÃO ──
let currentPage = 'home';
const pageLabels = { home:'HOME', tasks:'TAREFA SP', redacao:'REDAÇÃO', provas:'PROVAS' };
function navTo(page, el) {
  if (page === currentPage) return;
  document.getElementById('page-'+currentPage).classList.remove('active');
  document.getElementById('page-'+page).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  if (el) el.classList.add('active');
  document.getElementById('topbar-page').textContent = pageLabels[page] || page.toUpperCase();
  currentPage = page;
}

// ── NOTIFICAÇÕES ──
function notify(msg, type='ok', dur=4000) {
  const stack = document.getElementById('notif-stack');
  const d = document.createElement('div');
  d.className = 'notif notif-'+type;
  d.textContent = msg;
  stack.appendChild(d);
  setTimeout(() => { d.style.opacity='0'; d.style.transition='opacity .3s'; setTimeout(()=>d.remove(),300); }, dur);
}

// ── STATUS ──
function setStatus(s) {
  const pill = document.getElementById('status-pill');
  pill.className = 'status' + (s==='running' ? ' running' : '');
  pill.textContent = s==='running' ? 'EXECUTANDO' : 'ONLINE';
}

// ── STEPS ──
function showStep(id) {
  document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

// ── TOGGLE SENHA ──
let loginPwVisible = false;
function toggleLoginPw() {
  loginPwVisible = !loginPwVisible;
  document.getElementById('login-senha').type = loginPwVisible ? 'text' : 'password';
  document.getElementById('login-pw-btn').textContent = loginPwVisible ? '🙈' : '👁';
}
let pwVisible = false;
function togglePw() {
  pwVisible = !pwVisible;
  document.getElementById('senha').type = pwVisible ? 'text' : 'password';
  document.getElementById('pw-toggle').textContent = pwVisible ? '🙈' : '👁';
}

// ── LOGIN ──
async function doLogin() {
  let ra = (document.getElementById('login-ra')||{value:''}).value.trim()
         || (document.getElementById('ra')||{value:''}).value.trim();
  let senha = (document.getElementById('login-senha')||{value:''}).value.trim()
            || (document.getElementById('senha')||{value:''}).value.trim();
  let cf = (document.getElementById('login-cf')||{value:''}).value.trim()
         || (document.getElementById('cf')||{value:''}).value.trim();

  if (!ra || !senha) { notify('Preencha RA e senha!','err'); return; }

  // Sincroniza campos
  if (document.getElementById('ra')) document.getElementById('ra').value = ra;
  if (document.getElementById('senha')) document.getElementById('senha').value = senha;
  if (document.getElementById('cf')) document.getElementById('cf').value = cf;

  const btnL = document.getElementById('btn-login');
  const btnF = document.getElementById('btn-fetch');
  if (btnL) { btnL.disabled=true; btnL.textContent='Aguarde...'; }
  if (btnF) { btnF.disabled=true; btnF.textContent='Aguarde...'; }

  notify('Resolvendo captcha...','warn',8000);
  try {
    const r = await fetch('/api/login', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ ra, senha, cf: cf||null, turnstile_token: turnstileToken })
    });
    const d = await r.json();
    if (!r.ok) {
      notify('Erro: '+(d.detail||r.status),'err');
      if (btnL) { btnL.disabled=false; btnL.textContent='ENTRAR →'; }
      if (btnF) { btnF.disabled=false; btnF.textContent='BUSCAR ATIVIDADES →'; }
      return;
    }
    state.token=d.token; state.captcha=d.captcha;
    state.nome=d.nome||'Estudante'; state.ra=ra;
    state.escola=d.escola||'EE Sala do Futuro'; state.cf=cf;

    // Atualiza UI
    document.getElementById('dash-nome').textContent = state.nome;
    document.getElementById('dash-escola').textContent = state.escola;
    document.getElementById('sidebar-nome').textContent = state.nome;
    document.getElementById('sidebar-ra').textContent = 'RA: '+ra;

    if (!state.loggedIn) {
      state.loggedIn = true;
      document.getElementById('login-screen').classList.add('hidden');
      document.getElementById('app').style.display = 'flex';
    }

    notify('Sessão iniciada ✓','ok');
    notify('Buscando atividades...','warn',5000);
    await fetchTasks();
  } catch(e) {
    notify('Erro: '+e.message,'err');
    if (btnL) { btnL.disabled=false; btnL.textContent='ENTRAR →'; }
    if (btnF) { btnF.disabled=false; btnF.textContent='BUSCAR ATIVIDADES →'; }
  }
}

// ── TAREFAS ──
async function fetchTasks() {
  try {
    const r = await fetch('/api/tasks', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ token:state.token, captcha:state.captcha, cf:state.cf||null })
    });
    const d = await r.json();
    if (!r.ok) { notify('Erro tarefas: '+(d.detail||r.status),'err'); return; }

    state.captcha = d.captcha || state.captcha;
    state.tasks = [...d.pending, ...d.expired];

    renderTasks(d.pending,'list-pending');
    renderTasks(d.expired,'list-expired');

    document.getElementById('task-section-pending').style.display = d.pending.length ? 'block':'none';
    document.getElementById('task-section-expired').style.display = d.expired.length ? 'block':'none';
    document.getElementById('badge-tasks').textContent = state.tasks.length||'0';
    document.getElementById('dash-pending').textContent = d.pending.length;
    document.getElementById('dash-done').textContent = d.expired.length;

    const btnF = document.getElementById('btn-fetch');
    if (btnF) { btnF.disabled=false; btnF.textContent='BUSCAR ATIVIDADES →'; }
    const btnL = document.getElementById('btn-login');
    if (btnL) { btnL.disabled=false; btnL.textContent='ENTRAR →'; }

    navTo('tasks', document.querySelectorAll('.nav-item')[1]);
    showStep('step-tasks');
  } catch(e) { notify('Erro: '+e.message,'err'); }
}

function renderTasks(tasks, listId) {
  const ul = document.getElementById(listId);
  ul.innerHTML = '';
  if (!tasks.length) {
    ul.innerHTML = '<li style="color:#555;font-size:12px;text-align:center;padding:16px">Nenhuma atividade</li>';
    return;
  }
  tasks.forEach(t => {
    const li = document.createElement('li');
    li.className = 'task-item'; li.dataset.id = t.id;
    li.innerHTML = `<div class="task-check"></div><div class="task-name">${t.title}</div><span class="task-type ${t.tipo==='pendente'?'type-p':'type-e'}">${t.tipo}</span><div class="task-date">${t.expire_at}</div>`;
    li.addEventListener('click', () => {
      const id = String(t.id);
      if (state.selected.has(id)) { state.selected.delete(id); li.classList.remove('selected'); }
      else { state.selected.add(id); li.classList.add('selected'); }
    });
    ul.appendChild(li);
  });
}

function selectAll() {
  state.tasks.forEach(t => {
    state.selected.add(String(t.id));
    const li = document.querySelector('[data-id="'+t.id+'"]');
    if (li) li.classList.add('selected');
  });
}

// ── CONFIGURAÇÕES ──
function setSpeed(s, b) {
  state.waitSec = s;
  document.querySelectorAll('.opts .opt').forEach(x => x.classList.remove('active'));
  if (b) b.classList.add('active');
}
function setMode(isDraft, b) {
  state.draft = isDraft;
  document.getElementById('mode-finalizar').classList.remove('active');
  document.getElementById('mode-rascunho').classList.remove('active');
  b.classList.add('active');
  document.getElementById('btn-run').textContent = isDraft ? 'SALVAR COMO RASCUNHO →' : 'COMPLETAR SELECIONADAS →';
}

// ── LOG ──
function log(id, msg, cls='') {
  const el = document.getElementById(id);
  const d = document.createElement('div');
  d.className = cls; d.textContent = '> '+msg;
  el.appendChild(d); el.scrollTop = el.scrollHeight;
}

// ── EXECUTAR ──
async function runTasks() {
  if (!state.selected.size) { notify('Selecione pelo menos uma atividade!','err'); return; }
  const toRun = state.tasks.filter(t => state.selected.has(String(t.id)));
  document.getElementById('log-run').innerHTML = '';
  showStep('step-running');
  setStatus('running');
  let ok = 0;
  for (let i=0; i<toRun.length; i++) {
    const t = toRun[i];
    document.getElementById('progress').style.width = Math.round(i/toRun.length*100)+'%';
    document.getElementById('running-status').textContent = '['+(i+1)+'/'+toRun.length+'] '+t.title;
    log('log-run','Iniciando: '+t.title,'log-info');
    try {
      const r = await fetch('/api/complete_task', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          token: state.token, captcha: state.captcha,
          task_id: t.id, publication_target: t.publication_target||'',
          wait_sec: state.waitSec, cf: state.cf||null, draft: state.draft
        })
      });
      const d = await r.json();
      if (r.ok) { ok++; log('log-run','✓ '+t.title+' ('+d.wait+'s)'+(d.draft?' [rascunho]':''),'log-ok'); }
      else { log('log-run','✗ '+t.title+': '+(d.detail||r.status),'log-err'); }
    } catch(e) { log('log-run','✗ Erro: '+e.message,'log-err'); }
  }
  document.getElementById('progress').style.width = '100%';
  document.getElementById('res-count').textContent = ok+'/'+toRun.length;
  document.getElementById('log-done').innerHTML = document.getElementById('log-run').innerHTML;
  setStatus('online');
  showStep('step-done');
}
</script>
</body>
</html>"""
