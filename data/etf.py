"""
SoSoValue ETF 流入数据爬虫
"""
import re
from datetime import datetime
from pathlib import Path

import requests


class ETFScraper:
    def __init__(self):
        self.url = "https://farside.co.uk/btc/"

    def _save_html_snapshot(self, html: str, reason: str) -> None:
        try:
            output_dir = Path("output") / "etf_snapshots"
            output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            reason_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", reason).strip("_")
            if not reason_slug:
                reason_slug = "snapshot"
            file_path = output_dir / f"{timestamp}_{reason_slug}.html"
            file_path.write_text(html, encoding="utf-8")
            print(f"-> 页面快照已保存: {file_path}")
        except Exception as snapshot_err:
            print(f"-> 页面快照保存失败: {snapshot_err}")

    def get_etf_inflow(self) -> dict | None:
        print("--- 正在抓取 Farside ETF 数据 ---")
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            response = requests.get(self.url, headers=headers, timeout=20)
            response.encoding = "utf-8"
            html = response.text

            tables = re.findall(r"<table[^>]*>.*?</table>", html, re.DOTALL | re.IGNORECASE)
            if not tables:
                self._save_html_snapshot(html, "no_table")
                print("未找到 ETF 表格数据，页面结构可能已变更。")
                return None

            def clean_cell(cell_html: str) -> str:
                cell_html = re.sub(r"<br\s*/?>", " ", cell_html, flags=re.IGNORECASE)
                cell_html = re.sub(r"<[^>]+>", "", cell_html)
                return re.sub(r"\s+", " ", cell_html).strip()

            date_pattern = re.compile(r"\b\d{1,2} [A-Za-z]{3} \d{4}\b")
            date_rows: list[list[str]] = []
            for table_html in tables:
                rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL | re.IGNORECASE)
                for row in rows:
                    cells = re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", row, re.DOTALL | re.IGNORECASE)
                    if not cells:
                        continue
                    texts = [clean_cell(c) for c in cells]
                    if texts and date_pattern.search(texts[0]):
                        date_rows.append(texts)

            if not date_rows:
                self._save_html_snapshot(html, "no_date_rows")
                print("未找到 ETF 日期行，页面结构可能已变更。")
                return None

            latest_row = date_rows[-1]
            if len(latest_row) < 2:
                self._save_html_snapshot(html, "row_too_short")
                print("ETF 数据行列数不足，页面结构可能已变更。")
                return None

            raw_total = latest_row[-1]
            num_match = re.search(r"\(?\s*([0-9.,]+)\s*\)?", raw_total)
            if not num_match:
                self._save_html_snapshot(html, "total_parse_failed")
                print("未能解析 ETF 总净流动。")
                return None

            value_str = num_match.group(1).replace(",", "")
            value_m = float(value_str)
            if "(" in raw_total and ")" in raw_total:
                value_m = -value_m

            value_num = value_m * 1_000_000
            print(
                f"-> 原始抓取数据: ${value_num:,.0f}" if value_num >= 0 else f"-> 原始抓取数据: -${abs(value_num):,.0f}"
            )
            return {"net_inflow_num": value_num}
        except Exception as e:
            print(f"Farside 抓取失败: {e}")
            return None

if __name__ == "__main__":
    scraper = ETFScraper()
    result = scraper.get_etf_inflow()
    if result is not None:
        print(f"最终解析结果: {result}")
    else:
        print("未能获取 ETF 流入数据。")