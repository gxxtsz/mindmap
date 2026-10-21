"""比较全量坐标与增量子树摘要，验证大图局部算法；不测完整 UI 交互。

运行：uv run --locked --all-packages python scripts/benchmark_incremental_layout.py
文字尺寸提前提供，全量参照关闭连接线路由；两侧均不包含排版、引擎事务和 Qt。
增量阶段只解析视口节点，完整几何展开单独计时并核对所有坐标。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from unittest.mock import patch

from benchmark import build_document
from mindmap_core import Rect
from mindmap_layouts.incremental import IncrementalTreeGeometry
from mindmap_layouts.tree import TreeLayout


def milliseconds(action):
    start = time.perf_counter()
    result = action()
    return (time.perf_counter() - start) * 1000, result


def summarize(values):
    return {"中位毫秒": round(statistics.median(values), 4), "最大毫秒": round(max(values), 4)}


def benchmark(count, samples):
    document = build_document(count)
    sizes = dict.fromkeys(document.nodes, (192.0, 42.0))
    cold, geometry = milliseconds(lambda: IncrementalTreeGeometry(document, sizes))
    branch = next(
        key for key in reversed(document.nodes)
        if len(document.nodes[key].children) == 5 and len(document.descendants(key)) == 6
    )
    initial = geometry.rectangle(branch)
    viewport = Rect(initial.x - 350, initial.y - 400, 1000, 800)
    full, local, queries, moved, updated, visited, counts = [], [], [], [], [], [], []
    layout = TreeLayout()
    # 只关闭参照算法的连接线路由，保留现有文档校验、树坐标和边界生成成本。
    with patch("mindmap_layouts.tree.route_branches", return_value={}):
        for index in range(samples):
            expanded = index % 2 == 1
            elapsed, _ = milliseconds(lambda: geometry.set_expanded(branch, expanded))
            local.append(elapsed)
            updated.append(geometry.last_updated_nodes)
            elapsed, visible = milliseconds(lambda: geometry.query(viewport))
            queries.append(elapsed)
            visited.append(geometry.last_visited_nodes)
            counts.append(len(visible))
            document.nodes[branch].expanded = expanded
            elapsed, expected = milliseconds(lambda: layout.layout(document, sizes))
            full.append(elapsed)
        flattened_ms, flattened = milliseconds(geometry.all_rectangles)
        for key, rect in expected.nodes.items():
            assert flattened[key] == rect
        assert flattened.keys() == expected.nodes.keys()
        keys = list(document.nodes)
        source, target = keys[-1], keys[-3]
        old = document.parents()[source]
        for _ in range(samples):
            elapsed, _ = milliseconds(lambda: geometry.move(source, target))
            moved.append(elapsed)
            geometry.move(source, old)
        document.nodes[old].children.remove(source)
        document.nodes[target].children.append(source)
        document.nodes[target].expanded = True
        geometry.move(source, target)
        expected = layout.layout(document, sizes)
        assert geometry.all_rectangles() == expected.nodes
    return {
        "节点数": count, "原型冷建索引毫秒": round(cold, 3),
        "全量坐标含校验不含连线": summarize(full), "局部展开收起摘要更新": summarize(local),
        "局部移动摘要更新": summarize(moved), "视口查询节点矩形": summarize(queries),
        "更新摘要节点数最大值": max(updated), "视口访问节点数最大值": max(visited),
        "视口返回节点数最大值": max(counts), "完整展开几何毫秒": round(flattened_ms, 3),
        "四方向一致性见自动化测试": True, "本次完整坐标与移动结果一致": True,
    }


def run():
    """单独测量几何内核；完整界面使用 benchmark_interactions，不能混用加速比。"""
    parser = argparse.ArgumentParser(description="增量子树几何内核基准")
    parser.add_argument("--nodes", type=int, nargs="+", default=[10000, 50000, 100000])
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--output", type=Path, default=Path(".artifacts/performance/增量算法实验.json"))
    args = parser.parse_args()
    if min(args.nodes) < 30 or args.samples < 2:
        parser.error("节点数至少 30，采样至少两次")
    report = {
        "范围": "单独测量已接入正式画布的几何内核；不含引擎事务、历史、排版、连线和 Qt 绘制。",
        "参照": "现有 TreeLayout 每次全量校验及生成所有可见节点矩形，关闭连接线路由。",
        "实验": "子树摘要增量传播、同级线段树、祖先坐标按需解析、分层视口裁剪。",
        "结果": [],
    }
    for count in args.nodes:
        row = benchmark(count, args.samples)
        report["结果"].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
