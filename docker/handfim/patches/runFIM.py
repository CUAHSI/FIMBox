import os
import sys
import glob
import shutil
import subprocess
from dotenv import load_dotenv

from .datadownload import setup_directories


def runfim(code_dir, output_dir, HUC_code, data_dir, label="", depth=False):
    """
    TC: 06/21/25 The label parameter was added to this function so that unique output directories
        could be created for running FIM operations in parallel. This prevents the moasic
        FIM operations from merging outputs from multuple runs of the same HUC code.
    """

    original_dir = os.getcwd()
    try:
        tools_path = os.path.join(code_dir, "tools")
        src_path = os.path.join(code_dir, "src")
        os.chdir(tools_path)
        dotenv_path = os.path.join(code_dir, ".env")
        load_dotenv(dotenv_path)
        sys.path.append(src_path)
        sys.path.append(code_dir)

        HUC_code = str(HUC_code)
        HUC_dir = os.path.join(output_dir, f"flood_{HUC_code}")
        csv_path = data_dir

        discharge_basename = os.path.basename(data_dir).split(".")[0]
        inundation_dir = os.path.join(HUC_dir, f"{HUC_code}_inundation", label)
        temp_dir = os.path.join(inundation_dir, "temp")
        print("Inundation directory:", inundation_dir, flush=True)
        print("TEMP directory:", temp_dir, flush=True)

        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)

        inundation_file = os.path.join(temp_dir, f"{discharge_basename}_inundation.tif")
        # `stdbuf -oL -eL` forces line-buffering at the OS level for this
        # process (and, best-effort, for non-Python tools it shells out to
        # such as GDAL/TauDEM). PYTHONUNBUFFERED alone only affects Python's
        # own stdio and has no effect on those compiled binaries, which
        # otherwise fully-buffer their output whenever stdout is a pipe
        # (which it always is here) instead of a real terminal.
        #
        # `-v`/`--verbose` is required for inundate_mosaic_wrapper.py to emit
        # any progress at all: it defaults to False, and its internal
        # fh.vprint() calls and tqdm progress bars are gated by that flag
        # (see NOAA-OWP/inundation-mapping tools/inundate_gms.py). Without it,
        # the script prints nothing until the whole run finishes, which looks
        # like a hang even though output is being streamed correctly.
        Command = [
            "stdbuf",
            "-oL",
            "-eL",
            sys.executable,
            "inundate_mosaic_wrapper.py",
            "-y",
            HUC_dir,
            "-u",
            HUC_code,
            "-f",
            csv_path,
            "-i",
            inundation_file,
            "-v",
        ]

        if depth:
            depth_file = os.path.join(temp_dir, f"{discharge_basename}_depth.tif")
            Command += ["-d", depth_file]
        else:
            depth_file = None

        env = os.environ.copy()
        env["PYTHONPATH"] = f"{src_path}{os.pathsep}{code_dir}"
        env["PYTHONUNBUFFERED"] = "1"

        # Stream the subprocess output as it's produced instead of buffering
        # it all and printing it after the process exits. This gives
        # real-time feedback, especially when multiple HUCs/reaches are
        # processed concurrently via ProcessPoolExecutor.
        #
        # Read one character at a time and flush on '\n' *or* '\r' rather
        # than iterating the file object by line: tqdm progress bars (used
        # when -v/--verbose is passed) redraw using '\r' without a trailing
        # newline, so line-based iteration would silently swallow every
        # progress update until a real '\n' finally showed up.
        prefix = f"[{HUC_code}:{label}]" if label else f"[{HUC_code}]"
        process = subprocess.Popen(
            Command,
            cwd=tools_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        chunk = ""
        for char in iter(lambda: process.stdout.read(1), ""):
            chunk += char
            if char in ("\n", "\r"):
                print(f"{prefix} {chunk}", end="", flush=True)
                chunk = ""
        if chunk:
            print(f"{prefix} {chunk}", flush=True)
        process.stdout.close()
        returncode = process.wait()

        if returncode == 0:
            print(f"Inundation mapping for {HUC_code} completed successfully.", flush=True)

            if os.path.exists(inundation_file):
                shutil.move(inundation_file, inundation_dir)

            if depth and depth_file and os.path.exists(depth_file):
                shutil.move(depth_file, inundation_dir)

            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
        else:
            print(f"Failed to complete inundation mapping for {HUC_code}.", flush=True)

    finally:
        os.chdir(original_dir)


def runOWPHANDFIM(huc, depth=False):
    code_dir, data_dir, output_dir = setup_directories()
    discharge = glob.glob(os.path.join(data_dir, f"*{huc}*.csv"))
    for file in discharge:
        runfim(code_dir, output_dir, huc, file, depth=depth)
