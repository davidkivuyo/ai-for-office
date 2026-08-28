# generate token for auth
# 32 random bytes -> hex (64 chars) — use any generator
openssl rand -hex 32

# Open http://localhost:8000/docs  (Swagger) and http://localhost:8000/app/

# 3. Frontend dev (TanStack + Vite) — optional; proxies /api to :8000
npm i
npm run dev
# Vite at http://localhost:5173 proxies /api -> http://localhost:8000
```
