"""
比特币价格监控服务
基于 Binance WebSocket Streams API 实时监控 BTC/USDT 价格
"""

import json
import time
import logging
from datetime import datetime
from typing import Optional, Callable
from websocket import WebSocketApp
from notifications.email import EmailSender
import config
from price_warn_config import WARN_PRICE

# 配置日志
logging.basicConfig(
    level=logging.ERROR,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BTCPriceWatcher:
    """比特币价格监控器"""
    
    # Binance WebSocket 端点
    BASE_URL = "wss://stream.binance.com:9443"
    
    def __init__(self, symbol: str = "btcusdt", stream_type: str = "ticker,bookTicker", callback: Optional[Callable] = None):
        """
        初始化价格监控器
        
        Args:
            symbol: 交易对符号，默认为 btcusdt
            stream_type: 流类型，默认为 ticker,bookTicker
            callback: 价格更新回调函数，接收价格数据作为参数
        """
        self.symbol = symbol.lower()
        self.stream_type_list = stream_type.split(",")
        self.callback = callback
        self.ws: Optional[WebSocketApp] = None
        self.running = False
        self.last_price: Optional[float] = None
        self.last_update_time: Optional[float] = None
        
    def _on_message(self, ws, message):
        """处理 WebSocket 消息"""
        try:
            data = json.loads(message)
            
            # 处理组合流格式：{"stream":"<streamName>","data":<rawPayload>}
            if isinstance(data, dict) and "stream" in data and "data" in data:
                stream_name = data.get("stream", "")
                payload = data.get("data", {})
                
                if "@ticker" in stream_name:
                    self._handle_ticker(payload)
                elif "@bookTicker" in stream_name:
                    self._handle_book_ticker(payload)
                elif "@avgPrice" in stream_name:
                    self._handle_avgprice(payload)
                else:
                    logger.warning(f"未知流类型: {stream_name}")
            # 处理原始流格式（直接数据）
            elif isinstance(data, dict):
                if data.get("e") == "24hrTicker":
                    self._handle_ticker(data)
                elif "b" in data and "a" in data and "s" in data:  # bookTicker 格式
                    self._handle_book_ticker(data)
                elif data.get("result") is not None:
                    # 订阅响应
                    logger.info(f"订阅响应: {data}")
                    
        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析错误: {e}, 消息: {message}")
        except Exception as e:
            logger.error(f"处理消息时出错: {e}")
    
    def _handle_ticker(self, data: dict):
        """处理 24hr Ticker 数据"""
        try:
            symbol = data.get("s", "")
            last_price = float(data.get("c", 0))
            price_change = float(data.get("p", 0))
            price_change_percent = float(data.get("P", 0))
            high_price = float(data.get("h", 0))
            low_price = float(data.get("l", 0))
            volume = float(data.get("v", 0))
            quote_volume = float(data.get("q", 0))
            event_time = data.get("E", 0)
            
            self.last_price = last_price
            self.last_update_time = time.time()
            
            price_info = {
                "symbol": symbol,
                "last_price": last_price,
                "price_change": price_change,
                "price_change_percent": price_change_percent,
                "high_24h": high_price,
                "low_24h": low_price,
                "volume_24h": volume,
                "quote_volume_24h": quote_volume,
                "timestamp": event_time,
                "update_time": self.last_update_time
            }
            
            # 打印价格信息
            logger.info(
                f"BTC/USDT 价格: ${last_price:,.2f} | "
                f"24h 涨跌: {price_change_percent:+.2f}% | "
                f"24h 最高: ${high_price:,.2f} | "
                f"24h 最低: ${low_price:,.2f} | "
                f"24h 成交量: {volume:,.2f} BTC"
            )
            
            # 调用回调函数
            if self.callback:
                self.callback(price_info)
                
        except Exception as e:
            logger.error(f"处理 ticker 数据时出错: {e}")
    
    def _handle_book_ticker(self, data: dict):
        """处理 Book Ticker 数据（最佳买卖价）"""
        try:
            symbol = data.get("s", "")
            best_bid_price = float(data.get("b", 0))
            best_bid_qty = float(data.get("B", 0))
            best_ask_price = float(data.get("a", 0))
            best_ask_qty = float(data.get("A", 0))
            update_id = data.get("u", 0)
            
            mid_price = (best_bid_price + best_ask_price) / 2
            spread = best_ask_price - best_bid_price
            spread_percent = (spread / mid_price) * 100
            
            self.last_price = mid_price
            self.last_update_time = time.time()
            
            price_info = {
                "symbol": symbol,
                "best_bid_price": best_bid_price,
                "best_bid_qty": best_bid_qty,
                "best_ask_price": best_ask_price,
                "best_ask_qty": best_ask_qty,
                "mid_price": mid_price,
                "spread": spread,
                "spread_percent": spread_percent,
                "update_id": update_id,
                "update_time": self.last_update_time
            }
            
            # 打印价格信息
            logger.info(
                f"BTC/USDT 买卖价: 买 ${best_bid_price:,.2f} | "
                f"卖 ${best_ask_price:,.2f} | "
                f"中间价: ${mid_price:,.2f} | "
                f"价差: ${spread:.2f} ({spread_percent:.3f}%)"
            )
            
            # 调用回调函数
            if self.callback:
                self.callback(price_info)
                
        except Exception as e:
            logger.error(f"处理 book ticker 数据时出错: {e}")
    
    def _handle_avgprice(self, data: dict):
        """处理平均价格数据"""
        try:
            symbol = data.get("s", "")
            avg_price = float(data.get("w", 0))
            last_time = data.get("T", 0)
            price_info = {
                "symbol": symbol,
                "avg_price": avg_price,
                "last_time": last_time,
            }
            # 打印价格信息
            logger.info(
                f"BTC/USDT 平均价格: ${avg_price:,.2f} | "
                f"最后更新时间: {last_time}"
            )
            # 调用回调函数
            if self.callback:
                self.callback(price_info)
        except Exception as e:
            logger.error(f"处理平均价格数据时出错: {e}")
    
    def _on_error(self, ws, error):
        """处理 WebSocket 错误"""
        logger.error(f"WebSocket 错误: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        """处理 WebSocket 关闭"""
        logger.warning(f"WebSocket 连接已关闭: status={close_status_code}, msg={close_msg}")
        was_running = self.running
        self.running = False
        
        # 如果之前还在运行，尝试重连
        if was_running:
            logger.info("尝试重新连接...")
            time.sleep(5)
            if not self.running:  # 确保没有在停止过程中
                try:
                    self.start()
                except Exception as e:
                    logger.error(f"重连失败: {e}")
    
    def _on_open(self, ws):
        """WebSocket 连接打开时的处理"""
        logger.info("WebSocket 连接已建立")
        self.running = True
        # 注意：使用 /stream 端点时，流已经在 URL 中指定，无需再次订阅
        logger.info(f"已连接并监听 {self.symbol.upper()} 价格流")
    
    def _on_ping(self, ws, message):
        """处理 ping 帧，自动回复 pong"""
        try:
            ws.send(message, opcode=0x9)  # 发送 pong
            logger.debug("收到 ping，已回复 pong")
        except Exception as e:
            logger.error(f"回复 pong 时出错: {e}")
    
    def start(self):
        """启动价格监控服务"""
        if self.running:
            logger.warning("监控服务已在运行中")
            return
        
        # 使用 /stream 端点以支持动态订阅/取消订阅
        # 组合流格式：/stream?streams=<streamName1>/<streamName2>
        streams = []
        for stream_type in self.stream_type_list:
            streams.append(f"{self.symbol}@{stream_type}")
        streams = "/".join(streams)
        ws_url = f"{self.BASE_URL}/stream?streams={streams}"
        
        logger.info(f"正在连接到 Binance WebSocket: {ws_url}")
        
        self.ws = WebSocketApp(
            ws_url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
            on_ping=self._on_ping
        )
        
        # 运行 WebSocket（阻塞）
        try:
            self.ws.run_forever(
                ping_interval=20,  # 每20秒发送一次 ping
                ping_timeout=10,
                reconnect=5  # 自动重连，最多5次
            )
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在停止...")
            self.stop()
        except Exception as e:
            logger.error(f"运行 WebSocket 时出错: {e}")
            self.stop()
    
    def stop(self):
        """停止价格监控服务"""
        logger.info("正在停止价格监控服务...")
        self.running = False
        
        if self.ws:
            try:
                # 使用 /stream 端点时，直接关闭连接即可
                self.ws.close()
            except:
                pass
            
            self.ws = None
        
        logger.info("价格监控服务已停止")
    
    def get_last_price(self) -> Optional[float]:
        """获取最后一次更新的价格"""
        return self.last_price
    
    def get_last_update_time(self) -> Optional[float]:
        """获取最后一次更新的时间"""
        return self.last_update_time

def main():
    """主函数"""
    sender = EmailSender()    
    # 记录上次发送价格报告的时间
    last_report_time = time.time()
    
    def send_price_report(price_data: dict):
        """发送每小时价格报告"""
        avg_price = price_data.get("avg_price", 0)
        last_time = price_data.get("last_time", 0)
        
        # 格式化时间
        report_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        last_update_time = datetime.fromtimestamp(last_time / 1000).strftime("%Y-%m-%d %H:%M:%S") if last_time else "未知"
        
        
        subject = f"📊 BTC/USDT 每小时价格报告: ${avg_price:,.2f}"
        content = f"""BTC/USDT 每小时价格报告

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        报告时间: {report_time}
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        当前价格: ${avg_price:,.2f}
        数据更新时间: {last_update_time}

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        此报告每小时自动发送一次
        """
        sender.send_email(
            to_email=config.TO_EMAIL,
            subject=subject,
            content=content
        )
        logger.info(f"已发送每小时价格报告: ${avg_price:,.2f}")

    def send_price_alert_email(price_data: dict):
        nonlocal last_report_time
        avg_price = price_data.get("avg_price", 0)
        last_time = price_data.get("last_time", 0)
        
        # 检查是否需要发送每小时价格报告
        current_time = time.time()
        if (
            config.ENABLE_BTC_HOURLY_EMAIL
            and current_time - last_report_time >= config.REPORT_INTERVAL
        ):
            send_price_report(price_data)
            last_report_time = current_time
        
        for warn_price in WARN_PRICE:
            if warn_price["预警方向"] == "up_to" and not warn_price["alert_status"]:
                if avg_price >= float(warn_price["价格"]):
                    subject = f"🚀 BTC/USDT 极好预警: ${avg_price:,.2f}"
                    content = f"""BTC/USDT 价格预警

                    当前价格: ${avg_price:,.2f}
                    预警级别: 🚀 极好
                    操作建议: {warn_price["操作建议"]}
                    最后更新时间: {last_time}

                    请及时关注市场动态！"""
                    sender.send_email(
                        to_email=config.TO_EMAIL,
                        subject=subject,
                        content=content
                    )
                    warn_price["alert_status"] = True
            elif warn_price["预警方向"] == "down_to" and not warn_price["alert_status"]:
                if avg_price <= float(warn_price["价格"]):
                    subject = f"🔴 BTC/USDT 危险预警: ${avg_price:,.2f}"
                    content = f"""BTC/USDT 价格预警

                    当前价格: ${avg_price:,.2f}
                    预警级别: 🔴 危险
                    操作建议: {warn_price["操作建议"]}
                    最后更新时间: {last_time}
                    请立即关注市场动态！"""
                    sender.send_email(
                        to_email=config.TO_EMAIL,
                        subject=subject,
                        content=content
                    )
                    warn_price["alert_status"] = True
    
    # 创建价格监控器
    watcher = BTCPriceWatcher(symbol="btcusdt", stream_type="avgPrice", callback=send_price_alert_email)
    
    try:
        # 启动监控
        watcher.start()
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        watcher.stop()  

if __name__ == "__main__":
    main()