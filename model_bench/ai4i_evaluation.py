"""Evaluate and export predictions on the project's fixed AI4I test split."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import csv
from io import BytesIO, StringIO
import json
from pathlib import Path
from threading import Event, Lock, Thread
from time import perf_counter
from zipfile import ZIP_DEFLATED, ZipFile

from ai4i_simulation import CASES
from ai4i_trial import build_state


LABELS = tuple(expected for _, expected, _ in CASES)
INVALID = "선택지 밖"
TEST_DATA = Path(__file__).with_name("ai4i_test_cases.json")


def score(rows):
    counts = Counter((row["expected"], row["prediction"]) for row in rows)
    expected_counts = Counter(row["expected"] for row in rows)
    predicted_counts = Counter(row["prediction"] for row in rows)
    classes = []
    for label in LABELS:
        correct = counts[label, label]
        total = expected_counts[label]
        predicted = predicted_counts[label]
        classes.append({
            "label": label, "correct": correct, "total": total,
            "precision": correct / predicted if predicted else 0.0,
            "recall": correct / total if total else 0.0,
            "f1": 2 * correct / (predicted + total) if predicted + total else 0.0,
        })
    return {
        "total": len(rows),
        "accuracy": sum(item["correct"] for item in classes) / len(rows),
        "macro_f1": sum(item["f1"] for item in classes) / len(classes),
        "invalid": predicted_counts[INVALID],
        "classes": classes,
    }


def csv_bytes(header, rows):
    stream = StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


class AI4IEvaluation:
    def __init__(self, trial, simulation):
        self.trial = trial
        self.simulation = simulation
        self.lock = Lock()
        self.cancel = Event()
        self.phase = "idle"
        self.message = "모델을 불러오고 Test 평가 시작을 누르세요."
        self.cases = []
        self.rows = []
        self.metrics = None
        self.completed_at = None
        self.dataset = {}
        try:
            with TEST_DATA.open(encoding="utf-8") as source:
                data = json.load(source)
            cases = data["cases"]
            if len(cases) != 80 or len({case["udi"] for case in cases}) != len(cases):
                raise ValueError("시험 데이터는 서로 다른 80건이어야 합니다.")
            if {case["expected"] for case in cases} != set(LABELS):
                raise ValueError("시험 데이터의 정답 유형을 확인하세요.")
            self.dataset = data["source"]
            self.cases = cases
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.phase = "error"
            self.message = f"시험 데이터를 읽을 수 없습니다. {exc}"

    def snapshot(self):
        model = self.simulation.snapshot()["model"]
        with self.lock:
            return {"model": model, "phase": self.phase, "message": self.message,
                    "dataset": self.dataset, "total": len(self.cases),
                    "completed": len(self.rows), "metrics": self.metrics}

    def is_running(self):
        with self.lock:
            return self.phase == "running"

    def start(self):
        if not self.cases:
            raise ValueError(self.message)
        model = self.simulation.snapshot()
        if model["model"] != "ready":
            raise ValueError("모델을 먼저 불러오세요.")
        if model["active"]:
            raise ValueError("시연이 끝나거나 중지된 뒤 평가를 시작하세요.")
        if self.trial.snapshot()["phase"] == "working":
            raise ValueError("수동 판단이 끝난 뒤 평가를 시작하세요.")
        if set(self.trial.questions["diagnosis"]["criteria"]) != set(LABELS):
            raise ValueError("이 평가에 사용할 모델에는 AI4I의 다섯 가지 분류 선택지가 필요합니다.")
        with self.lock:
            if self.phase == "running":
                raise ValueError("Test 평가가 이미 진행 중입니다.")
            self.rows = []
            self.metrics = None
            self.completed_at = None
            self.phase = "running"
            self.message = "Test 사례를 평가 중입니다."
        Thread(target=self._run, daemon=True).start()
        return self.snapshot()

    def _run(self):
        try:
            for case in self.cases:
                if self.cancel.is_set():
                    return
                state = build_state(case["sensors"])
                start = perf_counter()
                answer = self.trial.predict(state, self.cancel, allow_invalid=True)
                latency = (perf_counter() - start) * 1000
                if self.cancel.is_set():
                    return
                row = {"udi": case["udi"], "expected": case["expected"],
                       "prediction": answer if answer in LABELS else INVALID,
                       "latency_ms": round(latency, 3)}
                with self.lock:
                    self.rows.append(row)
            with self.lock:
                self.metrics = score(self.rows)
                self.completed_at = datetime.now(timezone.utc).isoformat()
                self.phase = "complete"
                self.message = "Test 평가 완료"
        except Exception as exc:
            with self.lock:
                self.phase = "error"
                self.message = f"평가에 실패했습니다 ({type(exc).__name__}). {exc}"

    def export(self):
        with self.lock:
            if self.phase != "complete":
                raise ValueError("Test 평가가 완료된 뒤 결과를 내보낼 수 있습니다.")
            rows = list(self.rows)
            report = {"model": self.trial.model_path.name, "dataset": self.dataset,
                      "completed_at": self.completed_at, "metrics": self.metrics}
        predictions = ("udi", "expected", "prediction", "latency_ms")
        class_fields = ("label", "correct", "total", "precision", "recall", "f1")
        counts = Counter((row["expected"], row["prediction"]) for row in rows)
        columns = (*LABELS, INVALID)
        data = BytesIO()
        with ZipFile(data, "w", ZIP_DEFLATED) as archive:
            archive.writestr("metrics.json", json.dumps(report, ensure_ascii=False, indent=2))
            archive.writestr("predictions.csv", csv_bytes(
                predictions, ([row[key] for key in predictions] for row in rows)))
            archive.writestr("class_metrics.csv", csv_bytes(
                class_fields, ([item[key] for key in class_fields] for item in report["metrics"]["classes"])))
            archive.writestr("confusion_matrix.csv", csv_bytes(
                ("실제 상태 / 예측 상태", *columns),
                ([label, *(counts[label, predicted] for predicted in columns)] for label in LABELS)))
        return data.getvalue()
