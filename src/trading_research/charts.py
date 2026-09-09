"""Read-only figures that distinguish observed prices, proposals and simulated fills."""

import math
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import plotly.graph_objects as go

from trading_research.data import Bundle, DataError, timestamp

SERIES = (
    ("strategy", "전략", "#2563eb"),
    ("KR_reference", "국내 기준선", "#64748b"),
    ("US_reference", "미국 기준선", "#d97706"),
)
SIDES = {"BUY": ("매수", "#0f766e", "triangle-up"), "SELL": ("매도", "#c2410c", "triangle-down")}


def _instant(value, field: str) -> datetime:
    if isinstance(value, str):
        value = timestamp(value)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise DataError(f"{field} must be a timezone-aware timestamp")
    return value.astimezone(UTC)


def _decimal(value, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DataError(f"{field} must be a finite number") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise DataError(f"{field} must be {'positive and ' if positive else ''}finite")
    return result


def _number(value, field: str) -> float:
    result = float(_decimal(value, field))
    if not math.isfinite(result):
        raise DataError(f"{field} is outside the chart's finite numeric range")
    return result


def _field(mapping, key: str):
    if not isinstance(mapping, dict) or key not in mapping:
        raise DataError(f"Chart input is missing {key}")
    return mapping[key]


def _records(value, field: str) -> list[dict]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(row, dict) for row in value):
        raise DataError(f"{field} must contain records")
    return list(value)


def _layout(figure: go.Figure, title: str, y_title: str) -> go.Figure:
    figure.update_layout(
        template="plotly_white",
        title={"text": title, "x": 0},
        font={"family": "Arial, Apple SD Gothic Neo, Malgun Gothic, sans-serif", "size": 13},
        paper_bgcolor="#ffffff",
        plot_bgcolor="#ffffff",
        margin={"l": 30, "r": 25, "t": 75, "b": 45},
        legend={"orientation": "h", "y": 1.12, "x": 0},
        hovermode="x unified",
        height=440,
        xaxis={"title": "시각 (UTC)", "type": "date", "showgrid": False},
        yaxis={"title": y_title, "gridcolor": "#e2e8f0", "zerolinecolor": "#cbd5e1"},
    )
    return figure


def equity_figure(report: dict, metric: str = "unit_value") -> go.Figure:
    """Compare recorded simulations; unit values start at one, independently of deposits."""
    if metric not in {"unit_value", "nav_krw"}:
        raise DataError("Chart metric must be unit_value or nav_krw")
    results = _field(report, "results")
    figure = go.Figure()
    for key, label, color in SERIES:
        points = _records(_field(_field(results, key), "curve"), f"{key}.curve")
        ordered = sorted(
            ((_instant(_field(point, "at"), "curve.at"), point) for point in points),
            key=lambda item: item[0],
        )
        values = []
        for _, point in ordered:
            value = _decimal(_field(point, metric), metric)
            if value < 0:
                raise DataError(f"{metric} must be nonnegative")
            values.append(_number((value - 1) * 100 if metric == "unit_value" else value, metric))
        figure.add_trace(
            go.Scatter(
                x=[at for at, _ in ordered],
                y=values,
                name=label,
                mode="lines",
                line={"color": color, "width": 2.5 if key == "strategy" else 1.8},
                hovertemplate=(
                    "%{x|%Y-%m-%d %H:%M} UTC<br>"
                    + (
                        "누적 수익률 %{y:+.2f}%"
                        if metric == "unit_value"
                        else "총 평가금액 %{y:,.0f}원"
                    )
                    + "<extra>%{fullData.name}</extra>"
                ),
            )
        )
    is_return = metric == "unit_value"
    prefix = (
        "합성 자료 · "
        if any("SYNTHETIC" in w for w in report.get("warnings", []))
        else "모의 결과 · "
    )
    _layout(
        figure,
        prefix + ("입출금 영향을 분리한 누적 수익률" if is_return else "총 평가금액 · 입금 포함"),
        "누적 수익률 (%)" if is_return else "총 평가금액 (원)",
    )
    figure.update_yaxes(
        ticksuffix="%" if is_return else "원", tickformat=".1f" if is_return else ",.0f"
    )
    return figure


def price_figure(
    bundle: Bundle,
    instrument_id: str,
    as_of: datetime | str,
    recommendation: dict | None = None,
    backtest: dict | None = None,
) -> go.Figure:
    """Draw available raw closes and explicitly labelled proposal/simulation markers."""
    cutoff = _instant(as_of, "as_of")
    instrument = next((i for i in bundle.instruments if i.instrument_id == instrument_id), None)
    if instrument is None:
        raise DataError("Chart instrument is absent from the dataset")
    if instrument.known_at > cutoff:
        raise DataError("Chart instrument metadata is not yet available")
    bars = sorted(
        (
            bar
            for bar in bundle.bars
            if bar.instrument_id == instrument_id
            and bar.available_at <= cutoff
            and bar.session_close_at <= cutoff
        ),
        key=lambda bar: (bar.session_date, bar.session_close_at),
    )
    currency = instrument.currency
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=[bar.session_close_at.astimezone(UTC) for bar in bars],
            y=[_number(_decimal(bar.close, "close", positive=True), "close") for bar in bars],
            customdata=[
                [bar.session_date.isoformat(), bar.available_at.astimezone(UTC).isoformat()]
                for bar in bars
            ],
            name="관측 종가 (원가격)",
            mode="lines",
            line={"color": "#2563eb", "width": 2},
            hovertemplate=(
                f"종가 %{{y:,.4f}} {currency}<br>세션 %{{customdata[0]}}"
                "<br>종료 %{x|%Y-%m-%d %H:%M} UTC<br>가용 %{customdata[1]}"
                "<extra>관측 원가격</extra>"
            ),
        )
    )
    if recommendation is not None:
        decision = _instant(_field(recommendation, "as_of"), "recommendation.as_of")
        if decision <= cutoff:
            actions = _records(_field(recommendation, "actions"), "recommendation.actions")
            reference_bars = [
                bar
                for bar in bars
                if bar.available_at <= decision and bar.session_close_at <= decision
            ]
            for action in actions:
                if action.get("instrument_id") != instrument_id:
                    continue
                side = _field(action, "side")
                if side not in SIDES:
                    raise DataError("Recommendation chart side must be BUY or SELL")
                if not reference_bars:
                    raise DataError(
                        "Recommendation has no observed reference price at its decision"
                    )
                reference = reference_bars[-1]
                price = _decimal(
                    _field(action, "reference_price_native"),
                    "reference_price_native",
                    positive=True,
                )
                if price != reference.close:
                    raise DataError(
                        "Recommendation reference price does not match its observed session"
                    )
                label, color, _ = SIDES[side]
                quantity = _number(
                    _decimal(_field(action, "quantity"), "quantity", positive=True), "quantity"
                )
                figure.add_trace(
                    go.Scatter(
                        x=[decision],
                        y=[_number(price, "reference_price_native")],
                        customdata=[[reference.session_date.isoformat(), quantity]],
                        name=f"미체결 {label} 제안",
                        mode="markers",
                        marker={
                            "symbol": "diamond-open",
                            "size": 13,
                            "color": color,
                            "line": {"width": 2},
                        },
                        hovertemplate=(
                            f"미체결 제안 · {label}<br>판단 %{{x|%Y-%m-%d %H:%M}} UTC"
                            f"<br>참조 종가 %{{y:,.4f}} {currency}<br>참조 세션 %{{customdata[0]}}"
                            "<br>제안 수량 %{customdata[1]:,g}주<extra>주문·체결 아님</extra>"
                        ),
                    )
                )
    if backtest is not None:
        strategy = _field(_field(backtest, "results"), "strategy")
        fills = []
        for event in _records(_field(strategy, "executions"), "strategy.executions"):
            if (
                event.get("status") != "simulated_fill"
                or event.get("instrument_id") != instrument_id
            ):
                continue
            at = _instant(_field(event, "at"), "execution.at")
            if at > cutoff:
                continue
            side = _field(event, "side")
            if side not in SIDES:
                raise DataError("Simulated fill chart side must be BUY or SELL")
            decision = _instant(_field(event, "decision_at"), "execution.decision_at")
            if decision >= at:
                raise DataError("Simulated fill must be strictly after its decision")
            price = _number(
                _decimal(_field(event, "price_native"), "price_native", positive=True),
                "price_native",
            )
            quantity = _number(
                _decimal(_field(event, "quantity"), "quantity", positive=True), "quantity"
            )
            fills.append((at, side, price, quantity, decision))
        for side, (label, color, symbol) in SIDES.items():
            selected = sorted((fill for fill in fills if fill[1] == side), key=lambda fill: fill[0])
            if not selected:
                continue
            figure.add_trace(
                go.Scatter(
                    x=[fill[0] for fill in selected],
                    y=[fill[2] for fill in selected],
                    customdata=[[fill[3], fill[4].isoformat()] for fill in selected],
                    name=f"모의 {label}",
                    mode="markers",
                    marker={"symbol": symbol, "size": 11, "color": color},
                    hovertemplate=(
                        f"모의 {label}<br>모의 체결 %{{x|%Y-%m-%d %H:%M}} UTC"
                        f"<br>원가격 %{{y:,.4f}} {currency}<br>수량 %{{customdata[0]:,g}}주"
                        "<br>판단 %{customdata[1]}<extra>백테스트 모의 체결</extra>"
                    ),
                )
            )
    prefix = "합성 자료 · " if bundle.manifest["kind"] == "synthetic" else ""
    _layout(
        figure,
        prefix + f"{instrument.name} ({instrument.symbol}) · 관측 원가격",
        f"원가격 ({currency})",
    )
    figure.update_layout(hovermode="closest")
    if not bars:
        figure.add_annotation(
            text="기준 시각까지 이용 가능한 종가가 없습니다",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
        )
    return figure
