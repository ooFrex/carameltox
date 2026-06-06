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

HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CaraMeltoX — NEP Solutions</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0f;--surface:#111118;--border:#1e1e2e;--green:#39ff14;--green-dim:#1a7a00;--cyan:#00f5ff;--yellow:#ffe600;--red:#ff3b3b;--text:#e8e8f0;--muted:#555570}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Space Mono',monospace;min-height:100vh;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.08) 2px,rgba(0,0,0,.08) 4px);pointer-events:none;z-index:9999}
.grid-bg{position:fixed;inset:0;background-image:linear-gradient(rgba(57,255,20,.03) 1px,transparent 1px),linear-gradient(90deg,rgba(57,255,20,.03) 1px,transparent 1px);background-size:40px 40px;pointer-events:none}
.container{max-width:680px;margin:0 auto;padding:40px 20px 80px;position:relative;z-index:1}
.header{text-align:center;margin-bottom:48px;animation:fadeDown .6s ease}
.dino-art{color:var(--green);font-size:11px;line-height:1.4;display:block;margin-bottom:16px;opacity:.8}
.logo{font-family:'Syne',sans-serif;font-size:52px;font-weight:800;letter-spacing:-2px;background:linear-gradient(135deg,var(--green),var(--cyan));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;line-height:1}
.tagline{color:var(--muted);font-size:11px;letter-spacing:3px;text-transform:uppercase;margin-top:8px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:28px;margin-bottom:20px;position:relative;animation:fadeUp .5s ease both}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--green),var(--cyan),transparent)}
.card-title{font-family:'Syne',sans-serif;font-size:12px;letter-spacing:3px;text-transform:uppercase;color:var(--green);margin-bottom:20px;display:flex;align-items:center;gap:8px}
.card-title::before{content:'>';color:var(--cyan)}
.field{margin-bottom:16px}
label{display:block;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
input[type=text],input[type=password]{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:2px;color:var(--text);font-family:'Space Mono',monospace;font-size:13px;padding:10px 14px;outline:none;transition:border-color .2s}
input:focus{border-color:var(--green);box-shadow:0 0 0 2px rgba(57,255,20,.08)}
.btn{width:100%;padding:12px;border:none;border-radius:2px;font-family:'Space Mono',monospace;font-size:12px;letter-spacing:2px;text-transform:uppercase;cursor:pointer;transition:all .2s;margin-top:8px}
.btn-primary{background:var(--green);color:#000;font-weight:700}
.btn-primary:hover{background:#50ff25;box-shadow:0 0 20px rgba(57,255,20,.4)}
.btn-primary:disabled{background:var(--green-dim);color:#333;cursor:not-allowed;box-shadow:none}
.btn-secondary{background:transparent;border:1px solid var(--border);color:var(--muted)}
.btn-secondary:hover{border-color:var(--cyan);color:var(--cyan)}
.terminal{background:#000;border:1px solid var(--border);border-radius:2px;padding:16px;font-size:11px;line-height:1.8;max-height:200px;overflow-y:auto;margin-top:16px}
.terminal:empty::before{content:'// aguardando...';color:var(--muted)}
.log-ok{color:var(--green)}.log-warn{color:var(--yellow)}.log-err{color:var(--red)}.log-info{color:var(--cyan)}
.task-list{list-style:none}
.task-item{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s}
.task-item:last-child{border-bottom:none}
.task-item:hover{background:rgba(57,255,20,.03)}
.task-check{width:16px;height:16px;border:1px solid var(--muted);border-radius:2px;flex-shrink:0;display:flex;align-items:center;justify-content:center;transition:all .2s}
.task-item.selected .task-check{background:var(--green);border-color:var(--green)}
.task-item.selected .task-check::after{content:'✓';font-size:10px;color:#000;font-weight:700}
.task-name{flex:1;font-size:12px}
.task-badge{font-size:9px;padding:2px 6px;border-radius:2px;letter-spacing:1px;text-transform:uppercase}
.badge-pending{background:rgba(57,255,20,.1);color:var(--green)}
.badge-expired{background:rgba(255,59,59,.1);color:var(--red)}
.task-date{font-size:10px;color:var(--muted);white-space:nowrap}
.speed-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
.speed-btn{padding:10px;border:1px solid var(--border);border-radius:2px;background:transparent;color:var(--muted);font-family:'Space Mono',monospace;font-size:10px;cursor:pointer;text-align:center;transition:all .2s}
.speed-btn:hover{border-color:var(--green);color:var(--green)}
.speed-btn.active{border-color:var(--green);background:rgba(57,255,20,.08);color:var(--green)}
.progress-bar-wrap{background:var(--border);border-radius:2px;height:4px;margin-top:16px;overflow:hidden}
.progress-bar{height:100%;background:linear-gradient(90deg,var(--green),var(--cyan));width:0%;transition:width .5s ease;box-shadow:0 0 8px var(--green)}
.result-box{background:rgba(57,255,20,.05);border:1px solid var(--green-dim);border-radius:2px;padding:16px;text-align:center;margin-top:16px}
.result-num{font-family:'Syne',sans-serif;font-size:42px;font-weight:800;color:var(--green);line-height:1}
.result-label{font-size:10px;letter-spacing:2px;color:var(--muted);text-transform:uppercase;margin-top:6px}
.step{display:none}.step.active{display:block}
.footer{text-align:center;margin-top:48px;font-size:10px;color:var(--muted);line-height:2}
.footer a{color:var(--cyan);text-decoration:none}.footer a:hover{text-decoration:underline}
@keyframes fadeDown{from{opacity:0;transform:translateY(-20px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.welcome{color:var(--cyan);font-size:13px;margin-bottom:20px;padding:10px 14px;background:rgba(0,245,255,.05);border-left:2px solid var(--cyan);border-radius:0 2px 2px 0}
.select-all-btn{font-size:10px;color:var(--cyan);background:none;border:none;cursor:pointer;letter-spacing:1px;text-transform:uppercase;font-family:'Space Mono',monospace;float:right;padding:0}
.select-all-btn:hover{text-decoration:underline}
.section-label{font-size:10px;color:var(--muted);letter-spacing:2px;text-transform:uppercase;margin:16px 0 8px}
.empty-msg{color:var(--muted);font-size:11px;text-align:center;padding:20px}
</style>
</head>
<body>
<div class="grid-bg"></div>
<div class="container">
  <div class="header">
    <pre class="dino-art">        .~~~~~.
       / o   o \
      (    ^    )
       \  ~~~  /---.
        )     (    |
       / | | | \   /
      (  | | |  )-'
       \_|_|_|_/</pre>
    <div class="logo">CaraMeltoX</div>
    <div class="tagline">NEP Solutions · CMSP · Sala do Futuro</div>
  </div>

  <div class="step active" id="step-login">
    <div class="card">
      <div class="card-title">Acesso</div>
      <div class="field">
        <label>cf_clearance (Cloudflare Cookie)</label>
        <input type="text" id="cf" placeholder="Cole o valor do cf_clearance...">
        <div style="font-size:10px;color:var(--muted);margin-top:4px">Abra taskitos.cupiditys.lol → F12 → Application → Cookies</div>
      </div>
      <div class="field">
        <label>RA + Dígito + SP</label>
        <input type="text" id="ra" placeholder="ex: 1100000001sp">
      </div>
      <div class="field">
        <label>Senha</label>
        <input type="password" id="senha" placeholder="sua senha SED">
      </div>
      <button class="btn btn-primary" id="btn-login" onclick="doLogin()">Entrar →</button>
      <div class="terminal" id="log-login"></div>
    </div>
  </div>

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
      <div class="section-label" style="margin-top:24px">Tempo por atividade</div>
      <div class="speed-grid">
        <button class="speed-btn" onclick="setSpeed(60,this)">Mínimo<br><span style="color:var(--green);font-size:13px;font-weight:700">60s</span></button>
        <button class="speed-btn active" onclick="setSpeed(90,this)">Normal<br><span style="color:var(--green);font-size:13px;font-weight:700">90s</span></button>
        <button class="speed-btn" onclick="setSpeed(120,this)">Longo<br><span style="color:var(--green);font-size:13px;font-weight:700">120s</span></button>
      </div>
      <button class="btn btn-primary" onclick="runTasks()">Completar Selecionadas →</button>
      <button class="btn btn-secondary" onclick="showStep('step-login')" style="margin-top:8px">← Voltar</button>
    </div>
  </div>

  <div class="step" id="step-running">
    <div class="card">
      <div class="card-title">Executando</div>
      <div id="running-status" style="font-size:12px;color:var(--cyan);margin-bottom:12px"></div>
      <div class="terminal" id="log-run"></div>
      <div class="progress-bar-wrap"><div class="progress-bar" id="progress"></div></div>
    </div>
  </div>

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

  <div class="footer">
    creditos: <a href="https://www.instagram.com/__richardzs__/" target="_blank">richardzs</a> · NEP Solutions<br>
    <a href="https://www.tiktok.com/@_yoriichi__0" target="_blank">tiktok</a> · feito com ♥
  </div>
</div>
<script>
let state={token:'',captcha:'',cf:'',nome:'',tasks:[],selected:new Set(),waitSec:90};
function showStep(id){document.querySelectorAll('.step').forEach(s=>s.classList.remove('active'));document.getElementById(id).classList.add('active');}
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
    log('log-login','Login ok! Bem-vindo, '+(d.nome||'aluno'),'log-ok');
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
  if(state.nome){wb.style.display='block';wb.textContent=`Bem-vindo, ${state.nome}! ${state.tasks.length} atividade(s) encontrada(s).`;}
  document.getElementById('task-section-pending').style.display=d.pending.length?'block':'none';
  document.getElementById('task-section-expired').style.display=d.expired.length?'block':'none';
  log('log-login',`Pendentes: ${d.pending.length} | Expiradas: ${d.expired.length}`,'log-ok');
  showStep('step-tasks');
  document.getElementById('btn-login').disabled=false;document.getElementById('btn-login').textContent='Entrar →';
}
function renderTasks(tasks,listId){
  const ul=document.getElementById(listId);ul.innerHTML='';
  if(!tasks.length){ul.innerHTML='<li class="empty-msg">Nenhuma atividade nesta categoria</li>';return;}
  tasks.forEach(t=>{
    const li=document.createElement('li');li.className='task-item';li.dataset.id=t.id;
    li.innerHTML=`<div class="task-check"></div><div class="task-name">${t.title}</div><span class="task-badge ${t.tipo==='pendente'?'badge-pending':'badge-expired'}">${t.tipo}</span><div class="task-date">${t.expire_at}</div>`;
    li.addEventListener('click',()=>{const id=String(t.id);if(state.selected.has(id)){state.selected.delete(id);li.classList.remove('selected');}else{state.selected.add(id);li.classList.add('selected');}});
    ul.appendChild(li);
  });
}
function selectAll(){state.tasks.forEach(t=>{state.selected.add(String(t.id));const li=document.querySelector(`[data-id="${t.id}"]`);if(li)li.classList.add('selected');});}
async function runTasks(){
  if(!state.selected.size){alert('Selecione pelo menos uma atividade!');return;}
  const toRun=state.tasks.filter(t=>state.selected.has(String(t.id)));
  clearLog('log-run');showStep('step-running');
  let ok=0;
  for(let i=0;i<toRun.length;i++){
    const t=toRun[i];
    document.getElementById('progress').style.width=Math.round(i/toRun.length*100)+'%';
    document.getElementById('running-status').textContent=`[${i+1}/${toRun.length}] ${t.title}`;
    log('log-run','Iniciando: '+t.title,'log-info');
    try{
      const r=await fetch('/api/complete_task',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:state.token,captcha:state.captcha,task_id:t.id,publication_target:t.publication_target||'',wait_sec:state.waitSec,cf:state.cf||null})});
      const d=await r.json();
      if(r.ok){ok++;log('log-run',`✓ ${t.title} (${d.wait}s)`,'log-ok');}
      else{log('log-run',`✗ ${t.title}: ${d.detail||r.status}`,'log-err');}
    }catch(e){log('log-run','✗ Erro: '+e.message,'log-err');}
  }
  document.getElementById('progress').style.width='100%';
  document.getElementById('res-count').textContent=`${ok}/${toRun.length}`;
  document.getElementById('log-done').innerHTML=document.getElementById('log-run').innerHTML;
  showStep('step-done');
}
</script>
</body>
</html>"""
