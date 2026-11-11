# The city Mini App

The bot can open a graphical city inside Telegram: towers that grow a floor with every ✅,
scaffolding on buildings with overdue work, today's tasks with one-tap buttons, and a
form for new tasks. It reads the same database as the bot.

Telegram only opens Mini Apps from a public `https://` address. The bot's web server listens
on `127.0.0.1:8080` on your server, and a Cloudflare Tunnel gives it a secure public address
without opening any port. Every request is checked against Telegram's signature and your
chat id, so nobody else can read or change your city.

## Option A: Caddy is already on the server (no domain needed)

If the server already runs Caddy for another Mini App (for example `C:\tools\Caddyfile`),
use a free `sslip.io` name that points at the server's IP. With the IP `46.249.98.106`:

1. Add to the end of the Caddyfile:
   ```
   city.46-249-98-106.sslip.io {
       reverse_proxy 127.0.0.1:8080
   }
   ```
2. Restart Caddy: `C:\tools\nssm.exe restart caddy`. It gets the https certificate by itself.
3. Add `WEBAPP_URL=https://city.46-249-98-106.sslip.io` to `C:\ocitytasks\.env` and restart the bot
   (step 6 below).

## Option B: Cloudflare Tunnel, step by step

You need a domain whose DNS is on Cloudflare (the free plan is enough). The city will live on a
subdomain such as `city.example.com`.

1. Install cloudflared (in cmd as administrator):
   ```
   winget install --id Cloudflare.cloudflared
   ```
   Close and reopen cmd as administrator afterwards.
2. Log in and create the tunnel:
   ```
   cloudflared tunnel login
   cloudflared tunnel create ocity
   cloudflared tunnel route dns ocity city.example.com
   ```
   `tunnel create` prints the tunnel id and the path of a credentials `.json` file.
3. Create `C:\Users\<you>\.cloudflared\config.yml` with Notepad:
   ```
   tunnel: <tunnel id>
   credentials-file: C:\Users\<you>\.cloudflared\<tunnel id>.json
   ingress:
     - hostname: city.example.com
       service: http://localhost:8080
     - service: http_status:404
   ```
4. Run it as a Windows service so it starts with the server:
   ```
   cloudflared --config C:\Users\<you>\.cloudflared\config.yml service install
   ```
5. Add to `C:\ocitytasks\.env`:
   ```
   WEBAPP_URL=https://city.example.com
   ```
6. Install the new dependency and restart the bot:
   ```
   schtasks /end /tn OCityBot
   cd C:\ocitytasks
   git pull
   .venv\Scripts\pip install -r requirements.txt
   schtasks /run /tn OCityBot
   ```

The bot sends a message with a «🏙 باز کردن شهر» button, and the menu button next to the
chat box opens the city too. Without `WEBAPP_URL` the bot runs exactly as before.
