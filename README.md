# Codex Turn Notifications

Run a persistent monitor for Telegram and Slack notifications from a shared Codex app-server.

1. Install the WebSocket client with Python 3.8 or later.

   ```bash
   python3 -m pip install -r requirements.txt
   ```

2. Keep your v1 `config.json` next to `main.py`. For a fresh setup, copy `config.example.json` to `config.json`, set your Telegram or Slack credentials, and disable unused destinations. Set `app_server.endpoint` to the shared listener.
3. Remove the old v1 `notify` entry from the app-server's Codex `config.toml` to avoid duplicate delivery. Restart the app-server after updating its configuration.
4. Start the shared app-server if needed, launch the monitor, and connect Codex to the same endpoint. Run each command in a separate terminal.

   ```bash
   codex app-server --listen ws://127.0.0.1:8765
   python3 main.py
   codex --remote ws://127.0.0.1:8765
   ```

[Unix socket setup, notification filters, and reconnection behavior](docs/monitoring.md)
