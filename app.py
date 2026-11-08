import os
import json
import signal
import subprocess
import shutil
import zipfile
import hashlib
import psutil
import threading
import time
import urllib.request
import sys
import shlex
import re
from pathlib import Path
from functools import wraps
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"
SERVERS_DIR = BASE_DIR / "servers"
SERVERS_DIR.mkdir(exist_ok=True)

NORMAL_PASSWORD = os.environ.get("NORMAL_PASSWORD", "TYNEX")

RUNNING_PROCESSES = {}

def load_data():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except Exception:
            pass
    return {
        "servers": {},
        "users": {},
        "settings": {
            "maintenance": False,
            "maintenance_msg": "System under maintenance.",
            "theme_color": "#f04452",
            "normal_password": NORMAL_PASSWORD,
            "site_name": "TY HOSTING SERVER",
            "auto_restart_interval": 300
        }
    }

def save_data(data):
    DATA_FILE.write_text(json.dumps(data, indent=2, default=str))

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def get_theme_color():
    data = load_data()
    return data.get("settings", {}).get("theme_color", "#00ff41")

@app.context_processor
def inject_theme():
    return {"theme_color": get_theme_color()}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        data = load_data()
        settings = data.get("settings", {})
        if settings.get("maintenance"):
            return render_template("maintenance.html", message=settings.get("maintenance_msg", "Under maintenance"), site_name=settings.get("site_name", "TY HOSTING SERVER"), theme_color=get_theme_color())
        return f(*args, **kwargs)
    return decorated

def is_process_alive(pid):
    try:
        if not pid:
            return False
        p = psutil.Process(pid)
        return p.is_running() and p.status() not in [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

def kill_process(pid):
    try:
        p = psutil.Process(pid)
        children = p.children(recursive=True)
        p.terminate()
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except psutil.TimeoutExpired:
            p.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

def auto_detect_main_file(extract_dir):
    candidates = ["main.py", "bot.py", "app.py", "index.js", "bot.js", "app.js", "index.ts", "server.js"]
    for f in candidates:
        if (extract_dir / f).exists():
            return f
    for p in extract_dir.iterdir():
        if p.is_file() and p.suffix in [".py", ".js", ".ts", ".php", ".go", ".sh"]:
            return p.name
    return "main.py"

def prepare_environment_and_deps(extract_dir, log_path, runtime="python"):
    env = os.environ.copy()
    subfolders = [str(extract_dir)]
    for item in extract_dir.iterdir():
        if item.is_dir() and not item.name.startswith((".", "__pycache__", "node_modules")):
            subfolders.append(str(item))
    
    existing_pythonpath = env.get("PYTHONPATH", "")
    new_pythonpath = os.pathsep.join(subfolders)
    env["PYTHONPATH"] = f"{new_pythonpath}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else new_pythonpath

    try:
        if runtime == "node":
            pkg_json = extract_dir / "package.json"
            node_modules = extract_dir / "node_modules"
            if pkg_json.exists() and not node_modules.exists():
                with open(log_path, "a") as lf:
                    lf.write(f"\n[SYSTEM] Installing Node.js dependencies via npm install...\n")
                subprocess.run(["npm", "install"], cwd=str(extract_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            req_file = extract_dir / "requirements.txt"
            if req_file.exists():
                with open(log_path, "a") as lf:
                    lf.write(f"\n[SYSTEM] Installing Python dependencies via pip install...\n")
                subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=str(extract_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        with open(log_path, "a") as lf:
            lf.write(f"\n[SYSTEM WARNING] Dependency auto-install error: {e}\n")

    return env

def get_run_command(runtime, main_file):
    ext = Path(main_file).suffix.lower()
    if runtime == "node" or ext in (".js", ".ts", ".mjs"):
        return ["node", main_file]
    elif runtime == "php" or ext == ".php":
        return ["php", main_file]
    elif runtime == "go" or ext == ".go":
        return ["go", "run", main_file]
    elif runtime == "sh" or ext == ".sh":
        return ["bash", main_file]
    elif runtime == "static":
        return [sys.executable, "-m", "http.server", "8080"]
    else:
        return [sys.executable, "-u", main_file]

def _sync_process_status():
    data = load_data()
    changed = False
    for name, cfg in data["servers"].items():
        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            changed = True
    if changed:
        save_data(data)

_sync_process_status()

# ==================== RENDER RESTART PREVENTION ====================
def render_keep_alive():
    while True:
        try:
            time.sleep(600)
            port = os.environ.get("PORT", 5000)
            url = f"http://127.0.0.1:{port}/api/ping"
            
            req = urllib.request.Request(url, headers={'User-Agent': 'Render-KeepAlive/1.0'})
            urllib.request.urlopen(req, timeout=10)
            
            external_url = os.environ.get("RENDER_EXTERNAL_URL")
            if external_url:
                ping_url = f"{external_url}/api/ping"
                req2 = urllib.request.Request(ping_url, headers={'User-Agent': 'Render-KeepAlive/1.0'})
                urllib.request.urlopen(req2, timeout=10)
        except Exception:
            pass

threading.Thread(target=render_keep_alive, daemon=True).start()

@app.route("/api/ping")
def ping():
    return "pong", 200

def keep_alive():
    while True:
        time.sleep(240)
        try:
            url = os.environ.get("RENDER_EXTERNAL_URL")
            if url:
                ping_url = f"{url}/api/ping"
            else:
                port = os.environ.get("PORT", 5000)
                ping_url = f"http://127.0.0.1:{port}/api/ping"
            req = urllib.request.Request(ping_url, headers={'User-Agent': 'KeepAlive-Bot/1.0'})
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

threading.Thread(target=keep_alive, daemon=True).start()

# ==================== AUTO RESTART SYSTEM ====================
def auto_restart_server(name):
    try:
        data = load_data()
        cfg = data["servers"].get(name)
        if not cfg:
            return
        
        pid = cfg.get("pid")
        if pid and is_process_alive(pid):
            kill_process(pid)
            if name in RUNNING_PROCESSES:
                try:
                    RUNNING_PROCESSES[name]["proc"].terminate()
                    RUNNING_PROCESSES[name]["log_file"].close()
                except Exception:
                    pass
                del RUNNING_PROCESSES[name]
        
        extract_dir = SERVERS_DIR / name / "extracted"
        main_file = cfg.get("main_file") or auto_detect_main_file(extract_dir)
        main_cmd = cfg.get("main_command") or ""
        main_path = extract_dir / main_file
        
        if not main_path.exists():
            return
        
        log_path = SERVERS_DIR / name / "logs.txt"
        if main_cmd:
            cmd = shlex.split(main_cmd)
        else:
            cmd = get_run_command(cfg.get("runtime", "python"), main_file)
            
        env = prepare_environment_and_deps(extract_dir, log_path, cfg.get("runtime", "python"))
        env["PORT"] = str(cfg.get("port", 8080))
        
        with open(log_path, "a") as lf:
            lf.write(f"\n{'='*50}\n[{datetime.now().isoformat()}] AUTO-RESTART triggered\n{'='*50}\n")
        log_file = open(log_path, "a")
        proc = subprocess.Popen(cmd, cwd=str(extract_dir), stdout=log_file, stderr=log_file, env=env, preexec_fn=os.setsid)
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
        data["servers"][name] = cfg
        save_data(data)
    except Exception as e:
        print(f"Auto-restart error for {name}: {e}")

def auto_restart_monitor():
    while True:
        try:
            data = load_data()
            settings = data.get("settings", {})
            interval = settings.get("auto_restart_interval", 300)
            
            for name, cfg in data["servers"].items():
                pid = cfg.get("pid")
                if pid and not is_process_alive(pid):
                    cfg["status"] = "stopped"
                    cfg["pid"] = None
                    save_data(data)
                
                if cfg.get("status") == "stopped":
                    extract_dir = SERVERS_DIR / name / "extracted"
                    main_file = cfg.get("main_file") or auto_detect_main_file(extract_dir)
                    if (extract_dir / main_file).exists():
                        threading.Thread(target=auto_restart_server, args=[name], daemon=True).start()
            
            time.sleep(interval)
        except Exception:
            time.sleep(30)

threading.Thread(target=auto_restart_monitor, daemon=True).start()

# ==================== LANDING + LOGIN ====================
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{2,20}$")

def site_name_value():
    return load_data().get("settings", {}).get("site_name", "TY HOSTING SERVER")

@app.route("/")
def landing():
    # First page: landing screen. START button -> login page.
    if session.get("username"):
        return redirect(url_for("dashboard"))
    data = load_data()
    return render_template("landing.html", site_name=site_name_value(),
                           total_users=len(data.get("users", {})),
                           total_servers=len(data.get("servers", {})),
                           theme_color=get_theme_color())

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("username"):
        return redirect(url_for("dashboard"))

    data = load_data()
    settings = data.get("settings", {})
    site_name = settings.get("site_name", "ZX FILE")

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "").strip()
        normal_pass = settings.get("normal_password", NORMAL_PASSWORD)

        def fail(msg):
            return render_template("login.html", error=msg, username=username,
                                   theme_color=get_theme_color(), site_name=site_name)

        if not USERNAME_RE.match(username):
            return fail("Username must be 2-20 chars (a-z, 0-9, _ . -)")
        if password != normal_pass:
            return fail("Wrong password")

        users = data.setdefault("users", {})
        if username not in users:
            users[username] = {"joined": datetime.now().isoformat()}
        users[username]["password_hash"] = hash_password(password)
        save_data(data)

        session["username"] = username
        return redirect(url_for("dashboard"))

    return render_template("login.html", error=None, username="",
                           theme_color=get_theme_color(), site_name=site_name)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ==================== DASHBOARD ====================
@app.route("/dashboard")
@login_required
def dashboard():
    username = session["username"]
    data = load_data()
    settings = data.get("settings", {})
    site_name = settings.get("site_name", "TY HOSTING SERVER")
    user_servers = {k: v for k, v in data["servers"].items() if v.get("owner") == username}
    changed = False
    for name, cfg in user_servers.items():
        pid = cfg.get("pid")
        if pid and not is_process_alive(pid):
            cfg["status"] = "stopped"
            cfg["pid"] = None
            data["servers"][name] = cfg
            changed = True
    if changed:
        save_data(data)
    running = sum(1 for v in user_servers.values() if v.get("status") == "running")
    return render_template("dashboard.html", servers=user_servers, running=running, total=len(user_servers), username=username, site_name=site_name, theme_color=get_theme_color())

@app.route("/api/stats")
@login_required
def system_stats():
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    return jsonify({"cpu": cpu, "ram": ram, "disk": disk})

# ==================== SERVER CRUD ====================
@app.route("/server/create", methods=["POST"])
@login_required
def create_server():
    name = request.form.get("name", "").strip().replace(" ", "-")
    runtime = request.form.get("runtime", "python")
    if not name:
        return redirect(url_for("dashboard"))
    data = load_data()
    if name in data["servers"]:
        return redirect(url_for("dashboard"))
    cfg = {
        "name": name,
        "owner": session["username"],
        "runtime": runtime,
        "status": "stopped",
        "main_file": "",
        "main_command": "",
        "port": 8080,
        "pid": None,
        "created": datetime.now().isoformat()
    }
    data["servers"][name] = cfg
    save_data(data)
    (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
    return redirect(url_for("server_detail", name=name))

@app.route("/server/delete/<name>", methods=["POST"])
@login_required
def delete_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if cfg and cfg.get("owner") == session["username"]:
        pid = cfg.get("pid")
        if pid:
            kill_process(pid)
        if name in RUNNING_PROCESSES:
            try:
                RUNNING_PROCESSES[name]["proc"].terminate()
                RUNNING_PROCESSES[name]["log_file"].close()
            except Exception:
                pass
            del RUNNING_PROCESSES[name]
        del data["servers"][name]
        save_data(data)
        shutil.rmtree(SERVERS_DIR / name, ignore_errors=True)
    return redirect(url_for("dashboard"))

@app.route("/server/<name>")
@login_required
def server_detail(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return "Server not found", 404
    if cfg.get("owner") != session["username"]:
        return "Access denied", 403
    pid = cfg.get("pid")
    if pid and not is_process_alive(pid):
        cfg["status"] = "stopped"
        cfg["pid"] = None
        data["servers"][name] = cfg
        save_data(data)
    if "main_command" not in cfg:
        cfg["main_command"] = ""
    extract_dir = SERVERS_DIR / name / "extracted"
    files = list_files(extract_dir)
    return render_template("server.html", server_name=name, config=cfg, files=files, theme_color=get_theme_color())

def list_files(directory, base=""):
    result = []
    if not directory.exists():
        return result
    try:
        for entry in sorted(directory.iterdir(), key=lambda e: (e.is_file(), e.name)):
            rel = f"{base}/{entry.name}" if base else entry.name
            if entry.is_dir():
                result.append({"name": entry.name, "path": rel, "type": "dir", "size": 0})
                result.extend(list_files(entry, rel))
            else:
                result.append({"name": entry.name, "path": rel, "type": "file", "size": entry.stat().st_size})
    except Exception:
        pass
    return result

def pip_run(args, cwd=None):
    """Run pip; retry with --break-system-packages on PEP 668 hosts."""
    base = [sys.executable, "-m", "pip", "install"]
    res = subprocess.run(base + args, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0 and "externally-managed" in (res.stderr or ""):
        res = subprocess.run(base + ["--break-system-packages"] + args, cwd=cwd, capture_output=True, text=True)
    return res

def _auto_install_from_files(name):
    """After upload: install modules from requirements.txt / package.json (runs in background, logs to console)."""
    extract_dir = SERVERS_DIR / name / "extracted"
    log_path = SERVERS_DIR / name / "logs.txt"
    def log(msg):
        with open(log_path, "a") as lf:
            lf.write(msg)
    try:
        reqs = [p for p in extract_dir.rglob("requirements.txt") if "node_modules" not in p.parts]
        reqs.sort(key=lambda p: len(p.parts))
        if reqs:
            req = reqs[0]
            log(f"\n[AUTO-INSTALL] requirements.txt found ({req.relative_to(extract_dir)}). Installing modules...\n")
            res = pip_run(["-r", str(req)], cwd=str(req.parent))
            if res.returncode == 0:
                log("[AUTO-INSTALL] DONE: all modules installed successfully\n")
            else:
                log((res.stderr or res.stdout or "")[-1500:] + "\n[AUTO-INSTALL] FAILED: some modules could not be installed\n")
        pkgs = [p for p in extract_dir.rglob("package.json") if "node_modules" not in p.parts]
        pkgs.sort(key=lambda p: len(p.parts))
        if pkgs:
            pj = pkgs[0]
            log(f"\n[AUTO-INSTALL] package.json found. Running npm install...\n")
            res = subprocess.run(["npm", "install"], cwd=str(pj.parent), capture_output=True, text=True)
            if res.returncode == 0:
                log("[AUTO-INSTALL] DONE: npm modules installed successfully\n")
            else:
                log((res.stderr or res.stdout or "")[-1500:] + "\n[AUTO-INSTALL] FAILED: npm install error\n")
    except Exception as e:
        log(f"[AUTO-INSTALL] ERROR: {e}\n")

@app.route("/server/<name>/upload", methods=["POST"])
@login_required
def upload_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400
    
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"success": False, "error": "Empty filename"}), 400
    
    extract_dir = SERVERS_DIR / name / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    
    upload_path = SERVERS_DIR / name / f"upload_{f.filename}"
    f.save(upload_path)
    
    extracted_files = []
    
    if f.filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(upload_path, "r") as z:
                for member in z.infolist():
                    if member.filename.startswith(("/", "\\", "..", "../")):
                        upload_path.unlink(missing_ok=True)
                        return jsonify({"success": False, "error": "Invalid zip path"})
                
                z.extractall(extract_dir)
                for member in z.infolist():
                    if not member.is_dir():
                        extracted_files.append(member.filename)
            upload_path.unlink(missing_ok=True)
        except Exception as e:
            upload_path.unlink(missing_ok=True)
            return jsonify({"success": False, "error": f"Zip extraction failed: {str(e)}"}), 500
    else:
        dest = extract_dir / f.filename
        shutil.move(str(upload_path), str(dest))
        extracted_files = [f.filename]
        
    if not cfg.get("main_file"):
        cfg["main_file"] = auto_detect_main_file(extract_dir)
        data['servers'][name] = cfg
        save_data(data)

    threading.Thread(target=_auto_install_from_files, args=(name,), daemon=True).start()
    return redirect(url_for('server_detail', name=name))


@app.route("/server/<name>/settings", methods=["POST"])
@login_required
def save_settings(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    
    payload = request.get_json()
    cfg["main_file"] = payload.get("main_file", cfg.get("main_file", ""))
    cfg["main_command"] = payload.get("main_command", cfg.get("main_command", ""))
    cfg["port"] = payload.get("port", cfg.get("port", 8080))
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})

@app.route("/server/<name>/start", methods=["POST"])
@login_required
def start_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False, "error": "Server not found"}), 404
    
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    
    pid = cfg.get("pid")
    if pid and is_process_alive(pid):
        return jsonify({"success": False, "error": "Already running"})
    
    extract_dir = SERVERS_DIR / name / "extracted"
    main_file = cfg.get("main_file") or auto_detect_main_file(extract_dir)
    main_cmd = cfg.get("main_command") or ""
    main_path = extract_dir / main_file
    
    if not main_path.exists():
        return jsonify({"success": False, "error": f"{main_file} not found. Upload your files first."})
    
    log_path = SERVERS_DIR / name / "logs.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    
    if main_cmd:
        cmd = shlex.split(main_cmd)
    else:
        cmd = get_run_command(cfg.get("runtime", "python"), main_file)
    
    env = prepare_environment_and_deps(extract_dir, log_path, cfg.get("runtime", "python"))
    env["PORT"] = str(cfg.get("port", 8080))
    
    try:
        with open(log_path, "a") as lf:
            lf.write(f"\n{'='*50}\n[{datetime.now().isoformat()}] Starting: {' '.join(cmd)}\n{'='*50}\n")
        log_file = open(log_path, "a")
        proc = subprocess.Popen(cmd, cwd=str(extract_dir), stdout=log_file, stderr=log_file, env=env, preexec_fn=os.setsid)
        RUNNING_PROCESSES[name] = {"proc": proc, "log_file": log_file}
        cfg["status"] = "running"
        cfg["pid"] = proc.pid
        cfg["main_file"] = main_file
        data["servers"][name] = cfg
        save_data(data)
        return jsonify({"success": True, "pid": proc.pid})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/server/<name>/stop", methods=["POST"])
@login_required
def stop_server(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False}), 404
    
    if cfg.get("owner") != session["username"]:
        return jsonify({"success": False}), 403
    
    pid = cfg.get("pid")
    stopped = False
    
    if name in RUNNING_PROCESSES:
        entry = RUNNING_PROCESSES[name]
        proc = entry["proc"]
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            entry["log_file"].close()
        except Exception:
            pass
        del RUNNING_PROCESSES[name]
        stopped = True
    
    if pid and not stopped:
        kill_process(pid)
    
    log_path = SERVERS_DIR / name / "logs.txt"
    try:
        with open(log_path, "a") as lf:
            lf.write(f"[{datetime.now().isoformat()}] Server stopped\n")
    except Exception:
        pass
    
    cfg["status"] = "stopped"
    cfg["pid"] = None
    data["servers"][name] = cfg
    save_data(data)
    return jsonify({"success": True})

# ==================== LOGS & EXECUTION ====================
@app.route("/server/<name>/logs")
@login_required
def get_logs(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"logs": "Server not found"})
    
    log_path = SERVERS_DIR / name / "logs.txt"
    if not log_path.exists():
        return jsonify({"logs": "No logs yet. Start the server to see output."})
    
    try:
        with open(log_path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 50000), os.SEEK_SET)
            content = f.read().decode('utf-8', errors='ignore')
            
        lines = content.splitlines()
        if len(lines) > 200:
            lines = lines[-200:]
            content = "... (showing last 200 lines) ...\n" + "\n".join(lines)
        return jsonify({"logs": content or "No output yet."})
    except Exception as e:
        return jsonify({"logs": f"Error reading logs: {e}"})

@app.route("/server/<name>/exec", methods=["POST"])
@login_required
def exec_command(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json() or {}
    cmd_text = payload.get("command", "").strip()

    if not cmd_text:
        return jsonify({"success": False, "error": "No command specified"})

    log_path = SERVERS_DIR / name / "logs.txt"
    extract_dir = SERVERS_DIR / name / "extracted"

    try:
        with open(log_path, "a") as lf:
            lf.write(f"\n$ {cmd_text}\n")

        # shlex ব্যবহার করে নিরাপদে আর্গুমেন্ট পার্স করা হচ্ছে
        cmd_args = shlex.split(cmd_text)
        with open(log_path, "a") as lf:
            subprocess.Popen(
                cmd_args,
                cwd=str(extract_dir),
                stdout=lf,
                stderr=lf
            )
        return jsonify({"success": True})
    except Exception as e:
        with open(log_path, "a") as lf:
            lf.write(f"Command execution error: {e}\n")
        return jsonify({"success": False, "error": str(e)})

@app.route("/server/<name>/logs/clear", methods=["POST"])
@login_required
def clear_logs(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg:
        return jsonify({"success": False})
    
    log_path = SERVERS_DIR / name / "logs.txt"
    try:
        log_path.write_text("")
    except Exception:
        pass
    return jsonify({"success": True})

# ==================== PACKAGE MANAGER ENDPOINTS ====================

@app.route("/server/<name>/packages", methods=["GET"])
@login_required
def list_packages(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    runtime = cfg.get("runtime", "python")
    installed = []

    try:
        if runtime == "node":
            extract_dir = SERVERS_DIR / name / "extracted"
            pkg_json = extract_dir / "package.json"
            if pkg_json.exists():
                pdata = json.loads(pkg_json.read_text())
                deps = pdata.get("dependencies", {})
                for pkg, ver in deps.items():
                    installed.append({"name": pkg, "version": ver})
        else:
            req_file = SERVERS_DIR / name / "extracted" / "requirements.txt"
            if req_file.exists():
                lines = req_file.read_text().splitlines()
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("==")
                        pkg_name = parts[0].strip()
                        ver = parts[1].strip() if len(parts) > 1 else "latest"
                        installed.append({"name": pkg_name, "version": ver})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    return jsonify({"success": True, "packages": installed, "runtime": runtime})

PKG_RE = re.compile(r"^[A-Za-z0-9@/_.\-]+$")

def _run_install(name, runtime, pkg_name, version):
    extract_dir = SERVERS_DIR / name / "extracted"
    log_path = SERVERS_DIR / name / "logs.txt"
    label = f"{pkg_name}=={version}" if (version and runtime != "node") else (f"{pkg_name}@{version}" if version else pkg_name)
    def log(msg):
        with open(log_path, "a") as lf:
            lf.write(msg)
    try:
        log(f"\n[PACKAGE] Installing {label} ...\n")
        if runtime == "node":
            cmd = ["npm", "install", label]
            res = subprocess.run(cmd, cwd=str(extract_dir), capture_output=True, text=True)
        else:
            res = pip_run([label])
        if res.returncode != 0:
            log((res.stderr or res.stdout or "")[-1500:] + f"\n[PACKAGE] FAILED: {label}\n")
            return
        if runtime != "node":
            req_file = extract_dir / "requirements.txt"
            lines = req_file.read_text().splitlines() if req_file.exists() else []
            lines = [l for l in lines if not re.match(rf"^{re.escape(pkg_name)}(==|$)", l.strip(), re.I)]
            lines.append(label)
            req_file.write_text("\n".join(lines) + "\n")
        log(f"[PACKAGE] DONE: {label} installed successfully\n")
    except Exception as e:
        log(f"[PACKAGE] ERROR: {e}\n")

@app.route("/server/<name>/packages/install", methods=["POST"])
@login_required
def install_package(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json() or {}
    pkg_name = payload.get("name", "").strip()
    version = payload.get("version", "").strip()

    if not pkg_name or not PKG_RE.match(pkg_name) or (version and not PKG_RE.match(version)):
        return jsonify({"success": False, "error": "Invalid package name"}), 400

    runtime = cfg.get("runtime", "python")
    (SERVERS_DIR / name / "extracted").mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_run_install, args=(name, runtime, pkg_name, version), daemon=True).start()
    return jsonify({"success": True, "message": f"Installing {pkg_name}... check LOGS"})

@app.route("/server/<name>/file/delete", methods=["POST"])
@login_required
def delete_file(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403
    rel = (request.get_json() or {}).get("filename", "").strip()
    base = (SERVERS_DIR / name / "extracted").resolve()
    target = (base / rel).resolve()
    if not rel or base not in target.parents or not target.exists():
        return jsonify({"success": False, "error": "Not found"}), 404
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink()
    return jsonify({"success": True})

@app.route("/server/<name>/packages/remove", methods=["POST"])
@login_required
def remove_package(name):
    data = load_data()
    cfg = data["servers"].get(name)
    if not cfg or cfg.get("owner") != session["username"]:
        return jsonify({"success": False, "error": "Access denied"}), 403

    payload = request.get_json() or {}
    pkg_name = payload.get("name", "").strip()

    if not pkg_name:
        return jsonify({"success": False, "error": "Package name is required"}), 400

    runtime = cfg.get("runtime", "python")
    extract_dir = SERVERS_DIR / name / "extracted"

    try:
        if runtime == "node":
            cmd = ["npm", "uninstall", pkg_name]
            res = subprocess.run(cmd, cwd=str(extract_dir), capture_output=True, text=True)
            if res.returncode != 0:
                return jsonify({"success": False, "error": res.stderr or "Failed to remove NPM package"})
        else:
            req_file = extract_dir / "requirements.txt"
            if req_file.exists():
                lines = req_file.read_text().splitlines()
                lines = [l for l in lines if not l.startswith(pkg_name + "==") and l != pkg_name]
                req_file.write_text("\n".join(lines))

        return jsonify({"success": True, "message": f"Package {pkg_name} removed successfully!"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
