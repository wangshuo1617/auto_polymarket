"""
邮件发送服务
支持通过 SMTP 服务器向特定邮箱发送邮件
"""

import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from config import TO_EMAIL, SMTP_SERVER, SMTP_PORT, FROM_EMAIL, FROM_EMAIL_PASSWORD

# 获取日志记录器（日志配置由主程序统一管理）
logger = logging.getLogger(__name__)


class EmailSender:
    """邮件发送器"""
    
    def __init__(self):
        """
        初始化邮件发送器

        """
        self.config = {
            "smtp_server": SMTP_SERVER,
            "smtp_port": SMTP_PORT,
            "sender_email": FROM_EMAIL,
            "sender_password": FROM_EMAIL_PASSWORD,
            "use_tls": False,
            "use_ssl": True
        }
    
    def send_email(
        self,
        to_email: str,
        subject: str,
        content: str,
        content_type: str = "plain"
    ) -> bool:
        """
        发送邮件
        
        Args:
            to_email: 收件人邮箱地址
            subject: 邮件主题
            content: 邮件内容
            content_type: 内容类型，'plain' 或 'html'
        
        Returns:
            bool: 发送成功返回 True，失败返回 False
        """
        try:
            # 创建邮件对象
            msg = MIMEMultipart('alternative')
            msg['From'] = Header(self.config['sender_email'], 'utf-8')
            msg['To'] = Header(to_email, 'utf-8')
            msg['Subject'] = Header(subject, 'utf-8')
            
            # 添加邮件正文
            text_part = MIMEText(content, content_type, 'utf-8')
            msg.attach(text_part)
            
            # 构建收件人列表
            recipients = [to_email]
            
            # 连接 SMTP 服务器并发送邮件
            if self.config['use_ssl']:
                server = smtplib.SMTP_SSL(self.config['smtp_server'], self.config['smtp_port'])
            else:
                server = smtplib.SMTP(self.config['smtp_server'], self.config['smtp_port'])
                if self.config['use_tls']:
                    server.starttls()
            
            # 登录
            server.login(self.config['sender_email'], self.config['sender_password'])
            
            # 发送邮件
            server.sendmail(self.config['sender_email'], recipients, msg.as_string())
            
            # 关闭连接
            server.quit()
            
            logger.info(f"邮件发送成功: {to_email}, 主题: {subject}")
            return True
            
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP 认证失败: {e}")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"SMTP 错误: {e}")
            return False
        except Exception as e:
            logger.error(f"发送邮件时出错: {e}")
            return False
    
    def send_html_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
    ) -> bool:
        """
        发送 HTML 格式邮件
        
        Args:
            to_email: 收件人邮箱地址
            subject: 邮件主题
            html_content: HTML 格式的邮件内容
            cc_emails: 抄送邮箱列表（可选）
            bcc_emails: 密送邮箱列表（可选）
        
        Returns:
            bool: 发送成功返回 True，失败返回 False
        """
        return self.send_email(
            to_email=to_email,
            subject=subject,
            content=html_content,
            content_type='html',
        )

def main():
    """主函数 - 示例用法"""

    # 创建邮件发送器
    sender = EmailSender()
    # 发送纯文本邮件
    success = sender.send_email(
        to_email=TO_EMAIL,
        subject="测试邮件",
        content="这是一封测试邮件。\n\n来自自动发送服务。"
    )
    
    if success:
        print("邮件发送成功！")
    else:
        print("邮件发送失败！")


if __name__ == "__main__":
    # 如果作为独立脚本运行，配置日志
    main()
