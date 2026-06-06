# Yokitos

NEP Solutions - CMSP - Sala do Futuro

## Deploy no Render

1. Sobe esse repo no GitHub
2. Acessa render.com → New → Web Service
3. Conecta o repo
4. Configura:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Clica Deploy!
