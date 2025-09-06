#!/usr/bin/env python3
import os, re, glob, time, yaml, subprocess, sys, json
from evdev import UInput, ecodes as E
try:
    import requests
except Exception:
    requests = None

# ==== CONFIG ====
HIDRAW_OVERRIDE = "/dev/hidraw0"   # <-- set this
VID_HEX, PID_HEX = "045e", "003b"   # Microsoft SideWinder Game Voice
REPORT_LEN = 8                      # many legacy HIDs use 8; change to 16/32/64 if needed
MAP_FILE = "gvmap.yaml"             # same mapping file as before

# ---- Node-RED / MQTT config ----
NR_ENABLED     = True
NR_URL         = "http://127.0.0.1:1880/gv/event"  # set to your Node-RED HTTP In endpoint
NR_AUTH_HEADER = None  # e.g., {"Authorization": "Bearer <token>"} if you secure it

MQTT_ENABLED   = False
MQTT_HOST      = "127.0.0.1"  # your broker
MQTT_PORT      = 1883
MQTT_TOPIC     = "gv/event"   # default topic if you use global emit

# --- constants (top of file) ---
BIT_ALL   = 0x01
BIT_TEAM  = 0x02
BIT_CH1   = 0x04
BIT_CH2   = 0x08
BIT_CH3   = 0x10
BIT_CH4   = 0x20
BIT_CMD   = 0x40
BIT_MUTE  = 0x80

BIT_NAMES = {
    BIT_ALL:  "all",
    BIT_TEAM: "team",
    BIT_CH1:  "chan1",
    BIT_CH2:  "chan2",
    BIT_CH3:  "chan3",
    BIT_CH4:  "chan4",
    BIT_CMD:  "command",
    BIT_MUTE: "mute",
}

PRINT_ORDER = [BIT_MUTE, BIT_CMD, BIT_ALL, BIT_TEAM, BIT_CH1, BIT_CH2, BIT_CH3, BIT_CH4]

# ---- helpers ----
KEYCODES = {name: getattr(E, name) for name in dir(E) if name.startswith("KEY_")}

def find_hidraw_by_vidpid(vid_hex, pid_hex):
    for u in glob.glob("/sys/class/hidraw/hidraw*/device/uevent"):
        txt = open(u).read()
        m = re.search(r'HID_ID=\S+:(\w{4}):(\w{4})', txt)
        if not m:
            continue
        vid, pid = m.group(1).lower(), m.group(2).lower()
        if vid == vid_hex.lower() and pid == pid_hex.lower():
            return "/dev/" + u.split('/')[-3]
    return None

def load_map(path=MAP_FILE):
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "bindings" not in data:
        raise ValueError("Top-level 'bindings' missing in mapping file.")
    return data["bindings"]

def press_combo(ui: UInput, keys):
    for k in keys: ui.write(E.EV_KEY, KEYCODES[k], 1)
    for k in reversed(keys): ui.write(E.EV_KEY, KEYCODES[k], 0)
    ui.syn()

def do_action(ui, spec):
    t = spec.get("type", "key")
    if t == "key":
        press_combo(ui, spec["keys"])
    elif t == "shell":
        subprocess.Popen(spec["cmd"], shell=True)

# ---- decode ----
def decode_edges(prev_mask: int, curr_mask: int):
    """Return list of semantic events for bits that flipped."""
    events = []
    changed = prev_mask ^ curr_mask
    # Emit in stable order so logs read nicely
    for bit in PRINT_ORDER:
        if changed & bit:
            base = BIT_NAMES[bit]
            edge = "on" if (curr_mask & bit) else "off"
            events.append((f"{base}_{edge}", bit, edge))
    return events

def decode_edges0(prev_mask: int, curr_mask: int):
    """
    Return a list of (event_name, edge) for every bit that changed.
    edge is "on" if bit now set, "off" if bit now cleared.
    """
    events = []
    print ()
    changed = prev_mask ^ curr_mask
    for bit, base in BIT_NAMES.items():
        if changed & bit:
            edge = "on" if (curr_mask & bit) else "off"
            events.append((f"{base}_{edge}", edge))
    # Optional: also emit the aggregate mask for dashboards/logic
    events.append((f"mask_{curr_mask:02x}", "state"))
    return events

def active_set(mask: int):
    """Return list of active flag names in stable order."""
    names = []
    for bit in PRINT_ORDER:
        if mask & bit:
            names.append(BIT_NAMES[bit])
    return names

def fmt_mask(mask: int):
    names = active_set(mask)
    if names:
        return f"0x{mask:02x} [{', '.join(names)}]"
    return f"0x{mask:02x} [none]"

# ---- hidraw ----
def find_hidraw_by_vidpid(vid_hex, pid_hex, verbose=True):
    """
    Find /dev/hidrawX for a given VID:PID.
    Supports kernels that expose 8-hex digits in HID_ID (e.g., 0000045E:0000003B).
    """
    vid_hex = vid_hex.lower()
    pid_hex = pid_hex.lower()

    candidates = glob.glob("/sys/class/hidraw/hidraw*/device/uevent") \
               + glob.glob("/sys/class/hidraw/hidraw*/uevent")

    found = []
    for u in sorted(candidates):
        try:
            txt = open(u, "r").read()
        except Exception as e:
            if verbose: print(f"[GV] WARN: cannot read {u}: {e}")
            continue

        # Match BUS:VID:PID where VID/PID may be 4..8 hex chars
        m = re.search(r'HID_ID=\S+:([0-9A-Fa-f]{4,8}):([0-9A-Fa-f]{4,8})', txt)
        if not m:
            continue
        raw_vid, raw_pid = m.group(1).lower(), m.group(2).lower()
        tail_vid, tail_pid = raw_vid[-4:], raw_pid[-4:]  # compare last 4
        node = "/dev/" + u.split('/')[-3]  # .../hidrawX/...

        found.append((node, raw_vid, raw_pid))
        if verbose:
            print(f"[GV] Probe: {node} HID_ID VID={raw_vid} PID={raw_pid}")

        if tail_vid == vid_hex and tail_pid == pid_hex:
            if verbose:
                print(f"[GV] Match: {node} (VID {raw_vid} PID {raw_pid})")
            return node

    if verbose:
        print(f"[GV] No match for VID:PID {vid_hex}:{pid_hex}. Probed: {found}")
    return None

# ---- node-red mqtt ifttt ----
def emit_to_nodered(event_name: str, edge: str, mask: int):
    if not NR_ENABLED:
        return
    payload = {
        "event": event_name,        # e.g., "chan1_on"
        "edge": edge,               # "on" / "off" / "state"
        "mask_hex": f"{mask:02x}",  # e.g., "28"
        "mask_int": mask,           # 40
        "active": active_set(mask), # ["chan2","chan4"]
        "ts": time.time()
    }
    # Prefer requests; fall back to curl if not available
    if requests:
        try:
            headers = {"Content-Type": "application/json"}
            if NR_AUTH_HEADER:
                headers.update(NR_AUTH_HEADER)
            requests.post(NR_URL, json=payload, headers=headers, timeout=1.5)
        except Exception as e:
            print(f"[GV] WARN: Node-RED POST failed: {e}")
    else:
        try:
            os.spawnlp(os.P_NOWAIT, "curl", "curl", "-sS", "-X", "POST",
                      "-H", "Content-Type: application/json",
                      "-d", json.dumps(payload), NR_URL)
        except Exception as e:
            print(f"[GV] WARN: curl POST failed: {e}")

def emit_mqtt(payload: dict, topic: str = MQTT_TOPIC):
    if not MQTT_ENABLED: return
    try:
        os.spawnlp(os.P_NOWAIT, "mosquitto_pub", "mosquitto_pub",
                   "-h", MQTT_HOST, "-p", str(MQTT_PORT),
                   "-t", topic, "-m", json.dumps(payload))
    except Exception as e:
        print(f"[GV] WARN: MQTT publish failed: {e}")



# ---- main loop ----
def main():
    # locate hidraw node
    #node = find_hidraw_by_vidpid(VID_HEX, PID_HEX)
    node = HIDRAW_OVERRIDE or find_hidraw_by_vidpid(VID_HEX, PID_HEX,true)
    if not node:
        print(f"[GV] ERROR: hidraw node not found for {VID_HEX}:{PID_HEX}.", file=sys.stderr)
        sys.exit(3)
    print(f"[GV] Using {node}")

    # open virtual keyboard
    ui = UInput()

    # open hidraw non-blocking
    fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)

    # load bindings
    bindings = load_map()

    prev = None
    print("[GV] Daemon running. Press buttons; readable diagnostics below. Ctrl+C to exit.")
    try:
        while True:
            try:
                data = os.read(fd, 1)        # one-byte state
                if not data:
                    continue
                curr = data[0]

                if prev is None:
                    prev = curr
                    print(f"[GV] INIT state = {fmt_mask(curr)}")
                    continue

                if curr == prev:
                    # No change; ignore
                    continue

                # Log semantic edges
                for ev_name, bit, edge in decode_edges(prev, curr):
                    print(f"[GV] {ev_name:12s}  . state {fmt_mask(curr)}")

                    # emit to Node-RED (and/or MQTT)
                    emit_to_nodered(ev_name, edge, curr)
                    # emit_mqtt({"event": ev_name, "edge": edge, "mask": curr, "active": active_set(curr), "ts": time.time()})

                    # Dispatch action if bound
                    spec = bindings.get(ev_name)
                    if spec:
                        do_action(ui, spec)

                prev = curr

            except BlockingIOError:
                pass
            time.sleep(0.003)
    except KeyboardInterrupt:
        pass

    finally:
        ui.close()
        os.close(fd)

# ---- program start ----
if __name__ == "__main__":
    main()

