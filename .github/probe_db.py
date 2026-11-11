"""TEMP probe (TDM#639): `desk db-migrate` the way the launcher starts it, in different places and with different flags."""
import os, subprocess, sys, shutil, time
from pathlib import Path

py = Path(sys.argv[1])
base = Path(os.environ["RUNNER_TEMP"])
cases = {
    "D_flags_file": (base / "pr1", subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW, "file"),
    "D_plain": (base / "pr2", 0, "pipe"),
    "C_flags_file": (Path(os.environ["LOCALAPPDATA"]) / "pr3", subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW, "file"),
    "D_flags_file_precreated_home": (base / "pr4", subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW, "file"),
}
for name, (home, flags, out) in cases.items():
    shutil.rmtree(home, ignore_errors=True)
    if "precreated" in name:
        (home / "logs").mkdir(parents=True)
    env = dict(os.environ, DESK_HOME=str(home), PYTHONUTF8="1")
    env.pop("DESK_STORE_URL", None)
    log = base / f"{name}.log"
    t0 = time.time()
    with open(log, "wb") as f:
        r = subprocess.run([str(py), "-I", "-X", "utf8", "-m", "desk.cli", "db-migrate"], env=env, creationflags=flags,
                           stdout=f if out == "file" else subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    text = (log.read_text(encoding="utf-8", errors="replace") if out == "file" else r.stdout.decode("utf-8", "replace"))
    last = [l for l in text.splitlines() if l.strip()][-3:]
    print(f"{name}: rc={r.returncode} in {time.time()-t0:.0f}s :: {' | '.join(x[:160] for x in last)}", flush=True)
    for pid_file in Path(home).glob("pg/postmaster.pid"):
        try:
            import psutil
            psutil.Process(int(pid_file.read_text().splitlines()[0])).kill()
        except Exception:
            pass
