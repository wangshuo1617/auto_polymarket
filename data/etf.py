"""
SoSoValue ETF 流入数据爬虫
"""
import re
from playwright.sync_api import sync_playwright


class ETFScraper:
    def __init__(self):
        self.url = "https://sosovalue.com/assets/etf/us-btc-spot"

    def get_etf_inflow(self) -> dict | None:
        print("--- 正在抓取 SoSoValue ETF 数据 ---")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_extra_http_headers({
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                })
                page.goto(self.url, wait_until="networkidle")
                page.wait_for_selector("h3:has-text('Daily Total Net Inflow')", timeout=15000)

                header_locator = page.locator('h3:has-text("Daily Total Net Inflow")')
                header_locator.wait_for(state="visible", timeout=5000)
                parent_container = header_locator.locator("xpath=../..")
                container_text = parent_container.inner_text()

                def parse_inflow(match: re.Match) -> float | None:
                    neg, num = match.group(1), match.group(2)
                    if not num:
                        return None
                    mult = 1e9 if "B" in num else 1e6 if "M" in num else 1e3 if "K" in num else 1
                    clean = num.replace("B", "").replace("M", "").replace("K", "").replace(",", "").strip()
                    try:
                        val = float(clean) * mult
                        return -val if neg else val
                    except ValueError:
                        return None

                pattern = re.compile(r"(-\s*)?\$?\s*([0-9.,]+[KMB]?)")
                value_num = None
                for m in pattern.finditer(container_text):
                    v = parse_inflow(m)
                    if v is not None and abs(v) >= 1e3:
                        value_num = v
                        break

                if value_num is not None:
                    print(f"-> 原始抓取数据: ${value_num:,.0f}" if value_num >= 0 else f"-> 原始抓取数据: -${abs(value_num):,.0f}")
                    return {"net_inflow_num": value_num}

                header_div = header_locator.locator("xpath=..")
                next_sibling = header_div.locator("xpath=following-sibling::*[1]")
                if next_sibling.count() > 0:
                    sibling_text = next_sibling.inner_text()
                    for m in pattern.finditer(sibling_text):
                        v = parse_inflow(m)
                        if v is not None and abs(v) >= 1e3:
                            print(f"-> 原始抓取数据(兄弟节点): ${v:,.0f}" if v >= 0 else f"-> 原始抓取数据(兄弟节点): -${abs(v):,.0f}")
                            return {"net_inflow_num": v}

                print("未找到 ETF 流入数据，页面结构可能已变更。")
                return None
        except Exception as e:
            print(f"SoSoValue 抓取失败 (请确保已安装 playwright): {e}")
            return None
