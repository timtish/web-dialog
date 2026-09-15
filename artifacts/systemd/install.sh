cd /opt/dialog
pnpm install
PORT=8011 BASE_PATH=/ pnpm --filter @workspace/dialog run build   # появится dist/public
sudo cp systemd/dialog.service /etc/systemd/system/

sudo useradd -r -s /usr/sbin/nologin dialog
sudo chown -R dialog:dialog /opt/dialog

# uv sync --frozen
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install "fastapi>=0.141.1" "httpx>=0.28.1" "uvicorn>=0.52.4"

sudo systemctl daemon-reload
sudo systemctl enable --now dialog

sudo journalctl -u dialog -f