"""Risk Engine — независимый контур управления риском.

Модуль умышленно НЕ зависит от Claude и не может быть переопределён его
решениями: агент предлагает сделку, движок решает, какого она размера и
допустима ли вообще. Это разделение — не стилистическое, а защитное.
"""
from src.risk.engine import (  # noqa: F401
    RiskConfig,
    RiskDecision,
    RiskState,
    load_config,
    size_position,
    check_limits,
    evaluate_trade,
    REASONS,
)
