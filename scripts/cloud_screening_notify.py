#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端选股推送脚本 — 供 GitHub Actions 调用

职责：
1. 读取 SCREENING_STRATEGY / SCREENING_MARKET / SCREENING_MAX_RESULTS 环境变量
2. 通过 ScreeningService 执行选股（默认 capital_heat, cn, 12）
3. 格式化为 Telegram Markdown 并推送
4. 失败不阻断主流程（exit 0）

参考本地 screen_daily.sh 的策略：capital_heat
"""

import os
import sys
import logging
from datetime import datetime

# 确保能 import src
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main():
    strategy = (os.getenv("SCREENING_STRATEGY") or os.getenv("SCREENING_STRATEGY_NAME") or "capital_heat").strip() or "capital_heat"
    market = (os.getenv("SCREENING_MARKET") or "cn").strip() or "cn"
    try:
        max_results = int((os.getenv("SCREENING_MAX_RESULTS") or "12").strip())
    except ValueError:
        max_results = 12
    max_results = max(1, min(max_results, 30))

    logger.info("=" * 60)
    logger.info("云端选股推送启动")
    logger.info("策略=%s 市场=%s 数量=%d", strategy, market, max_results)
    logger.info("=" * 60)

    try:
        from src.config import get_config
        config = get_config()
    except Exception as exc:
        logger.error("加载配置失败: %s", exc)
        return 0

    if not getattr(config, "screening_enabled", False):
        logger.warning("SCREENING_ENABLED=false，跳过选股推送（请在 GitHub Variables 设置 SCREENING_ENABLED=true）")
        return 0

    if not (getattr(config, "telegram_bot_token", None) and getattr(config, "telegram_chat_id", None)):
        logger.warning("Telegram 未配置（TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID），跳过推送")
        return 0

    # 执行选股
    try:
        from src.services.screening_service import ScreeningService
        service = ScreeningService(config, db_manager=None)
        logger.info("开始选股: strategy=%s market=%s max_results=%d", strategy, market, max_results)
        result = service.screen(strategy=strategy, market=market, max_results=max_results)
        candidates = result.get("candidates") or []
        logger.info("选股完成: 候选数=%d", len(candidates))
    except Exception as exc:
        logger.error("选股失败: %s", exc)
        import traceback
        traceback.print_exc()
        # 仍尝试推送失败通知？ 这里静默跳过
        return 0

    if not candidates:
        msg = f"【选股结果】{strategy} ({market})\n\n今日无候选（可能为非交易日或硬过滤后为空）\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M %Z')}"
        try:
            from src.notification_sender.telegram_sender import TelegramSender
            sender = TelegramSender(config)
            sender.send_to_telegram(msg)
            logger.info("已推送空结果到 Telegram")
        except Exception as exc:
            logger.error("推送空结果失败: %s", exc)
        return 0

    # 格式化消息
    lines = []
    for i, c in enumerate(candidates, 1):
        code = c.get("code") or c.get("stock_code") or ""
        name = c.get("name") or c.get("stock_name") or ""
        score = c.get("score")
        try:
            score_str = f"{round(float(score), 1):.1f}" if score is not None else "-"
        except Exception:
            score_str = str(score) if score is not None else "-"
        # 额外字段：涨幅、量比等若有则展示
        change = c.get("change_pct")
        if change is None:
            change = c.get("pct_chg")
        change_str = f" 涨{change:+.1f}%" if isinstance(change, (int, float)) else ""
        # 评分过高/过低标记
        lines.append(f"{i}. {name}({code}) {score_str}分{change_str}")

    header = f"🔥 *云端选股* `{strategy}` \\| 市场 `{market}` \\| {len(candidates)}只\n"
    header += f"_时间 {datetime.now().strftime('%Y-%m-%d %H:%M')} \\(北京时间\\)_\n"
    # 来自策略的说明
    strategy_desc = result.get("strategy") or strategy
    # 排名逻辑
    ranking_mode = result.get("ranking_mode") or "factor"
    footer = ""
    if result.get("llm_market_view"):
        footer += f"\n> {result.get('llm_market_view')[:120]}"
    if result.get("warnings"):
        footer += f"\n⚠️ {'; '.join(result.get('warnings')[:2])}"

    # Telegram Markdown 特殊字符需转义？ TelegramSender 内部会处理，这里简单拼
    body = header + "\n" + "\n".join(lines) + footer
    # 加上本地无法获取的提示
    body += "\n\n_由 GitHub Actions 云端自动推送_"

    # 限制长度 4096，TelegramSender 会自动分段
    try:
        from src.notification_sender.telegram_sender import TelegramSender
        sender = TelegramSender(config)
        ok = sender.send_to_telegram(body)
        if ok:
            logger.info("✅ 选股结果已推送到 Telegram (%d只)", len(candidates))
        else:
            logger.error("❌ Telegram 推送返回 False")
    except Exception as exc:
        logger.error("Telegram 推送异常: %s", exc)
        import traceback
        traceback.print_exc()

    # 同时打印到日志便于 Actions 查看
    print("\n" + "=" * 60)
    print(body)
    print("=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
