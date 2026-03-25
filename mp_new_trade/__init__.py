"""
独立 MP 研究管线：人工定时执行，每批固定窗口数写入本目录 data/ 下 SQLite。
"""

__all__ = ["PACKAGE_ROOT", "DATA_DIR", "DEFAULT_LOCAL_DB", "BATCH_WINDOWS"]

from mp_new_trade.settings import BATCH_WINDOWS, DATA_DIR, DEFAULT_LOCAL_DB, PACKAGE_ROOT
