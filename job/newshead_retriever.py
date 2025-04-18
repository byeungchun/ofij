import os
import sys
import time
import signal
import logging
import logging.handlers
import pandas as pd
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# -------------- PATH CONFIGURATION --------------
UTIL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '../util'))
sys.path.append(UTIL_DIR)

import kis_auth as ka
import kis_domstk as kb
# -------------- CONFIGURATION --------------

HOME = os.path.expanduser('~')
DESTDIR = os.path.join(HOME, 'data', 'news')
os.makedirs(DESTDIR, exist_ok=True)

DB_PATH = os.path.join(DESTDIR, 'news_database.duckdb')
TABLE_NAME = 'news_titles'
LOG_FILE = os.path.join(DESTDIR, 'news_processing.log')
PIDFILE = os.path.join(DESTDIR, 'news_daemon.pid')

DROP_COLS = [
    'iscd2', 'iscd3', 'iscd4', 'iscd5', 'iscd6',
    'iscd7', 'iscd8', 'iscd9', 'iscd10',
    'kor_isnm2', 'kor_isnm3', 'kor_isnm4',
    'kor_isnm5', 'kor_isnm6', 'kor_isnm7',
    'kor_isnm8', 'kor_isnm9', 'kor_isnm10'
]  # Example list

# ------------- INIT LOGGING --------------
def setup_logging(logfile):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    # Log rotation: 10 MB, keep last 5 files
    handler = logging.handlers.RotatingFileHandler(
        logfile, maxBytes=10*1024*1024, backupCount=5)
    formatter = logging.Formatter('%(asctime)s %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    # Also log to stderr
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger

logger = setup_logging(LOG_FILE)

# ------------- SIGNALS & GRACEFUL SHUTDOWN --------------
class ShutdownFlag:
    shutting_down = False
shutdown_flag = ShutdownFlag()

def handle_signal(signum, frame):
    logger.warning(f"Received signal {signum}. Shutting down gracefully...")
    shutdown_flag.shutting_down = True

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# ------------- PID FILE CHECK --------------
def write_pid(pidfile):
    pid = os.getpid()
    if os.path.exists(pidfile):
        logger.error(f"PID file {pidfile} exists. Daemon already running?")
        sys.exit(1)
    with open(pidfile, "w") as f:
        f.write(str(pid))

def remove_pid(pidfile):
    try:
        os.remove(pidfile)
    except Exception:
        pass

# ---------- MYSQL CONFIGURATION -------------
load_dotenv()  # automatically loads .env in current/parent dir

mysql_host = os.getenv('MYSQL_HOST')
mysql_db = os.getenv('MYSQL_DB')
mysql_user = os.getenv('MYSQL_USER')
mysql_pass = os.getenv('MYSQL_PASS')

MYSQL_URL = f"mysql+pymysql://{mysql_user}:{mysql_pass}@{mysql_host}/{mysql_db}"

# Table creation SQL for MySQL
create_table_sql = """
CREATE TABLE IF NOT EXISTS news_titles (
    cntt_usiq_srno VARCHAR(255) NOT NULL UNIQUE,
    news_ofer_entp_code VARCHAR(255),
    data_dt VARCHAR(32),
    data_tm VARCHAR(32),
    hts_pbnt_titl_cntt TEXT,
    news_lrdv_code VARCHAR(255),
    dorg VARCHAR(255),
    iscd1 VARCHAR(255),
    kor_isnm1 VARCHAR(255)
);
"""

# ------------- CORE DAEMON LOGIC --------------

MAX_RUNTIME_SECONDS = 23 * 60 * 60  # 23 hours

def main_loop():
    ka.auth()  # Authenticate before main loop

    # con = None
    total_rows_processed_today = 0
    BUFFER = []
    BUFFER_MAX_AGE = 300  # seconds (5 minutes)
    last_commit_time = time.time()
    start_time = time.time()

    engine = create_engine(MYSQL_URL)

    while not shutdown_flag.shutting_down:
        # ----- Check for auto-shutdown after MAX_RUNTIME_SECONDS -----
        if time.time() - start_time > MAX_RUNTIME_SECONDS:
            logger.info("Maximum runtime reached (23 hours). Triggering graceful shutdown to refresh API key.")
            shutdown_flag.shutting_down = True
            break  # exit the while loop to process buffer and cleanup
        # if con is None:
        #     try:
        #         engine = create_engine(MYSQL_URL)
        #         with engine.connect() as conn:
        #             conn.execute(text(create_table_sql))
        #         logger.info("Connected to MySQL and ensured table exists.")
        #     except Exception as e:
        #         logger.critical(f"Failed to connect to DuckDB: {e}", exc_info=True)
        #         time.sleep(60)
        #         continue

        yyyymmdd_api = datetime.now().strftime('%Y%m%d')
        hhmmss_api = datetime.now().strftime("%H%M%S").rjust(10, "0")

        try:
            news_data = []
            try:
                news_data = kb.get_news_titles(date_1=yyyymmdd_api, hour_1=hhmmss_api)
            except Exception as fetch_err:
                logger.error(f"Error fetching data for {yyyymmdd_api}@{hhmmss_api}: {fetch_err}")
                time.sleep(10)
                continue

            if news_data:
                news_chunk_df = pd.DataFrame(news_data)
                cols_to_drop_actual = [col for col in DROP_COLS if col in news_chunk_df.columns]
                if cols_to_drop_actual:
                    news_chunk_df.drop(cols_to_drop_actual, axis=1, inplace=True)

                if 'cntt_usiq_srno' not in news_chunk_df.columns:
                    logger.warning(f"'cntt_usiq_srno' missing in data chunk. Skipping insert.")
                else:
                    BUFFER.append(news_chunk_df)
                    total_rows_processed_today += len(news_chunk_df)
                    logger.info(f"Buffered {len(news_chunk_df)} rows for {yyyymmdd_api}@{hhmmss_api[-6:]}")

            now = time.time()
            # flush buffer if interval passed, or if shutting down
            if (now - last_commit_time >= BUFFER_MAX_AGE) or shutdown_flag.shutting_down:
                if BUFFER:
                    try:
                        batch_df = pd.concat(BUFFER, ignore_index=True)
                        batch_df.drop_duplicates(subset=['cntt_usiq_srno'], keep='first', inplace=True)
                        # Bulk insert to MySQL; will duplicate on unique error, so use 'ignore'
                        batch_df.to_sql('news_titles', engine, if_exists='append', index=False, method='multi')
                        logger.info(f"Committed batch of {len(batch_df)} rows to MySQL.")
                    except Exception as db_err:
                        logger.error(f"Error batch inserting rows: {db_err}", exc_info=True)
                    BUFFER.clear()
                last_commit_time = now

        except Exception as e:
            logger.error(f"Error in processing loop: {e}", exc_info=True)
            # if con:
            #     con.close()
            #     con = None
            time.sleep(60)

        # Sleep for a short interval (as before)
        for _ in range(20):
            if shutdown_flag.shutting_down:
                break
            time.sleep(1)

    # Final flush on shutdown
    if BUFFER:
        logger.info("Final flush of buffered data before exiting ...")
        try:
            batch_df = pd.concat(BUFFER, ignore_index=True)
            batch_df.drop_duplicates(subset=['cntt_usiq_srno'], keep='first', inplace=True)
            batch_df.to_sql('news_titles', engine, if_exists='append', index=False, method='multi')
            logger.info(f"Final commit: {len(batch_df)} rows.")
        except Exception as db_err:
            logger.error(f"Error on final batch insert: {db_err}", exc_info=True)
    logger.info(f"Daemon main loop exiting. Total rows processed today: {total_rows_processed_today}")

# ------------- DAEMON ENTRYPOINT --------------
def main():
    write_pid(PIDFILE)
    logger.info(f"Starting News Daemon. PID={os.getpid()}")
    try:
        main_loop()
    finally:
        remove_pid(PIDFILE)
        logger.info("PID file removed. Exiting.")

if __name__ == "__main__":
    main()