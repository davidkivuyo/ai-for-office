# Generate token for auth

random bytes -> hex (64 chars) — use any generator

``
openssl rand -hex 32
``

Open 

http://localhost:8000/docs (Swagger)

and

http://localhost:8000/app/

Frontend dev (TanStack + Vite) — optional; proxies /api to :8000

```
npm i
npm run dev
Vite at http://localhost:5173 proxies /api -> http://localhost:8000
```

# Codeql

* make a database after every changes
```
codeql database create python-db --language=python --source-root=. --overwrite
```
* analyze against the code
```
codeql database analyze python-db ~/codeql-repo/python/ql/src/codeql-suites/python-security-and-quality.qls --format=sarif-latest --output=results.sarif
```
> Depend on where you cloned codeql