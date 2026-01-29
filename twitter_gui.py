import pandas as pd
import time
import random
import os
import sys
import shutil
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright, TimeoutError, Error as PlaywrightError
from concurrent.futures import ThreadPoolExecutor, as_completed

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

PKT = timezone(timedelta(hours=5))

# ────────────────────────────────────────────────
# GLOBAL CONTROL VARIABLES
# ────────────────────────────────────────────────
stop_event = threading.Event()
running = False
was_stopped = False       # flag to detect real STOP click
all_results = []          # for partial save
executor = None           # reference to ThreadPoolExecutor

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
def process_accounts(account_batch, time_window_min, max_tweets, run_output_dir, results_list):
    if stop_event.is_set():
        return

    browser = None
    context = None
    page = None

    try:
        with sync_playwright() as p:
            # Use bundled browser path if running as exe
            launch_kwargs = {"headless": False}
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
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()

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
                        page.goto(url, timeout=90000, wait_until="domcontentloaded")
                        if stop_event.is_set():
                            break
                        time.sleep(4 + random.uniform(1, 2))
                        if stop_event.is_set():
                            break

                        page.mouse.wheel(0, 1200)
                        time.sleep(2)
                        if stop_event.is_set():
                            break

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

                                tweet_link = time_el.evaluate("el => el.closest('a').href")
                                handle = tweet_link.split("/")[3]
                                tweet_id = tweet_link.split("/")[-1]

                                screenshot_file = os.path.join(
                                    run_output_dir,
                                    f"{handle}_{tweet_id}.png"
                                )

                                abs_path = os.path.abspath(screenshot_file).replace(os.sep, "/")
                                image_link = f'=HYPERLINK("file:///{abs_path}", "View Image")'

                                tweet.screenshot(path=screenshot_file)

                                results_list.append({
                                    "account_handle": handle,
                                    "tweet_link": tweet_link,
                                    "image": image_link,
                                    "tweet_time_pkt": utc_to_pkt(tweet_time_utc),
                                    "screenshot_taken_pkt": utc_to_pkt(datetime.now(timezone.utc))
                                })

                                log(f"✅ {handle} | {tweet_link}")

                            except Exception as e:
                                log(f"  ┖─ tweet error: {e}")
                                continue

                        time.sleep(3 + random.uniform(1, 2))
                        if stop_event.is_set():
                            break

                        break  # success

                    except (TimeoutError, PlaywrightError) as e:
                        retry_count += 1
                        log(f"⚠️ Retry {retry_count}/{max_retries+1} for {url}: {str(e)}")

                        if retry_count > max_retries:
                            log(f"⚠️ Giving up after retries: {url}")
                            break

                        if page.is_closed():
                            page = context.new_page()
                        else:
                            page.reload(timeout=30000, wait_until="domcontentloaded")

            if browser is not None:
                browser.close()

    except Exception as e:
        log(f"🔥 Worker crashed but recovered: {e}")
        if browser is not None:
            try:
                browser.close()
            except:
                pass

# ────────────────────────────────────────────────
# MAIN AUTOMATION THREAD
# ────────────────────────────────────────────────
def run_automation():
    global running, all_results, executor, was_stopped
    running = True
    was_stopped = False   # Reset flag on every new run
    btn_start.config(state="disabled")
    btn_stop.config(state="normal")
    all_results = []

    try:
        if not os.path.exists(EXCEL_PATH):
            messagebox.showerror("Error", "OSINT_Links.xlsx not found!")
            stop()
            return

        df = pd.read_excel(EXCEL_PATH)
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

        run_time = datetime.now().strftime("%Y-%m-%d_%H-%M")
        run_output_dir = os.path.join(BASE_OUTPUT_DIR, run_time)
        os.makedirs(run_output_dir, exist_ok=True)

        excel_output = os.path.join(BASE_DIR, f"captured_tweets_{run_time}.xlsx")

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
                    all_results
                )
            )

        for future in as_completed(futures):
            if stop_event.is_set():
                break
            try:
                future.result()
            except Exception as e:
                log(f"Future error: {e}")

        if all_results and not stop_event.is_set():
            save_excel(all_results, excel_output)
            log(f"\nSuccess! Excel saved → {excel_output}")
        elif stop_event.is_set():
            log("\nRun stopped by user.")
        else:
            log("\nNo recent tweets captured.")

    except Exception as e:
        log(f"\nFatal error: {e}")
        messagebox.showerror("Error", str(e))

    finally:
        stop()

def save_excel(results, excel_path):
    if not results:
        log("No screenshots taken – no Excel saved.")
        return

    final_df = pd.DataFrame(results)
    with pd.ExcelWriter(excel_path, engine="xlsxwriter") as writer:
        final_df.to_excel(writer, index=False, sheet_name="Captured Tweets")
        ws = writer.sheets["Captured Tweets"]
        ws.freeze_panes(1, 0)
        for col_idx, col in enumerate(final_df.columns):
            width = min(max(final_df[col].astype(str).map(len).max(), len(col)) + 3, 50)
            ws.set_column(col_idx, col_idx, width)
    log(f"Partial/Full Excel saved → {excel_path} ({len(results)} items)")

def stop():
    global running, executor, was_stopped
    if running:
        stop_event.set()
        log("STOP clicked — terminating remaining operations...")

        time.sleep(3.0)

        if executor is not None:
            log("Forcing thread pool shutdown...")
            executor.shutdown(wait=False, cancel_futures=True)
            executor = None
            log("Thread pool shutdown done.")

        # Save partial ONLY if STOP was actually clicked (not on normal finish)
        if was_stopped:
            if all_results:
                run_time = datetime.now().strftime("%Y-%m-%d_%H-%M")
                excel_output = os.path.join(BASE_DIR, f"captured_tweets_{run_time}_partial.xlsx")
                save_excel(all_results, excel_output)
            else:
                log("No data captured – no partial Excel saved.")
        # else: normal finish — do nothing here (normal Excel already saved)

        running = False
        btn_start.config(state="normal")
        btn_stop.config(state="disabled")
        stop_event.clear()
        was_stopped = False  # Reset for next run

        log("STOP complete. All threads terminated. Ready for new run.")

# ────────────────────────────────────────────────
# GUI
# ────────────────────────────────────────────────
root = tk.Tk()
root.title("Twitter/X Screenshot Tool")
root.geometry("780x620")
root.resizable(False, False)

# ── Header ──
tk.Label(root, text="Twitter/X Recent Tweets Screenshot Tool", font=("Segoe UI", 14, "bold")).pack(pady=10)

# ── Settings frame ──
frame_settings = ttk.LabelFrame(root, text=" Settings ", padding=10)
frame_settings.pack(fill="x", padx=12, pady=(0,10))

row = 0
tk.Label(frame_settings, text="Time window (minutes, 1–1440):").grid(row=row, column=0, sticky="e", padx=5, pady=4)
entry_time = tk.Entry(frame_settings, width=8)
entry_time.insert(0, str(DEFAULT_TIME_WINDOW_MIN))
entry_time.grid(row=row, column=1, sticky="w")
row += 1

tk.Label(frame_settings, text="Max tweets per account:").grid(row=row, column=0, sticky="e", padx=5, pady=4)
entry_tweets = tk.Entry(frame_settings, width=8)
entry_tweets.insert(0, str(DEFAULT_MAX_TWEETS_PER_ACC))
entry_tweets.grid(row=row, column=1, sticky="w")
row += 1

tk.Label(frame_settings, text="Max parallel workers:").grid(row=row, column=0, sticky="e", padx=5, pady=4)
entry_workers = tk.Entry(frame_settings, width=8)
entry_workers.insert(0, str(DEFAULT_MAX_WORKERS))
entry_workers.grid(row=row, column=1, sticky="w")
tk.Label(frame_settings, text="(keep ≤ 4)").grid(row=row, column=2, sticky="w", padx=8)

# ── Buttons ──
frame_buttons = tk.Frame(root)
frame_buttons.pack(pady=10)

btn_start = tk.Button(frame_buttons, text="START", font=("Segoe UI", 11, "bold"),
                      bg="#4CAF50", fg="white", width=12,
                      command=lambda: threading.Thread(target=run_automation, daemon=True).start())
btn_start.pack(side="left", padx=20)

btn_stop = tk.Button(frame_buttons, text="STOP", font=("Segoe UI", 11, "bold"),
                     bg="#f44336", fg="white", width=12,
                     command=lambda: [globals().__setitem__('was_stopped', True), stop()],
                     state="disabled")
btn_stop.pack(side="left", padx=20)

# ── Log area ── (must be created before calling ensure_excel_template)
text_log = scrolledtext.ScrolledText(root, height=22, width=92, font=("Consolas", 10))
text_log.pack(padx=12, pady=8, fill="both", expand=True)

# Initial log
text_log.insert(tk.END, "GUI ready. Adjust settings and press START.\n\n")

# NOW safe to call ensure_excel_template (text_log exists)
ensure_excel_template()

root.protocol("WM_DELETE_WINDOW", lambda: [stop(), root.destroy()])  # graceful exit
root.mainloop()