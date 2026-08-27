On PC 2 (192.168.1.102), run this to configure Ollama to accept connections from other machines:

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
  ──────
  ## Fix 3: Verify from PC 1 that PC 2's Ollama is reachable

  After fixing PC 2, test from PC 1's terminal:

    # Should return JSON with model list
    curl http://192.168.1.102:11434/api/tags

    # Also verify PC 1's own local Ollama is reachable
    curl http://192.168.1.101:11434/api/tags

────────────────────────────────────────────────────────────

  ## Fix for PC 2 (Windows) — Make Ollama listen on LAN

  ### Option A: Set it permanently via System Environment Variables (Recommended)

  1. Press Win + S, search for "Environment Variables" → click "Edit the system environment variables"
  2. Click "Environment Variables..." button
  3. Under System variables, click New
  4. Set:
      • Variable name: OLLAMA_HOST
      • Variable value: 0.0.0.0:11434
  5. Click OK on all dialogs
  6. Restart the Ollama app (close it from the system tray and reopen)
  7. 
  ──────
  ### Option B: Set it temporarily in PowerShell (for this session only)

  Open PowerShell and run:

    $env:OLLAMA_HOST = "0.0.0.0:11434"
    ollama serve
  ──────
  ### Option C: Set it temporarily in Command Prompt (cmd)

    set OLLAMA_HOST=0.0.0.0:11434
    ollama serve
  ──────
  ### ⚠️ Also check Windows Firewall on PC 2

  Windows Firewall will likely block incoming connections on port 11434. You need to allow it:

  1. Press Win + S, search "Windows Defender Firewall with Advanced Security"
  2. Click Inbound Rules → New Rule...
  3. Select Port → TCP → Specific port: 11434
  4. Select Allow the connection
  5. Apply to Domain, Private, Public (at minimum Private for LAN)
  6. Name it: Ollama LAN

  Or run this in PowerShell as Administrator (one command):

    New-NetFirewallRule -DisplayName "Ollama LAN" -Direction Inbound -Protocol TCP -LocalPort 11434 -Action Allow
  ──────
  ### Verify it works from PC 1 (Linux)

  After the above, test from PC 1's terminal:

    curl http://192.168.1.102:11434/api/tags
  You should get a JSON response with the model list. If you do, the connection is working and your queries should succeed.

# fedora firewall
sudo firewall-cmd --state
sudo firewall-cmd --permanent --add-port=11434/tcp
sudo firewall-cmd --reload
sudo firewall-cmd --list-ports

# test ollama
    curl http://192.168.1.105:11434/api/generate \
      -H "Content-Type: application/json" \
      -d '{
        "model": "llama3.1",
        "prompt": "Why is the sky blue?",
        "stream": false
      }'


# Future plans

* make the send button in the frontend disabled while a request is in progress, to prevent multiple requests being sent at once
* add a "stop" button to cancel a request in progress
* add a "clear conversation" button to reset the chat history
* add a "delete conversation" button to remove a conversation from the list
* add a "rename conversation" button to change the name of a conversation
* add a "copy message" button to copy a message to the clipboard

* Make the ai output not markdown, but instead a rich text output, so that it can be formatted and styled better
