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
                # 我们寻找页面上包含 "Total Net Inflow" 关键词的区域
                # SoSoValue 的类名是动态的 (css-xyz)，所以我们用文本匹配定位
                page.wait_for_selector("text=Total Net Inflow", timeout=15000)
                
                # 抓取逻辑：
                # 1. 找到 "Total Net Inflow" 文本
                # 2. 找到它附近的数值。通常在同一个容器或兄弟节点中。
                # 这里我们暴力抓取整个 Header 区域的文本进行正则解析，这样最抗造
                content = page.content() 
                
                # 正则匹配：寻找 "Total Net Inflow" 后面的 $数字
                # 网页结构通常是: <div...>Total Net Inflow</div><div...>$123.45M</div>
                # 这种简单的正则可以适配大多数页面改版
                # 匹配格式如: Total Net Inflow ... $ 1.23M 或 -$ 45.6M
                matches = re.findall(r"Total Net Inflow.*?\$([0-9.,]+[KMB]?)", content, re.DOTALL)
                
                if matches:
                    raw_value = matches[0] # 获取第一个匹配到的，通常是总览数据
                    print(f"-> 原始抓取数据: ${raw_value}")
                    
                    # 简单的单位清洗逻辑
                    value_num = 0
                    multiplier = 1
                    if 'B' in raw_value: multiplier = 1e9
                    elif 'M' in raw_value: multiplier = 1e6
                    elif 'K' in raw_value: multiplier = 1e3
                    
                    clean_str = raw_value.replace('B','').replace('M','').replace('K','').replace(',','')
                    value_num = float(clean_str) * multiplier
                    
                    # 判断正负（网页上可能有负号在$前面，正则需要更精细，这里简化处理）
                    # 更好的方法是看颜色，或者看上下文中的负号
                    is_negative = "-" in content[content.find("Total Net Inflow"):content.find("Total Net Inflow")+100]
                    if is_negative: value_num *= -1

                    result = {
                        "net_inflow_num": value_num
                    }
                    return result
                else:
                    print("未找到 ETF 流入数据，页面结构可能已变更。")
                    return None
                
                browser.close()

        except Exception as e:
            print(f"SoSoValue 抓取失败 (请确保已安装 playwright): {e}")
            return None

# 测试运行
if __name__ == "__main__":
    scraper = ETFScraper()
    scraper.get_etf_inflow()