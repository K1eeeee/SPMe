from __future__ import annotations

from pybamm_w10.figures import plot_degradation_contributions


class RecordingAxes:
    def __init__(self) -> None:
        self.series = []

    def plot(self, x, y, *, label):
        self.series.append((list(x), list(y), label))


def test_degradation_plot_uses_lam_percentage_columns() -> None:
    rows = [{"cycle": "1", "negative_lam_pct": "1.5", "positive_lam_pct": "2.5"}]
    axes = RecordingAxes()
    plot_degradation_contributions(axes, rows)
    assert axes.series == [([1.0], [1.5], "negative_lam_pct"), ([1.0], [2.5], "positive_lam_pct")]
