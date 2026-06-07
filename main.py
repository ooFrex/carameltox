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

# ─── ROTAS API ───────────────────────────────────────────────────────────────

TURNSTILE_SECRET = "0x4AAAAAADf8FX1DAuHNy6M-3rohj2wvMvw"

def verify_turnstile(token):
    if not token:
        return False
    try:
        data = json.dumps({"secret": TURNSTILE_SECRET, "response": token}).encode()
        r = urllib.request.Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(r, context=ctx, timeout=10) as res:
            result = json.loads(res.read())
            return result.get("success", False)
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

# ─── FRONTEND ────────────────────────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse)
def index():
    return HTML


HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NEP Solutions — Sala do Futuro</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;700;900&family=Exo+2:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#03000a;
  --bg2:#07030f;
  --surface:#0d0720;
  --border:#1a0d35;
  --red:#e8003d;
  --red2:#ff1a55;
  --red3:#ff4d7a;
  --redglow:rgba(232,0,61,0.4);
  --redglow2:rgba(232,0,61,0.15);
  --text:#e8e0f5;
  --muted:#6a5a8a;
  --card:#0a0618;
  --cyan:#00ffe5;
  --cyanglow:rgba(0,255,229,0.2);
}
html,body{height:100%;overflow:hidden}
body{
  background:var(--bg);
  font-family:'Exo 2',sans-serif;
  color:var(--text);
  display:flex;
  overflow:hidden;
}

/* ── SCANLINE OVERLAY ── */
body::before{
  content:'';
  position:fixed;
  inset:0;
  background:repeating-linear-gradient(
    0deg,
    transparent,
    transparent 2px,
    rgba(0,0,0,0.08) 2px,
    rgba(0,0,0,0.08) 4px
  );
  pointer-events:none;
  z-index:9998;
}

/* ── PARTICLES ── */
#particles{position:fixed;inset:0;z-index:0;pointer-events:none}

/* ── SIDEBAR ── */
.sidebar{
  width:240px;
  min-width:240px;
  height:100vh;
  background:linear-gradient(180deg,#0a0520 0%,#06030f 100%);
  border-right:1px solid var(--border);
  display:flex;
  flex-direction:column;
  position:relative;
  z-index:100;
  box-shadow:4px 0 30px rgba(232,0,61,0.08);
}
.sidebar::after{
  content:'';
  position:absolute;
  right:0;top:0;bottom:0;width:1px;
  background:linear-gradient(180deg,transparent,var(--red),transparent);
  opacity:0.4;
}

/* ── LOGO ── */
.logo-area{
  padding:28px 20px 24px;
  border-bottom:1px solid var(--border);
}
.logo-title{
  font-family:'Orbitron',monospace;
  font-size:15px;
  font-weight:900;
  letter-spacing:3px;
  color:var(--text);
  line-height:1.2;
}
.logo-title span{color:var(--red);text-shadow:0 0 15px var(--redglow)}
.logo-sub{
  font-size:9px;
  letter-spacing:4px;
  color:var(--muted);
  text-transform:uppercase;
  margin-top:4px;
}

/* ── NAV ── */
.nav{padding:16px 0;flex:1}
.nav-item{
  display:flex;
  align-items:center;
  gap:12px;
  padding:13px 20px;
  cursor:pointer;
  transition:all 0.25s;
  position:relative;
  overflow:hidden;
  border-left:2px solid transparent;
}
.nav-item::before{
  content:'';
  position:absolute;
  left:0;top:0;right:0;bottom:0;
  background:linear-gradient(90deg,var(--redglow2),transparent);
  opacity:0;
  transition:opacity 0.3s;
}
.nav-item:hover::before,.nav-item.active::before{opacity:1}
.nav-item.active{border-left:2px solid var(--red)}
.nav-item.active .nav-icon{color:var(--red);filter:drop-shadow(0 0 6px var(--red))}
.nav-item.active .nav-label{color:var(--text)}
.nav-icon{
  width:18px;height:18px;
  flex-shrink:0;
  color:var(--muted);
  transition:all 0.25s;
}
.nav-label{
  font-size:12px;
  letter-spacing:1.5px;
  text-transform:uppercase;
  color:var(--muted);
  font-weight:500;
  transition:color 0.25s;
}
.nav-item:hover .nav-label{color:var(--text)}
.nav-item:hover .nav-icon{color:var(--red3)}
.nav-badge{
  margin-left:auto;
  background:var(--red);
  color:#fff;
  font-size:9px;
  font-weight:700;
  padding:2px 6px;
  border-radius:10px;
  font-family:'Orbitron',monospace;
  box-shadow:0 0 8px var(--redglow);
}
.nav-soon{
  margin-left:auto;
  font-size:8px;
  letter-spacing:1px;
  color:var(--muted);
  border:1px solid var(--border);
  padding:2px 6px;
  border-radius:4px;
}

/* ── SIDEBAR BOTTOM ── */
.sidebar-bottom{
  padding:16px 20px;
  border-top:1px solid var(--border);
}
.dev-tag{
  font-size:9px;
  letter-spacing:2px;
  color:var(--muted);
  text-transform:uppercase;
  margin-bottom:8px;
}
.dev-name{
  font-size:11px;
  color:var(--red);
  font-family:'Orbitron',monospace;
  font-weight:600;
  letter-spacing:1px;
  text-shadow:0 0 10px var(--redglow);
}

/* ── MAIN ── */
.main{
  flex:1;
  height:100vh;
  display:flex;
  flex-direction:column;
  overflow:hidden;
  position:relative;
  z-index:10;
}

/* ── TOPBAR ── */
.topbar{
  height:60px;
  background:rgba(7,3,15,0.9);
  border-bottom:1px solid var(--border);
  display:flex;
  align-items:center;
  padding:0 28px;
  gap:16px;
  backdrop-filter:blur(10px);
  flex-shrink:0;
}
.topbar-title{
  font-family:'Orbitron',monospace;
  font-size:11px;
  letter-spacing:3px;
  color:var(--muted);
  text-transform:uppercase;
  flex:1;
}
.topbar-title span{color:var(--red);text-shadow:0 0 8px var(--redglow)}

/* ── CONTENT AREA ── */
.content{
  flex:1;
  overflow-y:auto;
  overflow-x:hidden;
  position:relative;
}
.content::-webkit-scrollbar{width:4px}
.content::-webkit-scrollbar-track{background:transparent}
.content::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}

/* ── PAGE TRANSITION ── */
.page{
  position:absolute;
  inset:0;
  padding:28px;
  opacity:0;
  transform:translateX(30px);
  pointer-events:none;
  transition:opacity 0.4s cubic-bezier(.4,0,.2,1), transform 0.4s cubic-bezier(.4,0,.2,1);
  overflow-y:auto;
}
.page.active{
  opacity:1;
  transform:translateX(0);
  pointer-events:all;
  position:relative;
}
.page.exit{
  opacity:0;
  transform:translateX(-30px);
}

/* ── LOGIN PAGE ── */
.login-wrap{
  max-width:420px;
  margin:0 auto;
  padding-top:20px;
}
.login-hero{
  text-align:center;
  margin-bottom:32px;
}
.login-hero h1{
  font-family:'Orbitron',monospace;
  font-size:28px;
  font-weight:900;
  letter-spacing:2px;
  background:linear-gradient(135deg,var(--text),var(--red));
  -webkit-background-clip:text;
  -webkit-text-fill-color:transparent;
  background-clip:text;
}
.login-hero p{
  font-size:12px;
  letter-spacing:2px;
  color:var(--muted);
  margin-top:8px;
  text-transform:uppercase;
}

/* ── CARD ── */
.card{
  background:var(--card);
  border:1px solid var(--border);
  border-radius:12px;
  padding:24px;
  margin-bottom:20px;
  position:relative;
  overflow:hidden;
}
.card::before{
  content:'';
  position:absolute;
  top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--red),transparent);
  opacity:0.4;
}
.card-title{
  font-family:'Orbitron',monospace;
  font-size:10px;
  letter-spacing:3px;
  text-transform:uppercase;
  color:var(--muted);
  margin-bottom:20px;
  display:flex;
  align-items:center;
  gap:8px;
}
.card-title .dot{
  width:6px;height:6px;
  background:var(--red);
  border-radius:50%;
  box-shadow:0 0 6px var(--red);
}

/* ── FORM ── */
.field{margin-bottom:14px}
label{
  display:block;
  font-size:10px;
  letter-spacing:2px;
  color:var(--muted);
  text-transform:uppercase;
  margin-bottom:7px;
}
input[type=text],input[type=password]{
  width:100%;
  background:rgba(3,0,10,0.8);
  border:1px solid var(--border);
  border-radius:8px;
  color:var(--text);
  font-size:13px;
  padding:11px 14px;
  outline:none;
  transition:all 0.2s;
  font-family:'Exo 2',sans-serif;
}
input:focus{
  border-color:var(--red);
  box-shadow:0 0 0 3px var(--redglow2),inset 0 0 15px rgba(232,0,61,0.04);
}
.input-wrap{position:relative}
.input-wrap input{padding-right:42px}
.toggle-pw{
  position:absolute;right:12px;top:50%;
  transform:translateY(-50%);
  background:none;border:none;
  color:var(--muted);cursor:pointer;
  font-size:15px;padding:0;
}
.cf-hint{font-size:10px;color:var(--muted);margin-top:5px;letter-spacing:1px}

/* ── BUTTONS ── */
.btn{
  width:100%;padding:13px;border:none;
  border-radius:8px;font-size:12px;
  font-weight:700;cursor:pointer;
  transition:all 0.2s;margin-top:6px;
  font-family:'Orbitron',monospace;
  letter-spacing:2px;text-transform:uppercase;
}
.btn-primary{
  background:linear-gradient(135deg,var(--red),#b8002e);
  color:#fff;
  box-shadow:0 0 20px var(--redglow2);
}
.btn-primary:hover{
  background:linear-gradient(135deg,var(--red2),var(--red));
  box-shadow:0 0 30px var(--redglow),0 4px 16px rgba(232,0,61,0.3);
  transform:translateY(-1px);
}
.btn-primary:disabled{
  background:rgba(100,0,30,0.4);
  color:#5a2030;
  cursor:not-allowed;
  box-shadow:none;
  transform:none;
}
.btn-secondary{
  background:transparent;
  border:1px solid var(--border);
  color:var(--muted);
}
.btn-secondary:hover{border-color:var(--red);color:var(--red)}

/* ── DASH COMING SOON PAGE ── */
.soon-wrap{
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  height:100%;
  min-height:400px;
  text-align:center;
  gap:16px;
}
.soon-icon{
  font-size:60px;
  filter:grayscale(0.5);
  opacity:0.4;
}
.soon-title{
  font-family:'Orbitron',monospace;
  font-size:14px;
  letter-spacing:4px;
  color:var(--muted);
  text-transform:uppercase;
}
.soon-sub{
  font-size:12px;
  color:#3a2a5a;
  letter-spacing:1px;
}
.soon-line{
  width:60px;height:1px;
  background:linear-gradient(90deg,transparent,var(--red),transparent);
  opacity:0.5;
}

/* ── TASKS PAGE ── */
.tasks-header{
  display:flex;
  align-items:center;
  justify-content:space-between;
  margin-bottom:24px;
}
.tasks-title{
  font-family:'Orbitron',monospace;
  font-size:16px;
  font-weight:700;
  letter-spacing:2px;
}
.tasks-title span{color:var(--red);text-shadow:0 0 10px var(--redglow)}
.select-all-btn{
  font-size:9px;
  font-family:'Orbitron',monospace;
  letter-spacing:2px;
  text-transform:uppercase;
  color:var(--red);
  background:rgba(232,0,61,0.08);
  border:1px solid rgba(232,0,61,0.3);
  border-radius:6px;
  padding:6px 12px;
  cursor:pointer;
  transition:all 0.2s;
}
.select-all-btn:hover{background:rgba(232,0,61,0.15);box-shadow:0 0 10px var(--redglow2)}
.section-label{
  font-size:9px;letter-spacing:3px;
  color:var(--muted);text-transform:uppercase;
  margin:16px 0 10px;
  display:flex;align-items:center;gap:8px;
}
.section-label::after{
  content:'';flex:1;height:1px;
  background:linear-gradient(90deg,var(--border),transparent);
}
.task-list{list-style:none}
.task-item{
  display:flex;align-items:center;gap:12px;
  padding:12px 14px;
  border:1px solid transparent;
  border-radius:8px;
  cursor:pointer;
  transition:all 0.2s;
  margin-bottom:6px;
  background:rgba(10,6,24,0.6);
}
.task-item:hover{
  border-color:var(--border);
  background:rgba(232,0,61,0.04);
}
.task-item.selected{
  border-color:rgba(232,0,61,0.4);
  background:rgba(232,0,61,0.06);
  box-shadow:inset 0 0 20px rgba(232,0,61,0.04);
}
.task-check{
  width:16px;height:16px;
  border:1px solid var(--muted);
  border-radius:4px;
  flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  transition:all 0.2s;
}
.task-item.selected .task-check{
  background:var(--red);
  border-color:var(--red);
  box-shadow:0 0 8px var(--redglow);
}
.task-item.selected .task-check::after{content:'✓';font-size:10px;color:#fff;font-weight:700}
.task-name{flex:1;font-size:12px;letter-spacing:0.5px}
.task-badge{
  font-size:8px;padding:3px 8px;
  border-radius:4px;letter-spacing:1px;
  text-transform:uppercase;
  font-family:'Orbitron',monospace;
}
.badge-pending{
  background:rgba(232,0,61,0.1);
  color:var(--red);
  border:1px solid rgba(232,0,61,0.2);
}
.badge-expired{
  background:rgba(255,100,0,0.1);
  color:#ff6633;
  border:1px solid rgba(255,100,0,0.2);
}
.task-date{font-size:10px;color:var(--muted);white-space:nowrap}

/* ── SPEED GRID ── */
.speed-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.speed-btn{
  padding:10px;border:1px solid var(--border);
  border-radius:6px;background:transparent;
  color:var(--muted);font-size:10px;cursor:pointer;
  text-align:center;transition:all 0.2s;
  font-family:'Exo 2',sans-serif;
}
.speed-btn:hover,.speed-btn.active{
  border-color:var(--red);color:var(--red);
  background:rgba(232,0,61,0.06);
  box-shadow:0 0 10px var(--redglow2);
}
.mode-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px}

/* ── PROGRESS ── */
.progress-bar-wrap{
  background:var(--border);border-radius:4px;
  height:3px;margin-top:14px;overflow:hidden;
}
.progress-bar{
  height:100%;
  background:linear-gradient(90deg,var(--red),var(--red3));
  width:0%;transition:width 0.5s ease;
  box-shadow:0 0 8px var(--redglow);
}

/* ── TERMINAL ── */
.terminal{
  background:#000;
  border:1px solid rgba(232,0,61,0.2);
  border-radius:8px;padding:14px;
  font-size:11px;line-height:1.9;
  max-height:200px;overflow-y:auto;
  margin-top:14px;font-family:'Courier New',monospace;
}
.terminal:empty::before{content:'// sistema aguardando...';color:var(--muted)}
.log-ok{color:#00ff88}.log-warn{color:#ffaa00}
.log-err{color:var(--red)}.log-info{color:#8880ff}

/* ── RESULT ── */
.result-box{
  background:rgba(232,0,61,0.05);
  border:1px solid rgba(232,0,61,0.25);
  border-radius:10px;padding:24px;
  text-align:center;margin-top:14px;
}
.result-num{
  font-family:'Orbitron',monospace;
  font-size:48px;font-weight:900;
  color:var(--red);line-height:1;
  text-shadow:0 0 30px var(--redglow);
}
.result-label{
  font-size:9px;letter-spacing:3px;
  color:var(--muted);text-transform:uppercase;
  margin-top:8px;
}

/* ── WELCOME BANNER ── */
.welcome{
  background:linear-gradient(135deg,rgba(232,0,61,0.06),rgba(100,0,200,0.06));
  border:1px solid rgba(232,0,61,0.2);
  border-radius:8px;padding:12px 16px;
  font-size:12px;color:var(--red3);
  margin-bottom:20px;
  font-family:'Orbitron',monospace;
  letter-spacing:1px;
}

/* ── NOTIF ── */
#notif-stack{
  position:fixed;top:70px;right:16px;
  z-index:9999;display:flex;
  flex-direction:column;gap:8px;
  align-items:flex-end;
}
.notif-item{
  display:flex;align-items:center;gap:10px;
  background:rgba(7,3,15,0.95);
  border:1px solid var(--red);
  border-radius:10px;
  padding:9px 16px 9px 12px;
  font-size:12px;font-weight:600;
  max-width:280px;
  animation:slideIn .3s ease;
  backdrop-filter:blur(10px);
  box-shadow:0 0 20px var(--redglow2);
}
.notif-ok{border-color:#00aa55;color:#00ff88;box-shadow:0 0 15px rgba(0,170,85,0.2)}
.notif-err{border-color:var(--red);color:var(--red3)}
.notif-warn{border-color:#ffaa00;color:#ffaa00;box-shadow:0 0 15px rgba(255,170,0,0.15)}
@keyframes slideIn{from{opacity:0;transform:translateX(24px)}to{opacity:1;transform:translateX(0)}}
@keyframes fadeUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
.empty-msg{
  color:var(--muted);font-size:11px;
  text-align:center;padding:24px;
  letter-spacing:1px;
}

/* ── CF SCREEN ── */
#cf-screen{
  position:fixed;inset:0;
  background:radial-gradient(ellipse at center,#0d0320 0%,#03000a 70%);
  z-index:99999;display:flex;
  flex-direction:column;align-items:center;justify-content:center;
  gap:20px;transition:opacity 0.8s ease;
}
#cf-screen.hidden{opacity:0;pointer-events:none}
.cf-logo{
  font-family:'Orbitron',monospace;
  font-size:13px;letter-spacing:6px;
  color:var(--text);text-transform:uppercase;
}
.cf-logo span{color:var(--red);text-shadow:0 0 15px var(--redglow)}
.cf-spinner{
  width:40px;height:40px;
  border:2px solid rgba(232,0,61,0.15);
  border-top:2px solid var(--red);
  border-radius:50%;
  animation:spin 1s linear infinite;
  box-shadow:0 0 20px var(--redglow2);
}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── STEP MANAGEMENT ── */
.step{display:none}.step.active{display:block;animation:fadeUp 0.4s ease}
</style>
</head>
<body>

<!-- CF Screen -->
<div id="cf-screen">
  <div class="cf-logo">NEP <span>SOLUTIONS</span></div>
  <div class="cf-spinner" id="cf-spinner"></div>
  <div style="font-size:12px;letter-spacing:3px;color:var(--muted);text-transform:uppercase">Verificando acesso</div>
  <div id="cf-turnstile-wrap">
    <div class="cf-turnstile" data-sitekey="0x4AAAAAADf8FSTL21uTKbKu" data-callback="onTurnstileSuccess"></div>
  </div>
</div>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>

<!-- Particles -->
<canvas id="particles"></canvas>

<!-- Notif stack -->
<div id="notif-stack"></div>

<!-- SIDEBAR -->
<nav class="sidebar">
  <div class="logo-area">
    <div class="logo-title">SALA <span>DO</span><br>FUTURO</div>
    <div class="logo-sub">NEP Solutions · CMSP</div>
  </div>

  <div class="nav">
    <div class="nav-item active" data-page="tasks" onclick="navTo('tasks',this)">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/>
      </svg>
      <span class="nav-label">Tarefas</span>
      <span class="nav-badge" id="badge-tasks">0</span>
    </div>

    <div class="nav-item" data-page="redacao" onclick="navTo('redacao',this)">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/>
      </svg>
      <span class="nav-label">Redação Paulista</span>
      <span class="nav-soon">SOON</span>
    </div>

    <div class="nav-item" data-page="provas" onclick="navTo('provas',this)">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/>
      </svg>
      <span class="nav-label">Provas</span>
      <span class="nav-soon">SOON</span>
    </div>

    <div class="nav-item" data-page="plataformas" onclick="navTo('plataformas',this)">
      <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/>
      </svg>
      <span class="nav-label">Plataformas</span>
      <span class="nav-soon">SOON</span>
    </div>
  </div>

  <div class="sidebar-bottom">
    <div class="dev-tag">Desenvolvido por</div>
    <div class="dev-name">richardzs | nep</div>
  </div>
</nav>

<!-- MAIN AREA -->
<div class="main">
  <div class="topbar">
    <div class="topbar-title" id="topbar-label">TAREFAS <span>// NEP SOLUTIONS</span></div>
  </div>

  <div class="content">
    <!-- PAGE: TASKS -->
    <div class="page active" id="page-tasks">
      <!-- STEP: LOGIN -->
      <div class="step active" id="step-login">
        <div class="login-wrap">
          <div class="login-hero">
            <h1>ACESSO</h1>
            <p>Insira suas credenciais CMSP</p>
          </div>
          <div class="card">
            <div class="card-title"><span class="dot"></span>Identificação</div>
            <div class="field">
              <label>RA do Aluno</label>
              <input type="text" id="ra" placeholder="ex: 1100000001sp">
            </div>
            <div class="field">
              <label>Senha</label>
              <div class="input-wrap">
                <input type="password" id="senha" placeholder="Digite sua senha">
                <button class="toggle-pw" onclick="togglePw()" id="pw-toggle">👁</button>
              </div>
            </div>
            <div class="field">
              <label>Código de Segurança</label>
              <input type="text" id="cf" placeholder="Cole o cf_clearance aqui...">
              <div class="cf-hint">→ F12 → Application → Cookies → cf_clearance</div>
            </div>
            <button class="btn btn-primary" id="btn-login" onclick="doLogin()">INICIAR SESSÃO →</button>
          </div>
          <div class="footer-links" style="display:flex;align-items:center;justify-content:center;gap:20px;margin-top:16px;font-size:12px">
            <a href="https://discord.gg/ESVB9598dt" target="_blank" style="color:var(--muted);text-decoration:none;display:flex;align-items:center;gap:6px;transition:color 0.2s" onmouseover="this.style.color='var(--red)'" onmouseout="this.style.color='var(--muted)'">
              <svg width="14" height="12" viewBox="0 0 71 55" fill="currentColor"><path d="M60.1 4.9A58.5 58.5 0 0 0 45.7.7a40 40 0 0 0-1.8 3.6 54.2 54.2 0 0 0-16.2 0A38.3 38.3 0 0 0 26 .7 58.3 58.3 0 0 0 11.5 5C1.7 19.3-1 33.2.3 46.9a58.9 58.9 0 0 0 17.9 9 44.3 44.3 0 0 0 3.8-6.2 38.3 38.3 0 0 1-6-2.9l1.5-1.1a42 42 0 0 0 35.9 0l1.4 1.1a38.5 38.5 0 0 1-6 2.9 44 44 0 0 0 3.8 6.2 58.7 58.7 0 0 0 17.9-9C72 31 67.8 17.2 60.1 4.9ZM23.7 38.3c-3.5 0-6.4-3.2-6.4-7.1s2.8-7.1 6.4-7.1c3.5 0 6.4 3.2 6.3 7.1 0 3.9-2.8 7.1-6.3 7.1Zm23.6 0c-3.5 0-6.4-3.2-6.4-7.1s2.8-7.1 6.4-7.1c3.5 0 6.4 3.2 6.3 7.1 0 3.9-2.8 7.1-6.3 7.1Z"/></svg>
              Discord
            </a>
          </div>
        </div>
      </div>

      <!-- STEP: TASK LIST -->
      <div class="step" id="step-tasks">
        <div class="tasks-header">
          <div class="tasks-title">TAREFAS <span>ATIVAS</span></div>
          <button class="select-all-btn" onclick="selectAll()">SELECIONAR TODAS</button>
        </div>
        <div id="welcome-banner" class="welcome" style="display:none"></div>
        <div class="card">
          <div id="task-section-pending" style="display:none">
            <div class="section-label">📋 Pendentes</div>
            <ul class="task-list" id="list-pending"></ul>
          </div>
          <div id="task-section-expired" style="display:none">
            <div class="section-label">⚠ Expiradas</div>
            <ul class="task-list" id="list-expired"></ul>
          </div>
          <div class="section-label" style="margin-top:20px">Tempo por atividade</div>
          <div class="speed-grid">
            <button class="speed-btn" onclick="setSpeed(60,this)">Mínimo<br><span style="font-size:12px;font-weight:700">60s</span></button>
            <button class="speed-btn active" onclick="setSpeed(90,this)">Normal<br><span style="font-size:12px;font-weight:700">90s</span></button>
            <button class="speed-btn" onclick="setSpeed(120,this)">Longo<br><span style="font-size:12px;font-weight:700">120s</span></button>
          </div>
          <div class="section-label">Modo de envio</div>
          <div class="mode-grid">
            <button class="speed-btn active" id="mode-finalizar" onclick="setMode(false,this)" style="border-color:rgba(232,0,61,0.4);color:var(--red)">Finalizar<br><span style="font-size:10px;font-weight:400;color:var(--muted)">Entrega definitiva</span></button>
            <button class="speed-btn" id="mode-rascunho" onclick="setMode(true,this)">Rascunho<br><span style="font-size:10px;font-weight:400">Em andamento</span></button>
          </div>
          <button class="btn btn-primary" onclick="runTasks()" id="btn-run">COMPLETAR SELECIONADAS →</button>
          <button class="btn btn-secondary" onclick="showStep('step-login')" style="margin-top:8px">← VOLTAR</button>
        </div>
      </div>

      <!-- STEP: RUNNING -->
      <div class="step" id="step-running">
        <div class="card">
          <div class="card-title"><span class="dot"></span>Executando</div>
          <div id="running-status" style="font-family:'Orbitron',monospace;font-size:11px;letter-spacing:1px;color:var(--red);margin-bottom:12px"></div>
          <div class="terminal" id="log-run"></div>
          <div class="progress-bar-wrap"><div class="progress-bar" id="progress"></div></div>
        </div>
      </div>

      <!-- STEP: DONE -->
      <div class="step" id="step-done">
        <div class="card">
          <div class="card-title"><span class="dot"></span>Concluído</div>
          <div class="result-box">
            <div class="result-num" id="res-count">0/0</div>
            <div class="result-label">atividades concluídas</div>
          </div>
          <div class="terminal" id="log-done" style="margin-top:16px"></div>
          <button class="btn btn-primary" onclick="showStep('step-tasks')" style="margin-top:16px">RODAR NOVAMENTE →</button>
        </div>
      </div>
    </div>

    <!-- PAGE: REDAÇÃO -->
    <div class="page" id="page-redacao">
      <div class="soon-wrap">
        <div class="soon-line"></div>
        <div class="soon-icon">✍️</div>
        <div class="soon-title">Redação Paulista</div>
        <div class="soon-sub">Em desenvolvimento — em breve disponível</div>
        <div class="soon-line"></div>
      </div>
    </div>

    <!-- PAGE: PROVAS -->
    <div class="page" id="page-provas">
      <div class="soon-wrap">
        <div class="soon-line"></div>
        <div class="soon-icon">📄</div>
        <div class="soon-title">Provas</div>
        <div class="soon-sub">Em desenvolvimento — em breve disponível</div>
        <div class="soon-line"></div>
      </div>
    </div>

    <!-- PAGE: PLATAFORMAS -->
    <div class="page" id="page-plataformas">
      <div class="soon-wrap">
        <div class="soon-line"></div>
        <div class="soon-icon">🖥️</div>
        <div class="soon-title">Plataformas de Aprendizagem</div>
        <div class="soon-sub">Em desenvolvimento — em breve disponível</div>
        <div class="soon-line"></div>
      </div>
    </div>
  </div>
</div>

<script>
// ── Turnstile ──
let turnstileToken = null;
function onTurnstileSuccess(token){
  turnstileToken = token;
  const s = document.getElementById('cf-screen');
  const sp = document.getElementById('cf-spinner');
  if(sp) sp.style.display='none';
  s.classList.add('hidden');
  setTimeout(()=>s.style.display='none', 900);
}

// ── Particles ──
(function(){
  const c=document.getElementById('particles');
  const ctx=c.getContext('2d');
  let W,H,pts=[];
  function resize(){W=c.width=window.innerWidth;H=c.height=window.innerHeight;}
  resize();window.addEventListener('resize',resize);
  for(let i=0;i<45;i++)pts.push({
    x:Math.random()*1920,y:Math.random()*1080,
    r:Math.random()*1.2+0.2,
    vx:(Math.random()-.5)*0.025,vy:(Math.random()-.5)*0.02,
    o:Math.random()*0.35+0.05
  });
  function draw(){
    ctx.clearRect(0,0,W,H);
    pts.forEach(p=>{
      p.x+=p.vx;p.y+=p.vy;
      if(p.x<0)p.x=W;if(p.x>W)p.x=0;
      if(p.y<0)p.y=H;if(p.y>H)p.y=0;
      ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,Math.PI*2);
      ctx.fillStyle=`rgba(232,0,61,${p.o})`;ctx.fill();
    });
    requestAnimationFrame(draw);
  }
  draw();
})();

// ── Navigation ──
const pageLabels = {
  tasks: 'TAREFAS <span>// NEP SOLUTIONS</span>',
  redacao: 'REDAÇÃO PAULISTA <span>// EM BREVE</span>',
  provas: 'PROVAS <span>// EM BREVE</span>',
  plataformas: 'PLATAFORMAS <span>// EM BREVE</span>',
};
let currentPage = 'tasks';
function navTo(page, el){
  if(page === currentPage) return;
  const oldPage = document.getElementById('page-'+currentPage);
  const newPage = document.getElementById('page-'+page);
  oldPage.classList.add('exit');
  setTimeout(()=>{
    oldPage.classList.remove('active','exit');
    newPage.classList.add('active');
  }, 300);
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('topbar-label').innerHTML = pageLabels[page] || page.toUpperCase();
  currentPage = page;
}

// ── Notifications ──
function notify(msg,type='ok',dur=4000){
  const stack=document.getElementById('notif-stack');
  const d=document.createElement('div');
  d.className='notif-item notif-'+type;
  d.textContent=msg;
  stack.appendChild(d);
  setTimeout(()=>{d.style.transition='opacity .4s';d.style.opacity='0';setTimeout(()=>d.remove(),400);},dur);
}

let state={token:'',captcha:'',cf:'',nome:'',tasks:[],selected:new Set(),waitSec:90,draft:false};
let pwVisible=false;

function togglePw(){
  pwVisible=!pwVisible;
  const i=document.getElementById('senha');
  i.type=pwVisible?'text':'password';
  document.getElementById('pw-toggle').textContent=pwVisible?'🙈':'👁';
}

function showStep(id){
  document.querySelectorAll('.step').forEach(s=>s.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

function log(id,msg,cls=''){
  const el=document.getElementById(id);
  const d=document.createElement('div');
  d.className=cls;
  d.textContent='> '+msg;
  el.appendChild(d);
  el.scrollTop=el.scrollHeight;
}

function setSpeed(s,b){
  state.waitSec=s;
  document.querySelectorAll('.speed-btn').forEach(x=>{if(['60','90','120'].some(v=>x.textContent.includes(v)))x.classList.remove('active')});
  b.classList.add('active');
}

function setMode(isDraft,b){
  state.draft=isDraft;
  document.getElementById('mode-finalizar').classList.remove('active');
  document.getElementById('mode-rascunho').classList.remove('active');
  b.classList.add('active');
  const btn=document.getElementById('btn-run');
  btn.textContent=isDraft?'SALVAR COMO RASCUNHO →':'COMPLETAR SELECIONADAS →';
}

async function doLogin(){
  const ra=document.getElementById('ra').value.trim();
  const senha=document.getElementById('senha').value.trim();
  state.cf=document.getElementById('cf').value.trim();
  if(!ra||!senha){notify('Preencha RA e senha!','err');return;}
  const btn=document.getElementById('btn-login');
  btn.disabled=true;btn.textContent='AGUARDE...';
  notify('Resolvendo captcha...','warn',6000);
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ra,senha,cf:state.cf||null,turnstile_token:turnstileToken})});
    const d=await r.json();
    if(!r.ok){notify('Erro: '+(d.detail||r.status),'err');btn.disabled=false;btn.textContent='INICIAR SESSÃO →';return;}
    state.token=d.token;state.captcha=d.captcha;state.nome=d.nome;
    notify('Logado com sucesso ✓','ok');
    notify('Buscando atividades...','warn',4000);
    await fetchTasks();
  }catch(e){notify('Erro: '+e.message,'err');btn.disabled=false;btn.textContent='INICIAR SESSÃO →';}
}

async function fetchTasks(){
  const r=await fetch('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:state.token,captcha:state.captcha,cf:state.cf||null})});
  const d=await r.json();
  if(!r.ok){notify('Erro tarefas: '+(d.detail||r.status),'err');document.getElementById('btn-login').disabled=false;document.getElementById('btn-login').textContent='INICIAR SESSÃO →';return;}
  state.captcha=d.captcha||state.captcha;
  state.tasks=[...d.pending,...d.expired];
  renderTasks(d.pending,'list-pending');
  renderTasks(d.expired,'list-expired');
  const wb=document.getElementById('welcome-banner');
  if(state.nome){
    wb.style.display='block';
    wb.textContent='BEM-VINDO, '+state.nome.toUpperCase()+' — '+state.tasks.length+' ATIVIDADE(S) ENCONTRADA(S)';
  }
  document.getElementById('task-section-pending').style.display=d.pending.length?'block':'none';
  document.getElementById('task-section-expired').style.display=d.expired.length?'block':'none';
  document.getElementById('badge-tasks').textContent=state.tasks.length||'0';
  showStep('step-tasks');
  document.getElementById('btn-login').disabled=false;
  document.getElementById('btn-login').textContent='INICIAR SESSÃO →';
}

function renderTasks(tasks,listId){
  const ul=document.getElementById(listId);
  ul.innerHTML='';
  if(!tasks.length){ul.innerHTML='<li class="empty-msg">// nenhuma atividade nesta categoria</li>';return;}
  tasks.forEach(t=>{
    const li=document.createElement('li');
    li.className='task-item';li.dataset.id=t.id;
    li.innerHTML=`<div class="task-check"></div><div class="task-name">${t.title}</div><span class="task-badge ${t.tipo==='pendente'?'badge-pending':'badge-expired'}">${t.tipo}</span><div class="task-date">${t.expire_at}</div>`;
    li.addEventListener('click',()=>{
      const id=String(t.id);
      if(state.selected.has(id)){state.selected.delete(id);li.classList.remove('selected');}
      else{state.selected.add(id);li.classList.add('selected');}
    });
    ul.appendChild(li);
  });
}

function selectAll(){
  state.tasks.forEach(t=>{
    state.selected.add(String(t.id));
    const li=document.querySelector('[data-id="'+t.id+'"]');
    if(li)li.classList.add('selected');
  });
}

async function runTasks(){
  if(!state.selected.size){notify('Selecione pelo menos uma atividade!','err');return;}
  const toRun=state.tasks.filter(t=>state.selected.has(String(t.id)));
  document.getElementById('log-run').innerHTML='';
  showStep('step-running');
  let ok=0;
  for(let i=0;i<toRun.length;i++){
    const t=toRun[i];
    document.getElementById('progress').style.width=Math.round(i/toRun.length*100)+'%';
    document.getElementById('running-status').textContent='['+( i+1)+'/'+toRun.length+'] '+t.title;
    log('log-run','Iniciando: '+t.title,'log-info');
    try{
      const r=await fetch('/api/complete_task',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:state.token,captcha:state.captcha,task_id:t.id,publication_target:t.publication_target||'',wait_sec:state.waitSec,cf:state.cf||null,draft:state.draft})});
      const d=await r.json();
      if(r.ok){ok++;log('log-run','✓ '+t.title+' ('+d.wait+'s)'+(d.draft?' [rascunho]':''),'log-ok');}
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
