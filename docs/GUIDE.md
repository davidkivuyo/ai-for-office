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

# Setting ollama

On PC 2 (e.g 192.168.1.101), run this to configure Ollama to accept connections from other machines:

## linux

    # Set OLLAMA_HOST environment variable before starting Ollama
    export OLLAMA_HOST=0.0.0.0:11434
    ollama serve

  Or for a persistent fix (so it survives reboots), create a systemd override on PC 2:

    sudo systemctl edit ollama

  In the editor that opens, add:

    [Service]
    Environment="OLLAMA_HOST=0.0.0.0:11434"

  Then reload and restart:

    sudo systemctl daemon-reload
    sudo systemctl restart ollama

-----
  ## Verify from PC 1 that PC 2's Ollama is reachable

  After fixing PC 2, test from PC 1's terminal:

    # Should return JSON with model list
    curl http://192.168.1.102:11434/api/tags

    # Also verify PC 1's own local Ollama is reachable
    curl http://192.168.1.101:11434/api/tags

------

  ## Windows — Make Ollama listen on LAN

  ### Option A: Set it permanently via System Environment Variables

  1. Press Win + S, search for "Environment Variables" → click "Edit the system environment variables"
  2. Click "Environment Variables..." button
  3. Under System variables, click New
  4. Set:
      • Variable name: OLLAMA_HOST
      • Variable value: 0.0.0.0:11434
  5. Click OK on all dialogs
  6. Restart the Ollama app (close it from the system tray and reopen)
  7. 
 
  ### ⚠️ Also check Windows Firewall

  Windows Firewall will likely block incoming connections on port 11434. You need to allow it:

  1. Press Win + S, search "Windows Defender Firewall with Advanced Security"
  2. Click Inbound Rules → New Rule...
  3. Select Port → TCP → Specific port: 11434
  4. Select Allow the connection
  5. Apply to Domain, Private, Public (at minimum Private for LAN)
  6. Name it: Ollama LAN

  ### Verify it works from PC 1 (Linux)

  After the above, test from PC 1's terminal:

    curl http://192.168.1.102:11434/api/tags
  You should get a JSON response with the model list. If you do, the connection is working and your queries should succeed.

# Test ollama
    curl http://192.168.1.105:11434/api/generate \
      -H "Content-Type: application/json" \
      -d '{
        "model": "llama3.1",
        "prompt": "Why is the sky blue?",
        "stream": false
      }'
