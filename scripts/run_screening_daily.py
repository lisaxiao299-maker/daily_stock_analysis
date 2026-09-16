#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日自动选股 + 推送（无头运行）

适配 GitHub Actions 云端定时任务与本地 cron / 手动执行：
1. 按环境变量或参数选择选股策略（支持多个策略依次运行）
2. 交易日检查（非交易日默认跳过，--force-run 可强制）
3. 运行 DSA 内置选股引擎（全市场快照 → 过滤 → 评分 → LLM 重排 → 入选）
4. 生成 Markdown 报告保存到 reports/
5. 通过 NotificationService 推送到所有已配置渠道（Telegram / 企业微信 / 飞书等）
   - 云端只需配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，其余渠道留空即可
   - 如需只推 Telegram，可在仓库设置 NOTIFICATION_REPORT_CHANNELS=telegram

用法示例:
    python scripts/run_screening_daily.py
    python scripts/run_screening_daily.py --strategies balanced_alpha,volume_breakout
    python scripts/run_screening_daily.py --max-results 10 --force-run --no-notify
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 必须在导入 src.* 之前设置：无头模式直接启用选股引擎
os.environ.setdefault("SCREENING_ENABLED", "true")

# 项目根目录加入 sys.path（脚本位于 scripts/ 下）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("screening_daily")

DEFAULT_STRATEGIES = "balanced_alpha"
DEFAULT_MARKET = "cn"
DEFAULT_MAX_RESULTS = 10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="每日自动选股并推送")
    parser.add_argument(
        "--strategies",
        default=os.getenv("SCREENING_STRATEGIES", DEFAULT_STRATEGIES),
        help="逗号分隔的选股策略 ID（默认 SCREENING_STRATEGIES 或 balanced_alpha）",
    )
    parser.add_argument(
        "--market",
        default=os.getenv("SCREENING_MARKET", DEFAULT_MARKET),
        help="选股市场（默认 SCREENING_MARKET 或 cn）",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=int(os.getenv("SCREENING_MAX_RESULTS", str(DEFAULT_MAX_RESULTS))),
        help="每个策略入选股票数量（默认 SCREENING_MAX_RESULTS 或 10）",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="只生成报告，不推送通知",
    )
    parser.add_argument(
        "--force-run",
        action="store_true",
        help="跳过交易日检查，强制执行",
    )
    parser.add_argument(
        "--skip-save",
        action="store_true",
        help="不保存报告文件",
    )
    return parser.parse_args()


def check_trading_day(market: str, force_run: bool) -> bool:
    """交易日检查：目标市场今日休市则跳过（返回 False）。"""
    if force_run:
        return True
    try:
        from src.core.trading_calendar import get_open_markets_today

        open_markets = get_open_markets_today()
        if market in open_markets or not open_markets:
            return True
        logger.info(
            "今日市场 %s 未开市（开市: %s），跳过选股。可用 --force-run 强制执行。",
            market,
            ",".join(sorted(open_markets)) or "无",
        )
        return False
    except Exception as exc:  # 日历不可用时 fail-open，与主程序行为一致
        logger.warning("交易日检查失败（%s），继续执行选股。", exc)
        return True


def build_service():
    """构建 ScreeningService；数据库可选（失败不阻塞选股）。"""
    from src.config import Config
    from src.services.screening_service import ScreeningService

    # Config 是 dataclass：直接 Config() 会使用默认值（screening_enabled=False）。
    # 必须用 get_instance() 单例，从环境变量 / .env 正确加载。
    config = Config.get_instance()
    db_manager = None
    try:
        from src.storage import DatabaseManager

        db_manager = DatabaseManager()
    except Exception as exc:
        logger.warning("数据库不可用（%s），本次运行不保存选股历史。", exc)
    return ScreeningService(config, db_manager)


def _fmt_num(value: Any, suffix: str = "", digits: int = 2) -> str:
    try:
        num = float(value)
        return f"{num:.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "-"


def _fmt_change_pct(value: Any) -> str:
    try:
        num = float(value)
        sign = "+" if num > 0 else ""
        return f"{sign}{num:.2f}%"
    except (TypeError, ValueError):
        return "-"


def format_screen_report(
    runs: List[Dict[str, Any]],
    market: str,
    max_results: int,
) -> str:
    """将多个策略的选股结果格式化为 Markdown 报告。"""
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    weekday_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday = weekday_map[now.weekday()]

    lines: List[str] = []
    lines.append(f"## 🎯 每日自动选股 {date_str}（{weekday}）")
    lines.append("")

    ok_runs = [r for r in runs if r.get("ok")]
    failed_runs = [r for r in runs if not r.get("ok")]

    if not ok_runs and failed_runs:
        lines.append("⚠️ **所有策略选股均失败**")
        for r in failed_runs:
            lines.append(f"- `{r.get('strategy')}`: {r.get('error', '未知错误')}")
        lines.append("")
        return "\n".join(lines)

    summary_bits = []
    for r in ok_runs:
        strategy_label = r.get("display_name") or r.get("strategy", "")
        count = r.get("candidate_count", 0)
        ranking = "🤖 LLM 重排" if r.get("ranking_mode") == "llm" else "📊 因子评分"
        summary_bits.append(f"{strategy_label} {count} 只（{ranking}）")
    lines.append(f"**市场**: {market} | **入选**: {' / '.join(summary_bits)}")
    lines.append("")

    for r in ok_runs:
        strategy_label = r.get("display_name") or r.get("strategy", "")
        lines.append(f"### 📌 {strategy_label}")
        lines.append("")
        snapshot = r.get("snapshot_count")
        if snapshot:
            lines.append(f"全市场扫描 {snapshot} 只 → 入选 {r.get('candidate_count', 0)} 只")
            lines.append("")

        # LLM 市场观点（如有）
        market_view = (r.get("llm_market_view") or "").strip()
        selection_logic = (r.get("llm_selection_logic") or "").strip()
        if market_view:
            lines.append(f"**市场观点**: {market_view}")
            lines.append("")
        if selection_logic:
            lines.append(f"**选股逻辑**: {selection_logic}")
            lines.append("")

        candidates = r.get("candidates") or []
        for c in candidates:
            rank = c.get("rank") or "-"
            code = c.get("code") or "-"
            name = c.get("name") or ""
            price = _fmt_num(c.get("price"))
            change = _fmt_change_pct(c.get("change_pct"))
            score = _fmt_num(c.get("score"), digits=1)
            industry = c.get("industry") or c.get("llm_sector") or ""
            risk_level = c.get("risk_level") or ""

            head = f"**{rank}. {name}({code})** 评分:{score}"
            extra_bits = []
            if industry:
                extra_bits.append(industry)
            if risk_level:
                extra_bits.append(f"风险:{risk_level}")
            if extra_bits:
                head += " | " + " · ".join(extra_bits)
            lines.append(head)
            detail_bits = [f"价格 {price}", f"涨跌 {change}"]
            lines.append(f"　{', '.join(detail_bits)}")

            reason = (c.get("reason") or "").strip()
            if reason:
                lines.append(f"　入选理由: {reason}")
            thesis = (c.get("llm_thesis") or "").strip()
            if thesis and thesis != reason:
                lines.append(f"　AI 观点: {thesis}")
            catalysts = c.get("llm_catalysts") or []
            if catalysts:
                joined = "；".join(str(x) for x in catalysts[:2])
                lines.append(f"　催化: {joined}")
            risks = c.get("llm_risks") or []
            if risks:
                joined = "；".join(str(x) for x in risks[:2])
                lines.append(f"　⚠️ 风险: {joined}")
            lines.append("")

        portfolio_risk = (r.get("llm_portfolio_risk") or "").strip()
        if portfolio_risk:
            lines.append(f"**组合风险提示**: {portfolio_risk}")
            lines.append("")

        warnings = r.get("warnings") or []
        degradation = r.get("degradation") or []
        notes = [str(w) for w in (list(warnings) + list(degradation)) if str(w).strip()]
        if notes:
            lines.append("> 备注: " + "；".join(notes[:4]))
            lines.append("")

        lines.append("---")
        lines.append("")

    if failed_runs:
        lines.append("⚠️ 失败策略: " + "; ".join(
            f"`{r.get('strategy')}`({r.get('error', '未知')})" for r in failed_runs
        ))
        lines.append("")

    lines.append(f"🤖 数据快照 {now.strftime('%H:%M')}，仅供参考，不构成投资建议")
    return "\n".join(lines)


def save_report(content: str) -> Optional[str]:
    """保存报告到 reports/ 目录。"""
    try:
        reports_dir = PROJECT_ROOT / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / f"screening_{datetime.now().strftime('%Y%m%d')}.md"
        path.write_text(content, encoding="utf-8")
        logger.info("选股报告已保存: %s", path)
        return str(path)
    except Exception as exc:
        logger.warning("报告保存失败: %s", exc)
        return None


def send_notification(content: str, enabled: bool) -> bool:
    """推送到所有已配置渠道（Telegram 等）。"""
    if not enabled:
        logger.info("推送已禁用（--no-notify）")
        return False
    try:
        from src.notification import NotificationService

        notifier = NotificationService()
        return notifier.send(content, route_type="report")
    except Exception as exc:
        logger.error("通知推送失败: %s", exc)
        return False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    args = parse_args()
    market = (args.market or "cn").strip()
    max_results = max(1, min(int(args.max_results or 10), 50))
    strategies = [s.strip() for s in (args.strategies or "").split(",") if s.strip()]
    if not strategies:
        strategies = [DEFAULT_STRATEGIES]

    logger.info("=" * 50)
    logger.info("🎯 每日自动选股启动")
    logger.info("   市场: %s | 策略: %s | 每策略入选: %s", market, ",".join(strategies), max_results)
    logger.info("=" * 50)

    if not check_trading_day(market, args.force_run):
        return 0

    try:
        service = build_service()
    except Exception as exc:
        logger.error("选股服务初始化失败: %s", exc)
        return 1

    runs: List[Dict[str, Any]] = []
    for strategy in strategies:
        logger.info("▶ 运行策略: %s", strategy)
        try:
            result = service.screen(
                strategy=strategy,
                market=market,
                max_results=max_results,
            )
            display_name = strategy
            try:
                for item in service.strategies().get("strategies", []):
                    if item.get("id") == strategy:
                        display_name = item.get("display_name") or item.get("name") or strategy
                        break
            except Exception:
                pass
            runs.append({
                "ok": True,
                "strategy": strategy,
                "display_name": display_name,
                "candidates": result.get("candidates") or [],
                "candidate_count": result.get("candidate_count", 0),
                "snapshot_count": result.get("snapshot_count"),
                "ranking_mode": result.get("ranking_mode"),
                "llm_market_view": result.get("llm_market_view"),
                "llm_selection_logic": result.get("llm_selection_logic"),
                "llm_portfolio_risk": result.get("llm_portfolio_risk"),
                "warnings": result.get("warnings"),
                "degradation": result.get("degradation"),
            })
            logger.info(
                "✔ 策略 %s 完成: %s 只 (ranking=%s)",
                strategy,
                result.get("candidate_count", 0),
                result.get("ranking_mode"),
            )
        except Exception as exc:
            logger.error("✘ 策略 %s 失败: %s", strategy, exc)
            runs.append({"ok": False, "strategy": strategy, "error": str(exc)})

    if not any(r.get("ok") for r in runs):
        logger.error("所有策略均失败，退出码 1")
        return 1

    report = format_screen_report(runs, market, max_results)

    if not args.skip_save:
        save_report(report)

    sent = send_notification(report, not args.no_notify)
    logger.info("%s", "📤 选股报告已推送" if sent else "📤 报告未推送或无可用渠道")

    logger.info("✅ 每日选股完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
