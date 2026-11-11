import sys
import time
import json
import argparse
from pathlib import Path
import ctypes
from ctypes import c_byte, c_short, c_int, c_float, POINTER
import serial

BUF_SIZE = 16384

class TpUserBuf(ctypes.Structure):
    _fields_ = [("Buffer", ctypes.c_int16 * BUF_SIZE)]

class LynxDLL:
    def __init__(self, dll_path: str):
        self.dll = ctypes.WinDLL(str(Path(dll_path).resolve()))
        self._bind()

    def _bind(self):
        d = self.dll
        d.OpenDriver.argtypes = []
        d.OpenDriver.restype = c_byte
        d.CloseDriver.argtypes = []
        d.CloseDriver.restype = None
        d.ConnectAC2122.argtypes = [ctypes.c_char_p]
        d.ConnectAC2122.restype = c_byte
        d.DisconnectAC2122.argtypes = []
        d.DisconnectAC2122.restype = None
        d.Clear_CM.argtypes = []
        d.Clear_CM.restype = None
        d.Insert_AI_CM.argtypes = [c_byte, c_byte, c_byte]
        d.Insert_AI_CM.restype = c_byte
        d.AcquisitionSetup.argtypes = [c_byte, c_float, POINTER(c_float), POINTER(c_int)]
        d.AcquisitionSetup.restype = c_byte
        d.StartAcquisition.argtypes = []
        d.StartAcquisition.restype = c_byte
        d.StopAcquisition.argtypes = []
        d.StopAcquisition.restype = None
        d.GetSamples.argtypes = [POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(TpUserBuf)]
        d.GetSamples.restype = c_byte

    def open_driver(self): return bool(self.dll.OpenDriver())
    def close_driver(self): self.dll.CloseDriver()
    def connect(self, ip: str): return bool(self.dll.ConnectAC2122(ip.encode("ascii") + b"\0"))
    def disconnect(self): self.dll.DisconnectAC2122()
    def clear_channels(self): self.dll.Clear_CM()
    def insert_channel(self, addr: int, ch: int, rng: int): return bool(self.dll.Insert_AI_CM(addr, ch, rng))
    def setup_acquisition(self, ext: bool, hz: float):
        actual, err = c_float(), c_int()
        ok = bool(self.dll.AcquisitionSetup(c_byte(1 if ext else 0), c_float(hz), ctypes.byref(actual), ctypes.byref(err)))
        return ok, actual.value, err.value
    def start(self): return bool(self.dll.StartAcquisition())
    def stop(self): self.dll.StopAcquisition()
    def get_samples(self, n_ch: int):
        err, last, read = c_int(), c_int(), c_int()
        buf = TpUserBuf()
        ok = bool(self.dll.GetSamples(ctypes.byref(err), ctypes.byref(last), ctypes.byref(read), ctypes.byref(buf)))
        n_rows = max(0, min(read.value, BUF_SIZE // max(1, n_ch)))
        return ok, err.value, last.value, n_rows, list(buf.Buffer[:n_rows * n_ch])

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    acq_cfg = cfg["acquisition"]
    ser_cfg = cfg.get("serial", {})

    dll = LynxDLL(acq_cfg["dll_path"])
    if not dll.open_driver():
        print(json.dumps({"event": "error", "message": "OpenDriver falhou"}))
        sys.exit(1)

    print("@@ADS@@ " + json.dumps({"event": "connected"}), flush=True)
    if not dll.connect(acq_cfg["ip"]):
        print(json.dumps({"event": "error", "message": "ConnectAC2122 falhou"}))
        sys.exit(1)

    # Configuration des canaux actifs du JSON ADS2000
    dll.clear_channels()
    active_channels = []
    for ch_str, ch_data in cfg.get("channels", {}).items():
        if ch_data.get("enabled"):
            ch_idx = int(ch_str)
            dll.insert_channel(acq_cfg["module_addr"], ch_idx, ch_data.get("range_index", 0))
            active_channels.append(ch_idx)

    ok, actual_hz, _ = dll.setup_acquisition(acq_cfg.get("external_clock", False), acq_cfg.get("sample_rate_hz", 1000))
    if not ok or not dll.start():
        print(json.dumps({"event": "error", "message": "StartAcquisition falhou"}))
        sys.exit(1)

    print("@@ADS@@ " + json.dumps({"event": "acq_started"}), flush=True)
    
    # Ouverture de la liaison série dédiée à l'ADS2000 (ex: COM26) depuis son propre JSON
    com_port = ser_cfg.get("com_port", "COM26")
    baudrate = ser_cfg.get("baudrate", 115200)
    period = ser_cfg.get("period_ms", 100) / 1000.0
    tx_chs = ser_cfg.get("tx_channels", active_channels)

    ser = None
    try:
        ser = serial.Serial(com_port, baudrate, timeout=0.1)
        print(json.dumps({"event": "info", "message": f"Port série {com_port} ouvert pour l'ADS2000"}))
    except Exception as e:
        print(json.dumps({"event": "error", "message": f"Impossible d'ouvrir {com_port}: {e}"}))

    default_ranges = [-10.0, -5.0, -2.0, -1.0]

    try:
        while True:
            ok, err, last, nrows, values = dll.get_samples(len(active_channels))
            if nrows > 0:
                n_ch = len(active_channels)
                latest_vals = {}
                
                for r in range(nrows):
                    for idx, ch in enumerate(active_channels):
                        raw = values[r * n_ch + idx]
                        
                        ch_data = cfg.get("channels", {}).get(str(ch), {})
                        coef = float(ch_data.get("coefficient", 1.0))
                        offset = float(ch_data.get("offset", 0.0))
                        range_idx = int(ch_data.get("range_index", 0))
                        
                        r_val = default_ranges[range_idx] if 0 <= range_idx < len(default_ranges) else default_ranges[0]
                        if r_val < 0:
                            volts = raw * (-r_val) / 32768.0
                        else:
                            volts = raw * r_val / 65536.0 + r_val / 2.0
                            
                        value = coef * volts + offset
                        latest_vals[ch] = value

                # Formatage et envoi direct sur le port série (ex: COM26) défini dans le JSON ADS2000
                row_data = [f"{latest_vals.get(ch, 0.0):.6f}" for ch in tx_chs]
                line = ",".join(row_data) + "\r\n"
                
                if ser and ser.is_open:
                    try:
                        ser.write(line.encode("ascii", "replace"))
                    except Exception:
                        pass

                # Optionnel : log sur la console pour le suivi
                print(line.strip(), flush=True)

            time.sleep(period)
    except KeyboardInterrupt:
        pass
    finally:
        if ser and ser.is_open:
            ser.close()
        dll.stop()
        dll.disconnect()
        dll.close_driver()

if __name__ == "__main__":
    main()
