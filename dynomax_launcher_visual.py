import argparse
import csv
import io
import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None

try:
    import can
except ImportError:
    can = None

try:
    import serial
except ImportError:
    serial = None

import ctypes

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

if not is_admin():
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
    sys.exit()
    
APP_TITLE = "Dynomax Launcher - Laboratório de Ensaio de Motores"
BASE_DIR = Path(__file__).resolve().parent
LOGO_PATH = BASE_DIR / "assets" / "logo_lem_simbolo.png"

BG = "#F2F6FA"
NAVY = "#12395F"
BLUE = "#1473C9"
GREEN = "#2DA56A"
RED = "#D9534F"
TEXT = "#17324D"
MUTED = "#66788A"
BORDER = "#D5E0EA"
PANEL = "#FFFFFF"
CONSOLE = "#151D25"

DEFAULT_SIGNALS = [
    dict(pid="PID001", name="RPM", can_channel=0, can_id="0x18FF50E5", extended=True, start_bit=0, length=16, endian="Intel", signed=False, factor=0.125, offset=0, unit="rpm"),
    dict(pid="PID002", name="Torque", can_channel=0, can_id="0x18FF50E5", extended=True, start_bit=16, length=16, endian="Intel", signed=True, factor=0.25, offset=-500, unit="Nm"),
    dict(pid="PID003", name="Vel_veiculo", can_channel=1, can_id="0x0CFEF100", extended=True, start_bit=0, length=16, endian="Intel", signed=False, factor=0.01, offset=0, unit="km/h"),
]

def parse_can_id(value):
    return int(str(value).strip(), 0)

def _motorola_positions(start, length):
    pos = start
    positions = []
    for _ in range(length):
        if pos < 0 or pos >= 64:
            raise ValueError("Sinal Motorola excede 8 bytes")
        positions.append(pos)
        pos = pos - 1 if pos % 8 else pos + 15
    return positions

def encode_raw(data, sig, raw):
    length, start = int(sig["length"]), int(sig["start_bit"])
    endian = sig["endian"].lower()
    raw = int(raw) & ((1 << length) - 1)
    if endian == "intel":
        positions = [start + i for i in range(length)]
        for i, pos in enumerate(positions):
            if pos >= 64:
                raise ValueError(f"{sig['name']}: sinal excede 8 bytes")
            mask = 1 << (pos % 8)
            if (raw >> i) & 1:
                data[pos // 8] |= mask
            else:
                data[pos // 8] &= ~mask
    elif endian == "motorola":
        for i, pos in enumerate(_motorola_positions(start, length)):
            mask = 1 << (7 - (pos % 8))
            if (raw >> (length - 1 - i)) & 1:
                data[pos // 8] |= mask
            else:
                data[pos // 8] &= ~mask
    else:
        raise ValueError("Endian deve ser Intel ou Motorola")

def decode_raw(data, sig):
    length, start = int(sig["length"]), int(sig["start_bit"])
    endian = sig["endian"].lower()
    raw = 0
    if endian == "intel":
        for i in range(length):
            pos = start + i
            if pos >= len(data) * 8:
                return None
            raw |= ((data[pos // 8] >> (pos % 8)) & 1) << i
    elif endian == "motorola":
        for pos in _motorola_positions(start, length):
            if pos >= len(data) * 8:
                return None
            raw = (raw << 1) | ((data[pos // 8] >> (7 - pos % 8)) & 1)
    else:
        raise ValueError("Endian deve ser Intel ou Motorola")
    if sig.get("signed", False) and raw & (1 << (length - 1)):
        raw -= 1 << length
    return raw

def format_value(v):
    if isinstance(v, str):
        return v
    v = float(v)
    if v.is_integer():
        return str(int(v))
    return f"{v:.6f}".rstrip("0").rstrip(".")

class LogoWidget(tk.Frame):
    def __init__(self, master, width=250, height=115, **kwargs):
        bg = kwargs.pop("bg", PANEL)
        super().__init__(master, width=width, height=height, bg=bg, **kwargs)
        self.pack_propagate(False)
        self._image_ref = None
        if Image is not None and LOGO_PATH.exists():
            image = Image.open(LOGO_PATH).convert("RGB")
            image.thumbnail((width - 8, height - 8), Image.Resampling.LANCZOS)
            self._image_ref = ImageTk.PhotoImage(image)
            tk.Label(self, image=self._image_ref, bg=bg, bd=0).place(
                relx=0.5, rely=0.5, anchor="center")
        else:
            canvas = tk.Canvas(self, width=width, height=height, bg=bg, highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            cx, cy = width // 2, height // 2
            r = min(width, height) * 0.32
            canvas.create_oval(cx-r, cy-r, cx+r, cy+r, outline=NAVY, width=6)
            canvas.create_arc(cx-r-5, cy-r-5, cx+r+5, cy+r+5, start=205, extent=250,
                              style=tk.ARC, outline=GREEN, width=5)
            canvas.create_text(cx, cy, text="LEM", fill=NAVY, font=("Segoe UI", 14, "bold"))


class DynomaxLauncher:
    STEPS = [
        ("Verificar conexão com módulo CAN (VECTOR VN1630A)", "usb"),
        ("Abrir e Verificar recursos de null modem (Portas Seriais Virtuais - com0com)", "serial"),
        ("Conectar com o ADS2000 (Background & Aquisição)", "signal"),
        ("Verificar conexão com módulo de controle da sala dinamométrica (CAN Module 1) e CAN 2", "network"),
        ("Verificar conexão com sistema de aquisição e controle (Dynomite Board Pro)", "message"),
    ]

    def __init__(self, root, config_path=None):
        self.root = root
        self.config_path = config_path
        self.cfg = self._load_config(config_path)
        
        default_ads_cfg = self.cfg.get("ads2000_config_path", str(BASE_DIR / "ads2002_config.json"))
        self.ads2000_config_path = tk.StringVar(value=default_ads_cfg)

        self.ads2000_script_path = str(BASE_DIR / "ads2000_worker.py")
        self.ads2000_process = None

        self.root.title(APP_TITLE)
        self.root.geometry("1080x600")
        self.root.minsize(960, 750)
        self.root.configure(bg=BG)
        self.step_labels = []
        self.is_running = False
        self.stop_event = threading.Event()
        
        self._style()
        self._show_splash()

    def _load_config(self, path):
        if path and Path(path).exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Erro ao carregar JSON: {e}")
        
        pids = [s.get("pid", s["name"]) for s in DEFAULT_SIGNALS]
        return {
            "serial_port": "COM24",
            "serial_baud": 115200,
            "message_rate_ms": 100,
            "vector_app": "CAN2CSV",
            "channels": [
                {"channel": 0, "bitrate": 500000},
                {"channel": 1, "bitrate": 250000}
            ],
            "csv_path": "dynomax.csv",
            "write_header": True,
            "save_csv_enabled": True,
            "send_serial_enabled": True,
            "send_can_enabled": True,
            "signals": DEFAULT_SIGNALS,
            "com_to_can_layout": pids,
            "can_to_com_layout": ["timestamp_s", "can_id"] + pids,
            "ads2000_config_path": str(BASE_DIR / "ads2002_config.json")
        }

    def _style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 21, "bold"), foreground=NAVY)
        style.configure("Subtitle.TLabel", font=("Segoe UI", 11), foreground=MUTED)
        style.configure("Section.TLabel", font=("Segoe UI", 12, "bold"), foreground=NAVY)
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold", "bold"), padding=(16, 10))
        style.configure("Secondary.TButton", font=("Segoe UI", 10), padding=(12, 8))

    def _show_splash(self):
        self.splash = tk.Toplevel(self.root)
        self.splash.title("Laboratório de Ensaio de Motores")
        self.splash.configure(bg=PANEL)
        self.splash.resizable(False, False)
        self.splash.geometry("500x330")
        self.splash.transient(self.root)
        self.root.withdraw()

        LogoWidget(self.splash, width=260, height=175, bg=PANEL).pack(pady=(22, 10))
        tk.Label(self.splash, text="DYNOMAX LAUNCHER", bg=PANEL, fg=NAVY,
                 font=("Segoe UI", 15, "bold")).pack()
        tk.Label(self.splash, text="Inicialização do ambiente de ensaios",
                 bg=PANEL, fg=MUTED, font=("Segoe UI", 10)).pack(pady=(4, 18))
        bar = ttk.Progressbar(self.splash, mode="indeterminate", length=340)
        bar.pack(pady=5)
        bar.start(12)
        tk.Label(self.splash, text="Versão 1.0.0 — Mode Réel / Matériel (com0com)",
                 bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(side="bottom", pady=16)
        self.root.after(2200, lambda: self._close_splash(bar))

    def _close_splash(self, bar):
        try:
            bar.stop()
            self.splash.destroy()
        except tk.TclError:
            pass
        self.root.deiconify()
        self._build_main()

    def _build_main(self):
        sidebar = tk.Frame(self.root, bg=NAVY, width=190)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(sidebar, text="LEM", bg=NAVY, fg="white",
                 font=("Segoe UI", 24, "bold")).pack(anchor="w", padx=22, pady=(28, 0))
        tk.Label(sidebar, text="Laboratório de Ensaio\nde Motores", bg=NAVY, fg="#DCEAF6",
                 justify="left", font=("Segoe UI", 10)).pack(anchor="w", padx=22, pady=(0, 35))
        for label, symbol in [("Início", "⌂"), ("Configuração", "⚙"), ("Log", "▤"), ("Sobre", "ⓘ")]:
            tk.Label(sidebar, text=f"   {symbol}   {label}", bg=NAVY, fg="white",
                     font=("Segoe UI", 11), anchor="w", padx=14, pady=12).pack(fill="x", padx=8, pady=2)
        tk.Label(sidebar, text="VERSÃO 1.0.0", bg=NAVY, fg="#B7CADB",
                 font=("Segoe UI", 8)).pack(side="bottom", anchor="w", padx=22, pady=18)

        main = ttk.Frame(self.root, padding=(24, 20, 24, 20))
        main.pack(side="left", fill="both", expand=True)

        header = ttk.Frame(main)
        header.pack(fill="x")
        LogoWidget(header, width=150, height=100, bg=BG).pack(side="left")
        right = ttk.Frame(header)
        right.pack(side="right", anchor="center", padx=(10, 0))
        ttk.Label(right, text="Dynomax Launcher", style="Title.TLabel").pack(anchor="e")
        ttk.Label(right, text="Inicialização do ambiente de ensaios com Hardware",
                  style="Subtitle.TLabel").pack(anchor="e", pady=(4, 0))

        ttk.Separator(main).pack(fill="x", pady=(10, 12))

        cfg_panel = ttk.LabelFrame(main, text="Ficheiros de Configuração", padding=10)
        cfg_panel.pack(fill="x", pady=(0, 6))

        f_vec = ttk.Frame(cfg_panel)
        f_vec.pack(fill="x", pady=1)
        ttk.Label(f_vec, text="Config Vector/Launcher:", width=22).pack(side="left")
        self.lbl_vec_cfg = ttk.Label(f_vec, text=str(self.config_path) if self.config_path else "Padrão (Predefinido)", foreground=TEXT)
        self.lbl_vec_cfg.pack(side="left", padx=5, fill="x", expand=True)
        ttk.Button(f_vec, text="📂 Escolher...", style="Secondary.TButton", command=self._select_config_file).pack(side="right")

        f_ads = ttk.Frame(cfg_panel)
        f_ads.pack(fill="x", pady=2)
        ttk.Label(f_ads, text="Config ADS2000:", width=22).pack(side="left")
        self.lbl_ads_cfg = ttk.Label(f_ads, textvariable=self.ads2000_config_path, foreground=MUTED)
        self.lbl_ads_cfg.pack(side="left", padx=5, fill="x", expand=True)
        ttk.Button(f_ads, text="📂 Escolher ADS...", style="Secondary.TButton", command=self._select_ads_config_file).pack(side="right")

        ttk.Label(main, text="Sequência de inicialização", style="Section.TLabel").pack(anchor="w", pady=(0, 9))

        steps_frame = ttk.Frame(main)
        steps_frame.pack(fill="x")
        self.step_labels = []
        for i, (description, _icon) in enumerate(self.STEPS, start=1):
            row = tk.Frame(steps_frame, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
            row.pack(fill="x", pady=3, ipady=4)
            tk.Label(row, text=f"{i}", bg=BLUE, fg="white",
                     font=("Segoe UI", 10, "bold"), width=3, height=1).pack(side="left", padx=(10, 12), pady=4)
            tk.Label(row, text=description, bg=PANEL, fg=TEXT,
                     font=("Segoe UI", 10), anchor="w", wraplength=590,
                     justify="left").pack(side="left", fill="x", expand=True, pady=4)
            lbl_status = tk.Label(row, text="●   Aguardando", bg=PANEL, fg=MUTED, font=("Segoe UI", 9), padx=12)
            lbl_status.pack(side="right")
            self.step_labels.append(lbl_status)

        ttk.Separator(main).pack(fill="x", pady=(12, 10))
        ttk.Label(main, text="Log de inicialização", style="Section.TLabel").pack(anchor="w", pady=(0, 5))
        self.console = tk.Text(main, height=6, bg=CONSOLE, fg="#D9E7F5",
                               insertbackground="white", relief="flat", font=("Consolas", 10),
                               padx=12, pady=5, wrap="word")
        self.console.pack(fill="both", expand=True)
        self.log_to_console(">> Interface pronta. Aguardando início da verificação.")
        self.console.configure(state="disabled")

        footer = ttk.Frame(main)
        footer.pack(fill="x", pady=(12, 0))
        
        self.lbl_mode = ttk.Label(footer, text="Pronto", foreground=MUTED)
        self.lbl_mode.pack(side="left", padx=5)
        
        self.btn_start = ttk.Button(footer, text="▶   Iniciar verificação", style="Primary.TButton",
                                    command=self.start_verification_thread)
        self.btn_start.pack(side="right", padx=(8, 0))
        
        self.btn_dynomax = ttk.Button(footer, text="Abrir Dynomax", style="Secondary.TButton",
                                      state="disabled", command=self._open_dynomax)
        self.btn_dynomax.pack(side="right")

    def _select_config_file(self):
        file_path = filedialog.askopenfilename(
            title="Selecionar arquivo de configuração JSON (Launcher/Vector)",
            filetypes=[("Arquivos JSON", "*.json"), ("Todos os arquivos", "*.*")]
        )
        if file_path:
            self.config_path = file_path
            self.cfg = self._load_config(file_path)
            self.lbl_vec_cfg.config(text=file_path)
            self.log_to_console(f">> Configuração do Launcher carregada: {Path(file_path).name}")

    def _select_ads_config_file(self):
        file_path = filedialog.askopenfilename(
            title="Selecionar arquivo de configuração JSON do ADS2000",
            filetypes=[("Arquivos JSON", "*.json"), ("Todos os arquivos", "*.*")]
        )
        if file_path:
            self.ads2000_config_path.set(file_path)
            self.log_to_console(f">> Configuração do ADS2000 selecionada: {Path(file_path).name}")

    def log_to_console(self, text):
        self.console.configure(state="normal")
        self.console.insert("end", text + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def update_step_status(self, step_idx, status_text, color):
        if 0 <= step_idx < len(self.step_labels):
            self.step_labels[step_idx].config(text=status_text, fg=color)

    def _open_dynomax(self):
        exe_path = "C:/Program Files (x86)/DYNO-MAX/DYNO-MAX.exe"
        try:
            subprocess.Popen([exe_path])
            self.log_to_console(f">> Dynomax iniciado com sucesso: {exe_path}")
        except Exception as e:
            messagebox.showerror("Erro", f"Impossível iniciar o Dynomax:\n{e}")
            
    def start_verification_thread(self):
        if self.is_running:
            return
        self.is_running = True
        self.btn_start.state(["disabled"])
        for lbl in self.step_labels:
            lbl.config(text="●   Aguardando", fg=MUTED)
        
        threading.Thread(target=self._run_real_hardware_bridge, daemon=True).start()

    def _run_real_hardware_bridge(self):
        cfg = self.cfg
        success = True

        if can is None or serial is None:
            self.log_to_console(">> ERRO CRÍTICO: As bibliotecas 'python-can' e 'pyserial' são obrigatórias.")
            success = False

        # --- ETAPA 1: Verificar conexão com módulo CAN (VECTOR VN1630A) ---
        if success:
            self.update_step_status(0, "●  Verificando...", BLUE)
            self.log_to_console(">> [Etapa 1] Detectando hardware Vector...")
            try:
                configs = can.detect_available_configs(interfaces=["vector"])
                if configs:
                    self.log_to_console(f">> Hardware Vector encontrado: {configs}")
                else:
                    self.log_to_console(">> Nenhum hardware retornado pelo detect, tentativa direta...")
                self.update_step_status(0, "●  Sucesso", GREEN)
            except Exception as e:
                self.log_to_console(f">> Aviso Vector: {e}")
                self.update_step_status(0, "●  Sucesso (Forçado)", GREEN)

        # --- ETAPA 2: Abrir porta série principal (COM24 pour com0com) ---
        ser = None
        if success:
            self.update_step_status(1, "●  Abrindo COM...", BLUE)
            serial_port = cfg.get("serial_port", "COM24")
            serial_baud = cfg.get("serial_baud", 115200)

            self.log_to_console(f">> [Etapa 2] Abrindo porta série principal ({serial_port})...")
            try:
                ser = serial.Serial(serial_port, serial_baud, timeout=0.02, write_timeout=0.5)
                self.log_to_console(f">> Porta serial principal {serial_port} aberta com sucesso.")
                self.update_step_status(1, "●  Sucesso", GREEN)
            except Exception as e:
                self.log_to_console(f">> Erro Porta Serial ({serial_port}): {e}")
                self.update_step_status(1, "●  Falhou", RED)
                success = False

        # --- ETAPA 3: Conectar com o ADS2000 (Lancement du worker en arrière-plan) ---
        if success:
            self.update_step_status(2, "● Iniciando ADS2000...", BLUE)
            self.log_to_console(">> [Etapa 3] Iniciando ads2000_worker.py...")
            try:
                if self.ads2000_process:
                    try:
                        self.ads2000_process.terminate()
                        self.ads2000_process.wait(timeout=1)
                    except Exception:
                        pass
                    self.ads2000_process = None

                script_path = str(Path(self.ads2000_script_path).resolve())
                ads_config_file = str(Path(self.ads2000_config_path.get()).resolve())

                if not Path(script_path).is_file():
                    raise FileNotFoundError(f"Script worker introuvable : {script_path}")
                if not Path(ads_config_file).is_file():
                    raise FileNotFoundError(f"Config JSON introuvable : {ads_config_file}")

                cmd = [sys.executable, script_path, "--config", ads_config_file]
                self.log_to_console(f">> Commande: {' '.join(cmd)}")

                self.ads2000_process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE, text=True, encoding="utf-8",
                    errors="replace", bufsize=1
                )
                
                ads_connected = False
                ads_acq_started = False
                
                def read_ads_output():
                    nonlocal ads_connected, ads_acq_started
                    while self.ads2000_process and self.ads2000_process.poll() is None:
                        line = self.ads2000_process.stdout.readline()
                        if not line:
                            break
                        line_str = line.strip()
                        if not line_str:
                            continue
                        if line_str.startswith("@@ADS@@"):
                            try:
                                evt = json.loads(line_str.replace("@@ADS@@", "").strip())
                                if evt.get("event") == "connected":
                                    ads_connected = True
                                elif evt.get("event") == "acq_started":
                                    ads_acq_started = True
                            except Exception:
                                pass
                            self.root.after(0, lambda l=f"[ADS2000 Event] {line_str}": self.log_to_console(l))
                        else:
                            self.root.after(0, lambda l=f"[ADS2000] {line_str}": self.log_to_console(l))

                threading.Thread(target=read_ads_output, daemon=True).start()
                
                for _ in range(50):
                    if ads_connected and ads_acq_started:
                        break
                    time.sleep(0.2)

                if ads_connected and ads_acq_started:
                    self.update_step_status(2, "●  Sucesso", GREEN)
                    self.log_to_console(">> [Etapa 3] ADS2000 operando e adquirindo dados em segundo plano.")
                else:
                    raise RuntimeError("Timeout ao iniciar a aquisição do ADS2000.")

            except Exception as e:
                self.log_to_console(f">> Erro ADS2000: {e}")
                self.update_step_status(2, "●  Falhou", RED)
                success = False

        # --- ETAPA 4: Abrir barramentos CAN (CH0 e CH1) ---
        buses = {}
        if success:
            self.update_step_status(3, "●  Abrindo canais...", BLUE)
            self.log_to_console(">> [Etapa 4] Abrindo barramentos CAN (CH0 e CH1)...")
            try:
                channels_cfg = cfg.get("channels", [{"channel": 0, "bitrate": 500000}, {"channel": 1, "bitrate": 250000}])
                vector_app = cfg.get("vector_app", "CAN2CSV")
                for ch_cfg in channels_cfg:
                    ch = ch_cfg["channel"]
                    br = ch_cfg["bitrate"]
                    self.log_to_console(f">> Tentative d'ouverture canal CAN {ch} (@ {br} bit/s, App: {vector_app})...")
                    buses[ch] = can.Bus(interface="vector", channel=ch, app_name=vector_app, bitrate=br, fd=False)
                    self.log_to_console(f">> Canal CAN {ch} conectado com sucesso!")
                self.update_step_status(3, "●  Sucesso", GREEN)
            except Exception as e:
                self.log_to_console(f">> ERRO CRITIQUE CAN: Impossible d'ouvrir les canaux Vector : {e}")
                self.update_step_status(3, "●  Falhou", RED)
                success = False

        # --- ETAPA 5: Ponte active, démarrage de Dynomax ---
        if success:
            step_idx = 4
            self.update_step_status(step_idx, "●  Em execução", BLUE)
            self.log_to_console(f">> [{self.STEPS[step_idx][0]}] Ponte ativa! Transmissão CAN / COM iniciada.")
            self.btn_dynomax.config(state="normal")
            
            # Lancement automatique de Dynomax
            self._open_dynomax()
            
            try:
                input_layout = cfg.get("com_to_can_layout", [s.get("pid", s["name"]) for s in DEFAULT_SIGNALS])
                output_layout = cfg.get("can_to_com_layout", ["timestamp_s", "can_id"] + input_layout)
                save_csv = cfg.get("save_csv_enabled", True)
                send_serial = cfg.get("send_serial_enabled", True)
                send_can = cfg.get("send_can_enabled", True)

                signals_by_name = {}
                signals_list = cfg.get("signals", DEFAULT_SIGNALS)
                for sig in signals_list:
                    signals_by_name[sig["name"]] = sig
                    signals_by_name[sig.get("pid", sig["name"])] = sig

                csv_handle = None
                writer = None
                if save_csv:
                    csv_path_str = cfg.get("csv_path", "dynomax.csv")
                    path = Path(csv_path_str)
                    if str(path.parent) not in ("", "."):
                        path.parent.mkdir(parents=True, exist_ok=True)
                    new_file = not path.exists() or path.stat().st_size == 0
                    csv_handle = open(path, "a", newline="", encoding="utf-8")
                    writer = csv.writer(csv_handle)
                    if new_file and cfg.get("write_header", True):
                        writer.writerow(output_layout)
                        csv_handle.flush()

                last_values = {}
                serial_fields = [f for f in output_layout if f not in ("timestamp_s", "can_id")]
                rate_ms = cfg.get("message_rate_ms", 100)
                last_send = time.monotonic()
                sent_state = {"line": None}

                def send_serial_line():
                    try:
                        serial_line = ",".join(format_value(last_values.get(f, 0)) for f in serial_fields)
                        payload = (serial_line + "\r\n").encode("ascii", "replace")
                        
                        if ser and ser.is_open:
                            ser.write(payload)  # Envoi vers COM24 (com0com)
                            
                        if rate_ms == 0 or serial_line != sent_state["line"]:
                            sent_state["line"] = serial_line
                    except Exception:
                        pass

                while self.is_running:
                    # Lecture et émission sur les bus CAN (Vector VN1630A)
                    for ch, bus in buses.items():
                        for _ in range(50):
                            msg = bus.recv(timeout=0)
                            if msg is None:
                                break
                            signal_values = {}
                            matched = False
                            for sig in signals_list:
                                if int(sig.get("can_channel", 0)) == ch and \
                                   parse_can_id(sig["can_id"]) == msg.arbitration_id and \
                                   bool(sig["extended"]) == msg.is_extended_id:
                                    matched = True
                                    try:
                                        raw = decode_raw(msg.data, sig)
                                        physical = "" if raw is None else raw * float(sig["factor"]) + float(sig["offset"])
                                    except Exception:
                                        physical = ""
                                    signal_values[sig["name"]] = physical
                                    signal_values[sig.get("pid", sig["name"])] = physical

                            if matched:
                                for k, v in signal_values.items():
                                    if v != "":
                                        last_values[k] = v
                                value_map = {"timestamp_s": f"{msg.timestamp:.6f}", "can_id": f"0x{msg.arbitration_id:X}"}
                                value_map.update(signal_values)
                                row = [format_value(value_map.get(field, "")) for field in output_layout]
                                
                                if save_csv and writer is not None:
                                    writer.writerow(row)
                                    csv_handle.flush()

                                if send_serial and rate_ms == 0:
                                    send_serial_line()

                    if send_serial and rate_ms > 0:
                        now = time.monotonic()
                        if now - last_send >= rate_ms / 1000.0:
                            last_send = now
                            send_serial_line()

                    time.sleep(0.005)

                if csv_handle:
                    csv_handle.close()
            except Exception as e:
                self.log_to_console(f">> Erro na execução da ponte: {e}")

        # Nettoyage et arrêt propre
        try:
            if ser and ser.is_open:
                ser.close()
            for bus in buses.values():
                bus.shutdown()
            if self.ads2000_process:
                try:
                    self.ads2000_process.stdin.write("quit\n")
                    self.ads2000_process.stdin.flush()
                except Exception:
                    pass
                self.ads2000_process.terminate()
        except Exception:
            pass

        self.is_running = False
        self.btn_start.state(["!disabled"])


def main():
    parser = argparse.ArgumentParser(description="Dynomax Launcher com seleção JSON (com0com)")
    parser.add_argument("--config", type=str, help="Caminho para o arquivo JSON de configuração")
    args = parser.parse_args()

    root = tk.Tk()
    DynomaxLauncher(root, config_path=args.config)
    root.mainloop()


if __name__ == "__main__":
    main()
