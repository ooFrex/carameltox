import urllib.request
import urllib.error
import json
import ssl
import time
import base64
import hashlib
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

BASE    = 'https://taskitos.cupiditys.lol'
OCP_KEY = 'd701a2043aa24d7ebb37e9adf60d043b'
UA      = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36'

# ─── HTTP UTILS ──────────────────────────────────────────────────────────────

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
    try:
        p = sed_token.split('.')[1]; p += '=' * (4 - len(p) % 4)
        nome = json.loads(base64.b64decode(p)).get('NAME', '').title()
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
            return {'token': tok, 'nome': nome, 'captcha': cap2}
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
        url = (f'{BASE}/p/https://edusp-api.ip.tv/tms/task/todo'
               f'?expired_only={str(expired).lower()}&limit=100&offset=0'
               f'&filter_expired=true&is_exam=false&with_answer=true&is_essay=false'
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

def do_complete_task(token, captcha, task_id, publication_target, wait_sec, cf=None):
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
            'lesson_id': task_id, 'draft': False, 'lesson_info': lesson,
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
        return {'success': True, 'wait': wait}
    raise Exception(f'complete falhou {s2}: {res.get("message") or res.get("error") or res}')

# ─── MODELS ──────────────────────────────────────────────────────────────────

class LoginBody(BaseModel):
    ra: str
    senha: str
    cf: Optional[str] = None

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

# ─── ROTAS API ───────────────────────────────────────────────────────────────

@app.post('/api/login')
def api_login(body: LoginBody):
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
                                body.publication_target, body.wait_sec, body.cf)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ─── FRONTEND ────────────────────────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse)
def index():
    return HTML


# ─── FRONTEND ────────────────────────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse)
def index():
    return HTML

HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CaraMeltoX — NEP Solutions</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@1,700&family=Inter:wght@400;500;600&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d0814;--surface:#140d1e;--border:#2a1a3e;--red:#e63946;--red2:#ff4d5a;--text:#e8e0f0;--muted:#7a6a8a;--card:#1a1028}
body{background:radial-gradient(ellipse at 30% 20%,#1a0a1e 0%,#0d0814 60%);min-height:100vh;font-family:'Inter',sans-serif;color:var(--text);overflow-x:hidden}
/* Toast */
.toast{position:fixed;top:20px;left:50%;transform:translateX(-50%) translateY(-80px);background:#1a2e1a;border:1px solid #2d5a2d;border-radius:8px;padding:12px 20px;font-size:13px;color:#6fcf6f;display:flex;align-items:center;gap:8px;z-index:99999;transition:transform .4s cubic-bezier(.34,1.56,.64,1);box-shadow:0 4px 20px rgba(0,0,0,.4)}
.toast.show{transform:translateX(-50%) translateY(0)}
/* Modal overlay */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9000;display:none;align-items:center;justify-content:center;backdrop-filter:blur(4px)}
.modal-overlay.show{display:flex}
.modal-box{background:#1a1028;border:1px solid var(--border);border-radius:12px;padding:28px;max-width:340px;width:90%;text-align:center;animation:fadeUp .3s ease}
.modal-box h3{font-size:16px;margin-bottom:10px;color:var(--text)}
.modal-box p{font-size:13px;color:var(--muted);line-height:1.6}
.modal-close{margin-top:20px;padding:10px 28px;background:var(--red);color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600}
/* Owner card */
.owner-card{position:fixed;top:16px;right:16px;z-index:8000;display:flex;align-items:center;gap:10px;background:rgba(20,10,30,.9);border:1px solid var(--red);border-radius:10px;padding:8px 14px 8px 8px;box-shadow:0 0 20px rgba(230,57,70,.2);backdrop-filter:blur(8px)}
.owner-card img{width:38px;height:38px;border-radius:50%;object-fit:cover;border:2px solid var(--red)}
.owner-card span{color:#ff8090;font-size:13px;font-weight:600;white-space:nowrap}
/* Layout */
.container{max-width:480px;margin:0 auto;padding:60px 20px 80px;position:relative;z-index:1}
.header{text-align:center;margin-bottom:40px}
.logo{font-family:'Playfair Display',serif;font-style:italic;font-size:48px;color:var(--text);line-height:1.1}
.logo span{color:var(--red)}
.tagline{font-size:12px;color:var(--muted);margin-top:6px;letter-spacing:1px}
/* Card */
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:28px;margin-bottom:20px;animation:fadeUp .5s ease}
.card-title{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:20px;display:flex;align-items:center;gap:6px}
.card-title-icon{font-size:14px}
.field{margin-bottom:14px}
label{display:block;font-size:11px;letter-spacing:1px;color:var(--muted);margin-bottom:6px;text-transform:uppercase}
input[type=text],input[type=password]{width:100%;background:#0d0814;border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:14px;padding:11px 14px;outline:none;transition:border-color .2s;font-family:'Inter',sans-serif}
input:focus{border-color:var(--red);box-shadow:0 0 0 3px rgba(230,57,70,.1)}
.input-wrap{position:relative}
.input-wrap input{padding-right:40px}
.toggle-pw{position:absolute;right:12px;top:50%;transform:translateY(-50%);background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:0;line-height:1}
.cf-hint{font-size:10px;color:var(--muted);margin-top:5px;letter-spacing:.5px}
.btn{width:100%;padding:13px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:all .2s;margin-top:6px;font-family:'Inter',sans-serif}
.btn-primary{background:var(--red);color:#fff}
.btn-primary:hover{background:var(--red2);box-shadow:0 4px 16px rgba(230,57,70,.35)}
.btn-primary:disabled{background:#4a2028;color:#7a5058;cursor:not-allowed;box-shadow:none}
.btn-secondary{background:transparent;border:1px solid var(--border);color:var(--muted)}
.btn-secondary:hover{border-color:var(--red);color:var(--red)}
/* Footer */
.footer-links{display:flex;align-items:center;justify-content:center;gap:20px;margin-top:32px;font-size:13px}
.footer-links a,.footer-links button{color:var(--muted);text-decoration:none;background:none;border:none;cursor:pointer;font-size:13px;font-family:'Inter',sans-serif;transition:color .2s;display:flex;align-items:center;gap:5px}
.footer-links a:hover,.footer-links button:hover{color:var(--text)}
.dot{color:var(--border);font-size:16px}
.dev-section{text-align:center;margin-top:24px}
.dev-label{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin-bottom:12px}
.dev-avatars{display:flex;align-items:center;justify-content:center;gap:20px}
.dev-item{display:flex;flex-direction:column;align-items:center;gap:6px}
.dev-item img,.dev-avatar{width:44px;height:44px;border-radius:50%;object-fit:cover;border:2px solid var(--border)}
.dev-avatar{background:linear-gradient(135deg,var(--red),#6a0dad);display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;color:#fff}
.dev-name{font-size:11px;color:var(--muted)}
/* Tasks/Steps */
.step{display:none}.step.active{display:block}
.terminal{background:#000;border:1px solid var(--border);border-radius:8px;padding:14px;font-size:11px;line-height:1.8;max-height:180px;overflow-y:auto;margin-top:14px;font-family:monospace}
.terminal:empty::before{content:'// aguardando...';color:var(--muted)}
.log-ok{color:#6fcf6f}.log-warn{color:#f6a623}.log-err{color:var(--red)}.log-info{color:#9b8fd4}
.task-list{list-style:none}
.task-item{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border);cursor:pointer}
.task-item:last-child{border-bottom:none}
.task-item:hover{background:rgba(230,57,70,.04);border-radius:6px;padding-left:4px}
.task-check{width:16px;height:16px;border:1px solid var(--muted);border-radius:4px;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:all .2s}
.task-item.selected .task-check{background:var(--red);border-color:var(--red)}
.task-item.selected .task-check::after{content:'✓';font-size:10px;color:#fff;font-weight:700}
.task-name{flex:1;font-size:12px}
.task-badge{font-size:9px;padding:2px 6px;border-radius:4px;letter-spacing:1px;text-transform:uppercase}
.badge-pending{background:rgba(230,57,70,.12);color:var(--red)}
.badge-expired{background:rgba(255,100,0,.12);color:#ff6633}
.task-date{font-size:10px;color:var(--muted);white-space:nowrap}
.speed-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.speed-btn{padding:10px;border:1px solid var(--border);border-radius:6px;background:transparent;color:var(--muted);font-size:10px;cursor:pointer;text-align:center;transition:all .2s;font-family:'Inter',sans-serif}
.speed-btn:hover,.speed-btn.active{border-color:var(--red);color:var(--red);background:rgba(230,57,70,.07)}
.progress-bar-wrap{background:var(--border);border-radius:4px;height:4px;margin-top:14px;overflow:hidden}
.progress-bar{height:100%;background:linear-gradient(90deg,var(--red),#ff8090);width:0%;transition:width .5s ease}
.result-box{background:rgba(230,57,70,.06);border:1px solid rgba(230,57,70,.3);border-radius:8px;padding:20px;text-align:center;margin-top:14px}
.result-num{font-size:40px;font-weight:700;color:var(--red);line-height:1}
.result-label{font-size:10px;letter-spacing:2px;color:var(--muted);text-transform:uppercase;margin-top:6px}
.select-all-btn{font-size:10px;color:var(--red);background:none;border:none;cursor:pointer;letter-spacing:1px;text-transform:uppercase;float:right;padding:0;font-family:'Inter',sans-serif}
.section-label{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin:14px 0 8px}
.empty-msg{color:var(--muted);font-size:11px;text-align:center;padding:16px}
.welcome{color:#9b8fd4;font-size:13px;margin-bottom:16px;padding:10px 14px;background:rgba(155,143,212,.06);border-left:2px solid #9b8fd4;border-radius:0 6px 6px 0}
@keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeDown{from{opacity:0;transform:translateY(-16px)}to{opacity:1;transform:translateY(0)}}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
</style>
</head>
<body>

<!-- Toast -->
<div class="toast" id="toast">✅ Logado com sucesso</div>

<!-- Modal Doações -->
<div class="modal-overlay" id="modal-doacoes">
  <div class="modal-box">
    <h3>💸 Doações</h3>
    <p>Em breve...</p>
    <button class="modal-close" onclick="document.getElementById('modal-doacoes').classList.remove('show')">Fechar</button>
  </div>
</div>

<!-- Owner card -->
<div class="owner-card">
  <img src="data:image/jpeg;base64,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" alt="dono">
  <span>Najuh volta bby &lt;#</span>
</div>

<div class="container">
  <div class="header">
    <div class="logo">Cara <span>MeltoX</span></div>
    <div class="tagline">Sua plataforma Caramelto</div>
  </div>

  <!-- Step: Login -->
  <div class="step active" id="step-login">
    <div class="card">
      <div class="card-title"><span class="card-title-icon">🔲</span> RA</div>
      <div class="field">
        <label>Número</label>
        <input type="text" id="ra" placeholder="ex: 1100000001sp">
      </div>
      <div class="field">
        <label>🔒 Senha</label>
        <div class="input-wrap">
          <input type="password" id="senha" placeholder="Digite sua senha">
          <button class="toggle-pw" onclick="togglePw()" id="pw-toggle">👁</button>
        </div>
      </div>
      <div class="field">
        <label>CF Clearance</label>
        <input type="text" id="cf" placeholder="Cole o codigo de segurança aqui...">
        <div class="cf-hint">Acesse *********.************.*** → F12 → Application → Cookies</div>
      </div>
      <button class="btn btn-primary" id="btn-login" onclick="doLogin()">Entrar →</button>
      <div class="terminal" id="log-login"></div>
    </div>

    <div class="footer-links">
      <a href="https://discord.gg/ESVB9598dt" target="_blank">
        <svg width="16" height="12" viewBox="0 0 71 55" fill="currentColor"><path d="M60.1 4.9A58.5 58.5 0 0 0 45.7.7a40 40 0 0 0-1.8 3.6 54.2 54.2 0 0 0-16.2 0A38.3 38.3 0 0 0 26 .7 58.3 58.3 0 0 0 11.5 5C1.7 19.3-1 33.2.3 46.9a58.9 58.9 0 0 0 17.9 9 44.3 44.3 0 0 0 3.8-6.2 38.3 38.3 0 0 1-6-2.9l1.5-1.1a42 42 0 0 0 35.9 0l1.4 1.1a38.5 38.5 0 0 1-6 2.9 44 44 0 0 0 3.8 6.2 58.7 58.7 0 0 0 17.9-9C72 31 67.8 17.2 60.1 4.9ZM23.7 38.3c-3.5 0-6.4-3.2-6.4-7.1s2.8-7.1 6.4-7.1c3.5 0 6.4 3.2 6.3 7.1 0 3.9-2.8 7.1-6.3 7.1Zm23.6 0c-3.5 0-6.4-3.2-6.4-7.1s2.8-7.1 6.4-7.1c3.5 0 6.4 3.2 6.3 7.1 0 3.9-2.8 7.1-6.3 7.1Z"/></svg>
        Discord
      </a>
      <span class="dot">·</span>
      <button onclick="document.getElementById('modal-doacoes').classList.add('show')">
        ❤️ Doações
      </button>
    </div>

    <div class="dev-section">
      <div class="dev-label">Desenvolvido por</div>
      <div class="dev-avatars">
        <div class="dev-item">
          <div class="dev-avatar">R</div>
          <div class="dev-name">richardzs | nep</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Step: Tasks -->
  <div class="step" id="step-tasks">
    <div class="card">
      <div class="card-title">Tarefas <button class="select-all-btn" onclick="selectAll()">Selecionar todas</button></div>
      <div id="welcome-banner" class="welcome" style="display:none"></div>
      <div id="task-section-pending">
        <div class="section-label">📋 Pendentes</div>
        <ul class="task-list" id="list-pending"></ul>
      </div>
      <div id="task-section-expired">
        <div class="section-label">⚠ Expiradas</div>
        <ul class="task-list" id="list-expired"></ul>
      </div>
      <div class="section-label" style="margin-top:20px">Tempo por atividade</div>
      <div class="speed-grid">
        <button class="speed-btn" onclick="setSpeed(60,this)">Mínimo<br><span style="font-size:13px;font-weight:700">60s</span></button>
        <button class="speed-btn active" onclick="setSpeed(90,this)">Normal<br><span style="font-size:13px;font-weight:700">90s</span></button>
        <button class="speed-btn" onclick="setSpeed(120,this)">Longo<br><span style="font-size:13px;font-weight:700">120s</span></button>
      </div>
      <button class="btn btn-primary" onclick="runTasks()">Completar Selecionadas →</button>
      <button class="btn btn-secondary" onclick="showStep('step-login')" style="margin-top:8px">← Voltar</button>
    </div>
  </div>

  <!-- Step: Running -->
  <div class="step" id="step-running">
    <div class="card">
      <div class="card-title">Executando</div>
      <div id="running-status" style="font-size:12px;color:var(--red);margin-bottom:12px"></div>
      <div class="terminal" id="log-run"></div>
      <div class="progress-bar-wrap"><div class="progress-bar" id="progress"></div></div>
    </div>
  </div>

  <!-- Step: Done -->
  <div class="step" id="step-done">
    <div class="card">
      <div class="card-title">Concluído</div>
      <div class="result-box">
        <div class="result-num" id="res-count">0/0</div>
        <div class="result-label">atividades concluídas</div>
      </div>
      <div class="terminal" id="log-done" style="margin-top:16px"></div>
      <button class="btn btn-primary" onclick="showStep('step-tasks')" style="margin-top:16px">Rodar novamente</button>
    </div>
  </div>
</div>

<script>
let state={token:'',captcha:'',cf:'',nome:'',tasks:[],selected:new Set(),waitSec:90};
let pwVisible=false;
function togglePw(){pwVisible=!pwVisible;const i=document.getElementById('senha');i.type=pwVisible?'text':'password';document.getElementById('pw-toggle').textContent=pwVisible?'🙈':'👁';}
function showStep(id){document.querySelectorAll('.step').forEach(s=>s.classList.remove('active'));document.getElementById(id).classList.add('active');}
function showToast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),3000);}
function log(id,msg,cls=''){const el=document.getElementById(id);const d=document.createElement('div');d.className=cls;d.textContent='> '+msg;el.appendChild(d);el.scrollTop=el.scrollHeight;}
function clearLog(id){document.getElementById(id).innerHTML='';}
function setSpeed(s,b){state.waitSec=s;document.querySelectorAll('.speed-btn').forEach(x=>x.classList.remove('active'));b.classList.add('active');}
async function doLogin(){
  const ra=document.getElementById('ra').value.trim();
  const senha=document.getElementById('senha').value.trim();
  state.cf=document.getElementById('cf').value.trim();
  if(!ra||!senha){alert('Preencha RA e senha!');return;}
  clearLog('log-login');
  const btn=document.getElementById('btn-login');btn.disabled=true;btn.textContent='Aguarde...';
  log('log-login','Resolvendo captcha e fazendo login...','log-warn');
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ra,senha,cf:state.cf||null})});
    const d=await r.json();
    if(!r.ok){log('log-login','Erro: '+(d.detail||r.status),'log-err');btn.disabled=false;btn.textContent='Entrar →';return;}
    state.token=d.token;state.captcha=d.captcha;state.nome=d.nome;
    showToast('✅ Logado com sucesso');
    log('log-login','Buscando atividades...','log-warn');
    await fetchTasks();
  }catch(e){log('log-login','Erro: '+e.message,'log-err');btn.disabled=false;btn.textContent='Entrar →';}
}
async function fetchTasks(){
  const r=await fetch('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:state.token,captcha:state.captcha,cf:state.cf||null})});
  const d=await r.json();
  if(!r.ok){log('log-login','Erro tarefas: '+(d.detail||r.status),'log-err');document.getElementById('btn-login').disabled=false;document.getElementById('btn-login').textContent='Entrar →';return;}
  state.captcha=d.captcha||state.captcha;
  state.tasks=[...d.pending,...d.expired];
  renderTasks(d.pending,'list-pending');
  renderTasks(d.expired,'list-expired');
  const wb=document.getElementById('welcome-banner');
  if(state.nome){wb.style.display='block';wb.textContent='Bem-vindo, '+state.nome+'! '+state.tasks.length+' atividade(s) encontrada(s).';}
  document.getElementById('task-section-pending').style.display=d.pending.length?'block':'none';
  document.getElementById('task-section-expired').style.display=d.expired.length?'block':'none';
  showStep('step-tasks');
  document.getElementById('btn-login').disabled=false;document.getElementById('btn-login').textContent='Entrar →';
}
function renderTasks(tasks,listId){
  const ul=document.getElementById(listId);ul.innerHTML='';
  if(!tasks.length){ul.innerHTML='<li class="empty-msg">Nenhuma atividade nesta categoria</li>';return;}
  tasks.forEach(t=>{
    const li=document.createElement('li');li.className='task-item';li.dataset.id=t.id;
    li.innerHTML='<div class="task-check"></div><div class="task-name">'+t.title+'</div><span class="task-badge '+(t.tipo==='pendente'?'badge-pending':'badge-expired')+'">'+t.tipo+'</span><div class="task-date">'+t.expire_at+'</div>';
    li.addEventListener('click',()=>{const id=String(t.id);if(state.selected.has(id)){state.selected.delete(id);li.classList.remove('selected');}else{state.selected.add(id);li.classList.add('selected');}});
    ul.appendChild(li);
  });
}
function selectAll(){state.tasks.forEach(t=>{state.selected.add(String(t.id));const li=document.querySelector('[data-id="'+t.id+'"]');if(li)li.classList.add('selected');});}
async function runTasks(){
  if(!state.selected.size){alert('Selecione pelo menos uma atividade!');return;}
  const toRun=state.tasks.filter(t=>state.selected.has(String(t.id)));
  clearLog('log-run');showStep('step-running');
  let ok=0;
  for(let i=0;i<toRun.length;i++){
    const t=toRun[i];
    document.getElementById('progress').style.width=Math.round(i/toRun.length*100)+'%';
    document.getElementById('running-status').textContent='['+(i+1)+'/'+toRun.length+'] '+t.title;
    log('log-run','Iniciando: '+t.title,'log-info');
    try{
      const r=await fetch('/api/complete_task',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:state.token,captcha:state.captcha,task_id:t.id,publication_target:t.publication_target||'',wait_sec:state.waitSec,cf:state.cf||null})});
      const d=await r.json();
      if(r.ok){ok++;log('log-run','✓ '+t.title+' ('+d.wait+'s)','log-ok');}
      else{log('log-run','✗ '+t.title+': '+(d.detail||r.status),'log-err');}
    }catch(e){log('log-run','✗ Erro: '+e.message,'log-err');}
  }
  document.getElementById('progress').style.width='100%';
  document.getElementById('res-count').textContent=ok+'/'+toRun.length;
  document.getElementById('log-done').innerHTML=document.getElementById('log-run').innerHTML;
  showStep('step-done');
}
</script>
</body>
</html>"""
