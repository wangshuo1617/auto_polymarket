"""
DefiLlama 稳定币流动性数据
"""
import requests


class StablecoinMonitor:
    def __init__(self):
        self.url = "https://stablecoins.llama.fi/stablecoins"
        self.target_coins = ["Tether", "USDC"]

    def get_macro_liquidity(self) -> dict | None:
        try:
            response = requests.get(self.url, timeout=10)
            response.raise_for_status()
            data = response.json()["peggedAssets"]

            total_mcap = 0
            for coin in data:
                if coin["name"] in self.target_coins:
                    total_mcap += coin["circulating"]["peggedUSD"]

            return {"total_mcap_usd": total_mcap}
        except Exception as e:
            print(f"DefiLlama 数据获取失败: {e}")
            return None
