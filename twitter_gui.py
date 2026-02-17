import pandas as pd
import time
import random
import os
import sys
import shutil
import threading
import tkinter as tk
import warnings
import contextlib
import io
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright, TimeoutError, Error as PlaywrightError
from concurrent.futures import ThreadPoolExecutor, as_completed

# Suppress greenlet threading warnings that occur during forced browser shutdown
warnings.filterwarnings("ignore", message=".*greenlet.*")
warnings.filterwarnings("ignore", category=RuntimeWarning)

# Context manager to suppress stderr (used during forced browser shutdown)
@contextlib.contextmanager
def suppress_stderr():
    """Temporarily suppress stderr to hide Playwright async errors during forced shutdown"""
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        yield
    finally:
        sys.stderr = old_stderr

# ────────────────────────────────────────────────
# Force Playwright to use bundled browsers in exe
# ────────────────────────────────────────────────
if getattr(sys, 'frozen', False):
    browsers_path = os.path.join(sys._MEIPASS, "playwright", "driver", "package", ".local-browsers")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path

# ────────────────────────────────────────────────
# CONFIG DEFAULTS (can be changed in GUI)
# ────────────────────────────────────────────────
BASE_DIR = os.getcwd()
EXCEL_PATH = os.path.join(BASE_DIR, "OSINT_Links.xlsx")
BASE_OUTPUT_DIR = os.path.join(BASE_DIR, "screenshots")

DEFAULT_TIME_WINDOW_MIN = 60
MIN_TIME_WINDOW_MIN = 1
MAX_TIME_WINDOW_MIN = 1440  # 24 hours

DEFAULT_MAX_TWEETS_PER_ACC = 5
DEFAULT_MAX_WORKERS = 3
HEADLESS_MODE = True  # Default to headless for stability in some environments

PKT = timezone(timedelta(hours=5))

# ────────────────────────────────────────────────
# GLOBAL CONTROL VARIABLES
# ────────────────────────────────────────────────
stop_event = threading.Event()
running = False
was_stopped = False       # flag to detect real STOP click
all_results = []          # for partial save
executor = None           # reference to ThreadPoolExecutor
current_run_time = None   # current run timestamp
current_excel_output = None  # current excel output path
active_browsers = []      # track all active browser instances for cleanup
results_lock = threading.Lock()  # thread-safe access to all_results

# ────────────────────────────────────────────────
# BUNDLED RESOURCE HELPERS (for PyInstaller --onefile)
# ────────────────────────────────────────────────
def get_bundled_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller exe"""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def ensure_excel_template():
    """Auto-copy bundled OSINT_Links.xlsx if missing in current directory"""
    target_excel = os.path.join(BASE_DIR, "OSINT_Links.xlsx")

    if os.path.exists(target_excel):
        log("Using existing OSINT_Links.xlsx in current folder.")
        return

    bundled_excel = get_bundled_path("OSINT_Links.xlsx")

    if not os.path.exists(bundled_excel):
        messagebox.showerror("Critical Error", "Embedded Excel template not found.\nPlease rebuild the exe.")
        sys.exit(1)

    try:
        shutil.copyfile(bundled_excel, target_excel)
        log("First run: Created default OSINT_Links.xlsx template in current folder.")
        messagebox.showinfo(
            "First Run",
            "Default template created.\n\n"
            "Please edit OSINT_Links.xlsx with your Twitter/X profile links\n"
            "and press START again."
        )
    except Exception as e:
        messagebox.showerror("Error", f"Could not create template Excel:\n{e}")
        sys.exit(1)

# ────────────────────────────────────────────────
# HELPERS
# ────────────────────────────────────────────────
def minutes_ago(tweet_time_utc):
    return (datetime.now(timezone.utc) - tweet_time_utc).total_seconds() / 60

def utc_to_pkt(utc_dt):
    return utc_dt.astimezone(PKT).strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    if text_log:
        text_log.insert(tk.END, msg + "\n")
        text_log.see(tk.END)
    print(msg)

# ────────────────────────────────────────────────
# CORE WORKER — added frequent stop checks
# ────────────────────────────────────────────────
def process_accounts(account_batch, time_window_min, max_tweets, run_output_dir, results_list, headless=True):
    if stop_event.is_set():
        return

    browser = None
    context = None
    page = None

    try:
        with sync_playwright() as p:
            if stop_event.is_set():
                return

            # Use bundled browser path if running as exe
            launch_kwargs = {"headless": headless}
            if getattr(sys, 'frozen', False):
                chrome_path = os.path.join(
                    sys._MEIPASS,
                    "playwright",
                    "driver",
                    "package",
                    ".local-browsers",
                    "chromium-1200",  # your exact version
                    "chrome-win64",
                    "chrome.exe"
                )
                launch_kwargs["executable_path"] = chrome_path

            browser = p.chromium.launch(**launch_kwargs)
            # Register browser for cleanup
            global active_browsers
            active_browsers.append(browser)

            if stop_event.is_set():
                if browser: browser.close()
                return

            context = browser.new_context(viewport={"width": 1280, "height": 900})
            if stop_event.is_set():
                if browser: browser.close()
                return

            page = context.new_page()
            if stop_event.is_set():
                if browser: browser.close()
                return

            for url in account_batch:
                if stop_event.is_set():
                    break

                log(f"[Worker] Opening: {url}")

                retry_count = 0
                max_retries = 2

                while retry_count <= max_retries:
                    if stop_event.is_set():
                        break

                    try:
                        if stop_event.is_set(): break
                        page.goto(url, timeout=90000, wait_until="domcontentloaded")
                        if stop_event.is_set(): break

                        # Extra wait for dynamic content
                        for _ in range(4):
                            if stop_event.is_set(): break
                            time.sleep(0.5)

                        if stop_event.is_set(): break

                        # Split sleep into smaller chunks for faster stop response
                        for _ in range(8):
                            if stop_event.is_set():
                                break
                            time.sleep(0.5)

                        if stop_event.is_set(): break
                        page.mouse.wheel(0, 1500)
                        if stop_event.is_set(): break

                        # Split sleep into smaller chunks
                        for _ in range(6):
                            if stop_event.is_set():
                                break
                            time.sleep(0.5)

                        if stop_event.is_set(): break
                        try:
                            page.wait_for_selector("article", timeout=10000)
                        except:
                            log(f"  ┖─ No tweets found on page for {url}")
                            break
                        if stop_event.is_set(): break

                        tweets = page.locator("article")
                        tweet_count = tweets.count()

                        for i in range(min(tweet_count, max_tweets)):
                            if stop_event.is_set():
                                break

                            try:
                                tweet = tweets.nth(i)

                                if tweet.locator("text=Pinned").count() > 0:
                                    continue

                                time_el = tweet.locator("time").first
                                dt_attr = time_el.get_attribute("datetime")
                                if not dt_attr:
                                    continue

                                tweet_time_utc = datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))

                                if minutes_ago(tweet_time_utc) > time_window_min:
                                    continue

                                if stop_event.is_set(): break
                                tweet_link = time_el.evaluate("el => el.closest('a').href")
                                if stop_event.is_set(): break
                                handle = tweet_link.split("/")[3]
                                tweet_id = tweet_link.split("/")[-1]

                                screenshot_file = os.path.join(
                                    run_output_dir,
                                    f"{handle}_{tweet_id}.png"
                                )

                                abs_path = os.path.abspath(screenshot_file).replace(os.sep, "/")
                                image_link = f'=HYPERLINK("file:///{abs_path}", "View Image")'

                                if stop_event.is_set(): break
                                tweet.screenshot(path=screenshot_file)
                                if stop_event.is_set(): break

                                # Thread-safe result appending
                                with results_lock:
                                    results_list.append({
                                        "account_handle": handle,
                                        "tweet_link": tweet_link,
                                        "image": image_link,
                                        "tweet_time_pkt": utc_to_pkt(tweet_time_utc),
                                        "screenshot_taken_pkt": utc_to_pkt(datetime.now(timezone.utc))
                                    })

                                log(f"✅ {handle} | {tweet_link}")

                            except Exception as e:
                                if stop_event.is_set():
                                    break
                                log(f"  ┖─ tweet error: {e}")
                                continue

                        # Split sleep into smaller chunks
                        for _ in range(8):
                            if stop_event.is_set():
                                break
                            time.sleep(0.5)

                        if stop_event.is_set():
                            break

                        break  # success

                    except (TimeoutError, PlaywrightError) as e:
                        if stop_event.is_set():
                            break

                        retry_count += 1
                        log(f"⚠️ Retry {retry_count}/{max_retries+1} for {url}: {str(e)}")

                        if retry_count > max_retries:
                            log(f"⚠️ Giving up after retries: {url}")
                            break

                        if stop_event.is_set():
                            break

                        try:
                            if page.is_closed():
                                page = context.new_page()
                            else:
                                page.reload(timeout=30000, wait_until="domcontentloaded")
                        except Exception:
                            # If page/context closed during stop, just break
                            if stop_event.is_set():
                                break
                            raise

            # Ensure browser is closed immediately when stopping
            if browser is not None:
                with suppress_stderr():
                    try:
                        browser.close()
                        if browser in active_browsers:
                            active_browsers.remove(browser)
                    except Exception:
                        # Suppress errors - expected during forced shutdown
                        pass

    except Exception as e:
        if not stop_event.is_set():
            log(f"🔥 Worker crashed but recovered: {e}")
        if browser is not None:
            with suppress_stderr():
                try:
                    browser.close()
                    if browser in active_browsers:
                        active_browsers.remove(browser)
                except Exception:
                    pass
    finally:
        # Force close browser on stop
        if browser is not None:
            with suppress_stderr():
                try:
                    browser.close()
                    if browser in active_browsers:
                        active_browsers.remove(browser)
                except Exception:
                    pass

# ────────────────────────────────────────────────
# MAIN AUTOMATION THREAD
# ────────────────────────────────────────────────
def run_automation():
    global running, all_results, executor, was_stopped, current_run_time, current_excel_output, active_browsers
    running = True
    was_stopped = False   # Reset flag on every new run
    active_browsers = []  # Reset browser list
    btn_start.config(state="disabled")
    btn_stop.config(state="normal")
    all_results = []

    try:
        if not os.path.exists(EXCEL_PATH):
            messagebox.showerror("Error", f"Excel file not found at:\n{EXCEL_PATH}\n\nPlease ensure it exists.")
            stop()
            return

        try:
            df = pd.read_excel(EXCEL_PATH)
        except Exception as e:
            messagebox.showerror("Error", f"Could not read Excel file:\n{e}")
            stop()
            return

        links = df.iloc[:, 0].dropna().tolist()
        if not links:
            messagebox.showwarning("Warning", "No links found in Excel.")
            stop()
            return

        try:
            time_window_str = entry_time.get().strip()
            time_window = int(time_window_str)
            if time_window < MIN_TIME_WINDOW_MIN or time_window > MAX_TIME_WINDOW_MIN:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Input", f"Time window must be a number between {MIN_TIME_WINDOW_MIN} and {MAX_TIME_WINDOW_MIN} minutes.")
            stop()
            return

        max_tweets = int(entry_tweets.get())
        max_workers = int(entry_workers.get())
        headless = var_headless.get()

        run_time = datetime.now().strftime("%Y-%m-%d_%H-%M")
        current_run_time = run_time  # Store globally for stop function
        run_output_dir = os.path.join(BASE_OUTPUT_DIR, run_time)
        os.makedirs(run_output_dir, exist_ok=True)

        excel_output = os.path.join(BASE_DIR, f"captured_tweets_{run_time}.xlsx")
        current_excel_output = excel_output  # Store globally for stop function

        log(f"Starting run → {run_time}")
        log(f"Time window: {time_window} minutes")
        log(f"Output folder: {run_output_dir}")
        log(f"Will save Excel → {excel_output}")

        batches = [links[i::max_workers] for i in range(max_workers)]

        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures = []
        for batch in batches:
            if stop_event.is_set():
                break
            futures.append(
                executor.submit(
                    process_accounts,
                    batch,
                    time_window,
                    max_tweets,
                    run_output_dir,
                    all_results,
                    headless
                )
            )

        # Wait for all futures to complete or stop event
        max_wait_after_stop = 5  # seconds to wait for graceful shutdown
        stop_time = None

        while any(not f.done() for f in futures):
            if stop_event.is_set():
                if stop_time is None:
                    log("🛑 STOP button detected: Cancelling remaining tasks...")
                    stop_time = time.time()
                    # Try to cancel all pending tasks
                    for f in futures:
                        f.cancel()
                    # Force cleanup browsers immediately
                    log("🛑 Forcing browser cleanup...")
                    cleanup_browsers()
                elif time.time() - stop_time > max_wait_after_stop:
                    # Force exit if tasks don't complete
                    log("⚠️ Force terminating remaining tasks...")
                    break
            time.sleep(0.2)  # Faster polling for better responsiveness

        # Ensure browsers are cleaned up
        if stop_event.is_set():
            cleanup_browsers()

        # Save results based on completion status (thread-safe)
        with results_lock:
            result_count = len(all_results)
            has_results = result_count > 0

        if has_results:
            if stop_event.is_set():
                # User stopped - save as partial
                partial_excel = os.path.join(BASE_DIR, f"captured_tweets_{run_time}_partial.xlsx")
                if save_excel(all_results, partial_excel):
                    log(f"\n⚠️ Stopped by user. Partial results saved → {partial_excel}")
                    log(f"📊 Captured {result_count} tweet(s) before stopping.")
                else:
                    log(f"\n⚠️ Stopped by user. Excel save failed but {result_count} tweet(s) were captured.")
            else:
                # Normal completion - save full results
                if save_excel(all_results, excel_output):
                    log(f"\n✅ Success! Excel saved → {excel_output}")
                    log(f"📊 Total tweets captured: {result_count}")
                else:
                    log(f"\n⚠️ Excel save failed but {result_count} tweet(s) were captured.")
        else:
            if stop_event.is_set():
                log("\n⚠️ Stopped by user. No data captured.")
            else:
                log("\nNo recent tweets captured.")

    except Exception as e:
        log(f"\nFatal error: {e}")
        messagebox.showerror("Error", str(e))

    finally:
        cleanup_after_run()

def save_excel(results, excel_path):
    """Save results to Excel file with error handling and retry logic"""
    if not results:
        log("No screenshots taken – no Excel saved.")
        return False

    # Thread-safe copy of results
    with results_lock:
        results_copy = list(results)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            final_df = pd.DataFrame(results_copy)

            # Ensure directory exists
            os.makedirs(os.path.dirname(excel_path) if os.path.dirname(excel_path) else '.', exist_ok=True)

            with pd.ExcelWriter(excel_path, engine="xlsxwriter") as writer:
                final_df.to_excel(writer, index=False, sheet_name="Captured Tweets")
                ws = writer.sheets["Captured Tweets"]
                ws.freeze_panes(1, 0)
                for col_idx, col in enumerate(final_df.columns):
                    width = min(max(final_df[col].astype(str).map(len).max(), len(col)) + 3, 50)
                    ws.set_column(col_idx, col_idx, width)
            log(f"✅ Excel saved → {excel_path} ({len(results_copy)} items)")
            return True
        except ImportError as e:
            log(f"❌ ERROR: Missing xlsxwriter library. Install it with: pip install xlsxwriter")
            log(f"   Details: {e}")
            messagebox.showerror("Missing Dependency", "xlsxwriter is not installed.\n\nPlease run:\npip install xlsxwriter")
            return False
        except Exception as e:
            if attempt < max_retries - 1:
                log(f"⚠️ Excel save attempt {attempt + 1} failed, retrying... ({e})")
                time.sleep(0.5)
            else:
                log(f"❌ ERROR: Could not save Excel file after {max_retries} attempts: {e}")
                # Don't show messagebox if stopping
                if not stop_event.is_set():
                    messagebox.showerror("Excel Save Error", f"Could not save Excel file:\n{e}")
                return False
    return False

def cleanup_browsers():
    """Force close all active browser instances"""
    global active_browsers
    if active_browsers:
        log(f"Closing {len(active_browsers)} active browser(s)...")
        # Suppress stderr to hide Playwright async callback errors during forced shutdown
        with suppress_stderr():
            for browser in active_browsers[:]:  # Copy list to avoid modification during iteration
                try:
                    browser.close()
                except Exception:
                    # Suppress errors - expected during forced shutdown
                    pass
        active_browsers.clear()
        log("All browsers closed.")

def stop():
    """Immediately stop all operations - called when user clicks STOP button"""
    global was_stopped
    if running:
        was_stopped = True  # Mark that STOP was clicked
        stop_event.set()
        log("\n" + "🛑" * 30)
        log("🛑 STOP button pressed — halting all operations immediately...")
        log("🛑" * 30 + "\n")
        btn_stop.config(state="disabled", text="Stopping...")

        # Immediately cleanup browsers
        cleanup_browsers()

def cleanup_after_run():
    """Cleanup function called after run completes or is stopped"""
    global running, executor, was_stopped, active_browsers

    # Force cleanup any remaining browsers
    cleanup_browsers()

    if executor is not None:
        log("Shutting down thread pool...")
        # Force immediate shutdown without waiting
        executor.shutdown(wait=False, cancel_futures=True)
        executor = None
        log("Thread pool terminated.")

    # Reset state
    running = False
    was_stopped = False
    stop_event.clear()
    active_browsers.clear()

    # Re-enable UI
    btn_start.config(state="normal")
    btn_stop.config(state="disabled", text="■ STOP")

    log("─" * 60)
    log("Ready for new run.\n")

# ────────────────────────────────────────────────
# GUI
# ────────────────────────────────────────────────
root = tk.Tk()
root.title("Twitter/X Screenshot Tool")
root.geometry("900x700")
root.resizable(False, False)

# Configure color scheme
BG_COLOR = "#f5f6fa"
HEADER_BG = "#2c3e50"
HEADER_FG = "#ffffff"
FRAME_BG = "#ffffff"
LABEL_FG = "#2c3e50"
ENTRY_BG = "#ffffff"
BTN_START_BG = "#27ae60"
BTN_START_HOVER = "#229954"
BTN_STOP_BG = "#e74c3c"
BTN_STOP_HOVER = "#c0392b"
LOG_BG = "#2c3e50"
LOG_FG = "#ecf0f1"

root.configure(bg=BG_COLOR)

# ── Header Frame ──
header_frame = tk.Frame(root, bg=HEADER_BG, height=70)
header_frame.pack(fill="x", padx=0, pady=0)
header_frame.pack_propagate(False)

header_label = tk.Label(
    header_frame,
    text="Twitter/X Screenshot Tool",
    font=("Segoe UI", 18, "bold"),
    bg=HEADER_BG,
    fg=HEADER_FG
)
header_label.pack(pady=20)

subtitle_label = tk.Label(
    header_frame,
    text="Capture recent tweets from monitored accounts",
    font=("Segoe UI", 10),
    bg=HEADER_BG,
    fg="#95a5a6"
)
subtitle_label.pack(pady=0)

# ── Main Container ──
main_container = tk.Frame(root, bg=BG_COLOR)
main_container.pack(fill="both", expand=True, padx=15, pady=15)

# ── Settings Frame ──
frame_settings = tk.LabelFrame(
    main_container,
    text=" ⚙ Configuration Settings ",
    font=("Segoe UI", 11, "bold"),
    bg=FRAME_BG,
    fg=LABEL_FG,
    relief="solid",
    borderwidth=1,
    padx=20,
    pady=15
)
frame_settings.pack(fill="x", pady=(0, 15))

# Settings grid with improved spacing and alignment
settings_grid = tk.Frame(frame_settings, bg=FRAME_BG)
settings_grid.pack(fill="x", padx=10, pady=5)

row = 0

# Time Window
lbl_time = tk.Label(
    settings_grid,
    text="Time Window (minutes):",
    font=("Segoe UI", 10),
    bg=FRAME_BG,
    fg=LABEL_FG,
    anchor="w"
)
lbl_time.grid(row=row, column=0, sticky="w", padx=5, pady=8)

entry_time = tk.Entry(
    settings_grid,
    width=12,
    font=("Segoe UI", 10),
    relief="solid",
    borderwidth=1
)
entry_time.insert(0, str(DEFAULT_TIME_WINDOW_MIN))
entry_time.grid(row=row, column=1, sticky="w", padx=10)

lbl_time_hint = tk.Label(
    settings_grid,
    text="(Range: 1–1440 minutes)",
    font=("Segoe UI", 9),
    bg=FRAME_BG,
    fg="#7f8c8d"
)
lbl_time_hint.grid(row=row, column=2, sticky="w", padx=5)
row += 1

# Max Tweets
lbl_tweets = tk.Label(
    settings_grid,
    text="Max Tweets per Account:",
    font=("Segoe UI", 10),
    bg=FRAME_BG,
    fg=LABEL_FG,
    anchor="w"
)
lbl_tweets.grid(row=row, column=0, sticky="w", padx=5, pady=8)

entry_tweets = tk.Entry(
    settings_grid,
    width=12,
    font=("Segoe UI", 10),
    relief="solid",
    borderwidth=1
)
entry_tweets.insert(0, str(DEFAULT_MAX_TWEETS_PER_ACC))
entry_tweets.grid(row=row, column=1, sticky="w", padx=10)

lbl_tweets_hint = tk.Label(
    settings_grid,
    text="(Number of tweets to capture)",
    font=("Segoe UI", 9),
    bg=FRAME_BG,
    fg="#7f8c8d"
)
lbl_tweets_hint.grid(row=row, column=2, sticky="w", padx=5)
row += 1

# Max Workers
lbl_workers = tk.Label(
    settings_grid,
    text="Parallel Workers:",
    font=("Segoe UI", 10),
    bg=FRAME_BG,
    fg=LABEL_FG,
    anchor="w"
)
lbl_workers.grid(row=row, column=0, sticky="w", padx=5, pady=8)

entry_workers = tk.Entry(
    settings_grid,
    width=12,
    font=("Segoe UI", 10),
    relief="solid",
    borderwidth=1
)
entry_workers.insert(0, str(DEFAULT_MAX_WORKERS))
entry_workers.grid(row=row, column=1, sticky="w", padx=10)

lbl_workers_hint = tk.Label(
    settings_grid,
    text="(Recommended: ≤ 4 workers)",
    font=("Segoe UI", 9),
    bg=FRAME_BG,
    fg="#7f8c8d"
)
lbl_workers_hint.grid(row=row, column=2, sticky="w", padx=5)
row += 1

# Headless Mode Checkbox
var_headless = tk.BooleanVar(value=HEADLESS_MODE)
check_headless = tk.Checkbutton(
    settings_grid,
    text="Run in Headless Mode (browser hidden)",
    variable=var_headless,
    font=("Segoe UI", 10),
    bg=FRAME_BG,
    fg=LABEL_FG,
    activebackground=FRAME_BG,
    selectcolor=FRAME_BG
)
check_headless.grid(row=row, column=0, columnspan=3, sticky="w", padx=5, pady=8)

# ── Control Buttons Frame ──
frame_buttons = tk.Frame(main_container, bg=BG_COLOR)
frame_buttons.pack(pady=15)

btn_start = tk.Button(
    frame_buttons,
    text="▶ START",
    font=("Segoe UI", 12, "bold"),
    bg=BTN_START_BG,
    fg="white",
    width=15,
    height=2,
    relief="flat",
    cursor="hand2",
    command=lambda: threading.Thread(target=run_automation, daemon=True).start()
)
btn_start.pack(side="left", padx=15)

# Hover effects for START button
def on_start_enter(e):
    if btn_start['state'] == 'normal':
        btn_start['bg'] = BTN_START_HOVER

def on_start_leave(e):
    if btn_start['state'] == 'normal':
        btn_start['bg'] = BTN_START_BG

btn_start.bind("<Enter>", on_start_enter)
btn_start.bind("<Leave>", on_start_leave)

btn_stop = tk.Button(
    frame_buttons,
    text="■ STOP",
    font=("Segoe UI", 12, "bold"),
    bg=BTN_STOP_BG,
    fg="white",
    width=15,
    height=2,
    relief="flat",
    cursor="hand2",
    command=stop,
    state="disabled"
)
btn_stop.pack(side="left", padx=15)

# Hover effects for STOP button
def on_stop_enter(e):
    if btn_stop['state'] == 'normal':
        btn_stop['bg'] = BTN_STOP_HOVER

def on_stop_leave(e):
    if btn_stop['state'] == 'normal':
        btn_stop['bg'] = BTN_STOP_BG

btn_stop.bind("<Enter>", on_stop_enter)
btn_stop.bind("<Leave>", on_stop_leave)

# ── Log Frame ──
log_frame = tk.LabelFrame(
    main_container,
    text=" 📋 Activity Log ",
    font=("Segoe UI", 11, "bold"),
    bg=FRAME_BG,
    fg=LABEL_FG,
    relief="solid",
    borderwidth=1,
    padx=5,
    pady=5
)
log_frame.pack(fill="both", expand=True, pady=(0, 0))

# ── Log area ── (must be created before calling ensure_excel_template)
text_log = scrolledtext.ScrolledText(
    log_frame,
    height=18,
    width=100,
    font=("Consolas", 9),
    bg=LOG_BG,
    fg=LOG_FG,
    insertbackground=LOG_FG,
    relief="flat",
    padx=10,
    pady=10
)
text_log.pack(fill="both", expand=True, padx=5, pady=5)

# Initial log
text_log.insert(tk.END, "GUI ready. Adjust settings and press START.\n\n")

# NOW safe to call ensure_excel_template (text_log exists)
ensure_excel_template()

root.protocol("WM_DELETE_WINDOW", lambda: [stop(), root.destroy()])  # graceful exit
root.mainloop()