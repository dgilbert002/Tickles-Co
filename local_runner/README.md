# Tickles Local Runner

Desktop companion app that runs backtests on your local machine and pushes
results to the VPS. Same engine as the VPS — results are identical (Rule #1).

## What's in the box

| File            | What it does                                              |
|-----------------|-----------------------------------------------------------|
| `ssh_tunnel.py` | Opens an SSH tunnel to the VPS and auto-restarts on drop. |
| `runner.py`     | The worker loop — claims jobs, runs backtests, submits.   |
| `tray.py`       | Windows system-tray UI with pause/resume/quit.            |
| `requirements.txt` | Python dependencies.                                   |

## First-time setup (Windows)

```powershell
# 1. Clone the shared backtest package alongside this folder:
#    C:\Tickles\
#      ├── shared\            <- from /opt/tickles/shared on the VPS
#      └── local_runner\      <- this folder
#
# 2. Install Python 3.12 + deps:
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 3. Make sure your SSH config has a `vps` alias:
#    ~/.ssh/config
#    Host vps
#      HostName 203.0.113.42
#      User root
#      IdentityFile ~/.ssh/id_rsa
#
# 4. Start the tunnel in one terminal:
python ssh_tunnel.py

# 5. Start the runner in another:
python runner.py --id "local-<your-pc-name>"

# 6. (Optional) run the tray for a tidy UI:
python tray.py
```

All three processes talk to each other through:
- The SSH tunnel (which maps VPS 5432/6379/9000 onto localhost)
- A tiny state file at `%USERPROFILE%\.tickles_runner\state.json`

## Packaging into a single .exe

```powershell
pip install pyinstaller
pyinstaller --windowed --onefile --name TicklesRunner tray.py
# also package runner (separately so you can close the UI but keep jobs running):
pyinstaller --onefile --name TicklesWorker runner.py
```

Drop the two `.exe` files into your Windows startup folder and you're done —
your desktop is now a JarvAIs cluster node.
