from playwright.sync_api import sync_playwright
import time
import re

class ETFScraper:
    def __init__(self):
        # SoSoValue 比特币 ETF 看板地址
        self.url = "https://sosovalue.com/assets/etf/us-btc-spot"

    def get_etf_inflow(self):
        print(f"--- 正在抓取 SoSoValue ETF 数据 ---")
        try:
            with sync_playwright() as p:
                # 启动无头浏览器 (Headless)
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                
                # 设置 User-Agent 伪装成正常用户
                page.set_extra_http_headers({
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                })

                # 访问页面
                page.goto(self.url, wait_until="networkidle")
                
                # 等待数据加载 (关键步骤)
                # 页面结构: <div class="text-sm flex items-center mb-2"><h3>Daily Total Net Inflow</h3><img...></div>
                # 数值通常在 header div 的兄弟节点或父容器的其他子节点中
                page.wait_for_selector("h3:has-text('Daily Total Net Inflow')", timeout=15000)
                
                # 定位策略：找到 h3 包含 "Daily Total Net Inflow" 的元素，向上找父容器，再找数值
                # 父结构通常是: <div> [header div] [value div/span] </div>
                header_locator = page.locator('h3:has-text("Daily Total Net Inflow")')
                header_locator.wait_for(state="visible", timeout=5000)
                
                # 获取 header 的父 div (text-sm flex items-center mb-2)，再获取其父容器
                parent_container = header_locator.locator("xpath=../..")
                container_text = parent_container.inner_text()
                
                # 正则：提取 -$数字 或 $数字 (支持 K/M/B)，负号可能在 $ 前
                def parse_inflow(match: re.Match) -> float | None:
                    neg, num = match.group(1), match.group(2)
                    if not num: return None
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
                    if v is not None and abs(v) >= 1e3:  # 合理范围：至少 $1K 级别
                        value_num = v
                        break
                
                if value_num is not None:
                    print(f"-> 原始抓取数据: ${value_num:,.0f}" if value_num >= 0 else f"-> 原始抓取数据: -${abs(value_num):,.0f}")
                    return {"net_inflow_num": value_num}
                
                # 备选：若父容器未包含数值，尝试找 header 的下一个兄弟节点
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

# 测试运行
if __name__ == "__main__":
    scraper = ETFScraper()
    scraper.get_etf_inflow()