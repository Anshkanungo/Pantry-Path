"""Gantt report: lane packing, summaries and SVG output from fake task timings."""

from datetime import UTC, datetime, timedelta

from pantrypath_pipeline.gantt import TaskBar, assign_lanes, render_svg, summarize, summary_markdown

T0 = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)


def bar(task, start_s, end_s, map_index=-1, state="success"):
    return TaskBar(task, map_index, T0 + timedelta(seconds=start_s),
                   T0 + timedelta(seconds=end_s), state)


def test_assign_lanes_reuses_a_lane_once_it_is_free():
    bars = [bar("r", 0, 10, 0), bar("r", 0, 10, 1), bar("r", 10, 20, 2), bar("r", 5, 15, 3)]
    assert assign_lanes(bars) == [0, 1, 0, 2]


def test_summarize_reports_wall_time_and_parallelism():
    bars = [bar("download_off", 0, 120), bar("download_foodcom", 0, 10),
            bar("refine_dish", 10, 40, 0), bar("refine_dish", 10, 40, 1),
            bar("refine_dish", 40, 70, 2), bar("notify", 120, 125)]
    s = summarize("dag", {"dag_run_id": "run1", "state": "success"}, bars)
    assert s["wall_s"] == 125
    assert s["tasks"]["refine_dish"] == {"instances": 3, "busy_s": 90.0, "span_s": 60.0,
                                         "max_parallel": 2}
    assert next(iter(s["tasks"])) == "download_off"  # slowest span first


def test_render_svg_draws_every_bar_and_marks_failures():
    bars = [bar("download_off", 0, 60), bar("refine_dish", 0, 30, 0),
            bar("refine_dish", 0, 30, 1, state="failed")]
    svg = render_svg("build_reference_data", bars)
    assert svg.startswith("<svg") and svg.count("<rect") == 1 + 3  # background + 3 bars
    assert "refine_dish ×2" in svg and "#dc2626" in svg and "1.0 min wall time" in svg


def test_summary_markdown_compares_two_runs():
    a = {"dag_id": "serial", "run_id": "r1", "state": "success", "wall_s": 600, "busy_s": 590,
         "tasks": {}}
    b = dict(a, dag_id="parallel", wall_s=300)
    md = summary_markdown([a, b])
    assert "| `serial` | `r1` | success | 10.0 min | 9.8 min |" in md
    assert "-50%" in md
