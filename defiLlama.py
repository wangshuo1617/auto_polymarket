import requests
import pandas as pd

class StablecoinMonitor:
    def __init__(self):
        self.url = "https://stablecoins.llama.fi/stablecoins"
        # 我们只关心这两大巨头，因为它们代表了主要流动性
        self.target_coins = ['Tether', 'USDC'] 

    def get_macro_liquidity(self):
        try:
            response = requests.get(self.url, timeout=10)
            response.raise_for_status()
            data = response.json()['peggedAssets']
            
            total_mcap = 0
            weighted_change_7d = 0
            
            print(f"--- 宏观资金流 (Stablecoin Flow) ---")
            
            for coin in data:
                if coin['name'] in self.target_coins:
                    mcap = coin['circulating']['peggedUSD'] # 获取当前流通市值
                    total_mcap += mcap

            result = {
                "total_mcap_usd": total_mcap
            }
            return result

        except Exception as e:
            print(f"DefiLlama 数据获取失败: {e}")
            return None
# 测试运行
if __name__ == "__main__":
    monitor = StablecoinMonitor()
    monitor.get_macro_liquidity()