import os
import sys
import time
import signal
import logging
import logging.handlers
import pandas as pd
from datetime import datetime
import duckdb

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

try:
    con = duckdb.connect(DB_PATH)
    logger.info(f"Successfully connected to DuckDB: {DB_PATH}")
except duckdb.Error as e:
    logger.error(f"Error connecting to DuckDB: {e}", exc_info=True)
    con = None  # Ensure con is None if connection fails


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

# ------------- DATABASE TABLE SCHEMA --------------
CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    "cntt_usiq_srno"        VARCHAR NOT NULL UNIQUE,
    "news_ofer_entp_code"   VARCHAR,
    "data_dt"               VARCHAR(8),
    "data_tm"               VARCHAR(6),
    "hts_pbnt_titl_cntt"    VARCHAR,
    "news_lrdv_code"        VARCHAR,
    "dorg"                  VARCHAR,
    "iscd1"                 VARCHAR,
    "kor_isnm1"             VARCHAR
);
"""

# ------------- CORE DAEMON LOGIC --------------


def get_connection():
    global con
    if con is not None:
        return con
    try:
        con = duckdb.connect(DB_PATH)
        logger.info(f"Connected to DuckDB: {DB_PATH}")
        con.execute(CREATE_TABLE_SQL)
        return con
    except Exception as e:
        logger.error(f"Failed to connect to DuckDB: {e}", exc_info=True)
        con = None
        return None

def main_loop():
    global con
    total_rows_processed_today = 0
    BUFFER = []
    BUFFER_MAX_AGE = 300  # seconds (5 minutes)
    last_commit_time = time.time()
    start_time = time.time()

    while not shutdown_flag.shutting_down:
        try:
            # ... all fetch logic here (unchanged) ...
            
            now = time.time()
            if (now - last_commit_time >= BUFFER_MAX_AGE) or shutdown_flag.shutting_down:
                if BUFFER:
                    con = get_connection()
                    if con is None:
                        logger.error("No DB connection for batch insert; will retry on next attempt.")
                        # Don't clear BUFFER; keep for next try
                        time.sleep(10)
                        continue
                    try:
                        batch_df = pd.concat(BUFFER, ignore_index=True)
                        con.begin()
                        con.register('batch_df', batch_df)
                        con.execute(f"INSERT OR IGNORE INTO {TABLE_NAME} SELECT * FROM batch_df")
                        con.unregister('batch_df')
                        con.commit()
                        con.execute('CHECKPOINT')
                        logger.info(f"Committed batch of {len(batch_df)} rows to DuckDB.")
                        BUFFER.clear()  # Clear only after success!
                        last_commit_time = now
                    except Exception as db_err:
                        logger.error(f"Error batch inserting rows: {db_err}", exc_info=True)
                        try:
                            con.rollback()
                        except Exception:
                            logger.error("Rollback failed or unnecessary.")
                        # Optionally, set con=None here to force reconnection next time
                        con = None
                        time.sleep(10)
                        # Don't clear BUFFER

        except Exception as e:
            logger.error(f"Error in processing loop: {e}", exc_info=True)
            if con:
                try:
                    con.close()
                except Exception:
                    pass
                con = None
            time.sleep(60)

        for _ in range(20):
            if shutdown_flag.shutting_down:
                break
            time.sleep(1)

    # Final buffer flush at shutdown
    if BUFFER:
        con = get_connection()
        if con is not None:
            try:
                batch_df = pd.concat(BUFFER, ignore_index=True)
                con.begin()
                con.register('batch_df', batch_df)
                con.execute(f"INSERT OR IGNORE INTO {TABLE_NAME} SELECT * FROM batch_df")
                con.unregister('batch_df')
                con.commit()
                con.execute('CHECKPOINT')
                logger.info(f"Committed batch of {len(batch_df)} rows to DuckDB at shutdown.")
                BUFFER.clear()
            except Exception as db_err:
                logger.error(f"Error final batch inserting rows: {db_err}", exc_info=True)
                try:
                    con.rollback()
                except Exception:
                    pass
        else:
            logger.warning("Could not flush buffer at shutdown: no DB connection.")



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