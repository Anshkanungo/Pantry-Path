"""Gantt charts of finished Airflow runs, for the pipeline-optimization write-up.

Reads task timings from the Airflow REST API and writes, for each DAG, an SVG chart
(reports/gantt_<dag_id>.svg) plus reports/gantt_summary.{md,json} comparing the runs.
Mapped tasks (one per dish) share one row, split into lanes, so the chart shows how
many ran at once. Airflow's own Gantt view (Grid -> run -> Gantt) shows the same data.

    python -m pantrypath_pipeline.gantt build_reference_data_serial build_reference_data

AIRFLOW_URL, AIRFLOW_USER and AIRFLOW_PASSWORD override the local defaults.
"""

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime
from html import escape

import requests

from pantrypath_pipeline.config import load_config

FINISHED = ("success", "failed")
# Bar colours per branch: OFF, Food.com, LLM, shared tail
COLOURS = {"off": "#3b82f6", "foodcom": "#16a34a", "llm": "#f59e0b", "tail": "#6b7280"}
BRANCH = {
    "download_off": "off", "filter_off": "off", "validate_off": "off",
    "download_foodcom": "foodcom", "clean_recipes": "foodcom", "group_dishes": "foodcom",
    "select_dishes": "foodcom", "dish_ids": "foodcom",
    "refine_dish": "llm", "assemble_catalog": "llm", "validate_catalog": "llm",
}


@dataclass
class TaskBar:
    task_id: str
    map_index: int
    start: datetime
    end: datetime
    state: str


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


class AirflowAPI:
    def __init__(self, url: str, user: str, password: str):
        self.url = url.rstrip("/")
        token = requests.post(
            f"{self.url}/auth/token", json={"username": user, "password": password}, timeout=15
        ).json()["access_token"]
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str, **params) -> dict:
        resp = self.session.get(f"{self.url}/api/v2{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def latest_finished_run(self, dag_id: str) -> dict:
        runs = self.get(f"/dags/{dag_id}/dagRuns", order_by="-start_date", limit=20)["dag_runs"]
        for run in runs:
            if run["state"] in FINISHED:
                return run
        raise SystemExit(f"{dag_id} has no finished run yet")

    def task_bars(self, dag_id: str, run_id: str) -> list[TaskBar]:
        bars, offset = [], 0
        while True:
            page = self.get(f"/dags/{dag_id}/dagRuns/{run_id}/taskInstances",
                            limit=100, offset=offset)
            for ti in page["task_instances"]:
                start, end = _parse(ti.get("start_date")), _parse(ti.get("end_date"))
                if start and end:
                    bars.append(TaskBar(ti["task_id"], ti.get("map_index", -1), start, end,
                                        ti["state"]))
            offset += len(page["task_instances"])
            if not page["task_instances"] or offset >= page["total_entries"]:
                return bars


def assign_lanes(bars: list[TaskBar]) -> list[int]:
    """Greedy interval packing: each bar goes to the first lane free at its start."""
    lane_ends: list[datetime] = []
    lanes = []
    for bar in bars:
        for i, end in enumerate(lane_ends):
            if end <= bar.start:
                lane_ends[i] = bar.end
                lanes.append(i)
                break
        else:
            lane_ends.append(bar.end)
            lanes.append(len(lane_ends) - 1)
    return lanes


def summarize(dag_id: str, run: dict, bars: list[TaskBar]) -> dict:
    run_start = min(b.start for b in bars)
    run_end = max(b.end for b in bars)
    per_task: dict[str, dict] = {}
    for b in bars:
        t = per_task.setdefault(b.task_id, {"instances": 0, "busy_s": 0.0,
                                             "first_start": b.start, "last_end": b.end})
        t["instances"] += 1
        t["busy_s"] += (b.end - b.start).total_seconds()
        t["first_start"] = min(t["first_start"], b.start)
        t["last_end"] = max(t["last_end"], b.end)
    tasks = {
        task: {"instances": t["instances"], "busy_s": round(t["busy_s"], 1),
               "span_s": round((t["last_end"] - t["first_start"]).total_seconds(), 1),
               "max_parallel": max(assign_lanes(sorted(
                   (b for b in bars if b.task_id == task), key=lambda b: b.start))) + 1}
        for task, t in per_task.items()
    }
    return {
        "dag_id": dag_id, "run_id": run["dag_run_id"], "state": run["state"],
        "wall_s": round((run_end - run_start).total_seconds(), 1),
        "busy_s": round(sum(t["busy_s"] for t in tasks.values()), 1),
        "tasks": dict(sorted(tasks.items(), key=lambda kv: -kv[1]["span_s"])),
    }


def render_svg(dag_id: str, bars: list[TaskBar], width: int = 1000) -> str:
    """One row per task (mapped tasks split into lanes), x axis in minutes since start."""
    order: list[str] = []
    for b in sorted(bars, key=lambda b: b.start):
        if b.task_id not in order:
            order.append(b.task_id)
    run_start = min(b.start for b in bars)
    total = max((max(b.end for b in bars) - run_start).total_seconds(), 1.0)
    label_w, lane_h, pad, top = 170, 14, 6, 52  # title row, then the minute labels
    plot_w = width - label_w - 20

    def x(t: datetime) -> float:
        return label_w + (t - run_start).total_seconds() / total * plot_w

    rows, y = [], top
    for task in order:
        task_bars = sorted((b for b in bars if b.task_id == task), key=lambda b: b.start)
        lanes = assign_lanes(task_bars)
        n_lanes = max(lanes) + 1
        colour = COLOURS[BRANCH.get(task, "tail")]
        label = task if n_lanes == 1 and len(task_bars) == 1 else (
            f"{task} ×{len(task_bars)}")
        rows.append(f'<text x="{label_w - 8}" y="{y + n_lanes * lane_h / 2 + 4}" '
                    f'text-anchor="end">{escape(label)}</text>')
        for bar, lane in zip(task_bars, lanes, strict=True):
            bx, bw = x(bar.start), max(x(bar.end) - x(bar.start), 1.0)
            fill = colour if bar.state == "success" else "#dc2626"
            rows.append(f'<rect x="{bx:.1f}" y="{y + lane * lane_h + 1}" width="{bw:.1f}" '
                        f'height="{lane_h - 2}" fill="{fill}" rx="2"/>')
        y += n_lanes * lane_h + pad
    minutes = total / 60
    step = 1 if minutes <= 12 else 5 if minutes <= 60 else 10
    ticks = []
    for m in range(0, int(minutes) + 1, step):
        tx = label_w + m * 60 / total * plot_w
        ticks.append(f'<line x1="{tx:.1f}" y1="{top - 6}" x2="{tx:.1f}" y2="{y}" '
                     f'stroke="#e5e7eb"/><text x="{tx:.1f}" y="{top - 10}" '
                     f'text-anchor="middle">{m} min</text>')
    title = f"{dag_id}: {minutes:.1f} min wall time"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{y + 10}" '
        f'font-family="Helvetica, Arial, sans-serif" font-size="11">'
        f'<rect width="100%" height="100%" fill="#ffffff"/>'
        f'<text x="10" y="18" font-size="13" font-weight="bold">{escape(title)}</text>'
        + "".join(ticks) + "".join(rows) + "</svg>\n"
    )


def summary_markdown(summaries: list[dict]) -> str:
    lines = ["# Pipeline runs: Gantt summary", "",
             "| DAG | Run | State | Wall time | Task time (sum) |",
             "|---|---|---|---|---|"]
    for s in summaries:
        lines.append(f"| `{s['dag_id']}` | `{s['run_id']}` | {s['state']} | "
                     f"{s['wall_s'] / 60:.1f} min | {s['busy_s'] / 60:.1f} min |")
    if len(summaries) == 2 and summaries[1]["wall_s"]:
        saved = 1 - summaries[1]["wall_s"] / summaries[0]["wall_s"]
        lines += ["", f"Wall time change from the first to the second run: {-saved:+.0%}."]
    for s in summaries:
        lines += ["", f"## `{s['dag_id']}`: slowest tasks", "",
                  "| Task | Instances | Span | Busy time | Max parallel |", "|---|---|---|---|---|"]
        for task, t in list(s["tasks"].items())[:6]:
            lines.append(f"| `{task}` | {t['instances']} | {t['span_s'] / 60:.1f} min | "
                         f"{t['busy_s'] / 60:.1f} min | {t['max_parallel']} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dag_ids", nargs="+", help="DAGs to chart (latest finished run each)")
    args = parser.parse_args(argv)
    api = AirflowAPI(os.environ.get("AIRFLOW_URL", "http://localhost:8080"),
                     os.environ.get("AIRFLOW_USER", "airflow"),
                     os.environ.get("AIRFLOW_PASSWORD", "airflow"))
    cfg = load_config()
    out = cfg.path(cfg["paths"]["reports"])
    summaries = []
    for dag_id in args.dag_ids:
        run = api.latest_finished_run(dag_id)
        bars = api.task_bars(dag_id, run["dag_run_id"])
        (out / f"gantt_{dag_id}.svg").write_text(render_svg(dag_id, bars))
        summaries.append(summarize(dag_id, run, bars))
        print(f"{dag_id}: {summaries[-1]['wall_s'] / 60:.1f} min -> gantt_{dag_id}.svg")
    (out / "gantt_summary.json").write_text(json.dumps(summaries, indent=2, default=str))
    (out / "gantt_summary.md").write_text(summary_markdown(summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
