"""
MOODEX — Chart Generator

Генерирует свечной график (candlestick) из данных MOEX
и возвращает его как base64 PNG для передачи в Claude Vision.

Что рисуется на графике:
  - Японские свечи (OHLC) за последние N дней
  - Объём торгов (нижняя панель)
  - SMA 20 и SMA 50
  - RSI (отдельная панель)
  - Уровни поддержки/сопротивления
  - Аннотации: текущая цена, 52н хай/лой
"""
import io
import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


async def generate_chart_b64(
    ticker: str,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    opens: list[float],
    dates: list[str],
    days: int = 120,
) -> Optional[str]:
    """
    Генерирует свечной PNG-график и возвращает base64-строку.
    Возвращает None если matplotlib недоступен или данных мало.
    """
    try:
        import pandas as pd
        import mplfinance as mpf
        import matplotlib
        matplotlib.use("Agg")   # без GUI
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
    except ImportError:
        logger.warning("mplfinance/matplotlib не установлены — chart generation недоступен")
        return None

    if len(closes) < 20:
        return None

    # Берём последние `days` свечей
    n = min(days, len(closes))
    c = closes[-n:]
    h = highs[-n:]
    l = lows[-n:]
    o = opens[-n:] if opens else c
    d = dates[-n:] if dates else [
        (datetime.now(timezone.utc) - timedelta(days=n - i)).strftime("%Y-%m-%d")
        for i in range(n)
    ]

    try:
        # Строим DataFrame для mplfinance
        idx = pd.to_datetime(d)
        df = pd.DataFrame({
            "Open":   o,
            "High":   h,
            "Low":    l,
            "Close":  c,
            "Volume": [0] * len(c),   # объём не всегда есть в ISS
        }, index=idx)

        # SMA 20 и SMA 50
        add_plots = []
        if len(c) >= 20:
            sma20 = df["Close"].rolling(20).mean()
            add_plots.append(mpf.make_addplot(sma20, color="#2196F3", width=1.2, label="SMA20"))
        if len(c) >= 50:
            sma50 = df["Close"].rolling(50).mean()
            add_plots.append(mpf.make_addplot(sma50, color="#FF9800", width=1.2, label="SMA50"))

        # RSI
        rsi_values = _calc_rsi(c, 14)
        if rsi_values and len(rsi_values) == len(c):
            rsi_series = pd.Series(rsi_values, index=idx)
            add_plots.append(mpf.make_addplot(
                rsi_series, panel=2, color="#9C27B0", width=1.2,
                ylim=(0, 100), ylabel="RSI",
            ))
            # Уровни перекупленности/перепроданности
            ob = pd.Series([70] * len(c), index=idx)
            os_ = pd.Series([30] * len(c), index=idx)
            add_plots.append(mpf.make_addplot(ob,  panel=2, color="red",  linestyle="--", width=0.8, alpha=0.5))
            add_plots.append(mpf.make_addplot(os_, panel=2, color="green", linestyle="--", width=0.8, alpha=0.5))

        # Стиль
        mc = mpf.make_marketcolors(
            up="#26A69A", down="#EF5350",
            wick={"up": "#26A69A", "down": "#EF5350"},
            edge={"up": "#26A69A", "down": "#EF5350"},
            volume={"up": "#26A69A44", "down": "#EF535044"},
        )
        style = mpf.make_mpf_style(
            marketcolors=mc,
            facecolor="#1a1a2e",
            edgecolor="#333",
            figcolor="#1a1a2e",
            gridcolor="#333",
            gridstyle="--",
            gridaxis="both",
            y_on_right=True,
            rc={
                "axes.labelcolor": "#aaa",
                "xtick.color": "#aaa",
                "ytick.color": "#aaa",
                "text.color": "#eee",
            },
        )

        # 52-нед. хай/лой для аннотаций
        w52 = min(252, len(h))
        hi52 = max(h[-w52:])
        lo52 = min(l[-w52:])

        fig, axes = mpf.plot(
            df,
            type="candle",
            style=style,
            title=f"\n  {ticker}  |  {c[-1]:.2f} ₽  |  52н: {lo52:.2f} – {hi52:.2f}",
            figsize=(14, 9),
            panel_ratios=(4, 1, 1.5) if rsi_values else (4, 1),
            addplot=add_plots if add_plots else None,
            volume=True,
            returnfig=True,
            tight_layout=True,
            datetime_format="%b %Y" if n > 90 else "%d %b",
            xrotation=0,
        )

        # Горизонтальные линии хай/лой
        ax_main = axes[0]
        ax_main.axhline(hi52, color="#FF9800", linestyle=":", linewidth=0.9, alpha=0.7)
        ax_main.axhline(lo52, color="#4CAF50", linestyle=":", linewidth=0.9, alpha=0.7)
        ax_main.text(df.index[2], hi52 * 1.002, f"52н хай {hi52:.2f}", color="#FF9800",
                     fontsize=7, alpha=0.9)
        ax_main.text(df.index[2], lo52 * 0.997, f"52н лой {lo52:.2f}", color="#4CAF50",
                     fontsize=7, alpha=0.9)

        fig.patch.set_facecolor("#1a1a2e")

        # Сохраняем в буфер
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                    facecolor="#1a1a2e", edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode("utf-8")

    except Exception as e:
        logger.warning(f"Ошибка генерации графика {ticker}: {e}")
        return None


def _calc_rsi(closes: list[float], period: int = 14) -> Optional[list[float]]:
    """RSI для всего ряда (для отрисовки на графике)."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    rsi_vals = [None] * (period + 1)   # первые period+1 точек без RSI

    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l != 0 else 100
        rsi_vals.append(100 - 100 / (1 + rs))

    # Заполняем None первым валидным значением (чтобы pandas не падал)
    first_valid = next((v for v in rsi_vals if v is not None), 50)
    return [v if v is not None else first_valid for v in rsi_vals]
